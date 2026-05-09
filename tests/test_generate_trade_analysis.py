"""
Tests for scripts/generate_trade_analysis.py — the failure-detection,
pairing, summary, and HTML rendering logic. Claude integration is mocked.
"""
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from generate_trade_analysis import (  # noqa: E402
    Failure,
    find_failures,
    latest_trading_date,
    load_jsonl,
    pair_buys_with_sells,
    render_html,
    render_status_json,
    seven_day_summary,
)


def _entry(timestamp: str, action: str, symbol: str | None, status: str = "SUBMITTED",
           qty: float = 10, price: float = 100.0, **extra) -> dict:
    """Build a minimal journal entry."""
    date, time_part = timestamp.split("T")
    time_est = time_part.split(".")[0][:8]
    return {
        "timestamp":         timestamp,
        "date":              date,
        "time_est":          time_est,
        "action":            action,
        "symbol":            symbol,
        "execution_status":  status,
        "quantity":          qty,
        "entry_price":       price,
        "signal_score":      extra.pop("signal_score", 4),
        "signals_fired":     extra.pop("signals_fired", []),
        "confidence":        extra.pop("confidence", "MEDIUM"),
        "reasoning":         extra.pop("reasoning", "test reasoning"),
        "rejection_reason":  extra.pop("rejection_reason", ""),
        **extra,
    }


# ── load_jsonl ────────────────────────────────────────────────────────────────

def test_load_jsonl_returns_empty_for_missing(tmp_path):
    assert load_jsonl(tmp_path / "missing.jsonl") == []


