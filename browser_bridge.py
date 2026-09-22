"""Everything the three browser engines share, so they cannot drift apart.

The engines differ only in how they start a browser and how they ask it for
a URL and for `document.documentElement.outerHTML`. Everything that decides
what a run DOES — the challenge ladder, pagination, the terminating
condition, the exit codes — lives here, once.

Deliberately NO JavaScript crosses this boundary. Selenium's `execute_script`
takes a function BODY with an explicit `return`, while Playwright and
pyppeteer take `() => expr`; a shared module that passed JS would quietly
acquire one driver's dialect. The driver protocol below names OPERATIONS
instead (`content()`, `count(selector)`), and each engine writes its own
one-liner (CLAUDE.md §1).

The challenge ladder on this site
---------------------------------
Rung 0 is `http_scraper.py` and is not reached from here.

Rung 1 — **the browser itself**, and on homedepot.com this is the rung that
actually works. Akamai's interstitial is a JS sensor: it scores how the
client executes, then reloads itself. A real browser clears it with no help,
which is why `wait_out_challenge` below waits and re-reads rather than
reaching for anything paid.

Rung 2 — the **2Captcha solver API**, for a real captcha with a sitekey.
Measured 2026-09-22: no such widget appears on a listing or product page, so
this rung exists for the case where one starts appearing, not because one
did. `--solve-captcha when-blocked` (the default) keeps it a fallback: it
fires only after the browser has failed to clear the page, and it counts
products on the spot rather than running the full readiness wait first.

A 403 is NOT on this ladder at all. It is a decision about the address, has
no puzzle in it, and the only useful response is a different exit.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import env_config
import output_writer
import page_flow
import product_parser as P
import proxy_pool
from output_writer import EXIT_REMOTE_API_ERROR, Product, finish_run

logger = logging.getLogger("homedepot")

_CREDENTIALS_IN_URL_RE = re.compile(r"(\w+://)[^/@\s]+:[^/@\s]+@")

DEFAULT_OUT = "homedepot_products"

# How many product links mean "the grid has painted". Must be > 1: waiting for
# one match resolves on an unrelated `/p/` link (a recommendation strip, a
# footer promo) long before the grid itself paints.
MIN_CARD_MATCHES = 4
CARD_SELECTOR = 'a[href*="/p/"]'

# Ordered MOST DURABLE FIRST: a standards-based signal before a build
# artefact. In practice this site is paged by `?Nao=`, which
# `product_parser.page_url` constructs, so a missing next-link is not fatal —
# but a selector that agrees is worth having, and one that has silently died
# must not be the only thing pagination rests on (CLAUDE.md §7).
NEXT_PAGE_SELECTOR = (
    'head link[rel="next"]',
    'a[aria-label="Next"]',
    'a[data-testid="pagination-next"]',
)

CHALLENGE_SETTLE_MS = 25000
CHALLENGE_POLL_MS = 1000


def mask_text(text: str) -> str:
    """Redact `user:pass@` inside any URL in free text, keeping the text.

    NOT `proxy_pool.mask()`, which is for a URL: handed an exception message
    that one returns "?://?" and destroys it.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


class BridgeError(RuntimeError):
    """The BROWSER could not do its job — it never got an answer from the site.

    Deliberately distinct from a page that came back and said no. A navigation
    timeout, a dropped CDP socket and a dead exit are transport faults: the
    site never refused us, our own plumbing failed. Mapping them to `blocked`
    (exit 3) tells a consumer the site is refusing this scraper, which sends
    the reader to the anti-bot problem when the actual answer is "the remote
    browser timed out, run it again".

    They map to EXIT_REMOTE_API_ERROR (5) instead. The family states the same
    rule for proxies: a dead proxy and a timeout want opposite responses.
    """


# ---------------------------------------------------------------------------
# The driver protocol
# ---------------------------------------------------------------------------
#
# An engine supplies an object with:
#   name          str
#   start()       bring a browser up
#   navigate(url) -> Optional[int]   HTTP status if the engine can see one
#   content()     -> str             the rendered document
#   count(sel)    -> int             matches for a CSS selector
#   attr(sel, a)  -> Optional[str]   one attribute off the first match
#   sleep(ms)
#   stop()
#
# Nothing here calls anything else on it.


