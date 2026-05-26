"""
Tests for agents/_claude_retry.py — the shared Claude client retry helper.

M-3 (audit 2026-05-26): analyst_out / analyst_in were single-shot, so any
429 from the 30k-token/min rate limit aborted the suggestion pipeline.
This helper retries 3x on transient errors and fails fast on permanent ones.
"""
from unittest.mock import MagicMock

import pytest

from agents._claude_retry import call_with_retry, classify_api_error


# ── classify_api_error ──────────────────────────────────────────────────────

def test_classify_429_status():
    e = Exception("rate limited")
    e.status_code = 429
    assert classify_api_error(e) == "rate_limit"


def test_classify_429_text_only():
    assert classify_api_error(Exception("Error code: 429 rate_limit_error")) == "rate_limit"


def test_classify_auth_401():
    e = Exception("bad")
    e.status_code = 401
    assert classify_api_error(e) == "auth"


def test_classify_credit_exhausted():
    assert classify_api_error(Exception("Your credit balance is too low")) == "credit_exhausted"


def test_classify_connection():
    assert classify_api_error(Exception("Connection refused")) == "connection"


def test_classify_unknown_other():
    assert classify_api_error(Exception("¯\\_(ツ)_/¯")) == "other"


# ── call_with_retry ─────────────────────────────────────────────────────────

def _mock_client(side_effect_list):
    client = MagicMock()
    client.messages.create.side_effect = side_effect_list
    return client


def _ok_response(text="OK"):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


def test_first_attempt_success_no_sleep():
    sleeps = []
    client = _mock_client([_ok_response("hello")])
    text, kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text == "hello"
    assert kind is None
    assert sleeps == []
    assert client.messages.create.call_count == 1


def test_retries_then_succeeds_on_rate_limit():
    sleeps = []
    err = Exception("429 rate_limit")
    err.status_code = 429
    client = _mock_client([err, _ok_response("recovered")])
    text, kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text == "recovered"
    assert kind is None
    assert sleeps == [30]      # rate-limit backoff used
    assert client.messages.create.call_count == 2


def test_retries_use_short_backoff_for_connection():
    sleeps = []
    err = Exception("Connection timed out")
    client = _mock_client([err, _ok_response("ok")])
    text, _kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text == "ok"
    assert sleeps == [5]       # short backoff for transient


def test_gives_up_after_max_attempts_on_persistent_429():
    sleeps = []
    err = Exception("429 rate_limit")
    err.status_code = 429
    client = _mock_client([err, err, err])
    text, kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text is None
    assert kind == "rate_limit"
    assert sleeps == [30, 30]  # two sleeps for 3 attempts
    assert client.messages.create.call_count == 3


def test_fails_fast_on_auth_no_retry():
    sleeps = []
    err = Exception("authentication failed")
    err.status_code = 401
    client = _mock_client([err])
    text, kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text is None
    assert kind == "auth"
    assert sleeps == []
    assert client.messages.create.call_count == 1


def test_fails_fast_on_credit_exhausted_no_retry():
    sleeps = []
    client = _mock_client([Exception("Your credit balance is too low to access the Anthropic API")])
    text, kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text is None
    assert kind == "credit_exhausted"
    assert sleeps == []


def test_custom_max_attempts():
    sleeps = []
    err = Exception("Connection refused")
    client = _mock_client([err, _ok_response("ok")])
    text, _kind = call_with_retry(
        client, agent_name="T", model="m", system="s", user_content="u",
        max_attempts=2,
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert text == "ok"
