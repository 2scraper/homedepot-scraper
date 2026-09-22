#!/usr/bin/env python3
"""Run N live browser iterations and record what the challenge ladder did.

Answers three questions the ordinary CLI does not:

1. **Did rung 1 fire?** The Scraping Browser API exposes a `Captcha` CDP
   domain. `Captcha.setAutoSolve` arms it and `Captcha.solveFinished` is the
   success signal. This probe arms it, subscribes to every Captcha event it
   can, and records each one with a timestamp — so "the browser cleared it"
   stops being an assumption.
2. **What did the page actually serve on the way?** Each iteration snapshots
   the document state before and after the readiness wait, so an interstitial
   that cleared itself is visible rather than invisible.
3. **Do repeated runs agree?** Iterations on the same target are compared
   field by field at the end.

    python3 tools/autosolve_probe.py --iterations 10 --pages 2

Never prints the endpoint: it carries a password, and Playwright repeats it
five times in one error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import browser_bridge  # noqa: E402
import env_config  # noqa: E402
import product_parser as P  # noqa: E402
from output_writer import Product  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

# Deliberately a mix. Repeats on one target test whether the scraper is
# DETERMINISTIC; different targets test whether it is right in more than one
# corner of the catalogue.
TARGETS = [
    ("miter-saws", "category",
     "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7"),
    ("miter-saws", "category",
     "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7"),
    ("miter-saws", "category",
     "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7"),
    ("miter-saws", "category",
     "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7"),
    ("freezerless-fridges", "category",
     "https://www.homedepot.com/b/Appliances-Refrigerators-Freezerless-Refrigerators/N-5yc1vZc3p9"),
    ("table-saws", "category",
     "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Table-Saws/N-5yc1vZ2fkokih"),
    ("dumbbells", "category",
     "https://www.homedepot.com/b/Exercise-Equipment-Weight-Lifting-Equipment-Dumbbells/N-5yc1vZceru"),
    ("chalked-paint", "category",
     "https://www.homedepot.com/b/Paint-Craft-Paint-Furniture-Paint-Chalked-Paint/N-5yc1vZcccc"),
    ("dws780-pdp", "product",
     "https://www.homedepot.com/p/DEWALT-15-Amp-Corded-12-in-Double-Bevel-Sliding-Compound-Miter-Saw-with-XPS-technology-Blade-Wrench-and-Material-Clamp-DWS780/321488310"),
    ("tools-hub", "category",
     "https://www.homedepot.com/b/Tools/N-5yc1vZc1xy"),   # a HUB — expected to yield 0
]

CAPTCHA_EVENTS = ("Captcha.solveFinished", "Captcha.solveStarted",
                  "Captcha.detected", "Captcha.solveFailed")


def now():
    return datetime.now(timezone.utc).isoformat()


def arm_autosolve(cdp, log):
    """Arm the vendor's auto-solver and subscribe to its events.

    Returns `(armed, [support notes])`. A domain the endpoint does not
    implement is recorded, not raised — the point of this probe is to find out
    what is actually there.
    """
    notes = []
    armed = False
    for method, params in (("Captcha.setAutoSolve", {"autoSolve": True}),
                           ("Captcha.enable", {})):
        try:
            cdp.send(method, params)
            notes.append("%s: ok" % method)
            armed = True
        except Exception as exc:
            notes.append("%s: %s" % (method, str(exc).split("\n")[0][:120]))
    return armed, notes


def profile_free(endpoint, wait_s=0, log=None):
    """Whether the Scraping Browser profile is free, optionally waiting.

    **Calling this ACQUIRES the lock.** Measured 2026-09-23: three profiles
    reported free, and a connect seconds later failed `profile_locked` on the
    one just checked. Treat it as a one-off diagnostic — "is something else
    holding it right now?" — never as a pre-flight before connecting.

    A profile allows ONE live connection, and a connection that ended
    ABNORMALLY holds it for a long time — measured 2026-09-22, a
    `connect_over_cdp` that timed out kept the profile locked for over ten
    minutes afterwards, on two separate accounts.

    Playwright reports that as a connect timeout or a bare `500 Internal
    Server Error` on the WebSocket upgrade, never as the reason. The plain
    HTTP endpoint says it outright, so a run checks here first rather than
    spending ten iterations discovering it one timeout at a time.
    """
    import base64
    import http.client
    import urllib.parse
    parts = urllib.parse.urlparse(endpoint)
    token = base64.b64encode(
        ("%s:%s" % (parts.username, parts.password)).encode()).decode()
    deadline = time.time() + wait_s
    while True:
        try:
            conn = http.client.HTTPConnection(parts.hostname, parts.port,
                                              timeout=20)
            conn.request("GET", "/json/version",
                         headers={"Authorization": "Basic " + token})
            response = conn.getresponse()
            body = response.read().decode("utf-8", "replace")
            if response.status == 200 and "Browser" in body:
                return True
            reason = body.strip()[:40]
        except Exception as exc:
            reason = type(exc).__name__
        if time.time() >= deadline:
            if log:
                log("    profile not free: %s" % reason)
            return False
        time.sleep(10)


def run_one(index, name, mode, url, pages, timeout, records, local=False):
    env = argparse.Namespace(cdp_endpoint=None, proxy=None, twocaptcha_key=None,
                             url=None)
    env_config.apply(env)
    endpoint = env.cdp_endpoint
    if not local and not endpoint:
        raise SystemExit("no HOMEDEPOT_CDP_ENDPOINT in .env")

    # NO pre-flight lock check here, deliberately. `GET /json/version` on this
    # vendor ACQUIRES the profile lock it is being asked about: three profiles
    # reported free, and a `connect_over_cdp` seconds later failed
    # `profile_locked` on the one that had just been checked. The check was
    # creating the condition it reported, and the first version of this probe
    # did it to itself ten times in a row. `profile_free()` is kept for one-off
    # diagnostics and must never be called on a path that then connects.
    record = {
        "iteration": index, "target": name, "mode": mode, "url": url,
        "started_at": now(), "captcha_events": [], "autosolve_notes": [],
        "states": [], "rows": 0, "skus": [], "error": None,
        "pre_wait_state": None, "challenge_seen": False, "ms": 0,
    }
    record["transport"] = "local-chromium" if local else "scraping-browser-cdp"
    started = time.time()
    rows = []
    with sync_playwright() as pw:
        try:
            if local:
                # A LOCAL browser exercises rung 1 — "does a real browser clear
                # Akamai's interstitial by itself?" — which is the rung that
                # actually matters on this site. It cannot exercise the
                # VENDOR's Captcha CDP domain; that is recorded, not faked.
                import proxy_pool
                launch = {"headless": True}
                if env.proxy:
                    # A FRESH session-pinned exit per iteration. Reusing one
                    # pinned exit across ten iterations measures how scored
                    # that single address is, not how the scraper behaves —
                    # and the exit in .env had been in use all day.
                    exit_url = proxy_pool.with_session(
                        env.proxy, "probe%d%d" % (index, int(time.time()) % 100000))
                    launch["proxy"] = proxy_pool.to_playwright(exit_url)
                    record["exit"] = proxy_pool.mask(exit_url)
                browser = pw.chromium.launch(**launch)
            else:
                browser = pw.chromium.connect_over_cdp(endpoint,
                                                       timeout=timeout * 1000)
        except Exception as exc:
            # Masked: the endpoint carries a password and Playwright repeats it
            # five times in one error.
            record["error"] = "connect: %s" % browser_bridge.mask_text(
                str(exc).split("\n")[0])[:200]
            record["ms"] = int((time.time() - started) * 1000)
            records.append(record)
            return rows, record
        try:
            ctx = (browser.new_context(locale="en-US") if local
                   else (browser.contexts[0] if browser.contexts
                         else browser.new_context()))
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)

            armed, notes = arm_autosolve(cdp, None)
            record["autosolve_notes"] = notes
            record["autosolve_armed"] = armed

            def on_event(event_name):
                def handler(payload):
                    record["captcha_events"].append(
                        {"at": now(), "event": event_name,
                         "payload": str(payload)[:400]})
                return handler

            for event in CAPTCHA_EVENTS:
                try:
                    cdp.on(event, on_event(event))
                except Exception:
                    pass

            page.set_default_timeout(timeout * 1000)
            targets = ([url] if mode == "product"
                       else [P.page_url(url, n) for n in range(1, pages + 1)])
            seen = set()
            for page_no, target in enumerate(targets, 1):
                response = page.goto(target, wait_until="domcontentloaded",
                                     timeout=timeout * 1000)
                status = response.status if response else None

                # BEFORE the readiness wait: this is where an interstitial is
                # visible. After it, a cleared one looks like it never was.
                early = page.content()
                early_state = P.detect_page_state(early, status=status, url=target)
                if page_no == 1:
                    record["pre_wait_state"] = early_state
                if early_state == "challenge":
                    record["challenge_seen"] = True

                waited = 0
                want = 4 if mode == "category" else 1
                # A hard refusal will never paint a grid, so waiting the full
                # timeout on it measures nothing and costs a minute an
                # iteration. A CHALLENGE is different and IS waited out — that
                # wait is the whole point of this probe.
                budget = 0 if early_state == "blocked" else timeout * 1000
                while waited < budget:
                    try:
                        if mode == "product":
                            if P.jsonld_product(page.content() or ""):
                                break
                        elif page.locator('a[href*="/p/"]').count() >= want:
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    waited += 500

                html = page.content()
                state = P.detect_page_state(html, status=status, url=target)
                record["states"].append(
                    {"page": page_no, "status": status, "state": state,
                     "bytes": len(html), "wait_ms": waited,
                     "early_state": early_state})
                if state in ("blocked", "captcha", "challenge"):
                    break
                page_rows = (P.parse_product(html, url=target) if mode == "product"
                             else P.parse_category(html, url=target))
                for row in page_rows:
                    if row.sku not in seen:
                        seen.add(row.sku)
                        row.page = page_no
                        rows.append(row)
            record["rows"] = len(rows)
            record["skus"] = [r.sku for r in rows]
        except Exception as exc:
            record["error"] = "%s: %s" % (
                type(exc).__name__,
                browser_bridge.mask_text(str(exc).split("\n")[0])[:200])
        finally:
            try:
                browser.close()
            except Exception:
                pass
    record["ms"] = int((time.time() - started) * 1000)
    records.append(record)
    return rows, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--out", default="live/autosolve")
    parser.add_argument("--local", action="store_true",
                        help="Launch a LOCAL Chromium through HOMEDEPOT_PROXY "
                             "instead of connecting to the Scraping Browser. "
                             "Exercises rung 1 (does a real browser clear the "
                             "interstitial?) but not the vendor's Captcha CDP "
                             "domain.")
    parser.add_argument("--settle", type=float, default=8.0,
                        help="Seconds between iterations. A Scraping Browser "
                             "profile allows ONE live connection and a just-"
                             "closed one is held briefly.")
    args = parser.parse_args(argv)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    records = []
    all_rows = {}
    for index in range(args.iterations):
        name, mode, url = TARGETS[index % len(TARGETS)]
        print("[%2d/%d] %-20s %s" % (index + 1, args.iterations, name, mode),
              flush=True)
        rows, record = run_one(index + 1, name, mode, url, args.pages,
                               args.timeout, records, local=args.local)
        all_rows[index + 1] = [r.__dict__ for r in rows]
        print("        state=%s rows=%d challenge=%s captcha_events=%d %s"
              % ([s["state"] for s in record["states"]] or record["error"],
                 record["rows"], record["challenge_seen"],
                 len(record["captcha_events"]),
                 "ERR:" + record["error"] if record["error"] else ""),
              flush=True)
        with open(args.out + ".jsonl", "w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        with open(args.out + ".rows.json", "w", encoding="utf-8") as handle:
            json.dump(all_rows, handle, ensure_ascii=False, indent=1)
        if index + 1 < args.iterations:
            time.sleep(args.settle)
    print("\nwrote %s.jsonl and %s.rows.json" % (args.out, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
