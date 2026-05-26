"""
scripts/liquidate_binance_testnet.py

Sells every non-USDT coin on Binance testnet back to USDT so you start
from a clean $10,000 USDT baseline with no seeded junk positions.

Run once from trading-bot/trading-bot/:
    python scripts/liquidate_binance_testnet.py

Safe guards:
  - Only runs when BINANCE_TESTNET=true (refuses to touch live accounts)
  - Skips coins below Binance's minimum notional (~$10) — cannot be sold
  - Prints a dry-run preview first, then asks for confirmation
"""

import sys, os, time
from pathlib import Path
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Norton TLS fix
class _A(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        ctx = create_urllib3_context()
        ctx.load_default_certs()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kw["ssl_context"] = ctx
        super().init_poolmanager(*a, **kw)

_orig = requests.Session.__init__
def _p(self, *a, **kw):
    _orig(self, *a, **kw)
    self.mount("https://", _A())
requests.Session.__init__ = _p

# Safety gate — refuse to run on live
testnet = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
if not testnet:
    print("ERROR: BINANCE_TESTNET is not 'true'. This script only runs on testnet.")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.crypto_fetcher import BinanceFetcher

f = BinanceFetcher()
if not f.exchange:
    print("ERROR: Binance not connected. Check BINANCE_API_KEY / BINANCE_SECRET_KEY in .env")
    sys.exit(1)

print("Fetching testnet balances...")
raw = f.exchange.fetch_balance()

# Collect everything that isn't USDT
to_sell = []
for coin, data in (raw.get("total") or {}).items():
    if coin == "USDT":
        continue
    amount = float(data or 0)
    if amount <= 1e-6:
        continue
    # Skip obvious non-tradeable testnet tokens
    if not (coin.isascii() and coin.isalnum() and not coin.isdigit()):
        continue
    symbol = f"{coin}/USDT"
    try:
        ticker = f.exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or 0)
    except Exception:
        print(f"  {coin}: no USDT pair on testnet — skipping")
        continue
    value = amount * price
    if value < 10:
        print(f"  {coin}: {amount:.6f} = ~${value:.2f} — below min notional, skipping")
        continue
    to_sell.append({"coin": coin, "symbol": symbol, "amount": amount, "price": price, "value": value})

if not to_sell:
    print("Nothing to liquidate — account is already clean.")
    usdt = float((raw.get("USDT") or {}).get("free", 0) or 0)
    print(f"Free USDT: ${usdt:,.2f}")
    sys.exit(0)

print()
print("Positions to liquidate:")
total_value = 0
for s in to_sell:
    print(f"  SELL {s['amount']:.6f} {s['coin']} (~${s['value']:,.2f} @ ${s['price']:,.2f})")
    total_value += s["value"]
usdt_before = float((raw.get("USDT") or {}).get("free", 0) or 0)
print(f"\n  Current USDT:    ${usdt_before:,.2f}")
print(f"  Expected gain:   ~${total_value:,.2f}")
print(f"  Expected total:  ~${usdt_before + total_value:,.2f}")
print()

answer = input("Proceed with liquidation? [y/N]: ").strip().lower()
if answer != "y":
    print("Aborted.")
    sys.exit(0)

print()
errors = []
for s in to_sell:
    try:
        # Use amount_to_precision to respect Binance lot-size rules
        qty = float(f.exchange.amount_to_precision(s["symbol"], s["amount"]))
        if qty <= 0:
            print(f"  {s['coin']}: precision rounded to 0 — skipping")
            continue
        order = f.exchange.create_market_sell_order(s["symbol"], qty)
        fill = float(order.get("average") or order.get("price") or s["price"])
        received = qty * fill
        print(f"  SOLD {qty} {s['coin']} @ ${fill:,.4f} = ${received:,.2f} USDT  [order {order.get('id')}]")
        time.sleep(0.3)  # Respect testnet rate limits
    except Exception as e:
        print(f"  ERROR selling {s['coin']}: {e}")
        errors.append(s["coin"])

print()
print("Fetching final balance...")
time.sleep(1)
final = f.exchange.fetch_balance()
usdt_after = float((final.get("USDT") or {}).get("free", 0) or 0)
print(f"Final free USDT: ${usdt_after:,.2f}")
if errors:
    print(f"Failed coins (manual action needed): {', '.join(errors)}")
else:
    print("All positions liquidated successfully.")
