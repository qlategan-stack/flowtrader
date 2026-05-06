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

Path("journal").mkdir(exist_ok=True)

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

    # Check daily loss limit before doing anything (reads from active risk profile)
    day_pl = account.get("day_pl", 0)
    portfolio_value = account.get("portfolio_value", 10000)
    from agents.executor import load_risk_profile
    _profile_name, _profile = load_risk_profile()
    daily_loss_limit = _profile.get("max_daily_loss_pct", 0.02)
    if day_pl < 0 and abs(day_pl) / portfolio_value >= daily_loss_limit:
        logger.warning(f"Daily loss limit hit: ${day_pl:,.2f} ({abs(day_pl)/portfolio_value:.1%} ≥ {daily_loss_limit:.0%}). Shutting down for today.")
        return {
            "status": "DAILY_LIMIT_HIT",
            "day_pl": day_pl,
            "message": "Trading stopped for the day due to loss limit"
        }

    # Step 2: Build market snapshot — combine equities (only when US market open)
    # with crypto (always, since it trades 24/7)
    watchlist_cfg   = config.get("watchlist", {})
    equity_symbols  = list(watchlist_cfg.get("equities", []))
    crypto_symbols  = list(watchlist_cfg.get("crypto", []))

    market_open, market_reason = is_market_hours(config)
    if not market_open:
        logger.info(f"Equity market gated off: {market_reason} — scanning crypto only")
        equity_symbols = []

    watchlist = equity_symbols + crypto_symbols
    if not watchlist:
        logger.info("No symbols to scan (equities gated, no crypto configured). Exiting.")
        return {"status": "NO_WATCHLIST", "message": market_reason}

    logger.info(f"Scanning {len(watchlist)} symbols ({len(equity_symbols)} equity, {len(crypto_symbols)} crypto)...")

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
        send_telegram_notification(decision, execution_result, account, market_snapshot)

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
    in_ids, out_ids = [], []
    try:
        in_ids = run_analyst_in(config)
    except Exception as e:
        logger.error(f"In-strategy analyst failed: {e}")
    try:
        out_ids = run_analyst_out(config)
    except Exception as e:
        logger.error(f"Out-strategy analyst failed: {e}")
    result = {"in_strategy": len(in_ids), "out_strategy": len(out_ids)}
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        _send_analyst_telegram_notification(len(in_ids), len(out_ids))
    return result


def _send_analyst_telegram_notification(in_count: int, out_count: int):
    """Send Telegram notification summarising the analyst run."""
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
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


def _tg_escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_reason(text: str, max_chars: int = 400) -> str:
    """Truncate at the last complete sentence within max_chars."""
    if not text or len(text) <= max_chars:
        return text or "—"
    chunk = text[:max_chars]
    for sep in (". ", "! ", "? "):
        idx = chunk.rfind(sep)
        if idx > max_chars // 2:
            return chunk[:idx + 1]
    idx = chunk.rfind(" ")
    return (chunk[:idx] if idx > 0 else chunk) + "…"


def send_telegram_notification(
    decision: dict, execution: dict, account: dict, market_snapshot: dict = None
):
    """Send a Telegram notification. Only fires on a trade or when positions are open."""
    import requests

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    action = decision.get("action", "SKIP")
    est = pytz.timezone("America/New_York")
    time_str = datetime.now(est).strftime("%H:%M EST")

    portfolio = account.get("portfolio_value", 0) or 0
    day_pl    = account.get("day_pl", 0) or 0
    pl_sign   = "+" if day_pl >= 0 else ""
    acct_line = f"💼 <b>${portfolio:,.0f}</b>  ·  Day P&amp;L: <b>{pl_sign}${day_pl:,.2f}</b>"

    positions = account.get("positions", [])

    if action in ["BUY", "SELL"]:
        symbol = decision.get("symbol") or "?"
        entry  = float(decision.get("entry_price") or 0)
        stop   = float(decision.get("stop_loss")   or 0)
        target = float(decision.get("take_profit") or 0)
        score  = decision.get("signal_score", 0)

        stop_pct   = (stop   - entry) / entry * 100 if entry else 0
        target_pct = (target - entry) / entry * 100 if entry else 0
        rr         = abs(target_pct / stop_pct) if stop_pct else 0

        status = execution.get("status", "")
        icon = "✅" if status in ("SUBMITTED", "FILLED", "SIMULATED") else "❌"
        status_label = {
            "SUBMITTED": "Order placed",
            "FILLED":    "Filled",
            "SIMULATED": "Paper trade",
            "REJECTED":  "Rejected",
            "ERROR":     "Error",
        }.get(status, status or "Unknown")
        venue = "Bybit" if "/" in str(symbol) else "Alpaca"

        price_block = (
            f"<code>"
            f"Entry   ${entry:>12,.2f}\n"
            f"Stop    ${stop:>12,.2f}  ({stop_pct:+.1f}%)\n"
            f"Target  ${target:>12,.2f}  ({target_pct:+.1f}%)\n"
            f"R:R 1:{rr:.1f}   Score {score}/6"
            f"</code>"
        )

        lines = [
            f"{icon} <b>FlowTrader — {action} {_tg_escape(symbol)}</b>",
            f"<i>{status_label}  ·  {venue}</i>",
            "",
            price_block,
        ]
        if execution.get("reason"):
            lines.append(f"⚠️ {_tg_escape(str(execution['reason'])[:120])}")
        lines += ["", acct_line, f"🕐 {time_str}"]

    elif positions:
        # No trade this session but positions are open — show their status
        pos_lines = []
        for p in positions:
            sym  = p.get("symbol", "?")
            qty  = p.get("qty", 0)
            pl   = p.get("unrealized_pl", 0) or 0
            plpc = (p.get("unrealized_plpc", 0) or 0) * 100
            sign = "+" if pl >= 0 else ""
            pos_lines.append(
                f"  {_tg_escape(sym):<12} qty {qty:.4g}   {sign}${pl:,.2f} ({sign}{plpc:.1f}%)"
            )

        lines = [
            f"📋 <b>FlowTrader — {len(positions)} Position{'s' if len(positions) != 1 else ''} Open</b>",
            "",
            "<code>" + "\n".join(pos_lines) + "</code>",
            "",
            acct_line,
            f"🕐 {time_str}",
        ]

    else:
        # Nothing traded, nothing open — stay silent
        return

    message = "\n".join(lines)
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
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
    elif mode == "research-analyst":
        from agents.researcher import ResearchAnalyst
        watchlist = config.get("watchlist", {}).get("equities", [])
        analyst = ResearchAnalyst()
        result = analyst.run_full_analysis(current_watchlist=watchlist)
        print(json.dumps(result, indent=2))
    elif mode == "test":
        # Quick test — just fetch data and log, no trades
        fetcher = MarketDataFetcher()
        account = fetcher.get_account_snapshot()
        print("Account snapshot:")
        print(json.dumps(account, indent=2))

    elif mode == "analyst-in":
        result = run_analyst_in(config)
        print(json.dumps({"status": "complete", "suggestions_generated": len(result)}))

    elif mode == "analyst-out":
        result = run_analyst_out(config)
        print(json.dumps({"status": "complete", "suggestions_generated": len(result)}))

    elif mode == "analyst-full":
        result = run_analyst_full(config)
        print(json.dumps({"status": "complete", **result}))

    else:
        # Equity market hours are checked inside run_trading_session — crypto
        # runs 24/7 so we no longer exit on closed-market.
        result = run_trading_session(config, mode)
        print(json.dumps(result, indent=2))
