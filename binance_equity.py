"""
Shared Binance Stocks Trading API client + email sender, used by
weekly_portfolio_review.py.

SECURITY: never hardcode API keys/tokens here. Read them from environment
variables only (set as encrypted secrets in your CI, e.g. GitHub Actions
secrets). Never log the key/secret/token values.

Implemented against Binance's Stocks Trading REST API (`/sapi/v1/equity/*`,
launched 2026-07), per
https://developers.binance.com/en/docs/catalog/advanced-trading-stocks-trading/api/rest-api.
There is no dedicated positions/balance endpoint yet — get_account_holdings()
derives net share counts from summed trade history instead. Re-verify against
current docs before trusting this with real money; Binance may add a direct
positions endpoint later.
"""

import hashlib
import hmac
import os
import smtplib
import time
from decimal import Decimal
from email.mime.text import MIMEText

import requests

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

BINANCE_BASE_URL = "https://api.binance.com"

# Binance Stocks Trading (launched 2026-07) has no dedicated positions/balance
# endpoint — holdings are derived by summing signed trade history from this
# cutoff forward. Set well before your first real trade.
HOLDINGS_HISTORY_START_MS = int(os.environ.get("HOLDINGS_HISTORY_START_MS", "1735689600000"))  # 2025-01-01T00:00:00Z


def _binance_signed_request(method, path, params):
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 5000)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    signature = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_BASE_URL}{path}?{query}&signature={signature}"
    resp = requests.request(method, url, headers={"X-MBX-APIKEY": BINANCE_API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_current_prices(tickers):
    """Latest bid/ask quote per ticker (MARKET_DATA, API key only, no signature)."""
    prices = {}
    for ticker in tickers:
        resp = requests.get(
            f"{BINANCE_BASE_URL}/sapi/v1/equity/market/quote",
            headers={"X-MBX-APIKEY": BINANCE_API_KEY},
            params={"symbol": ticker},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            raise RuntimeError(f"No quote available for {ticker}")
        prices[ticker] = (Decimal(data["bidPrice"]) + Decimal(data["askPrice"])) / 2
    return prices


def get_account_holdings():
    """
    Binance's Stocks Trading API has no dedicated positions/balance endpoint
    (as of the 2026-07 launch) — holdings are derived by summing signed BUY/SELL
    quantities from paginated trade history. Revisit this if Binance adds a
    direct positions endpoint later.
    """
    holdings = {}
    page = 1
    now_ms = int(time.time() * 1000)
    while True:
        data = _binance_signed_request("GET", "/sapi/v1/equity/trade/history", {
            "startTime": HOLDINGS_HISTORY_START_MS,
            "endTime": now_ms,
            "current": page,
            "size": 100,
        })
        rows = data.get("rows", [])
        for row in rows:
            qty = Decimal(row["qty"])
            delta = qty if row["side"] == "BUY" else -qty
            holdings[row["symbol"]] = holdings.get(row["symbol"], Decimal("0")) + delta
        if not rows or page * data.get("size", 100) >= data.get("total", 0):
            break
        page += 1
    return holdings


def send_email_message(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
