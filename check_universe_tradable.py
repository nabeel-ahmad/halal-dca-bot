"""One-off diagnostic: confirm watchlist tickers are tradable/tokenized on Binance Stocks.
Not part of the bot's runtime pipeline — run manually, then remove."""

import os
import sys

import requests

BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
BASE = "https://api.binance.com"

tickers = [t.strip() for t in sys.argv[1].split(",")]

resp = requests.get(
    f"{BASE}/sapi/v1/equity/market/tokenized-assets",
    headers={"X-MBX-APIKEY": BINANCE_API_KEY},
    timeout=15,
)
resp.raise_for_status()
tokenized = {a["underlyingEquitySymbol"] for a in resp.json()}

for t in tickers:
    resp = requests.get(
        f"{BASE}/sapi/v1/equity/market/exchangeInfo",
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        params={"symbol": t},
        timeout=15,
    )
    resp.raise_for_status()
    symbols = resp.json().get("symbols", [])
    tradability = symbols[0]["tradability"] if symbols else "NOT_LISTED"
    print(f"{t}: tradability={tradability} tokenized={t in tokenized}")
