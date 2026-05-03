"""
main.py
FlowTrader — Main entry point.
Orchestrates the Research → Decision → Execute → Journal loop.
Run this file on schedule via GitHub Actions or cron.
"""

import os
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
import pytz
import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("journal/bot.log", mode="a")
    ]
)
logger = logging.getLogger("FlowTrader")

# ── Import modules ─────────────────────────────────────────────────────────────
from data.fetcher import MarketDataFetcher
from agents.decision import TradingDecisionAgent
from agents.executor import OrderExecutor
from journal.logger import TradeJournal


def load_config() -> dict:
    """Load configuration from config.yaml."""
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def is_market_hours(config: dict) -> tuple[bool, str]:
    """
    Check if US market is open and within trading window.
    Returns (is_open, reason).
    """
    est = pytz.timezone("America/New_York")
    now = datetime.now(est)

    # Weekend check
    if now.weekday() >= 5:
        return False, f"Weekend — market closed ({now.strftime('%A')})"

    # Market hours: 9:30 AM to 4:00 PM EST
    market_open = now.replace(hour=9, minute=30, second=0)
    market_close = now.replace(hour=16, minute=0, second=0)
    cutoff = now.replace(hour=14, minute=55, second=0)

    if now < market_open:
        return False, f"Pre-market ({now.strftime('%H:%M')} EST)"

    if now > market_close:
        return False, f"After-hours ({now.strftime('%H:%M')} EST)"

    if now > cutoff:
        return False, f"Time gate active — no new entries after 14:55 EST ({now.strftime('%H:%M')} EST)"

    # Buffer after open
    buffer_end = now.replace(
        hour=9,
        minute=30 + config.get("schedule", {}).get("market_open_buffer_minutes", 15),
        second=0
    )
    if now < buffer_end:
        return False, f"Market just opened — waiting for volatility to settle"

    return True, f"Market open ({now.strftime('%H:%M')} EST)"


def run_trading_session(config: dict, mode: str = "full") -> dict:
    """
    Run one complete trading cycle:
    1. Fetch market data
    2. Get Claude's decision
    3. Execute if valid
    4. Write journal entry
    """

    logger.info("=" * 60)
    logger.info(f"FlowTrader session starting — Mode: {mode}")
    logger.info(f"Paper trading: {os.getenv('PAPER_TRADING', 'true')}")
    logger.info("=" * 60)

    # Initialize components
    fetcher = MarketDataFetcher()
    decision_agent = TradingDecisionAgent()
    executor = OrderExecutor()
    journal = TradeJournal()

    # Step 1: Get account status
    account = fetcher.get_account_snapshot()
    logger.info(f"Account: ${account.get('portfolio_value', 0):,.2f} | Positions: {account.get('open_positions', 0)}/3")

    # Check daily loss limit before doing anything
    day_pl = account.get("day_pl", 0)
    portfolio_value = account.get("portfolio_value", 10000)
    if day_pl < 0 and abs(day_pl) / portfolio_value >= 0.02:
        logger.warning(f"Daily loss limit hit: ${day_pl:,.2f} ({abs(day_pl)/portfolio_value:.1%}). Shutting down for today.")
        return {
            "status": "DAILY_LIMIT_HIT",
            "day_pl": day_pl,
            "message": "Trading stopped for the day due to loss limit"
        }

    # Step 2: Build market snapshot
    watchlist = config.get("watchlist", {}).get("equities", ["SPY", "QQQ"])
    logger.info(f"Scanning {len(watchlist)} symbols...")

    market_snapshot = fetcher.build_market_snapshot(watchlist)

    # Log top setups
    for item in market_snapshot.get("watchlist", [])[:3]:
        logger.info(
            f"  {item['symbol']}: Score={item['indicators'].get('signal_score', 0)} "
            f"Quality={item.get('setup_quality')} "
            f"RSI={item['indicators'].get('rsi', 'N/A')} "
            f"Regime={item['indicators'].get('regime', 'N/A')}"
        )

    # Step 3: Get Claude's decision
    logger.info("Sending to Claude for analysis...")
    decision = decision_agent.analyze_market(market_snapshot, account)
    logger.info(f"Claude decision: {decision.get('action')} {decision.get('symbol', '')} | Confidence: {decision.get('confidence', 'N/A')}")

    # Step 4: Execute
    execution_result = executor.place_order(decision, account)
    logger.info(f"Execution result: {execution_result.get('status')} — {execution_result.get('reason', 'OK')}")

    # Step 5: Journal
    journal_entry = journal.log_decision(
        decision=decision,
        execution_result=execution_result,
        market_snapshot=market_snapshot,
        account=account
    )

    # Step 6: Send notification if configured
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        send_telegram_notification(decision, execution_result, account)

    result = {
        "status": "COMPLETE",
        "action": decision.get("action"),
        "symbol": decision.get("symbol"),
        "execution": execution_result.get("status"),
        "signal_score": decision.get("signal_score", 0),
        "account_value": portfolio_value,
        "day_pl": day_pl
    }

    logger.info(f"Session complete: {json.dumps(result)}")
    return result


def run_weekly_review(config: dict) -> str:
    """Run the weekly self-review analysis."""
    logger.info("Running weekly review...")

    journal = TradeJournal()
    decision_agent = TradingDecisionAgent()

    entries = journal.get_entries(days=7)
    if not entries:
        logger.info("No journal entries found for weekly review")
        return "No entries to review"

    performance = journal.generate_performance_summary(days=7)
    review_text = decision_agent.run_weekly_review(entries)

    output_path = journal.write_weekly_summary(review_text, performance)
    logger.info(f"Weekly review saved to {output_path}")

    return review_text


def send_telegram_notification(decision: dict, execution: dict, account: dict):
    """Send a Telegram notification about the trade."""
    import requests

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    action = decision.get("action", "SKIP")
    symbol = decision.get("symbol", "N/A")
    status = execution.get("status", "N/A")

    if action in ["BUY", "SELL"]:
        message = (
            f"*FlowTrader* — Trade {'Placed ✅' if status == 'SUBMITTED' else 'Rejected ❌'}\n"
            f"Action: `{action} {symbol}`\n"
            f"Entry: ${decision.get('entry_price', 0):,.2f} | Stop: ${decision.get('stop_loss', 0):,.2f} | Target: ${decision.get('take_profit', 0):,.2f}\n"
            f"Signal Score: {decision.get('signal_score', 0)}/6 | Confidence: {decision.get('confidence', 'N/A')}\n"
            f"Account: ${account.get('portfolio_value', 0):,.2f} | Day P\\&L: ${account.get('day_pl', 0):+,.2f}\n"
            f"Reason: {execution.get('reason', 'Order placed')}"
        )
    else:
        message = (
            f"*FlowTrader* — Session Scanned, No Trade 📊\n"
            f"Top setup score: {decision.get('signal_score', 0)}/6\n"
            f"Reason: {decision.get('reasoning', '')[:200]}"
        )

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


if __name__ == "__main__":
    # Create required directories
    Path("journal").mkdir(exist_ok=True)

    config = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    if mode == "weekly-review":
        review = run_weekly_review(config)
        print(review)
    elif mode == "test":
        # Quick test — just fetch data and log, no trades
        fetcher = MarketDataFetcher()
        account = fetcher.get_account_snapshot()
        print("Account snapshot:")
        print(json.dumps(account, indent=2))
    else:
        # Check market hours
        is_open, reason = is_market_hours(config)
        if not is_open:
            logger.info(f"Market not available: {reason}")
            sys.exit(0)

        result = run_trading_session(config, mode)
        print(json.dumps(result, indent=2))
