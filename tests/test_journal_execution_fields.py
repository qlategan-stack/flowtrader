"""
Tests for H-1 (journal records EXECUTED quantity, not the AI decision's number)
and H-5 (open_positions raw vs filtered), audit 2026-06-16.
"""
import json

from journal.logger import _coalesce, _raw_open_positions, TradeJournal
import journal.logger as logmod


# ── H-1: _coalesce ───────────────────────────────────────────────────────────

def test_coalesce_prefers_first_positive():
    assert _coalesce(25, 3) == 25          # executor wins over decision
    assert _coalesce(None, 3) == 3         # fall back to decision
    assert _coalesce(0, 3) == 3            # executor 0 -> decision
    assert _coalesce(None, None) is None
    assert _coalesce(0, 0) == 0            # both non-positive -> first non-None


# ── H-5: _raw_open_positions ─────────────────────────────────────────────────

def test_raw_open_positions_augmented():
    a = {"open_positions": 1, "equity_positions": 1, "crypto_positions": 0, "crypto_positions_raw": 19}
    assert _raw_open_positions(a) == 20

def test_raw_open_positions_unaugmented():
    assert _raw_open_positions({"open_positions": 2}) == 2


# ── H-1 end-to-end: the MSFT scenario ────────────────────────────────────────

def test_journal_records_executed_quantity(tmp_path, monkeypatch):
    """Claude says 3 shares; executor sizes 25. Journal must record 25."""
    jfile = tmp_path / "trades.jsonl"
    monkeypatch.setattr(logmod, "JOURNAL_FILE", jfile)
    monkeypatch.setattr(logmod, "JOURNAL_DIR", tmp_path)

    decision = {"action": "BUY", "symbol": "MSFT", "quantity": 3,
                "entry_price": 394.82, "stop_loss": 390.0, "take_profit": 400.0}
    execution = {"status": "FILLED", "quantity": 25, "entry_price": 395.0,
                 "stop_loss": 390.0, "take_profit": 400.0, "order_id": "abc"}
    account = {"portfolio_value": 99000, "open_positions": 1,
               "equity_positions": 1, "crypto_positions": 0, "crypto_positions_raw": 19}

    j = TradeJournal()
    row = j.log_decision(decision, execution, {"watchlist": []}, account)

    assert row["quantity"] == 25            # executed, not 3
    assert row["intended_quantity"] == 3    # decision intent preserved
    assert row["entry_price"] == 395.0      # filled avg, not 394.82
    assert row["open_positions"] == 1       # filtered
    assert row["open_positions_raw"] == 20  # equity 1 + raw crypto 19


def test_journal_skip_falls_back_to_decision(tmp_path, monkeypatch):
    """On a SKIP (no order placed) there is no executed qty — keep decision's."""
    jfile = tmp_path / "trades.jsonl"
    monkeypatch.setattr(logmod, "JOURNAL_FILE", jfile)
    monkeypatch.setattr(logmod, "JOURNAL_DIR", tmp_path)

    decision = {"action": "SKIP", "symbol": None, "quantity": None}
    execution = {"status": "SKIPPED"}
    account = {"portfolio_value": 99000, "open_positions": 2}

    j = TradeJournal()
    row = j.log_decision(decision, execution, {"watchlist": []}, account)
    assert row["execution_status"] == "SKIPPED"
    assert row["quantity"] is None
    assert row["open_positions_raw"] == 2
