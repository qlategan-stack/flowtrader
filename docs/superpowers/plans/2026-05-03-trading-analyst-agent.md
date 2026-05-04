# Trading Analyst Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two specialized analyst agents (in-strategy and out-of-strategy) that review the trade journal daily, produce structured improvement suggestions with insight boxes, and allow one-click CLAUDE.md patching from a new Analyst tab on the Streamlit dashboard.

**Architecture:** A shared `SuggestionStore` persistence layer handles all JSONL read/write/update/action operations. Two independent agent classes (`InStrategyAnalyst`, `OutStrategyAnalyst`) each carry deeply specialized system prompts and call the Anthropic API to analyze the journal and produce suggestion objects. The Streamlit dashboard gains a 4th Analyst tab with suggestion cards, insight boxes, and approve/archive/cancel controls. GitHub Actions triggers `analyst-full` nightly at 17:30 EST weekdays.

**Tech Stack:** Python 3.11+, `anthropic>=0.34.0` (claude-sonnet-4-6), `yfinance` (macro data), `streamlit`, `pytest` + `unittest.mock`, JSONL for persistence.

**Spec:** `docs/superpowers/specs/2026-05-03-trading-analyst-agent-design.md`

---

## File Map

| File | Status | Responsibility |
|------|--------|----------------|
| `journal/suggestion_store.py` | CREATE | JSONL persistence: append, load, upsert (dedup by category), action, apply CLAUDE.md patch |
| `agents/analyst_in.py` | CREATE | In-strategy analyst: mean reversion deep system prompt, journal analysis, suggestion generation |
| `agents/analyst_out.py` | CREATE | Out-strategy analyst: broad methodology system prompt, yfinance macro data, suggestion generation |
| `main.py` | MODIFY | Add `analyst-in`, `analyst-out`, `analyst-full` CLI modes + Telegram notification |
| `dashboard.py` | MODIFY | Add 4th Analyst tab with suggestion cards, insight boxes, Run Now buttons, action controls |
| `.github/workflows/trading-bot.yml` | MODIFY | Add daily 17:30 EST analyst cron job |
| `requirements.txt` | MODIFY | Add `yfinance>=0.2.0` |
| `tests/__init__.py` | CREATE | Makes tests/ a Python package |
| `tests/test_suggestion_store.py` | CREATE | SuggestionStore unit tests |
| `tests/test_analyst_in.py` | CREATE | InStrategyAnalyst tests with mocked Anthropic client |
| `tests/test_analyst_out.py` | CREATE | OutStrategyAnalyst tests with mocked Anthropic client + yfinance |

---

## Task 1: SuggestionStore — Shared Persistence Layer

**Files:**
- Create: `journal/suggestion_store.py`
- Create: `tests/__init__.py`
- Create: `tests/test_suggestion_store.py`

The `SuggestionStore` is the foundation everything else builds on. It manages a JSONL file with append-only writes, full rewrites for updates, deduplication by category, lifecycle management (approve/archive/cancel), and CLAUDE.md patching via `str.replace`.

- [ ] **Step 1: Create `tests/__init__.py` and the failing tests**

```bash
# From trading-bot/trading-bot/
mkdir tests
```

Create `tests/__init__.py` (empty file).

Create `tests/test_suggestion_store.py`:

```python
# tests/test_suggestion_store.py
import json
import pytest
from pathlib import Path

from journal.suggestion_store import SuggestionStore


@pytest.fixture
def store(tmp_path):
    return SuggestionStore(tmp_path / "suggestions.jsonl")


def _make_suggestion(category: str = "rsi_threshold", status: str = "pending") -> dict:
    return {
        "id": "in-20260503-001",
        "type": "in_strategy",
        "status": status,
        "category": category,
        "priority": "high",
        "title": "Test suggestion",
        "analysis": "Some analysis with numbers: 14/22 trades",
        "rationale": "Some rationale",
        "insight": {
            "why_now": "Skip rate rising",
            "purpose": "Widen entry window",
            "expected_effect": "+6% trade frequency",
            "risks": "May admit lower-quality entries",
        },
        "current_rule": "RSI < 32: +2 points (strong oversold)",
        "proposed_rule": "RSI < 35: +2 points (strong oversold)",
        "proposed_claude_md_diff": "--- a\n+++ b\n-RSI < 32\n+RSI < 35\n",
        "confidence": 0.75,
        "supporting_data": {"trades_analyzed": 20, "win_rate_current": 0.47},
        "generated_at": "2026-05-03T17:00:00+00:00",
        "actioned_at": None,
        "actioned_by": None,
    }


def test_append_and_load_all(store):
    s = _make_suggestion()
    store.append(s)
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "in-20260503-001"


def test_load_all_empty_when_file_missing(store):
    assert store.load_all() == []


def test_find_pending_by_category_returns_match(store):
    store.append(_make_suggestion("rsi_threshold", "pending"))
    result = store.find_pending_by_category("rsi_threshold")
    assert result is not None
    assert result["category"] == "rsi_threshold"


def test_find_pending_by_category_ignores_actioned(store):
    store.append(_make_suggestion("rsi_threshold", "approved"))
    result = store.find_pending_by_category("rsi_threshold")
    assert result is None


def test_upsert_creates_new_when_no_existing_pending(store):
    s = _make_suggestion("rsi_threshold")
    suggestion_id = store.upsert(s)
    assert len(store.load_all()) == 1
    assert store.load_all()[0]["id"] == suggestion_id


def test_upsert_updates_existing_pending_same_category(store):
    store.upsert(_make_suggestion("rsi_threshold"))
    s2 = _make_suggestion("rsi_threshold")
    s2["title"] = "Updated title"
    s2["confidence"] = 0.9
    store.upsert(s2)
    records = store.load_all()
    assert len(records) == 1
    assert records[0]["title"] == "Updated title"
    assert records[0]["confidence"] == 0.9


def test_upsert_does_not_update_approved_record(store):
    approved = _make_suggestion("rsi_threshold", "approved")
    store.append(approved)
    store.upsert(_make_suggestion("rsi_threshold"))
    assert len(store.load_all()) == 2


def test_upsert_creates_separate_entries_for_different_categories(store):
    store.upsert(_make_suggestion("rsi_threshold"))
    s2 = _make_suggestion("adx_filter")
    s2["id"] = "in-20260503-002"
    store.upsert(s2)
    assert len(store.load_all()) == 2


def test_action_sets_status_and_timestamp(store):
    store.append(_make_suggestion())
    sid = store.load_all()[0]["id"]
    result = store.action(sid, "approved")
    assert result is True
    updated = store.load_all()[0]
    assert updated["status"] == "approved"
    assert updated["actioned_at"] is not None
    assert updated["actioned_by"] == "approved"


def test_action_returns_false_for_unknown_id(store):
    result = store.action("nonexistent-id", "approved")
    assert result is False


def test_action_cancelled_sets_status(store):
    store.append(_make_suggestion())
    sid = store.load_all()[0]["id"]
    store.action(sid, "cancelled")
    assert store.load_all()[0]["status"] == "cancelled"


def test_apply_to_claude_md_replaces_rule(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "- RSI < 32: +2 points (strong oversold)\n- ADX < 20 (ranging market): +1 point\n"
    )
    SuggestionStore.apply_to_claude_md(
        str(claude_md),
        "RSI < 32: +2 points (strong oversold)",
        "RSI < 35: +2 points (strong oversold)",
    )
    content = claude_md.read_text()
    assert "RSI < 35" in content
    assert "RSI < 32" not in content
    assert "ADX < 20" in content  # other rules untouched


def test_apply_to_claude_md_raises_if_rule_not_found(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("- Other rule\n")
    with pytest.raises(ValueError, match="not found in CLAUDE.md"):
        SuggestionStore.apply_to_claude_md(
            str(claude_md),
            "RSI < 32: +2 points",
            "RSI < 35: +2 points",
        )
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd trading-bot/trading-bot
python -m pytest tests/test_suggestion_store.py -v
```

