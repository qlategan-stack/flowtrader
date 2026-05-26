"""
scripts/reconcile_crypto_orders.py

Backfill terminal status for SUBMITTED crypto orders in trades.jsonl that
never reached FILLED/CANCELLED/REJECTED. Audit 2026-05-26 H-1: 97 crypto
SUBMITTED rows have no filled_at, cancelled_at, or filled_avg_price because
the executor's 10s post-submit wait was too short for testnet round-trips.

Queries the exchange (Bybit or Binance via CCXT, same selection logic as
the bot) for each unresolved order_id and appends a backfill row.

Run on demand or schedule daily.

Usage:
    python scripts/reconcile_crypto_orders.py            # last 7 days, live
    python scripts/reconcile_crypto_orders.py --days 30  # widen window
    python scripts/reconcile_crypto_orders.py --dry-run  # show actions, no writes
    python scripts/reconcile_crypto_orders.py --symbol NEAR/USDT   # one pair

Safe guards:
  - Read-only against the exchange (only fetch_order)
  - Appends ONE backfill row per terminal order; never modifies existing rows
  - Skips rows still in 'open' status on the exchange
  - Tolerates 'order not found' (testnet purges old orders) — counted, no row
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
from dotenv import load_dotenv

BOT_ROOT     = Path(__file__).resolve().parent.parent
JOURNAL_FILE = BOT_ROOT / "journal" / "trades.jsonl"

load_dotenv(BOT_ROOT / ".env")
sys.path.insert(0, str(BOT_ROOT))

# Reuse the bot's exchange-selection logic so this script always queries the
# same venue the bot used to place the original order.
from agents.executor import _get_crypto_client  # noqa: E402


def load_submitted_crypto_orders(days: int, symbol_filter: str | None) -> list[dict]:
    """Return SUBMITTED crypto rows from trades.jsonl newer than `days`."""
    if not JOURNAL_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    submitted: list[dict] = []
    for line in JOURNAL_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        sym = row.get("symbol") or ""
        if "/" not in sym:  # equities have no '/'
            continue
        if symbol_filter and sym != symbol_filter:
            continue
        if (row.get("execution_status") or "").upper() != "SUBMITTED":
            continue
        if not row.get("order_id"):
            continue
        ts_str = row.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = pytz.utc.localize(ts)
            if ts < cutoff:
                continue
        except Exception:
            continue
        submitted.append(row)
    return submitted


def _get_exchange(client):
    """Bybit uses .exchange_priv; Binance uses .exchange — return whichever is live."""
    return getattr(client, "exchange_priv", None) or getattr(client, "exchange", None)


def fetch_order_status(exchange, order_id: str, symbol: str) -> dict | None:
    """Return CCXT order dict, None if 'not found', {'_error': ...} on other error."""
    try:
        return exchange.fetch_order(order_id, symbol)
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "doesn't exist" in msg or "no such order" in msg or "404" in msg:
            return None
        return {"_error": str(e)}


def _ccxt_to_canonical(ccxt_status: str) -> str | None:
    """Map CCXT order status to our canonical execution_status. None = still open."""
    s = (ccxt_status or "").lower()
    if s in ("closed",):           # CCXT: fully filled
        return "FILLED"
    if s in ("canceled", "cancelled", "expired"):
        return "CANCELLED"
    if s in ("rejected",):
        return "REJECTED"
    if s in ("open",):              # still live; nothing to backfill yet
        return None
    return "ERROR"                  # unknown status — visible but non-canonical


def build_backfill_row(orig: dict, order: dict) -> dict | None:
    """Construct the journal row, or None if order is still open."""
    exec_status = _ccxt_to_canonical(str(order.get("status", "")))
    if exec_status is None:
        return None

    est = pytz.timezone("America/New_York")
    now = datetime.now(est)
    filled    = float(order.get("filled")   or 0)
    average   = order.get("average")
    return {
        "_backfill":           True,
        "_backfill_ts":        now.isoformat(),
        "_original_session":   orig.get("session_id"),
        "timestamp":           now.isoformat(),
        "date":                now.strftime("%Y-%m-%d"),
        "time_est":            now.strftime("%H:%M:%S"),
        "session_id":          orig.get("session_id"),
        "symbol":              orig.get("symbol"),
        "action":              orig.get("action"),
        "order_id":            orig.get("order_id"),
        "execution_status":    exec_status,
        "ccxt_status":         str(order.get("status", "")).lower(),
        "filled_qty":          filled,
        "filled_avg_price":    float(average) if average else None,
        "intended_qty":        orig.get("quantity"),
        "intended_entry":      orig.get("entry_price"),
        "stop_loss":           orig.get("stop_loss"),
        "take_profit":         orig.get("take_profit"),
        "paper_trade":         True,
        "note":                "Backfilled by reconcile_crypto_orders.py — H-1 remediation",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--days",    type=int, default=7, help="Look back N days (default 7)")
    ap.add_argument("--symbol",  type=str, default=None, help="Restrict to one pair, e.g. NEAR/USDT")
    ap.add_argument("--dry-run", action="store_true", help="Print actions; don't write journal")
    args = ap.parse_args()

    print("=" * 60)
    print(f"Crypto reconciliation — last {args.days} day(s)"
          + (f", symbol={args.symbol}" if args.symbol else ""))
    if args.dry_run:
        print("DRY RUN — no journal rows will be written")
    print("=" * 60)

    submitted = load_submitted_crypto_orders(args.days, args.symbol)
    if not submitted:
        print("No unresolved SUBMITTED crypto orders found. Nothing to do.")
        return 0
    print(f"Found {len(submitted)} unresolved SUBMITTED order(s).\n")

    client   = _get_crypto_client()
    exchange = _get_exchange(client)
    if exchange is None:
        print("ERROR: no live CCXT exchange handle (BINANCE_API_KEY / BYBIT keys not set?)",
              file=sys.stderr)
        return 1
    print(f"Exchange: {type(client).__name__}\n")

    backfilled = filled_n = cancelled_n = still_open = not_found = errored = 0

    for rec in submitted:
        oid = rec["order_id"]
        sym = rec["symbol"]
        print(f"  {oid}  {sym} {rec.get('action')} qty={rec.get('quantity')} "
              f"@ {rec.get('entry_price')}  ({rec.get('session_id')})")

        order = fetch_order_status(exchange, oid, sym)
        if order is None:
            print("    NOT FOUND on exchange (testnet may purge old orders) — skipping")
            not_found += 1
            continue
        if "_error" in order:
            print(f"    ERROR fetching: {order['_error']} — skipping")
            errored += 1
            continue

        row = build_backfill_row(rec, order)
        if row is None:
            print(f"    still open (ccxt={order.get('status')}) — no backfill")
            still_open += 1
            continue

        print(f"    {row['execution_status']} "
              f"filled={row['filled_qty']} avg={row['filled_avg_price']}")
        if not args.dry_run:
            with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        backfilled += 1
        if row["execution_status"] == "FILLED":
            filled_n += 1
        elif row["execution_status"] == "CANCELLED":
            cancelled_n += 1

    print()
    print("=" * 60)
    print(f"Done — {backfilled} backfilled (FILLED={filled_n} CANCELLED={cancelled_n}), "
          f"{still_open} still open, {not_found} not found, {errored} errored.")
    if args.dry_run:
        print("DRY RUN — no journal rows were written.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
