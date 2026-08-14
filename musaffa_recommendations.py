"""
Pulls candidate stocks/ETFs from Musaffa's screener using the exact filter
queries you specified: Sharia-compliant, Musaffa rating A/A+ (ranking_v2 in
[8,9]), analyst consensus Buy/Strong Buy for stocks, sorted by number of
holdings (diversification) for ETFs.

The screener table is rendered client-side (Angular) — a plain HTTP GET
returns an empty shell, so this uses a headless browser (Playwright) to
load the page and read the rendered rows. The Compliance/Halal Rating
*cells* show "Unlock" for anonymous sessions (Musaffa gates the display of
those specific cells), but that doesn't matter here: the URL's filter
params already guarantee every returned row meets that criteria — we
don't need to re-read the cell text, just the ticker/name/metrics columns,
which are freely visible for every row regardless of login state.

You supplied the exact working filter URLs (musaffa's screener uses
different param names for stocks vs ETFs: sharia_compliance/COMPLIANT for
stocks, shariahCompliantStatus/COMPLIANT for ETFs). If Musaffa changes
their frontend/params, this will need re-deriving the same way — pull up
the screener in a browser, apply the filters via the UI, and copy the
resulting URL.
"""

import re

from playwright.sync_api import sync_playwright

STOCK_SCREENER_URL = (
    "https://musaffa.com/stock-screener/?country=US&page=1"
    "&filters%5Bsharia_compliance%5D=COMPLIANT"
    "&filters%5Branking_v2%5D=%5B9,8%5D"
    "&filters%5Banalyst_recommendation_weighted_avg%5D=%5BStrong%20buy,Buy%5D"
    "&sortBy=ranking_v2&sortOrder=desc"
)
ETF_SCREENER_URL = (
    "https://musaffa.com/etf-screener/?country=US&page=1"
    "&filters%5BshariahCompliantStatus%5D=COMPLIANT"
    "&filters%5Branking_v2%5D=%5B8,9%5D"
    "&sortBy=numberOfHoldings&sortOrder=desc"
)

TICKER_TOKEN_RE = re.compile(r"^[A-Z0-9.]{1,6}$")

ANALYST_RANK = {"strong buy": 0, "buy": 1, "hold": 2, "sell": 3, "strong sell": 4}


def _scrape_rows(url, timeout_ms=20000):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_selector("tbody tr", timeout=timeout_ms)
            rows = page.eval_on_selector_all(
                "tbody tr",
                "rows => rows.map(r => Array.from(r.querySelectorAll('td'))"
                ".map(td => td.textContent.trim().replace(/\\s+/g, ' ')))",
            )
        finally:
            browser.close()
    return [r for r in rows if r and r[0:1] != ["No Data"] and len(r) > 1]


def _parse_name_cell(cell):
    """Name cell format is inconsistent: stocks are '<icon-letter> <TICKER> <Name>',
    but ETFs are sometimes '<TICKER> <Name>' with no icon letter at all."""
    parts = cell.split()
    if not parts:
        return None, cell
    if (
        len(parts) >= 2
        and len(parts[0]) == 1
        and TICKER_TOKEN_RE.match(parts[0])
        and len(parts[1]) > 1
        and TICKER_TOKEN_RE.match(parts[1])
    ):
        return parts[1], " ".join(parts[2:])
    if TICKER_TOKEN_RE.match(parts[0]):
        return parts[0], " ".join(parts[1:])
    return None, cell


def get_stock_candidates(limit=7):
    """Sharia-compliant, Musaffa rating A/A+, analyst Buy/Strong Buy — sorted
    by analyst rating (Strong Buy first), tie-broken by the screener's own
    order (its rating-desc sort)."""
    rows = _scrape_rows(STOCK_SCREENER_URL)
    candidates = []
    for row in rows:
        ticker, name = _parse_name_cell(row[1])
        if not ticker:
            continue
        analyst_rating = row[4]
        candidates.append({
            "ticker": ticker,
            "name": name,
            "analyst_rating": analyst_rating,
            "sector": row[6],
            "market_cap": row[8],
            "price": row[9],
        })
    candidates.sort(key=lambda c: ANALYST_RANK.get(c["analyst_rating"].strip().lower(), 99))
    return candidates[:limit]


def get_etf_candidates(limit=3):
    """Sharia-compliant, Musaffa rating A/A+ — already sorted by number of
    holdings (a diversification proxy) via the URL's sortBy param."""
    rows = _scrape_rows(ETF_SCREENER_URL)
    candidates = []
    for row in rows:
        ticker, name = _parse_name_cell(row[1])
        if not ticker:
            continue
        candidates.append({
            "ticker": ticker,
            "name": name,
            "num_holdings": row[6],
            "segment": row[7],
        })
    return candidates[:limit]
