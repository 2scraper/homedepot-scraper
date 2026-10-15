#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`.

    python3 diff_runs.py --old homedepot_products.2026-09-01.json \\
                          --new homedepot_products.2026-09-08.json

Typical use is a scheduled re-run of one category, kept under a dated
filename and diffed against the previous one to track price moves,
restocks and delistings.

Five buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or
                   just off this particular listing)
  changed        — sku in both, with a different tracked field
  source_changed — sku in both, whose price differs AND whose
                   `price_source` differs. Reported separately and ignored
                   by --fail-on-change, because it says something about our
                   own two readings, not about the site. See below.
  unmatched      — a row this project's parser could not recover a sku for,
                   counted rather than folded into added/removed

Why `source_changed` exists: a listing row reads its price from the
listing's Apollo state and a product row from the product page's JSON-LD
(see output_writer.Product.price_source). A category run and a product run of
the same sku therefore read the same price through different instruments, and
reporting that difference as a price move would be reporting our own
instrument rather than the shelf.

Prices and stock belong to a STORE. Two runs made from two different stores
are two different price lists, so this tool refuses to compare them (unless
--force), using each run's `scope.store_id` and, failing that, the store the
rows themselves name.

There is no `--price-tolerance-pct`: every row is in USD and a price on this
site moves in whole cents, not continuously.
"""

import argparse
import json
import re
import sys
from typing import Dict, List, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

TRACKED_FIELDS = (
    # Family prefix — the two every repo in this family diffs.
    "price", "currency",
    # What actually moves on this catalogue between two runs of one store: a
    # markdown starting or ending (`original_price` goes from None to a figure
    # and back), stock at the store and in its local-delivery pool, a rename,
    # and the product's rating.
    "original_price", "discount_pct", "availability", "title",
    "store_quantity", "local_delivery_quantity", "rating", "review_count",
    # NOT tracked, and each for its own reason:
    #
    #   `image_url`        — a CDN address, which moves without the image
    #                        moving.
    #   `scraped_at`       — the clock, not the catalogue.
    #   `page`/`row_index` — a row's position in a grid that reorders between
    #                        runs; tracking it reports movement, not change.
    #   `badges`           — merchandising labels, not a property of the item.
    #   `store_id` and the other store columns — a different store is a
    #                        different run, refused before rows are compared.
)


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(rows: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for r in rows:
        sku = r.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = r
    return indexed, unmatchable


def diff_rows(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed = []
    source_changed = []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue
        entry = {"sku": sku, "title": after.get("title"),
                 "changes": field_changes}
        # A price difference that arrives WITH a price_source difference is
        # our two instruments disagreeing, not the shelf price moving. It
        # goes in its own bucket and --fail-on-change ignores it.
        if "price" in field_changes and \
                before.get("price_source") != after.get("price_source"):
            entry["price_source"] = {"old": before.get("price_source"),
                                     "new": after.get("price_source")}
            source_changed.append(entry)
        else:
            changed.append(entry)

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result.get('source_changed', []))} read differently.")
    for r in result["added"]:
        print(f"  + {r.get('sku')}  {r.get('title')}  {r.get('price')} {r.get('currency')}")
    for r in result["removed"]:
        print(f"  - {r.get('sku')}  {r.get('title')}  {r.get('price')} {r.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("source_changed", []):
        src = c.get("price_source", {})
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[read differently: {src.get('old')!r} -> {src.get('new')!r}, "
              f"not counted as a price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str):
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _run_store(path: str):
    """The store a run priced: its sidecar's scope, else the one store all of
    its rows name. None when unknown."""
    _, meta = _run_status(path)
    meta = meta or {}
    store = (meta.get("scope") or {}).get("store_id") or \
        (meta.get("transport_facts") or {}).get("store_id")
    if store:
        return str(store)
    try:
        rows = _load(path)
    except (OSError, ValueError):
        return None
    found = {str(r.get("store_id")) for r in rows
             if isinstance(r, dict) and r.get("store_id")}
    return found.pop() if len(found) == 1 else None


def _check_comparable(args) -> bool:
    """Refuse a diff between runs that are not both complete, that are
    different modes, or that priced different stores. A partial run's
    unfetched pages would otherwise read as delisted products.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which this tool does "
                f"not know how to diff.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    stores = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        store = _run_store(path)
        if store:
            stores[label] = store
    if len(set(stores.values())) > 1:
        problems.append(
            f"the two runs priced different stores ({stores}). Home Depot "
            f"prices and stocks per store, so every difference would be the "
            f"store, not the catalogue.")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A category run "
            f"and a product run cover different sets of products, so "
            f"added/removed would describe the mode change rather than the "
            f"data.")
    if not problems:
        return True

    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare "
          "anyway (added/removed will include rows that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two homedepot-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was "
                        "partial or failed, the modes differ, or the two "
                        "runs priced different stores.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_rows(old, new)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
