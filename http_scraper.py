#!/usr/bin/env python3
"""homedepot.com scraper — the plain HTTP transport.

Two axes, both required
-----------------------
Measured 2026-09-22. `www.homedepot.com` needs a US exit AND a browser-shaped
request, and neither alone is enough:

    bare UA,      Moscow residential  ->  403   367 b
    bare UA,      US residential      ->  403   371 b   (robots.txt: 200)
    full headers, Moscow residential  ->  403   367 b
    full headers, US residential      ->  200   172 KB

And a third axis nobody expects: THE HTTP CLIENT ITSELF
-------------------------------------------------------
Satisfying both of the above is still not enough, and this is the finding that
shapes this module. Paired measurement, the SAME six session-pinned exits, the
same headers, the same minute:

    system curl, HTTP/2      4 content, 2 interstitial, 0 refused
    python-requests, HTTP/1.1               6 of 6 REFUSED (403, 2410 b)

`requests` never once fetched a listing page from this site. Neither did
`httpx` nor `curl_cffi` on seven browser-impersonation profiles — those two
speak HTTP/2 and still got the same branded 403, so it is not simply
"HTTP/1.1 is refused" either. Forcing curl itself to HTTP/1.1 got the
interstitial rather than content, so the version matters too. Which part of
the handshake is actually being scored has NOT been isolated, and this module
does not pretend to know.

What it does instead is use the client that was measured to work. `--http-client
curl` (the default where curl is on PATH) shells out; `requests` is kept
because it is the only option when curl is absent, and because it fetches the
SITEMAPS perfectly well — those are static XML and are not behind the bot
manager, which is why `catalog_walk.py` pulled 5,304 category URLs through it
without trouble.

Credentials never reach argv, including curl's. The proxy is passed on curl's
stdin via `--config -`, so `ps` sees no password.

The wall here is Akamai and it is an ADDRESS/CLIENT decision, not a puzzle. No
captcha solver can clear a 403 — see README.
"""


from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import env_config
import output_writer
import page_flow
import product_parser as P
import proxy_pool
from output_writer import (EXIT_BLOCKED, EXIT_NO_PRODUCTS, Product, finish_run)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


DEFAULT_OUT = "homedepot_products"

# The header set that turns 403 into 200. Every one of these was present in
# the measurement above; they have NOT been bisected to find a minimal set,
# and the honest thing is to send what was measured rather than to guess which
# ones Akamai actually scores.
#
# The user agent is a literal here and that is a deliberate exception to the
# family rule "the UA comes from the browser, not a literal" (CLAUDE.md §8):
# there is no browser in this transport to ask. It is kept consistent with the
# `sec-ch-ua` version below, because a Chrome 140 UA beside a Chrome 133 client
# hint is itself a mismatch — if you bump one, bump both.
CHROME_MAJOR = "140"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36" % CHROME_MAJOR
)
BROWSER_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": ('"Chromium";v="%s", "Not=A?Brand";v="24", '
                  '"Google Chrome";v="%s"' % (CHROME_MAJOR, CHROME_MAJOR)),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": USER_AGENT,
}


CURL_AVAILABLE = shutil.which("curl") is not None


def default_http_client() -> str:
    """`curl` where it exists, else `requests`.

    Defaulting to the one that was MEASURED to work rather than to the one
    that is a library. See the module docstring: `requests` was refused on 6
    of 6 exits where curl got content on 4 of the same 6.
    """
    return "curl" if CURL_AVAILABLE else "requests"


