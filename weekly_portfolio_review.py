"""
Weekly Portfolio Review — READ-ONLY, no orders ever placed.

Runs every Sunday and sends two separate emails:
  - PERSONAL_EMAIL_TO (your own address) gets the full report below,
    including your actual holdings, balances, and $ amounts.
  - EMAIL_TO (e.g. a group address) gets only item 4, the generic
    candidate list, with all $ figures stripped out — nothing tied to
    your personal account.

The full report covers these things about your actual current Binance
equity holdings:

  1. Sharia compliance re-check, against your trusted sources, all queried
     in parallel:
       - Musaffa (musaffa.com/stock/<TICKER>) — scraped
       - Halal Terminal (api.halalterminal.com) — real API, only checked if
         HALAL_TERMINAL_API_KEY is set (sign up yourself for a key; this
         project never creates accounts or handles credentials for you)
     Zoya was dropped as an automated source — its scrape stopped reliably
     matching the page (near-constant UNKNOWN), for reasons that could be a
     layout change, bot detection, or regional rendering; it's still linked
     for manual cross-check. Hyssa blocks non-browser requests (403) and
     Islamic Finance Guru charges per-ticker screening, so both are also
     only linked for manual cross-check, not scraped.
     Any fetch failure, unrecognized page structure, or disagreement
     between sources is reported as UNKNOWN, not silently skipped — this
     is exactly the case you'd want a human to look at.

  2. Mechanical concentration numbers: weight of each position, and a
     flag if any position exceeds CONCENTRATION_WARN_PCT of the portfolio.
     This reports numbers, not what to buy or sell.

  3. A "Consider selling" list: any held ticker that's gone NON_COMPLIANT,
     picked up an ethics flag, had its Musaffa halal letter grade drop below
     A (A- or lower), or whose grade simply couldn't be verified this run
     (a scrape failure is a "check this manually" signal, not silence — a
     downgrade flagged one week must not just vanish the next because the
     scrape happened to fail). This is informational only — no order is
     ever placed — and is not based on analyst rating, since Musaffa
     doesn't expose a per-ticker analyst-consensus rating outside its
     screener (only for the small set of tickers the screener itself
     returns, not arbitrary held tickers).

  4. Candidate stocks/ETFs matching a Musaffa screener filter you specified
     (Sharia-compliant, rating A/A+, analyst Buy/Strong Buy), excluding
     anything on the BDS priority-boycott list or a major US DoD prime
     contractor. Every candidate is then re-checked with the same
     dual-source compliance check (Musaffa + Halal Terminal) held positions
     get, routed to each source's ETF-specific endpoint for ETFs — a
     candidate either source can't confirm COMPLIANT (including an ETF
     neither can fully evaluate) is dropped, not shown with an unverified
     screener grade. Also cross-checked: the company name Musaffa's screener
     scraped against the one Halal Terminal's API independently reports for
     the same ticker — a mismatch (a renamed or repurposed company whose
     screener entry hasn't caught up) is dropped too, since the grade may
     have been computed against the wrong business. A mechanical $ split of
     idle cash above your reserve target follows.

SECURITY: never hardcode credentials. Env vars pulled from binance_equity.py.
"""

import os
import re
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape, unescape

import requests

from binance_equity import (
    BINANCE_API_KEY,
    EMAIL_TO,
    PERSONAL_EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    _binance_signed_request,
    get_account_holdings,
    get_current_prices,
    get_tradable_tickers,
    send_email_message,
)
from ethics_screens import BDS_TARGETS, DOD_CONTRACTORS, check_ethics_flags
from musaffa_recommendations import (
    enrich_with_halal_grade,
    get_etf_candidates,
    get_halal_grades,
    get_stock_candidates,
    grade_below_a,
)

EXCLUDED_TICKERS = frozenset(BDS_TARGETS) | frozenset(DOD_CONTRACTORS)

CONCENTRATION_WARN_PCT = Decimal(os.environ.get("CONCENTRATION_WARN_PCT", "30"))
CASH_RESERVE_TARGET_USD = Decimal(os.environ.get("CASH_RESERVE_TARGET_USD", "500"))
NUM_STOCK_PICKS = int(os.environ.get("NUM_STOCK_PICKS", "7"))
NUM_ETF_PICKS = int(os.environ.get("NUM_ETF_PICKS", "3"))
HALAL_TERMINAL_API_KEY = os.environ.get("HALAL_TERMINAL_API_KEY", "")
HTTP_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

