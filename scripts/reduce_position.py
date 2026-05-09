"""
One-shot tool: reduce an existing Alpaca equity position to a target quantity
and replace its protective stop on the remainder.

Usage:
    python scripts/reduce_position.py SYMBOL TARGET_QTY [STOP_PRICE]
    python scripts/reduce_position.py META 16 597.44
    python scripts/reduce_position.py META 0           # full close, no new stop

Sequence:
  1. Read current position. Bail if symbol not held, or already at/below target.
  2. Cancel any existing SELL orders for the symbol — the old stop covers a
     larger quantity than will remain after the sell. Leaving it in place
     would risk Alpaca shorting the account if the stop trigger fires while
     the qty is mid-adjustment.
  3. Submit a market SELL for (current - target) shares, TimeInForce.DAY.
     Fills immediately during regular hours; queues for the next open if
     submitted while the market is closed.
  4. If TARGET_QTY > 0 and STOP_PRICE given, submit a fresh GTC stop-market
     SELL for TARGET_QTY at STOP_PRICE.

Brief window between cancel and new-stop where the remainder is unprotected.
On a paper account in normal market conditions, the window is sub-second.
For live trading, prefer Alpaca's `close_position(qty=...)` API which handles
the stop adjustment internally.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

from dotenv import load_dotenv
load_dotenv(_HERE.parent.parent / ".env")

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopOrderRequest,
)


def main(symbol: str, target_qty: float, stop_price: float | None) -> int:
    paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
    client = TradingClient(
        api_key=os.getenv("ALPACA_API_KEY"),
        secret_key=os.getenv("ALPACA_SECRET_KEY"),
        paper=paper,
    )

    print(f"=== Position check — {symbol} ===")
    try:
        position = client.get_open_position(symbol)
    except Exception as e:
        print(f"  No open position for {symbol} ({e}). Nothing to reduce.")
        return 1

    current_qty = float(position.qty)
    avg_entry   = float(position.avg_entry_price)
    current_px  = float(position.current_price)
    print(f"  current_qty={current_qty}  avg_entry=${avg_entry:.2f}  current=${current_px:.2f}")
    print(f"  target_qty={target_qty}")

    if current_qty <= target_qty:
        print(f"  Position is already at/below target ({current_qty} <= {target_qty}). Nothing to do.")
        return 0

    sell_qty = current_qty - target_qty
    print(f"  -> sell {sell_qty} shares to reach target")

    # Step 2: cancel existing SELL orders so the old stop can't accidentally short us
    print()
    print(f"=== Cancelling existing SELL orders on {symbol} ===")
    open_orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=50)
    )
    cancelled = 0
    for o in open_orders:
        if str(o.side).split(".")[-1].upper() == "SELL":
            try:
                client.cancel_order_by_id(o.id)
                cancelled += 1
                print(f"  cancelled {o.id} ({o.order_type} qty={o.qty} stop={o.stop_price})")
            except Exception as e:
                print(f"  WARNING: could not cancel {o.id}: {e}")
    if cancelled == 0:
        print("  (no existing SELL orders to cancel)")

    # Step 3: market sell the excess
    print()
    print(f"=== Market SELL {sell_qty} {symbol} ===")
    sell_req = MarketOrderRequest(
        symbol=symbol,
        qty=sell_qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    sell_order = client.submit_order(order_data=sell_req)
    print(f"  order_id={sell_order.id}  status={sell_order.status}  qty={sell_order.qty}")
    print(f"  (market closed -> queued for next open; market open -> fills shortly)")

    # Step 4: fresh stop on the remainder
    if target_qty > 0 and stop_price is not None:
        print()
        print(f"=== Placing GTC stop-market SELL for remaining {target_qty} @ ${stop_price:.2f} ===")
        if stop_price >= current_px:
            print(f"  REFUSED: stop ${stop_price:.2f} is at or above current ${current_px:.2f}.")
            return 2

        stop_req = StopOrderRequest(
            symbol=symbol,
            qty=target_qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 2),
        )
        stop_order = client.submit_order(order_data=stop_req)
        print(f"  order_id={stop_order.id}  status={stop_order.status}  qty={stop_order.qty}  stop=${stop_order.stop_price}")
    elif target_qty > 0:
        print()
        print(f"  No STOP_PRICE provided — leaving remaining {target_qty} shares unprotected.")
    else:
        print()
        print("  Target qty is 0 — position fully closed, no new stop needed.")

    # Final state
    print()
    print(f"=== Open orders for {symbol} ===")
    open_orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=20)
    )
    if not open_orders:
        print("  (none)")
    for o in open_orders:
        print(f"  {o.id}  {str(o.side).split('.')[-1]:<4} {str(o.order_type).split('.')[-1]:<7} qty={o.qty} stop=${o.stop_price} status={o.status}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print(__doc__)
        sys.exit(64)
    sym = sys.argv[1].upper()
    try:
        target = float(sys.argv[2])
    except ValueError:
        print(f"Invalid target qty: {sys.argv[2]}")
        sys.exit(64)
    sp: float | None = None
    if len(sys.argv) == 4:
        try:
            sp = float(sys.argv[3])
        except ValueError:
            print(f"Invalid stop price: {sys.argv[3]}")
            sys.exit(64)
    sys.exit(main(sym, target, sp))