class CurlSession:
    """A `requests.Session`-shaped wrapper around the system curl binary.

    Only `.get()` is implemented, because that is all this module asks for.

    **The proxy never touches argv.** curl's `--proxy` would put the password
    where `ps` can read it, so the proxy line (and nothing else) is written to
    curl's own config format and piped in on stdin via `--config -`. That is
    curl's documented mechanism for exactly this.
    """

    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.headers = dict(BROWSER_HEADERS)

    def get(self, url: str, timeout: int = 60, allow_redirects: bool = True):
        header_args = []
        for name, value in self.headers.items():
            header_args += ["-H", "%s: %s" % (name, value)]
        command = [
            "curl", "--silent", "--show-error",
            # HTTP/2 explicitly: forcing curl to 1.1 got the interstitial
            # instead of content in the same measurement.
            "--http2",
            "--compressed",
            "--max-time", str(timeout),
            "--write-out", "\n%{http_code} %{url_effective}",
            "--config", "-",
        ]
        if allow_redirects:
            command.append("--location")
        command += header_args + [url]

        config = "proxy = \"%s\"\n" % self.proxy if self.proxy else ""
        try:
            completed = subprocess.run(
                command, input=config, capture_output=True, text=True,
                timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            raise CurlError("curl timed out after %ds" % (timeout + 15)) from None
        if completed.returncode != 0:
            message = _mask_text(completed.stderr.strip()[:300])
            # curl 56 with a 407, or an explicit auth complaint, is the PROXY
            # refusing us — not the site. See ProxyAuthError.
            if completed.returncode in (56, 7) and "407" in message:
                raise ProxyAuthError(message)
            raise CurlError("curl exited %d: %s"
                            % (completed.returncode, message))
        body, _, trailer = completed.stdout.rpartition("\n")
        if trailer.strip().startswith("407"):
            raise ProxyAuthError("the proxy refused the credential (HTTP 407)")
        parts = trailer.split(" ", 1)
        try:
            status = int(parts[0])
        except (ValueError, IndexError):
            raise CurlError("could not read curl's status line: %r"
                            % trailer[:120]) from None
        final_url = parts[1] if len(parts) > 1 else url
        return _CurlResponse(status, body, final_url)


class CurlError(RuntimeError):
    pass


class ProxyAuthError(CurlError):
    """The PROXY refused our credential — the site was never reached.

    Distinct from every other failure in this module for the same reason a
    navigation timeout is distinct from a block: it is a fault in our own
    plumbing, not a decision by homedepot.com, and the two want opposite
    responses. Reporting it as exit 3 sends the reader to the anti-bot
    problem — new exits, longer backoff, a browser — when the actual answer
    is "the credential in HOMEDEPOT_PROXY no longer authenticates".

    Rotating an exit cannot help either: every exit on a dead credential
    fails the same way, so a run that retried would burn its whole budget
    discovering that. It is raised rather than retried.
    """


class _CurlResponse:
    """Just enough of a `requests.Response` for `fetch()`.

    Response headers are deliberately NOT collected: curl would need `-D` to a
    file or `-i` inline, and inline would mean splitting headers out of a
    1.35 MB body on every page for information this module's classifier does
    not need — `detect_page_state` takes `headers` as optional precisely
    because not every transport can supply it.
    """

    __slots__ = ("status_code", "text", "url", "headers")

    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = {}


class FetchResult:
    """What a transport returns, so the runner never has to ask how it got it."""

    __slots__ = ("requested_url", "final_url", "status", "headers", "html",
                 "transport", "attempts", "elapsed_ms", "error", "exit_used")

    def __init__(self, requested_url: str, final_url: str = "",
                 status: Optional[int] = None,
                 headers: Optional[Dict[str, str]] = None, html: str = "",
                 transport: str = "http", attempts: int = 0,
                 elapsed_ms: int = 0, error: Optional[str] = None,
                 exit_used: Optional[str] = None):
        self.requested_url = requested_url
        self.final_url = final_url or requested_url
        self.status = status
        self.headers = headers or {}
        self.html = html
        self.transport = transport
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        self.error = error
        self.exit_used = exit_used

    @property
    def state(self) -> str:
        return P.detect_page_state(self.html, status=self.status,
                                   url=self.final_url, headers=self.headers)


def _log(args, message: str) -> None:
    if getattr(args, "verbose", False):
        print(message, file=sys.stderr)


def build_session(proxy: Optional[str], client: Optional[str] = None):
    """A session for the chosen HTTP client.

    Credentials travel inside the session object, never on a command line and
    never in a log — `proxy_pool.mask` is what reaches the terminal.
    """
    client = client or default_http_client()
    if client == "curl":
        if not CURL_AVAILABLE:
            raise RuntimeError(
                "--http-client curl was asked for but curl is not on PATH")
        return CurlSession(proxy)
    if requests is None:
        raise RuntimeError(
            "the requests client needs `requests`. Install it with "
            "`pip install -r requirements.txt`, or use --http-client curl.")
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def fetch(session, url: str, timeout: int, *, retries: int = 2,
          retry_delay: float = 2.0, log=None, rotate=None) -> FetchResult:
    """One page, with bounded retries. Never raises for an HTTP status.

    `rotate` is a callable returning `(session, masked_exit)` for a DIFFERENT
    exit, or None when there is no other exit to try. It is called only for
    the states that are a decision about the ADDRESS rather than a transient
    fault, because those two want opposite responses and this site serves
    both (page_flow.should_rotate_exit):

        blocked    HTTP 403, Akamai's refusal      -> different address
        challenge  HTTP 200, Akamai's interstitial -> different address
        timeout / connection reset                 -> same address, again

    Measured 2026-09-22, six session-pinned exits on one category URL in one
    minute: four served the full 1.35 MB listing, one served the interstitial,
    one timed out. Retrying the refused exit would have burned the whole
    budget on the address that had already said no.

    A `requests` exception message contains the FULL URL including its query
    string, and a ProxyError names the proxy URL — password included. So
    everything that reaches a log or an exception here goes through
    `_mask_text` first (CLAUDE.md §8).
    """
    started = time.time()
    last: Optional[FetchResult] = None
    attempts = max(1, retries + 1)
    exit_label = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
        except ProxyAuthError as exc:
            # Never retried and never rotated: every exit on a dead credential
            # fails identically.
            raise
        except Exception as exc:  # requests and curl both raise a wide family
            # `proxy_pool.mask()` is for a URL, not for free text: handed an
            # exception message it returns "?://?" and destroys it.
            masked = _mask_text(str(exc))
            last = FetchResult(url, error="%s: %s" % (type(exc).__name__, masked),
                               attempts=attempt, exit_used=exit_label,
                               elapsed_ms=int((time.time() - started) * 1000))
            if log:
                log("  attempt %d/%d failed: %s" % (attempt, attempts, last.error))
        else:
            last = FetchResult(
                url, final_url=str(response.url), status=response.status_code,
                headers={k.lower(): v for k, v in response.headers.items()},
                html=response.text, attempts=attempt, exit_used=exit_label,
                elapsed_ms=int((time.time() - started) * 1000))
            state = last.state
            if not page_flow.should_retry(state):
                return last
            if log:
                log("  attempt %d/%d: %s (HTTP %s, %d bytes)"
                    % (attempt, attempts, state, last.status, len(last.html)))
            if page_flow.should_rotate_exit(state) and rotate is not None:
                rotated = rotate()
                if rotated is not None:
                    session, exit_label = rotated
                    if log:
                        log("  rotating exit -> %s" % exit_label)
        if attempt < attempts:
            time.sleep(retry_delay)
    return last  # type: ignore[return-value]


def _mask_text(text: str) -> str:
    """Last-resort masker, used only if proxy_pool has no `mask_text`."""
    import re
    return re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***:***@", text or "")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def scrape(args) -> Tuple[List[Product], Dict[str, Any]]:
    """Returns `(rows, run_facts)`. Never writes anything."""
    pool = proxy_pool.from_args(args)
    proxy = pool.current if pool else None
    if not proxy:
        print("WARNING: no proxy configured. Measured 2026-09-22, every URL on "
              "www.homedepot.com answered HTTP 403 from a non-US address — "
              "set HOMEDEPOT_PROXY in .env to a US exit.", file=sys.stderr)
    session = build_session(proxy, args.http_client)

    url = args.url or _url_from_args(args)
    ok, reason = P.supported_host(url)
    if not ok:
        raise SystemExit("refusing %s: %s" % (url, reason))

    facts: Dict[str, Any] = {
        "transport": "http",
        "exit": proxy_pool.mask(proxy) if proxy else None,
        "pages_requested": args.pages,
        "pages_completed": 0,
        "failed_pages": [],
        "states": [],
        "store_id": None,
        "store_name": None,
        "total_products_reported": None,
        "blocked": False,
        "rotations": 0,
        "start_url": url,
        "final_url": url,
    }

    rows: List[Product] = []
    seen: set = set()
    mode = args.mode
    log = (lambda m: _log(args, m))

    state_box = {"session": session, "rotations": 0}

    def rotate():
        """A fresh session on a different exit, or None if there is not one.

        A fresh SESSION, not just a different proxy on the old one: cookies
        Akamai issued against exit A and replayed from exit B are a stronger
        signal than either address alone (CLAUDE.md §8, "a rotation is a
        fresh browser" — the same argument applies to a cookie jar).
        """
        if pool is None or len(pool) < 2:
            return None
        if state_box["rotations"] >= args.proxy_block_retries:
            return None
        state_box["rotations"] += 1
        nxt = pool.advance("blocked")
        state_box["session"] = build_session(nxt)
        facts["exit"] = proxy_pool.mask(nxt)
        facts["rotations"] = state_box["rotations"]
        return state_box["session"], proxy_pool.mask(nxt)

    if mode == "product":
        result = fetch(state_box["session"], url, args.timeout,
                       retries=args.retries, retry_delay=args.retry_delay,
                       log=log, rotate=rotate)
        facts["states"].append(result.state)
        _dump(args, result.html, 1)
        if page_flow.counts_as_blocked(result.state):
            facts["blocked"] = True
            facts["failed_pages"].append(1)
            return rows, facts
        rows = P.parse_product(result.html, url=result.final_url,
                               category=args.category)
        facts["pages_completed"] = 1
        return rows, facts

    for page in range(1, args.pages + 1):
        page_target = P.page_url(url, page)
        log("page %d: %s" % (page, page_target))
        result = fetch(state_box["session"], page_target, args.timeout,
                       retries=args.retries, retry_delay=args.retry_delay,
                       log=log, rotate=rotate)
        state = result.state
        facts["states"].append(state)
        _dump(args, result.html, page)

        if page_flow.counts_as_blocked(state):
            facts["blocked"] = True
            facts["failed_pages"].append(page)
            print("page %d: %s (HTTP %s, %d bytes)"
                  % (page, state, result.status, len(result.html)), file=sys.stderr)
            break

        if state == "hub":
            print("page %d: %s is a category HUB, not a product listing — it "
                  "has no grid. Pick a leaf category; %s lists them."
                  % (page, result.final_url,
                     "https://www.homedepot.com/sitemap/B/PLPs.xml"),
                  file=sys.stderr)
            facts["failed_pages"].append(page)
            break

        state_blob = P.apollo_state(result.html)
        if page == 1 and state_blob:
            store = P.store_context(state_blob)
            facts["store_id"] = store.get("store_id")
            facts["store_name"] = store.get("store_name")
            facts["store_postal_code"] = store.get("store_postal_code")
            report = P.search_report(state_blob)
            facts["total_products_reported"] = report.get("totalProducts")
            facts["page_size"] = report.get("pageSize")

        facts["final_url"] = result.final_url
        page_rows = P.parse_category(result.html, url=result.final_url,
                                     category=args.category)
        for row in page_rows:
            row.page = page
        fresh = output_writer.dedupe_by_sku(page_rows, seen)
        rows.extend(fresh)
        facts["pages_completed"] = page

        log("  %s: %d rows (%d new, %d total)"
            % (state, len(page_rows), len(fresh), len(rows)))

        # The terminating condition is DATA, never a selector and never
        # arithmetic over `totalProducts` — that figure is live inventory and
        # was measured moving 144 -> 143 inside four minutes (CLAUDE.md §7).
        if not page_rows:
            log("  page %d added no products — end of listing." % page)
            break
        if not fresh:
            log("  page %d added no NEW skus — end of listing." % page)
            break
        if args.max_products and len(rows) >= args.max_products:
            rows = rows[:args.max_products]
            break
        if page < args.pages:
            time.sleep(args.delay)

    return rows, facts


def _dump(args, html: str, page: int) -> None:
    """`--dump-html` writes on SUCCESS too, not only on failure.

    A run can return the right count with a field silently unpopulated, and
    then the exact bytes are the only way to tell a parsing bug from a page
    that genuinely did not carry the field (CLAUDE.md §9).
    """
    if not getattr(args, "dump_html", None):
        return
    import os
    path = args.dump_html
    if args.pages > 1 or args.mode == "category":
        root, ext = os.path.splitext(path)
        path = "%s_p%d%s" % (root, page, ext or ".html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html or "")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _url_from_args(args) -> str:
    if args.category_url:
        return args.category_url
    raise SystemExit(
        "nothing to fetch: pass --url, or --category-url with a leaf "
        "category address such as "
        "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape homedepot.com over plain HTTP (the free path).",
        epilog="Credentials belong in .env, never on a command line — `ps` "
               "reads argv.")
    p.add_argument("--url", default=None,
                   help="A leaf category or product URL on www.homedepot.com.")
    p.add_argument("--category-url", default=None, help="Alias for --url.")
    p.add_argument("--mode", choices=["category", "product"], default="category")
    p.add_argument("--category", default=None,
                   help="Label written into the rows' `category` column. The "
                        "site's own taxonomy is captured per row in "
                        "`category_hierarchy` regardless.")
    p.add_argument("--pages", type=_positive_int, default=1,
                   help="Listing pages to fetch, %d products each." % P.PAGE_SIZE)
    p.add_argument("--max-products", type=_positive_int, default=None)
    # `--retries` means RETRIES: the number of extra attempts AFTER the first,
    # so 0 means "try once", not "try never". The family has shipped the other
    # reading and it produced a run that fetched nothing and exited 0.
    p.add_argument("--retries", type=_non_negative_int, default=2,
                   help="Extra attempts after the first. 0 means one attempt.")
    p.add_argument("--retry-delay", type=_non_negative_float, default=2.0)
    p.add_argument("--delay", type=_non_negative_float, default=1.0)
    p.add_argument("--timeout", type=_positive_int, default=60)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--http-client", choices=["curl", "requests"],
                   default=default_http_client(),
                   help="Which client speaks to the site. Default %s. "
                        "Measured 2026-09-22 on six identical exits: curl got "
                        "content on 4 and the interstitial on 2; requests was "
                        "refused on all 6. requests still fetches the sitemaps "
                        "fine, which is what catalog_walk.py needs."
                        % default_http_client())
    p.add_argument("--proxy", default=None,
                   help="A single exit. Prefer HOMEDEPOT_PROXY in .env — a "
                        "proxy URL on the command line is visible to `ps`.")
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=proxy_pool.ROTATE_MODES,
                   default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-sessions", type=_positive_int, default=None,
                   help="Turn one proxy credential into N session-pinned "
                        "exits. Worth doing here: exits differ, and four of "
                        "six served content in one measurement.")
    p.add_argument("--proxy-block-retries", type=_non_negative_int, default=3,
                   help="How many times a refused or challenged page may move "
                        "to a different exit before the run gives up.")
    args = p.parse_args(argv)
    env_config.apply(args)
    if args.category_url and not args.url:
        args.url = args.category_url
    return args


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %s" % value)
    return number


def _non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


def _non_negative_float(value):
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


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


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        rows, facts = scrape(args)
    except ProxyAuthError as exc:
        print("[!] the proxy refused the credential: %s\n"
              "    This is NOT the site blocking the scraper — homedepot.com "
              "was never reached.\n"
              "    Check HOMEDEPOT_PROXY in .env; a rotated or expired "
              "credential fails exactly like this,\n"
              "    and every exit on it fails the same way, so retrying or "
              "rotating will not help."
              % exc, file=sys.stderr)
        return output_writer.EXIT_REMOTE_API_ERROR
    except SystemExit:
        raise
    except Exception as exc:
        print("run failed: %s: %s" % (type(exc).__name__, _mask_text(str(exc))),
              file=sys.stderr)
        return 1
    stop_reason = _stop_reason(args, facts)
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=facts["blocked"],
        stop_reason=stop_reason,
        pages_requested=args.pages,
        pages_completed=facts["pages_completed"],
        pages_failed=facts["failed_pages"],
        mode=args.mode,
        start_url=facts.get("start_url", ""),
        final_url=facts.get("final_url", ""),
        extra=facts,
    )


if __name__ == "__main__":
    sys.exit(main())
