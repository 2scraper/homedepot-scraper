#!/usr/bin/env python3
"""homedepot.com scraper — pyppeteer engine.

Behaves identically to `playwright_scraper.py`. **pyppeteer is effectively
unmaintained** and its own README points at Playwright; it is kept for parity
with the rest of this family and should not be anyone's first choice.

Unlike Selenium it CAN use an authenticated Scraping Browser endpoint:
`browserWSEndpoint` takes a full `ws://user:pass@host:port` and authenticates
on the WebSocket upgrade.

    python3 puppeteer_scraper.py \
        --url https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 \
        --pages 3
"""

from __future__ import annotations

import asyncio
import sys

import browser_bridge
import proxy_pool

# Module level — see selenium_scraper.py.
from pyppeteer import connect, launch


class PuppeteerDriver:
    name = "pyppeteer"

    def __init__(self, args):
        self.args = args
        self._loop = None
        self._browser = None
        self._page = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def start(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        if self.args.cdp_endpoint:
            try:
                self._browser = self._run(connect(
                    browserWSEndpoint=self.args.cdp_endpoint))
            except Exception as exc:
                raise browser_bridge.BridgeError(
                    "could not connect over CDP: %s: %s"
                    % (type(exc).__name__,
                       browser_bridge.mask_text(str(exc)))) from None
        else:
            options = {"headless": self.args.headless, "args": ["--lang=en-US"]}
            pool = proxy_pool.from_args(self.args)
            credentials = None
            if pool:
                bare, user, password = _split(pool.current)
                options["args"].append("--proxy-server=%s" % bare)
                if user:
                    credentials = {"username": user, "password": password}
            self._browser = self._run(launch(**options))
            self._page = self._run(self._browser.newPage())
            if credentials:
                # Through the driver's own field, never on the command line.
                self._run(self._page.authenticate(credentials))
        if self._page is None:
            self._page = self._run(self._browser.newPage())

    def navigate(self, url):
        response = self._run(self._page.goto(
            url, {"waitUntil": "domcontentloaded",
                  "timeout": self.args.timeout * 1000}))
        return response.status if response else None

    def content(self):
        try:
            return self._run(self._page.content())
        except Exception:
            return ""

    def count(self, selector):
        try:
            return len(self._run(self._page.querySelectorAll(selector)))
        except Exception:
            return 0

    def attr(self, selector, name):
        try:
            node = self._run(self._page.querySelector(selector))
            if node is None:
                return None
            return self._run(self._page.evaluate(
                "(el, n) => el.getAttribute(n)", node, name))
        except Exception:
            return None

    def sleep(self, ms):
        self._run(asyncio.sleep(ms / 1000.0))

    def stop(self):
        try:
            if self._browser is not None:
                self._run(self._browser.close())
        except Exception:
            pass
        finally:
            if self._loop is not None:
                self._loop.close()


def _split(url):
    """`(host:port, user, password)` for a proxy URL."""
    from urllib.parse import urlparse
    parts = urlparse(url or "")
    host = parts.hostname or ""
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    scheme = parts.scheme or "http"
    return "%s://%s" % (scheme, host), parts.username, parts.password


def main(argv=None) -> int:
    parser = browser_bridge.build_parser(
        "Scrape homedepot.com with pyppeteer.")
    args = browser_bridge.parse_args(parser, argv)
    return browser_bridge.run(args, PuppeteerDriver(args))


if __name__ == "__main__":
    sys.exit(main())
