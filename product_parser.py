"""homedepot.com extraction — the one module that knows the site.

Everything site-specific in this repo lives here and in a dozen named
constants in the engines. If you are writing Home Depot knowledge anywhere
else, move it here (family CLAUDE.md §1).

Three page kinds, and they are not interchangeable
--------------------------------------------------
Measured 2026-09-22 through a US residential exit:

| kind   | example                                        | products |
|--------|------------------------------------------------|----------|
| hub    | /b/Tools/N-5yc1vZc1xy                          | **0**    |
| leaf   | /b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 | 12    |
| product| /p/<slug>/321488310                            | 1        |

A hub answers HTTP 200 with a Hero/DamMedia content blob and no grid at all.
It is neither a block nor an empty category, and conflating the three is how
a run reports exit 4 on a page that never had products on it. `detect_page_state`
returns "hub" for exactly this case and `page_flow.STATE_POLICY` neither
retries it nor pays for it.

Where the data is
-----------------
NOT where the family usually finds it. On a LEAF the JSON-LD blocks are
`FAQPage` and `BreadcrumbList` only — zero products — and the grid is React,
so the served markup has no tiles. Everything comes from
`window.__APOLLO_STATE__`, one `BaseProduct` node per product keyed
`base-searchNav-<itemId>`. On a PRODUCT page JSON-LD *is* authoritative: three
blocks, one a complete `Product` with `offers.priceCurrency`.

So the family's "JSON-LD primary, DOM fallback" order is INVERTED on the
listing side, and `price_source` records which of the two actually answered.

The pricing field key is parameterised AND store-scoped
-------------------------------------------------------
The Apollo field is literally

    pricing({"isBrandPricingPolicyCompliant":false,"storeId":"121"})

Home Depot prices and inventory are per-store. Store 121 (Cumberland, GA
30339) is what our exit was assigned. Never look the key up by equality —
match the `pricing(` prefix — and always record which store answered, in the
row and in the sidecar, because a price without its store is not a fact.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised by the import guard test
    BeautifulSoup = None


SITE = "homedepot.com"
BASE_URL = "https://www.homedepot.com"

# Home Depot is a single-country storefront. There is no locale prefix and no
# ccTLD family: www.homedepot.com is the US site and homedepot.ca / .com.mx are
# separate businesses on separate platforms with a different page shape. They
# are NOT supported here, and `supported_host` refuses them WITH THE REASON
# rather than with "not a Home Depot site", which would be false and would send
# the reader looking for a typo (family CLAUDE.md §5).
HOSTS = ("www.homedepot.com", "homedepot.com")
UNSUPPORTED_HOSTS = {
    "www.homedepot.ca": "homedepot.ca is Home Depot Canada, a separate "
                        "storefront on a different platform — its pages carry "
                        "no __APOLLO_STATE__ grid and this parser cannot read "
                        "them",
    "homedepot.ca": "see www.homedepot.ca",
    "www.homedepot.com.mx": "homedepot.com.mx is Home Depot Mexico, a separate "
                            "storefront on a different platform",
    "homedepot.com.mx": "see www.homedepot.com.mx",
}

# A product URL. The slug is COSMETIC: measured 2026-09-22, a fabricated
# DEWALT slug on item id 205297000 301-redirected to the BEHR paint sample
# that actually owns that id. Only the trailing integer identifies a product,
# which is why this pattern anchors on it and treats everything before it as
# free text.
SELECTORS = {
    "item_link": 'a[href*="/p/"]',
}
_SKU_IN_URL_RE = re.compile(r"/p/(?:[^/?#]*/)?(\d{6,})(?:[/?#]|$)")

# A category URL carries its taxonomy id in an `N-...` segment.
_CATEGORY_IN_URL_RE = re.compile(r"/b/([^/?#]+)/(N-[A-Za-z0-9]+)")

# Paths under /b/ that are not categories.
_NOT_A_CATEGORY = ("/b/Featured-Products",)

# Pagination. The listing pages on an OFFSET, not a page number:
# `?Nao=<startIndex>` with a step of `PAGE_SIZE`. Verified 2026-09-22 —
# `?Nao=12` returned `startIndex: 12` and a product id set disjoint from
# page 1's.
PAGE_PARAM = "Nao"
PAGE_SIZE = 12

# Akamai's stock REFUSAL — an address/client decision, HTTP 403. Measured
# 2026-09-22: 367-381 bytes, `server: AkamaiGHost`. There is no vendor name in
# the body at all, so these markers are the page's own text plus the edgesuite
# error host it links to.
BOT_CHALLENGE_MARKERS = (
    "errors.edgesuite.net",
    "access denied",
    "you don't have permission to access",
    "akamaighost",
    # Home Depot's OWN branded refusal, HTTP 403, 2,410 bytes. Measured
    # 2026-09-22 against every Python HTTP/2 client tried (httpx, curl_cffi on
    # seven impersonation profiles) while system curl got 200 from the same
    # exit in the same minute.
    #
    # This is the a sibling repo lesson in this family's CLAUDE.md §8: the shop's
    # own error page under 403, with NO vendor marker in the body at all —
    # no Akamai name, no reference number, nothing. The status is the primary
    # signal, and these markers are the secondary one for Selenium and
    # pyppeteer, which have no response object to ask.
    "oops!! something went wrong",
    "#1 home improvement retailer</div>",
)

# Akamai Bot Manager's BEHAVIOURAL INTERSTITIAL — and it is a different thing
# from the 403 above in three ways that each change what you should do about
# it. Measured 2026-09-22, exit 67.85.50.68 (Cablevision, The Bronx):
#
#   * it arrives with **HTTP 200**, not 403, so a status-code check alone
#     reports it as a successful fetch of an empty page;
#   * it is ~2.5 KB of markup whose visible content is an Akamai logo and a
#     disabled progress button, plus two sensor scripts under an obfuscated
#     path (`/YK9NuP/oPXiG/...?v=<uuid>&t=<id>`);
#   * it is served PER EXIT, not per site. In the same minute, four of six
#     proxy exits returned the full 1.35 MB listing and this one returned the
#     interstitial.
#
# It is NOT a captcha. There is no puzzle, no sitekey and nothing for a solver
# to answer: the page scores the client's JS execution and then reloads
# itself. A real browser clears it transparently, which is why the ladder
# escalates to a browser rather than to the solver API — and why spending a
# paid solve here would buy nothing.
CHALLENGE_MARKERS = (
    "sec-if-cpt-container",
    "sec-bc-tile-container",
    "behavioral-content",
    "scf-akamai-logo",
)

# Deliberately NOT in either tuple: `_abck`, `bm_sz`, `bm-verify` and
# `sensor_data`. Those are the markers the family reaches for on an Akamai
# site, and on THIS site they appear in **zero** of seven captures — good
# pages, the 403 and the interstitial alike (measured 2026-09-22). Shipping
# them would be dead code that looks load-bearing, and the next reader would
# take their absence as evidence the page was fine.

# A REAL captcha — a puzzle with a sitekey, which a solver can answer.
#
# None was observed on any catalog page during recon. Three reCAPTCHA sitekeys
# do sit in the site's global config blob on every page (`siteBKey`,
# `siteCKey`, `RECAPTCHA_KEY_PR`) and an anchor probe on 2026-09-22 showed all
# three are **v2 checkbox** keys — they served a full 39 KB anchor under
# `size=normal`, where a deliberately bogus control key answered 1,495 bytes
# and `Invalid site key`. They belong to auth, checkout and pro-referral
# flows, which a catalog scraper never reaches, and no widget is rendered on a
# listing or product page.
#
# So these markers look for a rendered WIDGET, never for the keys themselves:
# matching a sitekey string would fire on every page the site serves and would
# route a perfectly good 1.35 MB listing to the paid solver.
CAPTCHA_MARKERS = (
    "g-recaptcha",
    "grecaptcha.enterprise",
    "recaptcha/api2/anchor",
    "recaptcha/enterprise.js",
)

# The site references its own asset hosts on every page it actually serves.
# Neither the 403 nor an interstitial does. This is the STRUCTURAL secondary
# signal the family asks for where a status code is not available (CLAUDE.md
# §8): a served page is built out of the site's own assets, an interstitial is
# not. Measured 2026-09-22: 40+ matches on every real page, 0 on the 403.
OWN_ASSET_HOSTS = (
    "assets.thdstatic.com",
    "images.thdstatic.com",
    "dam.thdstatic.com",
)

# Extension-injected markers, stripped before looking for the site's own.
# The Scraping Browser API's auto-solve extension injects hunters into every
# page it loads, and one of them declares `cf-turnstile`. Our marker set above
# contains nothing that extension injects, so this guard is currently
# BELT-AND-BRACES rather than load-bearing — it is here because the marker set
# is the thing most likely to be broadened later, and the family has already
# paid for that lesson once (CLAUDE.md §8).
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script[^>]+src=[\"'](?:chrome|moz)-extension://[^\"']*[\"'][^>]*>\s*</script>",
    re.I,
)


# ---------------------------------------------------------------------------
# __APOLLO_STATE__
# ---------------------------------------------------------------------------

_APOLLO_ANCHOR = "window.__APOLLO_STATE__"


def _scan_json_object(text: str, start: int) -> Optional[str]:
    """The balanced `{...}` beginning at `start`, string-aware.

    A regex cannot do this: the blob is ~160 KB of product copy containing
    every brace and quote character you can think of, and `\\"` inside a
    product description is common. Naive `\\{.*\\}` matching cut the miter-saw
    blob off 357 bytes early during recon and raised
    `json.JSONDecodeError: Extra data`, which reads like a site change rather
    than like our own bug.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def apollo_state(html: str) -> Optional[Dict[str, Any]]:
    """`window.__APOLLO_STATE__` as a dict, or None if the page has none.

    Returns None rather than raising for a page that simply does not carry
    one (the 403 body, a redirect stub), because "no Apollo state" is a
    legitimate answer that the caller classifies. A blob that IS present and
    does NOT parse is a different thing and is worth seeing, so it is logged
    by the caller via `apollo_state_error`.
    """
    if not html:
        return None
    i = html.find(_APOLLO_ANCHOR)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    raw = _scan_json_object(html, j)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _param_field(node: Dict[str, Any], prefix: str) -> Any:
    """The value of a parameterised Apollo field, looked up BY PREFIX.

    Apollo encodes a field's arguments into its key:

        pricing({"isBrandPricingPolicyCompliant":false,"storeId":"121"})

    so the key changes with the store, with argument ORDER, and with any
    argument the site adds later. Equality lookup on the whole key is the
    version of this that silently returns None after the next deploy.
    """
    if not isinstance(node, dict):
        return None
    exact = node.get(prefix)
    if exact is not None:
        return exact
    want = prefix + "("
    for key, value in node.items():
        if key.startswith(want):
            return value
    return None


