"""
agents/decision.py
The Claude decision agent. Reads market snapshot, applies strategy rules,
and returns a structured trade decision with full reasoning.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger(__name__)


def _classify_api_error(exc: Exception) -> str:
    """
    Classify an Anthropic SDK exception into a stable kind string used by the
    Telegram-alert rate-limiter. The classifier is intentionally string- and
    status-code-based (not isinstance) so it does not couple to specific SDK
    exception classes.
    """
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)

    if "credit balance" in msg or "billing" in msg:
        return "credit_exhausted"
    if status == 401 or "authentication" in msg or "invalid api key" in msg:
        return "auth"
    if status == 429 or "rate limit" in msg:
        return "rate_limit"
    if "connection" in msg or "timeout" in msg or "timed out" in msg:
        return "connection"
    return "other"


class TradingDecisionAgent:
    """
    Claude-powered decision agent.
    Reads the market snapshot, checks guardrails, and outputs trade decisions.
    """

    def __init__(self, claude_md_path: str = "CLAUDE.md"):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = "claude-sonnet-4-6"

        try:
            with open(claude_md_path, "r") as f:
                self.system_prompt = f.read()
        except FileNotFoundError:
            logger.warning("CLAUDE.md not found. Using default system prompt.")
            self.system_prompt = self._default_system_prompt()

        self.research_memo = self._load_research_memo()

        from agents.executor import load_risk_profile
        self._profile_name, self._profile = load_risk_profile()
        self._min_score = self._profile.get("min_signal_score", 3)
        logger.info(f"Decision agent using profile '{self._profile_name}' — min_signal_score={self._min_score}")

    def analyze_market(self, market_snapshot: dict, account_snapshot: dict) -> dict:
        """
        Send the market snapshot to Claude and get a trading decision back.
        Returns a structured decision object.
        """

        # Build the user message
        user_message = self._build_analysis_prompt(market_snapshot, account_snapshot)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=self.system_prompt,
                messages=[
                    {"role": "user", "content": user_message}
                ]
            )

            raw_response = response.content[0].text
            decision = self._parse_decision(raw_response)
            decision["raw_reasoning"] = raw_response
            return decision

        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return {
                "action": "SKIP",
                "symbol": None,
                "reasoning": f"Claude API error: {str(e)}",
                "journal_entry": f"Session skipped due to API error: {str(e)}",
                "api_error": True,
                "api_error_kind": _classify_api_error(e),
            }

    def analyze_single_symbol(self, symbol_data: dict, account: dict) -> dict:
        """Analyze a single symbol from the watchlist."""

        prompt = f"""
Analyze this trading setup and decide: BUY, SKIP, or HOLD.

ACCOUNT STATUS:
- Portfolio Value: ${account.get('portfolio_value', 0):,.2f}
- Buying Power: ${account.get('buying_power', 0):,.2f}
- Open Positions: {account.get('open_positions', 0)}/3 max
- Day P&L: ${account.get('day_pl', 0):,.2f}

SYMBOL: {symbol_data['symbol']}
Setup Quality: {symbol_data.get('setup_quality', 'UNKNOWN')}

TECHNICAL INDICATORS:
{json.dumps(symbol_data.get('indicators', {}), indent=2)}

NEWS SENTIMENT:
{json.dumps(symbol_data.get('news_sentiment', {}), indent=2)}

RECENT HEADLINES:
{json.dumps(symbol_data.get('recent_headlines', [])[:3], indent=2)}

