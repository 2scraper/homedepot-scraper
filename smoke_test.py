#!/usr/bin/env python3
"""homedepot-scraper — the offline suite.

One file of plain functions with fixtures cut from real captures. No pytest,
no conftest, no fixtures directory of its own beyond `fixtures/`.
`tests/test_smoke.py` wraps this as a single pytest test so `pytest` works as
an entry point without a second copy of the checks.

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and records a skip. For that to mean anything each engine imports its
driver at MODULE level, which is asserted below by walking the AST — an
engine that imports its driver inside `start()` would import cleanly with the
library absent, the group would never skip, and the CI job that exists to
fail on unexpected skips could not catch a broken import.

    python3 smoke_test.py
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import inspect
import io
import json
import os
import sys
import tempfile
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, HERE)

PASSES: List[str] = []
FAILURES: List[str] = []
SKIPS: List[str] = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print("PASS %s" % name)
    else:
        FAILURES.append("%s %s" % (name, detail))
        print("FAIL %s %s" % (name, detail))


def eq(name, got, want):
    check(name, got == want, "(got %r, want %r)" % (got, want))


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Page-state classification — the wall this site actually serves
# ---------------------------------------------------------------------------

def test_page_states():
    import product_parser as P
    cases = [
        ("listing_mitersaws.html", "content"),
        ("product_dws780.html", "product"),
        ("hub_tools.html", "hub"),
        ("home_recommender.html", "hub"),
        ("blocked_access_denied.html", "blocked"),
        ("blocked_oops.html", "blocked"),
        ("challenge_akamai.html", "challenge"),
    ]
    for name, want in cases:
        html = fixture(name)
        # WITHOUT a status code: the Selenium / pyppeteer case, where the
        # markers and the asset-host count are the only signals there are.
        eq("state(%s) no-status" % name, P.detect_page_state(html), want)
    # WITH a status code, the two 403s must still classify the same way.
    eq("state(access_denied) 403",
       P.detect_page_state(fixture("blocked_access_denied.html"), status=403),
       "blocked")
    eq("state(oops) 403",
       P.detect_page_state(fixture("blocked_oops.html"), status=403), "blocked")
    # The interstitial arrives as HTTP 200 and must not be waved through.
    eq("challenge arrives as HTTP 200",
       P.detect_page_state(fixture("challenge_akamai.html"), status=200),
       "challenge")


def test_challenge_is_not_a_captcha():
    """Regression: the interstitial must never be routed to the paid solver.

    It is a JS sensor with no puzzle and no sitekey. Paying for a solve here
    buys nothing, and the state policy is what keeps that from happening.
    """
    import page_flow
    check("challenge is not solved", not page_flow.should_solve("challenge"))
    check("challenge rotates the exit", page_flow.should_rotate_exit("challenge"))
    check("blocked is not solved", not page_flow.should_solve("blocked"))
    check("blocked rotates the exit", page_flow.should_rotate_exit("blocked"))
    check("captcha IS solved", page_flow.should_solve("captcha"))
    check("captcha does not rotate", not page_flow.should_rotate_exit("captcha"))
    # A hub is a correct answer to a URL that was never a grid. Retrying it or
    # paying for it just costs requests.
    check("hub is not retried", not page_flow.should_retry("hub"))
    check("hub is not blocked", not page_flow.counts_as_blocked("hub"))
    check("content is terminal", not page_flow.should_retry("content"))


def test_sitekeys_do_not_trigger_the_solver():
    """The three reCAPTCHA sitekeys sit in the global config blob on EVERY
    page. Matching a sitekey string rather than a rendered widget would route
    every good 1.35 MB listing to the paid solver."""
    import product_parser as P
    keys = ("6LevSy0aAAAAAMexygKgonkNBPEQZIDJKxev-dyu",
            "6LfARy0aAAAAACDDk19oD-fg4DG79YLf1kEF3dtB",
            "6LfEHBkTAAAAAHX6YgeUw9x1Sutr7EzhMdpbIfWJ")
    for key in keys:
        check("sitekey %s.. is not a captcha marker" % key[:10],
              not any(key.lower() in m for m in P.CAPTCHA_MARKERS))
    html = fixture("listing_mitersaws.html").replace(
        "</body>", '<script>var k="%s";</script></body>' % keys[0])
    eq("a page merely NAMING a sitekey is still content",
       P.detect_page_state(html), "content")


def test_dead_markers_are_not_shipped():
    """`_abck`/`bm_sz`/`bm-verify` appear in ZERO page bodies on this site —
    they are Set-Cookie headers only. Shipping them as body markers would be
    dead code that looks load-bearing."""
    import product_parser as P
    every = P.BOT_CHALLENGE_MARKERS + P.CHALLENGE_MARKERS + P.CAPTCHA_MARKERS
    for dead in ("_abck", "bm_sz", "bm-verify", "sensor_data"):
        check("%s is not a body marker" % dead,
              not any(dead in m for m in every))


# ---------------------------------------------------------------------------
# 2. The scoping bug that would ship a confidently wrong dataset
# ---------------------------------------------------------------------------

def test_carousels_are_not_a_listing():
    """A hub and the HOME PAGE both carry real BaseProduct nodes that belong
    to recommendation strips. Reading every BaseProduct in the blob returned
    24 rows for `/b/Tools/...` and 22 for `/`, each labelled with whatever
    category the caller asked for — real product data attached to the wrong
    question."""
    import product_parser as P
    for name in ("hub_tools.html", "home_recommender.html"):
        html = fixture(name)
        state = P.apollo_state(html)
        check("%s HAS BaseProduct nodes" % name, len(P._base_products(state)) > 0)
        eq("%s yields no listing rows" % name,
           len(P.parse_category(html, url="https://www.homedepot.com/b/Tools/N-5yc1vZc1xy")),
           0)
        eq("%s has no listing model" % name, P.search_model(state), None)


def test_listing_products_are_scoped_to_the_grid():
    import product_parser as P
    html = fixture("listing_mitersaws.html")
    state = P.apollo_state(html)
    model = P.search_model(state)
    check("listing has a plppage model", model is not None)
    eq("contentType", model["metadata"]["contentType"], "plppage")
    eq("scoped products == grid refs",
       len(P.listing_products(state)),
       len(P._param_field(model, "products")))


def test_product_page_does_not_return_a_neighbour():
    """A PDP's blob also holds 'compare similar' strips. Returning the first
    BaseProduct found would answer with a neighbour's product under this URL."""
    import product_parser as P
    html = fixture("product_dws780.html")
    rows = P.parse_product(html, url="https://www.homedepot.com/p/x/321488310")
    eq("pdp returns one row", len(rows), 1)
    eq("pdp row is the requested item", rows[0].sku, "321488310")
    eq("a PDP asked for the WRONG id returns nothing",
       len(P.parse_product(html, url="https://www.homedepot.com/p/x/999999999")), 0)
    eq("a page with no id in the URL returns nothing",
       len(P.parse_product(fixture("hub_tools.html"),
                           url="https://www.homedepot.com/b/Tools/N-5yc1vZc1xy")), 0)


# ---------------------------------------------------------------------------
# 3. Field extraction — VALUES on real fixtures, not coverage
# ---------------------------------------------------------------------------

def test_listing_values():
    import product_parser as P
    rows = P.parse_category(
        fixture("listing_mitersaws.html"),
        url="https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7")
    check("listing rows", len(rows) == 4, "(got %d)" % len(rows))
    by_sku = {r.sku: r for r in rows}
    # Pinned VALUES on a real fixture, not coverage: a column can be 100%
    # populated and entirely wrong.
    dewalt = by_sku.get("321488310")
    check("dewalt row present", dewalt is not None,
          "(fixture skus: %r)" % sorted(by_sku))
    if dewalt:
        eq("dewalt brand", dewalt.brand, "DEWALT")
        eq("dewalt model", dewalt.model_number, "DWS780")
        eq("dewalt price", dewalt.price, 649.0)
        eq("dewalt currency", dewalt.currency, "USD")
        eq("dewalt store sku", dewalt.store_sku, "1008223977")
        eq("dewalt parent", dewalt.parent_id, "341241630")
        eq("dewalt hierarchy", dewalt.category_hierarchy,
           ["Tools", "Power Tools", "Saws", "Miter Saws"])
        eq("dewalt price_source", dewalt.price_source, "apollo-searchnav")
        eq("dewalt store", dewalt.store_id, "121")
        eq("dewalt availability", dewalt.availability, "InStock")
        # Two DIFFERENT pools, pinned apart. A version that took max() of
        # the store-located services reported 23 units of a saw with 3 on
        # the shelf.
        eq("dewalt shelf quantity (bopis)", dewalt.store_quantity, 3)
        eq("dewalt local delivery pool", dewalt.local_delivery_quantity, 23)
        eq("dewalt badge", dewalt.badges, ["Top Rated"])
    eq("row_index follows the site's own sort order",
       [r.row_index for r in rows], [0, 1, 2, 3])
    for row in rows:
        check("row %s has a sku" % row.sku, bool(row.sku))
        check("row %s url is a /p/ url" % row.sku, "/p/" in row.url)
        check("row %s image has no <SIZE> placeholder" % row.sku,
              "<SIZE>" not in (row.image_url or ""))


def test_product_values():
    import product_parser as P
    rows = P.parse_product(fixture("product_dws780.html"),
                           url="https://www.homedepot.com/p/x/321488310")
    row = rows[0]
    # `sku` is productID (the URL id), NOT the JSON-LD `sku` field, which is
    # the STORE sku. Taking the latter would give a listing row and a product
    # row of the same item two different keys.
    eq("pdp sku is the itemId", row.sku, "321488310")
    eq("pdp store_sku is the JSON-LD sku", row.store_sku, "1008223977")
    eq("pdp gtin13", row.gtin13, "0885911906913")
    eq("pdp price", row.price, 649.0)
    eq("pdp currency", row.currency, "USD")
    eq("pdp brand", row.brand, "DEWALT")
    eq("pdp model", row.model_number, "DWS780")
    eq("pdp colour", row.color, "Yellow")
    eq("pdp rating", row.rating, 4.7)
    eq("pdp review count", row.review_count, 1451)
    eq("pdp price_source", row.price_source, "jsonld+apollo")
    eq("pdp hierarchy from breadcrumbs", row.category_hierarchy,
       ["Tools", "Power Tools", "Saws", "Miter Saws"])
    # PINNED KNOWN LIMITATION: a PDP publishes no stock figure in either
    # source (the JSON-LD offer has no `availability` key and the Apollo node
    # has no `fulfillment`). Asserting the CURRENT behaviour so a future
    # change is a decision rather than a surprise.
    eq("pdp availability is null, and that is measured", row.availability, None)


def test_store_scoped_pricing():
    """The pricing field key is parameterised and store-scoped. Looking it up
    by equality returns None the moment the store or the argument order
    changes."""
    import product_parser as P
    state = P.apollo_state(fixture("listing_mitersaws.html"))
    node = P._base_products(state)[0][1]
    check("pricing found by prefix", P._param_field(node, "pricing") is not None)
    args = P._param_field_args(node, "pricing")
    eq("pricing args carry the store", args.get("storeId"), "121")
    store = P.store_context(state)
    eq("store id", store["store_id"], "121")
    eq("store name", store["store_name"], "Cumberland")
    eq("store postcode", store["store_postal_code"], "30339")


def test_availability_reads_location_type_not_service_type():
    """Regression, and it cost 11 of 12 rows.

    Three services appear and they do not mean the same thing:
        pickup/bopis            location.type "store"   -> the shelf
        pickup/boss             location.type "online"  -> the network
        delivery/express        location.type "store"   -> the shelf
    `boss` is "buy online, ship to store": filed under pickup, but its
    quantity is the distribution network's. Keying on `service.type ==
    "bopis"` nulled store_quantity for every product whose store offers
    `boss` instead."""
    import product_parser as P
    node = {
        "availabilityType": {"discontinued": False},
        'fulfillment({"storeId":"121"})': {"fulfillmentOptions": [
            {"type": "pickup", "services": [{"type": "boss", "locations": [
                {"isAnchor": True, "type": "online",
                 "inventory": {"isInStock": True, "quantity": 85}}]}]},
            {"type": "delivery", "services": [{"type": "express delivery",
             "locations": [{"isAnchor": True, "type": "store",
                            "inventory": {"isInStock": True, "quantity": 2}}]}]},
        ]},
    }
    eq("boss quantity is not taken as store stock",
       P.availability_from_node(node), ("InStock", None, 2))
    # Discontinued is a fact and wins.
    eq("discontinued wins",
       P.availability_from_node({"availabilityType": {"discontinued": True}}),
       ("Discontinued", None, None))
    # No fulfilment data at all is None, never a guessed "OutOfStock".
    eq("no fulfilment data is null", P.availability_from_node({}),
       (None, None, None))


def test_availability_ignores_buyable():
    """`availabilityType.buyable` is about the super-SKU PARENT being directly
    purchasable, not about stock. Item 321488310 has buyable=false and
    status=false while its store reports 3 units in stock."""
    import product_parser as P
    node = {
        "availabilityType": {"buyable": False, "status": False,
                             "discontinued": False},
        'fulfillment({"storeId":"1"})': {"fulfillmentOptions": [
            {"type": "pickup", "services": [{"type": "bopis", "locations": [
                {"isAnchor": True, "type": "store",
                 "inventory": {"isInStock": True, "quantity": 3}}]}]}]},
    }
    eq("buyable=false does not mean out of stock",
       P.availability_from_node(node), ("InStock", 3, None))


def test_discount_is_computed_not_read():
    import product_parser as P
    eq("normal discount", P._discount_pct(399.0, 499.0), 20.04)
    eq("equal prices are not a discount", P._discount_pct(100.0, 100.0), None)
    eq("original BELOW price is not a negative discount",
       P._discount_pct(349.0, 299.0), None)
    eq("missing original", P._discount_pct(100.0, None), None)
    eq("zero original", P._discount_pct(100.0, 0.0), None)


def test_no_row_has_original_at_or_below_its_price():
    """One line, and it catches the regression forever."""
    import product_parser as P
    rows = P.parse_category(fixture("listing_mitersaws.html"))
    bad = [r for r in rows if r.original_price is not None and r.price is not None
           and r.original_price <= r.price]
    eq("no original_price at or below price", len(bad), 0)


def test_image_size_placeholder():
    import product_parser as P
    eq("placeholder resolved to preferred size",
       P._resolve_image({"url": "https://x/a_<SIZE>.jpg",
                         "sizes": ["65", "600", "1000"]}),
       "https://x/a_600.jpg")
    eq("falls back to the largest available",
       P._resolve_image({"url": "https://x/a_<SIZE>.jpg", "sizes": ["65", "100"]}),
       "https://x/a_100.jpg")
    # Returning the raw `<SIZE>` string would hand every consumer a URL that
    # is a guaranteed 404.
    eq("no sizes means no url, not a broken one",
       P._resolve_image({"url": "https://x/a_<SIZE>.jpg", "sizes": []}), None)
    eq("a url without a placeholder is passed through",
       P._resolve_image({"url": "https://x/a.jpg"}), "https://x/a.jpg")