def wait_for_content(driver, args, url: str, timeout_ms: int, log=None) -> str:
    """Wait, bounded, for the page to be ready. Returns what is there at the end.

    The anchor DEPENDS ON THE MODE, and getting that wrong is expensive
    rather than wrong: a product page holds one product, so waiting for
    `MIN_CARD_MATCHES` product links on it never succeeds and every PDP pays
    the full timeout before returning a page that was ready in a second.
    Measured: 45,000 ms per product page before this branch existed.

    Returns rather than raises on a timeout — the caller classifies the
    document, and "did not paint" and "was refused" want different exit codes.
    """
    waited = 0
    step = 500
    want_cards = MIN_CARD_MATCHES if args.mode == "category" else 1
    while waited < timeout_ms:
        try:
            if args.mode == "product":
                # A PDP is ready when its own structured data is present.
                if P.jsonld_product(driver.content() or "") is not None:
                    break
            elif driver.count(CARD_SELECTOR) >= want_cards:
                break
        except Exception:  # a driver may throw mid-navigation
            pass
        driver.sleep(step)
        waited += step
    html = driver.content()
    if log:
        log("  readiness wait: %dms, %d bytes" % (waited, len(html or "")))
    return html or ""


def wait_out_challenge(driver, url: str, log=None,
                       budget_ms: int = CHALLENGE_SETTLE_MS) -> tuple:
    """Let Akamai's interstitial clear itself. Returns `(html, cleared)`.

    This is the whole of rung 1 and it is deliberately passive. The
    interstitial runs a sensor script and then reloads the page on its own;
    clicking anything, or reloading it ourselves, does not make that faster
    and a reload can restart the scoring. So: wait, re-read, and classify.

    Measured 2026-09-22 — the interstitial is ~2.5 KB and arrives with HTTP
    **200**, so an engine that only watches the status code sees a successful
    fetch of a page with no products on it.
    """
    waited = 0
    latest = driver.content() or ""
    while waited < budget_ms:
        driver.sleep(CHALLENGE_POLL_MS)
        waited += CHALLENGE_POLL_MS
        fresh = driver.content() or ""
        if fresh:
            latest = fresh
        state = P.detect_page_state(latest, url=url)
        if state in ("content", "product"):
            if log:
                log("  challenge cleared after %.0fs (%d bytes)"
                    % (waited / 1000.0, len(latest)))
            return latest, True
    if log:
        log("  challenge did not clear within %.0fs (last document %d bytes) — "
            "reporting it rather than parsing it" % (budget_ms / 1000.0, len(latest)))
    return latest, False


def solve_if_captcha(driver, args, url: str, html: str, log=None) -> Optional[str]:
    """Rung 2. Returns fresh HTML if a solve happened, else None.

    A missing key or a solver error is a WARNING, not a crash: the run
    continues and reports exit 3 if it really was blocked (CLAUDE.md §8).
    """
    if getattr(args, "solve_captcha", "when-blocked") == "never":
        return None
    if P.detect_page_state(html, url=url) != "captcha":
        return None
    key = getattr(args, "twocaptcha_key", None)
    if not key:
        print("[!] a captcha was detected but no TWOCAPTCHA_KEY is set — "
              "continuing without solving.", file=sys.stderr)
        return None
    try:
        import captcha_solver
    except ImportError as exc:
        print("[!] captcha detected but the solver is unavailable: %s" % exc,
              file=sys.stderr)
        return None
    try:
        solved = captcha_solver.solve_on_page(
            driver, html, url=url, api_key=key,
            api_version=getattr(args, "captcha_api", "v2"),
            min_score=getattr(args, "min_score", 0.7))
    except Exception as exc:  # noqa: BLE001 — a solver failure is not a crash
        print("[!] solver failed: %s: %s"
              % (type(exc).__name__, mask_text(str(exc))), file=sys.stderr)
        return None
    if not solved:
        return None
    if log:
        log("  solver returned a token; re-reading the page")
    driver.sleep(3000)
    return driver.content()


