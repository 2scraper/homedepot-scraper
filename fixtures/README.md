# Fixtures

Cut from real captures taken 2026-09-22 through a US residential exit.

`listing_mitersaws.html`, `hub_tools.html`, `home_recommender.html` and
`product_dws780.html` are TRIMMED: they keep the page's `__APOLLO_STATE__`
blob (a few products each, not the whole grid), its JSON-LD where it had any,
and enough of the site's own asset-host references to preserve the structural
signal `detect_page_state` reads. Everything else is dropped.

An earlier cut trimmed the hub and home-page captures down to the Apollo blob
alone. That dropped them to ~1.5 KB with one asset-host reference, and they
then classified as `blocked` — correctly by the rules, because the trimming
had destroyed the very signal the fixture exists to exercise. **A fixture that
no longer behaves like its original is worse than no fixture**, so each one is
checked against the state its full capture produces before it is committed.

`blocked_access_denied.html`, `blocked_oops.html` and `challenge_akamai.html`
are the three walls, near-verbatim. They are small and carry no product data.

## Not verbatim

Per-request material has been replaced with obvious placeholders:

* Akamai challenge tokens — `?v=<uuid>&t=<id>`
* the edge's request-trace reference number and its `errors.edgesuite.net` path

They were expired and granted nothing, but a public repo is not the place for
them and the checks need the STRUCTURE of these pages, not the ids. The
markers the suite keys on (`sec-if-cpt-container`, `access denied`,
`oops!! something went wrong`) are untouched.

`smoke_test.test_fixtures_carry_no_session_material` guards this with
PATTERNS, not with the old literals, so a future capture's values are caught
too.
