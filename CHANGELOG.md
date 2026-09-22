# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims
at [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as closely as a
CLI toolkit can.

A **patch** release means fixes. It does not mean every flag and every default
is frozen: where a default changes because the old one was wrong or expensive,
that is called out at the top of the release notes rather than treated as a
violation.

## [1.0.0] — 2026-09-23

First release. Every number below was measured against the live site, not
inherited.

### Transports

- **`http_scraper.py`** — plain HTTP, and the cheapest path that works here.
  It shells out to system `curl` rather than using `requests`, because that is
  what was measured to work: on the same six session-pinned exits, same
  headers, same minute, curl returned content on 4 and `python-requests` was
  refused on all 6. `httpx` and `curl_cffi` on seven impersonation profiles
  were refused too. Which part of the handshake is scored has not been
  isolated, and nothing here claims otherwise. The proxy is passed to curl on
  stdin via `--config -`, never as `--proxy`, because `ps` reads argv.
- **`playwright_scraper.py`** — the primary browser engine, and over
  `--cdp-endpoint` the transport measured to clear Akamai's interstitial every
  time. It arms the Scraping Browser's own `Captcha.setAutoSolve` on every CDP
  connection and records whether it fired.
- **`selenium_scraper.py`**, **`puppeteer_scraper.py`** — parity engines over
  one shared `browser_bridge.py`, so the challenge ladder, pagination and exit
  codes cannot drift between them.
- **`scraper_api_client.py`** — the browserless 2Captcha Scraper API rung.
- **`catalog_walk.py`** — sitemap discovery. `PLP_CORE_TAX-0.xml` alone holds
  5,304 leaf category URLs, so no crawling is needed to find targets.

### Extraction

- The listing grid comes out of the page's own `window.__APOLLO_STATE__`
  (`base-searchNav-<itemId>` nodes); a product page comes out of its JSON-LD
  `Product` block merged with its Apollo node, because neither is complete
  alone. The JSON-LD on a *listing* page is `FAQPage` + `BreadcrumbList` and
  carries no products at all.
- Products are scoped to the `products(...)` ref list of the model whose
  `metadata.contentType` is `plppage`. A category hub carries 24 `BaseProduct`
  nodes and the **home page** carries 22 — all recommendation carousels, and
  collecting them would return real product data attached to the wrong
  question.
- Seven page states (`content`, `product`, `hub`, `empty`, `challenge`,
  `blocked`, `captcha`), classified correctly **with and without** an HTTP
  status code — Selenium and pyppeteer have no response object to ask.
- `store_id` / `store_name` / `store_postal_code` on every row and in the run
  sidecar: Home Depot prices per location, and a price without its store is
  not a fact.
- `store_quantity` and `local_delivery_quantity` are kept apart. They are two
  pools — the shelf, and what a local same-day delivery can draw on — and they
  disagree (3 vs 23, 2 vs 14, 6 vs 11 across one page, pickup ≤ delivery on
  every row).
- Pagination is `?Nao=<offset>`, step 12. A run terminates on "this page added
  no new sku", never on arithmetic over the page's own `totalProducts`, which
  is live inventory and was measured moving 144 → 143 in four minutes.

### Output

- One row per `itemId`, same field order in JSON and CSV, with a run-metadata
  sidecar. `sku` is the item id from the URL, not the JSON-LD `sku` field —
  that one is the store sku and lives in `store_sku`.
- Exit codes `0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero
  products · `5` remote/transport error · `6` partial. `3` and `5` are kept
  distinct: a navigation timeout, a locked Scraping Browser profile and a
  proxy credential returning HTTP 407 are all **our** plumbing failing, not
  the site refusing us, and reporting them as `3` sends the reader to the
  anti-bot problem when the answer is elsewhere.
- A run that finds nothing writes nothing. `<out>.latest_attempt.meta.json` is
  written on **every** run whatever the outcome, beside the `<out>.meta.json`
  that describes the dataset — without it a blocked run leaves yesterday's
  `status: complete` sidecar beside yesterday's data.
- All output writes are atomic: temp file in the same directory, `fsync`,
  `os.replace`.

### What the site actually does

- **A US residential exit is necessary and not sufficient.** From a non-US
  address every URL answers 403, `robots.txt` included; from a US address a
  bare user agent still gets 403.
- **Three refusal shapes**, and they want different responses: Akamai's
  address refusal (403, 367 b), Home Depot's own branded error page (403,
  2,410 b, no vendor marker anywhere in it), and Akamai's behavioural
  interstitial — which arrives with **HTTP 200**, so a status check reports it
  as a successful fetch of an empty page.
- **A vanilla Playwright Chromium is refused outright**, 10 of 10 paired
  trials, headless and headful alike. It is never even offered the
  interstitial, while `curl` through the identical exit got 200 on 9 of 10.
  The anti-detect browser is doing load-bearing work here.
- **There is no reCAPTCHA v3 on any page this scraper touches.** Three
  sitekeys sit in the global config blob on every page; an anchor probe with a
  bogus control key shows all three are **v2 checkbox**, and no widget renders
  on a listing or product page.

### Verified

- **10 browser iterations** over the Scraping Browser: 10 of 10 completed
  without error, 176 rows, **0 of 16 invariants violated**. Four repeats of
  one category returned an identical sku set with zero drift in any stable
  field, and those rows matched `sample_output.json` — cut from the *curl*
  transport — on all 24 skus with zero field and zero price differences.
- **Transport parity**: the same category through the browser and through the
  curl-backed HTTP client produced 35 rows each, the same sku set, and zero
  differing fields, including the inventory columns.
- **The auto-solve rung arms and has nothing to do**: `Captcha.setAutoSolve`
  accepted 10/10, **zero** Captcha CDP events, and the one interstitial met
  cleared to content with no solver involvement. The interstitial is satisfied
  by a real browser running its sensor JavaScript, not by a solve.
- **Two targets that must NOT produce a full page did not**: a category hub
  returned 0 rows despite carrying carousel products, and a short category
  returned 7 rows and stopped when page 2 came back empty.
- **524 offline checks**, fixtures cut from real captures and scrubbed of
  session material with a pattern guard for the next one.
- **The Docker image builds and runs**: entrypoint works, Chromium launches
  inside it, all 13 modules importable, and no `.env`, test suite, fixtures or
  `.git` shipped.
- **Both parity engines exercised** in their own virtualenvs — `pip check`
  clean, suite green with that engine's group not skipped, documented guards
  asserted.

### Secret hygiene

- `tools/scan_secrets.py` — one scanner, four scopes (`--staged`,
  `--worktree`, `--range`, `--history`), shared by the git hooks and by CI so
  what your machine enforces and what CI enforces cannot drift apart.
- `.githooks/pre-commit` and `.githooks/pre-push`, installed by
  `tools/install_hooks.sh` through `core.hooksPath` so they are versioned — a
  hook living only in `.git/hooks` protects exactly one clone.
- The scanner refuses a file on several independent grounds: a `.env` variant
  by name, a backup/key-shaped suffix, a 32-hex key, a proxy login shape and a
  Scraping Browser login shape. It stays silent on this repo's own
  documentation templates, because a check that is always red gets switched
  off.
- `.gitignore` covers `.env.*` and `*.bak`, not just the exact name `.env`.

These exist because they were needed: a `.env` backup with three live
credentials was committed during development. The guards that existed then
both failed — `.gitignore` matched the exact name only, and the secret check
walked a list of extensions that did not include `.bak`, so it reported
"nothing credential-shaped" in the commit that carried the keys. Both were
also CI-time checks, which is too late by definition. The regression test
reconstructs that file's shape and asserts it is caught on every ground
separately.

### Known and deliberate

- `availability` is null on product pages. Neither source publishes it there;
  the listing path does. Pinned by a test so a future change is a decision
  rather than a surprise.
- `selenium_scraper.py` and `puppeteer_scraper.py` have not been run against
  the live site.
- `scraper_api_client.py` has not been run.
- The canary has not been dispatched. A GitHub runner is a datacentre address,
  which this edge refuses, so it is written to skip with a `::notice::` when
  the proxy secret is absent; that skip path has not been exercised.
- Every measurement is from one store (121, Cumberland GA). Per-store price
  variation is real and has not been sampled.

[1.0.0]: https://github.com/2scraper/homedepot-scraper/releases/tag/v1.0.0
