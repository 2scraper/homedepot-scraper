#!/usr/bin/env python3
"""homedepot.com scraper — Playwright engine (the primary browser engine).

On this site the browser is the RELIABLE transport, not the expensive
fallback. Akamai answers a fraction of requests with a behavioural
interstitial (HTTP 200, ~2.5 KB, a JS sensor that scores the client and then
reloads); a real browser clears it transparently and a plain HTTP client
cannot. Measured 2026-09-22 — see README.

    python3 playwright_scraper.py \
        --url https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 \
        --pages 3 --out miter

With no `--cdp-endpoint` this launches a local Chromium and needs a US exit
in `HOMEDEPOT_PROXY`. With one, it connects to the Scraping Browser API,
which brings its own exit — do not set both.
"""

from __future__ import annotations

import sys

import browser_bridge
import proxy_pool

# Imported at MODULE level on purpose: the offline suite must be able to skip
# this engine when Playwright is absent, and it can only tell that if the
# import fails here rather than deep inside start() (CLAUDE.md §10).
from playwright.sync_api import sync_playwright


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, args):
        self.args = args
        self._over_cdp = False
        self._cdp = None
        self.autosolve_armed = False
        self.autosolve_events = []
        self.autosolve_notes = []
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def start(self):
        self._pw = sync_playwright().start()
        if self.args.cdp_endpoint:
            # Wrapped so a failure cannot print the endpoint, which carries a
            # password. Playwright repeats it five times in one error — the
            # message plus a four-line call log (CLAUDE.md §8).
            try:
                self._browser = self._pw.chromium.connect_over_cdp(
                    self.args.cdp_endpoint, timeout=self.args.timeout * 1000)
            except Exception as exc:
                raise browser_bridge.BridgeError(
                    "could not connect over CDP: %s: %s"
                    % (type(exc).__name__,
                       browser_bridge.mask_text(str(exc)))) from None
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else self._browser.new_context()
            self._over_cdp = True
        else:
            launch: dict = {"headless": self.args.headless}
            pool = proxy_pool.from_args(self.args)
            if pool:
                # Through Playwright's own fields, never `--proxy-server=`,
                # which would put the credential on the browser's command line
                # where `ps` reads it.
                launch["proxy"] = proxy_pool.to_playwright(pool.current)
            self._browser = self._pw.chromium.launch(**launch)
            # No user agent and no fingerprint are set here. Over CDP the
            # remote browser brings its own and stacking a second is a
            # contradiction; locally, a hardcoded UA drifts from whatever
            # Chromium is installed and claiming an older Chrome than the JS
            # engine reports is itself a mismatch (CLAUDE.md §8).
            self._context = self._browser.new_context(locale="en-US")
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.args.timeout * 1000)
        if self._over_cdp:
            self._arm_autosolve()

    def _arm_autosolve(self):
        """Rung 1a: ask the remote browser to clear challenges itself.

        The Scraping Browser API exposes a `Captcha` CDP domain.
        `Captcha.setAutoSolve` arms it and `Captcha.solveFinished` is the
        success signal. Arming it costs nothing when there is nothing to
        solve, and it runs BEFORE the local solver gets a turn — which is the
        whole point of the ladder: the paid `captcha_solver.py` rung should
        only ever see what the browser could not handle.

        Every failure here is a WARNING. An endpoint that does not implement
        the domain is a perfectly good endpoint for this site, where the wall
        is an Akamai 403 and a JS interstitial rather than a captcha; refusing
        to run would trade a working scrape for a missing feature.

        Solve events are counted rather than just logged, so a run can report
        whether the remote solver actually did anything — see
        `autosolve_events`.
        """
        if self.args.solve_captcha == "never":
            return
        try:
            self._cdp = self._context.new_cdp_session(self._page)
        except Exception as exc:
            print("[!] could not open a CDP session for auto-solve: %s"
                  % browser_bridge.mask_text(str(exc))[:160], file=sys.stderr)
            return
        for event in ("Captcha.solveStarted", "Captcha.solveFinished",
                      "Captcha.solveFailed", "Captcha.detected"):
            try:
                self._cdp.on(event, self._on_captcha_event(event))
            except Exception:
                pass
        for method, params in (("Captcha.setAutoSolve", {"autoSolve": True}),
                               ("Captcha.enable", {})):
            try:
                self._cdp.send(method, params)
                self.autosolve_armed = True
            except Exception as exc:
                self.autosolve_notes.append(
                    "%s unavailable: %s"
                    % (method, str(exc).splitlines()[0][:100]))

    def _on_captcha_event(self, name):
        def handler(payload):
            self.autosolve_events.append({"event": name,
                                          "payload": str(payload)[:300]})
            print("[captcha] %s %s" % (name, str(payload)[:160]), file=sys.stderr)
        return handler

    def navigate(self, url):
        response = self._page.goto(url, wait_until="domcontentloaded",
                                   timeout=self.args.timeout * 1000)
        return response.status if response else None

    def content(self):
        try:
            return self._page.content()
        except Exception:
            return ""

    def count(self, selector):
        try:
            return self._page.locator(selector).count()
        except Exception:
            return 0

    def attr(self, selector, name):
        try:
            node = self._page.locator(selector).first
            return node.get_attribute(name)
        except Exception:
            return None

    def sleep(self, ms):
        self._page.wait_for_timeout(ms)

    def stop(self):
        for closer in (self._page, self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if self._pw is not None:
            self._pw.stop()


def main(argv=None) -> int:
    parser = browser_bridge.build_parser(
        "Scrape homedepot.com with Playwright (the primary browser engine).")
    args = browser_bridge.parse_args(parser, argv)
    return browser_bridge.run(args, PlaywrightDriver(args))


if __name__ == "__main__":
    sys.exit(main())
