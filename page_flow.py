"""
page_flow.py
------------
homedepot.com's page-state policy, shared by every transport.

Pagination
----------
A listing is paged by the site's own `?Nao=<offset>` parameter, which
`product_parser.page_url` builds (`PAGE_SIZE` products a page). The terminating
condition is DATA, not markup: a page that adds no new `sku` ends the run.
`totalProducts` is live inventory and moves between pages, so it is recorded
but never used to plan pages.

What a page can answer
----------------------
    content    a listing grid: parse it
    product    a product page: parse it
    hub        a category page with no grid — a correct answer to a URL that
               was never a listing. Not retried, not paid for.
    empty      a served page with nothing to parse on it. Not retried.
    challenge  Akamai's behavioural interstitial (HTTP 200, ~2.5 KB). A real
               browser clears it by itself; there is nothing to pay for.
    blocked    Akamai's refusal (HTTP 403) — a decision about the address.
    captcha    a widget with a sitekey — the only state that can be paid for.

The functions here are pure, so every transport consults one copy of the
policy.
"""

from typing import Optional


# What each state means for the run. Kept as data, not as one if-chain per
# transport, so no transport can quietly disagree with the others about
# whether a page is worth retrying or paying for.
STATE_POLICY = {
    "content": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "product": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    # Akamai's 403. Retrying the SAME exit is pointless — the decision is about
    # the address — so this state asks for a different exit, not another try.
    "blocked": {"retry": True, "solve": False, "blocked": True, "rotate": True},
    # Akamai's behavioural interstitial, HTTP 200. Also per-exit, and also not
    # payable: there is no puzzle to solve, only JS to execute. A different
    # exit is the cheap answer and a real browser is the reliable one.
    "challenge": {"retry": True, "solve": False, "blocked": True, "rotate": True},
    # A real captcha. None was observed on a listing or product page during
    # recon; the solver rung exists for the case where one appears.
    "captcha": {"retry": True, "solve": True, "blocked": True, "rotate": False},
    # NOT retried and NOT paid for: a hub is a correct answer to a URL that was
    # never a product grid, and an out-of-range page is a correct answer to a
    # page number past the end. Retrying either just costs requests.
    "hub": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "empty": {"retry": False, "solve": False, "blocked": False, "rotate": False},
}


def should_rotate_exit(state: str) -> bool:
    """Whether this state means "try a different address", not "try again".

    A timeout deserves another try at the same exit, a refusal deserves a
    different one. On this site BOTH walls are address decisions, so both
    rotate.
    """
    return STATE_POLICY.get(state, {}).get("rotate", False)


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("retry", False)


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("solve", False)


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("blocked", False)


# How many paid solves one PAGE may buy, across every call site and every
# retry or rotation on that page. Checked BEFORE a purchase, never reset by a
# retry or a new exit: a fresh address is a reason to re-fetch, not a fresh
# allowance.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """The per-page allowance for paid captcha solves.

    One object per page. `allow()` is asked BEFORE anything is bought and
    `spend()` records the purchase, so a solve that was attempted and failed
    still counts — the money was spent either way.
    """

    def __init__(self, limit: Optional[int] = None):
        self.limit = SOLVES_PER_PAGE if limit is None else limit
        self.spent = 0

    def allow(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> None:
        self.spent += 1
