"""
Weekly Portfolio Review — READ-ONLY, no orders ever placed.

Runs every Sunday and emails you two things about your actual current
Binance equity holdings:

  1. Sharia compliance re-check, against your two trusted sources that
     publish a free, machine-readable compliance page per ticker:
       - Musaffa (musaffa.com/stock/<TICKER>)
       - Zoya (zoya.finance/stocks/<ticker>)
     Hyssa blocks non-browser requests (403) and Islamic Finance Guru
     charges per-ticker screening, so both are only linked for manual
     cross-check, not scraped.
     Any fetch failure, unrecognized page structure, or disagreement
     between sources is reported as UNKNOWN, not silently skipped — this
     is exactly the case you'd want a human to look at.

  2. Mechanical concentration numbers: weight of each position, and a
     flag if any position exceeds CONCENTRATION_WARN_PCT of the portfolio,
     plus deviation from the target weights in halal_universe.csv if that
     ticker is in your watchlist. This reports numbers against your own
     stated targets — it does not recommend what to buy or sell.

SECURITY: never hardcode credentials. Same env vars as halal_dca_bot.py.
"""

import os
import re
import smtplib
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape, unescape

import requests

from halal_dca_bot import (
    BINANCE_API_KEY,
    EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    UNIVERSE_FILE,
    _binance_signed_request,
    get_account_holdings,
    get_current_prices,
    load_watchlist,
    send_email_message,
)
from musaffa_recommendations import get_etf_candidates, get_stock_candidates

CONCENTRATION_WARN_PCT = Decimal(os.environ.get("CONCENTRATION_WARN_PCT", "30"))
CASH_RESERVE_TARGET_USD = Decimal(os.environ.get("CASH_RESERVE_TARGET_USD", "500"))
NUM_STOCK_PICKS = int(os.environ.get("NUM_STOCK_PICKS", "7"))
NUM_ETF_PICKS = int(os.environ.get("NUM_ETF_PICKS", "3"))
HTTP_TIMEOUT = 15
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


def check_zoya(ticker):
    """zoya.finance/stocks/<ticker> — '<TICKER> stock is (not )?Shariah-compliant'."""
    url = f"https://zoya.finance/stocks/{ticker.lower()}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return UNKNOWN, url, f"fetch failed: {e}"

    if ticker.upper() not in resp.text.upper():
        return UNKNOWN, url, "ticker not found on page"

    m = re.search(
        rf"{re.escape(ticker)}\s+stock is\s*(?:&nbsp;|\s)*(?:<[^>]+>)*\s*(not\s+)?Shariah-compliant",
        resp.text,
        re.I,
    )
    if not m:
        return UNKNOWN, url, "compliance phrase not found"
    return (NON_COMPLIANT if m.group(1) else COMPLIANT), url, ("not Shariah-compliant" if m.group(1) else "Shariah-compliant")


def check_compliance(ticker):
    results = {"musaffa": check_musaffa(ticker), "zoya": check_zoya(ticker)}
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


