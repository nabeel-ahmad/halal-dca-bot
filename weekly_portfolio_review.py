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
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from html import unescape

import requests

from halal_dca_bot import (
    BINANCE_API_KEY,
    EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_USER,
    UNIVERSE_FILE,
    get_account_holdings,
    get_current_prices,
    load_watchlist,
    send_email_message,
)

CONCENTRATION_WARN_PCT = Decimal(os.environ.get("CONCENTRATION_WARN_PCT", "30"))
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


def build_report(holdings, prices, watchlist_targets):
    total_value = sum(qty * prices[t] for t, qty in holdings.items())
    lines = []
    flagged_compliance = []
    flagged_concentration = []

    lines.append("=== Sharia compliance re-check ===\n")
    for ticker in sorted(holdings):
        overall, sources = check_compliance(ticker)
        lines.append(f"{ticker}: {overall}")
        for name, (status, url, detail) in sources.items():
            lines.append(f"    {name}: {status} ({detail}) — {url}")
        lines.append(
            f"    manual cross-check: https://hyssa.com/en/how-to-start/explore-halal-screened-stocks"
            f" | https://www.islamicfinanceguru.com/resources/halal-stocks-screening-guide"
        )
        if overall != COMPLIANT:
            flagged_compliance.append((ticker, overall))
        time.sleep(1)  # be polite — one fetch per ticker per week per source

    lines.append("\n=== Position weights & concentration ===\n")
    for ticker, qty in sorted(holdings.items(), key=lambda kv: -(kv[1] * prices[kv[0]])):
        value = qty * prices[ticker]
        pct = (value / total_value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if total_value else Decimal("0")
        line = f"{ticker}: ${value:.2f} ({pct}% of portfolio)"
        target = watchlist_targets.get(ticker)
        if target is not None:
            target_pct = (target * 100).quantize(Decimal("0.1"))
            line += f" — target {target_pct}%, deviation {pct - target_pct:+.1f}pp"
        lines.append(line)
        if pct > CONCENTRATION_WARN_PCT:
            flagged_concentration.append((ticker, pct))

    lines.append(f"\nTotal portfolio value: ${total_value:.2f}")
    if len(holdings) == 1:
        flagged_concentration.append((next(iter(holdings)), Decimal("100")))

    summary = []
    if flagged_compliance:
        summary.append(
            "COMPLIANCE: " + ", ".join(f"{t} ({s})" for t, s in flagged_compliance)
        )
    if flagged_concentration:
        summary.append(
            f"CONCENTRATION (>{CONCENTRATION_WARN_PCT}% or single-holding): "
            + ", ".join(f"{t} at {p}%" for t, p in flagged_concentration)
        )
    if not summary:
        summary.append("Nothing flagged this week.")

    return "\n".join(summary) + "\n\n" + "\n".join(lines)


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

    body = build_report(holdings, prices, watchlist_targets)
    send_email_message("Weekly portfolio review", body)


if __name__ == "__main__":
    main()
