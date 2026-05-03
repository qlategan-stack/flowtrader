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
from datetime import date, datetime, timezone
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

BOLLINGER BANDS (Bollinger):
- The 20-period, 2.0 std dev band is standard, but optimal settings vary by instrument and regime
- %B below 0 (price below lower band) is a strong signal — but strength depends on how far below
- BB squeeze (narrowing bands) often precedes expansion — entering during a squeeze can backfire
- Period: shorter (10) = more signals, more noise; longer (25-30) = fewer, higher quality
- Std dev: 1.5 = more signals, 2.5 = only extreme moves trigger

RSI:
- RSI < 30 is the classic oversold level but often too conservative for liquid large-caps
- RSI 32-40 often captures the best mean reversion entries — stressed but not fully panicked
- RSI divergence (price makes new low, RSI doesn't) is powerful confirmation not in current scoring
- The 14-period RSI is standard; 9-period responds faster but generates more false signals
- RSI is less reliable in strongly trending markets — hence the ADX filter

VWAP:
- VWAP deviation > 1% is meaningful intraday, but threshold should tighten late in the session
- Price > 2% below VWAP in a ranging market is a very strong mean reversion setup
- VWAP most reliable 10:00-14:00 EST; early morning and late afternoon signals are weaker

ADX:
- ADX > 25 correctly filters trends, but ADX > 20 rising is also warning-worthy (trend building)
- ADX slope matters as much as level: ADX 23 rising is more dangerous than ADX 27 falling
- ADX < 15 = very quiet market — signals in ultra-quiet markets can be false breakouts in disguise

ATR:
- Stop at 0.5x ATR is tight — for volatile symbols (high beta), 0.75x may be more appropriate
- Trail stop at 0.25x ATR after partial exit is very tight — often stops out before target
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

Return a JSON array of 1-3 suggestion objects. Focus on the HIGHEST IMPACT suggestions only. \
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
            with open(self.claude_md_path, encoding="utf-8") as f:
                claude_md = f.read()
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

Analyze this data and return 1-3 high-impact improvement suggestions as a JSON array."""

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
