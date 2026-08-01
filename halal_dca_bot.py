"""
Halal DCA / Rebalance Bot — PROPOSE-THEN-APPROVE, not fully autonomous.

What this does:
  1. Reads your halal watchlist (halal_universe.csv) — a pre-screened list YOU maintain
     (from a shariah index or an app like Zoya/Islamicly), intersected with whatever
     Binance actually lets you trade.
  2. Pulls current prices + your account balances from Binance.
  3. Works out a simple DCA / rebalance proposal (target-weight based).
  4. Emails you the proposal (SMTP) and WAITS for your explicit approval —
     a reply email containing "yes"/"no" — via IMAP, before placing any order.
  5. Never touches margin/futures/withdrawals. Spot-only, hard dollar caps enforced
     locally as a last line of defense even if your API key were mis-scoped.

SECURITY
  - Never hardcode API keys/tokens here. Read them from environment variables only
    (set as encrypted secrets in your CI, e.g. GitHub Actions secrets).
  - This script never logs the API key/secret/token values. Don't add print()
    statements that dump them, and don't paste them into chat with an AI assistant.
  - Run with DRY_RUN=true for your first several cycles.

IMPORTANT / TODO before this is production-ready
  - Binance's stock-trading feature launched in 2026 and is very new. The exact
    REST endpoint(s) for placing a STOCK order (as opposed to a crypto spot order)
    were not confirmed at the time this script was written. Check
    https://developers.binance.com/en for the current "Stocks" API section and
    fill in `place_stock_order()` accordingly before relying on this for real
    trades. Placeholder logic below assumes a Binance-compatible REST shape;
    verify field names against the official docs first.
"""

import csv
import email
import imaplib
import json
import os
import smtplib
import sys
import time
from decimal import Decimal, ROUND_DOWN
from email.mime.text import MIMEText

import requests

# ---------------------------------------------------------------------------
# Config — pull secrets from environment only. Never hardcode.
# ---------------------------------------------------------------------------
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# Hard safety caps — adjust to your own comfort level.
MAX_ORDER_USD = Decimal(os.environ.get("MAX_ORDER_USD", "200"))
MAX_TOTAL_USD_PER_RUN = Decimal(os.environ.get("MAX_TOTAL_USD_PER_RUN", "1000"))
MONTHLY_CONTRIBUTION_USD = Decimal(os.environ.get("MONTHLY_CONTRIBUTION_USD", "500"))

UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE", "halal_universe.csv")
APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "1800"))

BINANCE_BASE_URL = "https://api.binance.com"  # confirm this is correct host for stocks product


def load_watchlist(path):
    """Load your pre-screened halal watchlist: ticker,target_weight"""
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "ticker": row["ticker"].strip(),
                "target_weight": Decimal(row["target_weight"]),
            })
    total = sum(r["target_weight"] for r in rows)
    if abs(total - Decimal("1")) > Decimal("0.01"):
        raise ValueError(f"Target weights in {path} sum to {total}, expected ~1.0")
    return rows


def get_current_prices(tickers):
    """
    TODO: replace with the confirmed Binance stocks price endpoint.
    Returning a stub here so the rest of the pipeline is testable end-to-end
    before you've wired up the real API call.
    """
    raise NotImplementedError(
        "Fill in get_current_prices() using the official Binance stocks API "
        "reference once you've confirmed the endpoint at developers.binance.com"
    )


def get_account_holdings():
    """TODO: same as above — wire up to the real account/positions endpoint."""
    raise NotImplementedError("Fill in get_account_holdings()")


