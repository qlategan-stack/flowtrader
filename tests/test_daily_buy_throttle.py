"""
Tests for the per-symbol daily BUY throttle in main._symbols_at_daily_buy_cap.

M-2 (audit 2026-05-26): ETH/USDT was 40% of all 30d BUY entries because a
hot signal kept re-firing every cycle. The throttle caps entries per symbol
per calendar day so attempts spread across the watchlist.
"""
import json
from pathlib import Path

import pytest

main = __import__("main")


def _write_journal(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


# ── Threshold behaviour ──────────────────────────────────────────────────────

def test_under_cap_not_throttled(tmp_path):
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
    ])
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == set()


def test_at_cap_throttled(tmp_path):
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "CANCELLED"},
    ])
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == {"ETH/USDT"}


def test_above_cap_throttled(tmp_path):
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
    ])
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == {"ETH/USDT"}


def test_multiple_symbols_independent_counts(tmp_path):
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "BTC/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "LTC/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "LTC/USDT", "execution_status": "SUBMITTED"},
    ])
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == {"ETH/USDT", "LTC/USDT"}


# ── Filtering rules ──────────────────────────────────────────────────────────

def test_only_today_counted(tmp_path):
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-25", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-25", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
    ])
    # Yesterday's two don't count against today's tally
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == set()


def test_sells_do_not_count(tmp_path):
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "SELL", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "SELL", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
    ])
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == set()


def test_skips_do_not_count(tmp_path):
    """A SKIP didn't reach the exchange so shouldn't count against the daily cap."""
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SUBMITTED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SKIPPED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "SKIPPED"},
    ])
    # Only the SUBMITTED counts; SKIPPED entries aren't real attempts
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == set()


def test_cancelled_and_rejected_count(tmp_path):
    """CANCELLED/REJECTED attempts still went to the executor and burned a cycle —
    they count toward the cap so the bot doesn't keep retrying the same symbol."""
    journal = _write_journal(tmp_path, [
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "CANCELLED"},
        {"date": "2026-05-26", "action": "BUY", "symbol": "ETH/USDT", "execution_status": "REJECTED"},
    ])
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=journal) == {"ETH/USDT"}


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_missing_journal_returns_empty(tmp_path):
    assert main._symbols_at_daily_buy_cap(
        today_est="2026-05-26", cap=2, journal_file=tmp_path / "nope.jsonl"
    ) == set()


def test_malformed_lines_tolerated(tmp_path):
    p = tmp_path / "trades.jsonl"
    p.write_text(
        '{"date":"2026-05-26","action":"BUY","symbol":"ETH/USDT","execution_status":"SUBMITTED"}\n'
        "not json\n"
        '{"date":"2026-05-26","action":"BUY","symbol":"ETH/USDT","execution_status":"SUBMITTED"}\n',
        encoding="utf-8",
    )
    assert main._symbols_at_daily_buy_cap(today_est="2026-05-26", cap=2, journal_file=p) == {"ETH/USDT"}


def test_default_cap_is_two(tmp_path):
    """Sanity-check that the module-level default cap (env override) is 2 unless overridden."""
    assert main._MAX_BUYS_PER_SYMBOL_PER_DAY == 2
