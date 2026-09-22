# Troubleshooting

Organised by symptom. Every number here was measured on 2026-09-22 / 23; the
dates are given so you can tell what has moved since.

---

## Every page comes back exit 3 (blocked)

**This site refuses in three structurally different ways, and they want
different responses.** Run with `--dump-html page.html` and look at what
actually arrived:

| What the dump contains | Bytes | Status | What it is |
|---|---:|---|---|
| `Access Denied` + an `errors.edgesuite.net` link | ~367 | 403 | Akamai refusing the **address** |
| An orange header and *"Oops!! Something went wrong. Please refresh page"* | ~2,410 | 403 | Home Depot refusing the **client** |
| An Akamai logo and a disabled progress button | ~2,500 | **200** | Akamai's behavioural **interstitial** |
| A real page with sub-category links and no product grid | large | 200 | a **hub** — that is exit 4, not 3. See below. |

**A captcha key does not help with any of them.** None is a captcha: there is
no puzzle, no sitekey and nothing to solve. Do not pay for a solve expecting it
to clear a 403.

**The address one.** From a non-US address every URL answers 403, `robots.txt`
included. Set `HOMEDEPOT_PROXY` to a US residential exit. A VPS, a cloud
function, a CI runner or most consumer VPNs will be refused on page 1 whatever
else you configure.

**The client one.** This is what every Python HTTP client gets. `requests` was
refused on 6 of 6 exits where system `curl` got content on 4 of the same 6;
`httpx` and `curl_cffi` on seven impersonation profiles were refused too. If
you are on `--http-client requests`, switch to `curl` (the default wherever
curl is on PATH). A **local Playwright Chromium** is also refused — 10 of 10
paired trials, headless and headful alike — so switching to a local browser
makes this worse, not better. Use `--cdp-endpoint` with the Scraping Browser
API.

**The interstitial one.** Arrives with HTTP **200**, so a status check reports
it as a successful fetch of an empty page. A real browser clears it by running
its sensor JavaScript; `curl` never will. It is served per exit — in one minute
four of six exits returned the full listing and one returned this.

**How to tell which of your exits are getting what, right now:**

```bash
./tools/edge_probe.sh 6
```

**The fix**, in order of cost: more exits (`--proxy-sessions 8 --proxy-shuffle
--proxy-block-retries 5`), then the Scraping Browser API over
`--cdp-endpoint`.

---

## Exit 4 (zero products) on a URL that plainly has products in a browser

**You are almost certainly on a hub, not a leaf.**

```
/b/Tools/N-5yc1vZc1xy                              hub  — no grid
/b/Tools-Power-Tools-Saws-Miter-Saws/N-5yc1vZc2d7  leaf — 12 products a page
```

A hub answers 200 with a real Apollo blob and no listing model. The run says
`hub` and stops rather than retrying or paying for it, because a hub is a
correct response to a URL that was never a grid.

Confusingly, a hub's blob **does** carry 24 product nodes — and the home page
carries 22. Those are recommendation carousels, not a category. This scraper
deliberately does not collect them; if it did, you would get a full page of
real product data attached to the wrong question.

Get a leaf from the site's own taxonomy:

```bash
python3 catalog_walk.py categories --grep 'Miter' --limit 20
```

Also check for a redirect: `/b/Tools-Power-Tools/N-5yc1vZc1xy` **301s** to the
hub `/b/Tools/N-5yc1vZc1xy`. Classify the page you landed on, not the one you
asked for.

---

## Exit 5, and the run says the proxy refused the credential

An HTTP 407 on the CONNECT tunnel. **homedepot.com was never reached**, so this
is not the site blocking you and no amount of new exits, longer backoff or
switching to a browser will help. Every exit on a dead credential fails
identically, which is why this is neither retried nor rotated.

Check the credential on its own:

```bash
curl -x na.proxy.2captcha.com:2334 -U "<login>:<password>" http://ip-api.com/json
```

**A 407 can be transient.** On 2026-09-22 the credential answered 407 on every
request for about an hour — base login and session-pinned variants alike — and
answered 200 the next day with nothing changed at either end. Treat it as
"retry later" before concluding it has been rotated.

---

## HTTP 500 from the Scraping Browser endpoint, or `connect_over_cdp` times out

**Both are usually `profile_locked`.** A profile allows ONE live connection,
and Playwright reports the symptom rather than the reason.

```bash
python3 - <<'EOF'
import argparse, base64, http.client, urllib.parse, env_config
a = argparse.Namespace(cdp_endpoint=None, proxy=None, twocaptcha_key=None, url=None)
env_config.apply(a)
u = urllib.parse.urlparse(a.cdp_endpoint)
tok = base64.b64encode(f"{u.username}:{u.password}".encode()).decode()
c = http.client.HTTPConnection(u.hostname, u.port, timeout=20)
c.request("GET", "/json/version", headers={"Authorization": "Basic " + tok})
r = c.getresponse(); print(r.status, r.read().decode()[:120])
EOF
```

