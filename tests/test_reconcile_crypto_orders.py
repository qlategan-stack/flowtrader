"""
Tests for scripts/reconcile_crypto_orders.py — H-1 backfill helper.

Network calls are not exercised; we test the pure transformations:
- _ccxt_to_canonical:  CCXT status → canonical execution_status
- build_backfill_row:  original row + CCXT order → journal-shaped backfill
- load_submitted_crypto_orders: reads trades.jsonl and filters correctly
"""
import importlib.util
import json
from pathlib import Path

import pytest


# Dynamically load the script under test
def _import_reconciler():
    spec = importlib.util.spec_from_file_location(
        "reconcile_crypto_orders",
        Path(__file__).resolve().parents[1] / "scripts" / "reconcile_crypto_orders.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reconciler = _import_reconciler()


# ── _ccxt_to_canonical ──────────────────────────────────────────────────────

@pytest.mark.parametrize("ccxt,canonical", [
    ("closed",     "FILLED"),
    ("canceled",   "CANCELLED"),
    ("cancelled",  "CANCELLED"),
    ("expired",    "CANCELLED"),
    ("rejected",   "REJECTED"),
    ("open",       None),            # still live — caller should skip
    ("",           "ERROR"),
    ("partial",    "ERROR"),         # not a real CCXT status
])
def test_ccxt_to_canonical(ccxt, canonical):
    assert reconciler._ccxt_to_canonical(ccxt) == canonical


def test_ccxt_to_canonical_is_case_insensitive():
    assert reconciler._ccxt_to_canonical("CLOSED") == "FILLED"
    assert reconciler._ccxt_to_canonical("Canceled") == "CANCELLED"


# ── build_backfill_row ──────────────────────────────────────────────────────

def _orig(**overrides):
    base = {
        "session_id": "20260520_2049",
        "symbol":      "NEAR/USDT",
        "action":      "BUY",
        "order_id":    "abc123",
        "quantity":    520,
        "entry_price": 2.85,
        "stop_loss":   2.66,
        "take_profit": 3.23,
    }
    base.update(overrides)
    return base


def test_build_row_filled():
    order = {"status": "closed", "filled": 520, "average": 2.853}
    row = reconciler.build_backfill_row(_orig(), order)
    assert row["execution_status"] == "FILLED"
    assert row["filled_qty"] == 520
    assert row["filled_avg_price"] == 2.853
    assert row["symbol"] == "NEAR/USDT"
    assert row["order_id"] == "abc123"
    assert row["_backfill"] is True
    assert row["intended_entry"] == 2.85   # preserved from original
    assert row["paper_trade"] is True


def test_build_row_cancelled_no_fill():
    order = {"status": "canceled", "filled": 0, "average": None}
    row = reconciler.build_backfill_row(_orig(), order)
    assert row["execution_status"] == "CANCELLED"
    assert row["filled_qty"] == 0
    assert row["filled_avg_price"] is None


def test_build_row_open_returns_none():
    """Still-open orders must NOT produce a backfill row."""
    assert reconciler.build_backfill_row(_orig(), {"status": "open", "filled": 0}) is None


def test_build_row_links_original_session_via_marker():
    row = reconciler.build_backfill_row(_orig(session_id="X1"), {"status": "closed", "filled": 1, "average": 1})
    assert row["_original_session"] == "X1"
    assert row["session_id"]       == "X1"


# ── load_submitted_crypto_orders ────────────────────────────────────────────

def _write_journal(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_load_filters_to_submitted_crypto_within_window(tmp_path, monkeypatch):
    monkeypatch.setattr(reconciler, "JOURNAL_FILE", _write_journal(tmp_path, [
        # In-window crypto SUBMITTED — KEEP
        {"symbol": "NEAR/USDT", "execution_status": "SUBMITTED", "order_id": "n1",
         "timestamp": "2026-05-25T10:00:00+00:00"},
        # Equity SUBMITTED — DROP (no '/' in symbol)
        {"symbol": "AAPL", "execution_status": "SUBMITTED", "order_id": "a1",
         "timestamp": "2026-05-25T10:00:00+00:00"},
        # Crypto FILLED — DROP (already terminal)
        {"symbol": "BTC/USDT", "execution_status": "FILLED", "order_id": "b1",
         "timestamp": "2026-05-25T10:00:00+00:00"},
        # Crypto SUBMITTED but missing order_id — DROP (can't reconcile)
        {"symbol": "ETH/USDT", "execution_status": "SUBMITTED",
         "timestamp": "2026-05-25T10:00:00+00:00"},
        # Crypto SUBMITTED but older than window — DROP
        {"symbol": "LTC/USDT", "execution_status": "SUBMITTED", "order_id": "l1",
         "timestamp": "2020-01-01T10:00:00+00:00"},
    ]))
    result = reconciler.load_submitted_crypto_orders(days=365 * 5, symbol_filter=None)
    # All three valid candidates within 5 years: n1, plus the old l1 (cutoff stale)
    # but l1's 2020 stamp is >5y cutoff actually 5*365 = 1825 days back from now
    # We want a tight test — switch days=30 to verify cutoff drops l1
    result = reconciler.load_submitted_crypto_orders(days=30, symbol_filter=None)
    assert [r["order_id"] for r in result] == ["n1"]


def test_load_respects_symbol_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(reconciler, "JOURNAL_FILE", _write_journal(tmp_path, [
        {"symbol": "NEAR/USDT", "execution_status": "SUBMITTED", "order_id": "n1",
         "timestamp": "2026-05-25T10:00:00+00:00"},
        {"symbol": "ETH/USDT",  "execution_status": "SUBMITTED", "order_id": "e1",
         "timestamp": "2026-05-25T10:00:00+00:00"},
    ]))
    result = reconciler.load_submitted_crypto_orders(days=365, symbol_filter="ETH/USDT")
    assert [r["order_id"] for r in result] == ["e1"]


def test_load_tolerates_malformed_json_lines(tmp_path, monkeypatch):
    p = tmp_path / "trades.jsonl"
    p.write_text(
        '{"symbol":"NEAR/USDT","execution_status":"SUBMITTED","order_id":"n1","timestamp":"2026-05-25T10:00:00+00:00"}\n'
        "this is not json\n"
        "\n"
        '{"symbol":"ETH/USDT","execution_status":"SUBMITTED","order_id":"e1","timestamp":"2026-05-25T10:00:00+00:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(reconciler, "JOURNAL_FILE", p)
    result = reconciler.load_submitted_crypto_orders(days=365, symbol_filter=None)
    assert sorted(r["order_id"] for r in result) == ["e1", "n1"]


def test_load_returns_empty_when_journal_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(reconciler, "JOURNAL_FILE", tmp_path / "nonexistent.jsonl")
    assert reconciler.load_submitted_crypto_orders(days=7, symbol_filter=None) == []
