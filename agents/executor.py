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


def _emergency_stop_active() -> bool:
    """Top-level risk.max_open_positions == 0 in config.yaml is a hard kill-switch
    that overrides any active profile. Set to 0 to halt all new entries while
    keeping the infrastructure running (data fetch, journal, dashboard)."""
    try:
        import yaml
        cfg = yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8"))
        risk = cfg.get("risk") or {}
        return int(risk.get("max_open_positions", -1)) == 0
    except Exception:
        return False


def load_risk_profile() -> tuple[str, dict]:
    """
    Read the active risk profile name from risk_profile.json, then load
    its parameters from config.yaml.  Falls back to high_safety if either
    file is missing or the named profile doesn't exist in config.
    Returns (profile_name, profile_dict).
    """
    profile_name = _DEFAULT_PROFILE
    profile_file = _find_profile_file()
    # Loud failure when neither location has the file. Silently defaulting to
    # high_safety on every run was the M-3/M-1 audit finding (2026-05-22, 2026-05-23):
    # operators editing risk_profile.json in the bot tree saw no effect because the
    # bot fell back to a default.
    if not _LOCAL_PROFILE_FILE.exists() and not _DASHBOARD_PROFILE_FILE.exists():
        logger.error(
            f"risk_profile.json NOT FOUND in either location:\n"
            f"  - {_LOCAL_PROFILE_FILE}\n"
            f"  - {_DASHBOARD_PROFILE_FILE}\n"
            f"Falling back to {_DEFAULT_PROFILE}. Create the file in one of these "
            f"locations to make the active profile explicit."
        )
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