Apply your signal scoring and guardrails. Return your decision as JSON.
If signal_score < {self._min_score} or regime is TRENDING, the answer must be SKIP.
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1000,
                system=self.system_prompt,
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text
            return self._parse_decision(raw)

        except Exception as e:
            logger.error(f"Decision error for {symbol_data.get('symbol', 'UNKNOWN')}: {e}")
            return {
                "action": "SKIP",
                "symbol": symbol_data.get("symbol"),
                "reasoning": str(e),
                "api_error": True,
                "api_error_kind": _classify_api_error(e),
            }

    def run_weekly_review(self, journal_entries: list) -> str:
        """
        Feed the week's journal entries back to Claude for self-review.
        Returns a structured report with recommended adjustments.
        """

        entries_text = json.dumps(journal_entries[-50:], indent=2)  # Last 50 entries

        prompt = f"""
You are reviewing your own trading performance for the past week.
Here are your journal entries:

{entries_text}

Please analyze:
1. Win rate and average R-multiple
2. Which signal combinations led to profitable trades
3. Which led to losses
4. Are there systematic mistakes? (selling too early, stops too tight, wrong regime calls)
5. What 3 specific adjustments should be made to the CLAUDE.md rules next week?

Be honest and specific. Identify real patterns, not generic advice.
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                system="You are a disciplined trading analyst reviewing your own performance. Be honest and specific.",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        except Exception as e:
            return f"Weekly review failed: {str(e)}"

    def _build_analysis_prompt(self, snapshot: dict, account: dict) -> str:
        """Build the full market analysis prompt."""

        # A setup is a candidate if the regime router picked a strategy for it.
        # Either MEAN_REVERSION (oversold mean-reversion) or MOMENTUM (breakout
        # continuation) qualifies — the router has already enforced the min
        # signal-score and directional gates upstream.
        top_setups = [
            s for s in snapshot.get("watchlist", [])
            if s.get("indicators", {}).get("strategy_mode", "NONE") != "NONE"
            and s.get("setup_quality") != "NO_DATA"
        ][:3]

        all_symbols = snapshot.get("watchlist", [])

        research_ctx = self._build_research_context()

        max_pos = self._profile.get("max_open_positions", 3)

        watchlist_summary = [
            {
                "symbol": s["symbol"],
                "strategy_mode": s["indicators"].get("strategy_mode", "NONE"),
                "mr_score": s["indicators"].get("signal_score", 0),
                "mom_score": s["indicators"].get("momentum_score", 0),
                "quality": s["setup_quality"],
                "regime": s["indicators"].get("regime", "?"),
            }
            for s in all_symbols
        ]

        return f"""
TRADING SESSION — {snapshot.get('timestamp', 'Unknown time')}

RISK PROFILE: {self._profile_name} (min signal score to enter: {self._min_score}/6)

ACCOUNT:
- Portfolio: ${account.get('portfolio_value', 0):,.2f}
- Buying Power: ${account.get('buying_power', 0):,.2f}
- Open Positions: {account.get('open_positions', 0)}/{max_pos}
- Today's P&L: ${account.get('day_pl', 0):+,.2f}

{research_ctx + chr(10) if research_ctx else ""}STRATEGY MODES — TWO ENTRY PATHS ARE NOW ACTIVE:

  MEAN_REVERSION (the CLAUDE.md default rules apply):
    - Buy oversold extension expecting return to MA20
    - Requires at least one classical oversold trigger (RSI<45, BelowLowerBB,
      or BelowMA20>1%) — regime/math signals alone are NOT enough
    - Skip if regime == TRENDING (ADX > 25)
    - Stop = indicators.stop_loss_price (0.5x ATR below entry)
    - Target = indicators.take_profit_price (MA20, the mean)

  MOMENTUM (new path — overrides the CLAUDE.md "ADX>25 → skip" rule):
    - Buy *with* a confirmed uptrend, expecting continuation
    - Requires at least one directional uptrend trigger (RSI>55, AboveUpperBB,
      AboveMA20>1%, or 20-day high breakout)
    - ADX > 25 is a POSITIVE signal here (trend strength), not a veto
    - Stop = indicators.momentum_stop_loss_price (1x ATR or 5-day low, tighter)
    - Target = indicators.momentum_take_profit_price (2x ATR, 1:2 R:R)
    - Time stop: exit any momentum trade open longer than 5 trading days

The regime router has already chosen a strategy_mode per symbol; use that mode's
score and bracket levels. If strategy_mode == "MOMENTUM", set the JSON
`stop_loss` and `take_profit` fields to momentum_stop_loss_price and
momentum_take_profit_price — NOT the mean-reversion slots.

TOP SETUPS (regime router picked a strategy for each):
{json.dumps(top_setups, indent=2)}

FULL WATCHLIST SUMMARY:
{json.dumps(watchlist_summary, indent=2)}

TASK:
1. Review the top setups against your signal rules and guardrails
2. For each, honour its strategy_mode and use the matching bracket levels
3. Weigh them against the weekly research brief above
4. Select the BEST single trade to place this session (or SKIP if nothing qualifies)
5. If multiple qualify, prefer the higher active score with lowest stop distance

