"""
scripts/reconcile_alpaca_orders.py

Backfill terminal status for SUBMITTED Alpaca (equity) orders in trades.jsonl
that never reached FILLED/CANCELLED/REJECTED. Audit 2026-05-28 H-2: 20 equity
SUBMITTED rows have no terminal echo because no Alpaca reconciliation path
existed (the analogous reconcile_crypto_orders.py only covers crypto).

Queries Alpaca's /v2/orders/{id} endpoint for each unresolved order_id and
appends a backfill row to trades.jsonl with the resolved status. Modelled on
reconcile_crypto_orders.py — same CLI surface, same backfill row schema, same
read-only-against-the-broker guarantees.

Run on demand or schedule daily (e.g. tail of run_bot.bat).

Usage:
    python scripts/reconcile_alpaca_orders.py             # last 30 days
    python scripts/reconcile_alpaca_orders.py --days 60   # widen window
    python scripts/reconcile_alpaca_orders.py --dry-run   # show actions, no writes
    python scripts/reconcile_alpaca_orders.py --symbol AAPL  # one ticker

Default window is 30 days (vs 7 for crypto) because the equity orphan backlog
is older — current H-2 finding has the oldest orphan at 20 days.

Safe guards:
  - Read-only against Alpaca (only get_order_by_id / GET /v2/orders/{id})
  - Appends ONE backfill row per terminal order; never modifies existing rows
  - Skips rows whose Alpaca status is still 'new' / 'accepted' / 'pending_*'
  - Tolerates 'order not found' (Alpaca may purge old paper orders) — counted, no row
  - Falls back from alpaca-py SDK to direct REST with verify=False if the SDK
    can't reach the host (Norton AV TLS workaround — matches the dashboard's
    _alpaca_direct() pattern in build_positions_dashboard.py).
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

# Dashboard mirror: push_journal.py copies the bot's journal into the dashboard
# repo and the GitHub-Pages backend reads it from there. The mirror sometimes
# holds orphan SUBMITTED rows the bot's local journal has already lost (after
# a re-clone). The reconciler walks both and writes the backfill row to
# whichever journal(s) the orphan appeared in.
DASH_JOURNAL = (BOT_ROOT.parent.parent / "flowtrader-dashboard" / "journal" / "trades.jsonl")


def _journal_paths() -> list[Path]:
    """Return the list of journal files we should walk. Dashboard mirror
    included only if it exists on disk."""
    paths = [JOURNAL_FILE]
    if DASH_JOURNAL.exists() and DASH_JOURNAL.resolve() != JOURNAL_FILE.resolve():
        paths.append(DASH_JOURNAL)
    return paths

load_dotenv(BOT_ROOT / ".env")
sys.path.insert(0, str(BOT_ROOT))


# ── Order loading ─────────────────────────────────────────────────────────────

def load_submitted_equity_orders(days: int, symbol_filter: str | None
                                  ) -> tuple[list[dict], dict[str, list[Path]]]:
    """Walk every journal in `_journal_paths()`. Return:
      - A deduped list of SUBMITTED equity rows newer than `days` (first
        occurrence wins; canonical order_id).
      - A map `{order_id: [journal_paths]}` telling the caller which journal
        files each orphan was seen in, so backfill rows are written to all of
        them (keeping the mirror in sync).

    Equity rows are those whose symbol contains no '/' (crypto pairs are
    SYM/USDT). Drop session-level placeholders symbol='ALL'/'NONE'.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    submitted: dict[str, dict] = {}        # order_id -> canonical row
    sources:   dict[str, list[Path]] = {}  # order_id -> journals containing it
    for path in _journal_paths():
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sym = (row.get("symbol") or "").upper()
            if "/" in sym:
                continue
            if sym in ("", "ALL", "NONE"):
                continue
            if (row.get("execution_status") or "").upper() != "SUBMITTED":
                continue
            oid = row.get("order_id")
            if not oid:
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
            submitted.setdefault(oid, row)
            sources.setdefault(oid, []).append(path)
    return list(submitted.values()), sources


def already_reconciled_oids() -> set[str]:
    """Order_ids that already have a terminal echo in ANY tracked journal."""
    terminal = {"FILLED", "CANCELLED", "REJECTED", "PARTIAL"}
    done: set[str] = set()
    for path in _journal_paths():
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid = row.get("order_id")
            if not oid:
                continue
            status = (row.get("execution_status") or "").upper()
            # ORDERSTATUS.FILLED is the H-4 enum-leak — recognise it too.
            if status in terminal or status == "ORDERSTATUS.FILLED" or row.get("_backfill"):
                done.add(oid)
    return done


