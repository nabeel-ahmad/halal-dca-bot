"""
Weekly Portfolio Review — READ-ONLY, no orders ever placed.

Runs every Sunday and emails you two things about your actual current
Binance equity holdings:

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
     picked up an ethics flag, or had its Musaffa halal letter grade drop
     below A (A- or lower) since it was bought. This is informational only
     — no order is ever placed — and is not based on analyst rating, since
     Musaffa doesn't expose a per-ticker analyst-consensus rating outside
     its screener (only for the small set of tickers the screener itself
     returns, not arbitrary held tickers).

  4. Candidate stocks/ETFs matching a Musaffa screener filter you specified
     (Sharia-compliant, rating A/A+, analyst Buy/Strong Buy), excluding
     anything on the BDS priority-boycott list or a major US DoD prime
     contractor, with a mechanical $ split of idle cash above your reserve
     target.

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
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    _binance_signed_request,
    get_account_holdings,
    get_current_prices,
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


def check_musaffa(ticker):
    """musaffa.com/stock/<TICKER> — FAQ text: '<TICKER> is classified as <status>'."""
    url = f"https://musaffa.com/stock/{ticker}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return UNKNOWN, url, f"fetch failed: {e}"

    if ticker.upper() not in resp.text.upper():
        return UNKNOWN, url, "ticker not found on page"

    text = _strip_tags(resp.text)
    m = re.search(r"classified as\s+([a-z\- ]+?)(?:\.|,| according| by)", text, re.I)
    if not m:
        return UNKNOWN, url, "compliance phrase not found"
    status = m.group(1).strip().lower()
    if status == "halal":
        return COMPLIANT, url, status
    if status in ("not halal", "haram", "doubtful", "non-compliant"):
        return NON_COMPLIANT, url, status
    return UNKNOWN, url, f"unrecognized status text: {status!r}"


def check_halal_terminal(ticker):
    """api.halalterminal.com/api/screen/<TICKER> — real JSON API (not a
    scrape), screening against AAOIFI/DJIM/FTSE/MSCI/S&P simultaneously.
    Only called when HALAL_TERMINAL_API_KEY is set — sign up yourself at
    halalterminal.com for a free-tier key; this project never creates
    accounts or holds credentials on your behalf."""
    url = f"https://api.halalterminal.com/api/screen/{ticker}"
    if not HALAL_TERMINAL_API_KEY:
        return UNKNOWN, url, "HALAL_TERMINAL_API_KEY not set — skipped"
    try:
        resp = requests.post(url, headers={"X-API-Key": HALAL_TERMINAL_API_KEY}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return UNKNOWN, url, f"fetch failed: {e}"

    if resp.status_code == 401:
        return UNKNOWN, url, "API key missing or invalid"
    if resp.status_code == 429:
        return UNKNOWN, url, "quota exceeded"
    try:
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return UNKNOWN, url, f"fetch failed: {e}"

    # shariah_compliance_status is null on some cached rows (per the API's own
    # schema notes) — is_compliant is the boolean field that's actually
    # populated then, so fall back to it before giving up as UNKNOWN.
    status = data.get("shariah_compliance_status")
    if status == "compliant":
        return COMPLIANT, url, status
    if status == "non_compliant":
        return NON_COMPLIANT, url, status
    is_compliant = data.get("is_compliant")
    if is_compliant is True:
        return COMPLIANT, url, "is_compliant=true"
    if is_compliant is False:
        return NON_COMPLIANT, url, "is_compliant=false"
    return UNKNOWN, url, data.get("error_message") or status or "insufficient data"


def check_compliance(ticker):
    checks = {"musaffa": check_musaffa}
    if HALAL_TERMINAL_API_KEY:
        checks["halal_terminal"] = check_halal_terminal
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = {name: pool.submit(fn, ticker) for name, fn in checks.items()}
        results = {name: future.result() for name, future in futures.items()}

    statuses = {v[0] for v in results.values()}
    if NON_COMPLIANT in statuses:
        overall = NON_COMPLIANT
    elif statuses == {COMPLIANT}:
        overall = COMPLIANT
    else:
        overall = UNKNOWN  # any disagreement or fetch failure — flag for manual review
    return overall, results


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
        return {
            "ticker": ticker,
            "compliance": overall,
            "sources": sources,
            "value": value,
            "pct": pct if len(holdings) > 1 else Decimal("100"),
            "over_concentrated": over_concentrated,
            "ethics_flags": ethics_flags,
            "halal_grade": halal_grade,
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
        lines.append(f"{r['ticker']}: {r['compliance']} | halal rating: {r['halal_grade']}")
        for name, (status, url, detail) in r["sources"].items():
            lines.append(f"    {name}: {status} ({detail}) — {url}")
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
            f'<a href="{url}" style="color:#57606a;text-decoration:none;">{escape(name)}: {SYMBOL[status]} {escape(detail)}</a>'
            for name, (status, url, detail) in r["sources"].items()
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
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{escape(r["halal_grade"])}</td>'
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
    stocks = get_stock_candidates(NUM_STOCK_PICKS, exclude_tickers=EXCLUDED_TICKERS)
    etfs = get_etf_candidates(NUM_ETF_PICKS, exclude_tickers=EXCLUDED_TICKERS)
    picks = [dict(p, asset_type="stock") for p in stocks] + [dict(p, asset_type="etf") for p in etfs]
    enrich_with_halal_grade(picks)
    for p in picks:
        path = "etf" if p["asset_type"] == "etf" else "stock"
        p["musaffa_url"] = f"https://musaffa.com/{path}/{p['ticker']}"

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


def render_recommendations_text(rec):
    lines = ["\n=== This week's halal candidates (new money) ===\n"]
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
        if p["asset_type"] == "stock":
            lines.append(
                f"[STOCK] {p['ticker']} — {p['name']} | halal rating: {grade_note} | "
                f"analyst rating: {p['analyst_rating']} | "
                f"sector: {p['sector']} | mkt cap: {p['market_cap']} | suggested: ${p['suggested_usd']:.2f}"
            )
        else:
            lines.append(
                f"[ETF]   {p['ticker']} — {p['name']} | halal rating: {grade_note} | "
                f"holdings: {p['num_holdings']} | "
                f"segment: {p['segment']} | suggested: ${p['suggested_usd']:.2f}"
            )
    lines.append(
        "\nFilter applied (Musaffa screener): Sharia-compliant, Musaffa rating A/A+, "
        "analyst consensus Buy/Strong Buy (stocks) — sorted by number of holdings (ETFs). "
        "Also excludes anything on the BDS priority-boycott list or a major US DoD prime "
        "contractor (see ethics_screens.py)."
    )
    lines.append(
        "Binance has no API for marking favorites — favorite these manually in the app: "
        + ", ".join(p["ticker"] for p in rec["picks"])
    )
    return "\n".join(lines)


def render_recommendations_html(rec):
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
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{escape(p["ticker"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{type_badge}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{grade_badge(p)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-size:12px;">{escape(p["name"])}<br>{detail}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;text-align:right;">${p["suggested_usd"]:.2f}</td>'
            "</tr>"
        )

    note = ""
    if rec["investable"] == 0:
        note = '<p style="font-size:13px;color:#9a6700;">Nothing above the cash reserve target to deploy this week — candidates listed for reference only.</p>'

    favorite_list = ", ".join(escape(p["ticker"]) for p in rec["picks"])

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2328;background:#ffffff;max-width:680px;padding:16px;">
  <h3 style="margin-top:0;">This week's halal candidates (new money)</h3>
  <p style="font-size:13px;color:#57606a;">
    USDT+USDC balance: <b>${rec['stablecoin_balance']:.2f}</b> &nbsp;|&nbsp;
    cash reserve target: <b>${CASH_RESERVE_TARGET_USD}</b> &nbsp;|&nbsp;
    investable: <b>${rec['investable']:.2f}</b>
  </p>
  {note}
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;">
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Ticker</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Type</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Details</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;text-align:right;">Suggested</th>
      </tr>
    </thead>
    <tbody>
      {"".join(pick_row(p) for p in rec["picks"])}
    </tbody>
  </table>
  <p style="font-size:12px;color:#57606a;">
    Filter applied (Musaffa screener): Sharia-compliant, Musaffa rating A/A+, analyst consensus
    Buy/Strong Buy (stocks) &mdash; sorted by number of holdings (ETFs). Also excludes anything
    on the BDS priority-boycott list or a major US DoD prime contractor. Amounts split equally
    across the {len(rec["picks"])} picks from whatever's above your cash reserve target.
  </p>
  <p style="font-size:12px;color:#9a6700;">
    Binance has no API for marking favorites &mdash; favorite these manually in the app: {favorite_list}
  </p>
</div>
"""


def send_report_email(subject, text_body, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())


def main():
    required = [BINANCE_API_KEY, SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]
    if not all(required):
        print("Missing required environment variables/secrets. Aborting.", file=sys.stderr)
        sys.exit(1)

    holdings = {t: q for t, q in get_account_holdings().items() if q > 0}
    if not holdings:
        send_email_message(
            "Weekly portfolio review — nothing held",
            "No equity holdings found on Binance right now — nothing to review this week.",
        )
        return

    prices = get_current_prices(list(holdings))

    rows, total_value = compute_rows(holdings, prices)
    text_body = render_text(rows, total_value)
    html_body = render_html(rows, total_value)

    try:
        rec = compute_recommendations(get_stablecoin_balance())
        text_body += render_recommendations_text(rec)
        html_body += render_recommendations_html(rec)
    except Exception as e:
        print(f"Note: couldn't build this week's candidate recommendations: {e}", file=sys.stderr)
        text_body += f"\n\n(Couldn't pull this week's halal candidates: {e})"
        html_body += f'<p style="color:#9a6700;">(Couldn\'t pull this week\'s halal candidates: {escape(str(e))})</p>'

    send_report_email("Weekly portfolio review", text_body, html_body)


if __name__ == "__main__":
    main()