# ---------------------------------------------------------------------------
# 4. JSON-LD shapes that are legal and break a naive parser
# ---------------------------------------------------------------------------

def test_jsonld_hostile_shapes():
    import product_parser as P
    # An EXPLICIT null: a `.get("offers", {})` default does not apply.
    eq("offers null", P._first_offer({"offers": None}), {})
    eq("offers list with non-dicts",
       P._first_offer({"offers": ["x", {"price": 1}]}), {"price": 1})
    eq("offers absent", P._first_offer({}), {})
    eq("image as ImageObject",
       P._jsonld_image({"image": {"@type": "ImageObject", "url": "u"}}), "u")
    eq("image as contentUrl",
       P._jsonld_image({"image": [{"contentUrl": "c"}]}), "c")
    eq("image as string", P._jsonld_image({"image": "s"}), "s")
    eq("image absent", P._jsonld_image({}), None)
    # A product inside @graph rather than at the top level: the shape that
    # silently reports an empty page.
    doc = '<script type="application/ld+json">%s</script>' % json.dumps(
        {"@graph": [{"@type": "Product", "productID": "1", "name": "n",
                     "offers": {"price": 5, "priceCurrency": "USD"}}]})
    check("@graph product is found", P.jsonld_product(doc) is not None)
    # A list at the top level, which the real PDP uses.
    doc2 = '<script type="application/ld+json">%s</script>' % json.dumps(
        [{"@type": "WebPage"}, {"@type": "Product", "productID": "2"}])
    eq("top-level list", P.jsonld_product(doc2)["productID"], "2")
    # Malformed JSON must not raise.
    eq("malformed jsonld is skipped",
       P.jsonld_product('<script type="application/ld+json">{oops</script>'), None)


def test_currency_is_never_invented():
    import product_parser as P
    row = P.product_from_jsonld({"productID": "1", "name": "n",
                                 "offers": {"price": 5}},
                                url="https://www.homedepot.com/p/x/1")
    eq("no priceCurrency means no currency", row.currency, None)


# ---------------------------------------------------------------------------
# 5. The Apollo scanner
# ---------------------------------------------------------------------------

def test_apollo_scanner_is_string_aware():
    """A regex cannot do this. The blob is ~160 KB of product copy containing
    every brace and quote character there is, and `\\"` inside a description is
    common: a naive scan cut the real blob off 357 bytes early and raised
    `Extra data`, which reads like a site change rather than our own bug."""
    import product_parser as P
    tricky = {"a": 'a } brace and a \\" quote and a {nested} word', "b": {"c": 1}}
    html = "<script>window.__APOLLO_STATE__=%s;</script>" % json.dumps(tricky)
    eq("scanner survives braces inside strings", P.apollo_state(html), tricky)
    eq("no blob at all is None, not a raise", P.apollo_state("<html></html>"), None)
    eq("truncated blob is None, not a raise",
       P.apollo_state('<script>window.__APOLLO_STATE__={"a":</script>'), None)


def test_param_field_lookup_by_prefix():
    import product_parser as P
    node = {'pricing({"storeId":"999","z":1})': {"value": 5}}
    eq("found whatever the args are", P._param_field(node, "pricing"),
       {"value": 5})
    eq("args decoded", P._param_field_args(node, "pricing")["storeId"], "999")
    eq("missing field is None", P._param_field(node, "nope"), None)


# ---------------------------------------------------------------------------
# 6. URLs and pagination
# ---------------------------------------------------------------------------

def test_pagination():
    import product_parser as P
    base = "https://www.homedepot.com/b/X/N-1"
    eq("page 1 has no Nao", P.page_url(base, 1), base)
    eq("page 2 is an OFFSET, not an index", P.page_url(base, 2), base + "?Nao=12")
    eq("page 3", P.page_url(base, 3), base + "?Nao=24")
    # Calling twice must not produce ?Nao=12&Nao=24.
    eq("idempotent", P.page_url(P.page_url(base, 2), 3), base + "?Nao=24")
    eq("existing params preserved",
       P.page_url(base + "?a=1", 2), base + "?a=1&Nao=12")
    eq("page 1 strips an existing Nao", P.page_url(base + "?Nao=24", 1), base)


def test_url_helpers():
    import product_parser as P
    eq("sku from url", P.sku_from_url("/p/Some-Slug/321488310"), "321488310")
    eq("sku from a slugless url", P.sku_from_url("/p/321488310"), "321488310")
    eq("no sku", P.sku_from_url("/b/Tools/N-1"), None)
    eq("category slug",
       P.category_from_url("https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7"),
       "Tools-Power-Tools-Saws-Miter-Saws")
    eq("category id",
       P.category_id_from_url("https://www.homedepot.com/b/A-B/N-5yc1vZc2d7"),
       "N-5yc1vZc2d7")
    eq("featured-products is not a category",
       P.category_from_url("https://www.homedepot.com/b/Featured-Products/N-1"), None)
    check("a one-segment /b/ path looks like a hub",
          P.is_hub_url("https://www.homedepot.com/b/Tools/N-5yc1vZc1xy"))
    check("a multi-segment one does not",
          not P.is_hub_url("https://www.homedepot.com/b/Tools-Power-Tools-Saws/N-1"))


def test_unsupported_hosts_are_refused_with_a_reason():
    """'is not a Home Depot site' would be FALSE for homedepot.ca and would
    send the reader looking for a typo."""
    import product_parser as P
    ok, reason = P.supported_host("https://www.homedepot.ca/b/x/N-1")
    check("homedepot.ca refused", not ok)
    check("reason names Canada", "Canada" in (reason or ""), "(got %r)" % reason)
    ok2, reason2 = P.supported_host("https://www.homedepot.com.mx/b/x/N-1")
    check("homedepot.com.mx refused", not ok2)
    check("reason names Mexico", "Mexico" in (reason2 or ""))
    check("www.homedepot.com accepted",
          P.supported_host("https://www.homedepot.com/b/x/N-1")[0])
    check("a wholly unrelated host is refused",
          not P.supported_host("https://example.com/b/x/N-1")[0])


def test_total_products_is_not_used_to_paginate():
    """`totalProducts` is live inventory: it moved 144 -> 143 in four minutes.
    It must appear in the sidecar and NOWHERE in a stopping decision."""
    import product_parser as P
    source = inspect.getsource(P)
    for module_name in ("http_scraper", "browser_bridge"):
        module = __import__(module_name)
        text = inspect.getsource(module)
        # It may be READ into facts; it must not be compared against anything.
        for bad in ("totalProducts >", "totalProducts <", "total_products_reported >",
                    "total_products_reported <", "// PAGE_SIZE", "/ PAGE_SIZE"):
            check("%s does not paginate on %s" % (module_name, bad.strip()),
                  bad not in text)


# ---------------------------------------------------------------------------
# 7. Output contract
# ---------------------------------------------------------------------------

def test_schema_and_exit_codes():
    import output_writer as O
    names = [f.name for f in dataclasses.fields(O.Product)]
    eq("family prefix is first and in order", names[:10],
       ["source", "scraped_at", "url", "sku", "title", "image_url", "price",
        "currency", "category", "price_source"])
    eq("source", O.SOURCE_DEFAULT, "homedepot.com")
    eq("exit blocked", O.EXIT_BLOCKED, 3)
    eq("exit no products", O.EXIT_NO_PRODUCTS, 4)
    eq("exit remote api", O.EXIT_REMOTE_API_ERROR, 5)
    eq("exit partial", O.EXIT_PARTIAL, 6)


def test_empty_result_does_not_overwrite():
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        rows = [O.Product(sku="1", title="t", price=1.0, currency="USD")]
        O.save(rows, prefix, "json")
        check("first run wrote", os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        rc = O.save([], prefix, "json")
        eq("empty result exits 4", rc, O.EXIT_NO_PRODUCTS)
        eq("empty result left the data alone", open(prefix + ".json").read(), before)
        O.save([], prefix, "json", allow_empty=True)
        eq("--allow-empty does overwrite", json.load(open(prefix + ".json")), [])


def test_stale_metadata_cannot_report_false_success():
    """The P0 an audit found in a sibling repo: a blocked run left the previous
    `.meta.json` saying `status: complete` beside the previous data, and a
    pipeline reading the files after the run could not tell that from a fresh
    success."""
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        rows = [O.Product(sku="1", title="t", price=1.0, currency="USD")]
        O.finish_run(rows, prefix, "json", False, blocked=False,
                     stop_reason="completed", pages_requested=1,
                     pages_completed=1, start_url="u", final_url="u")
        first = json.load(open(prefix + ".meta.json"))
        eq("first run is complete", first["status"], "complete")
        rc = O.finish_run([], prefix, "json", False, blocked=True,
                          stop_reason="blocked", pages_requested=1,
                          pages_completed=0, start_url="u", final_url="u")
        eq("blocked run exits 3", rc, O.EXIT_BLOCKED)
        dataset = json.load(open(prefix + ".meta.json"))
        attempt = json.load(open(prefix + O.ATTEMPT_META_SUFFIX))
        eq("the DATASET sidecar still describes the data beside it",
           dataset["status"], "complete")
        eq("the ATTEMPT sidecar records the failure", attempt["status"], "failed")
        eq("and says the data was not updated", attempt["data_updated"], False)
        check("attempt sidecar records blocked", attempt["blocked"] is True)
        check("every run gets a run_id", bool(attempt.get("run_id")))


def test_writes_are_atomic():
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "x.json")
        O._atomic_write(target, "good")
        try:
            O._atomic_write(target, None)  # type: ignore[arg-type]
        except Exception:
            pass
        eq("a failed write left the previous file intact",
           open(target).read(), "good")
        leftovers = [n for n in os.listdir(tmp) if n.startswith(".tmp-")]
        eq("no temp file left behind", leftovers, [])


def test_empty_csv_keeps_its_header():
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "e.csv")
        O.write_csv([], path)
        first = open(path).readline().strip()
        check("header present", first.startswith("source,scraped_at,url,sku"),
              "(got %r)" % first[:60])


def test_dedupe_is_page_ordered():
    import output_writer as O
    seen = set()
    page1 = [O.Product(sku="a"), O.Product(sku="b")]
    page2 = [O.Product(sku="b"), O.Product(sku="c")]
    out = O.dedupe_by_sku(page1, seen) + O.dedupe_by_sku(page2, seen)
    eq("merged in page order", [r.sku for r in out], ["a", "b", "c"])


# ---------------------------------------------------------------------------
# 8. Credentials
# ---------------------------------------------------------------------------

def test_credentials_never_reach_free_text():
    import browser_bridge
    # Deliberately shaped like the real thing and deliberately not one: the
    # repo's own secret scanner refuses a committed URL that looks like it
    # carries a live credential, and it is right to.
    secret = "ws://" + "EXAMPLE-LOGIN" + ":" + "EXAMPLE-PASSWORD" + \
             "@cb.2captcha.com:9222"
    masked = browser_bridge.mask_text(
        "connect failed for %s and again for %s" % (secret, secret))
    check("password gone", "EXAMPLE-PASSWORD" not in masked)
    check("masked GLOBALLY, not just the first occurrence",
          masked.count("***:***@") == 2, "(got %r)" % masked)
    check("host and port kept — which exit a run used is the point of the log",
          "cb.2captcha.com:9222" in masked)


def test_mask_text_is_not_proxy_pool_mask():
    """`proxy_pool.mask()` is for a URL. Handed an exception message it
    returns '?://?' and destroys it."""
    import browser_bridge
    import proxy_pool
    message = "ProxyError: could not reach http://u:p@host:1 while fetching /b/X"
    check("mask_text keeps the message",
          "while fetching /b/X" in browser_bridge.mask_text(message))
    check("and still removes the password",
          "u:p@" not in browser_bridge.mask_text(message))


def test_env_keys():
    import env_config
    eq("env keys", set(env_config.ENV_KEYS),
       {"TWOCAPTCHA_KEY", "HOMEDEPOT_CDP_ENDPOINT", "HOMEDEPOT_PROXY",
        "HOMEDEPOT_URL"})


def test_env_example_documents_exactly_what_is_read():
    import env_config
    path = os.path.join(HERE, ".env.example")
    if not os.path.exists(path):
        SKIPS.append("env_example_missing")
        print("SKIP .env.example absent")
        return
    documented = set()
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            documented.add(line.split("=", 1)[0])
    eq("documented == read", documented, set(env_config.ENV_KEYS))


def test_no_secret_in_tracked_files():
    """A 32-hex string in a public repo reads as a live credential to every
    scanner that looks, including this family's own CI grep.

    Scans EVERY file git is tracking, not a list of extensions. The first
    version checked `.py/.md/.txt/.yml/.toml` only, and on 2026-09-22 a
    `.env.user-endpoint.bak` holding three live credentials was committed and
    pushed straight past it — `.gitignore` named `.env` exactly and the backup
    had a suffix, and `.bak` was not on the extension list. Both holes are
    closed; this is the one that matters, because it does not depend on
    anybody remembering to add an extension.
    """
    import re
    import subprocess
    # A credentials-shaped URL is EVERYWHERE in this repo's documentation, as
    # `http://user:pass@host:port` and `ws://{login}:{password}@…` templates
    # and as `***:***@` in masked log output. Flagging those would make the
    # check noise, and a check that is always red teaches everyone to ignore
    # it. So a match only counts when NEITHER half of the userinfo looks like
    # a placeholder.
    PLACEHOLDER = re.compile(
        r"^(\*+|user|username|login|pass|password|passwd|secret|token|key|"
        r"changeme|example[\w-]*|your[_\w-]*|xxx+|\.\.\.|<[^>]*>|\{[^}]*\}|"
        r"[A-Z][A-Z0-9_-]*)$", re.I)

    def real_credential(match):
        user, password = match.group(1), match.group(2)
        if PLACEHOLDER.match(user) or PLACEHOLDER.match(password):
            return False
        # A real one here is long and mixed-case/alphanumeric.
        return len(user) >= 8 and len(password) >= 8

    patterns = [
        ("a 32-hex credential",
         re.compile(r"(?<![/\w])[0-9a-f]{32}(?!\.(?:avif|jpe?g|png|webp|gif|svg))\b"),
         None),
        ("a url carrying a real password",
         re.compile(r"[a-z]+://([^/@:\s\"'<>]+):([^/@\s\"'<>]+)@"),
         real_credential),
    ]
    try:
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=HERE,
                                 capture_output=True, text=True, timeout=60)
    except Exception:
        SKIPS.append("secret scan (git unavailable)")
        print("SKIP secret scan (git unavailable)")
        return
    if tracked.returncode != 0:
        SKIPS.append("secret scan (not a git repo)")
        print("SKIP secret scan (not a git repo)")
        return
    names = [n for n in tracked.stdout.split("\0") if n]
    check("git is tracking files", len(names) > 10, "(got %d)" % len(names))
    offenders = []
    for name in names:
        # The suite itself carries the patterns it hunts for, and the fixtures
        # are documented as scrubbed and checked separately.
        if name in ("smoke_test.py", ".gitignore"):
            continue
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for label, pattern, judge in patterns:
            for match in pattern.finditer(text):
                if judge is not None and not judge(match):
                    continue
                if judge is None and "example" in match.group(0).lower():
                    continue
                offenders.append("%s: %s" % (name, label))
                break
    eq("no tracked file carries a credential", offenders, [])