def fetch_page(driver, args, url: str, log=None) -> Dict[str, Any]:
    """One page through a browser, with the ladder applied.

    Returns `{"html", "state", "status"}`.
    """
    status = None
    try:
        status = driver.navigate(url)
    except Exception as exc:  # noqa: BLE001
        raise BridgeError("navigation failed: %s: %s"
                          % (type(exc).__name__, mask_text(str(exc)))) from None

    html = wait_for_content(driver, args, url,
                            getattr(args, "timeout", 45) * 1000, log=log)
    state = P.detect_page_state(html, status=status, url=url)

    if state == "challenge":
        if log:
            log("  Akamai interstitial (HTTP %s) — letting the browser clear it"
                % status)
        html, cleared = wait_out_challenge(driver, url, log=log)
        state = P.detect_page_state(html, url=url)
        if cleared:
            html = wait_for_content(driver, args, url, 10000, log=log)
            state = P.detect_page_state(html, url=url)

    if state == "captcha":
        fresh = solve_if_captcha(driver, args, url, html, log=log)
        if fresh:
            html = fresh
            state = P.detect_page_state(html, url=url)

    return {"html": html, "state": state, "status": status}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(args, driver) -> int:
    """Drive one browser engine end to end and return its exit code."""
    log = (lambda m: print(m, file=sys.stderr)) if getattr(args, "verbose", False) \
        else (lambda m: None)

    url = args.url
    if not url:
        raise SystemExit(
            "nothing to fetch: pass --url with a leaf category or product "
            "address, e.g. https://www.homedepot.com/b/"
            "Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7")
    ok, reason = P.supported_host(url)
    if not ok:
        raise SystemExit("refusing %s: %s" % (url, reason))

    facts: Dict[str, Any] = {
        "transport": driver.name,
        "pages_requested": args.pages,
        "pages_completed": 0,
        "failed_pages": [],
        "states": [],
        "blocked": False,
        "start_url": url,
        "final_url": url,
    }
    rows: List[Product] = []
    seen: set = set()

    try:
        driver.start()
    except Exception as exc:  # noqa: BLE001
        print("could not start %s: %s: %s"
              % (driver.name, type(exc).__name__, mask_text(str(exc))),
              file=sys.stderr)
        return EXIT_REMOTE_API_ERROR

    try:
        if args.mode == "product":
            result = fetch_page(driver, args, url, log=log)
            facts["states"].append(result["state"])
            _dump(args, result["html"], 1)
            if page_flow.counts_as_blocked(result["state"]):
                facts["blocked"] = True
                facts["failed_pages"].append(1)
            else:
                rows = P.parse_product(result["html"], url=url,
                                       category=args.category)
                facts["pages_completed"] = 1
        else:
            for page in range(1, args.pages + 1):
                target = P.page_url(url, page)
                log("page %d: %s" % (page, target))
                result = fetch_page(driver, args, target, log=log)
                state = result["state"]
                facts["states"].append(state)
                _dump(args, result["html"], page)

                if page_flow.counts_as_blocked(state):
                    facts["blocked"] = True
                    facts["failed_pages"].append(page)
                    print("page %d: %s (HTTP %s, %d bytes)"
                          % (page, state, result["status"], len(result["html"])),
                          file=sys.stderr)
                    break
                if state == "hub":
                    print("page %d: that URL is a category HUB, not a product "
                          "listing — it has no grid. Pick a leaf category."
                          % page, file=sys.stderr)
                    facts["failed_pages"].append(page)
                    break

                blob = P.apollo_state(result["html"])
                if page == 1 and blob:
                    store = P.store_context(blob)
                    facts.update({k: store.get(k) for k in store})
                    report = P.search_report(blob)
                    facts["total_products_reported"] = report.get("totalProducts")

                page_rows = P.parse_category(result["html"], url=target,
                                             category=args.category)
                for row in page_rows:
                    row.page = page
                fresh = output_writer.dedupe_by_sku(page_rows, seen)
                rows.extend(fresh)
                facts["pages_completed"] = page
                log("  %s: %d rows (%d new, %d total)"
                    % (state, len(page_rows), len(fresh), len(rows)))

                if not page_rows or not fresh:
                    log("  page %d added no new skus — end of listing." % page)
                    break
                if args.max_products and len(rows) >= args.max_products:
                    rows = rows[:args.max_products]
                    break
                if page < args.pages:
                    time.sleep(args.delay)
    except BridgeError as exc:
        # A transport fault, NOT a refusal — see BridgeError. The run reports
        # exit 5 and does not claim the site blocked it.
        print("[!] %s" % exc, file=sys.stderr)
        facts["transport_error"] = str(exc)
        try:
            driver.stop()
        except Exception:
            pass
        return EXIT_REMOTE_API_ERROR
    except Exception as exc:  # noqa: BLE001
        print("[!] %s: %s" % (type(exc).__name__, mask_text(str(exc))),
              file=sys.stderr)
        return EXIT_REMOTE_API_ERROR
    finally:
        try:
            driver.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s did not shut down cleanly: %s",
                           driver.name, mask_text(str(exc)))

    # Whether rung 1a did anything is a FACT about the run, so it goes in the
    # sidecar rather than only into a log line nobody kept.
    facts["autosolve_armed"] = getattr(driver, "autosolve_armed", False)
    facts["autosolve_events"] = getattr(driver, "autosolve_events", [])
    facts["autosolve_notes"] = getattr(driver, "autosolve_notes", [])

    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=facts["blocked"],
        stop_reason=_stop_reason(args, facts),
        pages_requested=args.pages,
        pages_completed=facts["pages_completed"],
        pages_failed=facts["failed_pages"],
        mode=args.mode,
        start_url=facts["start_url"], final_url=facts["final_url"],
        extra=facts,
    )


