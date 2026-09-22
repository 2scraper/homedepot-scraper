# homedepot-scraper

Scrapes Home Depot category listings and product pages — prices, stock, brand,
model, ratings and the site's own category hierarchy. JSON or CSV, one row
schema for both modes, and a run-metadata sidecar that says whether the result
is complete.

Four transports: **plain HTTP** (free, and it works here), **Playwright**
(primary browser engine), **Selenium**, **pyppeteer**, or a remote browser over
CDP such as the **Scraping Browser API**.

[![release](https://img.shields.io/github/v/release/2scraper/homedepot-scraper?sort=semver)](https://github.com/2scraper/homedepot-scraper/releases)
[![tests](https://github.com/2scraper/homedepot-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/homedepot-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/homedepot-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/homedepot-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-lightgrey)](LICENSE)
[![engines](https://img.shields.io/badge/engines-HTTP%20%7C%20Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![needs a US exit](https://img.shields.io/badge/needs-a%20US%20residential%20exit-orange)](#the-one-thing-you-actually-need)

---

## The one thing you actually need

**A US residential exit — and that alone is not enough.** homedepot.com refuses
on three independent axes, and all three have to be satisfied at once.
Everything below was measured on **2026-09-22**, from a residential address in
Moscow and through a US residential exit.

**Axis 1 — the address.** From a non-US address every URL answers 403,
`/robots.txt` included.

**Axis 2 — the request shape.** From a US address a bare user agent still gets
403; the browser header set in `http_scraper.py` is what turns it into 200.

| Client | Address | `/robots.txt` | A listing page |
|---|---|---|---|
| `curl`, bare user agent | Moscow | **403**, 381 b | **403**, 367 b |
| `curl`, bare user agent | US | 200 | **403**, 371 b |
| `curl`, full browser headers | Moscow | — | **403**, 367 b |
| `curl`, full browser headers | US | 200 | **200**, 172 KB |

**Axis 3 — the HTTP client itself**, which is the one nobody expects. Paired
measurement, the **same six session-pinned exits**, same headers, same minute:

| Client | content | interstitial | refused |
|---|---:|---:|---:|
| system `curl`, HTTP/2 | 4 | 2 | **0** |
| `python-requests`, HTTP/1.1 | 0 | 0 | **6** |

`requests` never once fetched a listing page. Neither did `httpx` nor
`curl_cffi` on seven browser-impersonation profiles — both speak HTTP/2 and
still got refused, so it is not simply "HTTP/1.1 is rejected". Forcing `curl`
itself to HTTP/1.1 got the interstitial instead of content, so the version
matters too. **Which part of the handshake is being scored has not been
isolated, and this README does not claim to know.** `http_scraper.py` uses the
client that was measured to work: `--http-client curl` is the default wherever
curl is on PATH.

**And a vanilla Playwright Chromium is refused outright.** 10 paired trials,
each minting one fresh exit and putting both clients through *that same exit*:

| Client | content | interstitial | refused (403) |
|---|---:|---:|---:|
| system `curl` | 3 | 6 | 1 |
| local Playwright Chromium, headless | 0 | **0** | **10** |

The automated browser was not merely unable to clear a challenge — **it was
never offered one.** `curl` got 200 on 9 of 10 trials through the identical
address. Headful made no difference. Akamai is scoring the client as automation
and refusing it before the challenge stage, which is why the **Scraping Browser
API** is the browser transport that works here and a local Chromium is not.

---

## Install

```bash
git clone https://github.com/2scraper/homedepot-scraper
cd homedepot-scraper
pip install -r requirements.txt

# then ONE engine, if you want a browser at all
pip install -r requirements-playwright.txt && playwright install chromium
# or  pip install -r requirements-selenium.txt      (needs a local Chrome)
# or  pip install -r requirements-puppeteer.txt     (downloads its own Chromium)
```

The three engines' pins are mutually unsatisfiable (`pyee` <12 vs ≥13,
`urllib3` <2.0 vs ≥2.6), so use a virtualenv per engine if you want more than
one. `http_scraper.py` needs no engine at all.

### Credentials go in `.env`, never on the command line

`ps` reads argv, and argv lands in shell history and CI logs.

```bash
cp .env.example .env      # then fill in HOMEDEPOT_PROXY at minimum
python3 env_config.py     # prints what was picked up, without printing secrets
tools/install_hooks.sh    # refuse to commit or push a credential
```

**Install the hooks.** They are the only guard that runs while a secret is
still private:

| | |
|---|---|
| `pre-commit` | refuses a commit that stages anything credential-shaped |
| `pre-push` | scans the commits about to be pushed |
| `tools/scan_secrets.py --history` | every blob that ever existed — run this before making a repo public |

A CI check cannot save you here. By the time CI runs, the objects are on a
server, and **deleting the branch afterwards does not remove them**: a ref is
a pointer, and the commit, tree and blob stay in the object store and are
served by SHA. A pushed secret is a disclosed secret — rotate it, do not try
to hide it.

---

## Usage

```bash
# A category listing, three pages, JSON and CSV. No browser, no paid rung.
python3 http_scraper.py \
  --url https://www.homedepot.com/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7 \
  --pages 3 --out miter

# Spread it over a pool of exits, and let a refused page move to another one
python3 http_scraper.py --url <same> --pages 3 \
  --proxy-sessions 8 --proxy-shuffle --proxy-block-retries 5 --out miter

# Through a real browser — the transport that clears Akamai's interstitial
python3 playwright_scraper.py --url <same> --pages 3 --out miter

# One product page: adds gtin13, colour, dimensions and the full description
python3 playwright_scraper.py --mode product \
  --url https://www.homedepot.com/p/.../321488310 --out saw

# Find targets without crawling — the site publishes its whole taxonomy
python3 catalog_walk.py categories --grep 'Saws' --limit 20
python3 catalog_walk.py categories --out-file targets.txt

# Which wall are my exits getting right now?
./tools/edge_probe.sh 6
```

### Pagination

`?Nao=<startIndex>`, 12 products a page — an **offset**, not a page number.
`--pages 3` fetches `Nao=0`, `Nao=12`, `Nao=24`.

A run stops when a page adds no new item id. It never stops on arithmetic over
the page's own `totalProducts`, because that figure is live inventory: it moved
144 → 143 between two requests four minutes apart.

---

## What you get

One row per `itemId`, same field order in JSON and CSV.

```
source scraped_at url sku title image_url price currency category price_source
original_price discount_pct brand model_number parent_id store_sku is_super_sku
product_type gtin13 color availability store_id store_name store_postal_code
store_quantity local_delivery_quantity rating review_count category_hierarchy
badges unit_of_measure unit_price returnable page row_index
```

One real row, from `sample_output.json`:

```json
{
  "source": "homedepot.com",
  "url": "https://www.homedepot.com/p/DEWALT-...-DWS716/308351915",
  "sku": "308351915",
  "title": "15 Amp Corded 12 in. Compound Double Bevel Miter Saw",
  "brand": "DEWALT",
  "model_number": "DWS716",
  "price": 349.0,
  "currency": "USD",
  "original_price": 429.0,
  "discount_pct": 18.65,
  "availability": "InStock",
  "store_id": "121",
  "store_name": "Cumberland",
  "store_quantity": 6,
  "local_delivery_quantity": 11,
  "rating": 4.6375,
  "review_count": 582,
  "category_hierarchy": ["Tools", "Power Tools", "Saws", "Miter Saws"],
  "returnable": "90-Day",
  "price_source": "apollo-searchnav"
}
```

`sample_output.json` and `sample_output.csv` are cut from a real run, not
written by hand, and CI checks their columns against the `Product` schema.

**`sku` is the item id** — the integer at the end of a `/p/` URL. Not the
JSON-LD `sku` field, which is the *store* sku (1008223977 against item id
321488310 on the same product); that one is kept in `store_sku`. The slug in a
product URL is cosmetic: a wrong slug on a right id 301-redirects to the
canonical address.

**The price belongs to a store, and so does the stock.** Home Depot prices per
location, and the Apollo field carries the store in its own name:

```
pricing({"isBrandPricingPolicyCompliant":false,"storeId":"121"})
```

Store 121 is Cumberland, GA 30339 — the one our US exits were assigned. Every
row carries `store_id`, `store_name` and `store_postal_code`, and so does the
run sidecar. A price without its store is not a fact.

**`price_source`** says which of the two sources answered: `apollo-searchnav`
(the `base-searchNav-<itemId>` node in `__APOLLO_STATE__`, the only product
source on a listing page) or `jsonld+apollo` (a product page, where JSON-LD is
authoritative on price and is the only place this site states a currency, and
the Apollo node fills what it does not carry).

There is no DOM price path and no tile overlay. A listing page renders its grid
client-side and the served markup holds no tiles at all, so there is no second
view to reconcile against and an overlay would be dead code that looks
load-bearing.

---

## Traps that look like bugs

**A category hub returns zero products.** `/b/Tools/N-5yc1vZc1xy` is a *hub*;
`/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7` is a *leaf* with the grid on
it. A hub is not broken and not blocked — it never had a grid. It reports
`hub`, is neither retried nor paid for, and points you at the sitemap that
lists the leaves.

**The hub and the home page DO carry products, and they are not the listing.**
A hub's Apollo blob holds 24 `BaseProduct` nodes and the home page holds 22 —
all recommendation carousels. A parser that reads every `BaseProduct` returns
24 rows for a hub and 22 for the home page, every one of them real product data
attached to the wrong question. Products are scoped to the `products(...)` ref
list of the model whose `metadata.contentType` is `plppage`.

**`availability` is null on a product page.** Measured, not an oversight: the
JSON-LD offer has no `availability` key at all (its whole key set is `@type,
hasMerchantReturnPolicy, price, priceCurrency, priceValidUntil, url`) and the
PDP's Apollo node has no `fulfillment` — the stock figures are fetched after
load. The listing path *does* publish them, so the column is real and is
populated on category rows.

**`store_quantity` is null on some in-stock rows.** Those products offer no
in-store pickup at that store — 8 of 12 on one page had a `bopis` service.

**`store_quantity` and `local_delivery_quantity` disagree, on purpose.** They
are two pools, not two readings of one number: what is on the shelf for pickup,
and what a local same-day delivery out of that store can draw on. Across 12
products they ran 3 vs 23, 2 vs 14 and 6 vs 11, with pickup ≤ delivery on every
row.

**`totalProducts` in the sidecar disagrees with the row count.** It is live
inventory and it moves; the rows are what was actually collected.

**A short category stops early and that is correct.** One category returned 7
rows and stopped because page 2 came back empty. Terminating on data rather
than on a page count is what makes that the right answer instead of a partial
one.

---

## About reCAPTCHA

This site was expected to carry reCAPTCHA v3. **It does not, on any page this
scraper touches.**

Three sitekeys sit in the global config blob on every page — `siteBKey`,
`siteCKey` and `RECAPTCHA_KEY_PR` — alongside a `"recaptchaEnterprise":"true"`
feature flag. An anchor probe against `google.com/recaptcha/api2/anchor`, run
with a deliberately bogus key as a control:

| Key | `size=normal` | `size=invisible` |
|---|---|---|
| all three real keys | **39.4–39.8 KB anchor, no error** | 1,492 b |
| bogus control | 1,495 b, `Invalid site key` | 1,495 b |

A full anchor under `size=normal` means these are **reCAPTCHA v2 checkbox**
keys — not v3, not v2-invisible. They belong to sign-in, checkout and
pro-referral flows, and **no widget is rendered on a listing or a product
page**.

Measured over 10 browser iterations on 2026-09-23:

| | |
|---|---|
| iterations with `Captcha.setAutoSolve` accepted | **10 / 10** |
| Captcha CDP events observed | **0** |
| iterations that met Akamai's interstitial | 1 |
| interstitials that cleared to content | **1 of 1** |

The one interstitial cleared with **zero** Captcha events. The interstitial is
satisfied by a real browser executing its sensor JavaScript, not by a solve;
the auto-solver sat armed and silent because there was no puzzle. The detector
deliberately looks for a rendered widget and never for the sitekey strings —
matching those would fire on every page the site serves and route a perfectly
good 1.35 MB listing to the paid solver.

---

## Engines

**Plain HTTP is primary here**, which is unusual in this family. `http_scraper.py`
shells out to system `curl` (see *The one thing you actually need*), needs no
browser, and rotates exits when a page is refused.

**Playwright is the primary browser engine**, and over `--cdp-endpoint` it is
the transport measured to clear Akamai's interstitial every time. Selenium and
pyppeteer exist for parity: same flags, same exit codes, same run metadata.

|  | HTTP | Playwright | pyppeteer | Selenium | `--cdp-endpoint` |
|---|---|---|---|---|---|
| Live-verified here | **yes** | **yes** | no | no | **yes** |
| Returns the full grid | yes | yes | — | — | yes |
| Needs a browser installed | no | yes | yes | yes | no |
| Authenticated remote CDP | n/a | yes | yes | **no** | — |
| Authenticated proxy | yes | yes | yes | **no** | n/a |
| Rotates exits on a refusal | yes | no | no | no | no |
| `--fingerprint` | n/a | yes | no | yes | ignored |

**Live-verified** means it has fetched and parsed a real page from this site.
pyppeteer and Selenium have been exercised in their own virtualenvs — `pip
check` clean, suite green, their documented guards asserted — but neither has
been pointed at homedepot.com.

Two limits are real and are stated here rather than left to be discovered:

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` takes a full `ws://user:pass@host:port` and authenticates
  on the WebSocket upgrade; chromedriver's `debuggerAddress` takes a bare
  `host:port` with nowhere to put a password. `--cdp-endpoint` is refused
  rather than silently ignored.
* **Selenium's `--proxy-server` cannot authenticate at all.** The credential is
  stripped and warned about; the warning names the bare address actually handed
  to Chrome.

**pyppeteer is effectively unmaintained** and its own README points at
Playwright. It is kept for parity.

**A Scraping Browser profile allows ONE live connection**, and an
abnormally-ended one holds the lock for a very long time — measured
2026-09-22, a `connect_over_cdp` that timed out kept its profile locked through
25 minutes of continuous polling. `GET /json/version` answers `500
profile_locked` plainly, **but that call itself takes the lock**, so use it as
a one-off diagnostic and never as a pre-flight. If a profile is stuck, stop
touching it and wait.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Complete |
| `1` | Crash |
| `2` | Bad usage (including a URL that is not a homedepot.com host) |
| `3` | Blocked — the site refused this scraper |
| `4` | Zero products — including a hub category, which is a correct answer |
| `5` | Remote/transport error — **our** plumbing, not the site |
| `6` | Partial — some pages fetched, then stopped early |

**`3` and `5` mean different things and the difference matters.** `5` covers a
navigation timeout, a dropped CDP socket, a `profile_locked` profile, and a
proxy credential that no longer authenticates (HTTP 407 on the CONNECT tunnel —
homedepot.com is never reached at all). Reporting any of those as `3` sends you
to the anti-bot problem when the answer is somewhere else entirely.

**A run that finds nothing writes nothing.** Last night's good output is not
replaced with `[]`; `--allow-empty` is the opt-out.

`<out>.meta.json` records `status`, `stop_reason`, `mode`, `source` and
**which** pages failed by number. Separately, `<out>.latest_attempt.meta.json`
is written on **every** run whatever the outcome — without it, a blocked run
leaves yesterday's `status: complete` sidecar beside yesterday's data and a
pipeline cannot tell that from a fresh success.

---

## Measured results

`playwright_scraper.py` over the Scraping Browser API and `http_scraper.py`
over curl, 2026-09-22 and 2026-09-23:

| Run | Transport | Rows | Coverage |
|---|---|---:|---|
| Miter Saws, 3 pages | Playwright / CDP | 35 | sku, title, price, currency, brand, image, availability, rating, store, hierarchy, model: **35/35** |
| Miter Saws, 3 pages | `http_scraper` / curl | 35 | identical — see below |
| Freezerless Refrigerators, 2 pages | Playwright / CDP | 24 | price **24/24**, 11 distinct brands |
| One product page | Playwright / CDP | 1 | price, currency, gtin13, model, colour, rating all present |

**Transport parity.** The same category through the browser and through the
curl-backed HTTP client produced 35 rows each, the **same sku set**, and **zero
differing fields** — including `store_quantity`, `local_delivery_quantity` and
`availability`.

**10 browser iterations, 2026-09-23.** 10 of 10 completed without error, 176
rows, **0 of 16 invariants violated**. Four repeats of one category returned an
IDENTICAL sku set with zero drift in any stable field, and those 24 rows
matched `sample_output.json` — which was cut from the *curl* path — on all 24
skus with zero field and zero price differences.

Discovery, over plain HTTP: `PLP_CORE_TAX-0.xml` alone holds **5,304** leaf
category URLs; the product sitemap index spans 96 files.

---

## Comparing two runs

```bash
python3 diff_runs.py --old miter.2026-09-22.json --new miter.2026-09-23.json
```

Joins on `sku` and reports added, removed and changed rows. It refuses to
compare two runs that are not both `complete`, because a partial run's
un-fetched pages otherwise read as delisted products. A price difference that
comes with a `price_source` difference is reported as `source_changed` rather
than `changed`: that says something about our own two snapshots, not about the
site.

---

## Testing

```bash
python3 smoke_test.py          # 524 offline checks, no network, no browser
python3 -m pytest -q           # the same suite, as one pytest test
python3 .github/ci_checks.py --all
```

The suite passes with no engine library installed; absent engines report a
skip, and CI fails on an *unexpected* skip. `tests.yml` also runs one venv per
engine and builds the Docker image.

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — it is organised by symptom, from
"everything comes back exit 3" to "the row count is right but a column is
empty".

---

## Do you need the paid products?

**For the HTTP path: a US residential exit, yes. Everything else, no.** There
is no captcha to solve here, and the free `http_scraper.py` returns the full
grid from an unscored exit.

What the 2Captcha products buy:

* **[Proxies](https://2captcha.com/proxy)** — `HOMEDEPOT_PROXY`. The US exit
  this site requires. Exits are scored individually and the rate moves (4 of 6
  exits served content in one measurement, 1 of 3 an hour later, 3 of 6 later
  still), so a pool beats a single address.
* **Scraping Browser API** — `HOMEDEPOT_CDP_ENDPOINT`. **The only browser
  transport measured to work here**, because a local Chromium is refused
  outright. `country-us` picks the exit; `pid-` is a persistent profile.
* **[Captcha solving](https://2captcha.com)** — `TWOCAPTCHA_KEY`. Armed on
  every CDP connection and, on this site, it has never had anything to do.
* **Fingerprints** — `fingerprint_client.py`. Not needed over CDP, where the
  remote browser brings its own and stacking a second creates a contradiction
  rather than better cover.

---

## Contributing, security, licence

* [CONTRIBUTING.md](CONTRIBUTING.md) — how to report that Home Depot changed
  its markup, and what a fix needs to include.
* [SECURITY.md](SECURITY.md) — how to report a vulnerability. Note that "Home
  Depot changed its markup" and "the scraper is blocked from a datacentre IP"
  are not vulnerabilities.
* [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — the failure modes, by symptom.
* [CHANGELOG.md](CHANGELOG.md) — what changed, with the measurement behind it.
* MIT — see [LICENSE](LICENSE).

---

## Legal

This repo reads **public pages**. It does not log in, does not touch a cart or
an order, and does not attempt to defeat the site's protections: Akamai's
interstitial is waited out exactly as a browser would, and the only thing that
would ever get solved is a captcha you pay 2Captcha to read — which, on this
site, has never appeared.

`robots.txt` is respected: the disallowed paths are listed in
`robots.snapshot.txt` and none of them is on this scraper's route — `/b/` and
`/p/` are both allowed, and `/s/` search, `/cart*`, `/checkout*` and the rest
are not touched. Discovery goes through the sitemaps the site publishes for
that purpose.

Default `--delay` is 1 second between pages. Raise it for anything long, and do
not point concurrency at one address.

Respect the site's terms and the law where you operate. Scraping at volume
from one address will get that address blocked, which is the system working as
designed.