def test_load_jsonl_skips_corrupt(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text('{"a":1}\nnot json{\n{"a":2}\n', encoding="utf-8")
    assert load_jsonl(p) == [{"a": 1}, {"a": 2}]


# ── latest_trading_date ───────────────────────────────────────────────────────

def test_latest_trading_date_returns_max_date_with_action():
    entries = [
        _entry("2026-05-07T10:00:00", "BUY", "AAPL"),
        _entry("2026-05-08T10:00:00", "BUY", "AAPL"),
        # Entry without action shouldn't count
        {"date": "2026-05-09", "action": None},
    ]
    assert latest_trading_date(entries) == "2026-05-08"


def test_latest_trading_date_returns_none_for_empty():
    assert latest_trading_date([]) is None


# ── pair_buys_with_sells ──────────────────────────────────────────────────────

def test_pair_buys_pairs_simple_round_trip():
    entries = [
        _entry("2026-05-08T10:00:00", "BUY",  "AAPL", qty=10, price=100.0),
        _entry("2026-05-08T11:00:00", "SELL", "AAPL", qty=10, price=110.0),
    ]
    closed = pair_buys_with_sells(entries)
    assert len(closed) == 1
    t = closed[0]
    assert t["symbol"] == "AAPL"
    assert t["realized_pl"] == pytest.approx(100.0)  # (110-100)*10
    assert t["pl_pct"] == pytest.approx(10.0)


def test_pair_buys_uses_fifo():
    entries = [
        _entry("2026-05-08T09:00:00", "BUY",  "AAPL", qty=5, price=100.0),
        _entry("2026-05-08T10:00:00", "BUY",  "AAPL", qty=5, price=110.0),
        _entry("2026-05-08T11:00:00", "SELL", "AAPL", qty=5, price=105.0),
    ]
    closed = pair_buys_with_sells(entries)
    assert len(closed) == 1
    # First BUY at $100 paired with SELL at $105 = +$25 (5 shares × $5)
    assert closed[0]["realized_pl"] == pytest.approx(25.0)


def test_pair_buys_skips_rejected_orders():
    """Rejected/errored orders shouldn't pair into closed trades."""
    entries = [
        _entry("2026-05-08T09:00:00", "BUY",  "AAPL", qty=10, price=100.0, status="REJECTED"),
        _entry("2026-05-08T10:00:00", "SELL", "AAPL", qty=10, price=110.0),
    ]
    assert pair_buys_with_sells(entries) == []


def test_pair_buys_handles_orphan_sell():
    """SELL with no preceding BUY is silently skipped."""
    entries = [
        _entry("2026-05-08T11:00:00", "SELL", "AAPL", qty=10, price=110.0),
    ]
    assert pair_buys_with_sells(entries) == []


# ── find_failures ─────────────────────────────────────────────────────────────

def test_find_failures_includes_losses_only_for_target_date():
    entries = [
        # Yesterday's losing trade (sell happened yesterday)
        _entry("2026-05-07T10:00:00", "BUY",  "AAPL", qty=10, price=100.0),
        _entry("2026-05-07T15:00:00", "SELL", "AAPL", qty=10, price=95.0),
        # Today's losing trade (we want this one)
        _entry("2026-05-08T09:00:00", "BUY",  "MSFT", qty=5, price=200.0),
        _entry("2026-05-08T15:00:00", "SELL", "MSFT", qty=5, price=190.0),
    ]
    failures = find_failures(entries, "2026-05-08")
    losses = [f for f in failures if f.kind == "loss"]
    assert len(losses) == 1
    assert losses[0].symbol == "MSFT"
    assert losses[0].realized_pl == pytest.approx(-50.0)


def test_find_failures_excludes_winning_trades():
    """Only losing closed trades count as failures."""
    entries = [
        _entry("2026-05-08T09:00:00", "BUY",  "AAPL", qty=10, price=100.0),
        _entry("2026-05-08T15:00:00", "SELL", "AAPL", qty=10, price=110.0),  # win
    ]
    assert find_failures(entries, "2026-05-08") == []


def test_find_failures_includes_execution_errors():
    entries = [
        _entry("2026-05-08T10:00:00", "BUY", "META", status="ERROR",
               rejection_reason="No symbol in decision"),
        _entry("2026-05-08T11:00:00", "BUY", "AMD",  status="REJECTED",
               rejection_reason="Position too large (97% of account)"),
    ]
    failures = find_failures(entries, "2026-05-08")
    assert len(failures) == 2
    assert all(f.kind == "exec_error" for f in failures)
    assert "No symbol" in failures[0].rejection_reason


def test_find_failures_excludes_skipped_sessions():
    """SKIP entries are not failures even though they're not trades."""
    entries = [
        _entry("2026-05-08T10:00:00", "SKIP", None, status="SKIPPED",
               rejection_reason="Action is SKIP - no order placed"),
    ]
    assert find_failures(entries, "2026-05-08") == []


def test_find_failures_sorted_by_timestamp():
    entries = [
        _entry("2026-05-08T15:00:00", "BUY", "X", status="ERROR", rejection_reason="late"),
        _entry("2026-05-08T09:00:00", "BUY", "Y", status="ERROR", rejection_reason="early"),
    ]
    failures = find_failures(entries, "2026-05-08")
    assert failures[0].time_est < failures[1].time_est


# ── seven_day_summary ─────────────────────────────────────────────────────────

def test_seven_day_summary_counts_trades_and_errors():
    entries = [
        # Win
        _entry("2026-05-05T10:00:00", "BUY",  "A", qty=10, price=100.0),
        _entry("2026-05-05T15:00:00", "SELL", "A", qty=10, price=110.0),
        # Loss
        _entry("2026-05-06T10:00:00", "BUY",  "B", qty=5, price=200.0),
        _entry("2026-05-06T15:00:00", "SELL", "B", qty=5, price=190.0),
        # Error
        _entry("2026-05-08T10:00:00", "BUY",  "C", status="REJECTED", rejection_reason="x"),
        # Outside window (8+ days before end_date)
        _entry("2026-04-25T10:00:00", "BUY",  "Z", status="ERROR"),
    ]
    s = seven_day_summary(entries, "2026-05-08")
    assert s["closed_trades"] == 2
    assert s["wins"] == 1
    assert s["losses"] == 1
    assert s["exec_errors"] == 1
    assert s["win_rate_pct"] == pytest.approx(50.0)
    assert s["total_realized"] == pytest.approx(50.0)  # +100 - 50


def test_seven_day_summary_handles_zero_closed_trades():
    entries = [
        _entry("2026-05-08T10:00:00", "BUY", "C", status="REJECTED", rejection_reason="x"),
    ]
    s = seven_day_summary(entries, "2026-05-08")
    assert s["closed_trades"] == 0
    assert s["win_rate_pct"] is None  # avoids divide by zero


# ── render_html ───────────────────────────────────────────────────────────────

def test_render_html_includes_required_brand_pieces():
    failures = [
        Failure(kind="loss", symbol="MSFT", date="2026-05-08", time_est="15:00:00",
                timestamp="2026-05-08T15:00:00",
                entry_price=200.0, exit_price=190.0, quantity=5,
                realized_pl=-50.0, pl_pct=-5.0, signals_fired=["RSI<32", "BelowBB"],
                signal_score=4, confidence="HIGH"),
    ]
    summary = seven_day_summary([], "2026-05-08")
    html = render_html(failures, summary, "2026-05-08")

    # Brand system markers
    assert "Barlow+Condensed" in html
    assert "theme-dark" in html
    assert 'onclick="ftTheme(' in html  # 4-button toggle helper
    assert "FlowTrader" in html and "Trade Analysis" in html

    # Failure data appears
    assert "MSFT" in html
    assert "$-50" in html or "-50.00" in html
    assert "RSI&lt;32" in html  # HTML-escaped signal name
    assert "SCORE 4/6" in html

    # Vertical tabs structure
    assert 'class="tabs-list"' in html
    assert 'data-target="f-0"' in html


def test_render_html_empty_state_when_no_failures():
    summary = seven_day_summary([], "2026-05-08")
    html = render_html([], summary, "2026-05-08")
    assert "No failures on" in html
    assert "2026-05-08" in html


def test_render_html_escapes_user_content():
    """Symbol or reasoning containing < > shouldn't break the HTML."""
    failures = [
        Failure(kind="exec_error", symbol="<script>alert('x')</script>",
                date="2026-05-08", time_est="10:00:00",
                timestamp="2026-05-08T10:00:00",
                rejection_reason="<x>"),
    ]
    summary = seven_day_summary([], "2026-05-08")
    html = render_html(failures, summary, "2026-05-08")
    assert "<script>alert" not in html  # raw injection blocked
    assert "&lt;script&gt;" in html      # escaped form present


# ── render_status_json ────────────────────────────────────────────────────────

def test_render_status_json_shape():
    failures = [
        Failure(kind="loss", symbol="MSFT", date="2026-05-08", time_est="15:00:00",
                timestamp="2026-05-08T15:00:00",
                realized_pl=-50.0, quantity=5),
        Failure(kind="exec_error", symbol="META", date="2026-05-08", time_est="10:00:00",
                timestamp="2026-05-08T10:00:00",
                rejection_reason="No symbol in decision"),
    ]
    summary = seven_day_summary([], "2026-05-08")
    s = render_status_json(failures, summary, "2026-05-08", "https://example.com/report")

    assert s["failure_count"] == 2
    assert s["loss_count"] == 1
    assert s["error_count"] == 1
    assert s["report_date"] == "2026-05-08"
    assert s["report_url"] == "https://example.com/report"
    assert "summary_7day" in s
    assert len(s["top_failures"]) == 2
    assert s["top_failures"][0]["symbol"] == "MSFT"