def _stop_reason(args, facts: Dict[str, Any]) -> str:
    if facts["blocked"]:
        return "blocked"
    if "hub" in facts.get("states", []):
        return "hub_not_a_listing"
    if args.mode == "product":
        return "single_page_mode"
    if facts["pages_completed"] >= args.pages:
        return "completed"
    return "no_new_products"


def _dump(args, html: str, page: int) -> None:
    if not getattr(args, "dump_html", None):
        return
    import os
    path = args.dump_html
    root, ext = os.path.splitext(path)
    path = "%s_p%d%s" % (root, page, ext or ".html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html or "")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI, defined once for all three engines
# ---------------------------------------------------------------------------

def _positive_int(value):
    import argparse
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %s" % value)
    return number


def _non_negative_float(value):
    import argparse
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


def build_parser(description: str):
    """Every flag the browser engines share, defined ONCE.

    `smoke_test.test_engine_parity` compares the three engines against this
    one definition instead of three hand-kept copies.
    """
    import argparse
    p = argparse.ArgumentParser(
        description=description,
        epilog="Credentials belong in .env, never on a command line — `ps` "
               "reads argv and argv lands in shell history.")
    p.add_argument("--url", default=None,
                   help="A leaf category or product URL on www.homedepot.com.")
    p.add_argument("--mode", choices=["category", "product"], default="category")
    p.add_argument("--category", default=None)
    p.add_argument("--pages", type=_positive_int, default=1)
    p.add_argument("--max-products", type=_positive_int, default=None)
    p.add_argument("--delay", type=_non_negative_float, default=1.0)
    p.add_argument("--timeout", type=_positive_int, default=45)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP instead of "
                        "launching one. Falls back to HOMEDEPOT_CDP_ENDPOINT "
                        "in .env — never pass this on a command line, it "
                        "contains a password.")
    p.add_argument("--proxy", default=None,
                   help="Prefer HOMEDEPOT_PROXY in .env. Ignored with "
                        "--cdp-endpoint: the remote browser brings its own "
                        "exit and stacking a second is a contradiction, not "
                        "better cover.")
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=proxy_pool.ROTATE_MODES,
                   default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--twocaptcha-key", default=None,
                   help="Prefer TWOCAPTCHA_KEY in .env — `ps` reads argv.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always", "never"],
                   default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    return p


def parse_args(parser, argv=None):
    args = parser.parse_args(argv)
    env_config.apply(args)
    if args.cdp_endpoint and args.proxy:
        print("[!] --proxy is ignored with --cdp-endpoint: the remote browser "
              "brings its own exit.", file=sys.stderr)
        args.proxy = None
    return args
