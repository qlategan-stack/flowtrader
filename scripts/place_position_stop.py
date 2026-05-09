"""
One-shot tool: place a protective GTC stop-loss on an existing Alpaca position.

Usage:
    python scripts/place_position_stop.py SYMBOL STOP_PRICE
    python scripts/place_position_stop.py META 597.44

Why this exists: when the bot stacks bracket orders for a symbol it already
holds, Alpaca silently cancels the new bracket's stop legs the moment the
parent BUY fills (you can't have multiple OCO pairs on one position). After
several such BUYs the position is unprotected. This script attaches a single
GTC stop covering the FULL current quantity, replacing whatever stop legs
got auto-cancelled along the way.

Behaviour:
  * Reads the live position from Alpaca and uses that exact quantity.
  * Cancels any existing stop orders for the symbol first (so we don't end
    up with two stops fighting each other).
  * Places ONE GTC stop-market sell for the full position quantity.
  * Prints the resulting order id and the open-orders state for the symbol.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from repo root or scripts/
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent))

from dotenv import load_dotenv
load_dotenv(_HERE.parent.parent / ".env")

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest


def main(symbol: str, stop_price: float) -> int:
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
        print(f"  No open position for {symbol} ({e}). Nothing to protect.")
        return 1

    qty = float(position.qty)
    avg_entry = float(position.avg_entry_price)
    current = float(position.current_price)
    market_value = float(position.market_value)
    print(f"  qty={qty}  avg_entry=${avg_entry:.2f}  current=${current:.2f}  market_value=${market_value:,.2f}")
    print(f"  Proposed stop: ${stop_price:.2f} (worst-case loss from current = ${(current - stop_price) * qty:+,.2f})")

    if stop_price >= current:
        print(f"  REFUSED: stop ${stop_price:.2f} is at or above current ${current:.2f}. "
              f"Stop must be below current price for a long position.")
        return 2

    # Cancel any existing stop orders for the symbol so we don't double-stop
    print()
    print(f"=== Cancelling existing open SELL orders on {symbol} ===")
    open_orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=50)
    )
    cancelled = 0
    for o in open_orders:
        if str(o.side).split(".")[-1].upper() == "SELL":
            try:
                client.cancel_order_by_id(o.id)
                cancelled += 1
                print(f"  cancelled {o.id} ({o.order_type} qty={o.qty} stop={o.stop_price} limit={o.limit_price})")
            except Exception as e:
                print(f"  WARNING: could not cancel {o.id}: {e}")
    if cancelled == 0:
        print("  (no existing SELL orders to cancel)")

    # Place the protective stop
    print()
    print(f"=== Placing GTC stop-market sell ===")
    req = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=round(stop_price, 2),
    )
    order = client.submit_order(order_data=req)
    print(f"  order_id={order.id}  status={order.status}  qty={order.qty}  stop=${order.stop_price}")

    # Verify
    print()
    print(f"=== Open orders for {symbol} after placement ===")
    open_orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=20)
    )
    if not open_orders:
        print("  (none — submission may not have settled yet, re-run to confirm)")
    for o in open_orders:
        print(f"  {o.id}  {str(o.side).split('.')[-1]:<4} {str(o.order_type).split('.')[-1]:<5} qty={o.qty} stop=${o.stop_price} status={o.status}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(64)
    sym = sys.argv[1].upper()
    try:
        sp = float(sys.argv[2])
    except ValueError:
        print(f"Invalid stop price: {sys.argv[2]}")
        sys.exit(64)
    sys.exit(main(sym, sp))
