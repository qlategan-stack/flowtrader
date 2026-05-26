"""
Regression tests for the 14:55 EST equity-close cutoff parser in
agents.decision.TradingDecisionAgent.analyze_market.

Bug history: the original parser did
    timestamp_str.split()[-1]          # split on whitespace
    .split(':')[:2]                    # split on colon
    int(hour_str)
which fails on ISO 8601 strings (e.g. '2026-05-26T10:19:48.721989-04:00')
because there is no whitespace — split()[-1] returns the whole string and
the first colon-segment 'YYYY-MM-DDTHH' is not an int. The except clause
swallowed the failure and the BUY proceeded. Audit 2026-05-26 §C-2.
"""
from unittest.mock import MagicMock

import pytest

from agents.decision import TradingDecisionAgent


BUY_JSON = (
    '```json\n'
    '{"action":"BUY","symbol":"NVDA","quantity":5,"entry_price":100,'
    '"stop_loss":99,"take_profit":102,"signal_score":3,'
    '"signals_fired":["RSI<32"],"confidence":"HIGH",'
    '"reasoning":"x","journal_entry":"x"}\n'
    '```'
)


def _agent_returning_buy() -> TradingDecisionAgent:
    """Mocked agent whose Claude client always returns the BUY_JSON above."""
    agent = TradingDecisionAgent.__new__(TradingDecisionAgent)
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=BUY_JSON)]
    agent.client = MagicMock()
    agent.client.messages.create.return_value = fake_response
    agent.model = "test-model"
    agent.system_prompt = "test"
    agent.research_memo = {}
    agent._profile_name = "test"
    agent._profile = {"min_signal_score": 1, "max_open_positions": 5}
    agent._min_score = 1
    return agent


def _decide(timestamp_str: str) -> dict:
    agent = _agent_returning_buy()
    return agent.analyze_market(
        {"watchlist": [], "timestamp": timestamp_str},
        {"account_value": 100000, "buying_power": 100000, "positions": []},
    )


# ── Happy-path: pre-cutoff entries stay as BUY ────────────────────────────────

def test_iso_with_tz_before_cutoff_stays_buy():
    # The exact format that broke the old parser — should now parse cleanly.
    d = _decide("2026-05-26T10:19:48.721989-04:00")
    assert d["action"] == "BUY"


def test_iso_with_tz_at_1454_stays_buy():
    d = _decide("2026-05-26T14:54:59-04:00")
    assert d["action"] == "BUY"


def test_legacy_space_format_before_cutoff_stays_buy():
    # Older 'YYYY-MM-DD HH:MM:SS' form (Python 3.11+ fromisoformat accepts).
    d = _decide("2026-05-26 13:30:00")
    assert d["action"] == "BUY"


# ── Cutoff: post-14:55 entries become SKIP ────────────────────────────────────

def test_iso_with_tz_at_1455_becomes_skip():
    d = _decide("2026-05-26T14:55:00-04:00")
    assert d["action"] == "SKIP"
    assert "market close cutoff" in d["rejection_reason"]


def test_iso_with_tz_at_2019_becomes_skip():
    # The audit found 65 SUBMITTED at times like 20:19 — must be caught now.
    d = _decide("2026-05-26T20:19:00-04:00")
    assert d["action"] == "SKIP"
    assert "14:55" in d["rejection_reason"]


def test_iso_with_tz_at_2349_becomes_skip():
    d = _decide("2026-05-26T23:49:00-04:00")
    assert d["action"] == "SKIP"


# ── Timezone-aware: UTC-08:00 timestamp must be converted to EST before check ─

def test_utc_timestamp_converted_to_est_before_cutoff():
    # 19:00 UTC = 15:00 EDT → after 14:55 → SKIP.
    d = _decide("2026-05-26T19:00:00+00:00")
    assert d["action"] == "SKIP"


def test_utc_timestamp_converted_to_est_stays_buy():
    # 18:00 UTC = 14:00 EDT → before 14:55 → BUY.
    d = _decide("2026-05-26T18:00:00+00:00")
    assert d["action"] == "BUY"


# ── Graceful handling of bad / missing timestamps ─────────────────────────────

def test_empty_timestamp_does_not_block_buy():
    # No timestamp → can't check, but must NOT crash and must NOT silently SKIP.
    d = _decide("")
    assert d["action"] == "BUY"


def test_garbage_timestamp_does_not_crash():
    # Unparseable input — log warning, fall through. BUY proceeds (logged).
    d = _decide("not-a-timestamp")
    assert d["action"] == "BUY"
