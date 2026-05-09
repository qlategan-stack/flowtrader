"""
Tests for the 'no adds to existing positions' rule in OrderExecutor.

Background: Alpaca silently cancels a new bracket order's stop legs if the
parent BUY fills into a symbol that's already held — you can't have two OCO
pairs on one position. Stacking BUYs on the same symbol therefore produces
unprotected exposure. The bot's mean-reversion strategy enters once and
exits at the mean, so legitimate adds don't exist.

This test suite covers a single rule: BUY orders are rejected if the symbol
is already held (existing_qty > 0). SELL orders are unaffected.
"""
from agents.executor import OrderExecutor


def _make_executor() -> OrderExecutor:
    """Build an OrderExecutor without running __init__ side effects (no I/O)."""
    ex = OrderExecutor.__new__(OrderExecutor)
    ex.paper = True
    ex.profile_name = "high_safety"
    ex.profile = {
        "max_open_positions":    3,
        "max_daily_loss_pct":    0.02,
        "max_position_pct":      0.10,
        "risk_pct_per_trade":    0.01,
        "min_signal_score":      3,
        "max_stop_distance_pct": 0.05,
        "min_order_value":       100,
    }
    ex.alpaca_available = False
    return ex


# ── Sanity: existing checks still work for fresh entries ──────────────────────

def test_fresh_buy_passes_when_under_cap():
    ex = _make_executor()
    ok, reason = ex.validate_order(
        symbol="AMD",
        side="BUY",
        quantity=10,
        entry_price=100.0,
        stop_loss=97.0,
        account_value=100_000,
        current_positions=0,
        day_pl=0.0,
        existing_qty=0.0,
    )
    assert ok, reason


def test_fresh_buy_rejected_when_over_cap():
    ex = _make_executor()
    # 200 * 100 = $20k = 20% of account, over the 10% cap
    ok, reason = ex.validate_order(
        symbol="AMD",
        side="BUY",
        quantity=200,
        entry_price=100.0,
        stop_loss=97.0,
        account_value=100_000,
        current_positions=0,
        day_pl=0.0,
        existing_qty=0.0,
    )
    assert not ok
    assert "Position too large" in reason


# ── New rule: BUY rejected if symbol already held ─────────────────────────────

def test_buy_rejected_when_symbol_already_held():
    ex = _make_executor()
    ok, reason = ex.validate_order(
        symbol="META",
        side="BUY",
        quantity=10,
        entry_price=600.0,
        stop_loss=580.0,
        account_value=100_000,
        current_positions=1,
        day_pl=0.0,
        existing_qty=32,  # already hold 32 META shares
    )
    assert not ok
    assert "Already hold" in reason
    assert "META" in reason
    assert "32" in reason


def test_buy_rejected_when_existing_qty_is_fractional_crypto_style():
    """Even a tiny existing position blocks adding."""
    ex = _make_executor()
    ok, reason = ex.validate_order(
        symbol="AAPL",
        side="BUY",
        quantity=5,
        entry_price=200.0,
        stop_loss=195.0,
        account_value=100_000,
        current_positions=1,
        day_pl=0.0,
        existing_qty=0.5,
    )
    assert not ok
    assert "Already hold" in reason


def test_no_adds_rule_fires_before_per_order_cap():
    """
    When BOTH conditions are true (symbol already held AND order would
    exceed cap), the no-adds rule should fire first so the user gets the
    accurate diagnosis. Either rejection is correct, but the no-adds
    message is more informative.
    """
    ex = _make_executor()
    ok, reason = ex.validate_order(
        symbol="META",
        side="BUY",
        quantity=200,            # would also exceed per-order cap
        entry_price=600.0,
        stop_loss=580.0,
        account_value=100_000,
        current_positions=1,
        day_pl=0.0,
        existing_qty=32,
    )
    assert not ok
    assert "Already hold" in reason


# ── SELL is unaffected ────────────────────────────────────────────────────────

def test_sell_passes_when_position_held():
    """Exits must still work — that's how we close the existing position."""
    ex = _make_executor()
    ok, reason = ex.validate_order(
        symbol="META",
        side="SELL",
        quantity=216,
        entry_price=609.0,
        stop_loss=0.0,
        account_value=100_000,
        current_positions=1,
        day_pl=0.0,
        existing_qty=216,
    )
    assert ok, reason


# ── Backwards compatibility: existing_qty defaults to 0 ───────────────────────

def test_existing_qty_defaults_to_zero():
    """Callers that haven't been updated still work as before."""
    ex = _make_executor()
    ok, _ = ex.validate_order(
        symbol="AMD",
        side="BUY",
        quantity=10,
        entry_price=100.0,
        stop_loss=97.0,
        account_value=100_000,
        current_positions=0,
        day_pl=0.0,
        # existing_qty omitted
    )
    assert ok
