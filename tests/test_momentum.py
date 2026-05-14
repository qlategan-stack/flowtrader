"""
Tests for strategies/momentum.py — the directional gate, momentum scorer,
and regime router that together pick between MEAN_REVERSION and MOMENTUM.

Why these tests exist:
  1. Without the directional gate, the bot scored 28 consecutive SKIPs in
     May 2026 because regime/math signals alone could clear min_score even
     when no oversold trigger had fired. Test covers that exact scenario.
  2. The momentum scorer is symmetric to mean reversion but biased upward
     (breakouts, uptrend stack, ADX>25). Test covers both extremes.
  3. The regime router is the seam the decision agent reads — it must never
     return MEAN_REVERSION without a directional oversold trigger.
"""
import numpy as np
import pandas as pd

from strategies.momentum import (
    apply_directional_gate,
    compute_momentum,
    select_strategy_mode,
    active_score,
)


def _df_uptrend(n: int = 60, start: float = 100.0, drift: float = 0.5) -> pd.DataFrame:
    """Synthetic OHLCV with a steady uptrend — used to exercise momentum paths."""
    rng = np.random.default_rng(42)
    close = start + drift * np.arange(n) + rng.normal(0, 0.3, n)
    high  = close + rng.uniform(0.1, 0.6, n)
    low   = close - rng.uniform(0.1, 0.6, n)
    open_ = close - rng.normal(0, 0.2, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1000})


# ── Directional gate ──────────────────────────────────────────────────────────

def test_directional_gate_blocks_regime_only_score():
    """Regime/math signals alone must not pass the MR directional gate."""
    ind = {
        "signal_score": 3,
        "signals_fired": ["ADX<25 (ranging market)", "Hurst=0.48(anti-persistent+1)", "LevyJump:CALM(+1)"],
        "mean_reversion_eligible": True,
    }
    apply_directional_gate(ind)
    assert ind["directional_oversold"] is False
    assert ind["mean_reversion_eligible"] is False
    assert any("MR ineligible" in n for n in ind.get("eligibility_notes", []))


def test_directional_gate_lets_oversold_through():
    """An actual oversold trigger keeps MR eligibility intact."""
    ind = {
        "signal_score": 3,
        "signals_fired": ["RSI<35 (strong oversold)", "ADX<25 (ranging market)"],
        "mean_reversion_eligible": True,
    }
    apply_directional_gate(ind)
    assert ind["directional_oversold"] is True
    assert ind["mean_reversion_eligible"] is True


def test_directional_gate_below_bb_counts():
    ind = {"signals_fired": ["BelowLowerBB"], "mean_reversion_eligible": True}
    apply_directional_gate(ind)
    assert ind["directional_oversold"] is True
    assert ind["mean_reversion_eligible"] is True


# ── Momentum scorer ───────────────────────────────────────────────────────────

def test_momentum_score_uptrend_full_stack():
    df = _df_uptrend()
    last = float(df["close"].iloc[-1])
    ind = {
        "rsi": 70.0,
        "current_price": last,
        "ma20": last * 0.95,
        "ma50": last * 0.90,
        "adx": 32.0,
        "atr": 2.0,
        "bollinger": {"upper": last * 0.98},
    }
    compute_momentum(df, ind)

    assert ind["momentum_score"] >= 5, (
        f"Expected near-max momentum score, got {ind['momentum_score']} "
        f"(signals: {ind['momentum_signals_fired']})"
    )
    assert ind["momentum_eligible"] is True
    assert ind["momentum_stop_loss_price"] < last
    assert ind["momentum_take_profit_price"] > last
    # 1:2 R:R: target distance should be ~2x stop distance
    risk = last - ind["momentum_stop_loss_price"]
    reward = ind["momentum_take_profit_price"] - last
    assert reward >= 1.5 * risk


def test_momentum_score_flat_market_zero():
    df = _df_uptrend(drift=0.0)
    last = float(df["close"].iloc[-1])
    ind = {
        "rsi": 50.0,
        "current_price": last,
        "ma20": last,
        "ma50": last,
        "adx": 15.0,
        "atr": 1.5,
        "bollinger": {"upper": last * 1.02},
    }
    compute_momentum(df, ind)
    assert ind["momentum_score"] == 0
    assert ind["momentum_eligible"] is False


