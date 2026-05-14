"""
strategies/momentum.py
Momentum / breakout scoring — the counterpart to mean reversion.

Mean reversion buys *into* extension (price stretched below mean, RSI oversold,
expecting return to MA20).

Momentum buys *with* extension (price breaking out above range, RSI strong,
ADX trending, expecting continuation).

This module also exposes:
  apply_directional_gate(indicators) — enforces that mean-reversion eligibility
    requires at least one classical oversold trigger (RSI/BB/MA20). Regime or
    math signals alone are not enough.

  select_strategy_mode(indicators, min_score) — regime router. Sets the
    `strategy_mode` field on the indicators dict to "MEAN_REVERSION",
    "MOMENTUM", or "NONE" based on which side (if any) has both sufficient
    score and direction.

The decision agent reads strategy_mode per symbol and applies the matching
entry/stop/target slot — momentum trades use `momentum_stop_loss_price` and
`momentum_take_profit_price`, mean-reversion trades use the existing
`stop_loss_price` and `take_profit_price`.
"""

from typing import Optional
import pandas as pd

MAX_MOMENTUM_SCORE = 6
BREAKOUT_LOOKBACK = 20
RECENT_LOW_WINDOW = 5
ATR_STOP_MULTIPLIER = 1.0
ATR_TARGET_MULTIPLIER = 2.0


def compute_momentum(df: Optional[pd.DataFrame], indicators: dict) -> dict:
    """
    Compute momentum/breakout score and entry levels. Mutates and returns
    `indicators`. Adds:
      momentum_score              — int 0..MAX_MOMENTUM_SCORE
      momentum_signals_fired      — list[str]
      momentum_stop_loss_price    — float (0 if not computable)
      momentum_take_profit_price  — float (0 if not computable)
      momentum_eligible           — bool
    """
    if df is None or len(df) < 25 or "error" in indicators:
        indicators.setdefault("momentum_score", 0)
        indicators.setdefault("momentum_signals_fired", [])
        indicators.setdefault("momentum_stop_loss_price", 0.0)
        indicators.setdefault("momentum_take_profit_price", 0.0)
        indicators.setdefault("momentum_eligible", False)
        return indicators

    rsi   = float(indicators.get("rsi", 50) or 50)
    price = float(indicators.get("current_price", 0) or 0)
    ma20  = float(indicators.get("ma20", 0) or 0)
    ma50  = float(indicators.get("ma50", 0) or 0)
    adx   = float(indicators.get("adx", 0) or 0)
    atr   = float(indicators.get("atr", 0) or 0)
    upper = float((indicators.get("bollinger") or {}).get("upper", 0) or 0)

    signals: list[str] = []
    score = 0

    if rsi > 65:
        signals.append("RSI>65 (strong momentum)")
        score += 2
    elif rsi > 55:
        signals.append("RSI>55 (mild momentum)")
        score += 1

    if upper > 0 and price > upper:
        signals.append("AboveUpperBB")
        score += 1

    if ma20 > 0 and price > ma20 * 1.01:
        signals.append("AboveMA20>1%")
        score += 1

    if adx > 25:
        signals.append("ADX>25 (trending)")
        score += 1

    if ma20 > 0 and ma50 > 0 and ma20 > ma50:
        signals.append("MA20>MA50 (uptrend stack)")
        score += 1

    try:
        recent_high = float(df["high"].tail(BREAKOUT_LOOKBACK).max())
        if price > 0 and price >= recent_high * 0.999:
            signals.append(f"{BREAKOUT_LOOKBACK}DayHighBreakout")
            score += 1
    except Exception:
        pass

    score = min(score, MAX_MOMENTUM_SCORE)

    momentum_stop = 0.0
    momentum_target = 0.0
    if atr > 0 and price > 0:
        try:
            recent_low = float(df["low"].tail(RECENT_LOW_WINDOW).min())
        except Exception:
            recent_low = price - atr
        atr_stop = price - ATR_STOP_MULTIPLIER * atr
        # Use the higher (tighter) stop so risk is bounded — but never above entry
        candidate = max(atr_stop, recent_low)
        if candidate < price:
            momentum_stop = round(candidate, 6)
            momentum_target = round(price + ATR_TARGET_MULTIPLIER * atr, 6)

    indicators["momentum_score"] = score
    indicators["momentum_signals_fired"] = signals
    indicators["momentum_stop_loss_price"] = momentum_stop
    indicators["momentum_take_profit_price"] = momentum_target

    # Eligibility: needs at least 2/6 score, a valid stop below entry, and at
    # least one classical directional uptrend trigger (so regime/trend points
    # alone never qualify — this is the momentum side's directional gate).
    directional_up = any(
        s.startswith("RSI>") or s == "AboveUpperBB" or s.startswith("AboveMA20")
        or s.endswith("HighBreakout")
        for s in signals
    )
    indicators["momentum_eligible"] = (
        score >= 2 and momentum_stop > 0 and momentum_stop < price and directional_up
    )
    return indicators


def apply_directional_gate(indicators: dict) -> dict:
    """
    Mean reversion requires at least one classical oversold trigger
    (RSI<45, BelowLowerBB, or BelowMA20>1%) — regime and math signals
    alone are not sufficient. Without one, `mean_reversion_eligible` is
    forced to False so the regime router won't pick MEAN_REVERSION.
    """
    if "error" in indicators:
        return indicators

    fired = indicators.get("signals_fired", []) or []
    oversold = any(
        s.startswith("RSI<") or s == "BelowLowerBB" or s.startswith("BelowMA20")
        for s in fired
    )
    indicators["directional_oversold"] = oversold

    if not oversold and indicators.get("mean_reversion_eligible"):
        indicators["mean_reversion_eligible"] = False
        notes = indicators.setdefault("eligibility_notes", [])
        notes.append("MR ineligible: no directional oversold trigger (RSI/BB/MA20)")

    return indicators


def select_strategy_mode(indicators: dict, min_score: int) -> dict:
    """
    Regime router. Picks the active strategy for this symbol and writes it to
    `indicators["strategy_mode"]`. Both sides must clear `min_score` AND their
    own eligibility flag (which already encodes directional gating).
    """
    if "error" in indicators:
        indicators["strategy_mode"] = "NONE"
        return indicators

    mr_score  = int(indicators.get("signal_score", 0) or 0)
    mr_ok     = bool(indicators.get("mean_reversion_eligible")) and mr_score >= min_score
    mom_score = int(indicators.get("momentum_score", 0) or 0)
    mom_ok    = bool(indicators.get("momentum_eligible")) and mom_score >= min_score

    if mr_ok and mom_ok:
        indicators["strategy_mode"] = "MEAN_REVERSION" if mr_score >= mom_score else "MOMENTUM"
    elif mr_ok:
        indicators["strategy_mode"] = "MEAN_REVERSION"
    elif mom_ok:
        indicators["strategy_mode"] = "MOMENTUM"
    else:
        indicators["strategy_mode"] = "NONE"

    return indicators


def active_score(indicators: dict) -> int:
    """Return the score for the currently selected strategy_mode."""
    mode = indicators.get("strategy_mode", "NONE")
    if mode == "MOMENTUM":
        return int(indicators.get("momentum_score", 0) or 0)
    if mode == "MEAN_REVERSION":
        return int(indicators.get("signal_score", 0) or 0)
    return 0