Expected: `ModuleNotFoundError: No module named 'journal.suggestion_store'`

- [ ] **Step 3: Create `journal/suggestion_store.py`**

```python
# journal/suggestion_store.py
"""
Shared JSONL persistence for analyst suggestions.
Used by both InStrategyAnalyst and OutStrategyAnalyst.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_UPDATEABLE_FIELDS = [
    "title", "analysis", "rationale", "insight",
    "current_rule", "proposed_rule", "proposed_claude_md_diff",
    "confidence", "supporting_data", "generated_at", "priority",
]


class SuggestionStore:

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed suggestion record")
        return records

    def find_pending_by_category(self, category: str) -> Optional[dict]:
        for record in self.load_all():
            if record.get("category") == category and record.get("status") == "pending":
                return record
        return None

    def append(self, suggestion: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(suggestion) + "\n")

    def upsert(self, suggestion: dict) -> str:
        existing = self.find_pending_by_category(suggestion["category"])
        if existing:
            updates = {k: suggestion[k] for k in _UPDATEABLE_FIELDS if k in suggestion}
            self.update(existing["id"], updates)
            logger.info(f"Updated pending suggestion {existing['id']} for category {suggestion['category']}")
            return existing["id"]
        self.append(suggestion)
        logger.info(f"Created suggestion {suggestion['id']} for category {suggestion['category']}")
        return suggestion["id"]

    def update(self, suggestion_id: str, updates: dict) -> bool:
        records = self.load_all()
        found = False
        for record in records:
            if record["id"] == suggestion_id:
                record.update(updates)
                found = True
                break
        if not found:
            return False
        self._rewrite(records)
        return True

    def action(self, suggestion_id: str, action: str) -> bool:
        return self.update(suggestion_id, {
            "status": action,
            "actioned_at": datetime.now(timezone.utc).isoformat(),
            "actioned_by": action,
        })

    @staticmethod
    def apply_to_claude_md(claude_md_path: str, current_rule: str, proposed_rule: str) -> None:
        path = Path(claude_md_path)
        content = path.read_text(encoding="utf-8")
        if current_rule not in content:
            raise ValueError(f"Rule not found in CLAUDE.md: {current_rule!r}")
        updated = content.replace(current_rule, proposed_rule, 1)
        path.write_text(updated, encoding="utf-8")
        logger.info("CLAUDE.md patched successfully")

    def _rewrite(self, records: list[dict]) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_suggestion_store.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add journal/suggestion_store.py tests/__init__.py tests/test_suggestion_store.py
git commit -m "feat: add SuggestionStore persistence layer for analyst suggestions"
```

---

## Task 2: InStrategyAnalyst

**Files:**
- Create: `agents/analyst_in.py`
- Create: `tests/test_analyst_in.py`

The in-strategy analyst has a deep mean reversion system prompt and analyzes the trade journal to produce 1–3 high-impact parameter tuning suggestions. It reads `trades.jsonl` and the current `CLAUDE.md`, calls Claude, parses the JSON array response, and upserts each suggestion into `suggestions_in.jsonl`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_analyst_in.py`:

```python
# tests/test_analyst_in.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open


SAMPLE_JOURNAL_ENTRIES = [
    {
        "timestamp": "2026-05-01T10:30:00-05:00",
        "date": "2026-05-01",
        "action": "SKIP",
        "symbol": "NVDA",
        "signal_score": 2,
        "signals_fired": ["RSI<40"],
        "confidence": "LOW",
        "reasoning": "Signal score too low",
        "execution_status": "SKIPPED",
    }
] * 15  # 15 entries to pass the minimum threshold


SAMPLE_CLAUDE_RESPONSE = json.dumps([
    {
        "category": "rsi_threshold",
        "priority": "high",
        "title": "RSI threshold too conservative",
        "analysis": "14 of 22 skipped trades had RSI 32-36 and would have been profitable",
        "rationale": "Widening the threshold captures more high-quality setups",
        "insight": {
            "why_now": "Skip rate risen to 63%",
            "purpose": "Widen entry window",
            "expected_effect": "+6% trade frequency",
            "risks": "May admit lower-quality entries",
        },
        "current_rule": "RSI < 32: +2 points (strong oversold)",
        "proposed_rule": "RSI < 35: +2 points (strong oversold)",
        "confidence": 0.78,
    }
])


@pytest.fixture
def mock_store(tmp_path):
    return tmp_path / "suggestions_in.jsonl"


def _make_analyst(store_path):
    with patch("agents.analyst_in.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        from agents.analyst_in import InStrategyAnalyst
        analyst = InStrategyAnalyst(claude_md_path="CLAUDE.md")
        analyst.store.path = store_path
        return analyst, mock_client


def test_run_returns_suggestion_ids(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=SAMPLE_CLAUDE_RESPONSE)]
    mock_client.messages.create.return_value = mock_response

    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES), \
         patch("builtins.open", mock_open(read_data="- RSI < 32: +2 points (strong oversold)\n")):
        ids = analyst.run(days=30)

    assert len(ids) == 1
    assert ids[0].startswith("in-")