def _param_field_args(node: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """The decoded ARGUMENTS of a parameterised field — where `storeId` lives."""
    if not isinstance(node, dict):
        return {}
    want = prefix + "("
    for key in node:
        if key.startswith(want) and key.endswith(")"):
            try:
                args = json.loads(key[len(want):-1])
            except (ValueError, TypeError):
                return {}
            return args if isinstance(args, dict) else {}
    return {}


def _models(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    return [v for v in state.values()
            if isinstance(v, dict) and v.get("__typename") == "SearchModel"]


LISTING_CONTENT_TYPE = "plppage"


def search_model(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The `SearchModel` that IS this page's product grid — or None.

    Found by `__typename` rather than by key, because its key is a bare
    numeric string (`"1206728551"`) that changes per request and ROOT_QUERY
    reaches it through a `__ref`.

    **A SearchModel is not by itself a product grid**, and this is the trap on
    this site. Measured 2026-09-22:

        leaf PLP   SearchModel, metadata.contentType "plppage", 12 products
        hub        NO resolved SearchModel, but 24 loose BaseProduct nodes
        home page  SearchModel over `itemIds`, contentType None, 22 products

    The hub's 24 and the homepage's 22 are RECOMMENDATION CAROUSELS — "top
    sellers in Tools", "most popular" — not a category listing. A first
    version of this module read every `BaseProduct` node in the blob and so
    returned 24 rows for the hub `/b/Tools/N-5yc1vZc1xy` and 22 for the
    HOME PAGE, each labelled with whatever category the caller asked for.
    Every one of those rows was real product data attached to the wrong
    question — the family's "junk-link data theft" failure (CLAUDE.md §4) in
    its Apollo form.

    So a page is a listing only when a model says `contentType: "plppage"`,
    and its products are only the ones that model's own `products(...)` list
    points at.
    """
    for model in _models(state):
        meta = model.get("metadata")
        if isinstance(meta, dict) and meta.get("contentType") == LISTING_CONTENT_TYPE:
            return model
    return None


def any_search_model(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Any SearchModel, listing or recommender. Used only to classify."""
    models = _models(state)
    return models[0] if models else None


def search_report(state: Dict[str, Any]) -> Dict[str, Any]:
    """`{totalProducts, pageSize, startIndex, ...}` or `{}`.

    NOTE `totalProducts` is LIVE INVENTORY, not a stable count: it moved
    144 -> 143 between two requests four minutes apart on 2026-09-22. It is
    reported in the sidecar and is never used to decide when to stop
    (CLAUDE.md §7 layer 3 — terminate on "this page added no new sku").
    """
    model = search_model(state) or {}
    report = model.get("searchReport")
    return report if isinstance(report, dict) else {}


def store_context(state: Dict[str, Any]) -> Dict[str, Any]:
    """Which physical store priced this page.

    Read from the `pricing(...)` argument blob first — that is the store the
    prices on THIS page belong to — and fall back to the listing model's own
    `metadata.stores`, which carries the name and postcode.
    """
    out: Dict[str, Any] = {"store_id": None, "store_name": None, "store_postal_code": None}
    for node in (state or {}).values():
        if isinstance(node, dict) and node.get("__typename") == "BaseProduct":
            args = _param_field_args(node, "pricing")
            if args.get("storeId"):
                out["store_id"] = str(args["storeId"])
                break
    model = search_model(state) or {}
    meta = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    stores = meta.get("stores") if isinstance(meta.get("stores"), dict) else {}
    if stores.get("storeId") and not out["store_id"]:
        out["store_id"] = str(stores["storeId"])
    out["store_name"] = stores.get("storeName")
    address = stores.get("address") if isinstance(stores.get("address"), dict) else {}
    out["store_postal_code"] = address.get("postalCode")
    return out


def _resolve_ref(state: Dict[str, Any], ref: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(ref, dict):
        return None
    key = ref.get("__ref")
    if not isinstance(key, str):
        return None
    node = (state or {}).get(key)
    return node if isinstance(node, dict) else None


def listing_products(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The grid's products, in the order the site sorted them.

    Scoped to the listing model's own `products(...)` ref list — see
    `search_model` for why every-BaseProduct-in-the-blob is wrong.

    The order is the payload's, which is the site's own sort (TOP_SELLERS on
    an unfiltered category) and is therefore deterministic and meaningful,
    not an arrival artefact. That is what makes `row_index` mean "position in
    the grid" rather than "whatever order a dict happened to iterate in".
    """
    model = search_model(state)
    if model is None:
        return []
    refs = _param_field(model, "products")
    if not isinstance(refs, list):
        return []
    out = []
    for ref in refs:
        node = _resolve_ref(state, ref)
        if node is not None and node.get("__typename") == "BaseProduct":
            out.append(node)
    return out


def _base_products(state: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Every `BaseProduct` node in the blob, in a deterministic order.

    Used ONLY by the product-page fallback and by diagnostics. A listing must
    go through `listing_products`, which scopes to the grid.
    """
    if not isinstance(state, dict):
        return []
    found = [
        (key, value) for key, value in state.items()
        if isinstance(value, dict) and value.get("__typename") == "BaseProduct"
    ]
    found.sort(key=lambda kv: kv[0])
    return found



# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------

# The image URL carries a literal `<SIZE>` placeholder the site substitutes
# client-side: `...dewalt-miter-saws-dws780-64_<SIZE>.jpg`. Left unsubstituted
# it is a 404 for every consumer of this data, so it is resolved here against
# the node's own `sizes` list rather than against a guess.
IMAGE_SIZE_PLACEHOLDER = "<SIZE>"
PREFERRED_IMAGE_SIZE = "600"


def _resolve_image(image: Dict[str, Any]) -> Optional[str]:
    url = image.get("url") if isinstance(image, dict) else None
    if not isinstance(url, str) or not url:
        return None
    if IMAGE_SIZE_PLACEHOLDER not in url:
        return url
    sizes = [str(s) for s in (image.get("sizes") or []) if str(s).isdigit()]
    if PREFERRED_IMAGE_SIZE in sizes:
        pick = PREFERRED_IMAGE_SIZE
    elif sizes:
        # The largest available, so a consumer never gets a thumbnail when a
        # usable image existed.
        pick = max(sizes, key=int)
    else:
        # No sizes list at all. Returning the raw `<SIZE>` string would hand
        # every consumer a guaranteed 404 dressed as a URL, so report nothing.
        return None
    return url.replace(IMAGE_SIZE_PLACEHOLDER, pick)


def primary_image(media: Dict[str, Any]) -> Optional[str]:
    images = (media or {}).get("images")
    if not isinstance(images, list):
        return None
    dicts = [i for i in images if isinstance(i, dict)]
    for image in dicts:
        if image.get("subType") == "PRIMARY":
            resolved = _resolve_image(image)
            if resolved:
                return resolved
    for image in dicts:
        resolved = _resolve_image(image)
        if resolved:
            return resolved
    return None


def _number(value: Any) -> Optional[float]:
    """A price-shaped value as a float, or None.

    The Apollo payload types prices as JSON numbers, so there is no grouping
    convention to decode here and deliberately no general price-text parser:
    this repo never reads a price off rendered text, so a parser for
    `1.234,56` would be dead code that looks load-bearing (CLAUDE.md §4).
    The one string case that does occur is the JSON-LD `price`, which
    schema.org allows as a string.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def availability_from_node(node: Dict[str, Any]):
    """`(availability, store_quantity, local_delivery_quantity)`.

    Availability is read from `fulfillment(...)`, NOT from `availabilityType`.
    The two answer different questions and only the first answers ours.
    Measured 2026-09-22 on item 321488310: `availabilityType.buyable` is
    **false** and `availabilityType.status` is **false** while the store
    reports 3 units in stock. `buyable` is about the super-SKU PARENT being
    directly purchasable, not about stock — reading it as availability marks
    a stocked, orderable saw unavailable.

    The quantities need the LOCATION type, not the service type, and then
    they need keeping apart. Measured across one full listing page, three
    services appear and all three mean something different:

        pickup  / bopis             location "store"   the shelf at store 121
        pickup  / boss              location "online"  the distribution network
        delivery/ sth               location "online"  the distribution network
        delivery/ express delivery  location "store"   a local delivery pool

    Two mistakes are easy here and this function exists to avoid both.

    1. `boss` is "buy online, ship to store": filed under `pickup`, but its
       quantity is the network's, not the shelf's. Keying on
       `service.type == "bopis"` to find STORE stock therefore returned None
       for every product whose store offers `boss` instead — 11 of 12 rows on
       the first capture, silently. The location type is what separates them.

    2. `bopis` and `express delivery` are BOTH location "store" and they do
       not agree: across 12 products they ran 3 vs 23, 2 vs 14, 6 vs 11, and
       equal on four others — `bopis <= express` on every row without
       exception. They are not two readings of one number, they are two
       pools: what is on the shelf to be collected, and what a local
       same-day delivery can draw on. An earlier version took `max()` of the
       two and reported 23 units of a saw with 3 on the shelf. They get a
       column each.

    Nothing is summed across services: the same units are counted by more
    than one of them.
    """
    avail_type = node.get("availabilityType")
    if isinstance(avail_type, dict) and avail_type.get("discontinued") is True:
        return "Discontinued", None, None

    fulfillment = _param_field(node, "fulfillment")
    in_stock = False
    seen_any = False
    pickup_qty: Optional[int] = None
    delivery_qty: Optional[int] = None
    options = (fulfillment or {}).get("fulfillmentOptions") if isinstance(fulfillment, dict) else None
    for option in options or []:
        if not isinstance(option, dict):
            continue
        for service in option.get("services") or []:
            if not isinstance(service, dict):
                continue
            service_type = service.get("type")
            for location in service.get("locations") or []:
                if not isinstance(location, dict):
                    continue
                inventory = location.get("inventory")
                if not isinstance(inventory, dict):
                    continue
                seen_any = True
                if inventory.get("isInStock"):
                    in_stock = True
                quantity = inventory.get("quantity")
                if location.get("type") != "store" or not isinstance(quantity, int) \
                        or isinstance(quantity, bool):
                    continue
                if service_type == "bopis":
                    pickup_qty = quantity if pickup_qty is None else max(pickup_qty, quantity)
                else:
                    delivery_qty = quantity if delivery_qty is None else max(delivery_qty, quantity)
    if not seen_any:
        return None, None, None
    return ("InStock" if in_stock else "OutOfStock"), pickup_qty, delivery_qty


def _discount_pct(price: Optional[float], original: Optional[float]) -> Optional[float]:
    """Computed from the two figures, never read from a printed badge.

    Returns None — not 0 and not a negative — when the two are not what they
    were taken for, so a row never presents a nonsense discount as a fact.
    """
    if price is None or original is None:
        return None
    if original <= 0 or price < 0:
        return None
    if original <= price:
        return None
    return round((original - price) / original * 100.0, 2)


def product_from_node(node: Dict[str, Any], *, category: Optional[str] = None,
                      store: Optional[Dict[str, Any]] = None,
                      row_cls: Any = None) -> Optional[Any]:
    """One `BaseProduct` Apollo node -> one row, or None if it has no id.

    Returns None rather than a row with `sku=None`: a row whose key is
    missing cannot be deduped, cannot be diffed and is worse than an absent
    one.
    """
    if row_cls is None:
        from output_writer import Product as row_cls  # local import, see module docstring
    identifiers = node.get("identifiers") if isinstance(node.get("identifiers"), dict) else {}
    item_id = identifiers.get("itemId") or node.get("itemId")
    if not item_id:
        return None
    item_id = str(item_id)

    pricing = _param_field(node, "pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    price = _number(pricing.get("value"))
    original = _number(pricing.get("original"))
    # `original` equal to `value` is the site's way of saying "not reduced",
    # not a zero-percent discount. Normalise it away so `original_price` means
    # what the family says it means.
    if original is not None and price is not None and original <= price:
        original = None

    reviews = node.get("reviews") if isinstance(node.get("reviews"), dict) else {}
    ratings = reviews.get("ratingsReviews") if isinstance(reviews.get("ratingsReviews"), dict) else {}
    info = node.get("info") if isinstance(node.get("info"), dict) else {}
    hierarchy = info.get("categoryHierarchy")
    hierarchy = [str(h) for h in hierarchy] if isinstance(hierarchy, list) else []

    badges = _param_field(node, "badges")
    badge_labels = [b.get("label") for b in (badges or [])
                    if isinstance(b, dict) and b.get("label")] if isinstance(badges, list) else []

    availability, pickup_qty, delivery_qty = availability_from_node(node)
    canonical = identifiers.get("canonicalUrl") or ""
    store = store or {}

    unit = ((pricing.get("alternate") or {}).get("unit")
            if isinstance(pricing.get("alternate"), dict) else None)
    unit = unit if isinstance(unit, dict) else {}

    return row_cls(
        url=urljoin(BASE_URL, canonical) if canonical else f"{BASE_URL}/p/{item_id}",
        sku=item_id,
        title=identifiers.get("productLabel"),
        image_url=primary_image(node.get("media") or {}),
        price=price,
        # The Apollo payload states no currency anywhere. Home Depot's US
        # storefront prices in USD and the PDP JSON-LD says so explicitly
        # (`offers.priceCurrency: "USD"`), which is a fact about the site
        # rather than a guess from a `$` glyph — so it is asserted here for
        # the US host only. A row from any other host would not reach this
        # function; `supported_host` refuses it first.
        currency="USD",
        category=category or (" / ".join(hierarchy) if hierarchy else None),
        price_source="apollo-searchnav",
        original_price=original,
        discount_pct=_discount_pct(price, original),
        brand=identifiers.get("brandName"),
        model_number=identifiers.get("modelNumber"),
        parent_id=str(identifiers["parentId"]) if identifiers.get("parentId") else None,
        store_sku=identifiers.get("storeSkuNumber"),
        is_super_sku=identifiers.get("isSuperSku"),
        product_type=identifiers.get("productType"),
        availability=availability,
        store_quantity=pickup_qty,
        local_delivery_quantity=delivery_qty,
        store_id=store.get("store_id"),
        store_name=store.get("store_name"),
        store_postal_code=store.get("store_postal_code"),
        rating=_number(ratings.get("averageRating")),
        review_count=int(_number(ratings.get("totalReviews")) or 0) if ratings.get("totalReviews") else None,
        category_hierarchy=hierarchy or None,
        badges=badge_labels or None,
        unit_of_measure=pricing.get("unitOfMeasure"),
        unit_price=_number(unit.get("value")),
        returnable=info.get("returnable"),
    )


# ---------------------------------------------------------------------------
# JSON-LD — the PRODUCT page's authoritative source
# ---------------------------------------------------------------------------

def _iter_jsonld(html: str) -> Iterable[Any]:
    if not html:
        return
    for match in re.finditer(
            r"<script[^>]*?type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html, re.S | re.I):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except (ValueError, TypeError):
            continue


def _flatten_jsonld(doc: Any) -> Iterable[Dict[str, Any]]:
    """Every node in a JSON-LD document, covering the three legal shapes.

    A dict, a list of dicts, and a `@graph` wrapper are all valid schema.org
    and the family has been bitten by each (CLAUDE.md §4). A PDP capture on
    2026-09-22 held three top-level blocks, one of them a bare list.
    """
    if isinstance(doc, dict):
        graph = doc.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                yield from _flatten_jsonld(node)
        yield doc
    elif isinstance(doc, list):
        for node in doc:
            yield from _flatten_jsonld(node)


def _types(node: Dict[str, Any]) -> List[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def jsonld_product(html: str) -> Optional[Dict[str, Any]]:
    """The `Product` node from a PDP, or None."""
    for doc in _iter_jsonld(html):
        for node in _flatten_jsonld(doc):
            if isinstance(node, dict) and "Product" in _types(node):
                return node
    return None


def _first_offer(node: Dict[str, Any]) -> Dict[str, Any]:
    """`offers` as a dict, covering null / list / list-of-non-dicts.

    `"offers": null` is an EXPLICIT null, so a `.get("offers", {})` default
    does not apply — that is the exact shape that raised `AttributeError` in
    a sibling repo (CLAUDE.md §4).
    """
    offers = node.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                return offer
    return {}


def _jsonld_image(node: Dict[str, Any]) -> Optional[str]:
    """`image` in any of its four legal shapes."""
    image = node.get("image")
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        for key in ("url", "contentUrl"):
            if isinstance(image.get(key), str):
                return image[key]
        return None
    if isinstance(image, list):
        for entry in image:
            if isinstance(entry, str):
                return entry
            if isinstance(entry, dict):
                for key in ("url", "contentUrl"):
                    if isinstance(entry.get(key), str):
                        return entry[key]
    return None


def product_from_jsonld(node: Dict[str, Any], *, url: Optional[str] = None,
                        category: Optional[str] = None,
                        store: Optional[Dict[str, Any]] = None,
                        row_cls: Any = None) -> Optional[Any]:
    """A PDP `Product` JSON-LD node -> one row.

    `sku` is `productID`, NOT the node's own `sku` field. Measured on item
    321488310: `productID` is 321488310 (the id in the URL, the id the
    listing grid uses, the id `diff_runs.py` must join on) while `sku` is
    1008223977, the STORE sku. Taking the JSON-LD `sku` at face value would
    give a listing row and a product row of the same item two different keys
    and make every diff between the two modes pure noise. The store sku is
    kept in its own column.
    """
    if row_cls is None:
        from output_writer import Product as row_cls
    item_id = node.get("productID")
    if not item_id and url:
        found = _SKU_IN_URL_RE.search(url)
        item_id = found.group(1) if found else None
    if not item_id:
        return None

    offer = _first_offer(node)
    price = _number(offer.get("price"))
    brand = node.get("brand")
    brand_name = brand.get("name") if isinstance(brand, dict) else (brand if isinstance(brand, str) else None)
    rating = node.get("aggregateRating") if isinstance(node.get("aggregateRating"), dict) else {}
    store = store or {}

    availability = None
    raw_availability = offer.get("availability")
    if isinstance(raw_availability, str):
        tail = raw_availability.rsplit("/", 1)[-1]
        availability = tail or None

    return row_cls(
        url=offer.get("url") or url or "",
        sku=str(item_id),
        title=node.get("name"),
        image_url=_jsonld_image(node),
        price=price,
        # A fact from structured data — never overwritten from the DOM, and
        # never defaulted when absent.
        currency=offer.get("priceCurrency"),
        category=category,
        price_source="jsonld",
        brand=brand_name,
        model_number=node.get("model"),
        store_sku=str(node["sku"]) if node.get("sku") else None,
        gtin13=node.get("gtin13"),
        availability=availability,
        store_id=store.get("store_id"),
        store_name=store.get("store_name"),
        store_postal_code=store.get("store_postal_code"),
        rating=_number(rating.get("ratingValue")),
        review_count=int(_number(rating.get("reviewCount")) or 0) if rating.get("reviewCount") else None,
        color=node.get("color"),
    )


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

def strip_extension_scripts(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html or "")


# A REAL page references the site's own asset hosts 200+ times; the branded
# error page references them ONCE (a privacy-control script it still loads),
# and the Akamai 403 and interstitial reference them zero times. Measured
# 2026-09-22: 201 / 1 / 0 / 0. So this signal has to be a THRESHOLD, not a
# boolean — the first version asked `> 0` and the error page's single script
# tag was enough to make it claim the site had served the document.
MIN_OWN_ASSET_HITS = 5


def own_asset_hits(html: str) -> int:
    lowered = (html or "").lower()
    return sum(lowered.count(host) for host in OWN_ASSET_HOSTS)


def looks_site_served(html: str) -> bool:
    return own_asset_hits(html) >= MIN_OWN_ASSET_HITS


def detect_page_state(html: str, *, status: Optional[int] = None,
                      url: Optional[str] = None,
                      headers: Optional[Dict[str, str]] = None) -> str:
    """One of "content", "product", "hub", "empty", "challenge", "blocked", "captcha".

    Order matters and is deliberate:

    1. A **captcha** marker wins over a block marker, because a captcha is
       payable and a block is not, and mislabelling the first as the second
       throws away a page we could have had.
    2. A page with a populated **grid** is content, whatever else it says. A
       marker found on a page whose products are already there guards nothing.
    3. A **product** page is content of a different shape — a `Product`
       JSON-LD block and no listing model.
    4. **403**, or a short document naming none of the site's own asset
       hosts, is Akamai's address refusal. The status is the primary signal
       because the body carries no vendor name at all — just "Access Denied"
       and an `errors.edgesuite.net` link. The asset-host count is the
       structural secondary signal for the engines that have no response
       object to ask (CLAUDE.md §8).
    5. A **hub** is a 200 that carries an Apollo state and no listing model.
       Structurally different from a leaf that returned zero rows, and the two
       want different answers: a hub is a correct response to a URL that was
       never a grid, an empty leaf is a real result.

    `status` and `headers` are optional because Selenium and pyppeteer read
    the rendered DOM and have no response object. For them the markers and the
    asset-host count are the only signals, which is why both exist.
    """
    text = strip_extension_scripts(html or "")
    lowered = text.lower()

    for marker in CAPTCHA_MARKERS:
        if marker in lowered:
            return "captcha"

    state = apollo_state(text)
    if state is not None and listing_products(state):
        return "content"
    if jsonld_product(text) is not None:
        return "product"

    # Checked BEFORE the status code, because this one arrives as HTTP 200 and
    # a status check would wave it through as a successful empty fetch.
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return "challenge"

    blocked_marker = any(marker in lowered for marker in BOT_CHALLENGE_MARKERS)
    served_by_site = looks_site_served(text)

    if status is not None and status in (401, 403, 406, 429):
        return "blocked"
    if blocked_marker and not served_by_site:
        return "blocked"
    # The 403 body is 367 bytes, the interstitial 2.5 KB and the branded error
    # page 2.4 KB; a real page is 850 KB and up. A short document that does not
    # look site-served is not a page this site served, whatever it says.
    if not served_by_site and len(text) < 8000:
        return "blocked"

    if state is not None and search_model(state) is None:
        return "hub"
    return "empty"


def is_hub_url(url: str) -> bool:
    """A best-effort guess BEFORE fetching, used only to warn.

    Deliberately not used to skip a fetch: the taxonomy is the site's and a
    two-segment `/b/` path is not reliably a hub. `detect_page_state` decides
    for real, on the page that actually came back.
    """
    path = urlparse(url or "").path
    match = _CATEGORY_IN_URL_RE.search(path)
    if not match:
        return False
    return match.group(1).count("-") == 0


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def supported_host(url: str) -> Tuple[bool, Optional[str]]:
    """`(ok, reason_if_not)`. Refuses a wrong host WITH THE REASON."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return False, "no host in URL %r" % (url,)
    if host in HOSTS:
        return True, None
    if host in UNSUPPORTED_HOSTS:
        return False, UNSUPPORTED_HOSTS[host]
    return False, "%s is not a www.homedepot.com URL" % host


def page_url(url: str, page: int) -> str:
    """Page `page` (1-based) of the listing at `url`.

    The site pages on an OFFSET, not an index: `?Nao=<startIndex>` with a
    step of `PAGE_SIZE`. Existing query parameters are preserved and `Nao` is
    REPLACED rather than appended, so calling this twice does not produce
    `?Nao=12&Nao=24`.
    """
    if page <= 1:
        parts = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k != PAGE_PARAM]
        return urlunparse(parts._replace(query=urlencode(kept)))
    parts = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k != PAGE_PARAM]
    kept.append((PAGE_PARAM, str((page - 1) * PAGE_SIZE)))
    return urlunparse(parts._replace(query=urlencode(kept)))


def sku_from_url(url: str) -> Optional[str]:
    match = _SKU_IN_URL_RE.search(url or "")
    return match.group(1) if match else None


def category_from_url(url: str) -> Optional[str]:
    """The human-readable taxonomy trail a `/b/` URL carries.

    `/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7` -> the slug, with
    hyphens kept: the site joins BOTH the words inside one level and the
    levels themselves with `-`, so "Power-Tools" and the boundary between
    "Tools" and "Power Tools" are written identically and cannot be told
    apart from the URL. Splitting on `-` would invent a hierarchy. The real
    one is published per product in `info.categoryHierarchy` and that is what
    a row's `category_hierarchy` column carries.
    """
    path = urlparse(url or "").path
    for prefix in _NOT_A_CATEGORY:
        if path.startswith(prefix):
            return None
    match = _CATEGORY_IN_URL_RE.search(path)
    return match.group(1) if match else None


def category_id_from_url(url: str) -> Optional[str]:
    match = _CATEGORY_IN_URL_RE.search(urlparse(url or "").path)
    return match.group(2) if match else None


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------

def parse_category(html: str, *, url: Optional[str] = None,
                   category: Optional[str] = None,
                   row_cls: Any = None) -> List[Any]:
    """Every product row on a listing page, and NOTHING from a carousel.

    Returns [] for a hub or the home page, both of which carry real
    `BaseProduct` nodes that belong to recommendation strips rather than to
    any category — see `search_model`.
    """
    state = apollo_state(html or "")
    if not state:
        return []
    store = store_context(state)
    category = category or (category_from_url(url) if url else None)
    rows = []
    for index, node in enumerate(listing_products(state)):
        row = product_from_node(node, category=category, store=store, row_cls=row_cls)
        if row is not None:
            row.row_index = index
            rows.append(row)
    return rows


def parse_product(html: str, *, url: Optional[str] = None,
                  category: Optional[str] = None,
                  row_cls: Any = None) -> List[Any]:
    """The single row for a product page, MERGED from both of its sources.

    Neither source alone is complete on a PDP, and which fields each one has
    was measured rather than assumed (item 321488310, 2026-09-22):

        JSON-LD `Product`   price, priceCurrency (the ONLY currency this site
                            states anywhere), gtin13, color, dimensions,
                            aggregateRating
        Apollo `base-catalog-<itemId>`   store context, categoryHierarchy,
                            badges, availabilityType, unit pricing

    So JSON-LD is the base — it is authoritative on price and is the only
    place a currency is published — and the Apollo node fills what it does
    not carry. `price_source` records `jsonld+apollo` when both answered, so
    a diff can tell a real change from a run where one source was missing.

    **`availability` stays null on a product page, and that is correct.** The
    JSON-LD offer has no `availability` key at all (the offer's whole key set
    is `@type, hasMerchantReturnPolicy, price, priceCurrency, priceValidUntil,
    url`), and the PDP's Apollo node carries `availabilityType` but no
    `fulfillment` — the stock figures are fetched client-side after load. The
    listing path DOES publish them, so the column is real and is populated on
    category rows; inventing one here from `availabilityType.buyable` would
    repeat the mistake `availability_from_node` documents. The one thing that
    IS a fact is `discontinued`, and only that is taken.
    """
    if row_cls is None:
        from output_writer import Product as row_cls
    node = jsonld_product(html or "")
    state = apollo_state(html or "")
    wanted = sku_from_url(url or "")

    apollo_node = None
    if state:
        for _key, base in _base_products(state):
            item_id = str((base.get("identifiers") or {}).get("itemId")
                          or base.get("itemId") or "")
            if not wanted or item_id == wanted:
                apollo_node = base
                break

    if node is not None and wanted and str(node.get("productID") or "") \
            not in ("", wanted):
        # The page carries a `Product` block for a DIFFERENT item than the URL
        # asked for. That means a redirect landed somewhere else, or the blob
        # belongs to a "compare similar" panel. Answering with it would put a
        # neighbour's price under this URL.
        node = None

    if node is not None:
        row = product_from_jsonld(node, url=url, category=category,
                                  row_cls=row_cls)
        if row is None:
            return []
        if apollo_node is not None:
            _enrich_from_apollo(row, apollo_node, state or {})
            row.price_source = "jsonld+apollo"
        return [row]

    if apollo_node is None or not wanted:
        # No `Product` block and nothing that identifies WHICH product was
        # asked for. The blob on a hub or the home page is full of carousel
        # products that would each answer plausibly and wrongly.
        return []
    row = product_from_node(apollo_node, category=category,
                            store=store_context(state or {}), row_cls=row_cls)
    return [row] if row is not None else []


def _enrich_from_apollo(row: Any, node: Dict[str, Any],
                        state: Dict[str, Any]) -> None:
    """Fill the fields JSON-LD does not carry. Never overwrites a stated fact."""
    identifiers = node.get("identifiers") if isinstance(node.get("identifiers"), dict) else {}
    info = node.get("info") if isinstance(node.get("info"), dict) else {}
    store = store_context(state)

    if row.parent_id is None and identifiers.get("parentId"):
        row.parent_id = str(identifiers["parentId"])
    if row.is_super_sku is None:
        row.is_super_sku = identifiers.get("isSuperSku")
    if row.product_type is None:
        row.product_type = identifiers.get("productType")
    if row.store_sku is None and identifiers.get("storeSkuNumber"):
        row.store_sku = identifiers["storeSkuNumber"]

    # A PDP node has `info.categoryHierarchy` too, but it was NULL on the
    # capture this was measured against while `taxonomy.breadCrumbs` carried
    # the full trail. A listing node is the other way round. So read both,
    # most-specific source first, rather than assuming the page kinds agree.
    hierarchy = info.get("categoryHierarchy")
    if not isinstance(hierarchy, list) or not hierarchy:
        taxonomy = node.get("taxonomy") if isinstance(node.get("taxonomy"), dict) else {}
        crumbs = taxonomy.get("breadCrumbs")
        if isinstance(crumbs, list):
            hierarchy = [c.get("label") for c in crumbs
                         if isinstance(c, dict)
                         and c.get("dimensionName") == "Category"
                         and c.get("label")]
    if row.category_hierarchy is None and isinstance(hierarchy, list) and hierarchy:
        row.category_hierarchy = [str(h) for h in hierarchy]
    if row.category is None and row.category_hierarchy:
        row.category = " / ".join(row.category_hierarchy)
    if row.returnable is None:
        row.returnable = info.get("returnable")

    badges = _param_field(node, "badges")
    if row.badges is None and isinstance(badges, list):
        labels = [b.get("label") for b in badges
                  if isinstance(b, dict) and b.get("label")]
        row.badges = labels or None

    pricing = _param_field(node, "pricing")
    if isinstance(pricing, dict):
        if row.unit_of_measure is None:
            row.unit_of_measure = pricing.get("unitOfMeasure")
        original = _number(pricing.get("original"))
        if row.original_price is None and original is not None \
                and row.price is not None and original > row.price:
            row.original_price = original
            row.discount_pct = _discount_pct(row.price, original)

    for field_name in ("store_id", "store_name", "store_postal_code"):
        if getattr(row, field_name) is None:
            setattr(row, field_name, store.get(field_name))

    # The only availability fact a PDP publishes — see parse_product.
    avail = node.get("availabilityType")
    if row.availability is None and isinstance(avail, dict) \
            and avail.get("discontinued") is True:
        row.availability = "Discontinued"


def parse(html: str, mode: str = "category", **kwargs) -> List[Any]:
    if mode == "product":
        return parse_product(html, **kwargs)
    return parse_category(html, **kwargs)