COMPLIANT, NON_COMPLIANT, UNKNOWN = "COMPLIANT", "NON_COMPLIANT", "UNKNOWN"


def _strip_tags(html):
    return unescape(re.sub(r"<[^>]+>", " ", html))


def check_musaffa(ticker, asset_type="stock"):
    """musaffa.com/(stock|etf)/<TICKER> — FAQ text: '<TICKER> is classified as <status>'.
    Must use the /etf/ path for ETFs — the /stock/ page 404s for them.
    Returns (status, url, detail, name) — name is always None here; this
    scrape doesn't parse the page's own company-name text, unlike Halal
    Terminal's JSON which hands it back directly."""
    path = "etf" if asset_type == "etf" else "stock"
    url = f"https://musaffa.com/{path}/{ticker}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return UNKNOWN, url, f"fetch failed: {e}", None

    if ticker.upper() not in resp.text.upper():
        return UNKNOWN, url, "ticker not found on page", None

    text = _strip_tags(resp.text)
    m = re.search(r"classified as\s+([a-z\- ]+?)(?:\.|,| according| by)", text, re.I)
    if not m:
        return UNKNOWN, url, "compliance phrase not found", None
    status = m.group(1).strip().lower()
    if status == "halal":
        return COMPLIANT, url, status, None
    if status in ("not halal", "haram", "doubtful", "non-compliant"):
        return NON_COMPLIANT, url, status, None
    return UNKNOWN, url, f"unrecognized status text: {status!r}", None


def check_halal_terminal(ticker, asset_type="stock"):
    """api.halalterminal.com — real JSON API (not a scrape), screening
    against AAOIFI/DJIM/FTSE/MSCI/S&P simultaneously. Only called when
    HALAL_TERMINAL_API_KEY is set — sign up yourself at halalterminal.com
    for a free-tier key; this project never creates accounts or holds
    credentials on your behalf.

    ETFs need a different endpoint — the stock endpoint's own error message
    says so explicitly ("stock-screen endpoint cannot evaluate compliance.
    Use /api/etf/{symbol}/screen for holdings-based compliance"), and the
    two previously being conflated is why ETF candidates showed a
    confident-looking grade despite neither compliance source having
    actually evaluated them.

    Returns (status, url, detail, name) — name is the company name the API
    itself reports for the ticker (None if unavailable), used to cross-check
    against Musaffa's independently-scraped name and catch a stale
    ticker-to-company mapping on either side (e.g. a renamed/re-purposed
    company still carrying a grade computed against its old business)."""
    if asset_type == "etf":
        url = f"https://api.halalterminal.com/api/etf/{ticker}/screen"
    else:
        url = f"https://api.halalterminal.com/api/screen/{ticker}"
    if not HALAL_TERMINAL_API_KEY:
        return UNKNOWN, url, "HALAL_TERMINAL_API_KEY not set — skipped", None
    try:
        resp = requests.post(url, headers={"X-API-Key": HALAL_TERMINAL_API_KEY}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return UNKNOWN, url, f"fetch failed: {e}", None

    if resp.status_code == 401:
        return UNKNOWN, url, "API key missing or invalid", None
    if resp.status_code == 429:
        return UNKNOWN, url, "quota exceeded", None
    try:
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return UNKNOWN, url, f"fetch failed: {e}", None

    name = data.get("name")

    # shariah_compliance_status is null on some cached rows (per the API's own
    # schema notes) — is_compliant is the boolean field that's actually
    # populated then, so fall back to it before giving up as UNKNOWN.
    status = data.get("shariah_compliance_status")
    if status == "compliant":
        return COMPLIANT, url, status, name
    if status == "non_compliant":
        return NON_COMPLIANT, url, status, name
    is_compliant = data.get("is_compliant")
    if is_compliant is True:
        return COMPLIANT, url, "is_compliant=true", name
    if is_compliant is False:
        return NON_COMPLIANT, url, "is_compliant=false", name
    return UNKNOWN, url, data.get("error_message") or status or "insufficient data", name


def check_compliance(ticker, asset_type="stock"):
    checks = {"musaffa": check_musaffa}
    if HALAL_TERMINAL_API_KEY:
        checks["halal_terminal"] = check_halal_terminal
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = {name: pool.submit(fn, ticker, asset_type) for name, fn in checks.items()}
        results = {name: future.result() for name, future in futures.items()}

    statuses = {v[0] for v in results.values()}
    if NON_COMPLIANT in statuses:
        overall = NON_COMPLIANT
    elif statuses == {COMPLIANT}:
        overall = COMPLIANT
    else:
        overall = UNKNOWN  # any disagreement or fetch failure — flag for manual review
    return overall, results


_CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "holdings", "holding", "group", "trust", "the",
}