def test_the_scanner_catches_the_file_that_actually_leaked():
    """The regression test for the 2026-09-22 incident.

    A `.env.user-endpoint.bak` with three live credentials was committed and
    pushed to a public repo. `.gitignore` named `.env` exactly so the suffix
    slipped past it, and the secret check walked a list of extensions that did
    not include `.bak`.

    This reconstructs that file's SHAPE — never its values — and asserts the
    scanner refuses it on several independent grounds, so removing any one
    pattern still leaves it caught.
    """
    import subprocess
    scanner = os.path.join(HERE, "tools", "scan_secrets.py")
    check("the scanner exists", os.path.exists(scanner))
    if not os.path.exists(scanner):
        return
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import importlib
    scan = importlib.import_module("scan_secrets")

    leaked = ("TWOCAPTCHA_KEY=" + "0123456789abcdef" * 2 + "\n"
              "HOMEDEPOT_PROXY=http://uabc123def456-zone-custom-region-us"
              ":uabc123def456@na.proxy.2captcha.com:2334\n"
              "HOMEDEPOT_CDP_ENDPOINT=ws://bc9876fedcba-zone-scraping_browser"
              "-country-us-pid-pdeadbeef:Qq7RtYuIoPaSdF@cb.2captcha.com:9222\n")
    found = scan.scan_text(".env.user-endpoint.bak", leaked)
    check("the leaked file is refused", len(found) > 0)
    joined = " | ".join(found)
    check("caught by FILENAME (a .env variant)", ".env file" in joined)
    check("caught by FILENAME (a backup suffix)", "backup" in joined)
    check("caught by the 32-hex key shape", "32-hex" in joined)
    check("caught by the proxy login shape", "proxy gateway" in joined)
    check("caught by the Scraping Browser login shape",
          "Scraping Browser" in joined)
    check("several independent grounds, not one",
          len(found) >= 4, "(got %d)" % len(found))

    # And it must stay QUIET on this repo's own documentation, or nobody will
    # leave it switched on.
    for name in (".env.example", "env_config.py", "README.md"):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            hits = scan.scan_text(name, handle.read())
        eq("no false positive on %s" % name, hits, [])


def test_hooks_are_versioned_and_wired():
    """A hook living only in .git/hooks protects exactly one clone."""
    for name in ("pre-commit", "pre-push"):
        path = os.path.join(HERE, ".githooks", name)
        check(".githooks/%s exists" % name, os.path.exists(path))
        if os.path.exists(path):
            check(".githooks/%s is executable" % name, os.access(path, os.X_OK))
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            check(".githooks/%s runs the shared scanner" % name,
                  "scan_secrets.py" in body)
    installer = os.path.join(HERE, "tools", "install_hooks.sh")
    check("tools/install_hooks.sh exists", os.path.exists(installer))
    if os.path.exists(installer):
        with open(installer, encoding="utf-8") as handle:
            body = handle.read()
        check("the installer uses core.hooksPath, not .git/hooks",
              "core.hooksPath" in body)


def test_only_one_secret_scanner_implementation():
    """A second implementation of a security check is a second place for it to
    be wrong — which is exactly how the incident happened: CI had its own
    extension list and it was not the same one the suite used."""
    path = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.exists(path):
        SKIPS.append("ci_checks absent")
        return
    with open(path, encoding="utf-8") as handle:
        body = handle.read()
    check("ci_checks delegates to the shared scanner",
          "scan_secrets.py" in body)
    check("and no longer walks its own extension list",
          '".py", ".md"' not in body and "'.py', '.md'" not in body)


def test_gitignore_covers_every_env_variant():
    """`.gitignore` naming `.env` exactly is not enough.

    A `.env.user-endpoint.bak` was committed on 2026-09-22 for precisely this
    reason. The pattern has to cover the family.
    """
    path = os.path.join(HERE, ".gitignore")
    text = open(path, encoding="utf-8").read()
    for pattern in (".env", ".env.*", "!.env.example"):
        check("gitignore has %r" % pattern,
              any(line.strip() == pattern for line in text.splitlines()))


# ---------------------------------------------------------------------------
# 9. Engine parity and structure
# ---------------------------------------------------------------------------

ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")


