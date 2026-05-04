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

_MIN_JOURNAL_ENTRIES = 10
_SPY_HISTORY_PERIOD = "30d"
_SECTOR_HISTORY_PERIOD = "7d"


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
- Momentum strategies perform well in trending markets and poorly during reversals

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
- Regime transitions are the most dangerous periods

MACRO OVERLAY:
- VIX > 25: elevated fear — mean reversion less reliable (fear can sustain oversold conditions)
- VIX < 15: complacency — mean reversion works but watch for volatility expansion traps
- Sector rotation: money flowing from growth (XLK) to defensive (XLV, XLP) = risk-off environment
- SPY below its 20-day MA: market in correction — mean reversion entries carry more downside risk

BEHAVIORAL PATTERN RECOGNITION in trading journals:
- Overtrading signature: high trade frequency after a loss day (revenge trading)
- Position size drift: taking positions on lower signal scores after wins
- Anchoring: journal reasoning references entry price as justification for holding losers
- FOMO entries: high signal score trades placed with poor R:R ratios
- Premature exits: positions closed before target with no stated technical reason

RISK MANAGEMENT FRAMEWORKS:
- Kelly Criterion: f* = (bp - q) / b where b = R:R, p = win rate, q = 1-p
  * At 50% win rate and 2:1 R:R, full Kelly = 25%; half-Kelly (12.5%) is standard practice
- Portfolio heat: sum of all open position risk as % of account (1% per trade, 3 positions = 3% max)
- Correlation-adjusted sizing: two highly correlated open positions double the effective risk

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

Return a JSON array of 1-3 suggestion objects. Focus on the highest-impact strategic suggestions. \
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

        if len(entries) < _MIN_JOURNAL_ENTRIES:
            logger.info(f"OutStrategyAnalyst: only {len(entries)} entries — minimum {_MIN_JOURNAL_ENTRIES} required")
            return []

        try:
            with open(self.claude_md_path, encoding="utf-8") as f:
                claude_md = f.read()
        except FileNotFoundError:
            logger.error(f"CLAUDE.md not found at {self.claude_md_path}")
            return []

        macro = self._fetch_macro_context()
        prompt = self._build_prompt(entries, claude_md, macro, days)
        raw = self._call_claude(prompt)
        if raw is None:
            return []
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
            vix_close = round(yf.Ticker("^VIX").fast_info["lastPrice"], 2)
            vix_regime = "high" if vix_close > 25 else "elevated" if vix_close > 18 else "low"

            spy_hist = yf.Ticker("SPY").history(period=_SPY_HISTORY_PERIOD)
            spy_close = round(float(spy_hist["Close"].iloc[-1]), 2)
            spy_ma20 = round(float(spy_hist["Close"].rolling(20).mean().iloc[-1]), 2)
            spy_regime = "above_ma20" if spy_close > spy_ma20 else "below_ma20"

            sector_perf: dict[str, float] = {}
            for ticker, name in _SECTOR_ETFS.items():
                try:
                    hist = yf.Ticker(ticker).history(period=_SECTOR_HISTORY_PERIOD)
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

    def _load_journal(self, days: int) -> list[dict]:
        from journal.logger import TradeJournal
        return TradeJournal().get_entries(days=days)

    def _build_prompt(self, entries: list[dict], claude_md: str, macro: dict, days: int) -> str:
        return f"""CURRENT CLAUDE.MD RULES:
{claude_md}

MACRO MARKET CONTEXT:
{json.dumps(macro, indent=2)}

JOURNAL ENTRIES ({len(entries)} entries, last {days} days):
{json.dumps(entries, indent=2)}

Analyze this data from a strategic perspective. Return 1-3 high-impact suggestions as a JSON array."""

    def _call_claude(self, prompt: str) -> str | None:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"OutStrategyAnalyst Claude API error: {e}")
            return None

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