def compute_proposal(watchlist, prices, holdings, contribution):
    """
    Simple rebalance-toward-target-weight proposal using new contribution cash
    (doesn't sell existing winners just to rebalance — keeps it simple and avoids
    unnecessary taxable/zakatable events). Extend if you want full rebalancing.
    """
    current_value = sum(
        holdings.get(w["ticker"], Decimal("0")) * prices[w["ticker"]]
        for w in watchlist
    )
    total_after = current_value + contribution

    proposal = []
    for w in watchlist:
        target_value = total_after * w["target_weight"]
        current_pos_value = holdings.get(w["ticker"], Decimal("0")) * prices[w["ticker"]]
        buy_value = max(Decimal("0"), target_value - current_pos_value)
        buy_value = min(buy_value, MAX_ORDER_USD)
        if buy_value > Decimal("5"):  # Binance's stated $5 fractional-share minimum
            shares = (buy_value / prices[w["ticker"]]).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            proposal.append({
                "ticker": w["ticker"],
                "usd": buy_value,
                "approx_shares": shares,
            })

    total_proposed = sum(p["usd"] for p in proposal)
    if total_proposed > MAX_TOTAL_USD_PER_RUN:
        raise RuntimeError(
            f"Proposed total ${total_proposed} exceeds MAX_TOTAL_USD_PER_RUN "
            f"(${MAX_TOTAL_USD_PER_RUN}) — aborting run for manual review."
        )
    return proposal


def send_email_message(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())


def poll_for_approval(subject_token, timeout_seconds):
    """
    Polls the IMAP inbox for a reply whose subject contains subject_token
    (a unique run marker) and whose body starts with yes/approve or
    no/reject. Intentionally simple — swap for a proper webhook/mail-parsing
    library if you want something more robust.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(SMTP_USER, SMTP_PASSWORD)
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            for num in data[0].split():
                status, msg_data = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                if subject_token not in (msg.get("Subject") or ""):
                    continue
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="ignore")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore")
                reply = body.strip().lower()
                if reply.startswith(("yes", "approve", "confirm")):
                    return True
                if reply.startswith(("no", "reject", "cancel")):
                    return False
        time.sleep(15)
    return False  # timed out = no action taken, safest default


def place_stock_order(ticker, usd_amount):
    """
    TODO: Confirm the real endpoint/payload shape for Binance's stock product
    before enabling live orders. Do NOT assume this placeholder is correct.
    """
    raise NotImplementedError(
        "Wire this up to the confirmed Binance stocks order endpoint. "
        "Test thoroughly with tiny dollar amounts before trusting it."
    )


def main():
    required = [BINANCE_API_KEY, BINANCE_API_SECRET, SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO, IMAP_HOST]
    if not all(required):
        print("Missing required environment variables/secrets. Aborting.", file=sys.stderr)
        sys.exit(1)

    run_token = f"DCA-{int(time.time())}"
    subject = f"Halal DCA proposal [{run_token}]"

    watchlist = load_watchlist(UNIVERSE_FILE)
    prices = get_current_prices([w["ticker"] for w in watchlist])
    holdings = get_account_holdings()
    proposal = compute_proposal(watchlist, prices, holdings, MONTHLY_CONTRIBUTION_USD)

    if not proposal:
        send_email_message(subject, "Halal DCA bot: nothing to propose this run (all positions at target).")
        return

    lines = [f"{p['ticker']}: ~${p['usd']} (~{p['approx_shares']} shares)" for p in proposal]
    body = (
        "Halal DCA bot — proposed buys this cycle:\n"
        + "\n".join(lines)
        + f"\n\nTotal: ${sum(p['usd'] for p in proposal)}"
        + "\n\nReply to this email starting with 'yes' to approve, 'no' to skip this cycle."
    )
    send_email_message(subject, body)

    if DRY_RUN:
        print("DRY_RUN=true — stopping after proposal, no approval polling, no orders placed.")
        return

    approved = poll_for_approval(run_token, APPROVAL_TIMEOUT_SECONDS)
    if not approved:
        send_email_message(subject, "No approval received in time (or rejected) — skipping this cycle.")
        return

    for p in proposal:
        place_stock_order(p["ticker"], p["usd"])
    send_email_message(subject, "Orders placed. Review your Binance account to confirm fills.")


if __name__ == "__main__":
    main()