def test_momentum_eligible_requires_directional_trigger():
    """ADX + MA-stack alone (no RSI/BB/breakout) must NOT be eligible — symmetric to MR gate."""
    df = _df_uptrend(drift=0.0)
    last = float(df["close"].iloc[-1])
    ind = {
        "rsi": 50.0,
        "current_price": last,
        "ma20": last * 0.999,  # only barely above, no AboveMA20>1%
        "ma50": last * 0.95,
        "adx": 30.0,
        "atr": 2.0,
        "bollinger": {"upper": last * 1.02},
    }
    compute_momentum(df, ind)
    # Score may be 2 (ADX + MA stack) but no directional trigger → not eligible
    assert ind["momentum_eligible"] is False


# ── Regime router ─────────────────────────────────────────────────────────────

def test_router_picks_momentum_when_only_momentum_eligible():
    ind = {
        "signal_score": 1,
        "mean_reversion_eligible": False,
        "momentum_score": 4,
        "momentum_eligible": True,
    }
    select_strategy_mode(ind, min_score=2)
    assert ind["strategy_mode"] == "MOMENTUM"
    assert active_score(ind) == 4


def test_router_picks_mean_reversion_when_only_mr_eligible():
    ind = {
        "signal_score": 3,
        "mean_reversion_eligible": True,
        "momentum_score": 0,
        "momentum_eligible": False,
    }
    select_strategy_mode(ind, min_score=2)
    assert ind["strategy_mode"] == "MEAN_REVERSION"
    assert active_score(ind) == 3


def test_router_none_when_neither_qualifies():
    ind = {
        "signal_score": 1,
        "mean_reversion_eligible": False,
        "momentum_score": 1,
        "momentum_eligible": False,
    }
    select_strategy_mode(ind, min_score=2)
    assert ind["strategy_mode"] == "NONE"


def test_router_picks_higher_score_when_both_eligible():
    ind = {
        "signal_score": 2,
        "mean_reversion_eligible": True,
        "momentum_score": 5,
        "momentum_eligible": True,
    }
    select_strategy_mode(ind, min_score=2)
    assert ind["strategy_mode"] == "MOMENTUM"


def test_router_respects_min_score_threshold():
    """Eligibility flag alone isn't enough — must clear profile's min_signal_score."""
    ind = {
        "signal_score": 1,  # eligible flag set but below threshold
        "mean_reversion_eligible": True,
        "momentum_score": 1,
        "momentum_eligible": True,
    }
    select_strategy_mode(ind, min_score=3)
    assert ind["strategy_mode"] == "NONE"


# ── Regression: the May 2026 stuck-on-SKIP scenario ──────────────────────────

def test_overbought_crypto_no_mr_no_momentum_directional():
    """
    Reproduces the May 8-11 2026 LINK/USDT signature: signal_score=4 from
    regime/math alone, RSI~70, price above upper BB — no oversold trigger,
    no momentum directional trigger because RSI wasn't checked as momentum.

    Expected: directional gate blocks MR, momentum scores well (RSI>65,
    above BB, above MA20, MA stack), so router picks MOMENTUM. That's the
    fix — instead of SKIPping forever, the bot now has a way to participate.
    """
    df = _df_uptrend()
    last = 10.44
    ind = {
        "signal_score": 4,
        "signals_fired": [
            "ADX<25 (ranging market)",
            "Wavelet:TREND_DOMINANT",
            "Hurst=0.48(anti-persistent+1)",
            "LevyJump:CALM(+1)",
        ],
        "mean_reversion_eligible": True,
        "rsi": 70.6,
        "current_price": last,
        "ma20": 9.47,
        "ma50": 9.10,
        "adx": 22.0,
        "atr": 0.30,
        "bollinger": {"upper": 10.25},
    }
    # Need an uptrending df for the 20-day breakout check
    df_real = pd.DataFrame({
        "open":   np.linspace(9.0, last - 0.1, 60),
        "high":   np.linspace(9.1, last + 0.05, 60),
        "low":    np.linspace(8.9, last - 0.2, 60),
        "close":  np.linspace(9.0, last, 60),
        "volume": [1000] * 60,
    })

    apply_directional_gate(ind)
    compute_momentum(df_real, ind)
    select_strategy_mode(ind, min_score=2)

    assert ind["directional_oversold"] is False
    assert ind["mean_reversion_eligible"] is False, "MR must be blocked by directional gate"
    assert ind["momentum_score"] >= 4
    assert ind["strategy_mode"] == "MOMENTUM"
