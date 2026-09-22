"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three scrapers.

One kind of row
---------------
Home Depot is a catalogue, so this repo carries the family's ordinary
single `Product` dataclass rather than the two classes a sibling repo
needed for people and events. The first nine columns are the family prefix —
`source`, `scraped_at`, `url`, `sku`, `title`, `image_url`, `price`,
`currency`, `category` — so a consumer already written against another repo
in this family reads them unchanged.

`sku` is the VARIANT id (`P000476`), not the master/style id, because the
variant is what is actually unique per row: a category tile, the PDP's own
JSON-LD `sku`, and the tile's `data-pid` all agree on it, while one master id
(`F20100269`) covers up to nineteen shades of the same lipstick. The master
id is kept beside it in `master_id` — it is what the product URL carries, so
a consumer needs both to get from a row back to a page.

Columns that are NOT here, and the measurements that removed them
----------------------------------------------------------------
- `lowest_price_30d`: the EU Omnibus 30-day-low disclosure CLAUDE.md §4
  warns about. Measured 2026-09-18 over 317 tiles in 20 captures spanning
  `us`, `gb`, `int/en`, `jp` and `ru`: zero occurrences of a second struck
  price of any kind. The one struck price found is a LIST price ABOVE its
  sale price (£134.00 against £105.20), which is what `original_price`
  holds. Add the column back with a measurement showing the disclosure, not
  by analogy with a sibling repo.
- `description`: the PDP's JSON-LD carries one (several hundred words of
  marketing copy). Deliberately not exported — it would dominate every CSV
  row of a price-monitoring run. `product_parser.parse_product` reads the
  JSON-LD whole, so a caller that wants it has it.
- `rating` / `review_count`: the tiles carry a Bazaarvoice placeholder
  (`pr-category-snippet`) that is EMPTY in server-rendered HTML — the widget
  fills it client-side. Measured 0 populated ratings across the same 317
  tiles. A column that is null on every row of every run should not exist.