CRITICAL OUTPUT RULE: Your response MUST begin with the JSON decision block inside
a ```json code fence. Do NOT write any analysis or prose before the JSON block.
Reasoning and journal_entry go inside the JSON fields, not outside it.

Active risk profile requires the active score (mean reversion OR momentum,
matching strategy_mode) >= {self._min_score} to enter. SKIP freely below that threshold.
"""

    def _parse_decision(self, raw_text: str) -> dict:
        """Extract JSON decision from Claude's response."""
        import re

        # 1. Try explicit code-fence blocks first (most reliable)
        for pattern in [r'```json\s*(.*?)\s*```', r'```\s*(.*?)\s*```']:
            for m in re.findall(pattern, raw_text, re.DOTALL):
                try:
                    parsed = json.loads(m)
                    if "action" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    continue

        # 2. Brace-counting extractor — finds any balanced {...} block in the
        #    response that contains an "action" key.  This handles nested objects
        #    and arrays that the old [^{}]* regex could not.
        depth, start = 0, -1
        for i, ch in enumerate(raw_text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = raw_text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if "action" in parsed:
                            return parsed
                    except json.JSONDecodeError:
                        pass
                    start = -1

        # 3. Try parsing the entire response as JSON
        try:
            parsed = json.loads(raw_text)
            if "action" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

        # 4. Last resort — JSON extraction failed; always SKIP.
        # Keyword detection is intentionally removed: Claude's prose frequently
        # contains "BUY" in analytical sentences, causing a malformed trade with
        # no symbol/prices. A missed opportunity costs nothing; a bad Alpaca
        # order causes real errors. Log the raw response for diagnosis.
        logger.warning(f"JSON parse failed — forced SKIP. Raw (200): {raw_text[:200]}")
        return {
            "action": "SKIP",
            "symbol": None,
            "reasoning": f"[PARSE FAILURE — SKIP forced] {raw_text[:500]}",
            "journal_entry": f"Decision parse failed; session skipped for safety. Raw: {raw_text[:200]}",
            "parse_error": True
        }

    def _load_research_memo(self) -> dict:
        from pathlib import Path
        path = Path("journal/weekly_research_memo.json")
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load research memo: {e}")
            return {}

    def _build_research_context(self) -> str:
        memo = self.research_memo
        if not memo:
            return ""

        raw_regime = memo.get("market_regime", "UNKNOWN")
        if isinstance(raw_regime, dict):
            regime = raw_regime.get("trend_or_range", str(raw_regime))[:100]
            mean_rev_active = raw_regime.get("mean_reversion_active", True)
            sizing_guidance = raw_regime.get("vix_position_sizing_guidance", "")[:120]
        else:
            regime = str(raw_regime)[:100]
            mean_rev_active = "TREND" not in regime.upper()
            sizing_guidance = ""

        confidence = memo.get("confidence_score", 5)

        opps = memo.get("top_opportunities", [])
        if isinstance(opps, dict):
            opps = list(opps.values())
        opp_syms = [
            (o.get("symbol", "?") if isinstance(o, dict) else str(o))
            for o in opps[:3]
        ]

        wl_changes = memo.get("watchlist_changes", {})
        if isinstance(wl_changes, dict):
            avoid_raw = wl_changes.get(
                "symbols_to_avoid_earnings",
                wl_changes.get("avoid_earnings", wl_changes.get("avoid", []))
            )
        else:
            avoid_raw = []
        avoid_syms = [
            (a.get("symbol", str(a)) if isinstance(a, dict) else str(a))
            for a in avoid_raw
        ]

        top_warning = ""
        warnings = memo.get("risk_warnings", [])
        if isinstance(warnings, list):
            for w in warnings:
                if isinstance(w, dict) and str(w.get("priority", "")).upper() == "HIGH":
                    top_warning = w.get("warning", w.get("detail", ""))[:150]
                    break
        elif isinstance(warnings, dict):
            first = next(iter(warnings.values()), None)
            if isinstance(first, list) and first:
                w = first[0]
                top_warning = (
                    w.get("warning", w.get("detail", str(w))) if isinstance(w, dict) else str(w)
                )[:150]

        # ── Crypto outlook (separate from equity macro) ──────────────────────
        crypto_outlook = memo.get("crypto_outlook") or {}
        c_regime         = ""
        c_mr_active      = True
        c_sentiment      = ""
        c_dominance      = ""
        c_opp_syms: list = []
        c_top_warning    = ""
        if isinstance(crypto_outlook, dict) and crypto_outlook:
            c_regime    = str(crypto_outlook.get("regime", ""))[:80]
            c_mr_active = bool(crypto_outlook.get("mean_reversion_active_crypto", True))
            c_sentiment = str(crypto_outlook.get("sentiment_read", ""))[:160]
            c_dominance = str(crypto_outlook.get("dominance_read", ""))[:160]
            c_opps      = crypto_outlook.get("top_crypto_opportunities", []) or []
            c_opp_syms  = [
                (o.get("symbol", o.get("pair", "?")) if isinstance(o, dict) else str(o))
                for o in c_opps[:3]
            ]
            # Highest-severity crypto-specific risk
            for r in (crypto_outlook.get("crypto_risk_warnings", []) or []):
                if isinstance(r, dict) and str(r.get("severity", "")).upper() in ("CRITICAL", "HIGH"):
                    c_top_warning = (r.get("detail") or r.get("description") or r.get("event") or "")[:150]
                    break

        lines = [
            f"WEEKLY RESEARCH BRIEF (confidence {confidence}/10):",
            "",
            "── EQUITY MACRO ──",
            f"- Market Regime: {regime}",
            f"- Mean Reversion (equities): {'ACTIVE' if mean_rev_active else 'PAUSED — seriously consider SKIP for any equity entry'}",
        ]
        if sizing_guidance:
            lines.append(f"- Position Sizing Guidance: {sizing_guidance}")
        if opp_syms:
            lines.append(f"- Flagged Equity Opportunities: {', '.join(opp_syms)}")
        if avoid_syms:
            lines.append(f"- AVOID (earnings risk): {', '.join(avoid_syms)}")

        if crypto_outlook:
            lines += [
                "",
                "── CRYPTO MACRO ──",
                f"- Crypto Regime: {c_regime or 'Unknown'}",
                f"- Mean Reversion (crypto): {'ACTIVE' if c_mr_active else 'PAUSED — seriously consider SKIP for any crypto entry'}",
            ]
            if c_sentiment:
                lines.append(f"- Sentiment Read: {c_sentiment}")
            if c_dominance:
                lines.append(f"- BTC Dominance Read: {c_dominance}")
            if c_opp_syms:
                lines.append(f"- Flagged Crypto Opportunities: {', '.join(c_opp_syms)}")
            if c_top_warning:
                lines.append(f"- Crypto Risk Warning: {c_top_warning}")

        if top_warning:
            lines.append("")
            lines.append(f"- Key Cross-Asset Risk Warning: {top_warning}")

        if confidence <= 3:
            lines.append("")
            lines.append("- VERY LOW CONFIDENCE: Only A-grade setups. Halve normal position size.")
        elif confidence <= 5:
            lines.append("")
            lines.append("- MODERATE CONFIDENCE: Be selective. Prefer high-signal setups.")

        # Strategy gate: if a class is paused, the bot should default to SKIP
        # for that asset class regardless of the live signal scoring.
        if not mean_rev_active or (crypto_outlook and not c_mr_active):
            lines.append("")
            paused = []
            if not mean_rev_active:                     paused.append("equities")
            if crypto_outlook and not c_mr_active:      paused.append("crypto")
            lines.append(
                f"- STRATEGY GATE: Mean reversion is PAUSED for {', '.join(paused)} this week. "
                "Default to SKIP for that asset class unless a setup is unusually strong (A-grade, 5+ signals)."
            )

        return "\n".join(lines)

    def _default_system_prompt(self) -> str:
        return """
You are a disciplined mean reversion trading agent targeting consistent small gains.
Never risk more than 1% per trade. Require 3+ signals to enter.
Skip if ADX > 25. Always set stop loss. Max 3 open positions.
Return decisions as JSON with action, symbol, quantity, entry_price, stop_loss, take_profit, reasoning, journal_entry.
"""
