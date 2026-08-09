"""TEMP diagnostic: dump equity holdings (from trade history) and non-zero
spot balances, so we know what a weekly portfolio-review routine should
actually screen. Deleted after use."""
import os
from decimal import Decimal

from halal_dca_bot import get_account_holdings, _binance_signed_request

print("=== Equity holdings (derived from /sapi/v1/equity/trade/history) ===")
holdings = get_account_holdings()
if not holdings:
    print("(none)")
for symbol, qty in holdings.items():
    if qty != 0:
        print(f"{symbol}: {qty}")

print()
print("=== Spot account non-zero balances (/api/v3/account) ===")
data = _binance_signed_request("GET", "/api/v3/account", {})
for bal in data.get("balances", []):
    free = Decimal(bal["free"])
    locked = Decimal(bal["locked"])
    if free != 0 or locked != 0:
        print(f"{bal['asset']}: free={free} locked={locked}")
