"""
Tests for H-6: every execution_status written to trades.jsonl must be in
the canonical vocabulary {FILLED, PARTIAL, SUBMITTED, CANCELLED, REJECTED,
SKIPPED, ERROR, SIMULATED}.

Audit 2026-05-26 H-6 found "ORDERSTATUS.FILLED" (leaked enum repr) and
"ERROR" (undefined at the time) in the journal; downstream filters keyed
on the canonical set silently dropped these rows.
"""
import pytest

from journal.logger import _normalize_execution_status, _CANONICAL_STATUSES


# ── Direct canonical pass-through ────────────────────────────────────────────

@pytest.mark.parametrize("s", sorted(_CANONICAL_STATUSES))
def test_canonical_values_pass_through(s):
    assert _normalize_execution_status(s) == s


def test_lowercase_normalized_to_upper():
    assert _normalize_execution_status("filled") == "FILLED"
    assert _normalize_execution_status("submitted") == "SUBMITTED"


def test_whitespace_stripped():
    assert _normalize_execution_status("  FILLED  ") == "FILLED"


# ── Alpaca SDK enum leakage (the actual H-6 cause) ───────────────────────────

def test_enum_repr_orderstatus_filled():
    assert _normalize_execution_status("OrderStatus.FILLED") == "FILLED"


def test_enum_repr_uppercased():
    assert _normalize_execution_status("ORDERSTATUS.FILLED") == "FILLED"


def test_enum_repr_unknown_class_still_yields_last_segment():
    # Defensive: any "Foo.BAR" form should at least try the last segment.
    assert _normalize_execution_status("SomeWeirdClass.CANCELLED") == "CANCELLED"


# ── SDK synonyms / known quirks ──────────────────────────────────────────────

def test_partially_filled_maps_to_partial():
    assert _normalize_execution_status("PARTIALLY_FILLED") == "PARTIAL"
    assert _normalize_execution_status("PartiallyFilled") == "PARTIAL"


def test_canceled_us_spelling_maps_to_cancelled():
    assert _normalize_execution_status("CANCELED") == "CANCELLED"


def test_expired_and_replaced_map_to_cancelled():
    assert _normalize_execution_status("EXPIRED") == "CANCELLED"
    assert _normalize_execution_status("REPLACED") == "CANCELLED"


# ── Missing / unknown values ─────────────────────────────────────────────────

def test_none_becomes_error():
    assert _normalize_execution_status(None) == "ERROR"


def test_empty_string_becomes_error():
    assert _normalize_execution_status("") == "ERROR"


def test_na_string_becomes_error():
    assert _normalize_execution_status("N/A") == "ERROR"
    assert _normalize_execution_status("NA") == "ERROR"


def test_unknown_token_becomes_error_not_passed_through():
    # Garbage must not silently propagate into the journal.
    assert _normalize_execution_status("WAT") == "ERROR"


# ── Executor-side Alpaca status normaliser (H-2 follow-up, audit 2026-06-10) ──
# str(order.status) can be "OrderStatus.FILLED"; the executor's status_map and
# terminal-state set key on lower-case bare tokens. A live MSFT fill on
# 2026-06-16 was mislabelled SUBMITTED because the enum prefix wasn't stripped.

from agents.executor import _normalize_alpaca_status


@pytest.mark.parametrize("raw,expected", [
    ("OrderStatus.FILLED", "filled"),
    ("ORDERSTATUS.FILLED", "filled"),
    ("filled", "filled"),
    ("FILLED", "filled"),
    ("OrderStatus.PENDING_NEW", "pending_new"),
    ("accepted", "accepted"),
    ("OrderStatus.CANCELED", "canceled"),
])
def test_normalize_alpaca_status(raw, expected):
    assert _normalize_alpaca_status(raw) == expected
