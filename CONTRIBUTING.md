# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and
takes about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but two things are
**not** masked: raw HTML dumps and your shell history. Before pasting any
output into an issue or a PR, replace keys, proxy passwords and full
`ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed.
That check is a backstop, not a review — a leaked key has to be rotated
whether or not the check caught it.

## Reporting a site change

Home Depot changing how it ships its catalogue is the normal way this stops
working, and it has its own issue template. Worth knowing before you file:

**A listing page carries no product tiles in its markup.** The grid is
rendered client-side; what the served HTML does carry is the page's own
`window.__APOLLO_STATE__`, and that is what `product_parser.py` reads. A
product page is read from its `Product` JSON-LD block instead. So "the markup
changed" is rarely the diagnosis here; "the Apollo state changed" or "the
JSON-LD changed" is.

Three things can move, in decreasing order of durability:

1. **The page shape** — whether a listing still embeds `__APOLLO_STATE__`
   and a product page still carries `Product` JSON-LD.
2. **The model inside the state** — the `SearchModel` node whose
   `contentType` is `plppage`, and the
   `base-searchNav-<itemId>` product nodes it references
   (`search_model()`, `listing_products()`).
3. **The fields on a product node** — price, availability per fulfilment
   service, the store context (`product_from_node()`, `store_context()`).

Saying which of the three broke, and pasting the fragment rather than
describing it, turns a bug report into a fix.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a
single file of plain functions, with real captures in `fixtures/` — no
pytest, no conftest. Copy the nearest existing check and edit it.

Several properties in this repo exist because they were once absent, cost
real time to find, and are now pinned by a test so a PR that breaks one fails
rather than silently regressing:

- **A hub is not an empty listing.** A top-level category page such as
  `/b/Tools/N-5yc1vZc1xy` carries an Apollo state and no listing model; `detect_page_state` calls it `hub`,
  which is a correct answer to a URL that was never a grid, not a failure to
  retry and not a block.
- **The price and the stock belong to a store.** Every row carries
  `store_id`, and `diff_runs.py` refuses to compare runs from two different
  stores.
- **Pickup stock and local-delivery stock are separate pools.** They are two
  columns, never summed or folded together with `max()`.
- **Pagination is the site's own `?Nao=<offset>`**, built by
  `product_parser.page_url()`. The run stops on DATA — a page that adds no
  new `sku` — never on `totalProducts`, which is live inventory and moves
  between pages.
- **The edge refusal and the interstitial are different walls.** A 403 is a
  decision about the address and wants a different exit; the interstitial
  is a JS sensor a real browser clears by waiting. `STATE_POLICY` keeps
  them apart. Do not fold them together.

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` the content was never obtained, `6`
  partial. A pipeline branches on these.
- **An EMPTY result is never retried and never counted as blocked.** A grid
  that genuinely lists nothing is a correct answer to the question that was
  asked, not a failure. `page_flow.STATE_POLICY` holds that for every engine
  so they cannot disagree about it.
- **The edge refusal and the interstitial are different walls.** One is
  fixed by a different exit and the other by waiting; `is_edge_refusal()`
  and `is_self_clearing_challenge()` are deliberately separate and the 403
  matches only the first. Do not fold them together.
- **A `sku` already written earlier in the same run is dropped, not
  duplicated.** See `dedupe_by_key` in `output_writer.py`. A `sku` here is
  Home Depot's `itemId`.

There is also a naming check: certain phrases are banned repo-wide and the
suite fails naming them. If it trips, read the message — the phrase is wrong
for a reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it
  that way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has
  needed an explicit timeout its own API does not provide, and each has
  needed its own route out of the runtime — reporting a timeout is not the
  same as exiting on one. If you add a call to a remote browser or API,
  bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is a
  common bug class. A selector that matches the *wrong* element is worse
  than one that matches nothing, because the second one tells you.
- **Never present a guess as a fact.** A price that could not be read is
  `None`, not `0.0`, and a row carries a currency only when it has a price.

### If your change needs a live run

Most do not — the suite covers the parser, the writers and the CLI contract
against fixtures cut from real pages.

If yours genuinely needs homedepot.com, say in the PR what you ran (the URL,
`--pages`, which engine), whether you used a proxy, and what you got —
including the exit code and `.meta.json`'s `stop_reason`.

Be aware that **you cannot develop this from just any address**: the README's
measurements show the site refusing non-US exits and refusing requests that
do not look like a browser. "It returned nothing" without saying what exit
and which engine you used is not a reproducible report.

Do not add anything that requires logging in, or that submits a form on the
site. This project only reads what an anonymous visitor is served.

## Scope

This repo reads Home Depot's **public catalogue** pages exactly as an
anonymous visitor is served them. Out of scope: anything behind a login,
anything that submits a form, any path robots.txt disallows, and anything
that defeats a protection rather than passing it the way an ordinary browser
does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
