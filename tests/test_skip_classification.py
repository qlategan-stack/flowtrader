"""
Tests for main._classify_skip — the L-4 structured SKIP categoriser.

Audit 2026-05-26 L-4: rejection_reason on SKIP rows was uniformly
"Action is SKIP — no order placed", forcing pattern-matching on free
text. _classify_skip produces a discrete kind for aggregation.
"""
import pytest

main = __import__("main")


def _decision(reasoning: str = "", rejection_reason: str = "", api_error: bool = False) -> dict:
    return {
        "action": "SKIP",
        "reasoning": reasoning,
        "rejection_reason": rejection_reason,
        "api_error": api_error,
    }


def _candidate(score=0, momentum=0, mode="NONE", regime="RANGING", overbought=False) -> dict:
    return {
        "symbol": "TEST",
        "indicators": {
            "signal_score":         score,
            "momentum_score":       momentum,
            "strategy_mode":        mode,
            "regime":               regime,
            "overbought_extension": overbought,
            "rsi":                  50,
            "adx":                  20,
        },
    }


# ── API_ERROR ──────────────────────────────────────────────────────────────

def test_api_error_short_circuits_classification():
    """An API failure trumps everything — the cycle is non-informative."""
    d = _decision(api_error=True, reasoning="best candidate has high score")
    assert main._classify_skip(d, _candidate(score=5), min_score=2) == main.SKIP_KIND_API_ERROR


# ── NO_CANDIDATE ───────────────────────────────────────────────────────────

def test_no_candidate_when_watchlist_empty():
    assert main._classify_skip(_decision(), None, min_score=2) == main.SKIP_KIND_NO_CANDIDATE


# ── OFF_WINDOW ─────────────────────────────────────────────────────────────

def test_off_window_from_reasoning():
    d = _decision(reasoning="Off-window — opens at 08:00 EST")
    assert main._classify_skip(d, _candidate(), min_score=2) == main.SKIP_KIND_OFF_WINDOW


def test_off_window_from_outside_window_phrase():
    d = _decision(rejection_reason="entry outside the trading window")
    assert main._classify_skip(d, _candidate(), min_score=2) == main.SKIP_KIND_OFF_WINDOW


# ── RR_BELOW_MIN ───────────────────────────────────────────────────────────

def test_rr_below_min_from_canonical_phrase():
    d = _decision(reasoning="SKIP: R:R below 1.5 minimum (computed: 0.83:1)")
    assert main._classify_skip(d, _candidate(score=3), min_score=2) == main.SKIP_KIND_RR_BELOW_MIN


def test_rr_below_min_alt_phrasing():
    d = _decision(reasoning="Minimum R:R not met — computed 1.2")
    assert main._classify_skip(d, _candidate(score=3), min_score=2) == main.SKIP_KIND_RR_BELOW_MIN


# ── MEMO_PAUSED ────────────────────────────────────────────────────────────

def test_memo_paused_strategy_gate():
    d = _decision(reasoning="STRATEGY GATE: Mean reversion is PAUSED for equities this week")
    assert main._classify_skip(d, _candidate(score=3), min_score=2) == main.SKIP_KIND_MEMO_PAUSED


def test_memo_paused_long_form():
    d = _decision(reasoning="Mean reversion is paused per the weekly brief; defaulting to SKIP")
    assert main._classify_skip(d, _candidate(score=3), min_score=2) == main.SKIP_KIND_MEMO_PAUSED


# ── DIRECTIONAL_GATE ───────────────────────────────────────────────────────

def test_directional_gate_from_explicit_tag():
    d = _decision(reasoning="DIRECTIONAL_GATE_FAILED: price above upper BB and RSI>55")
    assert main._classify_skip(d, _candidate(), min_score=2) == main.SKIP_KIND_DIRECTIONAL_GATE


def test_directional_gate_from_indicator_flag():
    """Even if reasoning is sparse, the indicator flag triggers the classification."""
    d = _decision(reasoning="SKIP")
    cand = _candidate(score=3, overbought=True)
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_DIRECTIONAL_GATE


# ── LOW_SCORE vs TRENDING_REGIME ───────────────────────────────────────────

def test_low_score_ranging_regime():
    d = _decision(reasoning="Best candidate score below threshold")
    cand = _candidate(score=1, momentum=1, regime="RANGING")
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_LOW_SCORE


def test_trending_regime_split_out():
    """Same low score but in a trending regime — distinguishes ADX-veto skips."""
    d = _decision(reasoning="Best candidate score below threshold")
    cand = _candidate(score=1, momentum=1, regime="TRENDING")
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_TRENDING_REGIME


def test_score_at_threshold_not_low_score():
    """Score == min_score should NOT be LOW_SCORE — the rule passed."""
    d = _decision(reasoning="SKIP per Claude's judgement")
    cand = _candidate(score=2, regime="RANGING")
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_OTHER


# ── OTHER fallback ─────────────────────────────────────────────────────────

def test_other_when_nothing_matches():
    """Catch-all: real candidate, no recognisable reason — surface as OTHER for review."""
    d = _decision(reasoning="Claude chose to wait for a better setup")
    cand = _candidate(score=3, regime="RANGING")
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_OTHER


# ── Priority order ─────────────────────────────────────────────────────────

def test_api_error_priority_over_other_signals():
    """API_ERROR wins even if candidate would otherwise classify as LOW_SCORE."""
    d = _decision(api_error=True, reasoning="below threshold")
    cand = _candidate(score=0)
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_API_ERROR


def test_rr_priority_over_low_score():
    """When BOTH a low score AND an R:R failure are present, R:R is more specific."""
    d = _decision(reasoning="R:R below 1.5 minimum (computed: 0.5:1) and score=1 below threshold")
    cand = _candidate(score=1, regime="RANGING")
    assert main._classify_skip(d, cand, min_score=2) == main.SKIP_KIND_RR_BELOW_MIN
