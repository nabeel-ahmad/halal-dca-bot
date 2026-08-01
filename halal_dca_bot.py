"""
Halal DCA / Rebalance Bot — PROPOSE-THEN-APPROVE, not fully autonomous.

What this does:
  1. Reads your halal watchlist (halal_universe.csv) — a pre-screened list YOU maintain
     (from a shariah index or an app like Zoya/Islamicly), intersected with whatever
     Binance actually lets you trade.
  2. Pulls current prices + your account balances from Binance.
  3. Works out a simple DCA / rebalance proposal (target-weight based).
  4. Sends the proposal to you via Telegram and WAITS for your explicit approval
     before placing any order.
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
import json
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN

import requests

# ---------------------------------------------------------------------------
# Config — pull secrets from environment only. Never hardcode.
# ---------------------------------------------------------------------------
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def poll_for_approval(prompt_message_id, timeout_seconds):
    """
    Polls Telegram getUpdates for a reply of 'yes'/'approve' after the proposal
    message. This is intentionally simple — swap for a webhook if you want
    something more robust.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    deadline = time.time() + timeout_seconds
    last_update_id = None
    while time.time() < deadline:
        params = {"timeout": 20}
        if last_update_id:
            params["offset"] = last_update_id + 1
        resp = requests.get(url, params=params, timeout=25).json()
        for update in resp.get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {}).get("text", "").strip().lower()
            if msg in ("yes", "approve", "confirm"):
                return True
            if msg in ("no", "reject", "cancel"):
                return False
        time.sleep(3)
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
    if not all([BINANCE_API_KEY, BINANCE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print("Missing required environment variables/secrets. Aborting.", file=sys.stderr)
        sys.exit(1)

    watchlist = load_watchlist(UNIVERSE_FILE)
    prices = get_current_prices([w["ticker"] for w in watchlist])
    holdings = get_account_holdings()
    proposal = compute_proposal(watchlist, prices, holdings, MONTHLY_CONTRIBUTION_USD)

    if not proposal:
        send_telegram_message("Halal DCA bot: nothing to propose this run (all positions at target).")
        return

    lines = [f"{p['ticker']}: ~${p['usd']} (~{p['approx_shares']} shares)" for p in proposal]
    message = (
        "Halal DCA bot — proposed buys this cycle:\n"
        + "\n".join(lines)
        + f"\n\nTotal: ${sum(p['usd'] for p in proposal)}"
        + "\n\nReply 'yes' to approve, 'no' to skip this cycle."
    )
    send_telegram_message(message)

    if DRY_RUN:
        print("DRY_RUN=true — stopping after proposal, no approval polling, no orders placed.")
        return

    approved = poll_for_approval(None, APPROVAL_TIMEOUT_SECONDS)
    if not approved:
        send_telegram_message("No approval received in time (or rejected) — skipping this cycle.")
        return

    for p in proposal:
        place_stock_order(p["ticker"], p["usd"])
    send_telegram_message("Orders placed. Review your Binance account to confirm fills.")


if __name__ == "__main__":
    main()