def _significant_words(name):
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if w not in _CORP_SUFFIXES}


def _names_plausibly_match(a, b):
    """Loose same-company check after stripping generic corporate suffixes
    ('Holdings', 'Inc', ...) that would otherwise overlap for two unrelated
    companies. Missing data on either side isn't treated as a mismatch —
    only an actual disagreement between two independent sources is."""
    wa, wb = _significant_words(a), _significant_words(b)
    if not wa or not wb:
        return True
    return bool(wa & wb)


def get_verified_compliant_tickers(candidates):
    """Runs the full dual-source check_compliance() (same one used for held
    positions) against each new-money candidate, keyed to its own asset_type
    so ETFs hit the ETF endpoint instead of the stock one. Only tickers both
    sources actually confirm COMPLIANT pass — an UNKNOWN (declined/failed
    to evaluate, e.g. an ETF with no working screen) is excluded same as a
    NON_COMPLIANT, not defaulted through. The screener's own scraped letter
    grade is not a substitute for this — it's a different, unverified
    pipeline (see check_musaffa/check_halal_terminal vs.
    musaffa_recommendations.enrich_with_halal_grade).

    Also cross-checks the candidate's own Musaffa-scraped company name
    against the company name Halal Terminal's API independently reports for
    the same ticker. A renamed/repurposed company (a ticker whose business
    changed but whose screener entry didn't catch up) shows up as exactly
    this: two sources disagreeing on what company the ticker even is. A
    mismatch is excluded rather than trusted, since the grade may have been
    computed against stale business data."""
    candidates = list(candidates)
    with ThreadPoolExecutor(max_workers=min(TICKER_FETCH_WORKERS, len(candidates) or 1)) as pool:
        results = list(pool.map(lambda c: (c, check_compliance(c["ticker"], c["asset_type"])), candidates))

    verified = set()
    for candidate, (overall, sources) in results:
        ticker = candidate["ticker"]
        if overall != COMPLIANT:
            continue
        halal_terminal_name = sources.get("halal_terminal", (None, None, None, None))[3]
        if halal_terminal_name and not _names_plausibly_match(candidate.get("name"), halal_terminal_name):
            print(
                f"Note: {ticker} excluded from candidates — name mismatch between Musaffa "
                f"({candidate.get('name')!r}) and Halal Terminal ({halal_terminal_name!r}); "
                "likely a stale ticker-to-company mapping on one side.",
                file=sys.stderr,
            )
            continue
        verified.add(ticker)
    return verified


SYMBOL = {COMPLIANT: "✅", NON_COMPLIANT: "❌", UNKNOWN: "⚠️"}
COLOR = {COMPLIANT: "#1a7f37", NON_COMPLIANT: "#cf222e", UNKNOWN: "#9a6700"}
BADGE_BG = {COMPLIANT: "#dafbe1", NON_COMPLIANT: "#ffebe9", UNKNOWN: "#fff8c5"}

MANUAL_LINKS = (
    "https://hyssa.com/en/how-to-start/explore-halal-screened-stocks",
    "https://www.islamicfinanceguru.com/resources/halal-stocks-screening-guide",
)


TICKER_FETCH_WORKERS = 5