def test_engines_import_their_driver_at_module_level():
    """If an engine imports its driver inside start(), the module imports
    cleanly with the library absent, this suite never skips it, and the CI job
    that exists to fail on unexpected skips cannot catch a broken import."""
    for name in ENGINES:
        path = os.path.join(HERE, name + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        top = {n for node in tree.body
               if isinstance(node, (ast.Import, ast.ImportFrom))
               for n in [getattr(node, "module", None) or ""]}
        wanted = {"playwright_scraper": "playwright.sync_api",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}[name]
        check("%s imports %s at module level" % (name, wanted),
              any(t.startswith(wanted.split(".")[0]) for t in top if t),
              "(top-level imports: %r)" % sorted(t for t in top if t))


def test_engine_parity():
    """All three engines must expose the SAME flags, because they are compared
    against one definition rather than three hand-kept copies."""
    import browser_bridge
    shared = {a.dest for a in browser_bridge.build_parser("x")._actions}
    loaded = 0
    for name in ENGINES:
        try:
            module = __import__(name)
        except ImportError:
            SKIPS.append(name)
            print("SKIP %s (engine library absent)" % name)
            continue
        loaded += 1
        check("%s has a main()" % name, callable(getattr(module, "main", None)))
        driver_cls = [v for k, v in vars(module).items()
                      if k.endswith("Driver") and isinstance(v, type)]
        check("%s defines one driver" % name, len(driver_cls) == 1)
        if driver_cls:
            for method in ("start", "navigate", "content", "count", "attr",
                           "sleep", "stop"):
                check("%s.%s" % (name, method),
                      callable(getattr(driver_cls[0], method, None)))
            check("%s driver has a name" % name,
                  isinstance(getattr(driver_cls[0], "name", None), str))
    # NO "at least one engine was importable" assertion here, deliberately.
    # This suite's contract is that it passes with NO engine library installed
    # at all — that is what makes it offline, and the `offline` CI job installs
    # none on purpose. An earlier version asserted `loaded >= 1`; it passed on
    # a developer machine with Playwright installed and failed both offline
    # matrix jobs, which is the assertion being wrong rather than the job.
    #
    # The job that DOES check an engine loads is `engine-smoke`, which installs
    # one per venv and fails on an unexpected skip. That is the right place for
    # it: there, an absent engine is a real problem; here it is the point.
    if loaded == 0:
        print("note: no engine library installed — engine groups skipped, "
              "which is the offline contract, not a failure")

    # The shared parser is the single source of truth for the flags, and it
    # needs no engine, so these run whatever is installed.
    for flag in ("url", "mode", "pages", "format", "out", "allow_empty",
                 "dump_html", "cdp_endpoint", "proxy", "twocaptcha_key",
                 "solve_captcha", "min_score", "headless"):
        check("shared parser defines --%s" % flag.replace("_", "-"),
              flag in shared)


def test_a_transport_fault_is_not_a_block():
    """A navigation timeout means the browser never got an answer. Reporting
    it as exit 3 tells a consumer the SITE is refusing this scraper, and sends
    the reader to the anti-bot problem when the answer is "run it again"."""
    import browser_bridge
    from output_writer import EXIT_REMOTE_API_ERROR

    class Args:
        mode = "category"; url = "https://www.homedepot.com/b/A-B/N-1"
        pages = 1; category = None; max_products = None; delay = 0
        timeout = 1; format = "json"; out = "unused"; allow_empty = False
        dump_html = None; verbose = False; solve_captcha = "never"
        twocaptcha_key = None

    class Driver:
        name = "fake"
        def start(self): pass
        def navigate(self, url):
            raise TimeoutError("Page.goto: Timeout 45000ms exceeded")
        def content(self): return ""
        def count(self, s): return 0
        def attr(self, s, n): return None
        def sleep(self, ms): pass
        def stop(self): pass

    with tempfile.TemporaryDirectory() as tmp:
        args = Args()
        args.out = os.path.join(tmp, "o")
        rc = browser_bridge.run(args, Driver())
    eq("a navigation timeout exits 5, not 3", rc, EXIT_REMOTE_API_ERROR)


def test_autosolve_is_armed_and_its_failures_are_warnings():
    """Rung 1a: `Captcha.setAutoSolve` on the Scraping Browser's CDP session.

    Two things have to be true at once. It must actually be armed, and a
    endpoint that does NOT implement the domain must not break a run — on
    this site the wall is an Akamai 403 and a JS interstitial, so an endpoint
    without a Captcha domain is still a perfectly good endpoint and refusing
    to run would trade a working scrape for a missing feature.
    """
    try:
        import playwright_scraper
    except ImportError:
        SKIPS.append("autosolve (playwright absent)")
        print("SKIP autosolve (playwright absent)")
        return

    class FakeCdp:
        def __init__(self, fail_on=()):
            self.sent = []
            self.handlers = {}
            self.fail_on = fail_on
        def send(self, method, params=None):
            if method in self.fail_on:
                raise RuntimeError("%s wasn't found" % method)
            self.sent.append((method, params))
            return {}
        def on(self, event, handler):
            self.handlers[event] = handler

    class FakeContext:
        def __init__(self, cdp):
            self.cdp = cdp
        def new_cdp_session(self, page):
            return self.cdp

    def driver_with(cdp, solve="when-blocked"):
        args = argparse.Namespace(solve_captcha=solve, timeout=10,
                                  cdp_endpoint="ws://x", headless=True)
        d = playwright_scraper.PlaywrightDriver(args)
        d._context = FakeContext(cdp)
        d._page = object()
        return d

    import argparse

    # 1. armed, and the events are subscribed
    cdp = FakeCdp()
    d = driver_with(cdp)
    d._arm_autosolve()
    check("Captcha.setAutoSolve is sent",
          any(m == "Captcha.setAutoSolve" for m, _ in cdp.sent))
    check("autoSolve is actually true",
          any(p and p.get("autoSolve") is True for m, p in cdp.sent
              if m == "Captcha.setAutoSolve"))
    check("autosolve reports armed", d.autosolve_armed is True)
    for event in ("Captcha.solveFinished", "Captcha.solveFailed",
                  "Captcha.detected"):
        check("subscribed to %s" % event, event in cdp.handlers)

    # events are COUNTED, not just logged — a run has to be able to report
    # whether the remote solver did anything.
    cdp.handlers["Captcha.solveFinished"]({"ok": True})
    eq("a solve event is recorded", len(d.autosolve_events), 1)
    eq("and names itself", d.autosolve_events[0]["event"], "Captcha.solveFinished")

    # 2. an endpoint without the domain is a warning, not a crash
    cdp2 = FakeCdp(fail_on=("Captcha.setAutoSolve", "Captcha.enable"))
    d2 = driver_with(cdp2)
    d2._arm_autosolve()   # must not raise
    check("an unsupported Captcha domain does not raise", True)
    check("and is not reported as armed", d2.autosolve_armed is False)
    check("and the reason is kept", len(d2.autosolve_notes) >= 1)

    # 3. --solve-captcha never means never
    cdp3 = FakeCdp()
    d3 = driver_with(cdp3, solve="never")
    d3._arm_autosolve()
    eq("--solve-captcha never arms nothing", cdp3.sent, [])


def test_selenium_refuses_cdp_and_strips_proxy_credentials():
    """Selenium's two real limits, asserted rather than left in a docstring.

    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put the endpoint's password, and `--proxy-server=` cannot authenticate
    at all. Both are REFUSED or WARNED, never silently ignored — a user must
    not believe a `user:pass` URL is doing something.
    """
    try:
        import selenium_scraper
    except ImportError:
        SKIPS.append("selenium guards (engine absent)")
        print("SKIP selenium guards (engine absent)")
        return
    import browser_bridge
    import contextlib
    import io

    args = argparse.Namespace(
        cdp_endpoint="ws://" + "EXAMPLE-USER" + ":" + "EXAMPLE-PASS"
                     + "@cb.2captcha.com:9222",
        headless=True, timeout=5, proxy=None, proxy_file=None,
        proxy_rotate="per-run", proxy_shuffle=False, proxy_sessions=None)
    raised = None
    try:
        selenium_scraper.SeleniumDriver(args).start()
    except Exception as exc:
        raised = exc
    check("selenium refuses --cdp-endpoint",
          isinstance(raised, browser_bridge.BridgeError),
          "(got %r)" % type(raised).__name__)
    if raised:
        message = str(raised)
        check("and names the actual reason", "debuggerAddress" in message)
        check("and points at an engine that can", "playwright" in message.lower())
        check("without echoing the password", "EXAMPLE-PASS" not in message)

    args2 = argparse.Namespace(
        cdp_endpoint=None, headless=True, timeout=5,
        proxy="http://" + "EXAMPLE-USER" + ":" + "EXAMPLE-PASS"
              + "@proxy.invalid:2334",
        proxy_file=None, proxy_rotate="per-run", proxy_shuffle=False,
        proxy_sessions=None)
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            selenium_scraper.SeleniumDriver(args2).start()
    except Exception:
        pass   # chromedriver is not installed here; the warning fires first
    warning = stderr.getvalue()
    check("selenium warns that it stripped the credential",
          "STRIPPED" in warning, "(got %r)" % warning[:80])
    check("the password is not in the warning", "EXAMPLE-PASS" not in warning)
    # An earlier version printed the MASKED url here, which reads as "the
    # credential is being sent, just hidden from you" — the opposite of what
    # happens, and the whole point of the warning is that it is gone.
    check("the warning does not imply credentials are still being sent",
          "***:***@" not in warning)
    check("the warning names the bare address actually passed to Chrome",
          "http://proxy.invalid:2334" in warning)


def test_pyppeteer_keeps_the_password_off_the_command_line():
    """Chromium's `--proxy-server=` lands in the browser process's argv, where
    anything that can run `ps` reads it. The address goes there; the
    credentials go through `page.authenticate`."""
    try:
        import puppeteer_scraper
    except ImportError:
        SKIPS.append("pyppeteer split (engine absent)")
        print("SKIP pyppeteer split (engine absent)")
        return
    bare, user, password = puppeteer_scraper._split(
        "http://" + "EXAMPLE-USER" + ":" + "EXAMPLE-PASS" + "@proxy.invalid:2334")
    eq("the bare address carries no credential", bare, "http://proxy.invalid:2334")
    eq("the username is returned separately", user, "EXAMPLE-USER")
    eq("so is the password", password, "EXAMPLE-PASS")
    bare2, user2, _ = puppeteer_scraper._split("http://proxy.invalid:2334")
    eq("a credential-less proxy is unchanged", bare2, "http://proxy.invalid:2334")
    eq("and reports no user", user2, None)
    source = inspect.getsource(puppeteer_scraper)
    check("credentials go through page.authenticate", "authenticate(" in source)


def test_no_javascript_crosses_the_bridge():
    """Selenium's execute_script takes a function BODY with an explicit
    `return`; Playwright and pyppeteer take an arrow function. A shared module
    that passed JS would quietly acquire one driver's dialect.

    Scanned over CODE only, with docstrings and comments stripped: this
    module's own prose names both dialects in order to explain them, and the
    first version of this check failed on its own documentation.
    """
    import browser_bridge
    tree = ast.parse(inspect.getsource(browser_bridge))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    for token in ("() =>", "return document.", "execute_script"):
        check("bridge code contains no %r" % token, token not in code)


def test_removed_flags_stay_removed():
    """Scoped to the ENGINES: `--country` is banned on a scraper (it could
    disagree with the URL) and legitimate on fingerprint_client.py, where it
    picks a fingerprint locale."""
    import browser_bridge
    import http_scraper
    for module in (browser_bridge, http_scraper):
        text = inspect.getsource(module)
        for banned in ('"--country"', '"--antidetect"', "ANTIDETECT_LOCAL_API"):
            check("%s does not define %s" % (module.__name__, banned),
                  banned not in text)


def test_retries_means_retries():
    """`--retries 0` must perform ONE attempt, not zero. The family shipped the
    other reading and it produced a run that fetched nothing and exited 0."""
    import http_scraper
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200
        url = "https://www.homedepot.com/b/X/N-1"
        headers = {}
        text = fixture("listing_mitersaws.html")

    class FakeSession:
        def get(self, url, **kwargs):
            calls["n"] += 1
            return FakeResponse()

    http_scraper.fetch(FakeSession(), "https://www.homedepot.com/b/X/N-1", 5,
                       retries=0)
    eq("--retries 0 performs one attempt", calls["n"], 1)


def test_negative_numbers_are_refused_at_argparse():
    import http_scraper
    for argv in (["--pages", "0"], ["--pages", "-1"], ["--retries", "-1"],
                 ["--delay", "-1"], ["--timeout", "0"]):
        try:
            http_scraper.parse_args(["--url", "https://www.homedepot.com/b/X/N-1"] + argv)
        except SystemExit:
            PASSES.append("refused %s" % " ".join(argv))
            print("PASS refused %s" % " ".join(argv))
        else:
            FAILURES.append("accepted %s" % " ".join(argv))
            print("FAIL accepted %s" % " ".join(argv))


def test_curl_session_keeps_the_proxy_off_argv():
    """`ps` reads argv, and argv lands in shell history and in CI logs.

    curl's own `--proxy` flag would put the password there, so the proxy line
    goes in on stdin via `--config -`. This is the check that keeps it there.
    """
    import http_scraper
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = "<html></html>\n200 120 0 300 13 https://www.homedepot.com/b/A-B/N-1"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        return FakeCompleted()

    real_run = http_scraper.subprocess.run
    http_scraper.subprocess.run = fake_run
    try:
        # Assembled from parts: the repo's own secret scanner refuses a
        # committed URL that LOOKS like it carries a live credential, and it
        # is right to — that is the check this test exists to complement.
        session = http_scraper.CurlSession(
            "http://" + "EXAMPLE-USER" + ":" + "EXAMPLE-PASS"
            + "@" + "proxy.example.com:1234")
        response = session.get("https://www.homedepot.com/b/A-B/N-1", timeout=30)
    finally:
        http_scraper.subprocess.run = real_run

    argv = " ".join(captured["command"])
    check("the password is not in argv", "EXAMPLE-PASS" not in argv)
    check("the proxy host is not in argv either", "proxy.example.com" not in argv)
    check("--proxy is not used at all", "--proxy" not in argv)
    check("the proxy arrives on stdin",
          "EXAMPLE-PASS" in (captured["input"] or ""))
    check("stdin is curl config format",
          (captured["input"] or "").startswith("proxy = "))
    check("--config - is passed", "--config" in captured["command"])
    # Forcing curl to HTTP/1.1 got the interstitial instead of content in the
    # same measurement, so the version is pinned deliberately.
    check("HTTP/2 is requested explicitly", "--http2" in captured["command"])
    check("compression is requested", "--compressed" in captured["command"])
    eq("status parsed off the write-out line", response.status_code, 200)
    eq("body has the status line stripped", response.text, "<html></html>")


# Assembled from parts so the repo's own secret scanners do not read it as a
# committed credential. They are right to refuse one; see
# test_no_secret_in_tracked_files.
PLACEHOLDER_PROXY = "http://" + "EXAMPLE-USER" + ":" + "EXAMPLE-PASS" + "@proxy.invalid:1"


def test_a_dead_proxy_credential_is_not_a_site_block():
    """A 407 from the proxy means the site was never reached.

    Reporting it as exit 3 sends the reader to the anti-bot problem — new
    exits, longer backoff, a browser — when the answer is "the credential in
    HOMEDEPOT_PROXY no longer authenticates". It happened for real on
    2026-09-22 and the run reported `blocked`.

    It must also not be RETRIED or ROTATED: every exit on a dead credential
    fails identically, so a run that rotated would burn its whole budget
    discovering that.
    """
    import http_scraper
    from output_writer import EXIT_REMOTE_API_ERROR

    class Curl407:
        returncode = 56
        stdout = ""
        stderr = "curl: (56) CONNECT tunnel failed, response 407"

    real_run = http_scraper.subprocess.run
    http_scraper.subprocess.run = lambda *a, **k: Curl407()
    try:
        session = http_scraper.CurlSession(PLACEHOLDER_PROXY)
        raised = None
        try:
            session.get("https://www.homedepot.com/b/A-B/N-1", timeout=5)
        except Exception as exc:
            raised = exc
        check("a 407 raises ProxyAuthError",
              isinstance(raised, http_scraper.ProxyAuthError),
              "(got %r)" % type(raised).__name__)

        # and fetch() must let it out rather than retrying it
        rotations = {"n": 0}
        def rotate():
            rotations["n"] += 1
            return session, "masked"
        escaped = None
        try:
            http_scraper.fetch(session, "https://www.homedepot.com/b/A-B/N-1",
                               5, retries=3, retry_delay=0, rotate=rotate)
        except Exception as exc:
            escaped = exc
        check("fetch does not swallow it",
              isinstance(escaped, http_scraper.ProxyAuthError))
        eq("and does not rotate onto another dead exit", rotations["n"], 0)
    finally:
        http_scraper.subprocess.run = real_run

    # A status-line 407 (no curl error) is caught too.
    class Curl407Status:
        returncode = 0
        stdout = "\n407 https://www.homedepot.com/b/A-B/N-1"
        stderr = ""
    http_scraper.subprocess.run = lambda *a, **k: Curl407Status()
    try:
        session = http_scraper.CurlSession(PLACEHOLDER_PROXY)
        raised = None
        try:
            session.get("https://www.homedepot.com/b/A-B/N-1", timeout=5)
        except Exception as exc:
            raised = exc
        check("a 407 status line also raises ProxyAuthError",
              isinstance(raised, http_scraper.ProxyAuthError))
    finally:
        http_scraper.subprocess.run = real_run

    check("ProxyAuthError maps to the remote-error exit, not to blocked",
          EXIT_REMOTE_API_ERROR == 5)


def test_curl_session_sends_the_measured_headers():
    import http_scraper
    session = http_scraper.CurlSession(None)
    for name in ("user-agent", "accept", "accept-language", "sec-fetch-mode",
                 "sec-ch-ua", "upgrade-insecure-requests"):
        check("curl session sends %s" % name, name in session.headers)
    # A Chrome 140 user agent beside a Chrome 133 client hint is itself a
    # mismatch. If one is bumped, the other has to move with it.
    check("the UA and the client hint agree on the Chrome version",
          http_scraper.CHROME_MAJOR in session.headers["user-agent"]
          and http_scraper.CHROME_MAJOR in session.headers["sec-ch-ua"])


def test_default_http_client_prefers_the_one_that_works():
    """Defaulting to the client MEASURED to work rather than to the one that
    is a library: requests was refused on 6 of 6 exits where curl got content
    on 4 of the same 6."""
    import http_scraper
    eq("default follows curl's availability",
       http_scraper.default_http_client(),
       "curl" if http_scraper.CURL_AVAILABLE else "requests")


def test_fetch_rotates_on_a_refusal_not_on_a_timeout():
    import http_scraper
    rotations = {"n": 0}

    class Blocked:
        status_code = 403
        url = "u"
        headers = {}
        text = fixture("blocked_access_denied.html")

    class Session:
        def get(self, url, **kwargs):
            return Blocked()

    def rotate():
        rotations["n"] += 1
        return Session(), "masked"

    http_scraper.fetch(Session(), "https://www.homedepot.com/b/X/N-1", 5,
                       retries=2, retry_delay=0, rotate=rotate)
    check("a 403 rotates the exit", rotations["n"] >= 1,
          "(got %d)" % rotations["n"])


def test_unresolved_names():
    """`compileall` proves a file PARSES, not that its names RESOLVE. A live
    run in a sibling repo died with NameError on a line reached only while
    fetching, after an import was removed — the module imported cleanly,
    --help worked, compileall passed and the whole offline suite was green.

    Kept COARSE (pooled bindings, no scope tracking) so it under-reports
    rather than inventing problems."""
    import builtins
    modules = ["product_parser", "output_writer", "page_flow", "http_scraper",
               "browser_bridge", "catalog_walk", "env_config", "proxy_pool",
               "proxy_meter", "diff_runs"]
    def bind_args(node, bound):
        a = getattr(node, "args", None)
        if a is None:
            return
        for group in (a.posonlyargs, a.args, a.kwonlyargs):
            for arg in group:
                bound.add(arg.arg)
        for extra in (a.vararg, a.kwarg):
            if extra:
                bound.add(extra.arg)

    for name in modules:
        path = os.path.join(HERE, name + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                (bound if isinstance(node.ctx, ast.Store) else used).add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(node.name)
                bind_args(node, bound)
            elif isinstance(node, ast.Lambda):
                # A lambda's parameters are bindings too. Missing them is what
                # made the first version of this check report `kv` and `m` as
                # unresolved names in four modules that were perfectly fine.
                bind_args(node, bound)
            elif isinstance(node, ast.ClassDef):
                bound.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global):
                bound.update(node.names)
        missing = sorted(used - bound)
        eq("%s has no unresolved names" % name, missing, [])


def test_dockerfile_copies_every_module_the_entrypoint_imports():
    """An explicit COPY list is right — the image should carry no test suite
    and no stray .env — but it falls behind, and CI never builds the image.
    All three repos in this family shipped an image that died with
    ModuleNotFoundError on every invocation, --help included."""
    path = os.path.join(HERE, "Dockerfile")
    if not os.path.exists(path):
        SKIPS.append("dockerfile")
        print("SKIP Dockerfile absent")
        return
    text = open(path, encoding="utf-8").read()
    local = {n[:-3] for n in os.listdir(HERE) if n.endswith(".py")}
    needed = set()
    for entry in ("playwright_scraper", "http_scraper", "catalog_walk"):
        tree = ast.parse(open(os.path.join(HERE, entry + ".py"), encoding="utf-8").read())
        needed.add(entry)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in local:
                        needed.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module in local:
                needed.add(node.module)
    # one transitive hop
    for name in list(needed):
        tree = ast.parse(open(os.path.join(HERE, name + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in local:
                        needed.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module in local:
                needed.add(node.module)
    missing = sorted(m for m in needed if (m + ".py") not in text)
    eq("Dockerfile COPYs every module the entrypoints import", missing, [])
    # Scanned over the COPY lines only: the Dockerfile's own comments
    # legitimately mention `.env` (to say a credential is mounted, never
    # baked) and name this file (to say what checks the COPY list).
    copied = []
    continuing = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # A comment line may end in a backslash (the usage example does)
            # without continuing anything. Skipping comments first is what
            # keeps this check off the Dockerfile's own prose.
            continue
        if continuing or stripped.upper().startswith("COPY"):
            copied.append(stripped)
            continuing = stripped.endswith("\\")
    copied = " ".join(copied)
    check("image bakes in no .env — that would publish a credential to "
          "everyone who can pull it", ".env" not in copied)
    check("image carries no test suite", "smoke_test.py" not in copied)
    check("image carries no fixtures", "fixtures" not in copied)


def test_fixtures_carry_no_session_material():
    """A real page dump carries the session that fetched it.

    Guarded with PATTERNS rather than with the literals that were scrubbed, so
    a future capture's values are caught too — the whole point is that the
    NEXT person's dump gets scrubbed, not just this one.
    """
    import re
    patterns = [
        ("an Akamai challenge token",
         re.compile(r"\?v=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")),
        ("a request-trace reference",
         re.compile(r"Reference&#32;&#35;[0-9]")),
        # A 32-hex string reads as a live credential to every scanner that
        # looks, including this family's own CI grep. But an image CDN names
        # its files by content hash — `salsify-ecdn.com/images/<32hex>.avif`
        # appears 40+ times in the product fixture — so the check excludes a
        # hex that is part of an asset URL. A real credential in a capture
        # sits in a cookie or a JSON field, not in an <img srcset>.
        ("a 32-hex credential",
         re.compile(r"(?<![/\w])[0-9a-f]{32}(?!\.(?:avif|jpe?g|png|webp|gif|svg))\b")),
        ("an _abck cookie value", re.compile(r"_abck=[A-Za-z0-9]")),
        ("a bm_sz cookie value", re.compile(r"bm_sz=[A-Za-z0-9]")),
        ("a url with a password", re.compile(r"://[^/@\s\"']+:[^/@\s\"']+@")),
    ]
    for name in sorted(os.listdir(FIXTURES)):
        if not name.endswith(".html"):
            continue
        text = fixture(name)
        for label, pattern in patterns:
            check("%s carries no %s" % (name, label),
                  pattern.search(text) is None)


def test_banned_wordings():
    """Product naming is enforced, because the original brief predates it."""
    banned = ("cloud browser", "antidetect browser", "2scraper antidetect",
              "gate.2prx.com")
    for name in os.listdir(HERE):
        if not name.endswith((".py", ".md")):
            continue
        if name == "smoke_test.py":
            continue
        text = open(os.path.join(HERE, name), encoding="utf-8",
                    errors="replace").read().lower()
        for phrase in banned:
            check("%s does not say %r" % (name, phrase), phrase not in text)


def test_no_site_specific_drift_from_a_sibling_repo():
    """The family's copy-paste drift check. A Givenchy audit found
    Transfermarkt vocabulary all over a repo that had never touched football."""
    foreign = ("bershka", "givenchy", "farfetch", "mediamarkt", "transfermarkt",
               "spieler", "itxrest", "demandware")
    for name in os.listdir(HERE):
        if not name.endswith(".py") or name == "smoke_test.py":
            continue
        text = open(os.path.join(HERE, name), encoding="utf-8",
                    errors="replace").read().lower()
        for word in foreign:
            check("%s is free of %r" % (name, word), word not in text)


# ---------------------------------------------------------------------------
# Run outcomes, output files and the paid rung — added 2026-10-14
# ---------------------------------------------------------------------------

LISTING_URL = "https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7"


class _BridgeArgs:
    """The browser engines' parsed flags, for driving browser_bridge.run."""

    def __init__(self, out, **overrides):
        self.mode = "category"
        self.url = LISTING_URL
        self.pages = 2
        self.category = None
        self.max_products = None
        self.delay = 0
        self.timeout = 1
        self.format = "both"
        self.out = out
        self.allow_empty = False
        self.dump_html = None
        self.verbose = False
        self.solve_captcha = "when-blocked"
        self.twocaptcha_key = None
        self.captcha_api = "v2"
        self.min_score = 0.7
        self.headless = True
        for key, value in overrides.items():
            setattr(self, key, value)


class _PageDriver:
    """A fake browser serving `pages[n]` for page n (1-based). A value that is
    an exception instance is raised from navigate()."""

    name = "fake"

    def __init__(self, pages):
        self.pages = pages
        self.current = ""
        self.visits = 0

    def start(self):
        pass

    def navigate(self, url):
        self.visits += 1
        page = self.pages[min(self.visits, len(self.pages)) - 1]
        if isinstance(page, Exception):
            raise page
        self.current = page
        return 200

    def content(self):
        return self.current

    def count(self, selector):
        return 50 if self.current else 0

    def attr(self, selector, name):
        return None

    def sleep(self, ms):
        pass

    def stop(self):
        pass


def _quiet(fn, *args, **kwargs):
    """Call `fn` with stdout/stderr captured; returns (result, text)."""
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


def test_finish_run_exit_matrix():
    """0 rows means three different things, and an unknown reason must not
    read as "the catalogue is empty"."""
    import output_writer as O
    row = O.Product(sku="1", title="t", price=1.0, currency="USD")
    cases = (
        ([], False, "transport_failed", O.EXIT_REMOTE_API_ERROR),
        ([], False, "parser_found_nothing", O.EXIT_REMOTE_API_ERROR),
        ([], False, "a_reason_nobody_added_yet", O.EXIT_REMOTE_API_ERROR),
        ([], True, "blocked", O.EXIT_BLOCKED),
        ([], False, "hub_not_a_listing", O.EXIT_NO_PRODUCTS),
        ([], False, "no_new_products", O.EXIT_NO_PRODUCTS),
        ([row], False, "transport_failed", O.EXIT_PARTIAL),
        ([row], False, "completed", 0),
    )
    with tempfile.TemporaryDirectory() as tmp:
        for i, (rows, blocked, reason, want) in enumerate(cases):
            rc, _ = _quiet(O.finish_run, rows, os.path.join(tmp, "r%d" % i),
                           "json", False, blocked=blocked, stop_reason=reason,
                           pages_requested=2, pages_completed=1,
                           start_url="u", final_url="u")
            eq("finish_run(%d rows, %s) exit" % (len(rows), reason), rc, want)


def test_a_transport_fault_after_rows_keeps_the_rows():
    """Page 1 served, page 2 timed out: the rows of page 1 are written and the
    run is partial (6), with the attempt sidecar beside them."""
    import browser_bridge
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "o")
        driver = _PageDriver([fixture("listing_mitersaws.html"),
                              TimeoutError("Page.goto: Timeout exceeded")])
        rc, _ = _quiet(browser_bridge.run, _BridgeArgs(out), driver)
        eq("transport fault after rows exits 6", rc, O.EXIT_PARTIAL)
        check("page 1's rows were written", os.path.exists(out + ".json"))
        rows = json.load(open(out + ".json")) if os.path.exists(out + ".json") else []
        eq("all four page-1 rows kept", len(rows), 4)
        attempt = out + O.ATTEMPT_META_SUFFIX
        check("attempt sidecar written", os.path.exists(attempt))
        if os.path.exists(attempt):
            meta = json.load(open(attempt))
            eq("stop reason names the transport", meta["stop_reason"],
               "transport_failed")
            eq("page 2 recorded as failed", meta["pages_failed"], [2])
            eq("scope carries the store", meta["scope"]["store_id"], "121")


def test_an_empty_document_is_not_a_block():
    """39 bytes with none of the site's assets is a navigation that produced
    nothing — not Akamai's refusal (which is ~370 bytes)."""
    import browser_bridge
    import output_writer as O
    with tempfile.TemporaryDirectory() as tmp:
        driver = _PageDriver(["<html><head></head><body></body></html>"])
        rc, text = _quiet(browser_bridge.run,
                          _BridgeArgs(os.path.join(tmp, "o"), pages=1), driver)
        eq("an empty document exits 5, not 3", rc, O.EXIT_REMOTE_API_ERROR)
        check("the message says the document was empty",
              "EMPTY document" in text, "(got %r)" % text[-200:])


def test_a_listing_that_parses_to_nothing_is_a_parser_failure():
    import browser_bridge
    import output_writer as O
    import product_parser as P
    original = P.parse_category
    P.parse_category = lambda *a, **k: []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _PageDriver([fixture("listing_mitersaws.html")])
            rc, text = _quiet(browser_bridge.run,
                              _BridgeArgs(os.path.join(tmp, "o"), pages=1),
                              driver)
            meta = json.load(open(os.path.join(tmp, "o") + O.ATTEMPT_META_SUFFIX))
    finally:
        P.parse_category = original
    eq("a served grid parsed to 0 rows exits 5, not 4", rc,
       O.EXIT_REMOTE_API_ERROR)
    eq("and says why", meta["stop_reason"], "parser_found_nothing")


def test_usage_errors_exit_2():
    import browser_bridge
    import http_scraper
    def run(url):
        # A string SystemExit is exit 1 to the shell, so it is caught and
        # read rather than allowed to end the suite.
        try:
            with tempfile.TemporaryDirectory() as tmp:
                return _quiet(browser_bridge.run,
                              _BridgeArgs(os.path.join(tmp, "o"), url=url),
                              _PageDriver([""]))[0]
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1

    eq("browser engine: a foreign host is exit 2",
       run("https://www.homedepot.ca/b/x"), 2)
    eq("browser engine: no URL is exit 2", run(None), 2)
    for argv in (["--url", "https://www.homedepot.ca/x"], []):
        code = None
        try:
            _quiet(http_scraper.parse_args, argv)
        except SystemExit as exc:
            code = exc.code
        eq("http engine: %r is exit 2" % (argv,), code, 2)


def test_a_typed_category_url_beats_the_env_file():
    import http_scraper
    saved = os.environ.get("HOMEDEPOT_URL")
    os.environ["HOMEDEPOT_URL"] = "https://www.homedepot.com/b/From-Env/N-1"
    try:
        args = http_scraper.parse_args(
            ["--category-url", "https://www.homedepot.com/b/Typed/N-2"])
    finally:
        if saved is None:
            os.environ.pop("HOMEDEPOT_URL", None)
        else:
            os.environ["HOMEDEPOT_URL"] = saved
    eq("typed --category-url wins", args.url,
       "https://www.homedepot.com/b/Typed/N-2")


def _http_run(get, tmp, pages="2"):
    import http_scraper
    original = http_scraper.build_session

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return get(url)

    session = Session()
    http_scraper.build_session = lambda *a, **k: session
    try:
        rc, text = _quiet(http_scraper.main, [
            "--url", LISTING_URL, "--pages", pages, "--retries", "1",
            "--retry-delay", "0", "--delay", "0", "--format", "json",
            "--out", os.path.join(tmp, "o")])
    finally:
        http_scraper.build_session = original
    return rc, session.calls, text


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text
        self.url = LISTING_URL
        self.headers = {}


def test_http_transport_failure_is_not_a_block():
    import output_writer as O

    def dead(url):
        raise ConnectionError("Connection reset by peer")

    with tempfile.TemporaryDirectory() as tmp:
        rc, calls, _ = _http_run(dead, tmp)
        eq("http: no answer at all exits 5, not 3", rc, O.EXIT_REMOTE_API_ERROR)
        meta = json.load(open(os.path.join(tmp, "o") + O.ATTEMPT_META_SUFFIX))
        eq("http: stop reason", meta["stop_reason"], "transport_failed")

    listing = fixture("listing_mitersaws.html")

    def second_page_dies(url):
        if "Nao=" not in url:
            return _Resp(200, listing)
        raise ConnectionError("Connection reset by peer")

    with tempfile.TemporaryDirectory() as tmp:
        rc, _, _ = _http_run(second_page_dies, tmp)
        eq("http: rows then a dead connection exits 6", rc, O.EXIT_PARTIAL)


def test_http_does_not_ask_a_refusing_exit_again():
    import output_writer as O
    blocked = fixture("blocked_access_denied.html")
    with tempfile.TemporaryDirectory() as tmp:
        rc, calls, _ = _http_run(lambda url: _Resp(403, blocked), tmp, pages="1")
    eq("http: a 403 with no other exit is blocked", rc, O.EXIT_BLOCKED)
    eq("http: and the same exit is not asked again", calls, 1)


def test_output_modes_follow_the_umask():
    """mkstemp creates 0600 and os.replace keeps it; the writer must not."""
    import output_writer as O
    import stat
    if os.name != "posix":
        SKIPS.append("output modes (not posix)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        for umask, want in ((0o022, 0o644), (0o002, 0o664), (0o077, 0o600)):
            path = os.path.join(tmp, "m%o.json" % umask)
            old = os.umask(umask)
            try:
                O._atomic_write(path, "x")
            finally:
                os.umask(old)
            eq("new file under umask %03o" % umask,
               oct(stat.S_IMODE(os.stat(path).st_mode)), oct(want))
        path = os.path.join(tmp, "existing.json")
        open(path, "w").write("old")
        os.chmod(path, 0o640)
        O._atomic_write(path, "new")
        eq("an existing file keeps its mode",
           oct(stat.S_IMODE(os.stat(path).st_mode)), oct(0o640))


def test_csv_formula_cells_are_neutralised():
    import csv as _csv
    import output_writer as O
    rows = [O.Product(sku="1", title='=HYPERLINK("http://x","y")',
                      brand="@SUM(A1)", badges=["-2+3", "ok"], price=-5.0,
                      store_quantity=-1, currency="USD")]
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "o")
        _quiet(O.finish_run, rows, prefix, "both", False, blocked=False,
               stop_reason="completed", pages_requested=1, pages_completed=1,
               start_url="u", final_url="u", extra={"csv_cells_escaped": "caller"})
        got = list(_csv.DictReader(open(prefix + ".csv", newline="")))[0]
        eq("formula title escaped", got["title"], "'=HYPERLINK(\"http://x\",\"y\")")
        eq("formula brand escaped", got["brand"], "'@SUM(A1)")
        eq("a joined list is escaped after the join", got["badges"], "'-2+3 | ok")
        eq("a negative NUMBER is untouched", got["price"], "-5.0")
        data = json.load(open(prefix + ".json"))[0]
        eq("JSON keeps the site's bytes", data["title"], '=HYPERLINK("http://x","y")')
        meta = json.load(open(prefix + ".meta.json"))
        eq("sidecar declares the escaped count", meta["csv_cells_escaped"], 3)
        eq("caller's own extra is not overwritten",
           meta["transport_facts"]["csv_cells_escaped"], "caller")


class _SolverStub:
    def __init__(self):
        self.calls = 0

    def __call__(self, challenge, key, **kwargs):
        self.calls += 1
        return "TOKEN-%d" % self.calls


CAPTCHA_PAGE = ('<html><body><div class="g-recaptcha" data-sitekey="'
                '6LfakeSitekeyForTheOfflineSuite00000000000"></div>'
                '<script>grecaptcha.render("x", {});</script></body></html>')


class _CaptchaDriver(_PageDriver):
    def __init__(self, html, inject=True, enterprise=False):
        super().__init__([html])
        self.current = html
        self.injected = []
        self.enterprise = enterprise
        if not inject:
            self.inject_captcha_token = None

    def recaptcha_discovery(self):
        return {"found": True, "sitekey": "6LfakeSitekeyForTheOfflineSuite00000000000",
                "size": "normal", "enterprise": self.enterprise, "hints": []}

    def inject_captcha_token(self, token):
        self.injected.append(token)
        return True


def test_the_paid_rung_is_wired_and_budgeted():
    """The rung used to call a function that does not exist, and the error was
    swallowed. With the solver stubbed (nothing is ever sent anywhere): one
    purchase per page, the token is injected, and nothing is bought that
    could not be used."""
    import browser_bridge
    import captcha_solver
    import page_flow
    stub = _SolverStub()
    original = captcha_solver.solve_recaptcha
    captcha_solver.solve_recaptcha = stub
    try:
        args = _BridgeArgs("unused", twocaptcha_key="KEY-NOT-REAL")
        budget = page_flow.SolveBudget()
        driver = _CaptchaDriver(CAPTCHA_PAGE)
        _quiet(browser_bridge.solve_if_captcha, driver, args, LISTING_URL,
               CAPTCHA_PAGE, budget=budget)
        eq("one solve bought", stub.calls, 1)
        eq("and its token injected", driver.injected, ["TOKEN-1"])
        # A second detection on the SAME page: the allowance is spent.
        _quiet(browser_bridge.solve_if_captcha, driver, args, LISTING_URL,
               CAPTCHA_PAGE, budget=budget)
        eq("the page's allowance is not exceeded", stub.calls,
           page_flow.SOLVES_PER_PAGE)
        # Nothing bought when the token could not be used.
        before = stub.calls
        _quiet(browser_bridge.solve_if_captcha, _CaptchaDriver(CAPTCHA_PAGE,
               inject=False), args, LISTING_URL, CAPTCHA_PAGE)
        _quiet(browser_bridge.solve_if_captcha, _CaptchaDriver(CAPTCHA_PAGE,
               enterprise=True), args, LISTING_URL, CAPTCHA_PAGE)
        _quiet(browser_bridge.solve_if_captcha, _CaptchaDriver(CAPTCHA_PAGE),
               _BridgeArgs("unused"), LISTING_URL, CAPTCHA_PAGE)
        eq("no purchase without injection, for Enterprise, or without a key",
           stub.calls, before)
        # fetch_page owns the allowance: one page, one purchase, even though
        # the captcha is still there after the solve.
        stub.calls = 0
        result = _quiet(browser_bridge.fetch_page, _CaptchaDriver(CAPTCHA_PAGE),
                        args, LISTING_URL)[0]
        eq("fetch_page buys at most one solve per page", stub.calls, 1)
        eq("and reports it", result["solves_bought"], 1)
    finally:
        captcha_solver.solve_recaptcha = original


def test_shared_module_attributes_exist():
    """Every `module.attr` used on a LOCAL module from the engines and the
    shared code must exist. `captcha_solver.solve_on_page` did not, and a
    broad `except` turned the AttributeError into a warning nobody read."""
    local = {os.path.splitext(n)[0] for n in os.listdir(HERE)
             if n.endswith(".py")}
    callers = ("browser_bridge.py", "http_scraper.py", "scraper_api_client.py",
               "playwright_scraper.py", "puppeteer_scraper.py",
               "selenium_scraper.py", "diff_runs.py", "catalog_walk.py")
    import importlib
    checked = 0
    for name in callers:
        tree = ast.parse(open(os.path.join(HERE, name), encoding="utf-8").read())
        aliases = {}
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in local:
                        aliases[a.asname or a.name] = a.name
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases
                    and node.value.id not in bound):
                module = importlib.import_module(aliases[node.value.id])
                checked += 1
                check("%s: %s.%s exists" % (name, node.value.id, node.attr),
                      hasattr(module, node.attr))
    check("the attribute check bound something", checked > 20,
          "(only %d)" % checked)


def test_page_flow_has_no_unread_policy():
    """A policy function nothing outside page_flow calls decides nothing."""
    import page_flow
    others = ""
    for name in os.listdir(HERE):
        if name.endswith(".py") and name not in ("page_flow.py", "smoke_test.py"):
            others += open(os.path.join(HERE, name), encoding="utf-8").read()
    for name, value in vars(page_flow).items():
        if name.startswith("_") or not callable(value):
            continue
        if getattr(value, "__module__", None) != "page_flow":
            continue
        check("page_flow.%s has a reader outside page_flow" % name,
              ("page_flow.%s" % name) in others)


def test_scraper_api_reads_http_code_and_masks_debug():
    import logging
    import scraper_api_client as S

    class Response:
        status_code = 200
        text = ""

        def __init__(self, verdict):
            self.verdict = verdict
            self.headers = {"x-debug": "proxy=http://someuser:SECRETPW@1.2.3.4:80 key=abc123"}

        def json(self):
            return {"status": self.verdict, "http_code": 403,
                    "body": fixture("blocked_access_denied.html"), "headers": {}}

    class Args:
        timeout = 30; country = "us"; cdp_endpoint = None
        twocaptcha_key = "KEY-NOT-REAL"

    original = S.requests.post
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Grab()
    S.logger.addHandler(handler)
    S.logger.setLevel(logging.INFO)
    try:
        for verdict in ("ok", "warn"):
            S.requests.post = lambda *a, **k: Response(verdict)
            got = S.fetch_one(Args(), LISTING_URL)
            eq("api verdict %r: target status comes from http_code" % verdict,
               got.status, 403)
    finally:
        S.requests.post = original
        S.logger.removeHandler(handler)
    text = " ".join(records)
    check("x-debug is logged masked", "SECRETPW" not in text and "abc123" not in text,
          "(got %r)" % text[:200])


def test_diff_runs_tracks_this_sites_columns_and_refuses_another_store():
    import dataclasses as dc
    import diff_runs
    import output_writer as O
    names = {f.name for f in dc.fields(O.Product)}
    for field in diff_runs.TRACKED_FIELDS:
        check("diff_runs tracks a real column: %s" % field, field in names)
    rows = json.load(open(os.path.join(HERE, "sample_output.json")))
    changed = [dict(r) for r in rows]
    changed[0]["store_quantity"] = (changed[0].get("store_quantity") or 0) + 5
    result = diff_runs.diff_rows(rows, changed)
    check("a store stock change is reported",
          any("store_quantity" in c["changes"] for c in result["changed"]))
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for label, store in (("old", "121"), ("new", "999")):
            path = os.path.join(tmp, label + ".json")
            json.dump([dict(r, store_id=store) for r in rows], open(path, "w"))
            json.dump({"status": "complete", "mode": "category",
                       "scope": {"store_id": store}},
                      open(os.path.join(tmp, label + ".meta.json"), "w"))
            paths.append(path)

        class Args:
            old, new = paths

        ok, _ = _quiet(diff_runs._check_comparable, Args())
        check("two different stores are refused", ok is False)


def test_worktree_scan_reads_untracked_files_and_skips_virtualenvs():
    """Run the real scanner in a throwaway repository: an untracked,
    not-ignored file holding a key shape is reported; one inside a directory
    holding pyvenv.cfg (whatever it is called) is not."""
    import shutil
    import subprocess
    if shutil.which("git") is None:
        SKIPS.append("worktree scan (git unavailable)")
        return
    key = "a" * 32
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tools"))
        shutil.copy(os.path.join(HERE, "tools", "scan_secrets.py"),
                    os.path.join(tmp, "tools", "scan_secrets.py"))
        subprocess.run(["git", "init", "-q", tmp], check=True)
        open(os.path.join(tmp, "tracked.py"), "w").write("X = 1\n")
        subprocess.run(["git", "-C", tmp, "add", "tracked.py"], check=True)
        os.makedirs(os.path.join(tmp, "myenv", "lib"))
        open(os.path.join(tmp, "myenv", "pyvenv.cfg"), "w").write("home = /usr\n")
        open(os.path.join(tmp, "myenv", "lib", "vendored.py"), "w").write("# %s\n" % key)
        scan = [sys.executable, os.path.join(tmp, "tools", "scan_secrets.py"),
                "--worktree"]
        clean = subprocess.run(scan, capture_output=True, text=True, cwd=tmp)
        eq("a virtualenv by pyvenv.cfg is skipped", clean.returncode, 0)
        open(os.path.join(tmp, "scratch.py"), "w").write("KEY = '%s'\n" % key)
        dirty = subprocess.run(scan, capture_output=True, text=True, cwd=tmp)
        eq("an untracked file with a key shape is reported", dirty.returncode, 1)
        check("the scan says how many files it read",
              "file(s) read" in dirty.stdout, "(got %r)" % dirty.stdout[:200])


def _tracked_text_files():
    import subprocess
    try:
        names = subprocess.run(["git", "ls-files", "-z"], cwd=HERE,
                               capture_output=True, text=True).stdout.split("\0")
    except OSError:
        names = []
    names = [n for n in names if n]
    if not names:
        for root, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")
                       and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
            names += [os.path.relpath(os.path.join(root, f), HERE) for f in files]
    return [n for n in names
            if not n.startswith("fixtures/") and not n.endswith((".png", ".gz"))]


def test_no_private_citations_in_shipped_files():
    """Public files state their rules in their own words. Tokens assembled
    from pieces so this file does not match itself."""
    import re
    banned = re.compile("|".join([
        re.escape("CLAUDE" + ".md"), "§" + r"\s?\d", "claude" + "_2scraper",
        "2scraper" + "_repo_rules", "play" + r"book\s*§"]), re.I)
    found = []
    for name in _tracked_text_files():
        try:
            text = open(os.path.join(HERE, name), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        if banned.search(text):
            found.append(name)
    eq("no tracked file cites a private instruction file", found, [])


def test_no_other_sites_vocabulary_in_any_shipped_file():
    """Extends the .py-only drift check to every tracked text file: contributor
    docs, issue templates, ignore files and requirements headers too."""
    words = ("bersh" + "ka", "indi" + "tex", "itx" + "rest", "women" + ".html",
             "max" + "-grids", "max" + "_grids")
    found = []
    for name in _tracked_text_files():
        if name == "smoke_test.py":
            continue
        try:
            text = open(os.path.join(HERE, name), encoding="utf-8").read().lower()
        except (OSError, UnicodeDecodeError):
            continue
        for word in words:
            if word in text:
                found.append("%s: %s" % (name, word))
    eq("no tracked file carries another site's vocabulary", found, [])


# ---------------------------------------------------------------------------
# Traffic savers: the proxy pre-check and media blocking
# ---------------------------------------------------------------------------

def _status_proxy(status):
    """A local stand-in for a proxy gateway that answers every request,
    CONNECT included, with `status`. Returns (server, port, hits)."""
    import http.server
    import socketserver
    import threading
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            hits.append((self.command, self.path))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Proxy-Authenticate", 'Basic realm="stub"')
            self.end_headers()
        do_CONNECT = do_GET = do_HEAD = _answer

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], hits


class _CountingDriver(_PageDriver):
    def __init__(self, pages):
        super().__init__(pages)
        self.started = 0

    def start(self):
        self.started += 1


def test_proxy_precheck_stops_before_the_browser():
    import browser_bridge
    import output_writer as W
    import proxy_pool
    for status, words in ((407, "refused the credential"), (402, "balance exhausted")):
        server, port, hits = _status_proxy(status)
        try:
            tmp = tempfile.mkdtemp()
            driver = _CountingDriver([fixture("listing_mitersaws.html")])
            proxy = "http://user:S3cretPw@127.0.0.1:%d" % port
            args = _BridgeArgs(os.path.join(tmp, "run"), proxy=proxy,
                               cdp_endpoint=None, allow_empty=True, timeout=5)
            rc, text = _quiet(browser_bridge.run, args, driver)
        finally:
            server.shutdown()
            server.server_close()
        eq("pre-check: a proxy %d exits 5" % status, rc, 5)
        eq("pre-check: ...before the browser is started (%d)" % status, driver.started, 0)
        eq("pre-check: ...after ONE request, for robots.txt (%d)" % status,
           hits, [("CONNECT", "www.homedepot.com:443")])
        check("pre-check: ...says why (%d)" % status,
              words in text and "HOMEDEPOT_PROXY" in text, text[-300:])
        check("pre-check: ...and never prints the credential (%d)" % status,
              "S3cretPw" not in text)
        meta_path = os.path.join(tmp, "run" + W.ATTEMPT_META_SUFFIX)
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        eq("pre-check: ...attempt sidecar failed/proxy_auth_failed (%d)" % status,
           (meta.get("status"), meta.get("stop_reason"), meta.get("data_updated")),
           ("failed", "proxy_auth_failed", False))
        check("pre-check: ...and the sidecar never holds the credential (%d)" % status,
              "S3cretPw" not in json.dumps(meta))
        eq("pre-check: ...no data or dataset sidecar, even with --allow-empty (%d)" % status,
           sorted(os.listdir(tmp)), ["run" + W.ATTEMPT_META_SUFFIX])
    # Not a gateway verdict: a site 403 through the tunnel, or an unreachable
    # proxy, keeps the run going exactly as before.
    server, port, hits = _status_proxy(403)
    try:
        got = proxy_pool.precheck_proxy("http://u:p@127.0.0.1:%d" % port,
                                        "https://www.homedepot.com/robots.txt", 5)
    finally:
        server.shutdown()
        server.server_close()
    eq("pre-check: a 403 is not a proxy failure", got, None)
    eq("pre-check: an unreachable proxy is not a proxy failure (transport keeps its path)",
       proxy_pool.precheck_proxy("http://u:p@127.0.0.1:1", "https://x.invalid/robots.txt", 3), None)
    eq("pre-check: no proxy, no request",
       proxy_pool.precheck_proxy(None, "https://x/robots.txt"), None)
    eq("pre-check: robots.txt of the start URL",
       proxy_pool.robots_url(LISTING_URL + "?Nao=24"), "https://www.homedepot.com/robots.txt")
    # A 403 pre-check continues into the browser.
    server, port, hits = _status_proxy(403)
    try:
        driver = _CountingDriver([fixture("listing_mitersaws.html")])
        rc, _ = _quiet(browser_bridge.run, _BridgeArgs(
            os.path.join(tempfile.mkdtemp(), "r"), pages=1, cdp_endpoint=None,
            proxy="http://u:p@127.0.0.1:%d" % port, timeout=5), driver)
    finally:
        server.shutdown()
        server.server_close()
    eq("pre-check: after a 403 the browser still starts", driver.started, 1)
    eq("pre-check: ...and the run is the run it was", rc, 0)
    from unittest import mock
    driver = _CountingDriver([fixture("listing_mitersaws.html")])
    with mock.patch.object(proxy_pool, "precheck_proxy",
                           side_effect=AssertionError("pre-check over CDP")):
        rc, _ = _quiet(browser_bridge.run, _BridgeArgs(
            os.path.join(tempfile.mkdtemp(), "c"), pages=1,
            cdp_endpoint="ws://127.0.0.1:1", proxy=None), driver)
    eq("pre-check: never run over CDP (the remote browser brings its own exit)", rc, 0)
    driver = _CountingDriver([fixture("listing_mitersaws.html")])
    with mock.patch.object(proxy_pool, "precheck_proxy",
                           side_effect=AssertionError("pre-check with no proxy")):
        rc, _ = _quiet(browser_bridge.run, _BridgeArgs(
            os.path.join(tempfile.mkdtemp(), "n"), pages=1,
            cdp_endpoint=None, proxy=None), driver)
    eq("pre-check: never run with no proxy", rc, 0)


def test_http_paths_treat_a_402_like_a_407():
    import http_scraper
    import output_writer as W
    from unittest import mock

    class Curl402:
        returncode = 56
        stdout = ""
        stderr = "curl: (56) CONNECT tunnel failed, response 402"
    with mock.patch.object(http_scraper.subprocess, "run", lambda *a, **k: Curl402()):
        try:
            http_scraper.CurlSession(PLACEHOLDER_PROXY).get(LISTING_URL, timeout=5)
            status = None
        except http_scraper.ProxyAuthError as exc:
            status = exc.status
    eq("curl: a CONNECT 402 raises ProxyAuthError(402)", status, 402)
    # requests: the gateway's CONNECT answer arrives inside a ProxyError.
    if http_scraper.requests is not None:
        for code in (407, 402):
            server, port, hits = _status_proxy(code)
            try:
                session = http_scraper.build_session(
                    "http://u:S3cretPw@127.0.0.1:%d" % port, "requests")
                try:
                    http_scraper.fetch(session, LISTING_URL, 5, retries=3, retry_delay=0)
                    got = None
                except http_scraper.ProxyAuthError as exc:
                    got = exc.status
            finally:
                server.shutdown()
                server.server_close()
            eq("requests: a CONNECT %d raises ProxyAuthError, not retried" % code,
               (got, len(hits)), (code, 1))
    for code, words in ((407, "refused the credential"), (402, "balance exhausted")):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(http_scraper, "scrape",
                               side_effect=http_scraper.ProxyAuthError(status=code)):
            rc, text = _quiet(http_scraper.main, ["--url", LISTING_URL, "--out",
                                                  os.path.join(tmp, "r"), "--allow-empty"])
        eq("http_scraper: a proxy %d exits 5" % code, rc, 5)
        check("http_scraper: ...names HOMEDEPOT_PROXY and says why (%d)" % code,
              words in text and "HOMEDEPOT_PROXY" in text, text[-300:])
        eq("http_scraper: ...only the attempt sidecar is written (%d)" % code,
           sorted(os.listdir(tmp)), ["r" + W.ATTEMPT_META_SUFFIX])
    # The first request's refusal, end to end through curl and the stub.
    if http_scraper.CURL_AVAILABLE:
        server, port, hits = _status_proxy(402)
        tmp = tempfile.mkdtemp()
        try:
            rc, text = _quiet(http_scraper.main, [
                "--url", LISTING_URL, "--out", os.path.join(tmp, "r"),
                "--proxy", "http://u:S3cretPw@127.0.0.1:%d" % port,
                "--http-client", "curl", "--timeout", "5", "--allow-empty"])
        finally:
            server.shutdown()
            server.server_close()
        meta_path = os.path.join(tmp, "r" + W.ATTEMPT_META_SUFFIX)
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        eq("http_scraper via curl: a gateway 402 exits 5 after one request",
           (rc, len(hits)), (5, 1))
        eq("http_scraper via curl: ...failed/proxy_auth_failed, no data even with --allow-empty",
           (meta.get("status"), meta.get("stop_reason"), sorted(os.listdir(tmp))),
           ("failed", "proxy_auth_failed", ["r" + W.ATTEMPT_META_SUFFIX]))
        check("http_scraper via curl: ...without printing the credential",
              "S3cretPw" not in text and "S3cretPw" not in json.dumps(meta))
    import catalog_walk
    server, port, _ = _status_proxy(407)
    try:
        rc, text = _quiet(catalog_walk.main, [
            "categories", "--timeout", "5",
            "--proxy", "http://u:S3cretPw@127.0.0.1:%d" % port])
    finally:
        server.shutdown()
        server.server_close()
    eq("catalog_walk: a proxy 407 exits 5, not 3", rc, 5)
    check("catalog_walk: ...without printing the credential",
          "S3cretPw" not in text and "refused the credential" in text, text[-300:])


def test_media_blocking_policy():
    import proxy_pool
    ns = argparse.Namespace
    eq("media: blocked behind a local proxy",
       proxy_pool.should_block_media(ns(load_media=False), proxy_in_use=True), True)
    eq("media: --load-media keeps it",
       proxy_pool.should_block_media(ns(load_media=True), proxy_in_use=True), False)
    eq("media: no proxy, nothing metered, nothing blocked",
       proxy_pool.should_block_media(ns(load_media=False), proxy_in_use=False), False)
    eq("media: never over CDP",
       proxy_pool.should_block_media(ns(load_media=False), proxy_in_use=True, over_cdp=True), False)
    eq("media: the hidden test switch forces it without a proxy",
       proxy_pool.should_block_media(ns(load_media=False, force_block_media=True),
                                     proxy_in_use=False), True)

    class Route:
        def __init__(self, kind):
            self.request = ns(resource_type=kind)
            self.done = None

        def abort(self, reason=None):
            self.done = ("abort", reason)

        def continue_(self):
            self.done = ("continue",)
    for kind, want in (("image", "abort"), ("media", "abort"), ("font", "abort"),
                       ("document", "continue"), ("script", "continue"),
                       ("xhr", "continue"), ("fetch", "continue"),
                       ("stylesheet", "continue")):
        route = Route(kind)
        proxy_pool.playwright_media_route(route)
        eq("media: playwright %s -> %s" % (kind, want), route.done[0], want)

    class Sel:
        def __init__(self):
            self.sent = []

        def execute_cdp_cmd(self, cmd, params):
            self.sent.append((cmd, params))
    sel = Sel()
    proxy_pool.selenium_block_media(sel)
    blocked = dict(sel.sent).get("Network.setBlockedURLs", {}).get("urls", [])
    check("media: selenium blocks images, video and fonts by URL",
          all(p in blocked for p in ("*.jpg", "*.webp?*", "*.mp4", "*.woff2")) and
          not any(p.startswith("*.js") or p.startswith("*.css") for p in blocked), blocked)

    import asyncio

    class Client:
        def __init__(self):
            self.sent, self.handlers = [], {}

        def on(self, name, fn):
            self.handlers[name] = fn

        async def send(self, cmd, params=None):
            self.sent.append((cmd, params))

    client = Client()

    async def create():
        return client
    page = ns(target=ns(createCDPSession=create))
    loop = asyncio.new_event_loop()
    try:
        async def drive():
            await proxy_pool.pyppeteer_block_media(page)
            client.handlers["Fetch.requestPaused"]({"requestId": "r1", "resourceType": "Image"})
            await asyncio.sleep(0)
        loop.run_until_complete(drive())
    finally:
        loop.close()
    patterns = dict(client.sent)["Fetch.enable"]["patterns"]
    eq("media: pyppeteer pauses only images, media and fonts",
       sorted(p["resourceType"] for p in patterns), ["Font", "Image", "Media"])
    eq("media: ...and fails what it paused",
       client.sent[-1], ("Fetch.failRequest", {"requestId": "r1", "errorReason": "BlockedByClient"}))
    # Every local-browser engine must consult the policy. A source check,
    # because the suite runs without a browser binary.
    for module, call in (("playwright_scraper", "playwright_media_route"),
                         ("selenium_scraper", "selenium_block_media"),
                         ("puppeteer_scraper", "pyppeteer_block_media")):
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("media: %s applies the policy" % module,
              "should_block_media(" in source and call in source)
    import browser_bridge
    parser = browser_bridge.build_parser("t")
    eq("media: --load-media is off by default",
       parser.parse_args(["--url", LISTING_URL]).load_media, False)
    text = parser.format_help()
    check("media: --load-media is documented in --help, the test switch is not",
          "--load-media" in text and "--force-block-media" not in text)
    # The sidecar records whether the browser blocked anything.
    driver = _CountingDriver([fixture("listing_mitersaws.html")])
    driver.media_blocked = True
    tmp = tempfile.mkdtemp()
    _quiet(browser_bridge.run, _BridgeArgs(os.path.join(tmp, "m"), pages=1), driver)
    meta = json.load(open(os.path.join(tmp, "m.meta.json")))
    eq("media: the sidecar records transport_facts.media_blocked",
       meta.get("transport_facts", {}).get("media_blocked"), True)


# ---------------------------------------------------------------------------
# Proxy traffic meter and --proxy-budget-mb (proxy_meter.py; its byte counts
# against a stub proxy in real browsers are in tests/test_proxy_meter.py)
# ---------------------------------------------------------------------------

def _sidecars(prefix):
    import output_writer as W
    out = []
    for suffix in (".meta.json", W.ATTEMPT_META_SUFFIX):
        path = prefix + suffix
        out.append(json.load(open(path)) if os.path.exists(path) else None)
    return out


def test_proxy_budget_http_scraper():
    import http_scraper
    import output_writer as W
    import proxy_meter
    listing = fixture("listing_mitersaws.html")
    blocked = fixture("blocked_access_denied.html")
    original = http_scraper.build_session

    def run(responses, argv, cost):
        class Session:
            calls = 0

            def get(self, url, **kwargs):
                Session.calls += 1
                proxy_meter.current().add(cost)
                return responses(url)
        http_scraper.build_session = lambda *a, **k: Session()
        tmp = tempfile.mkdtemp()
        try:
            rc, text = _quiet(http_scraper.main, [
                "--url", LISTING_URL, "--pages", "3", "--retries", "1", "--retry-delay", "0",
                "--delay", "0", "--format", "json", "--out", os.path.join(tmp, "o"), *argv])
        finally:
            http_scraper.build_session = original
            proxy_meter.start(None, active=False)
        return rc, Session.calls, text, tmp
    proxied = ["--proxy", "http://127.0.0.1:9"]
    # 6 MB a page against a 5 MB budget: page 1 spends it, page 2 is never asked.
    rc, calls, text, tmp = run(lambda u: _Resp(200, listing), proxied + ["--proxy-budget-mb", "5"], 6_000_000)
    meta, attempt = _sidecars(os.path.join(tmp, "o"))
    eq("proxy budget (http): rows, then the budget -> exit 6", rc, W.EXIT_PARTIAL)
    eq("proxy budget (http): ...and no second page is requested", calls, 1)
    eq("proxy budget (http): ...stop_reason and the three sidecar keys",
       (meta["stop_reason"], meta["proxy_bytes"], meta["proxy_budget_bytes"], meta["proxy_budget_exceeded"]),
       ("proxy_budget_exceeded", 6_000_000, 5_000_000, True))
    eq("proxy budget (http): ...the attempt sidecar agrees",
       (attempt["stop_reason"], attempt["proxy_budget_exceeded"]), ("proxy_budget_exceeded", True))
    eq("proxy budget (http): ...one summary line", text.count("Proxy traffic: 6.0 MB of 5 MB budget"), 1)
    # Spent on a refused page: no rows -> 5, never 3 or 4; the data is not replaced.
    rc, calls, text, tmp = run(lambda u: _Resp(403, blocked), proxied + ["--proxy-budget-mb", "1"], 2_000_000)
    meta, attempt = _sidecars(os.path.join(tmp, "o"))
    eq("proxy budget (http): no rows -> exit 5", rc, W.EXIT_REMOTE_API_ERROR)
    eq("proxy budget (http): ...no dataset sidecar, the attempt one says why",
       (meta, attempt["stop_reason"], attempt["proxy_bytes"], attempt["data_updated"]),
       (None, "proxy_budget_exceeded", 2_000_000, False))
    # The retry loop stops too: a challenge that would be retried is not asked again.
    challenge = fixture("challenge_akamai.html")
    rc, calls, text, tmp = run(lambda u: _Resp(200, challenge), proxied + ["--proxy-budget-mb", "1"], 2_000_000)
    eq("proxy budget (http): no retry after the budget is spent", (calls, rc), (1, W.EXIT_REMOTE_API_ERROR))
    # No proxy: nothing metered, nothing stopped, no summary line.
    rc, calls, text, tmp = run(lambda u: _Resp(200, listing), ["--proxy-budget-mb", "0.001"], 10 ** 9)
    meta, attempt = _sidecars(os.path.join(tmp, "o"))
    eq("proxy budget (http): no proxy -> proxy_bytes null, not exceeded",
       (meta["proxy_bytes"], meta["proxy_budget_bytes"], meta["proxy_budget_exceeded"]), (None, None, False))
    check("proxy budget (http): ...and no 'Proxy traffic' line", "Proxy traffic" not in text)
    eq("proxy budget (http): ...the run is the run it was", meta["stop_reason"] != "proxy_budget_exceeded", True)
    # 0 = unlimited.
    rc, calls, text, tmp = run(lambda u: _Resp(200, listing), proxied + ["--proxy-budget-mb", "0"], 10 ** 9)
    meta, _ = _sidecars(os.path.join(tmp, "o"))
    eq("proxy budget (http): 0 is unlimited",
       (meta["proxy_budget_exceeded"], meta["proxy_budget_bytes"], meta["proxy_bytes"] >= 10 ** 9), (False, None, True))


def test_proxy_budget_http_each_loop():
    """The page loop and the retry loop each stop on their own."""
    import http_scraper
    import proxy_meter
    from unittest import mock
    listing = fixture("listing_mitersaws.html")
    asked = []

    def fake_fetch(session, url, timeout, **k):
        asked.append(url)
        proxy_meter.current().add(6_000_000)
        return http_scraper.FetchResult(url, status=200, html=listing)
    try:
        with mock.patch.object(http_scraper, "fetch", fake_fetch), \
                mock.patch.object(http_scraper, "build_session", lambda *a, **k: object()):
            _quiet(http_scraper.main, ["--url", LISTING_URL, "--pages", "3", "--delay", "0", "--format", "json",
                                       "--out", os.path.join(tempfile.mkdtemp(), "o"),
                                       "--proxy", "http://127.0.0.1:9", "--proxy-budget-mb", "5"])
        eq("proxy budget (http page loop): no page after the one that spent it", len(asked), 1)

        class Dying:
            calls = 0

            def get(self, url, **k):
                Dying.calls += 1
                proxy_meter.current().add(2_000_000)
                raise ConnectionError("reset")
        proxy_meter.start(1, active=True)
        result = http_scraper.fetch(Dying(), LISTING_URL, 5, retries=3, retry_delay=0)
        eq("proxy budget (http retry loop): no retry once the budget is spent", Dying.calls, 1)
        check("proxy budget (http retry loop): ...the result is the failed attempt", result.error is not None)
    finally:
        proxy_meter.start(None, active=False)


def test_proxy_meter_counts_curl_and_requests():
    import http_scraper
    import proxy_meter
    from unittest import mock

    class Completed:
        returncode = 0
        stdout = "<html></html>\n200 120 7 300 13 https://www.homedepot.com/b/A-B/N-1"
        stderr = ""
    try:
        meter = proxy_meter.start(10, active=True)
        with mock.patch.object(http_scraper.subprocess, "run", lambda *a, **k: Completed()):
            response = http_scraper.CurlSession(PLACEHOLDER_PROXY).get(LISTING_URL, timeout=5)
            eq("curl: the status and final URL still parse", (response.status_code, response.url),
               (200, "https://www.homedepot.com/b/A-B/N-1"))
            eq("curl: size_request + size_upload + size_header + size_download are counted", meter.bytes, 440)
            http_scraper.CurlSession(None).get(LISTING_URL, timeout=5)
            eq("curl: a session with no proxy adds nothing", meter.bytes, 440)
    finally:
        proxy_meter.start(None, active=False)
    if http_scraper.requests is None:
        SKIPS.append("requests meter (requests absent)")
        return
    sys.path.insert(0, os.path.join(HERE, "tests"))
    from test_proxy_meter import PAGES, StubProxy
    stub = StubProxy()
    try:
        meter = proxy_meter.start(0, active=True)
        session = http_scraper.build_session(stub.url, "requests")
        for path in PAGES:
            session.get("http://meter.test" + path, timeout=10)
        import time as _time
        _time.sleep(0.2)
        truth = stub.site_bytes()
        check("requests: a proxied session counts what crossed the stub proxy (within 2%%: %d vs %d)"
              % (meter.bytes, truth), truth > 100_000 and abs(meter.bytes - truth) / truth < 0.02)
        if http_scraper.CURL_AVAILABLE:
            meter = proxy_meter.start(0, active=True)
            stub.per_path.clear()
            curl = http_scraper.CurlSession(stub.url)
            for path in PAGES:
                curl.get("http://meter.test" + path, timeout=10)
            _time.sleep(0.2)
            truth = stub.site_bytes()
            check("curl: a proxied session counts what crossed the stub proxy (within 5%%: %d vs %d)"
                  % (meter.bytes, truth), truth > 100_000 and abs(meter.bytes - truth) / truth < 0.05)
        before = meter.bytes
        plain = http_scraper.build_session(None, "requests")
        check("requests: a session with no proxy has no meter hook", not plain.hooks["response"])
        eq("requests: ...and adds nothing", meter.bytes, before)
    finally:
        stub.close()
        proxy_meter.start(None, active=False)


def test_proxy_budget_browser_bridge():
    import browser_bridge
    import output_writer as W
    import proxy_meter
    import proxy_pool
    from unittest import mock
    listing = fixture("listing_mitersaws.html")
    blocked = fixture("blocked_access_denied.html")

    class Driver(_PageDriver):
        cost = 0

        def navigate(self, url):
            proxy_meter.current().add(self.cost)
            return super().navigate(url)

    def run(pages, cost, **overrides):
        driver = Driver(pages)
        driver.cost = cost
        tmp = tempfile.mkdtemp()
        args = _BridgeArgs(os.path.join(tmp, "r"), **dict(dict(pages=3, cdp_endpoint=None, proxy="http://127.0.0.1:9",
                                                               proxy_budget_mb=0.001), **overrides))
        try:
            with mock.patch.object(proxy_pool, "precheck_proxy", lambda *a, **k: None):
                rc, text = _quiet(browser_bridge.run, args, driver)
        finally:
            proxy_meter.start(None, active=False)
        return rc, driver, text, _sidecars(os.path.join(tmp, "r"))
    # A different listing each page would continue; the budget stops it after page 1.
    rc, driver, text, (meta, attempt) = run([listing, listing.replace("N-5yc1vZc2d7", "N-x")], 5000)
    eq("proxy budget (browser): rows then the budget -> exit 6", rc, W.EXIT_PARTIAL)
    eq("proxy budget (browser): ...no second page is opened", driver.visits, 1)
    eq("proxy budget (browser): ...sidecar",
       (meta["stop_reason"], meta["proxy_bytes"], meta["proxy_budget_bytes"], meta["proxy_budget_exceeded"]),
       ("proxy_budget_exceeded", 5000, 1000, True))
    check("proxy budget (browser): ...summary line", "Proxy traffic: 0.0 MB of 0.001 MB budget" in text, text[-300:])
    rc, driver, text, (meta, attempt) = run([blocked], 5000)
    eq("proxy budget (browser): a refused page after the budget is 5, not 3", rc, W.EXIT_REMOTE_API_ERROR)
    eq("proxy budget (browser): ...attempt sidecar", (meta, attempt["stop_reason"], attempt["proxy_budget_exceeded"]),
       (None, "proxy_budget_exceeded", True))
    rc, driver, text, (meta, attempt) = run([listing], 10 ** 9, cdp_endpoint="ws://127.0.0.1:1", proxy=None)
    eq("proxy budget (browser): over CDP nothing is metered",
       (meta["proxy_bytes"], meta["proxy_budget_exceeded"], "Proxy traffic" in text), (None, False, False))
    rc, driver, text, (meta, attempt) = run([listing], 10 ** 9, proxy=None)
    eq("proxy budget (browser): no proxy, nothing metered", (meta["proxy_bytes"], rc != W.EXIT_PARTIAL or
                                                            meta["stop_reason"] != "proxy_budget_exceeded"),
       (None, True))


def test_proxy_budget_finish_precedence():
    """The budget stop is above blocked / failed pages: 6 with rows, 5 without."""
    import output_writer as W
    import proxy_meter
    try:
        for rows, blocked, reason, want in (([W.Product(sku="1")], True, "blocked", 6), ([], True, "blocked", 5),
                                            ([W.Product(sku="1")], False, "completed", 6),
                                            ([], False, "hub_not_a_listing", 5)):
            proxy_meter.start(0.001, active=True)
            proxy_meter.current().add(5000)
            tmp = tempfile.mkdtemp()
            rc, _ = _quiet(W.finish_run, rows, os.path.join(tmp, "o"), "json", False, blocked=blocked,
                           stop_reason=reason, pages_requested=2, pages_completed=1, start_url="u", final_url="u")
            _, attempt = _sidecars(os.path.join(tmp, "o"))
            eq("proxy budget: finish_run over %s/%d rows -> %d" % (reason, len(rows), want),
               (rc, attempt["stop_reason"], attempt["proxy_budget_exceeded"]), (want, "proxy_budget_exceeded", True))
    finally:
        proxy_meter.start(None, active=False)


def test_proxy_budget_catalog_walk():
    import catalog_walk
    import http_scraper
    import proxy_meter
    from unittest import mock
    index = "<sitemapindex><sitemap><loc>https://www.homedepot.com/sitemap/B/PLP_CORE_TAX-0.xml</loc></sitemap>" \
            "<sitemap><loc>https://www.homedepot.com/sitemap/B/PLP_CORE_TAX-1.xml</loc></sitemap></sitemapindex>"
    files = "<urlset><url><loc>%s</loc></url></urlset>" % LISTING_URL
    asked = []

    def fake_fetch(session, url, timeout, **k):
        asked.append(url)
        proxy_meter.current().add(3_000_000)
        return http_scraper.FetchResult(url, status=200, html=index if url == catalog_walk.CATEGORY_INDEX else files)
    try:
        with mock.patch.object(http_scraper, "fetch", fake_fetch), \
                mock.patch.object(http_scraper, "build_session", lambda *a, **k: object()):
            rc, text = _quiet(catalog_walk.main, ["categories", "--proxy", "http://127.0.0.1:9",
                                                  "--proxy-budget-mb", "5"])
            eq("catalog_walk: the budget stops the walk with the targets found kept -> 6", rc, 6)
            eq("catalog_walk: ...no sitemap file after it is spent", len(asked), 2)
            check("catalog_walk: ...prints the summary line", "Proxy traffic: 6.0 MB of 5 MB budget" in text, text[-300:])
            asked.clear()
            rc, text = _quiet(catalog_walk.main, ["categories", "--proxy-budget-mb", "0.001"])
            check("catalog_walk: no proxy, no budget", rc == 0 and len(asked) == 3 and "Proxy traffic" not in text,
                  (rc, asked, text[-200:]))
    finally:
        proxy_meter.start(None, active=False)


def test_proxy_budget_flag_everywhere():
    import browser_bridge
    import catalog_walk
    import http_scraper
    parsers = {
        "browser engines": lambda argv: browser_bridge.build_parser("t").parse_args(["--url", LISTING_URL] + argv),
        "http_scraper": lambda argv: http_scraper.parse_args(["--url", LISTING_URL] + argv),
        "catalog_walk": None,
    }
    for name, parse in parsers.items():
        if parse is None:
            continue
        eq("--proxy-budget-mb defaults to 10 (%s)" % name, parse([]).proxy_budget_mb, 10)
        for bad in ("-1", "lots"):
            try:
                rc, text = _quiet(parse, ["--proxy-budget-mb", bad])
            except SystemExit as exc:
                rc = exc.code
                text = ""
            eq("--proxy-budget-mb %s is a usage error (%s)" % (bad, name), rc, 2)
    check("browser engines document --proxy-budget-mb",
          "--proxy-budget-mb" in browser_bridge.build_parser("t").format_help())
    try:
        rc, text = _quiet(catalog_walk.main, ["categories", "--proxy-budget-mb", "-1"])
    except SystemExit as exc:
        rc = exc.code
    eq("catalog_walk: --proxy-budget-mb -1 is a usage error", rc, 2)
    for name in ("browser_bridge", "http_scraper", "catalog_walk"):
        source = open(os.path.join(HERE, name + ".py"), encoding="utf-8").read()
        check("%s starts the run's meter" % name, "proxy_meter.start(" in source)


def test_proxy_meter_wiring_per_engine():
    """Each engine attaches the meter only to a LOCAL browser behind a proxy."""
    import proxy_meter
    from unittest import mock
    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
        SKIPS.append("meter wiring (playwright absent)")
    if playwright_scraper is not None:
        class Ctx:
            def __init__(self):
                self.events, self.routes = [], []

            def on(self, name, fn):
                self.events.append(name)

            def route(self, pattern, fn):
                self.routes.append(fn)

            def new_page(self):
                return mock.MagicMock()

        class PW:
            def __init__(self):
                self.ctx = Ctx()
                self.chromium = self

            def start(self):
                return self

            def launch(self, **kw):
                return self

            def new_context(self, **kw):
                return self.ctx

        for argv, want_events, want_routes in (([], True, 2), (["--proxy-budget-mb", "0"], True, 1),
                                               (["--load-media"], True, 1), (None, False, 0)):
            pw = PW()
            import browser_bridge
            args = browser_bridge.build_parser("t").parse_args(["--url", LISTING_URL] + (argv or []))
            args.proxy = None if argv is None else "http://127.0.0.1:9"
            args.proxy_file = None
            proxy_meter.start(args.proxy_budget_mb, active=argv is not None)
            with mock.patch.object(playwright_scraper, "sync_playwright", lambda: pw):
                playwright_scraper.PlaywrightDriver(args).start()
            eq("meter wiring (playwright %r): events, routes" % argv,
               (sorted(set(pw.ctx.events)) == ["requestfailed", "requestfinished"], len(pw.ctx.routes)),
               (want_events, want_routes))
            if want_routes == 2:
                check("meter wiring (playwright): the budget route is registered last (asked first)",
                      pw.ctx.routes[-1] == proxy_meter.current().playwright_budget_route)
    try:
        import selenium_scraper
    except ImportError:
        selenium_scraper = None
        SKIPS.append("meter wiring (selenium absent)")
    if selenium_scraper is not None:
        import browser_bridge
        for proxied in (True, False):
            class Driver:
                def __init__(self, *a, options=None, **k):
                    self.options = options

                def set_page_load_timeout(self, t):
                    pass

                def execute_cdp_cmd(self, *a):
                    pass

                def get_log(self, kind):
                    return []
            args = browser_bridge.build_parser("t").parse_args(["--url", LISTING_URL])
            args.proxy, args.proxy_file = ("http://127.0.0.1:9" if proxied else None), None
            args.cdp_endpoint = None
            proxy_meter.start(10, active=proxied)
            with mock.patch.object(selenium_scraper.webdriver, "Chrome", Driver):
                d = selenium_scraper.SeleniumDriver(args)
                d.start()
            caps = d._driver.options.to_capabilities().get("goog:loggingPrefs", {})
            eq("meter wiring (selenium, proxy=%s): performance log + tee" % proxied,
               (caps.get("performance") == "ALL", hasattr(d._driver, "_proxy_meter_poll")), (proxied, proxied))
    for module in ("puppeteer_scraper",):
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("meter wiring: %s attaches the CDP counter behind a local proxy only" % module,
              "attach_pyppeteer(self._page)" in source and "if pool:" in source.split("attach_pyppeteer")[0][-200:])
    proxy_meter.start(None, active=False)


def test_canary_reports_proxy_traffic():
    import subprocess
    if not os.path.isdir(os.path.join(HERE, ".github")):
        SKIPS.append("canary (no .github in this tree)")
        return
    text = open(os.path.join(HERE, ".github", "workflows", "canary.yml"), encoding="utf-8").read()
    joined = text.replace("\\\n", " ")
    runs = [line for line in joined.splitlines() if "_scraper.py" in line and "--out" in line]
    check("canary: every run passes --proxy-budget-mb 10",
          runs and all("--proxy-budget-mb 10" in line for line in runs), runs)
    head, _, rest = text.partition("- name: Proxy traffic")
    step = rest.split("- name:", 1)[0]
    check("canary: a 'Proxy traffic' step follows the live run",
          bool(rest) and "- name: Live run" in head and "if: always()" in step
          and "python3 .github/proxy_traffic_summary.py canary live 10" in step, step)
    script = os.path.join(HERE, ".github", "proxy_traffic_summary.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        env = dict(os.environ, GITHUB_STEP_SUMMARY=summary)

        def summarise(meta=None, attempt=None):
            for suffix, data in ((".meta.json", meta), (".latest_attempt.meta.json", attempt)):
                path = os.path.join(tmp, "canary" + suffix)
                if os.path.exists(path):
                    os.unlink(path)
                if data is not None:
                    open(path, "w").write(json.dumps(data))
            proc = subprocess.run([sys.executable, script, "canary", "live", "10"], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=60)
            last = open(summary).read().splitlines()[-1] if os.path.exists(summary) else ""
            return proc, last
        proc, last = summarise({"proxy_bytes": 4_321_000, "proxy_budget_exceeded": False, "finished_at": "2"},
                               {"proxy_bytes": 4_321_000, "proxy_budget_exceeded": False, "finished_at": "2"})
        eq("canary summary: the line", (proc.returncode, last), (0, "**live**: proxy traffic 4.3 MB of 10 MB"))
        proc, last = summarise(None, {"proxy_bytes": 10_200_000, "proxy_budget_exceeded": True, "finished_at": "3"})
        check("canary summary: budget exceeded -> red with the ::error:: line",
              proc.returncode == 1 and "::error::stopped: proxy budget 10 MB exceeded" in proc.stdout
              and last == "**live**: proxy traffic 10.2 MB of 10 MB", proc.stdout)
        # This repo writes ISO-8601 finished_at: the newer sidecar wins, and a
        # sidecar without one is ordered, not a TypeError.
        proc, last = summarise({"proxy_bytes": 1, "proxy_budget_exceeded": False,
                                "finished_at": "2026-10-21T08:00:00+00:00"},
                               {"proxy_bytes": 10_200_000, "proxy_budget_exceeded": True,
                                "finished_at": "2026-10-21T09:00:00+00:00"})
        check("canary summary: ISO finished_at -> the newer sidecar is the run's", proc.returncode == 1, proc.stderr)
        proc, last = summarise({"proxy_bytes": 1, "proxy_budget_exceeded": False},
                               {"proxy_bytes": 2_000_000, "proxy_budget_exceeded": False,
                                "finished_at": "2026-10-21T09:00:00Z"})
        check("canary summary: a missing finished_at beside an ISO one does not crash",
              proc.returncode == 0 and last == "**%s**: proxy traffic 2.0 MB of 10 MB" % 'live', (proc.stderr, last))
        proc, last = summarise()
        check("canary summary: no sidecar is reported, not a crash", proc.returncode == 0 and "not recorded" in last)
        proc, last = summarise({"proxy_bytes": None, "proxy_budget_exceeded": False})
        check("canary summary: over CDP (no local proxy) it says so", proc.returncode == 0 and "no local proxy" in last)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append("%s raised %s: %s" % (test.__name__,
                                                  type(exc).__name__, exc))
            print("FAIL %s raised %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d checks, %d failed, %d skipped"
          % (len(PASSES) + len(FAILURES), len(FAILURES), len(SKIPS)))
    if SKIPS:
        print("skipped: %s" % ", ".join(SKIPS))
    if FAILURES:
        print("\nFAILURES:")
        for failure in FAILURES:
            print("  " + failure)
        return 1
    print("smoke_test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
