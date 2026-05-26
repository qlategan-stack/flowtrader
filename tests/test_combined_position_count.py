"""
Tests for H-3: max_open_positions must count equities + crypto together.

Audit 2026-05-26 H-3: positions dashboard showed 4 equity + 4 crypto = ~8
holdings but the decision agent said "3 positions already open (5 max,
2 slots remaining)" because the gate read account.open_positions, which
was equity-only.
"""
import pytest

main = __import__("main")


# ── _significant_crypto_positions ────────────────────────────────────────────

def test_significant_crypto_positions_filters_dust():
    bal = {"positions": [
        {"currency": "NEAR", "value_usd": 1500.0},
        {"currency": "SHIB", "value_usd": 0.001},     # dust
        {"currency": "LTC", "value_usd": 800.0},
        {"currency": "DOT", "value_usd": 4.99},        # below 10
        {"currency": "LINK", "value_usd": 10.0},       # boundary
    ]}
    result = main._significant_crypto_positions(bal)
    coins = [p["currency"] for p in result]
    assert sorted(coins) == ["LINK", "LTC", "NEAR"]


def test_significant_crypto_positions_empty_balance_returns_empty_list():
    assert main._significant_crypto_positions({}) == []
    assert main._significant_crypto_positions({"positions": []}) == []


def test_significant_crypto_positions_custom_min_usd():
    bal = {"positions": [
        {"currency": "A", "value_usd": 50.0},
        {"currency": "B", "value_usd": 99.0},
        {"currency": "C", "value_usd": 100.0},
    ]}
    result = main._significant_crypto_positions(bal, min_usd=100.0)
    assert [p["currency"] for p in result] == ["C"]


# ── _augment_account_with_combined_positions ─────────────────────────────────

def test_augment_combines_equity_and_crypto():
    account = {"open_positions": 3}
    crypto_bal = {"positions": [
        {"currency": "NEAR", "value_usd": 1500.0},
        {"currency": "LTC", "value_usd": 800.0},
    ]}
    out = main._augment_account_with_combined_positions(account, crypto_bal)
    assert out["equity_positions"] == 3
    assert out["crypto_positions"] == 2
    assert out["open_positions"] == 5      # combined — was 3 before
    # H-3 scenario from the audit: equity=4 + crypto=4 = 8, breaches cap of 5


def test_augment_handles_no_crypto():
    account = {"open_positions": 2}
    out = main._augment_account_with_combined_positions(account, {})
    assert out["equity_positions"] == 2
    assert out["crypto_positions"] == 0
    assert out["open_positions"] == 2


def test_augment_handles_no_equity():
    account = {"open_positions": 0}
    crypto_bal = {"positions": [{"currency": "BTC", "value_usd": 50000.0}]}
    out = main._augment_account_with_combined_positions(account, crypto_bal)
    assert out["equity_positions"] == 0
    assert out["crypto_positions"] == 1
    assert out["open_positions"] == 1


def test_augment_excludes_crypto_dust_from_count():
    """Faucet dust must NOT inflate the position count and trigger a false
    cap breach. (H-4 evidence showed 30 dust 'positions' on Binance testnet.)"""
    account = {"open_positions": 1}
    crypto_bal = {"positions": [
        {"currency": "NEAR", "value_usd": 1500.0},   # real
        {"currency": "SHIB", "value_usd": 0.5},      # dust
        {"currency": "TRY", "value_usd": 2.0},       # fiat dust
        {"currency": "PEPE", "value_usd": 0.001},    # meme dust
    ]}
    out = main._augment_account_with_combined_positions(account, crypto_bal)
    assert out["crypto_positions"] == 1   # only NEAR counts
    assert out["open_positions"] == 2     # equity 1 + NEAR 1


def test_augment_mutates_account_in_place():
    account = {"open_positions": 1}
    main._augment_account_with_combined_positions(account, {"positions": [{"currency": "X", "value_usd": 100}]})
    assert account["open_positions"] == 2   # mutated
    assert account["crypto_positions"] == 1


def test_augment_handles_none_open_positions():
    account = {}
    out = main._augment_account_with_combined_positions(account, {})
    assert out["open_positions"] == 0
    assert out["equity_positions"] == 0
    assert out["crypto_positions"] == 0
