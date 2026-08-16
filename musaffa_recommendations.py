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
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

GRADE_FETCH_WORKERS = 5

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

# The screener table's Halal Rating *column* shows "Unlock" for anonymous
# sessions, but each ticker's own detail page renders the real letter grade
# for free (confirmed via headless browser — it's JS-rendered, not in the
# raw HTML either, so a plain HTTP GET won't see it). Pattern seen there:
# "Current Shariah Compliance ... Screening Methodology: AAOIFI\n\nHALAL\nA+".
GRADE_RE = re.compile(r"Screening Methodology:\s*AAOIFI\s*\n+\s*(?:HALAL|NOT HALAL)\s*\n+\s*([A-D][+-]?|-)", re.I)


def _scrape_rows(url, timeout_ms=15000):
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


GRADE_RANK = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4, "B-": 5, "C+": 6, "C": 7, "C-": 8, "D+": 9, "D": 10, "D-": 11}


def grade_below_a(grade):
    """True if grade is a recognized letter grade below 'A' (A- or lower).
    UNKNOWN/unrecognized grades return False — a scrape failure isn't a
    downgrade signal."""
    return GRADE_RANK.get(grade, -1) > GRADE_RANK["A"]


GRADE_POLL_ATTEMPTS = 8  # ~8s max poll after page load — was 20s; trades a bit
                         # more scrape-timeout risk (-> UNKNOWN) for speed.


def _read_grade(page, url, timeout_ms=12000):
    """Loads a Musaffa detail page and polls for its Halal Rating letter
    grade. The "Screening Methodology" label renders before the grade
    itself, which arrives via a separate async call — poll instead of
    waiting on a fixed delay or on the label alone."""
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        for _ in range(GRADE_POLL_ATTEMPTS):
            text = page.inner_text("body")
            m = GRADE_RE.search(text)
            if m:
                return m.group(1).upper()
            page.wait_for_timeout(1000)
    except Exception:
        pass
    return "UNKNOWN"


def _fetch_grade_standalone(url, timeout_ms=12000):
    """Own Playwright instance + browser, for use from a worker thread —
    the sync API is only safe to use from the thread that created it, so
    each parallel fetch gets its own instead of sharing one page/browser."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            return _read_grade(browser.new_page(), url, timeout_ms)
        finally:
            browser.close()


def enrich_with_halal_grade(picks, timeout_ms=12000, max_workers=GRADE_FETCH_WORKERS):
    """Adds a 'halal_grade' field (e.g. "A+") to each pick by loading its own
    detail page, fetched concurrently (one browser per worker thread) —
    fine for the ~10 picks this runs against, not meant for scanning the
    whole screener."""
    urls = [
        f"https://musaffa.com/{'etf' if p['asset_type'] == 'etf' else 'stock'}/{p['ticker']}"
        for p in picks
    ]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(picks) or 1)) as pool:
        grades = list(pool.map(lambda u: _fetch_grade_standalone(u, timeout_ms), urls))
    for pick, grade in zip(picks, grades):
        pick["halal_grade"] = grade
    return picks


def get_halal_grades(tickers, timeout_ms=12000, max_workers=GRADE_FETCH_WORKERS):
    """tickers: iterable of tickers held (asset type unknown — Binance's
    holdings response doesn't distinguish stock vs ETF). Tries the /stock/
    detail page first, falling back to /etf/ if that comes back UNKNOWN.
    Fetched concurrently (one browser per worker thread). Used to re-check
    existing holdings for a grade downgrade, not just new candidates."""
    tickers = list(tickers)

    def fetch(ticker):
        grade = _fetch_grade_standalone(f"https://musaffa.com/stock/{ticker}", timeout_ms)
        if grade == "UNKNOWN":
            grade = _fetch_grade_standalone(f"https://musaffa.com/etf/{ticker}", timeout_ms)
        return ticker, grade

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tickers) or 1)) as pool:
        return dict(pool.map(fetch, tickers))


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


def get_stock_candidates(limit=7, exclude_tickers=frozenset()):
    """Sharia-compliant, Musaffa rating A/A+, analyst Buy/Strong Buy — sorted
    by analyst rating (Strong Buy first), tie-broken by the screener's own
    order (its rating-desc sort). exclude_tickers is filtered out before
    truncating to limit, so excluded picks don't shrink the result count."""
    rows = _scrape_rows(STOCK_SCREENER_URL)
    candidates = []
    for row in rows:
        ticker, name = _parse_name_cell(row[1])
        if not ticker or ticker in exclude_tickers:
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


def get_etf_candidates(limit=3, exclude_tickers=frozenset()):
    """Sharia-compliant, Musaffa rating A/A+ — already sorted by number of
    holdings (a diversification proxy) via the URL's sortBy param."""
    rows = _scrape_rows(ETF_SCREENER_URL)
    candidates = []
    for row in rows:
        ticker, name = _parse_name_cell(row[1])
        if not ticker or ticker in exclude_tickers:
            continue
        candidates.append({
            "ticker": ticker,
            "name": name,
            "num_holdings": row[6],
            "segment": row[7],
        })
    return candidates[:limit]
