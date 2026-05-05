"""
agents/executor.py
Order execution module. Validates decisions against hard guardrails,
then routes orders to Alpaca (equities) or Bybit via CCXT (crypto).
Never bypasses risk checks.
"""

import os
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

def _is_crypto(symbol: str) -> bool:
    """Crypto symbols contain a slash — e.g. BTC/USDT."""
    return "/" in str(symbol)


class OrderExecutor:
    """
    Executes trade decisions via Alpaca API.
    All orders pass through guardrail validation before submission.
    PAPER TRADING is the default — never goes live without explicit config.
    """

    def __init__(self):
        self.paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
        self.alpaca_key = os.getenv("ALPACA_API_KEY")
        self.alpaca_secret = os.getenv("ALPACA_SECRET_KEY")

        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

            self.trading_client = TradingClient(
                api_key=self.alpaca_key,
                secret_key=self.alpaca_secret,
                paper=self.paper
            )
            self.MarketOrderRequest = MarketOrderRequest
            self.StopLossRequest = StopLossRequest
            self.TakeProfitRequest = TakeProfitRequest
            self.OrderSide = OrderSide
            self.TimeInForce = TimeInForce
            self.alpaca_available = True
            logger.info(f"Alpaca connected. Paper mode: {self.paper}")

        except Exception as e:
            self.alpaca_available = False
            logger.error(f"Alpaca init failed: {e}")

    def validate_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_loss: float,
        account_value: float,
        current_positions: int,
        day_pl: float
    ) -> tuple[bool, str]:
        """
        Hard guardrail validation. ALL checks must pass.
        Returns (is_valid, reason).
        """

        # 1. Max open positions
        if current_positions >= 3:
            return False, f"Max positions reached ({current_positions}/3)"

        # 2. Daily loss limit
        daily_loss_pct = abs(day_pl) / account_value if day_pl < 0 else 0
        if daily_loss_pct >= 0.02:
            return False, f"Daily loss limit hit ({daily_loss_pct:.1%}). No more trades today."

        # 3. Position size check
        order_value = quantity * entry_price
        position_pct = order_value / account_value
        if position_pct > 0.10:
            return False, f"Position too large ({position_pct:.1%} of account, max 10%)"

        # 4. Minimum order value
        if order_value < 100:
            return False, f"Order value too small (${order_value:.2f}, min $100)"

        # 5. Stop loss must be set
        if not stop_loss or stop_loss <= 0:
            return False, "Stop loss not defined. Cannot place order."

        # 6. Stop loss sanity check (not more than 5% away)
        if side.upper() == "BUY":
            stop_distance_pct = (entry_price - stop_loss) / entry_price
            if stop_distance_pct > 0.05:
                return False, f"Stop loss too far ({stop_distance_pct:.1%} from entry, max 5%)"
            if stop_loss >= entry_price:
                return False, "Stop loss must be BELOW entry for long positions"

        # 7. Paper trading check
        if not self.paper and os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() != "true":
            return False, "Live trading not confirmed. Set LIVE_TRADING_CONFIRMED=true to enable."

        return True, "All checks passed"

    def calculate_quantity(
        self,
        account_value: float,
        entry_price: float,
        stop_loss: float,
        risk_pct: float = 0.01
    ) -> int:
        """
        Calculate position size based on 1% account risk rule.
        Quantity = (Account * Risk%) / (Entry - Stop)
        """
        if entry_price <= stop_loss:
            return 0

        risk_amount = account_value * risk_pct
        risk_per_share = entry_price - stop_loss

        if risk_per_share <= 0:
            return 0

        quantity = int(risk_amount / risk_per_share)
        return max(1, quantity)  # Minimum 1 share

    def place_order(self, decision: dict, account: dict) -> dict:
        """
        Main order placement function.
        Equity orders use the Alpaca account context passed in. Crypto orders
        switch to the Bybit account context (free_usdt is the buying-power
        proxy; portfolio_value is the USD-equivalent total) before validating.
        """

        action = decision.get("action", "SKIP").upper()

        if action not in ["BUY", "SELL"]:
            return {
                "status": "SKIPPED",
                "reason": f"Action is {action} — no order placed",
                "decision": decision
            }

        symbol = decision.get("symbol")
        if not symbol:
            return {"status": "ERROR", "reason": "No symbol in decision"}

        entry_price = float(decision.get("entry_price", 0))
        stop_loss   = float(decision.get("stop_loss", 0))
        take_profit = float(decision.get("take_profit", 0))

        is_crypto = _is_crypto(symbol)

        # ── Account context: switch to Bybit's wallet for crypto orders ────
        if is_crypto:
            from data.crypto_fetcher import BybitFetcher
            bybit = BybitFetcher()
            bybit_bal = bybit.get_balance()
            if "error" in bybit_bal:
                return {"status": "ERROR", "reason": f"Bybit balance fetch failed: {bybit_bal['error']}", "symbol": symbol}
            account_value     = float(bybit_bal.get("account_value", 0))
            buying_power      = float(bybit_bal.get("free_usdt",     0))
            current_positions = int(bybit_bal.get("open_positions",  0))
            day_pl            = 0.0   # Bybit testnet doesn't expose realised day PL yet
        else:
            bybit = None
            account_value     = float(account.get("portfolio_value", 10000))
            buying_power      = float(account.get("buying_power",    0))
            current_positions = int(account.get("open_positions",    0))
            day_pl            = float(account.get("day_pl",          0))

        # ── Position size + buying-power check ────────────────────────────
        if is_crypto:
            # Crypto trades fractional units sized in USDT, not whole shares.
            # Risk-based sizing: USDT to risk = 1% of account; quantity = budget / entry.
            risk_usdt = account_value * 0.01
            stop_distance = max(entry_price - stop_loss, 1e-9) if action == "BUY" else 1e-9
            usdt_budget = (risk_usdt / stop_distance) * entry_price if stop_distance > 0 else 0
            usdt_budget = min(usdt_budget, buying_power * 0.9, account_value * 0.10)  # cap at 10% of account
            if usdt_budget < 10:
                return {"status": "SKIPPED", "reason": f"USDT budget too small (${usdt_budget:.2f}, min $10)", "symbol": symbol}
            quantity = usdt_budget / entry_price  # used only for validation
        else:
            quantity = self.calculate_quantity(account_value, entry_price, stop_loss)
            if quantity * entry_price > buying_power:
                quantity = int(buying_power * 0.9 / entry_price)
                if quantity < 1:
                    return {"status": "SKIPPED", "reason": "Insufficient buying power"}

        # ── Validate ─────────────────────────────────────────────────────
        is_valid, reason = self.validate_order(
            symbol=symbol,
            side=action,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            account_value=account_value,
            current_positions=current_positions,
            day_pl=day_pl,
        )

        if not is_valid:
            logger.warning(f"Order rejected: {reason}")
            return {
                "status": "REJECTED",
                "reason": reason,
                "symbol": symbol,
                "attempted_quantity": quantity,
            }

        # ── Place crypto order via Bybit ─────────────────────────────────
        if is_crypto:
            result = bybit.place_order(
                symbol=symbol,
                side=action,
                usdt_amount=usdt_budget,
                current_price=entry_price,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            logger.info(f"Bybit order: {action} {symbol} — {result.get('status')}")
            return result

        # ── Route equities to Alpaca ──────────────────────────────────────────
        if not self.alpaca_available:
            logger.warning("Alpaca not available. Simulating order.")
            return {
                "status": "SIMULATED",
                "symbol": symbol,
                "side": action,
                "quantity": quantity,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "reason": "Alpaca not connected — simulated fill"
            }

        try:
            side = self.OrderSide.BUY if action == "BUY" else self.OrderSide.SELL

            order_request = self.MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=side,
                time_in_force=self.TimeInForce.DAY,
                order_class="bracket",
                stop_loss=self.StopLossRequest(stop_price=round(stop_loss, 2)),
                take_profit=self.TakeProfitRequest(limit_price=round(take_profit, 2))
            )

            order = self.trading_client.submit_order(order_data=order_request)
            logger.info(f"Alpaca order: {action} {quantity} {symbol} @ ~{entry_price}")

            return {
                "status": "FILLED" if str(order.status) in ["filled", "partially_filled"] else "SUBMITTED",
                "order_id": str(order.id),
                "symbol": symbol,
                "side": action,
                "quantity": quantity,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "order_status": str(order.status),
                "paper_trade": self.paper
            }

        except Exception as e:
            logger.error(f"Order submission failed: {e}")
            return {
                "status": "ERROR",
                "reason": str(e),
                "symbol": symbol,
                "side": action,
                "quantity": quantity
            }
