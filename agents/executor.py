"""
agents/executor.py
Order execution module. Validates decisions against risk-profile guardrails,
then routes orders to Alpaca (equities) or Bybit via CCXT (crypto).
Never bypasses risk checks.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_BOT_ROOT       = Path(__file__).resolve().parent.parent
_DASHBOARD_ROOT = _BOT_ROOT.parent.parent / "flowtrader-dashboard"
_PROFILE_FILE   = _DASHBOARD_ROOT / "journal" / "risk_profile.json"
_CONFIG_FILE    = _BOT_ROOT / "config.yaml"

_DEFAULT_PROFILE = "high_safety"

_FALLBACK_PROFILES = {
    "high_safety": {
        "max_open_positions":    3,
        "max_daily_loss_pct":    0.02,
        "max_position_pct":      0.10,
        "risk_pct_per_trade":    0.01,
        "min_signal_score":      3,
        "max_stop_distance_pct": 0.05,
        "min_order_value":       100,
    },
    "medium_safety": {
        "max_open_positions":    5,
        "max_daily_loss_pct":    0.04,
        "max_position_pct":      0.15,
        "risk_pct_per_trade":    0.015,
        "min_signal_score":      2,
        "max_stop_distance_pct": 0.08,
        "min_order_value":       50,
    },
}


def load_risk_profile() -> tuple[str, dict]:
    """
    Read the active risk profile name from risk_profile.json, then load
    its parameters from config.yaml.  Falls back to high_safety if either
    file is missing or the named profile doesn't exist in config.
    Returns (profile_name, profile_dict).
    """
    profile_name = _DEFAULT_PROFILE
    try:
        data = json.loads(_PROFILE_FILE.read_text(encoding="utf-8"))
        profile_name = data.get("active_profile", _DEFAULT_PROFILE)
    except Exception:
        logger.warning(f"risk_profile.json not found or unreadable at {_PROFILE_FILE} — defaulting to {_DEFAULT_PROFILE}")

    try:
        import yaml
        cfg = yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8"))
        profiles = cfg.get("risk_profiles", {})
        if profile_name in profiles:
            return profile_name, profiles[profile_name]
        logger.warning(f"Profile '{profile_name}' not in config.yaml — falling back to high_safety")
    except Exception as e:
        logger.warning(f"Could not load config.yaml profiles: {e} — using hardcoded fallback")

    return profile_name, _FALLBACK_PROFILES.get(profile_name, _FALLBACK_PROFILES[_DEFAULT_PROFILE])


def _is_crypto(symbol: str) -> bool:
    """Crypto symbols contain a slash — e.g. BTC/USDT."""
    return "/" in str(symbol)


class OrderExecutor:
    """
    Executes trade decisions via Alpaca (equities) or Bybit (crypto).
    All orders pass through risk-profile guardrail validation before submission.
    PAPER TRADING is the default — never goes live without explicit config.
    """

    def __init__(self):
        self.paper = os.getenv("PAPER_TRADING", "true").lower() == "true"
        self.alpaca_key = os.getenv("ALPACA_API_KEY")
        self.alpaca_secret = os.getenv("ALPACA_SECRET_KEY")

        self.profile_name, self.profile = load_risk_profile()
        logger.info(
            f"Risk profile: {self.profile_name} | "
            f"max_pos={self.profile['max_open_positions']} | "
            f"daily_loss={self.profile['max_daily_loss_pct']:.0%} | "
            f"min_score={self.profile['min_signal_score']}/6 | "
            f"risk/trade={self.profile['risk_pct_per_trade']:.1%}"
        )

        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

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
        Guardrail validation against the active risk profile.
        ALL checks must pass. Returns (is_valid, reason).
        """
        p = self.profile

        # 1. Max open positions
        max_pos = p["max_open_positions"]
        if current_positions >= max_pos:
            return False, f"Max positions reached ({current_positions}/{max_pos})"

        # 2. Daily loss limit
        daily_loss_pct = abs(day_pl) / account_value if day_pl < 0 else 0
        loss_limit = p["max_daily_loss_pct"]
        if daily_loss_pct >= loss_limit:
            return False, f"Daily loss limit hit ({daily_loss_pct:.1%} ≥ {loss_limit:.0%}). No more trades today."

        # 3. Position size check
        order_value = quantity * entry_price
        position_pct = order_value / account_value
        max_pos_pct = p["max_position_pct"]
        if position_pct > max_pos_pct:
            return False, f"Position too large ({position_pct:.1%} of account, max {max_pos_pct:.0%})"

        # 4. Minimum order value
        min_val = p["min_order_value"]
        if order_value < min_val:
            return False, f"Order value too small (${order_value:.2f}, min ${min_val})"

        # 5. Stop loss must be set
        if not stop_loss or stop_loss <= 0:
            return False, "Stop loss not defined. Cannot place order."

        # 6. Stop loss sanity check
        max_stop = p["max_stop_distance_pct"]
        if side.upper() == "BUY":
            stop_distance_pct = (entry_price - stop_loss) / entry_price
            if stop_distance_pct > max_stop:
                return False, f"Stop loss too far ({stop_distance_pct:.1%} from entry, max {max_stop:.0%})"
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
        risk_pct: float = None,
    ) -> int:
        """
        Position size based on profile risk-per-trade rule.
        If always_max_position is set on the profile, skips the risk-per-share
        formula and sizes directly at max_position_pct — guaranteed to execute.
        Otherwise: Quantity = (Account × Risk%) / (Entry − Stop), capped at max_position_pct.
        """
        if entry_price <= 0:
            return 0

        max_order_value = account_value * self.profile["max_position_pct"]

        if self.profile.get("always_max_position"):
            return max(1, int(max_order_value / entry_price))

        if risk_pct is None:
            risk_pct = self.profile["risk_pct_per_trade"]

        if entry_price <= stop_loss:
            return 0

        risk_per_share = entry_price - stop_loss
        if risk_per_share <= 0:
            return 0

        quantity = int((account_value * risk_pct) / risk_per_share)
        quantity = min(quantity, int(max_order_value / entry_price))
        return max(1, quantity)

    def place_order(self, decision: dict, account: dict) -> dict:
        """
        Main order placement function.
        Equity orders use the Alpaca account context passed in. Crypto orders
        switch to the Bybit account context before validating.
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
        p = self.profile

        # ── Account context ───────────────────────────────────────────────────
        if is_crypto:
            from data.crypto_fetcher import BybitFetcher
            bybit = BybitFetcher()
            bybit_bal = bybit.get_balance()
            if "error" in bybit_bal:
                return {"status": "ERROR", "reason": f"Bybit balance fetch failed: {bybit_bal['error']}", "symbol": symbol}
            account_value     = float(bybit_bal.get("account_value", 0))
            buying_power      = float(bybit_bal.get("free_usdt",     0))
            current_positions = int(bybit_bal.get("open_positions",  0))
            day_pl            = 0.0
        else:
            bybit = None
            account_value     = float(account.get("portfolio_value", 10000))
            buying_power      = float(account.get("buying_power",    0))
            current_positions = int(account.get("open_positions",    0))
            day_pl            = float(account.get("day_pl",          0))

        # ── Position sizing ───────────────────────────────────────────────────
        if is_crypto:
            max_usdt = min(account_value * p["max_position_pct"], buying_power * 0.9)
            if p.get("always_max_position"):
                usdt_budget = max_usdt
            else:
                risk_usdt     = account_value * p["risk_pct_per_trade"]
                stop_distance = max(entry_price - stop_loss, 1e-9) if action == "BUY" else 1e-9
                usdt_budget   = (risk_usdt / stop_distance) * entry_price if stop_distance > 0 else 0
                usdt_budget   = min(usdt_budget, max_usdt)
            if usdt_budget < p["min_order_value"]:
                return {"status": "SKIPPED", "reason": f"USDT budget too small (${usdt_budget:.2f}, min ${p['min_order_value']})", "symbol": symbol}
            quantity = usdt_budget / entry_price
        else:
            quantity = self.calculate_quantity(account_value, entry_price, stop_loss)
            if quantity * entry_price > buying_power:
                quantity = int(buying_power * 0.9 / entry_price)
                if quantity < 1:
                    return {"status": "SKIPPED", "reason": "Insufficient buying power"}

        # ── Validate ──────────────────────────────────────────────────────────
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

        # ── Place crypto order via Bybit ──────────────────────────────────────
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
