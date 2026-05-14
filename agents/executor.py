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

_BOT_ROOT    = Path(__file__).resolve().parent.parent
load_dotenv(_BOT_ROOT / ".env")
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_CONFIG_FILE = _BOT_ROOT / "config.yaml"

# risk_profile.json search order:
#   1. journal/ inside the bot's own working directory (GitHub Actions cache, CI)
#   2. sibling flowtrader-dashboard repo (local dev)
_LOCAL_PROFILE_FILE     = _BOT_ROOT / "journal" / "risk_profile.json"
_DASHBOARD_PROFILE_FILE = _BOT_ROOT.parent.parent / "flowtrader-dashboard" / "journal" / "risk_profile.json"

def _find_profile_file() -> Path:
    if _LOCAL_PROFILE_FILE.exists():
        return _LOCAL_PROFILE_FILE
    if _DASHBOARD_PROFILE_FILE.exists():
        return _DASHBOARD_PROFILE_FILE
    return _LOCAL_PROFILE_FILE  # return local path so the warning message is useful

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
        "max_position_pct":      0.10,
        "risk_pct_per_trade":    0.015,
        "min_signal_score":      2,
        "max_stop_distance_pct": 0.08,
        "min_order_value":       50,
    },
    "low_safety": {
        "max_open_positions":    8,
        "max_daily_loss_pct":    0.06,
        "max_position_pct":      0.05,
        "risk_pct_per_trade":    0.02,
        "min_signal_score":      1,
        "max_stop_distance_pct": 0.12,
        "min_order_value":       25,
        "always_max_position":   True,
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
    profile_file = _find_profile_file()
    try:
        data = json.loads(profile_file.read_text(encoding="utf-8"))
        profile_name = data.get("active_profile", _DEFAULT_PROFILE)
        logger.info(f"Risk profile loaded from {profile_file}: {profile_name}")
    except Exception:
        logger.warning(f"risk_profile.json not found at {profile_file} — defaulting to {_DEFAULT_PROFILE}")

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


def _get_crypto_client():
    """Return BinanceFetcher if BINANCE_API_KEY is set, else BybitFetcher."""
    if os.getenv("BINANCE_API_KEY"):
        from data.crypto_fetcher import BinanceFetcher
        return BinanceFetcher()
    from data.crypto_fetcher import BybitFetcher
    return BybitFetcher()


# ── Hard absolute cap ─────────────────────────────────────────────────────────
# No single order may exceed this fraction of total account value, regardless
# of which risk profile is active.  This is a belt-and-suspenders safety net
# on top of each profile's max_position_pct — a sanity ceiling that prevents
# any future profile change from accidentally allowing oversized positions.
HARD_MAX_POSITION_PCT = 0.10  # 10% of account, absolute ceiling


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
        day_pl: float,
        existing_qty: float = 0.0,
    ) -> tuple[bool, str]:
        """
        Guardrail validation against the active risk profile.
        SELL orders (closing existing positions) skip entry-only checks.

        existing_qty: shares/units already held in this symbol. Non-zero
        existing_qty blocks new BUYs — see check #1 below.

        Returns (is_valid, reason).
        """
        p = self.profile
        is_exit = side.upper() == "SELL"

        # Daily loss limit applies to ALL orders — both new entries and exits
        daily_loss_pct = abs(day_pl) / account_value if day_pl < 0 else 0
        loss_limit = p["max_daily_loss_pct"]
        if daily_loss_pct >= loss_limit:
            return False, f"Daily loss limit hit ({daily_loss_pct:.1%} ≥ {loss_limit:.0%}). No more trades today."

        # Live-trading confirmation applies to all orders
        if not self.paper and os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() != "true":
            return False, "Live trading not confirmed. Set LIVE_TRADING_CONFIRMED=true to enable."

        # Exits skip entry-only guardrails (max positions, position size cap,
        # min order value, stop loss requirement, stop sanity check)
        if is_exit:
            if quantity <= 0:
                return False, "Exit quantity must be > 0"
            return True, "Exit checks passed"

        # ── Entry-only checks (BUY) ───────────────────────────────────────────
        # 1. No-adds rule: never stack a BUY onto an existing position.
        # Mean-reversion strategy enters once and exits at the mean — there is
        # no legitimate "add to winner" path. Adding also breaks Alpaca bracket
        # protection: a second bracket BUY on a held symbol silently cancels
        # its own stop-loss leg the moment the parent fills, leaving the new
        # quantity unprotected. Closing the existing position before re-entering
        # is the only safe way.
        if existing_qty > 0:
            return False, (
                f"Already hold {existing_qty:g} shares of {symbol}; bot does not add "
                f"to existing positions. Close before re-entering."
            )

        # 2. Max open positions
        max_pos = p["max_open_positions"]
        if current_positions >= max_pos:
            return False, f"Max positions reached ({current_positions}/{max_pos})"

        # 3. Position size check — profile cap AND hard absolute ceiling
        order_value  = quantity * entry_price
        position_pct = order_value / account_value
        max_pos_pct  = min(p["max_position_pct"], HARD_MAX_POSITION_PCT)
        if position_pct > max_pos_pct:
            cap_reason = "hard cap" if HARD_MAX_POSITION_PCT < p["max_position_pct"] else "profile cap"
            return False, f"Position too large ({position_pct:.1%} of account, max {max_pos_pct:.0%} — {cap_reason})"

        # 4. Minimum order value
        min_val = p["min_order_value"]
        if order_value < min_val:
            return False, f"Order value too small (${order_value:.2f}, min ${min_val})"

        # 5. Stop loss must be set for entries
        if not stop_loss or stop_loss <= 0:
            return False, "Stop loss not defined. Cannot place order."

        # 6. Stop loss sanity check
        max_stop = p["max_stop_distance_pct"]
        stop_distance_pct = (entry_price - stop_loss) / entry_price
        if stop_distance_pct > max_stop:
            return False, f"Stop loss too far ({stop_distance_pct:.1%} from entry, max {max_stop:.0%})"
        if stop_loss >= entry_price:
            return False, "Stop loss must be BELOW entry for long positions"

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

        # Apply hard absolute cap on top of profile setting
        effective_pct   = min(self.profile["max_position_pct"], HARD_MAX_POSITION_PCT)
        max_order_value = account_value * effective_pct

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
        is_exit   = (action == "SELL")
        p = self.profile

        # ── Account context ───────────────────────────────────────────────────
        if is_crypto:
            # Prefer Binance when API key is configured; fall back to Bybit.
            crypto_client = _get_crypto_client()
            crypto_bal = crypto_client.get_balance()
            if "error" in crypto_bal:
                exchange_name = type(crypto_client).__name__
                return {"status": "ERROR", "reason": f"{exchange_name} balance fetch failed: {crypto_bal['error']}", "symbol": symbol}
            account_value     = float(crypto_bal.get("account_value", 0))
            buying_power      = float(crypto_bal.get("free_usdt",     0))
            current_positions = int(crypto_bal.get("open_positions",  0))
            day_pl            = 0.0
        else:
            crypto_client = None
            account_value     = float(account.get("portfolio_value", 10000))
            buying_power      = float(account.get("buying_power",    0))
            current_positions = int(account.get("open_positions",    0))
            day_pl            = float(account.get("day_pl",          0))

        # Effective per-order cap: profile setting OR hard absolute ceiling,
        # whichever is smaller.  Guarantees no order ever exceeds 10% of account.
        effective_max_pct = min(p["max_position_pct"], HARD_MAX_POSITION_PCT)

        # ── Position sizing ───────────────────────────────────────────────────
        if is_exit:
            # Exits use the existing position quantity from the decision
            quantity    = float(decision.get("quantity", 0))
            usdt_budget = quantity * entry_price
            if quantity <= 0:
                return {"status": "ERROR", "reason": "Exit quantity must be > 0", "symbol": symbol}
        elif is_crypto:
            max_usdt = min(account_value * effective_max_pct, buying_power * 0.9)
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
            logger.info(
                f"Crypto sizing — {symbol}: order ${usdt_budget:,.2f} "
                f"({usdt_budget/account_value:.2%} of account, profile cap {p['max_position_pct']:.0%}, hard cap {HARD_MAX_POSITION_PCT:.0%})"
            )
        else:
            quantity = self.calculate_quantity(account_value, entry_price, stop_loss)
            if quantity * entry_price > buying_power:
                quantity = int(buying_power * 0.9 / entry_price)
                if quantity < 1:
                    return {"status": "SKIPPED", "reason": "Insufficient buying power"}
            logger.info(
                f"Equity sizing — {symbol}: {quantity} shares = ${quantity*entry_price:,.2f} "
                f"({quantity*entry_price/account_value:.2%} of account, profile cap {p['max_position_pct']:.0%}, hard cap {HARD_MAX_POSITION_PCT:.0%})"
            )

        # Existing-quantity lookup for the no-adds rule. Equities only — the
        # Bybit balance dict doesn't expose per-symbol holdings the same way,
        # and Bybit doesn't have Alpaca's bracket-OCO cancel issue.
        existing_qty = 0.0
        if not is_crypto:
            for pos in (account.get("positions") or []):
                if pos.get("symbol") == symbol:
                    existing_qty = float(pos.get("qty", 0))
                    break

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
            existing_qty=existing_qty,
        )

        if not is_valid:
            logger.warning(f"Order rejected: {reason}")
            return {
                "status": "REJECTED",
                "reason": reason,
                "symbol": symbol,
                "attempted_quantity": quantity,
            }

        # ── Place crypto order (Binance if configured, else Bybit) ───────────
        if is_crypto:
            result = crypto_client.place_order(
                symbol=symbol,
                side=action,
                usdt_amount=usdt_budget,
                current_price=entry_price,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            exchange_label = crypto_bal.get("exchange", "crypto")
            logger.info(f"{exchange_label.capitalize()} order: {action} {symbol} — {result.get('status')}")
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

            if is_exit:
                # Closing an existing position — simple market sell, no bracket.
                # Alpaca will cancel any open child orders (stop/target) when the
                # parent position is closed.
                order_request = self.MarketOrderRequest(
                    symbol=symbol,
                    qty=quantity,
                    side=side,
                    time_in_force=self.TimeInForce.DAY,
                )
            else:
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