# ── Alpaca clients ────────────────────────────────────────────────────────────

def _trading_client():
    """Return an alpaca-py TradingClient, or None if the SDK can't connect."""
    try:
        from alpaca.trading.client import TradingClient
        key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
        if not key or not secret:
            return None
        return TradingClient(api_key=key, secret_key=secret, paper=paper)
    except Exception as e:
        print(f"  alpaca-py init failed: {e}")
        return None


def _rest_base() -> str:
    paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
    return "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"


def _rest_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def fetch_order_sdk(client, order_id: str) -> dict | None:
    """Returns a normalised dict or {'_error': ...} or None if not found."""
    try:
        o = client.get_order_by_id(order_id)
        return {
            "status":           getattr(o, "status", None),
            "symbol":           getattr(o, "symbol", None),
            "filled_qty":       getattr(o, "filled_qty", None),
            "filled_avg_price": getattr(o, "filled_avg_price", None),
            "qty":              getattr(o, "qty", None),
            "side":             getattr(o, "side", None),
            "submitted_at":     getattr(o, "submitted_at", None),
            "filled_at":        getattr(o, "filled_at", None),
            "canceled_at":      getattr(o, "canceled_at", None),
        }
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "404" in msg or "does not exist" in msg:
            return None
        return {"_error": str(e)}


