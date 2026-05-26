"""
Tests for the H-2 fix: _compute_held_symbols filters the watchlist by live
holdings (equity + crypto) before Claude sees it, so "Already hold" cancels
stop burning cycles.

Audit history: 2026-05-26 H-2 — `journal/already_held_cooldown.json` had
{"NEAR/USDT": 2} yet 5 NEAR/USDT rejections fired in 24h because the 3-cycle
cooldown expired faster than the position closed.
"""
import sys
from unittest.mock import MagicMock, patch

import pytest


# Make `main` importable without running the bot's setup-on-import
def _import_main():
    return __import__("main")


def test_equity_position_added_to_held_set():
    main = _import_main()
    account = {"positions": [{"symbol": "AAPL", "qty": 10}, {"symbol": "MSFT", "qty": 5}]}
    held = main._compute_held_symbols(account, watchlist_symbols=["AAPL", "MSFT", "NVDA"])
    assert held == {"AAPL", "MSFT"}


def test_zero_qty_equity_not_held():
    main = _import_main()
    account = {"positions": [{"symbol": "AAPL", "qty": 0}]}
    assert main._compute_held_symbols(account, watchlist_symbols=["AAPL"]) == set()


def test_no_positions_no_crypto_in_watchlist_returns_empty():
    main = _import_main()
    # No crypto pairs in the watchlist → no crypto client call attempted
    assert main._compute_held_symbols({"positions": []}, watchlist_symbols=["AAPL", "MSFT"]) == set()


def test_crypto_position_matches_watchlist_pair():
    main = _import_main()
    with patch("agents.executor._get_crypto_client") as mock_client:
        mock_client.return_value.get_balance.return_value = {
            "positions": [
                {"currency": "NEAR", "value_usd": 1500.0},
                {"currency": "LTC", "value_usd": 800.0},
            ]
        }
        held = main._compute_held_symbols({}, watchlist_symbols=["NEAR/USDT", "LTC/USDT", "BTC/USDT"])
        assert held == {"NEAR/USDT", "LTC/USDT"}


def test_crypto_dust_below_10_usd_ignored():
    main = _import_main()
    with patch("agents.executor._get_crypto_client") as mock_client:
        mock_client.return_value.get_balance.return_value = {
            "positions": [
                {"currency": "NEAR", "value_usd": 1500.0},
                {"currency": "DOT",  "value_usd": 4.99},   # dust — ignored
                {"currency": "LINK", "value_usd": 10.0},   # boundary — held
            ]
        }
        held = main._compute_held_symbols({}, watchlist_symbols=["NEAR/USDT", "DOT/USDT", "LINK/USDT"])
        assert held == {"NEAR/USDT", "LINK/USDT"}


def test_crypto_balance_only_filters_symbols_in_watchlist():
    """If we hold SHIB faucet dust but SHIB isn't in our trading watchlist,
    it must not appear in the held set."""
    main = _import_main()
    with patch("agents.executor._get_crypto_client") as mock_client:
        mock_client.return_value.get_balance.return_value = {
            "positions": [{"currency": "SHIB", "value_usd": 50.0}]
        }
        held = main._compute_held_symbols({}, watchlist_symbols=["NEAR/USDT"])
        assert held == set()


def test_crypto_balance_fetch_failure_degrades_to_equity_only():
    main = _import_main()
    account = {"positions": [{"symbol": "AAPL", "qty": 10}]}
    with patch("agents.executor._get_crypto_client") as mock_client:
        mock_client.return_value.get_balance.side_effect = RuntimeError("exchange down")
        held = main._compute_held_symbols(account, watchlist_symbols=["AAPL", "NEAR/USDT"])
        # Equity still filtered; crypto check failed silently
        assert held == {"AAPL"}


def test_combined_equity_and_crypto():
    main = _import_main()
    account = {"positions": [{"symbol": "AAPL", "qty": 10}]}
    with patch("agents.executor._get_crypto_client") as mock_client:
        mock_client.return_value.get_balance.return_value = {
            "positions": [{"currency": "NEAR", "value_usd": 1500.0}]
        }
        held = main._compute_held_symbols(account, watchlist_symbols=["AAPL", "MSFT", "NEAR/USDT", "BTC/USDT"])
        assert held == {"AAPL", "NEAR/USDT"}
