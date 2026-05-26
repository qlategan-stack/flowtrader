"""
agents/_claude_retry.py
Shared Anthropic client wrapper that retries on transient errors (429
rate-limit, connection, timeout) and fails fast on permanent ones
(auth, credit exhausted).

M-3 fix (audit 2026-05-26): the analyst_out and analyst_in agents made a
single Claude call with no retry, so any 429 from the 30k-token/min rate
limit produced an empty suggestion set and aborted the weekly research
memo (M-4). The decision agent already had inline retry; this module
extracts that pattern so both analysts share it without copy-paste drift.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_NO_RETRY_KINDS  = {"auth", "credit_exhausted"}
_MAX_ATTEMPTS    = 3
_BACKOFF_S       = 5     # connection / timeout / other
_RATE_BACKOFF_S  = 30    # rate-limit


def classify_api_error(exc: Exception) -> str:
    """Classify an Anthropic SDK exception into a stable kind string.
    String/status-based to avoid coupling to specific SDK exception classes."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)

    if "credit balance" in msg or "billing" in msg:
        return "credit_exhausted"
    if status == 401 or "authentication" in msg or "invalid api key" in msg:
        return "auth"
    if status == 429 or "rate limit" in msg or "rate_limit" in msg:
        return "rate_limit"
    if "connection" in msg or "timeout" in msg or "timed out" in msg:
        return "connection"
    return "other"


def call_with_retry(
    client,
    *,
    agent_name: str,
    model: str,
    system: str,
    user_content: str,
    max_tokens: int = 4000,
    max_attempts: int = _MAX_ATTEMPTS,
    sleep_fn=time.sleep,        # injectable for tests
) -> tuple[str | None, str | None]:
    """
    Send a single-message Claude request with retry on transient failures.

    Returns (text, error_kind):
      - (text, None)   on success
      - (None, kind)   on permanent failure or after exhausting retries
        where `kind` is one of:
        rate_limit / connection / auth / credit_exhausted / other.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            return response.content[0].text, None
        except Exception as e:
            last_exc = e
            kind = classify_api_error(e)
            if kind in _NO_RETRY_KINDS:
                logger.error(f"{agent_name}: Claude API error (no retry — {kind}): {e}")
                return None, kind
            if attempt < max_attempts:
                wait = _RATE_BACKOFF_S if kind == "rate_limit" else _BACKOFF_S
                logger.warning(
                    f"{agent_name}: Claude API error "
                    f"(attempt {attempt}/{max_attempts}, kind={kind}, retrying in {wait}s): {e}"
                )
                sleep_fn(wait)
            else:
                logger.error(
                    f"{agent_name}: Claude API error "
                    f"(attempt {attempt}/{max_attempts}, kind={kind}, giving up): {e}"
                )
    return None, classify_api_error(last_exc) if last_exc else "other"