`200` + a `Browser` field means the profile is free. `500 profile_locked` means
it is not.

**Two things about that check, both of which cost real time:**

1. **The check itself takes the lock.** Three profiles reported free, and a
   connect seconds later failed `profile_locked` on the one just checked. Use
   it as a one-off diagnostic, never as a pre-flight before connecting, and
   never in a loop.
2. **A connection that ended abnormally holds the lock for a very long time** —
   25 minutes of continuous polling never saw one release, and polling is part
   of why: every poll re-takes it. If a profile is stuck, **stop touching it**
   and wait.

Budget ONE connection attempt per profile, not a retry loop. A retry that times
out does not cost you a retry; it costs you the profile.

---

## The row count is right but a column is empty

Several columns are legitimately null, and each has a measured reason:

| Column | When it is null | Why |
|---|---|---|
| `availability` | every product-page row | Neither source publishes it on a PDP — the JSON-LD offer has no `availability` key and the Apollo node has no `fulfillment`. The listing path does publish it. |
| `store_quantity` | rows whose store offers no in-store pickup | 8 of 12 products on one page had a `bopis` service. |
| `local_delivery_quantity` | rows with no store-located delivery service | A separate pool from `store_quantity`, not a second reading of it. |
| `original_price`, `discount_pct` | any product that is not reduced | `original` equal to `value` means "not reduced", and is normalised away rather than reported as a 0% discount. |
| `currency` | a row with no price | The family never states a currency it has no price for. |
| `gtin13`, `color` | listing rows | Product-page fields only. |

**If a column is empty across an entire run that should have it**, dump the
page and check you got content rather than an interstitial:

```bash
python3 http_scraper.py --url <url> --pages 1 --dump-html page.html
```

`--dump-html` writes on success too, not only on failure — a run can return the
right count with a field silently unpopulated, and then the exact bytes are the
only way to tell a parsing bug from a page that genuinely did not carry it.

---

## A product shows a negative or absurd discount

It should not, and there is a check for it: `original_price` is never reported
at or below `price`, and `discount_pct` is computed from those two figures
rather than read from a printed badge. If you see one, it is a real bug —
please open an issue with the `--dump-html` output.

What you *may* legitimately see is `store_quantity` far below
`local_delivery_quantity` (3 vs 23 on one saw). That is not an error: they are
the shelf and the local delivery pool, and pickup ≤ delivery on every row
measured.

---

## The run stopped early and reported fewer pages than I asked for

**A listing that ends is not a partial run.** A run stops when a page adds no
new item id, which is the only honest terminating condition here — the page's
own `totalProducts` is live inventory and was measured moving 144 → 143 in four
minutes, so paginating on it would be arithmetic over a moving number.

One category returned 7 rows and stopped because page 2 came back empty. That
is the complete answer for that category, and the sidecar says `status:
complete`. A genuinely partial run reports exit 6 and names which pages failed.

---

## Selenium: `--cdp-endpoint` or `--proxy` does not work

Both are real limits, and both are reported rather than silently ignored:

* `--cdp-endpoint` is **refused**. chromedriver's `debuggerAddress` takes a
  bare `host:port` and has nowhere to put the endpoint's password. Use
  `playwright_scraper.py` or `puppeteer_scraper.py`.
* `--proxy` credentials are **stripped**, with a warning naming the bare
  address actually handed to Chrome. Chrome's `--proxy-server` cannot
  authenticate. If your exit requires auth — and on this site it does — use
  Playwright, which passes credentials through the driver's own fields.

---

## `pip check` complains after installing two engines

Expected. The three engines' pins are mutually unsatisfiable: playwright wants
`pyee>=13` and pyppeteer wants `pyee<12`; pyppeteer wants `urllib3<2` and
selenium wants `>=2.6`. Use a virtualenv per engine:

```bash
python3 -m venv .venv-playwright
.venv-playwright/bin/pip install -r requirements.txt -r requirements-playwright.txt
```

---

## The canary is red

Interpret the exit code in the log before assuming the site changed. A GitHub
runner is a datacentre address, which this edge refuses, so the canary is
guarded on the proxy secret and goes green with a `::notice::` when it is
absent. A red canary from a bare runner is usually the address, not the markup.

---

## Something else

Open an issue with the command you ran, the exit code, the contents of
`<out>.latest_attempt.meta.json`, and — if you can — the `--dump-html` output
with anything sensitive removed. The sidecar's `transport_facts` records which
transport ran, which states each page returned and how many times an exit was
rotated, which is usually enough to tell the three walls apart without a dump.