def fetch_order_rest(order_id: str) -> dict | None:
    """Direct REST fallback — same Norton AV TLS workaround as the dashboard."""
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        r = requests.get(
            f"{_rest_base()}/v2/orders/{order_id}",
            headers=_rest_headers(),
            verify=False,
            timeout=10,
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            return {"_error": f"HTTP {r.status_code}: {r.text[:120]}"}
        d = r.json()
        return {
            "status":           d.get("status"),
            "symbol":           d.get("symbol"),
            "filled_qty":       d.get("filled_qty"),
            "filled_avg_price": d.get("filled_avg_price"),
            "qty":              d.get("qty"),
            "side":             d.get("side"),
            "submitted_at":     d.get("submitted_at"),
            "filled_at":        d.get("filled_at"),
            "canceled_at":      d.get("canceled_at"),
        }
    except Exception as e:
        return {"_error": str(e)}


# ── Status mapping ────────────────────────────────────────────────────────────

# Alpaca order statuses → our canonical execution_status. None = still open.
# (https://docs.alpaca.markets/docs/orders-at-alpaca#order-status)
def _alpaca_to_canonical(alpaca_status) -> str | None:
    if alpaca_status is None:
        return "ERROR"
    s = str(alpaca_status).lower()
    # The alpaca-py SDK returns an OrderStatus enum whose str() is
    # "OrderStatus.FILLED" — strip the prefix.
    if "." in s:
        s = s.split(".", 1)[1]
    if s == "filled":
        return "FILLED"
    if s in ("partially_filled",):
        return "PARTIAL"
    if s in ("canceled", "cancelled", "expired", "done_for_day", "stopped",
             "suspended", "replaced"):
        return "CANCELLED"
    if s == "rejected":
        return "REJECTED"
    # Still in-flight — nothing to backfill yet
    if s in ("new", "accepted", "pending_new", "pending_cancel",
             "pending_replace", "accepted_for_bidding", "calculated", "held"):
        return None
    return "ERROR"


# ── Backfill row ──────────────────────────────────────────────────────────────

def build_backfill_row(orig: dict, order: dict) -> dict | None:
    exec_status = _alpaca_to_canonical(order.get("status"))
    if exec_status is None:
        return None
    est = pytz.timezone("America/New_York")
    now = datetime.now(est)
    filled_qty   = order.get("filled_qty")
    filled_avg   = order.get("filled_avg_price")
    try:
        filled_qty = float(filled_qty) if filled_qty is not None else 0.0
    except (TypeError, ValueError):
        filled_qty = 0.0
    try:
        filled_avg = float(filled_avg) if filled_avg is not None else None
    except (TypeError, ValueError):
        filled_avg = None
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
        "alpaca_status":       str(order.get("status", "")).lower().split(".")[-1],
        "filled_qty":          filled_qty,
        "filled_avg_price":    filled_avg,
        "intended_qty":        orig.get("quantity"),
        "intended_entry":      orig.get("entry_price"),
        "stop_loss":           orig.get("stop_loss"),
        "take_profit":         orig.get("take_profit"),
        "signal_score":        orig.get("signal_score"),
        "signals_fired":       orig.get("signals_fired"),
        "paper_trade":         orig.get("paper_trade", True),
        "note":                "Backfilled by reconcile_alpaca_orders.py — H-2 remediation",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--days",    type=int, default=30, help="Look back N days (default 30)")
    ap.add_argument("--symbol",  type=str, default=None, help="Restrict to one ticker, e.g. AAPL")
    ap.add_argument("--dry-run", action="store_true", help="Print actions; don't write journal")
    args = ap.parse_args()

    print("=" * 60)
    print(f"Alpaca equity reconciliation — last {args.days} day(s)"
          + (f", symbol={args.symbol.upper()}" if args.symbol else ""))
    if args.dry_run:
        print("DRY RUN — no journal rows will be written")
    print("=" * 60)

    submitted, sources = load_submitted_equity_orders(
        args.days, args.symbol.upper() if args.symbol else None
    )
    print(f"Walked {len(_journal_paths())} journal file(s): "
          + ", ".join(str(p.relative_to(BOT_ROOT.parent.parent))
                      if p.is_relative_to(BOT_ROOT.parent.parent) else str(p)
                      for p in _journal_paths()))
    if not submitted:
        print("No unresolved SUBMITTED equity orders found in window. Nothing to do.")
        return 0

    # Drop any that were already reconciled out-of-band (e.g. by
    # reconcile_meta_orders.py) — we don't want to double-append.
    resolved = already_reconciled_oids()
    pre = len(submitted)
    submitted = [r for r in submitted if r["order_id"] not in resolved]
    if pre != len(submitted):
        print(f"({pre - len(submitted)} order_id(s) already have a terminal echo — skipping)")

    if not submitted:
        print("All orders in window already reconciled. Nothing to do.")
        return 0
    print(f"Found {len(submitted)} unresolved SUBMITTED order(s).\n")

    client = _trading_client()
    fetch  = lambda oid: (fetch_order_sdk(client, oid) if client is not None
                          else fetch_order_rest(oid))
    print(f"Client: {'alpaca-py SDK' if client is not None else 'direct REST (SDK unavailable)'}\n")

    backfilled = filled_n = cancelled_n = rejected_n = partial_n = 0
    still_open = not_found = errored = 0

    for rec in submitted:
        oid = rec["order_id"]
        sym = rec["symbol"]
        print(f"  {oid}  {sym} {rec.get('action')} qty={rec.get('quantity')} "
              f"@ {rec.get('entry_price')}  ({rec.get('session_id')})")

        order = fetch(oid)
        if order is None:
            print("    NOT FOUND on Alpaca (paper history may have aged out) — skipping")
            not_found += 1
            continue
        # SDK failed but we have credentials — try REST as a one-off fallback
        if isinstance(order, dict) and "_error" in order and client is not None:
            print(f"    SDK error ({order['_error'][:80]}) — retrying via REST")
            order = fetch_order_rest(oid)
            if order is None:
                print("    NOT FOUND via REST — skipping")
                not_found += 1
                continue
        if isinstance(order, dict) and "_error" in order:
            print(f"    ERROR fetching: {order['_error']} — skipping")
            errored += 1
            continue

        row = build_backfill_row(rec, order)
        if row is None:
            print(f"    still open (alpaca={order.get('status')}) — no backfill")
            still_open += 1
            continue

        print(f"    {row['execution_status']} "
              f"filled={row['filled_qty']} avg={row['filled_avg_price']}")
        # Write to every journal that contained the original SUBMITTED row, so
        # the bot and dashboard mirrors stay in sync. Crypto reconciler only
        # writes to the bot's journal, but it's run from CI where the bot
        # journal is the one push_journal.py syncs upstream. For Alpaca orphans
        # we know they currently live in the dashboard mirror only, so writing
        # only to the bot would leave the dashboard's history broken.
        targets = sources.get(rec["order_id"], [JOURNAL_FILE])
        if not args.dry_run:
            for tgt in targets:
                with open(tgt, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            print(f"    wrote to {len(targets)} journal(s)")
        backfilled += 1
        if   row["execution_status"] == "FILLED":    filled_n    += 1
        elif row["execution_status"] == "CANCELLED": cancelled_n += 1
        elif row["execution_status"] == "REJECTED":  rejected_n  += 1
        elif row["execution_status"] == "PARTIAL":   partial_n   += 1

    print()
    print("=" * 60)
    print(f"Done — {backfilled} backfilled "
          f"(FILLED={filled_n} CANCELLED={cancelled_n} REJECTED={rejected_n} PARTIAL={partial_n}), "
          f"{still_open} still open, {not_found} not found, {errored} errored.")
    if args.dry_run:
        print("DRY RUN — no journal rows were written.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
