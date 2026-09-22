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

    rc = browser_bridge.run(Args(), Driver())
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
        stdout = "<html></html>\n200 https://www.homedepot.com/b/A-B/N-1"
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
               "diff_runs"]
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
