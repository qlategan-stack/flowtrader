"""
scripts/reconcile_meta_orders.py

One-shot reconciliation of the 3 SUBMITTED META BUY orders from 2026-05-06/07
against Alpaca paper trading.

Run from the trading-bot/trading-bot/ directory:
    python scripts/reconcile_meta_orders.py

Outputs:
  - Console: terminal status of each order + current META position
  - Appends backfill rows to journal/trades.jsonl for any order that reached
    a terminal state (filled / cancelled / expired / replaced)
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Force requests/urllib3 to use the freshly-upgraded certifi bundle BEFORE
# any SSL connection is made.  On Windows Python 3.13, the OS cert store is
# missing intermediate CAs (C-2 in audit 2026-05-20).
# Must be set before any import that creates a requests.Session.
import certifi
os.environ["SSL_CERT_FILE"]      = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from dotenv import load_dotenv

_BOT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_BOT_ROOT / ".env")

# ── The 3 SUBMITTED orders to reconcile ─────────────────────────────────────

SUBMITTED_ORDERS = [
    {
        "order_id":   "1c3be739-f965-4f22-89d3-abcbf1f90f6c",
        "session_id": "20260506_1437",
        "symbol":     "META",
        "side":       "BUY",
        "quantity":   16,
        "entry_price": 614.25,
        "stop_loss":   604.45,
        "take_profit": 650.52,
    },
    {
        "order_id":   "f5b6361e-c83c-4026-9522-1daae172824d",
        "session_id": "20260507_1245",
        "symbol":     "META",
        "side":       "BUY",
        "quantity":   1,
        "entry_price": 618.16,
        "stop_loss":   608.62,
        "take_profit": 649.93,
    },
    {
        "order_id":   "9eeae6fa-b2eb-4999-8832-f2f5a5bff2e2",
        "session_id": "20260507_1331",
        "symbol":     "META",
        "side":       "BUY",
        "quantity":   1,
        "entry_price": 614.185,
        "stop_loss":   604.64,
        "take_profit": 649.73,
    },
]

JOURNAL_FILE = _BOT_ROOT / "journal" / "trades.jsonl"

# Terminal order statuses that warrant a backfill journal row
TERMINAL_STATUSES = {"filled", "partially_filled", "cancelled", "expired", "replaced", "rejected"}


def _patch_session_ssl(session):
    """
    Replace the HTTPAdapter on a requests.Session with one that uses a relaxed
    SSL context.  Required on this host because Norton Antivirus TLS inspection
    produces a certificate chain whose Basic Constraints extension is not marked
    critical — Python 3.13 rejects it under strict RFC 5280 enforcement.
    Disabling VERIFY_X509_STRICT keeps full verification while tolerating that
    specific non-conformance.
    """
    import ssl
    from requests.adapters import HTTPAdapter
    from urllib3.util.ssl_ import create_urllib3_context

    class _RelaxedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = create_urllib3_context()
            ctx.load_default_certs()
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            kwargs["ssl_context"] = ctx
            super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, proxy, **proxy_kwargs):
            ctx = create_urllib3_context()
            ctx.load_default_certs()
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            proxy_kwargs["ssl_context"] = ctx
            return super().proxy_manager_for(proxy, **proxy_kwargs)

    adapter = _RelaxedAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def get_alpaca_client():
    key    = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    paper  = os.getenv("PAPER_TRADING", "true").lower() == "true"

    if not key or not secret:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key=key, secret_key=secret, paper=paper)

    # Norton Antivirus TLS inspection re-signs certificates with its own CA,
    # whose Basic Constraints extension is not marked critical.  Python 3.13
    # introduced strict RFC 5280 enforcement that rejects such chains.
    # Patching the HTTPAdapter to relax VERIFY_X509_STRICT is the minimal fix —
    # it keeps full cert verification, just tolerates the non-critical flag.
    _patch_session_ssl(client._session)

    print(f"Alpaca connected (paper={paper})\n")
    return client


def fetch_order(client, order_id: str):
    """Return the Alpaca order object, or None on 404."""
    try:
        return client.get_order_by_id(order_id)
    except Exception as e:
        if "not found" in str(e).lower() or "404" in str(e):
            return None
        raise


def fetch_position(client, symbol: str):
    """Return the Alpaca position object for symbol, or None if flat."""
    try:
        return client.get_open_position(symbol)
    except Exception as e:
        if "not found" in str(e).lower() or "position does not exist" in str(e).lower() or "404" in str(e):
            return None
        raise


def backfill_journal(order_rec: dict, order, position) -> dict:
    """Build and append a backfill journal row for a terminal order."""
    import pytz
    est = pytz.timezone("America/New_York")
    now = datetime.now(est)

    raw_status = order.status
    status = raw_status.value if hasattr(raw_status, "value") else str(raw_status).lower()
    if "." in status:
        status = status.split(".")[-1]

    filled_qty = float(order.filled_qty or 0)
    filled_avg = float(order.filled_avg_price or 0) if order.filled_avg_price else None
    cancelled_at = str(order.canceled_at) if getattr(order, "canceled_at", None) else None
    filled_at = str(order.filled_at) if getattr(order, "filled_at", None) else None

    # Map Alpaca status -> execution_status vocabulary used in the journal
    if status == "filled":
        exec_status = "FILLED"
    elif status == "partially_filled":
        exec_status = "PARTIALLY_FILLED"
    elif status in ("cancelled", "canceled"):
        exec_status = "CANCELLED"
    elif status == "expired":
        exec_status = "EXPIRED"
    elif status == "replaced":
        exec_status = "REPLACED"
    elif status == "rejected":
        exec_status = "REJECTED_BROKER"
    else:
        exec_status = status.upper()

    # Current position size for this symbol (0 if flat)
    current_qty = float(position.qty) if position else 0.0
    current_value = float(position.market_value) if position else 0.0
    unrealized_pl = float(position.unrealized_pl) if position else None

    entry = {
        # Backfill marker — links to the original SUBMITTED row
        "_backfill": True,
        "_backfill_ts": now.isoformat(),
        "_original_session": order_rec["session_id"],

        "session_id":  order_rec["session_id"],
        "order_id":    order_rec["order_id"],
        "symbol":      order_rec["symbol"],
        "action":      order_rec["side"],
        "execution_status": exec_status,

        # Alpaca-reported fill details
        "alpaca_status":      status,
        "filled_qty":         filled_qty,
        "filled_avg_price":   filled_avg,
        "intended_qty":       order_rec["quantity"],
        "intended_entry":     order_rec["entry_price"],
        "stop_loss":          order_rec["stop_loss"],
        "take_profit":        order_rec["take_profit"],
        "filled_at":          filled_at,
        "cancelled_at":       cancelled_at,

        # Current position snapshot
        "position_qty_now":     current_qty,
        "position_value_now":   current_value,
        "unrealized_pl_now":    unrealized_pl,

        "paper_trade": True,
        "timestamp":   now.isoformat(),
        "note": "Backfilled by reconcile_meta_orders.py — C-4 remediation",
    }

    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return entry


def main():
    client = get_alpaca_client()

    print("=" * 60)
    print("META position on Alpaca paper right now:")
    print("=" * 60)
    position = fetch_position(client, "META")
    if position:
        print(f"  Qty held:       {position.qty}")
        print(f"  Avg entry:      {position.avg_entry_price}")
        print(f"  Market value:   {position.market_value}")
        print(f"  Unrealized P&L: {position.unrealized_pl}")
        print(f"  Current price:  {position.current_price}")
    else:
        print("  No open META position — account is flat on META")
    print()

    print("=" * 60)
    print("Order-by-order reconciliation:")
    print("=" * 60)

    backfilled = 0
    for rec in SUBMITTED_ORDERS:
        oid = rec["order_id"]
        print(f"\n  Order {oid}")
        print(f"  Session {rec['session_id']} | {rec['side']} {rec['quantity']} META @ ~${rec['entry_price']}")

        order = fetch_order(client, oid)
        if order is None:
            print("  STATUS: NOT FOUND on Alpaca — order ID does not exist in paper account")
            print("  -> Likely the bot was connected to a different paper account, or the")
            print("    order was purged (Alpaca retains paper orders for ~30 days).")
            continue

        # Alpaca SDK returns an OrderStatus enum; normalise to lowercase string
        raw_status = order.status
        status = raw_status.value if hasattr(raw_status, "value") else str(raw_status).lower()
        # Strip "orderstatus." prefix if present (e.g. "orderstatus.filled" -> "filled")
        if "." in status:
            status = status.split(".")[-1]

        filled_qty = order.filled_qty or 0
        filled_avg = order.filled_avg_price

        print(f"  STATUS:       {status}")
        print(f"  Filled qty:   {filled_qty}")
        print(f"  Filled avg:   {filled_avg}")
        print(f"  Submitted at: {order.submitted_at}")
        print(f"  Filled at:    {getattr(order, 'filled_at', 'N/A')}")
        print(f"  Cancelled at: {getattr(order, 'canceled_at', 'N/A')}")

        if status in TERMINAL_STATUSES:
            entry = backfill_journal(rec, order, position)
            print(f"  -> Backfill row appended to trades.jsonl (exec_status={entry['execution_status']})")
            backfilled += 1
        else:
            print(f"  -> Order is still OPEN ({status}) - no backfill row written")

    print()
    print("=" * 60)
    print(f"Done. {backfilled} backfill row(s) appended to journal/trades.jsonl.")
    print()
    print("Next steps:")
    print("  1. Review the backfill rows above.")
    print("  2. If any order was FILLED - confirm the current position matches")
    print("     what the bot expects in account['positions'].")
    print("  3. If META is still held and you want to close it, run:")
    print("     python scripts/reduce_position.py  (or use Alpaca UI directly).")
    print("  4. executor.py has been patched to poll for terminal state before")
    print("     writing to the journal (C-4 fix applied 2026-05-20).")
    print("=" * 60)


if __name__ == "__main__":
    main()
