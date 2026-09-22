#!/usr/bin/env python3
"""Sitemap discovery for homedepot.com — find targets without crawling.

The site publishes its whole taxonomy, so there is no need to spider it:

    /robots.txt  ->  Sitemap: /sitemap/main.xml
    /sitemap/main.xml
        /sitemap/B/PLPs.xml     43 files of CATEGORY urls
            PLP_CORE_TAX-0.xml      5,304 leaf categories (measured 2026-09-22)
            PLP_REFINED-0..41       filtered views of those
        /sitemap/P/PIPs.xml     96 files of PRODUCT urls

Measured 2026-09-22. `PLP_CORE_TAX` is the one worth walking: it is the plain
taxonomy, one entry per leaf category. `PLP_REFINED` is the same catalogue
sliced by facet (brand, price band, colour), so walking it re-collects the
same products many times over.

This module only DISCOVERS. It hands URLs to a transport and does not fetch
product pages itself, so it stays useful whichever engine is in front of it.

    python3 catalog_walk.py categories --limit 20
    python3 catalog_walk.py categories --grep 'Saws' --out-file targets.txt
    python3 catalog_walk.py products --limit 5
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Iterable, Iterator, List, Optional

import product_parser as P

SITEMAP_INDEX = "https://www.homedepot.com/sitemap/main.xml"
CATEGORY_INDEX = "https://www.homedepot.com/sitemap/B/PLPs.xml"
PRODUCT_INDEX = "https://www.homedepot.com/sitemap/P/PIPs.xml"

# The plain taxonomy. `PLP_REFINED` holds facet-filtered views of the SAME
# products, so including it multiplies work without adding catalogue.
CORE_TAXONOMY_MARKER = "PLP_CORE_TAX"

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def locs(xml: str) -> List[str]:
    """Every `<loc>` in a sitemap or sitemap index.

    Deliberately a regex rather than an XML parser: these files are large,
    well-formed and machine-generated, and the family already depends on
    `html.parser` rather than lxml. A malformed file yields fewer URLs here
    instead of raising, and the caller reports the count.
    """
    return _LOC_RE.findall(xml or "")


def category_sitemaps(index_xml: str, *, core_only: bool = True) -> List[str]:
    found = locs(index_xml)
    if core_only:
        return [u for u in found if CORE_TAXONOMY_MARKER in u]
    return found


def category_urls(xml: str) -> List[str]:
    """Leaf category URLs from one sitemap file, hubs filtered out.

    A hub carries no product grid (see product_parser.detect_page_state), so
    handing one to a scraper costs a fetch and returns nothing.
    """
    out = []
    for url in locs(xml):
        if P.category_id_from_url(url) and not P.is_hub_url(url):
            out.append(url)
    return out


def product_urls(xml: str) -> List[str]:
    return [u for u in locs(xml) if P.sku_from_url(u)]


def walk(fetch, index_url: str, *, kind: str = "categories",
         limit: Optional[int] = None, core_only: bool = True,
         grep: Optional[str] = None, log=None) -> Iterator[str]:
    """Yield target URLs, fetching sitemap files lazily.

    `fetch` is `callable(url) -> str`, supplied by the caller so this module
    needs no transport of its own and inherits whichever one already works.

    Lazy on purpose: `PLP_CORE_TAX-0.xml` alone holds 5,304 entries and the
    product index spans 96 files. A caller that wants twenty targets should
    not pay for all of them.
    """
    pattern = re.compile(grep, re.I) if grep else None
    index = fetch(index_url)
    files = (category_sitemaps(index, core_only=core_only) if kind == "categories"
             else locs(index))
    if log:
        log("%s index: %d file(s)" % (kind, len(files)))
    seen = set()
    yielded = 0
    for path in files:
        body = fetch(path)
        found = category_urls(body) if kind == "categories" else product_urls(body)
        if log:
            log("  %s -> %d" % (path.rsplit("/", 1)[-1], len(found)))
        for url in found:
            if url in seen:
                continue
            if pattern and not pattern.search(url):
                continue
            seen.add(url)
            yielded += 1
            yield url
            if limit and yielded >= limit:
                return


def _http_fetch(proxy: Optional[str], timeout: int):
    import http_scraper
    session = http_scraper.build_session(proxy)

    def fetch(url: str) -> str:
        result = http_scraper.fetch(session, url, timeout, retries=2)
        if result.error:
            raise RuntimeError("could not fetch %s: %s" % (url, result.error))
        return result.html
    return fetch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover homedepot.com scrape targets from its sitemaps.")
    parser.add_argument("kind", choices=["categories", "products"])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--grep", default=None,
                        help="Keep only URLs matching this regex.")
    parser.add_argument("--all-category-files", action="store_true",
                        help="Include PLP_REFINED, the facet-filtered views. "
                             "They re-list the same products and are off by "
                             "default.")
    parser.add_argument("--out-file", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-file", default=None)
    parser.add_argument("--proxy-rotate", default="per-run")
    parser.add_argument("--proxy-shuffle", action="store_true")
    parser.add_argument("--proxy-sessions", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    import env_config
    import proxy_pool
    env_config.apply(args)
    pool = proxy_pool.from_args(args)
    if not pool:
        print("WARNING: no proxy configured. The sitemaps answered from a "
              "non-US address during recon, but the pages they point at did "
              "not — set HOMEDEPOT_PROXY in .env.", file=sys.stderr)

    log = (lambda m: print(m, file=sys.stderr)) if args.verbose else (lambda m: None)
    fetch = _http_fetch(pool.current if pool else None, args.timeout)
    index = CATEGORY_INDEX if args.kind == "categories" else PRODUCT_INDEX

    found = []
    try:
        for url in walk(fetch, index, kind=args.kind, limit=args.limit,
                        core_only=not args.all_category_files,
                        grep=args.grep, log=log):
            found.append(url)
    except RuntimeError as exc:
        print("[!] %s" % exc, file=sys.stderr)
        return 3

    if args.out_file:
        with open(args.out_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(found) + "\n")
        print("[+] %d target(s) -> %s" % (len(found), args.out_file))
    else:
        for url in found:
            print(url)
    return 0 if found else 4


if __name__ == "__main__":
    sys.exit(main())