# Float tolerance for the position-size boundary check. Crypto sizing caps the
# notional at exactly `account_value * max_position_pct`, then divides by
# entry_price to get a fractional quantity. Multiplying that quantity back by
# entry_price inside validate_order can drift by ~1 ulp, pushing position_pct
# fractionally above the cap and triggering a spurious rejection at exactly 10%.
POSITION_PCT_EPS = 1e-6


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
        take_profit: float = 0.0,
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

        # Emergency stop: top-level risk.max_open_positions == 0 in config.yaml halts
        # all new entries regardless of active profile. Exits are still allowed so an
        # operator can flatten positions while the bot stays running for data/dashboard.
        if not is_exit and _emergency_stop_active():
            return False, (
                "Emergency stop active (config.yaml risk.max_open_positions=0). "
                "No new entries. Set to a positive value or delete the field to resume."
            )

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
        if position_pct > max_pos_pct + POSITION_PCT_EPS:
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

        # 7. Minimum R:R ratio gate (approved suggestion in-20260511-002)
        # Prevents entering trades where the profit target doesn't justify the risk.
        if take_profit and take_profit > entry_price and (entry_price - stop_loss) > 0:
            rr_ratio = (take_profit - entry_price) / (entry_price - stop_loss)
            if rr_ratio < 1.5:
                return False, f"R:R ratio too low ({rr_ratio:.2f}:1, minimum 1.5:1)"

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

    def _wait_for_terminal_status(self, order_id: str, max_wait_secs: int = 10):
        """
        Poll Alpaca until the order reaches a terminal state or timeout.
        Market orders on paper fill in < 1 s; 10 s is a generous safety margin.
        Returns the final order object (possibly still non-terminal on timeout).
        """
        import time
        terminal = {"filled", "partially_filled", "cancelled", "expired", "replaced", "rejected"}
        deadline = time.monotonic() + max_wait_secs
        while time.monotonic() < deadline:
            order = self.trading_client.get_order_by_id(order_id)
            if str(order.status) in terminal:
                return order
            time.sleep(0.5)
        logger.warning(f"Order {order_id} did not reach terminal state within {max_wait_secs}s — journalling as-is")
        return self.trading_client.get_order_by_id(order_id)

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
            return {"status": "CANCELLED", "reason": "No symbol in decision"}

        entry_price = float(decision.get("entry_price", 0))
        stop_loss   = float(decision.get("stop_loss", 0))
        take_profit = float(decision.get("take_profit", 0))

        is_crypto = _is_crypto(symbol)
        is_exit   = (action == "SELL")
        p = self.profile

        # ── Account context ───────────────────────────────────────────────────
        # H-3 (audit 2026-05-26): current_positions for the max_open_positions
        # cap is the COMBINED venue-aggregate count when main.py has augmented
        # `account` with it. Fall back to per-venue count when called outside
        # the main loop (tests, scripts).
        if is_crypto:
            crypto_client = _get_crypto_client()
            crypto_bal = crypto_client.get_balance()
            if "error" in crypto_bal:
                exchange_name = type(crypto_client).__name__
                return {"status": "CANCELLED", "reason": f"{exchange_name} balance fetch failed: {crypto_bal['error']}", "symbol": symbol}
            account_value     = float(crypto_bal.get("account_value", 0))
            buying_power      = float(crypto_bal.get("free_usdt",     0))
            current_positions = int(
                account.get("open_positions")
                if account.get("open_positions") is not None
                else crypto_bal.get("open_positions", 0)
            )
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
                return {"status": "CANCELLED", "reason": "Exit quantity must be > 0", "symbol": symbol}
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
                return {
                    "status": "SKIPPED",
                    "reason": f"USDT budget too small (${usdt_budget:.2f}, min ${p['min_order_value']})",
                    "symbol": symbol,
                    "venue_account": crypto_bal,
                }
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

        # Existing-quantity lookup for the no-adds rule.
        # For equities: three-layer check via account snapshot, live Alpaca
        # positions, and open Alpaca orders (see comments below).
        # For crypto: the exchange balance dict is the ground truth for coins
        # that have already settled, but SUBMITTED orders take 1–30 s to appear
        # as a balance — so we also check the journal for any BUY whose
        # execution_status is SUBMITTED or FILLED with no subsequent SELL in
        # the last 3 days.  This is the root cause of the LTC/USDT 15-order
        # loop on 2026-05-25 (C-3 pattern, audit 2026-05-25).
        existing_qty = 0.0

        if is_crypto and action == "BUY":
            # Crypto Layer 1: coins already visible in the exchange balance
            base_coin = symbol.split("/")[0]  # "LTC/USDT" → "LTC"
            for pos in (crypto_bal.get("positions") or []):
                if pos.get("currency") == base_coin and (pos.get("value_usd") or 0) >= 10:
                    existing_qty = float(pos.get("amount", 0))
                    logger.info(
                        f"No-adds (crypto): balance shows {existing_qty:.6f} {base_coin} "
                        f"(${pos.get('value_usd', 0):.2f}) — treating as existing position"
                    )
                    break

            # Crypto Layer 2: recent SUBMITTED/FILLED journal entries (catches
            # orders that haven't settled into the balance yet).
            if existing_qty == 0:
                journal_path = _BOT_ROOT / "journal" / "trades.jsonl"
                dash_journal  = _BOT_ROOT.parent.parent / "flowtrader-dashboard" / "journal" / "trades.jsonl"
                for jpath in (journal_path, dash_journal):
                    if not jpath.exists():
                        continue
                    try:
                        from datetime import datetime, timedelta
                        import pytz
                        cutoff = datetime.now(pytz.utc) - timedelta(days=3)
                        lines = jpath.read_text(encoding="utf-8-sig").splitlines()
                        # Walk newest-first: most recent entry per symbol
                        open_syms: set = set()
                        for raw in reversed(lines):
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                entry = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            sym_j = entry.get("symbol") or ""
                            if sym_j != symbol:
                                continue
                            ts_str = entry.get("timestamp", "")
                            try:
                                ts = datetime.fromisoformat(ts_str)
                                if ts.tzinfo is None:
                                    import pytz as _pytz
                                    ts = _pytz.utc.localize(ts)
                                if ts < cutoff:
                                    break  # entries are newest-first, stop
                            except Exception:
                                continue
                            act = entry.get("action", "")
                            status = entry.get("execution_status", "")
                            if act == "SELL":
                                break  # position was closed; no open position
                            if act == "BUY" and status in ("SUBMITTED", "FILLED", "SIMULATED"):
                                existing_qty = float(entry.get("quantity") or 1)
                                logger.info(
                                    f"No-adds (crypto): journal shows recent BUY {symbol} "
                                    f"({status} @ {ts_str[:19]}) — treating as existing position"
                                )
                                break
                        if existing_qty > 0:
                            break
                    except Exception as e:
                        logger.warning(f"Crypto journal no-adds check failed: {e}")

        if not is_crypto:
            # Layer 1: positions already reported in the account snapshot
            for pos in (account.get("positions") or []):
                if pos.get("symbol") == symbol:
                    existing_qty = float(pos.get("qty", 0))
                    break

            if action == "BUY" and self.alpaca_available:
                # Layer 2: live positions from Alpaca (catches fills the account
                # snapshot may have missed between poll cycles — root cause of
                # the 3 duplicate META SUBMITs on 2026-05-06/07, C-4 in audit).
                if existing_qty == 0:
                    try:
                        live_pos = self.trading_client.get_open_position(symbol)
                        if live_pos:
                            existing_qty = float(live_pos.qty or 0)
                            if existing_qty > 0:
                                logger.info(f"No-adds: live position check found {existing_qty} shares of {symbol}")
                    except Exception:
                        pass  # 404 = no position, which is fine

                # Layer 3: open orders (catches bracket BUYs that submitted but
                # haven't appeared in positions yet — the original check).
                if existing_qty == 0:
                    try:
                        from alpaca.trading.requests import GetOrdersRequest
                        from alpaca.trading.enums import QueryOrderStatus
                        open_orders = self.trading_client.get_orders(
                            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
                        )
                        if open_orders:
                            existing_qty = sum(float(o.qty or 0) for o in open_orders)
                            logger.info(f"No-adds: found {len(open_orders)} open order(s) for {symbol} totalling {existing_qty} shares — treating as existing position")
                    except Exception as e:
                        logger.warning(f"Could not check open orders for {symbol}: {e}")

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
            take_profit=take_profit,
        )

        if not is_valid:
            logger.warning(f"Order rejected: {reason}")
            rejection = {
                "status": "CANCELLED",
                "reason": reason,
                "symbol": symbol,
                "attempted_quantity": quantity,
            }
            if is_crypto:
                rejection["venue_account"] = crypto_bal
            return rejection

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
            result["venue_account"] = crypto_bal
            return result

        # ── Route equities to Alpaca ──────────────────────────────────────────
        if not self.alpaca_available:
            logger.warning("Alpaca not available. Simulating order.")
            return {
                "status": "FILLED",
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

            # Poll for terminal status — market orders on paper typically fill
            # within 1-2 seconds, but submit_order returns immediately with
            # status "new" or "accepted".  Returning SUBMITTED here caused the
            # journal to never record a FILLED row (C-4 in audit 2026-05-20).
            order = self._wait_for_terminal_status(str(order.id))

            final_status = str(order.status)
            filled_qty = float(order.filled_qty or 0)
            filled_avg = float(order.filled_avg_price or 0) if order.filled_avg_price else entry_price

            # Map Alpaca status strings to spec-compliant enum {FILLED, SKIPPED, PARTIAL, CANCELLED}
            status_map = {
                "filled": "FILLED",
                "partially_filled": "PARTIAL",
                "rejected": "CANCELLED",
                "cancelled": "CANCELLED",
                "expired": "CANCELLED",
                "replaced": "CANCELLED",
            }
            mapped_status = status_map.get(final_status.lower(), "CANCELLED")

            return {
                "status": mapped_status,
                "order_id": str(order.id),
                "symbol": symbol,
                "side": action,
                "quantity": filled_qty if filled_qty > 0 else quantity,
                "entry_price": filled_avg,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "order_status": final_status,
                "paper_trade": self.paper
            }

        except Exception as e:
            logger.error(f"Order submission failed: {e}")
            return {
                "status": "CANCELLED",
                "reason": str(e),
                "symbol": symbol,
                "side": action,
                "quantity": quantity
            }
