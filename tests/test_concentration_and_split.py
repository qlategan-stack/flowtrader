"""
Tests for M-7 (concentration cap) and M-2 (per-class decision split),
audit 2026-06-10.
"""
import json
import pytest

main = __import__("main")


def _journal(tmp_path, rows):
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


# ── M-7: _symbols_over_concentration_cap ─────────────────────────────────────

def test_concentration_flags_dominant_symbol(tmp_path):
    rows = [{"date": "2026-06-15", "action": "BUY", "execution_status": "FILLED", "symbol": "ETH/USDT"} for _ in range(5)]
    rows += [{"date": "2026-06-15", "action": "BUY", "execution_status": "FILLED", "symbol": s}
             for s in ["BTC/USDT", "SOL/USDT", "XRP/USDT", "LTC/USDT", "ADA/USDT"]]
    j = _journal(tmp_path, rows)
    over = main._symbols_over_concentration_cap(window_days=30, max_share=0.40, min_fills=8,
                                                journal_file=j, today_est="2026-06-16")
    assert over == {"ETH/USDT"}  # 5/10 = 50% >= 40%


def test_concentration_noop_below_min_fills(tmp_path):
    # 1/1 = 100% but below min_fills -> must NOT lock out the only symbol.
    rows = [{"date": "2026-06-15", "action": "BUY", "execution_status": "FILLED", "symbol": "ETH/USDT"}]
    j = _journal(tmp_path, rows)
    assert main._symbols_over_concentration_cap(min_fills=8, journal_file=j, today_est="2026-06-16") == set()


def test_concentration_disabled_at_share_1(tmp_path):
    rows = [{"date": "2026-06-15", "action": "BUY", "execution_status": "FILLED", "symbol": "ETH/USDT"} for _ in range(20)]
    j = _journal(tmp_path, rows)
    assert main._symbols_over_concentration_cap(max_share=1.0, journal_file=j, today_est="2026-06-16") == set()


def test_concentration_only_counts_filled(tmp_path):
    # SKIPs / SUBMITTED must not count toward fills.
    rows = [{"date": "2026-06-15", "action": "BUY", "execution_status": "SKIPPED", "symbol": "ETH/USDT"} for _ in range(20)]
    j = _journal(tmp_path, rows)
    assert main._symbols_over_concentration_cap(min_fills=1, journal_file=j, today_est="2026-06-16") == set()


def test_concentration_window_excludes_old(tmp_path):
    rows = [{"date": "2026-01-01", "action": "BUY", "execution_status": "FILLED", "symbol": "ETH/USDT"} for _ in range(20)]
    j = _journal(tmp_path, rows)
    # 30d window ending 2026-06-16 excludes Jan -> nothing counts
    assert main._symbols_over_concentration_cap(window_days=30, journal_file=j, today_est="2026-06-16") == set()


# ── M-2: _asset_class / _per_class_decision_split ────────────────────────────

@pytest.mark.parametrize("sym,cls", [
    ("BTC/USDT", "crypto"), ("AAPL", "equity"), ("GLD", "commodity"),
    ("TLT", "commodity"), ("NVDA", "equity"), (None, "unknown"),
])
def test_asset_class(sym, cls):
    assert main._asset_class(sym) == cls


def test_per_class_split_counts_buy_and_skip(tmp_path):
    rows = [
        {"date": "2026-06-15", "action": "BUY", "symbol": "BTC/USDT", "execution_status": "FILLED"},
        {"date": "2026-06-15", "action": "SKIP", "symbol": None, "top_setup_symbol": "ETH/USDT"},
        {"date": "2026-06-15", "action": "BUY", "symbol": "AAPL", "execution_status": "FILLED"},
        {"date": "2026-06-15", "action": "SKIP", "symbol": None, "top_setup_symbol": "MSFT"},
        {"date": "2026-06-15", "action": "SKIP", "symbol": None, "top_setup_symbol": "NVDA"},
    ]
    j = _journal(tmp_path, rows)
    split = main._per_class_decision_split(window_days=30, journal_file=j, today_est="2026-06-16")
    assert split["crypto"] == {"BUY": 1, "SKIP": 1, "buy_pct": 50}
    assert split["equity"]["BUY"] == 1 and split["equity"]["SKIP"] == 2
    assert split["equity"]["buy_pct"] == 33
