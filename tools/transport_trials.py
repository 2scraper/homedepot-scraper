#!/usr/bin/env python3
"""Paired transport trials: the same exit, the same URL, three clients.

Each trial mints ONE fresh session-pinned exit and puts three clients through
it back to back, so the comparison is not confounded by which address each one
happened to get:

    A  system curl            HTTP/2, the measured header set
    B  local Chromium         Playwright's own, headless
    C  Scraping Browser       over CDP, with `Captcha.setAutoSolve` armed

C is skipped when no profile is free — a profile allows one live connection
and an abnormally-ended one holds it for a long time. That is recorded as a
skip rather than quietly dropped.

Where a client meets Akamai's interstitial, it is WAITED OUT rather than
retried: the interstitial is a JS sensor that reloads the page itself, so what
is being measured is whether that client's JavaScript can satisfy it. That
wait is the autosolve observation this whole script exists for.

    python3 tools/transport_trials.py --trials 10
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import browser_bridge  # noqa: E402
import env_config  # noqa: E402
import http_scraper  # noqa: E402
import product_parser as P  # noqa: E402
import proxy_pool  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

URL = ("https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/"
       "N-5yc1vZc2d7")
CHALLENGE_WAIT_MS = 30000


def now():
    return datetime.now(timezone.utc).isoformat()


def profile_free(endpoint):
    if not endpoint:
        return False
    parts = urllib.parse.urlparse(endpoint)
    token = base64.b64encode(
        ("%s:%s" % (parts.username, parts.password)).encode()).decode()
    try:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
        conn.request("GET", "/json/version",
                     headers={"Authorization": "Basic " + token})
        response = conn.getresponse()
        return response.status == 200 and "Browser" in response.read().decode(
            "utf-8", "replace")
    except Exception:
        return False


def trial_curl(exit_url, url, timeout):
    started = time.time()
    session = http_scraper.CurlSession(exit_url)
    try:
        response = session.get(url, timeout=timeout)
    except Exception as exc:
        return {"client": "curl", "error": type(exc).__name__,
                "ms": int((time.time() - started) * 1000)}
    state = P.detect_page_state(response.text, status=response.status_code)
    rows = P.parse_category(response.text, url=url) if state == "content" else []
    return {"client": "curl", "status": response.status_code,
            "bytes": len(response.text), "state": state, "rows": len(rows),
            "skus": [r.sku for r in rows],
            "ms": int((time.time() - started) * 1000)}


def _drive(page, url, timeout, label, autosolve_events=None):
    started = time.time()
    response = page.goto(url, wait_until="domcontentloaded",
                         timeout=timeout * 1000)
    status = response.status if response else None
    early = page.content()
    early_state = P.detect_page_state(early, status=status, url=url)

    cleared = None
    html = early
    if early_state == "challenge":
        # Wait it out. The interstitial reloads itself once its sensor is
        # satisfied; retrying instead of waiting restarts the scoring.
        cleared = False
        waited = 0
        while waited < CHALLENGE_WAIT_MS:
            page.wait_for_timeout(1000)
            waited += 1000
            html = page.content()
            if P.detect_page_state(html, url=url) in ("content", "product"):
                cleared = True
                break
    elif early_state not in ("blocked",):
        waited = 0
        while waited < timeout * 1000:
            try:
                if page.locator('a[href*="/p/"]').count() >= 4:
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)
            waited += 500
        html = page.content()

    state = P.detect_page_state(html, status=status, url=url)
    rows = P.parse_category(html, url=url) if state == "content" else []
    out = {"client": label, "status": status, "bytes": len(html),
           "early_state": early_state, "state": state, "rows": len(rows),
           "skus": [r.sku for r in rows],
           "challenge_cleared": cleared,
           "ms": int((time.time() - started) * 1000)}
    if autosolve_events is not None:
        out["autosolve_events"] = list(autosolve_events)
    return out


def trial_local(exit_url, url, timeout):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, proxy=proxy_pool.to_playwright(exit_url))
        try:
            ctx = browser.new_context(locale="en-US")
            page = ctx.new_page()
            return _drive(page, url, timeout, "local-chromium")
        except Exception as exc:
            return {"client": "local-chromium",
                    "error": browser_bridge.mask_text(
                        str(exc).splitlines()[0])[:160]}
        finally:
            browser.close()


def trial_cdp(endpoint, url, timeout):
    events = []
    armed = False
    notes = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(endpoint,
                                                   timeout=timeout * 1000)
        except Exception as exc:
            return {"client": "scraping-browser", "skipped": True,
                    "error": browser_bridge.mask_text(
                        str(exc).splitlines()[0])[:160]}
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                cdp = ctx.new_cdp_session(page)
                for event in ("Captcha.solveStarted", "Captcha.solveFinished",
                              "Captcha.solveFailed", "Captcha.detected"):
                    cdp.on(event, lambda payload, e=event: events.append(
                        {"event": e, "at": now(), "payload": str(payload)[:200]}))
                for method, params in (("Captcha.setAutoSolve", {"autoSolve": True}),
                                       ("Captcha.enable", {})):
                    try:
                        cdp.send(method, params)
                        armed = True
                    except Exception as exc:
                        notes.append("%s: %s" % (method,
                                                 str(exc).splitlines()[0][:80]))
            except Exception as exc:
                notes.append("cdp session: %s" % str(exc).splitlines()[0][:80])
            result = _drive(page, url, timeout, "scraping-browser", events)
            result["autosolve_armed"] = armed
            result["autosolve_notes"] = notes
            return result
        except Exception as exc:
            return {"client": "scraping-browser",
                    "error": browser_bridge.mask_text(
                        str(exc).splitlines()[0])[:160],
                    "autosolve_armed": armed, "autosolve_notes": notes}
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--out", default="live/trials")
    parser.add_argument("--settle", type=float, default=5.0)
    args = parser.parse_args(argv)

    env = argparse.Namespace(cdp_endpoint=None, proxy=None,
                             twocaptcha_key=None, url=None)
    env_config.apply(env)
    if not env.proxy:
        raise SystemExit("HOMEDEPOT_PROXY is required for these trials")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    trials = []
    for index in range(1, args.trials + 1):
        exit_url = proxy_pool.with_session(
            env.proxy, "trial%d%d" % (index, int(time.time()) % 100000))
        record = {"trial": index, "at": now(),
                  "exit": proxy_pool.mask(exit_url), "results": []}
        print("[%2d/%d]" % (index, args.trials), flush=True)

        for name, fn in (("curl", lambda: trial_curl(exit_url, args.url, args.timeout)),
                         ("local", lambda: trial_local(exit_url, args.url, args.timeout))):
            result = fn()
            record["results"].append(result)
            print("   %-16s %s" % (
                result["client"],
                result.get("error") or "status=%s bytes=%s state=%s rows=%s%s"
                % (result.get("status"), result.get("bytes"),
                   result.get("state"), result.get("rows"),
                   "" if result.get("challenge_cleared") is None
                   else " cleared=%s" % result["challenge_cleared"])),
                  flush=True)

        if profile_free(env.cdp_endpoint):
            result = trial_cdp(env.cdp_endpoint, args.url, args.timeout)
            record["results"].append(result)
            print("   %-16s %s" % (
                "scraping-browser",
                result.get("error") or
                "status=%s bytes=%s state=%s rows=%s armed=%s events=%d"
                % (result.get("status"), result.get("bytes"),
                   result.get("state"), result.get("rows"),
                   result.get("autosolve_armed"),
                   len(result.get("autosolve_events") or []))), flush=True)
        else:
            record["results"].append({"client": "scraping-browser",
                                      "skipped": True,
                                      "error": "profile_locked"})
            print("   %-16s skipped: profile_locked" % "scraping-browser",
                  flush=True)

        trials.append(record)
        with open(args.out + ".json", "w", encoding="utf-8") as handle:
            json.dump(trials, handle, ensure_ascii=False, indent=1)
        if index < args.trials:
            time.sleep(args.settle)
    print("\nwrote %s.json" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