"""

import csv
import hashlib
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# One country, one hostname. Home Depot's US storefront is www.homedepot.com
# with no locale prefix and no ccTLD family: homedepot.ca and homedepot.com.mx
# are separate businesses on separate platforms, are refused by
# product_parser.supported_host, and never produce a row here. So `source` is
# constant and there is no locale column to carry.
SOURCE_DEFAULT = "homedepot.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # `itemId` — the integer at the end of a /p/ URL, the key the listing grid
    # uses, and the only id that joins a listing row to a product row.
    # Deliberately NOT the JSON-LD `sku` field, which is the STORE sku
    # (1008223977 against itemId 321488310 on the same product, measured
    # 2026-09-22). That one is kept in `store_sku`.
    sku: Optional[str] = None
    title: Optional[str] = None
    image_url: Optional[str] = None
    price: Optional[float] = None
    # USD on every row this repo can produce, and asserted rather than guessed
    # from a `$` glyph: the PDP JSON-LD states `offers.priceCurrency: "USD"`
    # explicitly, and no other host reaches the parser. Still nullable,
    # because a listing row whose Apollo node carried no price gets no
    # currency either — the family never states a currency it has no price for.
    currency: Optional[str] = None
    category: Optional[str] = None
    # Where price and currency were read:
    #   "apollo-searchnav" — `base-searchNav-<itemId>` in __APOLLO_STATE__,
    #                        the only product source on a listing page.
    #   "jsonld"           — the PDP's `Product` block, the only source on
    #                        this site that states a currency.
    # There is no DOM price source in this repo and no tile-overlay path: a
    # listing page renders its grid client-side and the served markup holds no
    # tiles at all, so there is no second view to reconcile against and an
    # overlay would be dead code that looks load-bearing (CLAUDE.md §4).
    price_source: Optional[str] = None

    # ---- Home Depot specific, appended so the family prefix above is stable --
    original_price: Optional[float] = None
    # Computed from price/original_price, never read from a printed badge, and
    # None rather than 0 or a negative when the two are not what they were
    # taken for.
    discount_pct: Optional[float] = None
    brand: Optional[str] = None
    model_number: Optional[str] = None
    # The super-SKU family. One physical product line (a saw in two voltages,
    # a paint in forty colours) has one `parent_id` and many `sku`.
    parent_id: Optional[str] = None
    store_sku: Optional[str] = None
    is_super_sku: Optional[bool] = None
    product_type: Optional[str] = None
    gtin13: Optional[str] = None
    color: Optional[str] = None
    availability: Optional[str] = None   # InStock / OutOfStock / Discontinued
    # THE PRICE AND THE STOCK BELONG TO A STORE. Home Depot prices per
    # location; our US exit was assigned store 121 (Cumberland, GA 30339) on
    # 2026-09-22. A price without its store is not a fact, so all three travel
    # with the row and the sidecar records them for the run.
    store_id: Optional[str] = None
    store_name: Optional[str] = None
    store_postal_code: Optional[str] = None
    # Units on the shelf at `store_id`, for in-store pickup (the `bopis`
    # service). Never summed with anything: several fulfilment services quote
    # overlapping pools.
    store_quantity: Optional[int] = None
    # What a local same-day delivery out of that store can draw on (the
    # `express delivery` service). A SEPARATE pool, not a second reading of
    # `store_quantity`: measured 2026-09-22 across 12 products the two ran
    # 3 vs 23, 2 vs 14 and 6 vs 11, with pickup <= delivery on every row.
    # Folding them together with max() reported 23 units of a saw that had 3
    # on the shelf.
    local_delivery_quantity: Optional[int] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # The site's own taxonomy for this product, which is the trustworthy
    # hierarchy. `category` names the URL slug the row was collected from, and
    # that slug joins levels and words with the same `-` so it cannot be split
    # back into levels — see product_parser.category_from_url.
    category_hierarchy: Optional[List[str]] = None
    badges: Optional[List[str]] = None
    unit_of_measure: Optional[str] = None
    unit_price: Optional[float] = None
    returnable: Optional[str] = None
    page: Optional[int] = None
    row_index: Optional[int] = None


# Row class by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {
    "category": Product,
    "product": Product,
}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both qualify, and deduping is not theoretical
# here: one `gb` listing served the same tile twice (`F20100141`, 25 tiles
# over 24 distinct ids, measured 2026-09-18).
UNIQUE_BY_SKU_MODES = ("category", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list (nationalities). Joining with " | " keeps the cell
# readable in a spreadsheet and round-trippable by splitting on the same
# separator; the JSON output keeps the real list.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def _atomic_write(path: str, text: str) -> None:
    """Write `text` to `path` so a crash cannot truncate what was there.

    Temp file in the SAME directory (so `os.replace` stays on one filesystem
    and is therefore atomic), flush, fsync, then replace. The family shipped
    the naive version for months: `open(path, "w")` truncates the previous
    good file the instant it is called, so a process killed mid-serialisation
    — or a full disk — destroyed last night's dataset and left a half-written
    one that parses as valid JSON right up to where it stops.
    """
    import os
    import tempfile
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle_fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                                      suffix=os.path.basename(path))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(rows: Sequence[Any], path: str) -> None:
    payload = json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False)
    _atomic_write(path, payload + "\n")


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    """CSV with the same field order as JSON.

    An EMPTY result still carries its header, so a consumer reads a table with
    no rows instead of failing on a zero-byte file.
    """
    import io
    names = [f.name for f in fields(row_cls)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=names, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_value(v) for k, v in asdict(row).items()})
    _atomic_write(path, buffer.getvalue())


EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early.
EXIT_PARTIAL = 6

# Exit code for a failure in one of THIS PROJECT's own 2Captcha-product calls
# -- the Fingerprint API rejecting a request (bad key, bad --tags, rate
# limit), or a Scraping Browser CDP connection failing (e.g. profile_locked)
# -- as opposed to EXIT_BLOCKED (the TARGET SITE refusing a page) or an
# uncaught crash (1). Per CLAUDE.md's family exit-code contract ("5" =
# "remote API error"). Deliberately NOT what a captcha-solve failure gets:
# per that same document's captcha section, a solver error is a WARNING that
# lets the run continue (see captcha_solver.py and each engine's
# handle_captcha_if_present) -- exit 5 is for calls the user explicitly
# opted into (--fingerprint, --cdp-endpoint) where silently continuing
# without them would hide a billing/plan/profile-lock problem rather than a
# page the site declined to serve.
EXIT_REMOTE_API_ERROR = 5


class RemoteAPIError(RuntimeError):
    """A 2Captcha product call (Fingerprint API, Scraping Browser CDP
    connect) failed on its own terms, not the target site blocking a page.

    Raised by fingerprint_client.get_fingerprint and by each engine's
    --cdp-endpoint connect path; caught once at each engine's entry point
    and mapped to EXIT_REMOTE_API_ERROR, so the three engines cannot drift
    on which of 1 (crash) / 3 (blocked) / 5 (remote API error) a given
    failure gets -- before this, both paths raised a bare RuntimeError with
    no engine catching it, so either failure reached the interpreter as an
    unhandled exception and exited 1 (a raw traceback, no run-metadata
    sidecar) regardless of which one it actually was.
    """


def write_run_meta(out_prefix: str, meta: dict, *, suffix: str = ".meta.json") -> str:
    """Write a run-metadata sidecar next to the output, return its path."""
    path = f"{out_prefix}{suffix}"
    _atomic_write(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


ATTEMPT_META_SUFFIX = ".latest_attempt.meta.json"


def write_attempt_meta(out_prefix: str, meta: dict) -> str:
    """Metadata for EVERY attempt, successful or not.

    Why this exists, and why it is separate from `.meta.json`
    --------------------------------------------------------
    `.meta.json` describes the DATASET sitting beside it, so it is written
    only when a dataset is written. That is correct, and on its own it is
    also a trap: a blocked run leaves last night's `.meta.json` untouched,
    saying `status: complete, products: 1`, beside last night's `.json`. A
    pipeline that reads the files after a run cannot tell that from a fresh
    success — it sees complete metadata and plausible data and ships stale
    prices.

    So every attempt writes THIS file, unconditionally, and the two answer
    different questions:

        <out>.meta.json                  what is the data beside me?
        <out>.latest_attempt.meta.json   what happened the last time we ran?

    A consumer that cares about freshness reads the second and checks
    `data_updated` and `finished_at`.
    """
    return write_run_meta(out_prefix, meta, suffix=ATTEMPT_META_SUFFIX)


def schema_version() -> str:
    """A short fingerprint of the row schema, so a diff can refuse to compare
    a run written before a column changed with one written after."""
    names = ",".join(f.name for f in fields(Product))
    return hashlib.sha256(names.encode()).hexdigest()[:12]


def scope_fingerprint(store_id: Optional[int] = None, locale: Optional[str] = None,
                      category: Optional[str] = None,
                      grid_ids: Optional[Sequence[str]] = None,
                      max_grids: Optional[int] = None,
                      max_products: Optional[int] = None) -> dict:
    """What a run actually covered, in the fields a diff must agree on.

    Without this, `diff_runs.py` compared only status and mode — so two
    complete runs of the SAME sku in `gb`/GBP and `de`/EUR were considered
    comparable and every row looked like a price change. The currency change
    would be reported, but the tool did nothing to stop the false alert, and
    the metadata did not even carry the market.

    `grids` is a hash rather than the list: a run over 400 grids should not
    put 400 ids in a sidecar, and the only question a diff asks of it is
    whether the two runs covered the same set.
    """
    ids = sorted(str(g) for g in (grid_ids or []))
    digest = hashlib.sha256("|".join(ids).encode()).hexdigest()[:12] if ids else None
    return {
        "store_id": store_id,
        "locale": locale,
        "category": category,
        "grids": {"count": len(ids), "digest": digest},
        "max_grids": max_grids,
        "max_products": max_products,
        "schema_version": schema_version(),
    }


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "category", source: str = SOURCE_DEFAULT,
             scope: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run."""
    return {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "scope": scope if scope is not None else scope_fingerprint(),
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    On zero rows, nothing is written at all unless `allow_empty` — see the
    family invariant in CLAUDE.md §8: a run that finds nothing must not
    silently replace yesterday's good output with an empty file.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "category", source: str = SOURCE_DEFAULT,
               scope: Optional[dict] = None,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them. See a sibling repo's output_writer.py for
    the full rationale; the logic here is unchanged.
    """
    import uuid
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    run_id = uuid.uuid4().hex[:12]
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    status = "complete" if (rows and complete) else ("partial" if rows else "failed")
    base = run_meta(
        status=status, stop_reason=stop_reason,
        pages_requested=pages_requested, pages_completed=pages_completed,
        pages_failed=pages_failed, mode=mode, source=source, scope=scope,
        start_url=start_url, final_url=final_url, products=len(rows))
    base["run_id"] = run_id
    base["blocked"] = bool(blocked)
    if extra:
        base["transport_facts"] = extra

    # The dataset's own sidecar: only when a dataset was written, because a
    # "failed" sidecar beside good data would contradict it.
    if wrote_output:
        write_run_meta(out_prefix, dict(base, data_updated=True))

    # The attempt sidecar: ALWAYS. See write_attempt_meta.
    write_attempt_meta(out_prefix, dict(base, data_updated=bool(wrote_output)))

    if not rows:
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
