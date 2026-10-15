#!/usr/bin/env python3
"""homedepot.com scraper — the 2Captcha Scraper API transport.

A browserless rung: the vendor runs the browser and the exit, and this client
posts a URL and gets HTML back. Useful when you want neither a local Chromium
nor a proxy of your own.

It is a PAID path and it is listed third on purpose. On this site the cheap
`http_scraper.py` often works outright, and `playwright_scraper.py` over the
Scraping Browser API is the one measured to clear Akamai's interstitial every
time. Reach for this when you want no browser infrastructure at all.

    python3 scraper_api_client.py \
        --url https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 \
        --pages 3 --out miter

The key rides in an `Authorization` header, not in a query parameter. That
matters: `requests` puts the FULL URL, query string included, into the text of
every `HTTPError` and every connection error, so a key passed as a query
parameter leaks the moment anything goes wrong.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from typing import Dict, List, Optional

import env_config
import output_writer
import page_flow
import product_parser as P
from output_writer import (EXIT_REMOTE_API_ERROR, Product, RemoteAPIError,
                           finish_run)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("homedepot.scraperapi")

API_BASE = "https://api.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"
MAX_API_TIMEOUT = 120
DEFAULT_OUT = "homedepot_products_scraperapi"

_CREDENTIALS_IN_URL_RE = re.compile(r"(\w+://)[^/@\s]+:[^/@\s]+@")


_SECRET_PARAM_RE = re.compile(
    r"(?i)((?:client)?key|token|api[_-]?key|password|passwd|pass|auth"
    r"|authorization)(\s*[=:]\s*)(?:bearer\s+)?[^\s&;,\"']+")


def _mask(text: str) -> str:
    """Mask GLOBALLY. A masker that handles only the first occurrence prints
    the password the other four times and looks like it is working.

    Covers `user:pass@` inside a URL and `key=…` / `token: …` pairs, which is
    how a debug header or an error body would carry a credential."""
    text = _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")
    return _SECRET_PARAM_RE.sub(r"\1\2***", text)


class Fetched:
    __slots__ = ("status", "html", "headers")

    def __init__(self, status: Optional[int], html: str, headers: Dict[str, str]):
        self.status = status
        self.html = html
        self.headers = headers

    @property
    def state(self) -> str:
        return P.detect_page_state(self.html, status=self.status,
                                   headers=self.headers)


def fetch_one(args, url: str) -> Fetched:
    if requests is None:
        raise RemoteAPIError("the Scraper API transport needs `requests`")
    payload = {
        "task_type": "scrape",
        "url": url,
        # `raw`, because the grid lives in a `window.__APOLLO_STATE__` script
        # tag. Any "cleaning" the API offers would strip exactly the element
        # this repo parses.
        "data_format": "raw",
        "format": "json",
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }
    # `country` is what makes the exit American, and on this site that is not
    # optional: a non-US address gets HTTP 403 on every URL.
    payload["country"] = args.country
    if args.cdp_endpoint:
        payload["cdpurl"] = args.cdp_endpoint

    try:
        response = requests.post(
            SYNC_ENDPOINT,
            headers={"Authorization": f"Bearer {args.twocaptcha_key}",
                     "Content-Type": "application/json"},
            json=payload,
            # More headroom than the API-side task timeout, so a task that
            # legitimately runs the full 120 s does not look like a local
            # network failure.
            timeout=min(args.timeout, MAX_API_TIMEOUT) + 30)
    except Exception as exc:  # noqa: BLE001
        raise RemoteAPIError("%s: %s" % (type(exc).__name__, _mask(str(exc)))) from None

    # The API's debug header can name the exit it used, credential included,
    # so it is masked like everything else that reaches a log.
    debug = response.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _mask(str(debug)))

    if response.status_code != 200:
        # The API's OWN call failing, not the site refusing a page: 422 a task
        # that ran and errored, 402 out of balance, 408 the sync wait exceeded.
        raise RemoteAPIError(
            "Scraper API returned HTTP %s: %s"
            % (response.status_code, _mask(response.text[:400])))

    try:
        body = response.json()
    except ValueError:
        raise RemoteAPIError("Scraper API answered HTTP 200 with a body that "
                             "is not JSON: %s" % _mask(response.text[:200])) from None
    text = body.get("body") or ""
    headers = {str(k).lower(): str(v) for k, v in (body.get("headers") or {}).items()}
    # Two different things, easy to swap: `http_code` is the TARGET site's
    # status, `status` is the API's own verdict string ("ok", "warn"). The
    # verdict can be "warn" on a 200 and on a 403 alike, so it says nothing
    # about the page; it is logged, never parsed as a number.
    upstream = _int_or_none(body.get("http_code"))
    logger.info("upstream %s (api verdict %r), %d bytes",
                upstream, body.get("status"), len(text))
    return Fetched(upstream, text, headers)


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def scrape(args):
    rows: List[Product] = []
    seen: set = set()
    facts: Dict[str, object] = {
        "transport": "scraper-api", "country": args.country,
        "pages_requested": args.pages, "pages_completed": 0,
        "failed_pages": [], "states": [], "blocked": False,
        "transport_error": None, "stop_reason": None,
        "start_url": args.url, "final_url": args.url,
    }

    def get(target: str, page: int) -> Optional[Fetched]:
        """One API call. Its failure ends the run HERE, so rows from earlier
        pages are still written (a partial run) instead of being lost."""
        try:
            return fetch_one(args, target)
        except RemoteAPIError as exc:
            print("[!] %s" % exc, file=sys.stderr)
            facts["transport_error"] = str(exc)[:300]
            facts["failed_pages"].append(page)
            facts["stop_reason"] = "remote_api_error"
            return None

    if args.mode == "product":
        result = get(args.url, 1)
        if result is None:
            return rows, facts
        facts["states"].append(result.state)
        if page_flow.counts_as_blocked(result.state):
            facts["blocked"] = True
            facts["failed_pages"].append(1)
        else:
            rows = P.parse_product(result.html, url=args.url,
                                   category=args.category)
            facts["pages_completed"] = 1
            if result.state == "product" and not rows:
                facts["stop_reason"] = "parser_found_nothing"
        return rows, facts

    for page in range(1, args.pages + 1):
        target = P.page_url(args.url, page)
        result = get(target, page)
        if result is None:
            break
        state = result.state
        facts["states"].append(state)
        if page_flow.counts_as_blocked(state):
            facts["blocked"] = True
            facts["failed_pages"].append(page)
            print("page %d: %s (upstream HTTP %s, %d bytes)"
                  % (page, state, result.status, len(result.html)), file=sys.stderr)
            break
        if state == "hub":
            print("page %d: that URL is a category hub, not a listing."
                  % page, file=sys.stderr)
            facts["failed_pages"].append(page)
            facts["stop_reason"] = "hub_not_a_listing"
            break
        blob = P.apollo_state(result.html)
        if page == 1 and blob:
            store = P.store_context(blob)
            facts.update({k: store.get(k) for k in store})
        page_rows = P.parse_category(result.html, url=target,
                                     category=args.category)
        for row in page_rows:
            row.page = page
        fresh = output_writer.dedupe_by_sku(page_rows, seen)
        rows.extend(fresh)
        facts["final_url"] = target
        if state == "content" and not page_rows:
            print("page %d: the page holds a listing grid but parsed to 0 "
                  "rows — a parser failure, not an empty category." % page,
                  file=sys.stderr)
            facts["failed_pages"].append(page)
            facts["stop_reason"] = "parser_found_nothing"
            break
        facts["pages_completed"] = page
        if not page_rows or not fresh:
            break
        if args.max_products and len(rows) >= args.max_products:
            rows = rows[:args.max_products]
            break
        if page < args.pages:
            time.sleep(args.delay)
    return rows, facts


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Scrape homedepot.com through the 2Captcha Scraper API.",
        epilog="The key belongs in .env as TWOCAPTCHA_KEY — `ps` reads argv.")
    p.add_argument("--url", required=False, default=None)
    p.add_argument("--mode", choices=["category", "product"], default="category")
    p.add_argument("--category", default=None)
    p.add_argument("--pages", type=_positive_int, default=1)
    p.add_argument("--max-products", type=_positive_int, default=None)
    p.add_argument("--delay", type=_non_negative_float, default=1.0)
    p.add_argument("--timeout", type=_positive_int, default=MAX_API_TIMEOUT)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--twocaptcha-key", default=None,
                   help="Prefer TWOCAPTCHA_KEY in .env.")
    # Legitimate here and banned on the browser engines, where it could
    # disagree with the URL. This endpoint has no other way to pick an exit,
    # and on this site the exit must be American.
    p.add_argument("--country", default="us",
                   help="Exit country for the API's own proxy. Default us, "
                        "which this site requires.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Route the API's task through an existing browser "
                        "session. Prefer HOMEDEPOT_CDP_ENDPOINT in .env.")
    args = p.parse_args(argv)
    env_config.apply(args)
    return args


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %s" % value)
    return number


def _non_negative_float(value):
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    if not args.url:
        print("nothing to fetch: pass --url", file=sys.stderr)
        return 2
    if not args.twocaptcha_key:
        print("no TWOCAPTCHA_KEY — this transport is the paid one and cannot "
              "run without it. `python3 http_scraper.py` needs no key.",
              file=sys.stderr)
        return 2
    ok, reason = P.supported_host(args.url)
    if not ok:
        print("refusing %s: %s" % (args.url, reason), file=sys.stderr)
        return 2
    try:
        rows, facts = scrape(args)
    except RemoteAPIError as exc:
        print("[!] %s" % exc, file=sys.stderr)
        return EXIT_REMOTE_API_ERROR
    stop = (facts["stop_reason"] if facts["stop_reason"] else
            "blocked" if facts["blocked"] else
            "single_page_mode" if args.mode == "product" else
            "completed" if facts["pages_completed"] >= args.pages else
            "no_new_products")
    return finish_run(rows, args.out, args.format, args.allow_empty,
                      blocked=bool(facts["blocked"]), stop_reason=stop,
                      pages_requested=args.pages,
                      pages_completed=int(facts["pages_completed"]),
                      pages_failed=facts["failed_pages"], mode=args.mode,
                      start_url=str(facts["start_url"]),
                      final_url=str(facts["final_url"]),
                      scope=output_writer.scope_fingerprint(
                          store_id=output_writer.run_store_id(facts, rows),
                          category=args.category,
                          max_products=args.max_products),
                      extra=facts)


if __name__ == "__main__":
    sys.exit(main())
