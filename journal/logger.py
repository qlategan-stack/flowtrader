"""
journal/logger.py
Records every trading decision — trades and skips — to a structured JSONL file.
The journal is your most valuable asset. Read it weekly.
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
import pytz

logger = logging.getLogger(__name__)

JOURNAL_DIR = Path("journal")
JOURNAL_FILE = JOURNAL_DIR / "trades.jsonl"
SUMMARY_FILE = JOURNAL_DIR / "weekly_summary.md"


class TradeJournal:
    """
    Append-only trade journal. Logs every decision with full context.
    Provides weekly summary and performance analytics.
    """

    def __init__(self):
        JOURNAL_DIR.mkdir(exist_ok=True)

    def log_decision(
        self,
        decision: dict,
        execution_result: dict,
        market_snapshot: dict,
        account: dict
    ) -> dict:
        """
        Log a complete trading cycle entry.
        Every call to the bot creates one journal entry.
        """

        est = pytz.timezone("America/New_York")
        now = datetime.now(est)

        entry = {
            "timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time_est": now.strftime("%H:%M:%S"),
            "session_id": now.strftime("%Y%m%d_%H%M"),

            # Decision details
            "action": decision.get("action", "SKIP"),
            "symbol": decision.get("symbol"),
            "signal_score": decision.get("signal_score", 0),
            "signals_fired": decision.get("signals_fired", []),
            "confidence": decision.get("confidence", "LOW"),

            # Trade parameters
            "entry_price": decision.get("entry_price"),
            "stop_loss": decision.get("stop_loss"),
            "take_profit": decision.get("take_profit"),
            "quantity": decision.get("quantity"),
            "risk_reward": self._calc_rr(decision),

            # Execution result
            "execution_status": execution_result.get("status", "N/A"),
            "order_id": execution_result.get("order_id"),
            "paper_trade": execution_result.get("paper_trade", True),
            "rejection_reason": execution_result.get("reason"),

            # Account state at time of decision
            "account_value": account.get("portfolio_value"),
            "open_positions": account.get("open_positions"),
            "day_pl_at_decision": account.get("day_pl"),

            # Claude's reasoning (full text)
            "reasoning": decision.get("reasoning", ""),
            "journal_entry": decision.get("journal_entry", ""),

            # Top watchlist setup at time
            "top_setup_symbol": market_snapshot.get("watchlist", [{}])[0].get("symbol") if market_snapshot.get("watchlist") else None,
            "top_setup_score": market_snapshot.get("watchlist", [{}])[0].get("indicators", {}).get("signal_score", 0) if market_snapshot.get("watchlist") else 0,
        }

        # Append to JSONL file
        with open(JOURNAL_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

        logger.info(f"Journal entry written: {entry['action']} {entry.get('symbol', 'N/A')} | Score: {entry['signal_score']}")
        return entry

    def get_entries(self, days: int = 7) -> list:
        """Read journal entries from the last N days."""
        if not JOURNAL_FILE.exists():
            return []

        from datetime import timedelta
        est = pytz.timezone("America/New_York")
        cutoff = datetime.now(est) - timedelta(days=days)

        entries = []
        with open(JOURNAL_FILE, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    entry_time = datetime.fromisoformat(entry["timestamp"])
                    if entry_time.replace(tzinfo=None) > cutoff.replace(tzinfo=None):
                        entries.append(entry)
                except Exception:
                    continue

        return entries

    def generate_performance_summary(self, days: int = 7) -> dict:
        """
        Calculate performance metrics from journal.
        Returns win rate, avg R, and counts.
        """
        entries = self.get_entries(days)
        trades = [e for e in entries if e.get("action") in ["BUY", "SELL"] and e.get("execution_status") == "FILLED"]
        skips = [e for e in entries if e.get("action") == "SKIP"]

        if not trades:
            return {
                "period_days": days,
                "total_cycles": len(entries),
                "trades_placed": 0,
                "skips": len(skips),
                "message": "No completed trades in period"
            }

        # Basic counts
        buys = [t for t in trades if t.get("action") == "BUY"]
        sells = [t for t in trades if t.get("action") == "SELL"]

        # Signal score distribution
        scores = [t.get("signal_score", 0) for t in trades]
        avg_score = sum(scores) / len(scores) if scores else 0

        return {
            "period_days": days,
            "total_cycles": len(entries),
            "trades_placed": len(trades),
            "buys": len(buys),
            "sells": len(sells),
            "skips": len(skips),
            "skip_rate": f"{len(skips) / len(entries) * 100:.1f}%",
            "avg_signal_score": round(avg_score, 2),
            "paper_trades": sum(1 for t in trades if t.get("paper_trade", True)),
            "live_trades": sum(1 for t in trades if not t.get("paper_trade", True)),
        }

    def write_weekly_summary(self, review_text: str, performance: dict) -> str:
        """Write the weekly review to a markdown file."""
        from datetime import date
        week_str = date.today().strftime("Week of %Y-%m-%d")

        summary = f"""# FlowTrader Weekly Review — {week_str}

## Performance Metrics
```json
{json.dumps(performance, indent=2)}
```

## Claude's Self-Analysis
{review_text}

---
*Generated automatically by FlowTrader journal system*
"""
        with open(SUMMARY_FILE, "w") as f:
            f.write(summary)

        logger.info(f"Weekly summary written to {SUMMARY_FILE}")
        return str(SUMMARY_FILE)

    def _calc_rr(self, decision: dict) -> Optional[float]:
        """Calculate risk-to-reward ratio."""
        try:
            entry = float(decision.get("entry_price", 0))
            stop = float(decision.get("stop_loss", 0))
            target = float(decision.get("take_profit", 0))

            if entry <= 0 or stop <= 0 or target <= 0:
                return None

            risk = entry - stop
            reward = target - entry

            if risk <= 0:
                return None

            return round(reward / risk, 2)
        except Exception:
            return None
