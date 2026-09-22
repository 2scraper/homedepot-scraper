#!/usr/bin/env python3
"""Compare the iterations `autosolve_probe.py` produced, and judge them.

Three separate questions, reported separately because they fail differently:

* **Consistency** — do repeated runs of the SAME target agree? A scraper that
  returns a different answer each time is not trustworthy even when every
  individual answer looks fine.
* **Correctness** — do the rows satisfy invariants that must hold regardless
  of what the site did? These are checks against arithmetic and against the
  site's own structure, not against a golden file, so they stay valid as the
  catalogue changes.
* **The ladder** — what did the challenge machinery actually do?

    python3 tools/compare_runs.py --in live/autosolve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# Fields that MUST be identical for the same sku across runs: they describe
# the product, not its moment.
STABLE = ("sku", "title", "brand", "model_number", "parent_id", "store_sku",
          "product_type", "category_hierarchy", "url", "currency",
          "unit_of_measure", "returnable")
# Fields that may legitimately move between runs minutes apart.
VOLATILE = ("scraped_at", "page", "row_index", "store_quantity",
            "local_delivery_quantity", "availability", "price",
            "original_price", "discount_pct", "badges", "rating",
            "review_count", "image_url")


def load(prefix):
    records = [json.loads(line) for line in
               open(prefix + ".jsonl", encoding="utf-8") if line.strip()]
    rows = json.load(open(prefix + ".rows.json", encoding="utf-8"))
    return records, rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="prefix", default="live/autosolve")
    args = parser.parse_args(argv)
    records, rows_by_iteration = load(args.prefix)

    print("=" * 72)
    print("1. WHAT EACH ITERATION DID")
    print("=" * 72)
    print("%-3s %-20s %-8s %-26s %6s %7s %s"
          % ("#", "target", "mode", "states", "rows", "ms", "note"))
    for record in records:
        states = ",".join(s["state"] for s in record["states"]) or "-"
        note = []
        if record["error"]:
            note.append("ERR " + record["error"][:40])
        if record["challenge_seen"]:
            note.append("CHALLENGE SEEN")
        if record["captcha_events"]:
            note.append("%d captcha event(s)" % len(record["captcha_events"]))
        print("%-3d %-20s %-8s %-26s %6d %7d %s"
              % (record["iteration"], record["target"], record["mode"],
                 states, record["rows"], record["ms"], "; ".join(note)))

    print()
    print("=" * 72)
    print("2. THE LADDER — did autosolve fire?")
    print("=" * 72)
    armed = sum(1 for r in records if r.get("autosolve_armed"))
    print("iterations with Captcha domain armed : %d/%d" % (armed, len(records)))
    print("iterations that SAW an interstitial  : %d"
          % sum(1 for r in records if r["challenge_seen"]))
    events = Counter(e["event"] for r in records for e in r["captcha_events"])
    print("captcha CDP events observed          : %s"
          % (dict(events) if events else "NONE"))
    notes = Counter(n for r in records for n in r.get("autosolve_notes", []))
    for note, count in notes.most_common():
        print("   %-60s x%d" % (note[:60], count))
    early = Counter(r["pre_wait_state"] for r in records if r["pre_wait_state"])
    print("page-1 state BEFORE the readiness wait: %s" % dict(early))
    cleared = [r for r in records if r["challenge_seen"]
               and r["states"] and r["states"][0]["state"] in ("content", "product")]
    print("interstitials that CLEARED to content : %d of %d"
          % (len(cleared), sum(1 for r in records if r["challenge_seen"])))

    print()
    print("=" * 72)
    print("3. CONSISTENCY — do repeats of one target agree?")
    print("=" * 72)
    by_target = defaultdict(list)
    for record in records:
        if record["rows"] and not record["error"]:
            by_target[record["target"]].append(record)
    any_repeat = False
    for target, group in sorted(by_target.items()):
        if len(group) < 2:
            continue
        any_repeat = True
        base = group[0]
        base_rows = {r["sku"]: r for r in rows_by_iteration[str(base["iteration"])]}
        print("\n%s — %d successful runs" % (target, len(group)))
        print("  run  rows  sku-set vs run %d   stable-field diffs"
              % base["iteration"])
        for record in group:
            current = {r["sku"]: r
                       for r in rows_by_iteration[str(record["iteration"])]}
            same = set(current) == set(base_rows)
            diffs = 0
            examples = []
            for sku in set(current) & set(base_rows):
                for field in STABLE:
                    if current[sku].get(field) != base_rows[sku].get(field):
                        diffs += 1
                        if len(examples) < 2:
                            examples.append("%s.%s" % (sku, field))
            print("  %-4d %-5d %-19s %d %s"
                  % (record["iteration"], record["rows"],
                     "IDENTICAL" if same
                     else "differs by %d" % len(set(current) ^ set(base_rows)),
                     diffs, ("e.g. " + ", ".join(examples)) if examples else ""))
    if not any_repeat:
        print("  (no target ran more than once successfully)")

    print()
    print("=" * 72)
    print("4. CORRECTNESS — invariants that must hold whatever the site did")
    print("=" * 72)
    checks = []
    all_rows = [r for rows in rows_by_iteration.values() for r in rows]
    total = len(all_rows)

    def add(name, bad, detail=""):
        checks.append((name, len(bad), detail
                       or (str(bad[:2]) if bad else "")))

    add("every row has a sku", [r for r in all_rows if not r.get("sku")])
    add("every row's url points at its own sku",
        [r for r in all_rows
         if r.get("sku") and r.get("url") and r["sku"] not in r["url"]])
    add("no image url still holds the <SIZE> placeholder",
        [r for r in all_rows if "<SIZE>" in (r.get("image_url") or "")])
    add("a priced row always states a currency",
        [r for r in all_rows if r.get("price") is not None
         and not r.get("currency")])
    add("currency is USD where stated",
        [r for r in all_rows if r.get("currency") not in (None, "USD")])
    add("no price is negative or zero",
        [r for r in all_rows if r.get("price") is not None and r["price"] <= 0])
    add("original_price is never at or below price",
        [r for r in all_rows if r.get("original_price") is not None
         and r.get("price") is not None
         and r["original_price"] <= r["price"]])
    add("discount_pct agrees with the two prices it was computed from",
        [r for r in all_rows
         if r.get("discount_pct") is not None
         and r.get("price") is not None and r.get("original_price")
         and abs(r["discount_pct"]
                 - round((r["original_price"] - r["price"])
                         / r["original_price"] * 100, 2)) > 0.011])
    add("shelf stock never exceeds the local delivery pool",
        [r for r in all_rows
         if r.get("store_quantity") is not None
         and r.get("local_delivery_quantity") is not None
         and r["store_quantity"] > r["local_delivery_quantity"]])
    add("a row with stock is not marked OutOfStock",
        [r for r in all_rows if r.get("availability") == "OutOfStock"
         and (r.get("store_quantity") or 0) > 0])
    add("every priced row names the store the price belongs to",
        [r for r in all_rows if r.get("price") is not None
         and not r.get("store_id")])
    add("category_hierarchy is a non-empty list where present",
        [r for r in all_rows if r.get("category_hierarchy") is not None
         and not isinstance(r["category_hierarchy"], list)])
    add("rating is within 0-5",
        [r for r in all_rows if r.get("rating") is not None
         and not 0 <= r["rating"] <= 5])
    add("review_count is a non-negative integer",
        [r for r in all_rows if r.get("review_count") is not None
         and (not isinstance(r["review_count"], int) or r["review_count"] < 0)])
    add("no duplicate sku within one run",
        [it for it, rows in rows_by_iteration.items()
         if len({r["sku"] for r in rows}) != len(rows)])
    # The hub is in the target list on purpose: a correct scraper returns
    # nothing for it, and a scraper reading carousels returns a full page.
    hub = [r for r in records if r["target"] == "tools-hub"]
    add("a category HUB yields zero rows",
        [r for r in hub if r["rows"] != 0])

    width = max(len(name) for name, _, _ in checks)
    failures = 0
    for name, bad, detail in checks:
        flag = "ok  " if bad == 0 else "FAIL"
        if bad:
            failures += 1
        print("  %s %-*s  %s" % (flag, width, name,
                                 "" if bad == 0 else "%d offender(s) %s"
                                 % (bad, detail[:60])))
    print("\n  %d rows checked across %d iterations, %d invariant(s) violated"
          % (total, len(rows_by_iteration), failures))

    print()
    print("=" * 72)
    print("5. VERDICT")
    print("=" * 72)
    ok_runs = [r for r in records if not r["error"]]
    produced = [r for r in ok_runs if r["rows"] > 0]
    print("  iterations            : %d" % len(records))
    print("  completed without error: %d" % len(ok_runs))
    print("  returned rows          : %d (the hub is expected to return 0)"
          % len(produced))
    print("  total rows             : %d" % total)
    print("  invariants violated    : %d" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