def compute_rows(holdings, prices, watchlist_targets):
    """One pass over holdings: compliance check + weight/target-deviation, in a
    single structured form both the text and HTML renderers read from."""
    total_value = sum(qty * prices[t] for t, qty in holdings.items())
    rows = []
    for ticker, qty in sorted(holdings.items(), key=lambda kv: -(kv[1] * prices[kv[0]])):
        overall, sources = check_compliance(ticker)
        value = qty * prices[ticker]
        pct = (value / total_value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if total_value else Decimal("0")
        target = watchlist_targets.get(ticker)
        target_pct = (target * 100).quantize(Decimal("0.1")) if target is not None else None
        over_concentrated = pct > CONCENTRATION_WARN_PCT or len(holdings) == 1
        rows.append({
            "ticker": ticker,
            "compliance": overall,
            "sources": sources,
            "value": value,
            "pct": pct if len(holdings) > 1 else Decimal("100"),
            "target_pct": target_pct,
            "deviation": (pct - target_pct) if target_pct is not None else None,
            "over_concentrated": over_concentrated,
        })
        time.sleep(1)  # be polite — one fetch per ticker per week per source
    return rows, total_value


def render_text(rows, total_value):
    lines = []
    flagged_compliance = [r for r in rows if r["compliance"] != COMPLIANT]
    flagged_concentration = [r for r in rows if r["over_concentrated"]]

    summary = []
    if flagged_compliance:
        summary.append("COMPLIANCE: " + ", ".join(f"{r['ticker']} ({r['compliance']})" for r in flagged_compliance))
    if flagged_concentration:
        summary.append(
            f"CONCENTRATION (>{CONCENTRATION_WARN_PCT}% or single-holding): "
            + ", ".join(f"{r['ticker']} at {r['pct']}%" for r in flagged_concentration)
        )
    if not summary:
        summary.append("Nothing flagged this week.")
    lines.append("\n".join(summary))

    lines.append("\n=== Sharia compliance re-check ===\n")
    for r in rows:
        lines.append(f"{r['ticker']}: {r['compliance']}")
        for name, (status, url, detail) in r["sources"].items():
            lines.append(f"    {name}: {status} ({detail}) — {url}")
        lines.append("    manual cross-check: " + " | ".join(MANUAL_LINKS))

    lines.append("\n=== Position weights & concentration ===\n")
    for r in rows:
        line = f"{r['ticker']}: ${r['value']:.2f} ({r['pct']}% of portfolio)"
        if r["target_pct"] is not None:
            line += f" — target {r['target_pct']}%, deviation {r['deviation']:+.1f}pp"
        lines.append(line)
    lines.append(f"\nTotal portfolio value: ${total_value:.2f}")

    return "\n".join(lines)


def render_html(rows, total_value):
    flagged_compliance = [r for r in rows if r["compliance"] != COMPLIANT]
    flagged_concentration = [r for r in rows if r["over_concentrated"]]

    def badge(status):
        return (
            f'<span style="background:{BADGE_BG[status]};color:{COLOR[status]};'
            f'border-radius:4px;padding:2px 8px;font-weight:600;white-space:nowrap;">'
            f'{SYMBOL[status]} {escape(status.replace("_", " "))}</span>'
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
    summary_html = "<br>".join(summary_parts) if summary_parts else "✅ Nothing flagged this week."

    def compliance_row(r):
        sources_html = "<br>".join(
            f'<a href="{url}" style="color:#57606a;text-decoration:none;">{escape(name)}: {SYMBOL[status]} {escape(detail)}</a>'
            for name, (status, url, detail) in r["sources"].items()
        )
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{escape(r["ticker"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{badge(r["compliance"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-size:12px;">{sources_html}</td>'
            "</tr>"
        )

    def weight_row(r):
        dev_cell = ""
        if r["target_pct"] is not None:
            dev_color = "#cf222e" if abs(r["deviation"]) >= 5 else "#57606a"
            dev_cell = f'<span style="color:{dev_color};">{r["deviation"]:+.1f}pp</span> (target {r["target_pct"]}%)'
        conc_symbol = "\U0001f534" if r["over_concentrated"] else "\U0001f7e2"
        return (
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;font-weight:600;">{escape(r["ticker"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">${r["value"]:.2f}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{conc_symbol} {r["pct"]}%</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d0d7de;">{dev_cell}</td>'
            "</tr>"
        )

    manual_links_html = " | ".join(f'<a href="{u}" style="color:#57606a;">{escape(u)}</a>' for u in MANUAL_LINKS)

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2328;background:#ffffff;max-width:680px;padding:16px;">
  <p style="font-size:15px;">{summary_html}</p>

  <h3 style="margin-top:24px;">Sharia compliance re-check</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;">
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Ticker</th>
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">Overall</th>
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
        <th style="padding:8px;border-bottom:2px solid #d0d7de;">vs. target</th>
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
    stocks = get_stock_candidates(NUM_STOCK_PICKS)
    etfs = get_etf_candidates(NUM_ETF_PICKS)
    picks = [dict(p, asset_type="stock") for p in stocks] + [dict(p, asset_type="etf") for p in etfs]

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
        if p["asset_type"] == "stock":
            lines.append(
                f"[STOCK] {p['ticker']} — {p['name']} | analyst rating: {p['analyst_rating']} | "
                f"sector: {p['sector']} | mkt cap: {p['market_cap']} | suggested: ${p['suggested_usd']:.2f}"
            )
        else:
            lines.append(
                f"[ETF]   {p['ticker']} — {p['name']} | holdings: {p['num_holdings']} | "
                f"segment: {p['segment']} | suggested: ${p['suggested_usd']:.2f}"
            )
    lines.append(
        "\nFilter applied (Musaffa screener): Sharia-compliant, Musaffa rating A/A+, "
        "analyst consensus Buy/Strong Buy (stocks) — sorted by number of holdings (ETFs)."
    )
    lines.append(
        "Binance has no API for marking favorites — favorite these manually in the app: "
        + ", ".join(p["ticker"] for p in rec["picks"])
    )
    return "\n".join(lines)


def render_recommendations_html(rec):
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
    Buy/Strong Buy (stocks) &mdash; sorted by number of holdings (ETFs). Amounts split equally
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

    watchlist_targets = {}
    try:
        for w in load_watchlist(UNIVERSE_FILE):
            watchlist_targets[w["ticker"]] = w["target_weight"]
    except (FileNotFoundError, ValueError) as e:
        print(f"Note: couldn't load {UNIVERSE_FILE} for target-weight comparison: {e}", file=sys.stderr)

    rows, total_value = compute_rows(holdings, prices, watchlist_targets)
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