def test_run_returns_empty_list_when_too_few_entries(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    with patch.object(analyst, "_load_journal", return_value=[{"action": "SKIP"}] * 5):
        ids = analyst.run(days=30)

    assert ids == []
    mock_client.messages.create.assert_not_called()


def test_run_deduplicates_same_category(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=SAMPLE_CLAUDE_RESPONSE)]
    mock_client.messages.create.return_value = mock_response

    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES), \
         patch("builtins.open", mock_open(read_data="- RSI < 32: +2 points (strong oversold)\n")):
        analyst.run(days=30)
        analyst.run(days=30)

    from journal.suggestion_store import SuggestionStore
    records = SuggestionStore(store_path).load_all()
    assert len(records) == 1  # second run updated, not duplicated


def test_system_prompt_contains_key_mean_reversion_terms(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, _ = _make_analyst(store_path)
    prompt = analyst.SYSTEM_PROMPT
    assert "Bollinger" in prompt
    assert "RSI" in prompt
    assert "VWAP" in prompt
    assert "ATR" in prompt
    assert "mean reversion" in prompt.lower()


def test_parse_suggestions_handles_json_in_code_block(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, _ = _make_analyst(store_path)
    raw = f"```json\n{SAMPLE_CLAUDE_RESPONSE}\n```"
    result = analyst._parse_suggestions(raw)
    assert len(result) == 1
    assert result[0]["category"] == "rsi_threshold"


def test_parse_suggestions_returns_empty_on_invalid_json(tmp_path):
    store_path = tmp_path / "suggestions_in.jsonl"
    analyst, _ = _make_analyst(store_path)
    result = analyst._parse_suggestions("This is not JSON at all.")
    assert result == []
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_analyst_in.py -v
```

Expected: `ModuleNotFoundError: No module named 'agents.analyst_in'`

- [ ] **Step 3: Create `agents/analyst_in.py`**

```python
# agents/analyst_in.py
"""
In-strategy analyst agent.
Reviews the trade journal and produces parameter-tuning suggestions
within the existing mean reversion strategy.
"""

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from journal.suggestion_store import SuggestionStore

load_dotenv()
logger = logging.getLogger(__name__)

SUGGESTIONS_FILE = Path("journal/suggestions_in.jsonl")


class InStrategyAnalyst:

    SYSTEM_PROMPT = """You are a specialized mean reversion trading analyst. Your ONLY job is to review \
a live mean reversion bot's trade journal and produce specific, data-driven suggestions to improve \
its existing parameters. You do NOT suggest new strategies. You tune what exists.

## YOUR EXPERTISE

BOLLINGER BANDS:
- The 20-period, 2.0 std dev band is standard, but optimal settings vary by instrument and regime
- %B below 0 (price below lower band) is a strong signal — but strength depends on how far below
- BB squeeze (narrowing bands) often precedes expansion — entering during a squeeze can backfire
- Period: shorter (10) = more signals, more noise; longer (25-30) = fewer, higher quality
- Std dev: 1.5 = more signals, 2.5 = only extreme moves trigger

RSI:
- RSI < 30 is the classic oversold level but often too conservative for liquid large-caps
- RSI 32–40 often captures the best mean reversion entries — stressed but not fully panicked
- RSI divergence (price makes new low, RSI doesn't) is powerful confirmation not in current scoring
- The 14-period RSI is standard; 9-period responds faster but generates more false signals
- RSI is less reliable in strongly trending markets — hence the ADX filter

VWAP:
- VWAP deviation > 1% is meaningful intraday, but threshold should tighten late in the session
- Price > 2% below VWAP in a ranging market is a very strong mean reversion setup
- VWAP most reliable 10:00–14:00 EST; early morning and late afternoon signals are weaker

ADX:
- ADX > 25 correctly filters trends, but ADX > 20 rising is also warning-worthy (trend building)
- ADX slope matters as much as level: ADX 23 rising is more dangerous than ADX 27 falling
- ADX < 15 = very quiet market — signals in ultra-quiet markets can be false breakouts in disguise

ATR:
- Stop at 0.5× ATR is tight — for volatile symbols (high beta), 0.75× may be more appropriate
- Trail stop at 0.25× ATR after partial exit is very tight — often stops out before target
- ATR should ideally be calibrated per-symbol volatility regime

SIGNAL SCORING:
- The 3/6 minimum threshold is the key tunable parameter
- Weak signals (RSI < 40 = only 1 point) included in borderline 3-signal setups create noise
- Some signal combinations are strongly predictive; others are coincidental

## WHAT YOU MUST ANALYZE

From the provided journal entries, analyze:
1. Win rate per signal combination (e.g., RSI<32+BelowBB vs RSI<40+BelowVWAP)
2. Average R-multiple achieved vs theoretical R:R at entry
3. Stop-out patterns — are stops being hit at similar levels, suggesting too-tight ATR multiplier?
4. Signal score distribution on profitable vs unprofitable trades
5. Skip decisions that would have been profitable (opportunity cost)
6. Confidence label accuracy — are HIGH confidence trades actually winning more often?

## OUTPUT FORMAT

Return a JSON array of 1–3 suggestion objects. Focus on the HIGHEST IMPACT suggestions only. \
If the journal has fewer than 10 trades, return [] and note that the sample is too small.

Each suggestion object must have exactly these fields:
{
  "category": "rsi_threshold|bollinger_params|adx_filter|vwap_deviation|atr_multiplier|signal_weight|exit_rule|time_gate|position_sizing|skip_rate",
  "priority": "high|medium|low",
  "title": "Short title under 80 chars",
  "analysis": "Specific findings — include actual numbers from the journal",
  "rationale": "Why this change improves performance",
  "insight": {
    "why_now": "What in the data triggered this suggestion now",
    "purpose": "What this change is designed to achieve",
    "expected_effect": "Expected impact on win rate, trade frequency, or R-multiple",
    "risks": "What could go wrong"
  },
  "current_rule": "Exact text from CLAUDE.md to replace (copy it exactly)",
  "proposed_rule": "Exact replacement text",
  "confidence": 0.0
}

Return ONLY the JSON array. No preamble, no explanation outside the JSON."""

    def __init__(self, claude_md_path: str = "CLAUDE.md"):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = "claude-sonnet-4-6"
        self.store = SuggestionStore(SUGGESTIONS_FILE)
        self.claude_md_path = claude_md_path

    def run(self, days: int = 30) -> list[str]:
        """
        Analyze the trade journal and upsert improvement suggestions.
        Returns list of suggestion IDs created or updated.
        """
        entries = self._load_journal(days)

        if len(entries) < 10:
            logger.info(f"InStrategyAnalyst: only {len(entries)} entries — minimum 10 required")
            return []

        try:
            claude_md = Path(self.claude_md_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.error(f"CLAUDE.md not found at {self.claude_md_path}")
            return []

        prompt = self._build_prompt(entries, claude_md)
        raw = self._call_claude(prompt)
        suggestions = self._parse_suggestions(raw)

        result_ids = []
        for s in suggestions:
            s["id"] = self._generate_id()
            s["type"] = "in_strategy"
            s["status"] = "pending"
            s.setdefault("proposed_claude_md_diff", None)
            s.setdefault("supporting_data", {"trades_analyzed": len(entries), "period_days": days})
            s.setdefault("actioned_at", None)
            s.setdefault("actioned_by", None)
            from datetime import datetime, timezone
            s["generated_at"] = datetime.now(timezone.utc).isoformat()
            sid = self.store.upsert(s)
            result_ids.append(sid)

        logger.info(f"InStrategyAnalyst: {len(result_ids)} suggestion(s) generated/updated")
        return result_ids

    def _load_journal(self, days: int) -> list[dict]:
        from journal.logger import TradeJournal
        return TradeJournal().get_entries(days=days)

    def _build_prompt(self, entries: list[dict], claude_md: str) -> str:
        return f"""CURRENT CLAUDE.MD RULES:
{claude_md}

JOURNAL ENTRIES ({len(entries)} entries, last 30 days):
{json.dumps(entries, indent=2)}

Analyze this data and return 1–3 high-impact improvement suggestions as a JSON array."""

    def _call_claude(self, prompt: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=3000,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"InStrategyAnalyst Claude API error: {e}")
            return "[]"

    def _parse_suggestions(self, raw: str) -> list[dict]:
        patterns = [
            r"```json\s*(\[.*?\])\s*```",
            r"```\s*(\[.*?\])\s*```",
            r"(\[.*?\])",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group(1))
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    continue
        logger.warning("InStrategyAnalyst: could not parse suggestions from Claude response")
        return []

    def _generate_id(self) -> str:
        date_str = date.today().strftime("%Y%m%d")
        existing = [
            r["id"] for r in self.store.load_all()
            if r.get("id", "").startswith(f"in-{date_str}")
        ]
        return f"in-{date_str}-{len(existing) + 1:03d}"
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_analyst_in.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/analyst_in.py tests/test_analyst_in.py
git commit -m "feat: add InStrategyAnalyst mean reversion optimizer agent"
```

---

## Task 3: OutStrategyAnalyst + yfinance dependency

**Files:**
- Create: `agents/analyst_out.py`
- Create: `tests/test_analyst_out.py`
- Modify: `requirements.txt`

The out-strategy analyst adds yfinance macro context (VIX, SPY regime, sector ETF performance) to the journal analysis and uses a broad multi-methodology system prompt. It writes to `suggestions_out.jsonl`.

- [ ] **Step 1: Add yfinance to requirements.txt**

Open `requirements.txt` and add after the last line:

```
yfinance>=0.2.0
```

Install it:

```bash
pip install yfinance>=0.2.0
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_analyst_out.py`:

```python
# tests/test_analyst_out.py
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
import pandas as pd


SAMPLE_JOURNAL_ENTRIES = [
    {
        "timestamp": "2026-05-01T10:30:00-05:00",
        "date": "2026-05-01",
        "action": "SKIP",
        "symbol": "NVDA",
        "signal_score": 2,
        "signals_fired": [],
        "confidence": "LOW",
        "reasoning": "Score too low",
        "execution_status": "SKIPPED",
    }
] * 15


SAMPLE_CLAUDE_RESPONSE = json.dumps([
    {
        "category": "regime_fit",
        "priority": "high",
        "title": "Current trending regime is unfavourable for mean reversion",
        "analysis": "VIX at 28.4 and SPY below 20-day MA indicate risk-off regime",
        "rationale": "Mean reversion underperforms when the market is trending down",
        "insight": {
            "why_now": "VIX spiked above 25 this week",
            "purpose": "Reduce trade frequency during unfavourable regime",
            "expected_effect": "Fewer losing trades during downtrends",
            "risks": "May miss bottom-fishing opportunities",
        },
        "current_rule": "If ADX > 25, do NOT take mean reversion signals. Skip the trade.",
        "proposed_rule": "If ADX > 25 OR VIX > 25, do NOT take mean reversion signals. Skip the trade.",
        "confidence": 0.82,
    }
])


def _make_analyst(store_path):
    with patch("agents.analyst_out.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        from agents.analyst_out import OutStrategyAnalyst
        analyst = OutStrategyAnalyst(claude_md_path="CLAUDE.md")
        analyst.store.path = store_path
        return analyst, mock_client


def _make_yf_ticker(close_price: float, history_df=None):
    ticker = MagicMock()
    ticker.fast_info = {"lastPrice": close_price}
    if history_df is not None:
        ticker.history.return_value = history_df
    return ticker


def test_fetch_macro_context_returns_dict(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, _ = _make_analyst(store_path)

    spy_prices = [470.0] * 25
    spy_df = pd.DataFrame({"Close": spy_prices})

    sector_df = pd.DataFrame({"Close": [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0]})

    with patch("agents.analyst_out.yf") as mock_yf:
        mock_yf.Ticker.side_effect = lambda sym: (
            _make_yf_ticker(22.5) if sym == "^VIX"
            else _make_yf_ticker(472.0, spy_df) if sym == "SPY"
            else _make_yf_ticker(100.0, sector_df)
        )
        macro = analyst._fetch_macro_context()

    assert "vix" in macro
    assert macro["vix"] == 22.5
    assert "spy_regime" in macro
    assert "sector_5d_performance" in macro


def test_fetch_macro_context_returns_error_dict_on_failure(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, _ = _make_analyst(store_path)

    with patch("agents.analyst_out.yf") as mock_yf:
        mock_yf.Ticker.side_effect = Exception("network error")
        macro = analyst._fetch_macro_context()

    assert "error" in macro


def test_run_includes_macro_context_in_prompt(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=SAMPLE_CLAUDE_RESPONSE)]
    mock_client.messages.create.return_value = mock_response

    macro = {"vix": 22.5, "vix_regime": "elevated", "spy_regime": "above_ma20", "sector_5d_performance": {}}

    with patch.object(analyst, "_load_journal", return_value=SAMPLE_JOURNAL_ENTRIES), \
         patch.object(analyst, "_fetch_macro_context", return_value=macro), \
         patch("builtins.open", mock_open(read_data="- If ADX > 25, do NOT take mean reversion signals.\n")):
        analyst.run(days=30)

    call_args = mock_client.messages.create.call_args
    user_message = call_args.kwargs["messages"][0]["content"]
    assert "22.5" in user_message  # VIX value present in prompt


def test_run_returns_empty_list_when_too_few_entries(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, mock_client = _make_analyst(store_path)

    with patch.object(analyst, "_load_journal", return_value=[{"action": "SKIP"}] * 5):
        ids = analyst.run(days=30)

    assert ids == []
    mock_client.messages.create.assert_not_called()


def test_system_prompt_covers_multiple_methodologies(tmp_path):
    store_path = tmp_path / "suggestions_out.jsonl"
    analyst, _ = _make_analyst(store_path)
    prompt = analyst.SYSTEM_PROMPT
    for term in ["Trend Following", "Momentum", "Breakout", "regime", "VIX", "behavioral"]:
        assert term.lower() in prompt.lower(), f"Missing term in system prompt: {term}"
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
python -m pytest tests/test_analyst_out.py -v
```

Expected: `ModuleNotFoundError: No module named 'agents.analyst_out'`

- [ ] **Step 4: Create `agents/analyst_out.py`**

```python
# agents/analyst_out.py
"""
Out-of-strategy analyst agent.
Reviews the trade journal alongside macro market context and produces
strategic suggestions that go beyond mean reversion parameter tuning.
"""

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yfinance as yf
from anthropic import Anthropic
from dotenv import load_dotenv

from journal.suggestion_store import SuggestionStore

load_dotenv()
logger = logging.getLogger(__name__)

SUGGESTIONS_FILE = Path("journal/suggestions_out.jsonl")

_SECTOR_ETFS = {
    "XLK": "Technology",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLI": "Industrials",
}


class OutStrategyAnalyst:

    SYSTEM_PROMPT = """You are a senior market strategist reviewing an algorithmic trading system from the outside. \
You have deep knowledge of all major trading methodologies and market dynamics. Your role is to evaluate whether \
the current strategy fits the prevailing market environment and identify strategic improvements that go beyond \
parameter tuning. The system runs a mean reversion strategy on US equities — a separate specialist handles \
parameter tuning. You zoom out.

## YOUR EXPERTISE

TREND FOLLOWING:
- Moving average crossovers (20/50/200 EMA), Donchian channel breakouts, ADX-driven entries
- Trend following performs best in macro regime shifts, not during consolidation phases
- Position sizing via ATR-based volatility targeting keeps risk constant across instruments

MOMENTUM TRADING:
- Relative strength: which sectors/symbols outperform on rolling 1-month, 3-month basis
- Rate of Change (ROC) as an entry filter; MACD signal line crossovers and histogram expansion
- Momentum strategies perform well in trending markets and poorly during reversals — the opposite of mean reversion

BREAKOUT TRADING:
- Consolidation detection: narrow Bollinger Bands, low ATR, low ADX
- Volume confirmation: breakouts on above-average volume are far more reliable
- False breakout filter: price must close above resistance, not just touch it
- Breakouts complement mean reversion: when mean reversion fails repeatedly, a breakout may be imminent

MARKET REGIME THEORY:
- 4 key regimes: Trending (ADX > 25, directional), Ranging (ADX < 20, oscillating), \
Volatile (high VIX, wide ATR), Low-vol (VIX < 15, tight ranges)
- Strategy fitness by regime:
  * Trending → trend following, momentum (NOT mean reversion)
  * Ranging → mean reversion (current strategy — ideal)
  * Volatile → reduce position size, widen stops, or sit out
  * Low-vol → mean reversion works but moves are small; commissions matter more
- Regime transitions are the most dangerous periods — the strategy is wrong on both sides of the shift

MACRO OVERLAY:
- VIX > 25: elevated fear — mean reversion less reliable (fear can sustain oversold conditions for weeks)
- VIX < 15: complacency — mean reversion works but watch for volatility expansion traps
- Sector rotation: money flowing from growth (XLK) to defensive (XLV, XLP) = risk-off environment
- SPY below its 20-day MA: market in correction — mean reversion entries carry more downside continuation risk

BEHAVIORAL PATTERN RECOGNITION in trading journals:
- Overtrading signature: high trade frequency after a loss day (revenge trading)
- Position size drift: taking positions on lower signal scores after wins (house money effect)
- Anchoring: journal reasoning references entry price as justification for holding losers
- FOMO entries: high signal score trades placed with poor R:R ratios
- Premature exits: positions closed before target with no stated technical reason

RISK MANAGEMENT FRAMEWORKS:
- Kelly Criterion: f* = (bp - q) / b where b = R:R, p = win rate, q = 1-p
  * At 50% win rate and 2:1 R:R, full Kelly = 25%; half-Kelly (12.5%) is standard practice
- Portfolio heat: sum of all open position risk as % of account (at 1% per trade max 3 positions = 3% max heat)
- Correlation-adjusted sizing: two highly correlated open positions (e.g., NVDA + AMD) double the effective risk

WATCHLIST ASSESSMENT:
- Mean reversion works best on high-liquidity instruments with strong historical range-bound behaviour
- Highly directional instruments (strong momentum stocks) are poor mean reversion candidates
- ETFs (SPY, QQQ, GLD) have natural mean reversion properties due to diversification

## WHAT YOU MUST ANALYZE

1. Whether the current macro regime (VIX level, SPY position vs MA) suits mean reversion
2. Behavioral patterns visible in the sequence of journal entries
3. Structural gaps: what profitable setups does this strategy structurally miss?
4. Watchlist suitability: are the symbols being traded well-suited to mean reversion?
5. Risk framework: is the current position sizing / daily loss limit optimal given the journal data?

## OUTPUT FORMAT

Return a JSON array of 1–3 suggestion objects. Focus on the highest-impact strategic suggestions. \
Do not suggest mean reversion parameter changes — those are handled by a separate specialist.

Each suggestion object must have exactly these fields:
{
  "category": "regime_fit|new_strategy|macro_overlay|behavioral|risk_framework|watchlist|correlation|sentiment_filter",
  "priority": "high|medium|low",
  "title": "Short title under 80 chars",
  "analysis": "Specific findings — include actual numbers from the journal and market data",
  "rationale": "Why this change improves performance",
  "insight": {
    "why_now": "What triggered this suggestion at this point in time",
    "purpose": "What this change is designed to achieve",
    "expected_effect": "Expected impact on performance",
    "risks": "What could go wrong"
  },
  "current_rule": "Exact text from CLAUDE.md to replace (or null if adding a new rule)",
  "proposed_rule": "Exact replacement text (or null if no CLAUDE.md change needed)",
  "confidence": 0.0
}

Return ONLY the JSON array. No preamble, no explanation outside the JSON."""

    def __init__(self, claude_md_path: str = "CLAUDE.md"):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = "claude-sonnet-4-6"
        self.store = SuggestionStore(SUGGESTIONS_FILE)
        self.claude_md_path = claude_md_path

    def run(self, days: int = 30) -> list[str]:
        """
        Analyze journal + macro context and upsert strategic suggestions.
        Returns list of suggestion IDs created or updated.
        """
        entries = self._load_journal(days)

        if len(entries) < 10:
            logger.info(f"OutStrategyAnalyst: only {len(entries)} entries — minimum 10 required")
            return []

        try:
            claude_md = Path(self.claude_md_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.error(f"CLAUDE.md not found at {self.claude_md_path}")
            return []

        macro = self._fetch_macro_context()
        prompt = self._build_prompt(entries, claude_md, macro)
        raw = self._call_claude(prompt)
        suggestions = self._parse_suggestions(raw)

        result_ids = []
        for s in suggestions:
            s["id"] = self._generate_id()
            s["type"] = "out_strategy"
            s["status"] = "pending"
            s.setdefault("proposed_claude_md_diff", None)
            s.setdefault("supporting_data", {
                "trades_analyzed": len(entries),
                "period_days": days,
                "vix_at_analysis": macro.get("vix"),
            })
            s.setdefault("actioned_at", None)
            s.setdefault("actioned_by", None)
            s["generated_at"] = datetime.now(timezone.utc).isoformat()
            sid = self.store.upsert(s)
            result_ids.append(sid)

        logger.info(f"OutStrategyAnalyst: {len(result_ids)} suggestion(s) generated/updated")
        return result_ids

    def _fetch_macro_context(self) -> dict:
        try:
            vix_ticker = yf.Ticker("^VIX")
            vix_close = round(vix_ticker.fast_info["lastPrice"], 2)
            vix_regime = "high" if vix_close > 25 else "elevated" if vix_close > 18 else "low"

            spy_hist = yf.Ticker("SPY").history(period="30d")
            spy_close = round(float(spy_hist["Close"].iloc[-1]), 2)
            spy_ma20 = round(float(spy_hist["Close"].rolling(20).mean().iloc[-1]), 2)
            spy_regime = "above_ma20" if spy_close > spy_ma20 else "below_ma20"

            sector_perf: dict[str, float] = {}
            for ticker, name in _SECTOR_ETFS.items():
                try:
                    hist = yf.Ticker(ticker).history(period="7d")
                    if len(hist) >= 5:
                        perf = (float(hist["Close"].iloc[-1]) / float(hist["Close"].iloc[-5]) - 1) * 100
                        sector_perf[name] = round(perf, 2)
                except Exception:
                    pass

            return {
                "vix": vix_close,
                "vix_regime": vix_regime,
                "spy_close": spy_close,
                "spy_ma20": spy_ma20,
                "spy_regime": spy_regime,
                "sector_5d_performance": sector_perf,
            }
        except Exception as e:
            logger.warning(f"Could not fetch macro context: {e}")
            return {"error": str(e)}

    def _build_prompt(self, entries: list[dict], claude_md: str, macro: dict) -> str:
        return f"""CURRENT CLAUDE.MD RULES:
{claude_md}

MACRO MARKET CONTEXT:
{json.dumps(macro, indent=2)}

JOURNAL ENTRIES ({len(entries)} entries, last 30 days):
{json.dumps(entries, indent=2)}

Analyze this data from a strategic perspective. Return 1–3 high-impact suggestions as a JSON array."""

    def _call_claude(self, prompt: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=3000,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"OutStrategyAnalyst Claude API error: {e}")
            return "[]"

    def _parse_suggestions(self, raw: str) -> list[dict]:
        patterns = [
            r"```json\s*(\[.*?\])\s*```",
            r"```\s*(\[.*?\])\s*```",
            r"(\[.*?\])",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group(1))
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    continue
        logger.warning("OutStrategyAnalyst: could not parse suggestions from Claude response")
        return []

    def _generate_id(self) -> str:
        date_str = date.today().strftime("%Y%m%d")
        existing = [
            r["id"] for r in self.store.load_all()
            if r.get("id", "").startswith(f"out-{date_str}")
        ]
        return f"out-{date_str}-{len(existing) + 1:03d}"
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_analyst_out.py -v
```

Expected: All 5 tests PASS.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: All 24 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/analyst_out.py tests/test_analyst_out.py requirements.txt
git commit -m "feat: add OutStrategyAnalyst broad strategy agent with yfinance macro context"
```

---

## Task 4: main.py CLI Modes

**Files:**
- Modify: `main.py`

Add three new CLI modes: `analyst-in`, `analyst-out`, `analyst-full`. Each runs the corresponding agent(s) and optionally sends a Telegram summary notification.

- [ ] **Step 1: Add `run_analyst_in`, `run_analyst_out`, `run_analyst_full` functions to `main.py`**

After the `run_weekly_review` function (line ~185), add:

```python
def run_analyst_in(config: dict) -> list[str]:
    """Run the in-strategy analyst and return generated suggestion IDs."""
    from agents.analyst_in import InStrategyAnalyst
    logger.info("Running in-strategy analyst...")
    analyst = InStrategyAnalyst()
    ids = analyst.run(days=30)
    logger.info(f"In-strategy analyst complete: {len(ids)} suggestion(s)")
    return ids


def run_analyst_out(config: dict) -> list[str]:
    """Run the out-of-strategy analyst and return generated suggestion IDs."""
    from agents.analyst_out import OutStrategyAnalyst
    logger.info("Running out-of-strategy analyst...")
    analyst = OutStrategyAnalyst()
    ids = analyst.run(days=30)
    logger.info(f"Out-strategy analyst complete: {len(ids)} suggestion(s)")
    return ids


def run_analyst_full(config: dict) -> dict:
    """Run both analysts sequentially and send Telegram notification if configured."""
    in_ids = run_analyst_in(config)
    out_ids = run_analyst_out(config)
    result = {"in_strategy": len(in_ids), "out_strategy": len(out_ids)}
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        _send_analyst_telegram_notification(len(in_ids), len(out_ids))
    return result


def _send_analyst_telegram_notification(in_count: int, out_count: int):
    """Send Telegram notification summarising the analyst run."""
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    message = (
        f"*FlowTrader Analyst* — Daily Review Complete 🧠\n"
        f"In\\-Strategy: {in_count} suggestion(s)\n"
        f"Out\\-Strategy: {out_count} suggestion(s)\n"
        f"→ Review on the dashboard"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "MarkdownV2"},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Analyst Telegram notification failed: {e}")
```

- [ ] **Step 2: Add the new modes to the `if __name__ == "__main__"` block**

In the existing `if __name__ == "__main__":` block, find the `elif mode == "weekly-review":` branch and add after it:

```python
    elif mode == "analyst-in":
        result = run_analyst_in(config)
        print(json.dumps({"status": "complete", "suggestions_generated": len(result)}))

    elif mode == "analyst-out":
        result = run_analyst_out(config)
        print(json.dumps({"status": "complete", "suggestions_generated": len(result)}))

    elif mode == "analyst-full":
        result = run_analyst_full(config)
        print(json.dumps({"status": "complete", **result}))
```

- [ ] **Step 3: Smoke-test the new CLI modes (no trades required)**

```bash
# Should exit cleanly with {"status": "complete", "suggestions_generated": 0}
# (0 because journal/trades.jsonl has fewer than 10 entries in a fresh environment)
python main.py analyst-in
python main.py analyst-out
python main.py analyst-full
```

Expected: Each prints JSON with `"status": "complete"` — no exceptions.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add analyst-in, analyst-out, analyst-full CLI modes to main.py"
```

---

## Task 5: Dashboard — Analyst Tab

**Files:**
- Modify: `dashboard.py`

Add the 4th Analyst tab. It loads suggestions from both JSONL stores, renders suggestion cards with insight boxes, and handles Approve/Archive/Cancel actions. "Run Now" buttons call the agents directly (not subprocess — Streamlit-safe).

- [ ] **Step 1: Update the tabs line and add cached suggestion loader**

In `dashboard.py`, find the line:

```python
tab_market, tab_account, tab_journal = st.tabs(["🔍 Market", "💼 Account", "📓 Journal"])
```

Replace it with:

```python
tab_market, tab_account, tab_journal, tab_analyst = st.tabs(
    ["🔍 Market", "💼 Account", "📓 Journal", "🧠 Analyst"]
)
```

Then add a new cached loader function near the top of the file, after the existing `fetch_journal_entries` function:

```python
@st.cache_data(ttl=30)
def fetch_suggestions(type_filter: str) -> list[dict]:
    from journal.suggestion_store import SuggestionStore
    results = []
    if type_filter in ("both", "in_strategy"):
        results.extend(SuggestionStore(Path("journal/suggestions_in.jsonl")).load_all())
    if type_filter in ("both", "out_strategy"):
        results.extend(SuggestionStore(Path("journal/suggestions_out.jsonl")).load_all())
    return sorted(results, key=lambda x: x.get("generated_at", ""), reverse=True)
```

- [ ] **Step 2: Add the Analyst tab content**

At the end of `dashboard.py`, before the auto-refresh footer section, add:

```python
# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — ANALYST
# ═══════════════════════════════════════════════════════════════════════════════
with tab_analyst:
    st.subheader("🧠 Trading Analyst — Suggested Improvements")

    # ── Controls ─────────────────────────────────────────────────────────────
    ctrl1, ctrl2 = st.columns([3, 2])
    with ctrl1:
        status_filter = st.radio(
            "Status", ["pending", "approved", "archived", "cancelled"],
            horizontal=True, index=0,
        )
    with ctrl2:
        type_filter = st.radio(
            "View", ["both", "in_strategy", "out_strategy"],
            horizontal=True, index=0,
            format_func=lambda v: {
                "both": "Both",
                "in_strategy": "In-Strategy",
                "out_strategy": "Out-Strategy",
            }[v],
        )

    # ── Run Now buttons ───────────────────────────────────────────────────────
    run1, run2, run3 = st.columns(3)
    with run1:
        if st.button("▶ Run In-Strategy", use_container_width=True):
            with st.spinner("Running in-strategy analyst..."):
                try:
                    from agents.analyst_in import InStrategyAnalyst
                    ids = InStrategyAnalyst().run(days=30)
                    st.cache_data.clear()
                    st.success(f"Done — {len(ids)} suggestion(s)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Analyst failed: {e}")
    with run2:
        if st.button("▶ Run Out-Strategy", use_container_width=True):
            with st.spinner("Running out-strategy analyst..."):
                try:
                    from agents.analyst_out import OutStrategyAnalyst
                    ids = OutStrategyAnalyst().run(days=30)
                    st.cache_data.clear()
                    st.success(f"Done — {len(ids)} suggestion(s)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Analyst failed: {e}")
    with run3:
        if st.button("▶ Run Both", use_container_width=True):
            with st.spinner("Running both analysts..."):
                try:
                    from agents.analyst_in import InStrategyAnalyst
                    from agents.analyst_out import OutStrategyAnalyst
                    in_ids = InStrategyAnalyst().run(days=30)
                    out_ids = OutStrategyAnalyst().run(days=30)
                    st.cache_data.clear()
                    st.success(f"Done — {len(in_ids)} in-strategy, {len(out_ids)} out-strategy")
                    st.rerun()
                except Exception as e:
                    st.error(f"Analyst failed: {e}")

    st.divider()

    # ── Load and filter ───────────────────────────────────────────────────────
    all_suggestions = fetch_suggestions(type_filter)
    filtered = [s for s in all_suggestions if s.get("status") == status_filter]
    in_suggestions  = [s for s in filtered if s.get("type") == "in_strategy"]
    out_suggestions = [s for s in filtered if s.get("type") == "out_strategy"]

    # ── Suggestion card renderer ──────────────────────────────────────────────
    def _render_suggestion_cards(suggestions: list[dict], store_path: str) -> None:
        from journal.suggestion_store import SuggestionStore

        if not suggestions:
            st.info(f"No {status_filter} suggestions.")
            return

        priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}

        for s in suggestions:
            icon  = priority_icon.get(s.get("priority", "low"), "⚪")
            conf  = int(s.get("confidence", 0) * 100)

            st.markdown(
                f"{icon} **{s.get('priority','').upper()}** &nbsp;·&nbsp; "
                f"`{s.get('category','')}` &nbsp;·&nbsp; Confidence: **{conf}%**"
            )
            st.markdown(f"#### {s.get('title', 'Untitled')}")
            st.markdown(s.get("analysis", ""))

            # Insight box
            insight = s.get("insight", {})
            if insight:
                with st.expander("💡 Insight — why this change, what it does, what to expect"):
                    st.markdown(f"**Why now:** {insight.get('why_now', '—')}")
                    st.markdown(f"**Purpose:** {insight.get('purpose', '—')}")
                    st.markdown(f"**Expected effect:** {insight.get('expected_effect', '—')}")
                    st.markdown(f"**Risks:** {insight.get('risks', '—')}")

            # Current vs proposed rule
            curr = s.get("current_rule")
            prop = s.get("proposed_rule")
            if curr or prop:
                col_c, col_p = st.columns(2)
                with col_c:
                    st.markdown("**Current rule:**")
                    st.code(curr or "(new rule)", language="")
                with col_p:
                    st.markdown("**Proposed rule:**")
                    st.code(prop or "(remove rule)", language="")

            # Supporting data metrics
            support = {
                k: v for k, v in (s.get("supporting_data") or {}).items()
                if v is not None
            }
            if support:
                metric_cols = st.columns(min(len(support), 4))
                for i, (k, v) in enumerate(list(support.items())[:4]):
                    metric_cols[i].metric(k.replace("_", " ").title(), v)

            # Action buttons (pending only)
            if s.get("status") == "pending":
                act1, act2, act3, _ = st.columns([2, 1, 1, 3])
                with act1:
                    if st.button("✅ Approve & Apply", key=f"approve_{s['id']}"):
                        try:
                            store = SuggestionStore(Path(store_path))
                            if curr and prop:
                                SuggestionStore.apply_to_claude_md("CLAUDE.md", curr, prop)
                            store.action(s["id"], "approved")
                            st.cache_data.clear()
                            st.success("CLAUDE.md updated — takes effect next trading session")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not apply: {e}")
                with act2:
                    if st.button("📦 Archive", key=f"archive_{s['id']}"):
                        SuggestionStore(Path(store_path)).action(s["id"], "archived")
                        st.cache_data.clear()
                        st.rerun()
                with act3:
                    if st.button("❌ Cancel", key=f"cancel_{s['id']}"):
                        SuggestionStore(Path(store_path)).action(s["id"], "cancelled")
                        st.cache_data.clear()
                        st.rerun()

            st.caption(f"Generated: {s.get('generated_at', '—')}")
            st.divider()

    # ── In-Strategy section ───────────────────────────────────────────────────
    if type_filter in ("both", "in_strategy"):
        st.subheader(f"In-Strategy Suggestions — {len(in_suggestions)} {status_filter}")
        _render_suggestion_cards(in_suggestions, "journal/suggestions_in.jsonl")

    # ── Out-Strategy section ──────────────────────────────────────────────────
    if type_filter in ("both", "out_strategy"):
        st.subheader(f"Out-of-Strategy Suggestions — {len(out_suggestions)} {status_filter}")
        _render_suggestion_cards(out_suggestions, "journal/suggestions_out.jsonl")
```

- [ ] **Step 3: Verify the dashboard starts without errors**

```bash
streamlit run dashboard.py
```

Open `http://localhost:8501`. Navigate to the **🧠 Analyst** tab. Expected:
- Tab renders with controls and three "Run" buttons
- Both sections show "No pending suggestions." (expected on fresh start)
- No Python exceptions in the terminal

- [ ] **Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: add Analyst tab to dashboard with suggestion cards and approve/archive/cancel actions"
```

---

## Task 6: GitHub Actions Scheduling

**Files:**
- Modify: `.github/workflows/trading-bot.yml`

Add a daily `analyst` job that runs at 17:30 EST (21:30 UTC) on weekdays. It reuses the same journal cache as the trading job, restoring it before the analyst run so the agents can read `trades.jsonl`.

- [ ] **Step 1: Add the analyst cron and new `analyst` job**

In `.github/workflows/trading-bot.yml`, in the `on.schedule` section, add after the last trading cron (line 19):

```yaml
    # ── Daily analyst run — 17:30 EST (21:30 UTC) weekdays ────────────────
    - cron: '30 21 * * 1-5'   # 17:30 EST = 21:30 UTC
```

Then add a new `analyst` job after the closing of the `trade` job:

```yaml
  analyst:
    name: Daily Analyst Review
    runs-on: ubuntu-latest
    # Only run on the 17:30 EST cron, not the trading-hours crons
    if: github.event_name == 'schedule' && contains(github.event.schedule, '30 21')

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Create required directories
        run: mkdir -p journal

      - name: Restore journal cache
        uses: actions/cache@v4
        with:
          path: journal/
          key: journal-${{ runner.os }}-${{ github.run_id }}
          restore-keys: |
            journal-${{ runner.os }}-

      - name: Run analyst
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python main.py analyst-full

      - name: Upload suggestion artifacts
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: analyst-suggestions-${{ github.run_id }}
          path: |
            journal/suggestions_in.jsonl
            journal/suggestions_out.jsonl
          retention-days: 90

      - name: Save updated journal to cache
        uses: actions/cache/save@v4
        if: always()
        with:
          path: journal/
          key: journal-${{ runner.os }}-${{ github.run_id }}
```

Also update the `workflow_dispatch.inputs.mode.options` to include the new modes. Find:

```yaml
        options:
          - full
          - test
          - weekly-review
```

Replace with:

```yaml
        options:
          - full
          - test
          - weekly-review
          - analyst-in
          - analyst-out
          - analyst-full
```

- [ ] **Step 2: Validate the YAML syntax**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/trading-bot.yml'))"
```

Expected: No output (no errors).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/trading-bot.yml requirements.txt
git commit -m "feat: add daily analyst cron job to GitHub Actions workflow"
```

---

## Final Verification

- [ ] **Run the full test suite**

```bash
python -m pytest tests/ -v
```

Expected: All 24 tests PASS. No warnings about missing modules.

- [ ] **End-to-end smoke test** (requires `ANTHROPIC_API_KEY` in `.env` and at least 10 journal entries)

```bash
# If journal is empty, seed a few test entries first:
python main.py test  # just fetches account snapshot

# Then run both analysts
python main.py analyst-full
```

Expected: `{"status": "complete", "in_strategy": N, "out_strategy": N}`

- [ ] **Dashboard visual check**

```bash
streamlit run dashboard.py
```

1. Navigate to **🧠 Analyst** tab
2. Click **▶ Run Both** — spinner appears, then success message
3. Suggestion cards appear with: priority badge, category, confidence %, analysis text, 💡 Insight expander, current/proposed rule columns, supporting data metrics, and action buttons
4. Click **📦 Archive** on a suggestion — card disappears from Pending view
5. Switch status filter to **Archived** — archived suggestion reappears
6. Click **✅ Approve & Apply** on a suggestion with `current_rule` set — success toast appears, `CLAUDE.md` is updated

- [ ] **Final commit**

```bash
git add .
git commit -m "feat: trading analyst agent system — in-strategy + out-strategy analysts with dashboard integration"
```
