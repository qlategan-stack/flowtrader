"""
Tests for journal.api_alert_state — the rate-limit decision logic that
prevents Telegram alert flooding when Anthropic returns the same error
on every cycle.
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

from journal.api_alert_state import should_alert, record_alert


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "last_api_alert.json"


def _t(hour: int = 12) -> datetime:
    return datetime(2026, 5, 9, hour, 0, tzinfo=timezone.utc)


def test_alerts_when_no_state_file_exists(state_path):
    assert should_alert(state_path, "credit_exhausted", now=_t()) is True


def test_alerts_when_state_file_corrupt(state_path):
    state_path.write_text("not json{{{", encoding="utf-8")
    assert should_alert(state_path, "credit_exhausted", now=_t()) is True


def test_does_not_alert_again_within_cooldown(state_path):
    t0 = _t(hour=10)
    record_alert(state_path, "credit_exhausted", now=t0)
    t1 = _t(hour=11)
    assert should_alert(state_path, "credit_exhausted", cooldown_hours=24, now=t1) is False


def test_alerts_again_when_cooldown_elapses(state_path):
    t0 = _t(hour=10)
    record_alert(state_path, "credit_exhausted", now=t0)
    t1 = t0 + timedelta(hours=24)
    assert should_alert(state_path, "credit_exhausted", cooldown_hours=24, now=t1) is True


def test_alerts_immediately_when_error_kind_changes(state_path):
    t0 = _t(hour=10)
    record_alert(state_path, "credit_exhausted", now=t0)
    t1 = _t(hour=11)
    assert should_alert(state_path, "rate_limit", cooldown_hours=24, now=t1) is True


def test_record_alert_resets_first_seen_on_kind_change(state_path):
    t0 = _t(hour=10)
    record_alert(state_path, "credit_exhausted", now=t0)
    t1 = _t(hour=11)
    record_alert(state_path, "rate_limit", now=t1)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["kind"] == "rate_limit"
    assert state["first_seen"] == t1.isoformat()
    assert state["last_alerted"] == t1.isoformat()


def test_record_alert_preserves_first_seen_on_same_kind(state_path):
    t0 = _t(hour=10)
    record_alert(state_path, "credit_exhausted", now=t0)
    t1 = _t(hour=11)
    record_alert(state_path, "credit_exhausted", now=t1)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["first_seen"] == t0.isoformat()
    assert state["last_alerted"] == t1.isoformat()


def test_record_alert_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "state.json"
    record_alert(nested, "credit_exhausted", now=_t())
    assert nested.exists()
