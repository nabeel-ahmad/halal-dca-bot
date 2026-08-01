"""One-off diagnostic: check USDT balance on the account tied to the API key.
Not part of the bot's runtime pipeline — run manually, then remove."""

import hashlib
import hmac
import os
import time

import requests

BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
BASE = "https://api.binance.com"

params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
query = "&".join(f"{k}={v}" for k, v in params.items())
signature = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

resp = requests.get(
    f"{BASE}/api/v3/account?{query}&signature={signature}",
    headers={"X-MBX-APIKEY": BINANCE_API_KEY},
    timeout=15,
)
resp.raise_for_status()
balances = resp.json().get("balances", [])
usdt = next((b for b in balances if b["asset"] == "USDT"), None)
if usdt:
    print(f"USDT: free={usdt['free']} locked={usdt['locked']}")
else:
    print("USDT: no balance entry (0)")