def compute_rows(holdings, prices):
    """One pass over holdings: compliance check + weight/concentration, in a
    single structured form both the text and HTML renderers read from.
    Tickers are processed concurrently — compliance/grade checks are one
    HTTP or headless-browser round-trip per ticker per source, otherwise
    serialized needlessly."""
    total_value = sum(qty * prices[t] for t, qty in holdings.items())
    try:
        halal_grades = get_halal_grades(holdings)
    except Exception as e:
        print(f"Note: couldn't pull halal grades for holdings: {e}", file=sys.stderr)
        halal_grades = {}

    def build_row(item):
        ticker, qty = item
        overall, sources = check_compliance(ticker)
        value = qty * prices[ticker]
        pct = (value / total_value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if total_value else Decimal("0")
        over_concentrated = pct > CONCENTRATION_WARN_PCT or len(holdings) == 1
        halal_grade = halal_grades.get(ticker, "UNKNOWN")
        sell_reasons = []
        if overall == NON_COMPLIANT:
            sell_reasons.append("no longer Sharia-compliant")
        ethics_flags = check_ethics_flags(ticker)
        if ethics_flags:
            sell_reasons.append("/".join(f["type"] for f in ethics_flags) + " ethics flag")
        if grade_below_a(halal_grade):
            sell_reasons.append(f"halal rating downgraded to {halal_grade}")
        elif halal_grade == "UNKNOWN":
            sell_reasons.append("halal rating could not be verified this run — check manually")
        return {
            "ticker": ticker,
            "compliance": overall,
            "sources": sources,
            "value": value,
            "pct": pct if len(holdings) > 1 else Decimal("100"),
            "over_concentrated": over_concentrated,
            "ethics_flags": ethics_flags,
            "halal_grade": halal_grade,
            # The grade comes from a separate pipeline (a screener-table
            # scrape) than compliance (direct Musaffa/Halal Terminal
            # lookups) — nothing reconciles them. A confident-looking grade
            # next to an UNKNOWN compliance result is not itself verified,
            # so callers must not display the grade as if it were.
            "halal_grade_verified": overall != UNKNOWN,
            "sell_reasons": sell_reasons,
        }

    items = sorted(holdings.items(), key=lambda kv: -(kv[1] * prices[kv[0]]))
    with ThreadPoolExecutor(max_workers=min(TICKER_FETCH_WORKERS, len(items) or 1)) as pool:
        rows = list(pool.map(build_row, items))
    return rows, total_value


def render_text(rows, total_value):
    lines = []
    flagged_compliance = [r for r in rows if r["compliance"] != COMPLIANT]
    flagged_concentration = [r for r in rows if r["over_concentrated"]]
    flagged_ethics = [r for r in rows if r["ethics_flags"]]
    flagged_sell = [r for r in rows if r["sell_reasons"]]

    summary = []
    if flagged_compliance:
        summary.append("COMPLIANCE: " + ", ".join(f"{r['ticker']} ({r['compliance']})" for r in flagged_compliance))
    if flagged_concentration:
        summary.append(
            f"CONCENTRATION (>{CONCENTRATION_WARN_PCT}% or single-holding): "
            + ", ".join(f"{r['ticker']} at {r['pct']}%" for r in flagged_concentration)
        )
    if flagged_ethics:
        summary.append(
            "ETHICS: " + ", ".join(
                f"{r['ticker']} ({'/'.join(f['type'] for f in r['ethics_flags'])})" for r in flagged_ethics
            )
        )
    if flagged_sell:
        summary.append("CONSIDER SELLING: " + ", ".join(r["ticker"] for r in flagged_sell))
    if not summary:
        summary.append("Nothing flagged this week.")
    lines.append("\n".join(summary))

    if flagged_sell:
        lines.append("\n=== Consider selling ===\n")
        for r in flagged_sell:
            lines.append(f"{r['ticker']}: " + "; ".join(r["sell_reasons"]))
        lines.append(
            "This is not a sell order — it never places one. Review and sell manually in Binance if you agree."
        )

    lines.append("\n=== Sharia compliance re-check ===\n")
    for r in rows:
        grade_note = r["halal_grade"]
        if r["halal_grade"] != "UNKNOWN" and not r["halal_grade_verified"]:
            grade_note += " (screener-scraped, unverified — compliance sources returned UNKNOWN)"
        lines.append(f"{r['ticker']}: {r['compliance']} | halal rating: {grade_note}")
        for source_name, (status, url, detail, _company_name) in r["sources"].items():
            lines.append(f"    {source_name}: {status} ({detail}) — {url}")
        lines.append("    manual cross-check: " + " | ".join(MANUAL_LINKS))
        for f in r["ethics_flags"]:
            lines.append(f"    ⚠ {f['type']}: {f['detail']} — {f['source']}")

    lines.append("\n=== Position weights & concentration ===\n")
    for r in rows:
        lines.append(f"{r['ticker']}: ${r['value']:.2f} ({r['pct']}% of portfolio)")
    lines.append(f"\nTotal portfolio value: ${total_value:.2f}")

    return "\n".join(lines)


def render_html(rows, total_value):
    flagged_compliance = [r for r in rows if r["compliance"] != COMPLIANT]
    flagged_concentration = [r for r in rows if r["over_concentrated"]]
    flagged_ethics = [r for r in rows if r["ethics_flags"]]
    flagged_sell = [r for r in rows if r["sell_reasons"]]

    def badge(status):
        return (
            f'<span style="background:{BADGE_BG[status]};color:{COLOR[status]};'
            f'border-radius:4px;padding:2px 8px;font-weight:600;white-space:nowrap;">'
            f'{SYMBOL[status]} {escape(status.replace("_", " "))}</span>'
        )

    def ethics_badge(flag_type):
        return (
            f'<span style="background:#ffebe9;color:#cf222e;border-radius:4px;padding:2px 8px;'
            f'font-weight:600;white-space:nowrap;">🚫 {escape(flag_type)}</span>'
        )

    summary_parts = []
    if flagged_compliance:
        summary_parts.append(
            "<b>Compliance:</b> " + ", ".join(f"{escape(r['ticker'])} {badge(r['compliance'])}" for r in flagged_compliance)
        )
    if flagged_concentration:
        summary_parts.append(
            f"<b>Concentration</b> (&gt;{CONCENTRATION_WARN_PCT}% or single-holding): "
            + ", ".join(f"\U0001f534 {escape(r['ticker'])} at {r['pct']}%" for r in flagged_concentration)
        )
    if flagged_ethics:
        summary_parts.append(
            "<b>Ethics:</b> " + ", ".join(
                f"{escape(r['ticker'])} " + " ".join(ethics_badge(f["type"]) for f in r["ethics_flags"])
                for r in flagged_ethics
            )
        )
    if flagged_sell:
        summary_parts.append(
            "<b>Consider selling:</b> " + ", ".join(f"\U0001f6ab {escape(r['ticker'])}" for r in flagged_sell)
        )
    summary_html = "<br>".join(summary_parts) if summary_parts else "✅ Nothing flagged this week."

    def sell_row(r):
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{escape(r["ticker"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{escape("; ".join(r["sell_reasons"]))}</td>'
            "</tr>"
        )

    def compliance_row(r):
        sources_html = "<br>".join(
            f'<a href="{url}" style="color:#57606a;text-decoration:none;">{escape(source_name)}: {SYMBOL[status]} {escape(detail)}</a>'
            for source_name, (status, url, detail, _company_name) in r["sources"].items()
        )
        ethics_html = ""
        if r["ethics_flags"]:
            ethics_html = "<br>" + "<br>".join(
                f'<span style="color:#cf222e;">🚫 {escape(f["type"])}: {escape(f["detail"])}</span>' for f in r["ethics_flags"]
            )
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{escape(r["ticker"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{badge(r["compliance"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{escape(r["halal_grade"])}'
            + (
                '<br><span style="font-size:11px;color:#9a6700;">(screener-scraped, unverified &mdash; '
                "compliance sources returned UNKNOWN)</span>"
                if r["halal_grade"] != "UNKNOWN" and not r["halal_grade_verified"] else ""
            )
            + "</td>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-size:12px;">{sources_html}{ethics_html}</td>'
            "</tr>"
        )

    def weight_row(r):
        conc_symbol = "\U0001f534" if r["over_concentrated"] else "\U0001f7e2"
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{escape(r["ticker"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">${r["value"]:.2f}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{conc_symbol} {r["pct"]}%</td>'
            "</tr>"
        )

    manual_links_html = " | ".join(f'<a href="{u}" style="color:#57606a;">{escape(u)}</a>' for u in MANUAL_LINKS)

    sell_section = ""
    if flagged_sell:
        sell_section = f"""
  <h3 style="margin-top:24px;">Consider selling</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;">
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Ticker</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Why</th>
      </tr>
    </thead>
    <tbody>
      {"".join(sell_row(r) for r in flagged_sell)}
    </tbody>
  </table>
  <p style="font-size:12px;color:#57606a;">This is not a sell order — it never places one. Review and sell manually in Binance if you agree.</p>
"""

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2328;background:#ffffff;max-width:680px;padding:16px;">
  <p style="font-size:15px;">{summary_html}</p>
  {sell_section}
  <h3 style="margin-top:24px;">Sharia compliance re-check</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;">
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Ticker</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Overall</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Halal rating</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Sources</th>
      </tr>
    </thead>
    <tbody>
      {"".join(compliance_row(r) for r in rows)}
    </tbody>
  </table>
  <p style="font-size:12px;color:#57606a;">Manual cross-check (not auto-checked): {manual_links_html}</p>

  <h3 style="margin-top:24px;">Position weights &amp; concentration</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;">
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Ticker</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Value</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">% of portfolio</th>
      </tr>
    </thead>
    <tbody>
      {"".join(weight_row(r) for r in rows)}
    </tbody>
  </table>
  <p style="font-size:13px;color:#57606a;">Total portfolio value: <b>${total_value:.2f}</b></p>
</div>
"""


def get_stablecoin_balance():
    """Free USDT + USDC on the spot account — the 'idle cash' this review
    tries to keep under CASH_RESERVE_TARGET_USD."""
    data = _binance_signed_request("GET", "/api/v3/account", {})
    total = Decimal("0")
    for bal in data.get("balances", []):
        if bal["asset"] in ("USDT", "USDC"):
            total += Decimal(bal["free"])
    return total


def compute_recommendations(stablecoin_balance):
    """Mechanical allocation: pull candidates matching the exact Musaffa filter
    you specified (Sharia-compliant, rating A/A+, analyst Buy/Strong Buy for
    stocks; sorted by number of holdings for ETFs), then split whatever idle
    cash sits above CASH_RESERVE_TARGET_USD equally across the picks. This
    doesn't pick stocks by growth judgment — it reports what your own filter
    returns and does the arithmetic to keep cash under your target."""
    # Overfetch since some Musaffa candidates won't be tradable on Binance
    # Stocks Trading, or get flagged by Halal Terminal; filtering those out
    # shouldn't shrink the final count.
    stocks = [dict(c, asset_type="stock") for c in get_stock_candidates(NUM_STOCK_PICKS * 4, exclude_tickers=EXCLUDED_TICKERS)]
    etfs = [dict(c, asset_type="etf") for c in get_etf_candidates(NUM_ETF_PICKS * 4, exclude_tickers=EXCLUDED_TICKERS)]
    candidates = stocks + etfs
    tradable = get_tradable_tickers({c["ticker"] for c in candidates})
    # Full dual-source compliance check per candidate, same as held positions
    # get — not just a Halal Terminal non-compliant filter. A candidate whose
    # sources can't actually evaluate it (e.g. an ETF the screen endpoint
    # declines) is excluded, not shown with an unverified "A" grade.
    verified_compliant = get_verified_compliant_tickers([c for c in candidates if c["ticker"] in tradable])
    keep = tradable & verified_compliant
    stocks = [c for c in stocks if c["ticker"] in keep][:NUM_STOCK_PICKS]
    etfs = [c for c in etfs if c["ticker"] in keep][:NUM_ETF_PICKS]
    picks = stocks + etfs
    enrich_with_halal_grade(picks)
    for p in picks:
        path = "etf" if p["asset_type"] == "etf" else "stock"
        p["musaffa_url"] = f"https://musaffa.com/{path}/{p['ticker']}"
        p["binance_url"] = f"https://www.binance.com/en/stocks/EQ_{p['ticker']}"

    investable = max(Decimal("0"), stablecoin_balance - CASH_RESERVE_TARGET_USD)
    per_asset = (investable / len(picks)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if picks else Decimal("0")
    for p in picks:
        p["suggested_usd"] = per_asset

    return {
        "picks": picks,
        "stablecoin_balance": stablecoin_balance,
        "investable": investable,
        "per_asset": per_asset,
    }


def render_recommendations_text(rec, include_amounts=True):
    lines = ["\n=== This week's halal candidates (new money) ===\n"]
    if include_amounts:
        lines.append(
            f"USDT+USDC balance: ${rec['stablecoin_balance']:.2f} | "
            f"cash reserve target: ${CASH_RESERVE_TARGET_USD} | "
            f"investable: ${rec['investable']:.2f}"
        )
        if rec["investable"] == 0:
            lines.append("Nothing above the cash reserve target to deploy this week — candidates listed for reference only.")
        lines.append("")
    for p in rec["picks"]:
        grade_note = p["halal_grade"] if p["halal_grade"] != "UNKNOWN" else f"UNKNOWN (verify: {p['musaffa_url']})"
        suggested = f" | suggested: ${p['suggested_usd']:.2f}" if include_amounts else ""
        if p["asset_type"] == "stock":
            lines.append(
                f"[STOCK] {p['ticker']} — {p['name']} | halal rating: {grade_note} | "
                f"analyst rating: {p['analyst_rating']} | "
                f"sector: {p['sector']} | mkt cap: {p['market_cap']}{suggested} | "
                f"buy on Binance: {p['binance_url']}"
            )
        else:
            lines.append(
                f"[ETF]   {p['ticker']} — {p['name']} | halal rating: {grade_note} | "
                f"holdings: {p['num_holdings']} | "
                f"segment: {p['segment']}{suggested} | "
                f"buy on Binance: {p['binance_url']}"
            )
    lines.append(
        "\nFilter applied (Musaffa screener): Sharia-compliant, Musaffa rating A/A+, "
        "analyst consensus Buy/Strong Buy (stocks) — sorted by number of holdings (ETFs). "
        "Also excludes anything on the BDS priority-boycott list or a major US DoD prime "
        "contractor (see ethics_screens.py). Every pick below additionally passed a full "
        "dual-source compliance re-check (Musaffa + Halal Terminal, same check held positions "
        "get) — anything that check couldn't confirm COMPLIANT, including ETFs neither source "
        "can fully evaluate, is excluded rather than shown unverified."
    )
    lines.append(
        "Binance has no API for marking favorites — favorite these manually in the app: "
        + ", ".join(p["ticker"] for p in rec["picks"])
    )
    return "\n".join(lines)


def render_recommendations_html(rec, include_amounts=True):
    def grade_badge(p):
        grade = p["halal_grade"]
        color = "#9a6700" if grade == "UNKNOWN" else "#1a7f37"
        bg = "#fff8c5" if grade == "UNKNOWN" else "#dafbe1"
        badge = f'<span style="background:{bg};color:{color};border-radius:4px;padding:2px 6px;font-weight:600;white-space:nowrap;">{escape(grade)}</span>'
        if grade == "UNKNOWN":
            badge += f'<br><a href="{p["musaffa_url"]}" style="font-size:11px;color:#57606a;">verify</a>'
        return badge

    def pick_row(p):
        if p["asset_type"] == "stock":
            detail = f"{escape(p['sector'])} · {escape(p['market_cap'])} · analyst: <b>{escape(p['analyst_rating'])}</b>"
            type_badge = '<span style="background:#ddf4ff;color:#0969da;border-radius:4px;padding:2px 6px;font-size:12px;">STOCK</span>'
        else:
            detail = f"{escape(p['segment'])} · {escape(p['num_holdings'])} holdings"
            type_badge = '<span style="background:#fbefff;color:#8250df;border-radius:4px;padding:2px 6px;font-size:12px;">ETF</span>'
        ticker_cell = (
            f'<a href="{escape(p["binance_url"])}" style="color:#1f2328;text-decoration:none;">'
            f'{escape(p["ticker"])}</a>'
        )
        amount_cell = (
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;text-align:right;">${p["suggested_usd"]:.2f}</td>'
            if include_amounts else ""
        )
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{ticker_cell}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{type_badge}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{grade_badge(p)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-size:12px;">{escape(p["name"])}<br>{detail}</td>'
            f"{amount_cell}"
            "</tr>"
        )

    note = ""
    if include_amounts and rec["investable"] == 0:
        note = '<p style="font-size:13px;color:#9a6700;">Nothing above the cash reserve target to deploy this week — candidates listed for reference only.</p>'

    favorite_list = ", ".join(escape(p["ticker"]) for p in rec["picks"])

    balance_line = (
        f"""<p style="font-size:13px;color:#57606a;">
    USDT+USDC balance: <b>${rec['stablecoin_balance']:.2f}</b> &nbsp;|&nbsp;
    cash reserve target: <b>${CASH_RESERVE_TARGET_USD}</b> &nbsp;|&nbsp;
    investable: <b>${rec['investable']:.2f}</b>
  </p>"""
        if include_amounts else ""
    )
    amount_header = (
        '<th style="padding:8px;border-bottom:2px solid #d0d7de;text-align:right;">Suggested</th>'
        if include_amounts else ""
    )
    amounts_note = (
        f" Amounts split equally across the {len(rec['picks'])} picks from whatever's above your cash reserve target."
        if include_amounts else ""
    )

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2328;background:#ffffff;max-width:680px;padding:16px;">
  <h3 style="margin-top:0;">This week's halal candidates (new money)</h3>
  {balance_line}
  {note}
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;">
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Ticker</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Type</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Details</th>
        {amount_header}
      </tr>
    </thead>
    <tbody>
      {"".join(pick_row(p) for p in rec["picks"])}
    </tbody>
  </table>
  <p style="font-size:12px;color:#57606a;">
    Filter applied (Musaffa screener): Sharia-compliant, Musaffa rating A/A+, analyst consensus
    Buy/Strong Buy (stocks) &mdash; sorted by number of holdings (ETFs). Also excludes anything
    on the BDS priority-boycott list or a major US DoD prime contractor. Every pick below
    additionally passed a full dual-source compliance re-check (Musaffa + Halal Terminal, same
    check held positions get) &mdash; anything that check couldn't confirm COMPLIANT, including
    ETFs neither source can fully evaluate, is excluded rather than shown unverified.{amounts_note}
  </p>
  <p style="font-size:12px;color:#9a6700;">
    Binance has no API for marking favorites &mdash; favorite these manually in the app: {favorite_list}
  </p>
</div>
"""


def send_report_email(subject, text_body, html_body, to):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to], msg.as_string())


def main():
    required = [BINANCE_API_KEY, SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO, PERSONAL_EMAIL_TO]
    if not all(required):
        print("Missing required environment variables/secrets. Aborting.", file=sys.stderr)
        sys.exit(1)

    dry_run = os.environ.get("DRY_RUN") == "1"

    holdings = {t: q for t, q in get_account_holdings().items() if q > 0}
    if not holdings:
        message = "No equity holdings found on Binance right now — nothing to review this week."
        if dry_run:
            print(f"DRY_RUN=1 — would have sent to {PERSONAL_EMAIL_TO}: {message}")
        else:
            send_email_message("Weekly portfolio review — nothing held", message, to=PERSONAL_EMAIL_TO)
        return

    prices = get_current_prices(list(holdings))

    rows, total_value = compute_rows(holdings, prices)
    personal_text = render_text(rows, total_value)
    personal_html = render_html(rows, total_value)

    group_text = ""
    group_html = ""
    try:
        rec = compute_recommendations(get_stablecoin_balance())
        personal_text += render_recommendations_text(rec)
        personal_html += render_recommendations_html(rec)
        group_text = render_recommendations_text(rec, include_amounts=False)
        group_html = render_recommendations_html(rec, include_amounts=False)
    except Exception as e:
        print(f"Note: couldn't build this week's candidate recommendations: {e}", file=sys.stderr)
        note_text = f"\n\n(Couldn't pull this week's halal candidates: {e})"
        note_html = f'<p style="color:#9a6700;">(Couldn\'t pull this week\'s halal candidates: {escape(str(e))})</p>'
        personal_text += note_text
        personal_html += note_html
        group_text += note_text
        group_html += note_html

    if dry_run:
        with open("digest_preview_personal.html", "w") as f:
            f.write(personal_html)
        with open("digest_preview_group.html", "w") as f:
            f.write(group_html)
        print("DRY_RUN=1 — wrote digest_preview_personal.html and digest_preview_group.html instead of sending email.")
        return

    send_report_email("Weekly portfolio review", personal_text, personal_html, to=PERSONAL_EMAIL_TO)
    if group_html:
        send_report_email("This week's halal candidates", group_text, group_html, to=EMAIL_TO)


if __name__ == "__main__":
    main()
