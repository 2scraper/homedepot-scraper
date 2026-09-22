#!/usr/bin/env python3
"""homedepot.com scraper — Selenium engine.

Behaves identically to `playwright_scraper.py`: same flags, same exit codes,
same run metadata. It exists for parity and is demoted in priority, not in
correctness.

Two limits are real and are stated here rather than left to be discovered:

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` takes a full `ws://user:pass@host:port` and
  authenticates on the WebSocket upgrade; chromedriver's `debuggerAddress`
  takes a bare `host:port` with nowhere to put a password. `--cdp-endpoint`
  is therefore refused here rather than silently ignored.
* **`--proxy-server` cannot authenticate.** Chrome's own flag carries no
  credential, so a `user:pass@` exit is stripped to `host:port` and warned
  about. Do not let a user believe the password is doing something.

    python3 selenium_scraper.py \
        --url https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 \
        --pages 3
"""

from __future__ import annotations

import sys

import browser_bridge
import proxy_pool

# Module level, so the offline suite can tell "engine absent" from "engine
# broken" (CLAUDE.md §10).
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


class SeleniumDriver:
    name = "selenium"

    def __init__(self, args):
        self.args = args
        self._driver = None

    def start(self):
        if self.args.cdp_endpoint:
            raise browser_bridge.BridgeError(
                "selenium cannot use --cdp-endpoint: chromedriver's "
                "debuggerAddress takes a bare host:port and has nowhere to "
                "put the endpoint's password. Use playwright_scraper.py or "
                "puppeteer_scraper.py for the Scraping Browser API.")
        options = Options()
        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--lang=en-US")
        pool = proxy_pool.from_args(self.args)
        if pool:
            bare, credentials = proxy_pool.split_credentials(pool.current)
            bare = bare or pool.current
            if credentials:
                # Print the BARE address — the thing actually handed to
                # Chrome. An earlier version printed the MASKED url here
                # (`http://***:***@host:port`), which reads as "the credential
                # is being sent, just hidden from you" and is the opposite of
                # what happens. The whole point of the warning is that the
                # password is gone.
                print("[!] selenium cannot authenticate a proxy. The "
                      "credential in HOMEDEPOT_PROXY has been STRIPPED and "
                      "Chrome is being given %s with no username or password. "
                      "If that exit requires auth this run will be refused — "
                      "use playwright_scraper.py, which passes credentials "
                      "through the driver's own fields." % bare,
                      file=sys.stderr)
            options.add_argument("--proxy-server=%s" % bare)
        self._driver = webdriver.Chrome(options=options)
        self._driver.set_page_load_timeout(self.args.timeout)

    def navigate(self, url):
        self._driver.get(url)
        # Selenium exposes no response object, so there is no status to
        # return. The classifier is built to stay correct without one — the
        # markers and the asset-host count carry it (product_parser).
        return None

    def content(self):
        try:
            return self._driver.page_source
        except Exception:
            return ""

    def count(self, selector):
        try:
            return len(self._driver.find_elements(By.CSS_SELECTOR, selector))
        except Exception:
            return 0

    def attr(self, selector, name):
        try:
            node = self._driver.find_element(By.CSS_SELECTOR, selector)
            return node.get_attribute(name)
        except Exception:
            return None

    def sleep(self, ms):
        import time
        time.sleep(ms / 1000.0)

    def stop(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = browser_bridge.build_parser(
        "Scrape homedepot.com with Selenium.")
    args = browser_bridge.parse_args(parser, argv)
    return browser_bridge.run(args, SeleniumDriver(args))


if __name__ == "__main__":
    sys.exit(main())
