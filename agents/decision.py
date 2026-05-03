"""
agents/decision.py
The Claude decision agent. Reads market snapshot, applies strategy rules,
and returns a structured trade decision with full reasoning.
"""

import os
import json
import logging
from typing import Optional
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class TradingDecisionAgent:
    """
    Claude-powered decision agent.
    Reads the market snapshot, checks guardrails, and outputs trade decisions.
    """

    def __init__(self, claude_md_path: str = "CLAUDE.md"):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = "claude-sonnet-4-6"

        # Load the system prompt from CLAUDE.md
        try:
            with open(claude_md_path, "r") as f:
                self.system_prompt = f.read()
        except FileNotFoundError:
            logger.warning("CLAUDE.md not found. Using default system prompt.")
            self.system_prompt = self._default_system_prompt()

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
                max_tokens=2000,
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
                "journal_entry": f"Session skipped due to API error: {str(e)}"
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
If signal_score < 3 or regime is TRENDING, the answer must be SKIP.
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
            return {"action": "SKIP", "symbol": symbol_data.get("symbol"), "reasoning": str(e)}

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

        # Get the top 3 setups (already sorted by score in fetcher)
        top_setups = [
            s for s in snapshot.get("watchlist", [])
            if s.get("setup_quality") not in ["SKIP", "NO_DATA"]
        ][:3]

        all_symbols = snapshot.get("watchlist", [])

        return f"""
TRADING SESSION — {snapshot.get('timestamp', 'Unknown time')}

ACCOUNT:
- Portfolio: ${account.get('portfolio_value', 0):,.2f}
- Buying Power: ${account.get('buying_power', 0):,.2f}
- Open Positions: {account.get('open_positions', 0)}/3
- Today's P&L: ${account.get('day_pl', 0):+,.2f}

TOP SETUPS RANKED BY SIGNAL SCORE:
{json.dumps(top_setups, indent=2)}

FULL WATCHLIST SUMMARY:
{json.dumps([{'symbol': s['symbol'], 'score': s['indicators'].get('signal_score',0), 'quality': s['setup_quality'], 'regime': s['indicators'].get('regime','?')} for s in all_symbols], indent=2)}

TASK:
1. Review the top setups against your signal rules and guardrails
2. Select the BEST single trade to place this session (or SKIP if nothing qualifies)
3. If multiple qualify, pick highest signal score with lowest risk
4. Return your decision as the JSON format specified in your rules
5. Write a detailed journal entry

Remember: No trade is better than a bad trade. SKIP freely.
"""

    def _parse_decision(self, raw_text: str) -> dict:
        """Extract JSON decision from Claude's response."""
        import re

        # Try to find JSON block in the response
        json_patterns = [
            r'```json\s*(.*?)\s*```',
            r'```\s*(.*?)\s*```',
            r'\{[^{}]*"action"[^{}]*\}'
        ]

        for pattern in json_patterns:
            matches = re.findall(pattern, raw_text, re.DOTALL)
            if matches:
                try:
                    return json.loads(matches[0])
                except json.JSONDecodeError:
                    continue

        # If no JSON found, try parsing the whole response
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            pass

        # Fallback: extract action keyword
        action = "SKIP"
        for word in ["BUY", "SELL", "HOLD", "SKIP"]:
            if word in raw_text.upper():
                action = word
                break

        return {
            "action": action,
            "symbol": None,
            "reasoning": raw_text[:500],
            "journal_entry": raw_text[:200],
            "parse_error": True
        }

    def _default_system_prompt(self) -> str:
        return """
You are a disciplined mean reversion trading agent targeting consistent small gains.
Never risk more than 1% per trade. Require 3+ signals to enter.
Skip if ADX > 25. Always set stop loss. Max 3 open positions.
Return decisions as JSON with action, symbol, quantity, entry_price, stop_loss, take_profit, reasoning, journal_entry.
"""
