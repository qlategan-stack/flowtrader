"""
main.py
FlowTrader — Main entry point.
Orchestrates the Research → Decision → Execute → Journal loop.
Run this file on schedule via GitHub Actions or cron.
"""

import os
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
import pytz
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# ── SSL fix for Norton Antivirus TLS inspection (Windows) ─────────────────────
# Norton re-signs TLS certificates with its own CA whose Basic Constraints
# extension is not marked critical.  Python 3.13 rejects this under strict
# RFC 5280 enforcement, breaking every HTTPS call (Alpaca, Anthropic, Binance,
# Bybit, Telegram).  Patching the default HTTPAdapter before any Session is
# created relaxes only that specific check while keeping full cert validation.
# Root-caused 2026-05-20 (C-2 in audit) — issuer: "Norton Web/Mail Shield Root".
import ssl as _ssl
from requests.adapters import HTTPAdapter as _HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context as _create_ctx
import requests as _requests


class _NortonCompatAdapter(_HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = _create_ctx()
        ctx.load_default_certs()
        ctx.verify_flags &= ~_ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        ctx = _create_ctx()
        ctx.load_default_certs()
        ctx.verify_flags &= ~_ssl.VERIFY_X509_STRICT
        proxy_kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **proxy_kwargs)


# Monkey-patch Session so every library that uses requests gets the fix.
_orig_session_init = _requests.Session.__init__


def _patched_session_init(self, *args, **kwargs):
    _orig_session_init(self, *args, **kwargs)
    self.mount("https://", _NortonCompatAdapter())
    self.mount("http://",  _NortonCompatAdapter())


_requests.Session.__init__ = _patched_session_init
# ── End SSL fix ───────────────────────────────────────────────────────────────

Path("journal").mkdir(exist_ok=True)

# ── Logging setup ──────────────────────────────────────────────────────────────
# Secret-redacting filter — applied to every record before it reaches
# stdout or bot.log. journal/bot.log is synced to the public
# flowtrader-dashboard repo via a GitHub Action, so anything that leaks
# into a log eventually becomes public. Patterns covered:
#   • Telegram bot tokens:   bot<digits>:<token>
#   • Generic secret= / token= / api_key= in query strings
#   • Bearer / Authorization tokens
_SECRET_PATTERNS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{20,}"), "bot<REDACTED>"),
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)=([^\s&'\"]{8,})"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer <REDACTED>"),
]


class _SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat, repl in _SECRET_PATTERNS:
            msg = pat.sub(repl, msg)
        record.msg = msg
        record.args = None
        return True


_stdout_handler = logging.StreamHandler(sys.stdout)
# Rotate bot.log at 10 MB with 5 backups (bot.log → bot.log.1 → … → bot.log.5).
# Unrotated logs grew to 10k+ lines and started bloating audits — flagged as L-5
# on 2026-05-23.
from logging.handlers import RotatingFileHandler as _RotatingFileHandler  # noqa: E402
_file_handler = _RotatingFileHandler(
    "journal/bot.log", mode="a", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
for _h in (_stdout_handler, _file_handler):
    _h.addFilter(_SecretRedactingFilter())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[_stdout_handler, _file_handler],
)
# Attach the redactor to the root logger as well so third-party libraries
# (anthropic, ccxt, urllib3, telegram) that get their own loggers via
# logging.getLogger(...) still pass through the filter.
logging.getLogger().addFilter(_SecretRedactingFilter())
logger = logging.getLogger("FlowTrader")

# ── "Already hold" cooldown store ─────────────────────────────────────────────
# H-2 fix (audit 2026-05-25): when the executor rejects a BUY with
# "Already hold …" the signal kept re-firing every 30 minutes because nothing
# suppressed the symbol between cycles.  This file-based store records which
# symbols are on cooldown (timestamped) so they can be filtered from the market
# snapshot before Claude sees them.  The cooldown is N cycles (each cycle is
# ~30 min), configurable via ALREADY_HELD_COOLDOWN_CYCLES env var (default 3).
_ALREADY_HELD_COOLDOWN_FILE = Path("journal/already_held_cooldown.json")
_ALREADY_HELD_COOLDOWN_CYCLES = int(os.getenv("ALREADY_HELD_COOLDOWN_CYCLES", "3"))

# M-2 fix (audit 2026-05-26): ETH/USDT was 40% of all 30d BUY entries because
# a hot signal kept re-firing every cycle. Cap entries per symbol per calendar
# day so attempts spread across the watchlist instead of concentrating.
_MAX_BUYS_PER_SYMBOL_PER_DAY = int(os.getenv("MAX_BUYS_PER_SYMBOL_PER_DAY", "2"))
_TRADES_JOURNAL_FILE = Path("journal/trades.jsonl")


def _load_cooldown() -> dict:
    """Return {symbol: cycles_remaining} from the cooldown store."""
    if not _ALREADY_HELD_COOLDOWN_FILE.exists():
        return {}
    try:
        return json.loads(_ALREADY_HELD_COOLDOWN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cooldown(state: dict) -> None:
    """Persist the cooldown store (removes symbols whose count hit 0)."""
    active = {sym: n for sym, n in state.items() if n > 0}
    _ALREADY_HELD_COOLDOWN_FILE.write_text(json.dumps(active, indent=2), encoding="utf-8")


def _tick_cooldown(state: dict) -> dict:
    """Decrement every symbol's remaining cycle count by 1."""
    return {sym: max(0, n - 1) for sym, n in state.items()}


def _add_cooldown(state: dict, symbol: str) -> dict:
    """Put a symbol on cooldown for _ALREADY_HELD_COOLDOWN_CYCLES cycles."""
    state[symbol] = _ALREADY_HELD_COOLDOWN_CYCLES
    return state


def _apply_cooldown_filter(market_snapshot: dict, state: dict) -> dict:
    """
    Remove symbols on cooldown from the watchlist inside market_snapshot so
    Claude never sees them (and cannot propose a BUY that will be immediately
    rejected again).  Returns a shallow copy with the filtered watchlist.
    """
    on_cooldown = {sym for sym, n in state.items() if n > 0}
    if not on_cooldown:
        return market_snapshot
    original = market_snapshot.get("watchlist") or []
    filtered = [s for s in original if s.get("symbol") not in on_cooldown]
    if len(filtered) < len(original):
        skipped = [s.get("symbol") for s in original if s.get("symbol") in on_cooldown]
        logger.info(f"Already-held cooldown: suppressing {skipped} from this cycle")
    return {**market_snapshot, "watchlist": filtered}


def _fetch_crypto_balance() -> dict:
    """Live crypto exchange balance; returns {} on failure (logged warning)."""
    try:
        from agents.executor import _get_crypto_client
        return _get_crypto_client().get_balance() or {}
    except Exception as e:
        logger.warning(f"Crypto balance fetch failed: {e}")
        return {}


def _significant_crypto_positions(crypto_balance: dict, min_usd: float = 10.0) -> list[dict]:
    """Crypto positions worth >= min_usd (excludes faucet dust)."""
    return [
        p for p in (crypto_balance.get("positions") or [])
        if float(p.get("value_usd") or 0) >= min_usd
    ]


def _symbols_at_daily_buy_cap(
    today_est: str | None = None,
    cap: int = _MAX_BUYS_PER_SYMBOL_PER_DAY,
    journal_file: Path | None = None,
) -> set[str]:
    """
    Return symbols that have already hit the per-day BUY cap, computed by
    scanning trades.jsonl for today's BUYs (any execution_status that
    represents a real attempt — SUBMITTED/FILLED/CANCELLED/REJECTED count).

    M-2 fix (audit 2026-05-26): one hot symbol was dominating activity; this
    spreads the next entries across the rest of the watchlist.
    """
    journal = journal_file or _TRADES_JOURNAL_FILE
    if not journal.exists():
        return set()
    if today_est is None:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        today_est = _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    real_attempt = {"SUBMITTED", "FILLED", "PARTIAL", "CANCELLED", "REJECTED", "SIMULATED"}
    try:
        for line in journal.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("date") != today_est:
                continue
            if (row.get("action") or "").upper() != "BUY":
                continue
            if (row.get("execution_status") or "").upper() not in real_attempt:
                continue
            sym = row.get("symbol")
            if sym:
                counts[sym] = counts.get(sym, 0) + 1
    except Exception as e:
        logger.warning(f"Per-symbol throttle check failed: {e}")
        return set()
    return {sym for sym, n in counts.items() if n >= cap}


def _augment_account_with_combined_positions(account: dict, crypto_balance: dict) -> dict:
    """
    Mutate `account` so `open_positions` is the venue-aggregate (equity +
    non-dust crypto) and the per-venue breakdowns are exposed.

    H-3 fix (audit 2026-05-26): the executor's max_open_positions gate was
    seeing equity-only counts, so equity=3 + crypto=4 read as "3/5 used" and
    the bot could exceed the hard cap of 5 with crypto positions. Combining
    here means every downstream consumer (decision agent prompt, executor
    gate) sees the same number.
    """
    equity_count = int(account.get("open_positions") or 0)
    crypto_count = len(_significant_crypto_positions(crypto_balance))
    account["equity_positions"] = equity_count
    account["crypto_positions"] = crypto_count
    account["open_positions"] = equity_count + crypto_count
    return account


def _compute_held_symbols(
    account: dict,
    watchlist_symbols: list[str],
    crypto_balance: dict | None = None,
) -> set[str]:
    """
    Return the set of symbols the bot already holds, across equity and crypto
    venues, matched against the supplied watchlist.

    H-2 fix (audit 2026-05-26): the cycle-counter cooldown expired after 3
    cycles but real positions persist for days, so NEAR/USDT and LTC/USDT
    were re-proposed every ~2 hours and re-rejected — wasting Claude calls.
    The authoritative source is the live position list, not a counter.

    Pass `crypto_balance` to reuse a fetch from elsewhere in the cycle
    (e.g. H-3 position-count); omit it for one-shot use.
    """
    held: set[str] = set()

    # Equities — Alpaca account snapshot
    for pos in account.get("positions") or []:
        sym = pos.get("symbol")
        if sym and float(pos.get("qty", 0) or 0) > 0:
            held.add(sym)

    # Crypto — match base coin against any "*/USDT" pair in the watchlist
    crypto_pairs = [s for s in watchlist_symbols if "/" in s]
    if crypto_pairs:
        if crypto_balance is None:
            crypto_balance = _fetch_crypto_balance()
        significant = _significant_crypto_positions(crypto_balance)
        held_coins = {str(p.get("currency", "")).upper() for p in significant}
        for pair in crypto_pairs:
            if pair.split("/")[0].upper() in held_coins:
                held.add(pair)

    return held
# ── End cooldown store ─────────────────────────────────────────────────────────

# ── Import modules ─────────────────────────────────────────────────────────────
from data.fetcher import MarketDataFetcher
from agents.decision import TradingDecisionAgent
from agents.executor import OrderExecutor
from journal.logger import TradeJournal


def load_config() -> dict:
    """Load configuration from config.yaml."""
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def check_exits(market_snapshot: dict, account: dict) -> list[dict]:
    """
    Inspect each open position against current indicators and return SELL
    decisions for positions whose strategy thesis has played out or been
    invalidated.

    Mean-reversion exit triggers (any one fires):
      • RSI ≥ 60 — mean-reverted from oversold to neutral/overbought
      • Price ≥ MA20 — price has reverted to the mean (target reached)
      • Regime turned TRENDING (ADX > 30) — MR assumption violated

    These MR-specific triggers are suppressed for positions whose current
    indicators show MOMENTUM mode: RSI > 60 is expected behaviour for a
    momentum trade and TRENDING is exactly the regime we want to ride. The
    Alpaca/Bybit bracket order (momentum_stop / momentum_target placed at
    entry) handles momentum exits automatically.
    """
    positions = account.get("positions", []) or []
    if not positions:
        return []

    watchlist = market_snapshot.get("watchlist", []) or []
    by_symbol = {item.get("symbol"): item for item in watchlist}

    decisions = []
    for pos in positions:
        sym = pos.get("symbol")
        item = by_symbol.get(sym)
        if not item:
            continue  # No fresh data for this symbol — skip exit check
        ind = item.get("indicators", {})
        if "error" in ind:
            continue

        # Don't apply MR exits to momentum positions; the bracket handles them.
        if ind.get("strategy_mode") == "MOMENTUM":
            continue

        rsi    = float(ind.get("rsi", 50))
        price  = float(ind.get("current_price", 0))
        ma20   = float(ind.get("ma20", 0))
        regime = ind.get("regime", "RANGING")

        reason = None
        if rsi >= 60:
            reason = f"RSI {rsi:.1f} ≥ 60 (mean reverted)"
        elif ma20 > 0 and price >= ma20:
            reason = f"Price ${price:.2f} reached MA20 ${ma20:.2f}"
        elif regime == "TRENDING":
            reason = "Regime turned TRENDING — exit mean-reversion position"

        if reason:
            decisions.append({
                "action":      "SELL",
                "symbol":      sym,
                "quantity":    float(pos.get("qty", 0)),
                "entry_price": price,
                "stop_loss":   0,
                "take_profit": 0,
                "signal_score": ind.get("signal_score", 0),
                "signals_fired": ind.get("signals_fired", []),
                "reasoning":   f"Exit signal: {reason}",
                "journal_entry": f"Closing {sym} ({pos.get('qty')} @ ${price:.2f}): {reason}",
                "confidence":  "HIGH",
            })

    return decisions


def is_trading_window(config: dict) -> tuple[bool, str]:
    """
    Check whether the current time falls inside the configured 12-hour
    trading window (default 08:00–20:00 EST, Mon–Fri).

    Both equities AND crypto respect this gate — the bot is intentionally
    dark for 12 h overnight so no new positions are opened.  Existing
    bracket orders (stop-loss / take-profit) stay live on the exchange
    24/7 regardless — the broker manages those independently.

    Returns (entries_allowed, reason).
    """
    est = pytz.timezone("America/New_York")
    now = datetime.now(est)
    sched = config.get("schedule", {})

    # Weekend check — no trading Saturday or Sunday
    if now.weekday() >= 5:
        return False, f"Weekend — trading window closed ({now.strftime('%A')})"

    win_start_h = sched.get("window_start_hour",  8)
    win_start_m = sched.get("window_start_minute", 0)
    win_end_h   = sched.get("window_end_hour",   20)
    win_end_m   = sched.get("window_end_minute",  0)
    cutoff_mins = sched.get("window_entry_cutoff_minutes", 15)

    window_open  = now.replace(hour=win_start_h, minute=win_start_m, second=0, microsecond=0)
    window_close = now.replace(hour=win_end_h,   minute=win_end_m,   second=0, microsecond=0)

    # Entry cutoff: N minutes before window end
    from datetime import timedelta
    entry_cutoff = window_close - timedelta(minutes=cutoff_mins)

    if now < window_open:
        mins_to_open = int((window_open - now).total_seconds() / 60)
        return False, (
            f"Off-window — opens at {win_start_h:02d}:{win_start_m:02d} EST "
            f"({mins_to_open} min away, now {now.strftime('%H:%M')} EST)"
        )

    if now >= window_close:
        return False, (
            f"Off-window — closed at {win_end_h:02d}:{win_end_m:02d} EST "
            f"(now {now.strftime('%H:%M')} EST). "
            f"Existing bracket orders remain active on the exchange."
        )

    if now >= entry_cutoff:
        return False, (
            f"Entry cutoff reached — no new entries in final {cutoff_mins} min "
            f"of window ({now.strftime('%H:%M')} EST, cutoff {entry_cutoff.strftime('%H:%M')} EST)"
        )

    # Equity-specific: don't trade in the first N minutes after equity open
    equity_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    buffer_mins = sched.get("market_open_buffer_minutes", 15)
    if now >= equity_open:
        buffer_end = equity_open.replace(minute=equity_open.minute + buffer_mins)
        if now < buffer_end:
            # Window is open, but equity volatility buffer applies.
            # Crypto is fine to trade; equities will be gated separately below.
            pass  # caller handles equity vs crypto split

    return True, f"Trading window open ({now.strftime('%H:%M')} EST, window {win_start_h:02d}:{win_start_m:02d}–{win_end_h:02d}:{win_end_m:02d} EST)"


def is_equity_active(config: dict, now=None) -> tuple[bool, str]:
    """
    Within the trading window, check whether US equity market is open
    and past the volatility buffer. Crypto ignores this — it is active
    for the full 12-hour window.
    Returns (equities_allowed, reason).
    """
    est = pytz.timezone("America/New_York")
    if now is None:
        now = datetime.now(est)
    sched = config.get("schedule", {})

    equity_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    equity_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    buffer_mins  = sched.get("market_open_buffer_minutes", 15)

    from datetime import timedelta
    buffer_end = equity_open + timedelta(minutes=buffer_mins)

    if now < equity_open:
        return False, f"Pre-equity-open ({now.strftime('%H:%M')} EST)"
    if now >= equity_close:
        return False, f"Equity market closed ({now.strftime('%H:%M')} EST)"
    if now < buffer_end:
        return False, f"Equity volatility buffer ({now.strftime('%H:%M')} EST, trading starts {buffer_end.strftime('%H:%M')} EST)"

    return True, f"Equity market open ({now.strftime('%H:%M')} EST)"


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

    # Load and tick the "already held" cooldown store.  Every new session
    # decrements each symbol's remaining count by 1, releasing the suppression
    # after _ALREADY_HELD_COOLDOWN_CYCLES cycles (~1.5 h at 30-min cadence).
    _cooldown_state = _tick_cooldown(_load_cooldown())
    _save_cooldown(_cooldown_state)

    # Step 2: Build market snapshot.
    # Both equities and crypto are gated to the 12-hour trading window
    # (08:00–20:00 EST, Mon–Fri). Outside the window the bot exits early —
    # no scanning, no Claude call, no journal entry.  Existing bracket orders
    # remain active on the exchange regardless (broker manages them).
    watchlist_cfg  = config.get("watchlist", {})
    equity_symbols = list(watchlist_cfg.get("equities", []))
    crypto_symbols = list(watchlist_cfg.get("crypto", []))

    window_open, window_reason = is_trading_window(config)
    if not window_open:
        logger.info(f"Trading window closed: {window_reason}")
        return {"status": "OFF_WINDOW", "message": window_reason}

    # Within the window, equities are additionally gated to exchange hours
    # (09:45–16:00 EST). Crypto trades for the full window.
    equity_active, equity_reason = is_equity_active(config)
    if not equity_active:
        logger.info(f"Equity market gated off: {equity_reason} — scanning crypto only")
        equity_symbols = []

    watchlist = equity_symbols + crypto_symbols
    if not watchlist:
        logger.info("No symbols to scan this cycle. Exiting.")
        return {"status": "NO_WATCHLIST", "message": window_reason}

    logger.info(f"Scanning {len(watchlist)} symbols ({len(equity_symbols)} equity, {len(crypto_symbols)} crypto)...")

    market_snapshot = fetcher.build_market_snapshot(watchlist)

    # Log top setups
    for item in market_snapshot.get("watchlist", [])[:3]:
        ind = item["indicators"]
        logger.info(
            f"  {item['symbol']}: Mode={ind.get('strategy_mode', 'NONE')} "
            f"MR={ind.get('signal_score', 0)} Mom={ind.get('momentum_score', 0)} "
            f"Quality={item.get('setup_quality')} "
            f"RSI={ind.get('rsi', 'N/A')} "
            f"Regime={ind.get('regime', 'N/A')}"
        )

    # Step 3a: Check existing positions for exit signals BEFORE new entries.
    # This implements proactive position management — closes positions whose
    # mean-reversion thesis has played out, rather than waiting for the bracket
    # stop/target to fire.
    exit_decisions = check_exits(market_snapshot, account)
    for exit_decision in exit_decisions:
        logger.info(f"Exit triggered: {exit_decision['symbol']} — {exit_decision['reasoning']}")
        exit_result = executor.place_order(exit_decision, account)
        logger.info(f"Exit execution: {exit_result.get('status')} — {exit_result.get('reason', 'OK')}")
        journal.log_decision(
            decision=exit_decision,
            execution_result=exit_result,
            market_snapshot=market_snapshot,
            account=account,
        )
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            send_telegram_notification(exit_decision, exit_result, account, market_snapshot)
        # Refresh account context so the new-entry logic sees the updated positions
        account = fetcher.get_account_snapshot()

    # Step 3b: Get Claude's decision for new entries.
    # Fetch crypto balance ONCE per cycle and use it for both (a) held-symbols
    # watchlist filter [H-2] and (b) combined position count for the
    # max_open_positions gate [H-3]. Both audit 2026-05-26.
    _watchlist_symbols = [s.get("symbol") for s in (market_snapshot.get("watchlist") or []) if s.get("symbol")]
    _crypto_bal = _fetch_crypto_balance()
    _augment_account_with_combined_positions(account, _crypto_bal)  # H-3
    _held = _compute_held_symbols(account, _watchlist_symbols, crypto_balance=_crypto_bal)  # H-2
    _throttled = _symbols_at_daily_buy_cap()  # M-2 — symbols that hit the daily cap
    if _held:
        logger.info(f"Already-holding filter: suppressing {sorted(_held)} from this cycle (live positions)")
    if _throttled:
        logger.info(
            f"Per-symbol daily throttle: suppressing {sorted(_throttled)} "
            f"(≥{_MAX_BUYS_PER_SYMBOL_PER_DAY} BUYs today)"
        )
    if account.get("crypto_positions"):
        logger.info(
            f"Combined position count: {account['open_positions']} "
            f"(equity={account['equity_positions']}, crypto={account['crypto_positions']})"
        )
    _suppress = _held | _throttled
    _combined_state = {**_cooldown_state, **{sym: 1 for sym in _suppress}}
    market_snapshot_filtered = _apply_cooldown_filter(market_snapshot, _combined_state)
    logger.info("Sending to Claude for analysis...")
    decision = decision_agent.analyze_market(market_snapshot_filtered, account)
    logger.info(f"Claude decision: {decision.get('action')} {decision.get('symbol', '')} | Confidence: {decision.get('confidence', 'N/A')}")

    # Step 4: Execute
    execution_result = executor.place_order(decision, account)
    logger.info(f"Execution result: {execution_result.get('status')} — {execution_result.get('reason', 'OK')}")

    # H-2 fix: if the executor rejected with "Already hold …", put the symbol
    # on cooldown so the signal is suppressed for the next N cycles.
    _exec_reason = execution_result.get("reason", "")
    if (
        execution_result.get("status") == "CANCELLED"
        and "Already hold" in _exec_reason
        and decision.get("symbol")
    ):
        _cooldown_state = _add_cooldown(_cooldown_state, decision["symbol"])
        _save_cooldown(_cooldown_state)
        logger.info(
            f"Already-held cooldown set for {decision['symbol']} "
            f"({_ALREADY_HELD_COOLDOWN_CYCLES} cycles)"
        )

    # Step 4b: Enrich SKIP reason with best-candidate info so the journal is
    # auditable.  "Action is SKIP — no order placed" with no signal context is
    # M-1 from the 2026-05-25 audit — 24 consecutive SKIPs all identical.
    if execution_result.get("status") == "SKIPPED":
        wl = market_snapshot.get("watchlist") or []
        if wl:
            best = max(
                wl,
                key=lambda s: (
                    s.get("indicators", {}).get("signal_score", 0)
                    + s.get("indicators", {}).get("momentum_score", 0)
                )
            )
            best_sym   = best.get("symbol", "?")
            best_ind   = best.get("indicators", {})
            best_mr    = best_ind.get("signal_score", 0)
            best_mom   = best_ind.get("momentum_score", 0)
            best_mode  = best_ind.get("strategy_mode", "NONE")
            best_rsi   = best_ind.get("rsi", "?")
            best_adx   = best_ind.get("adx", "?")
            best_regime = best_ind.get("regime", "?")
            min_score  = executor.profile.get("min_signal_score", 2)
            active_score = best_mr if best_mode != "MOMENTUM" else best_mom
            skip_detail = (
                f"Best candidate: {best_sym} "
                f"mode={best_mode} score={active_score} (needs {min_score}+) "
                f"MR={best_mr} Mom={best_mom} "
                f"RSI={best_rsi} ADX={best_adx} regime={best_regime}"
            )
            existing_reason = execution_result.get("reason", "")
            execution_result["reason"] = f"{existing_reason} | {skip_detail}"
            logger.info(f"SKIP detail: {skip_detail}")

    # Step 5: Journal — log the snapshot Claude actually saw, so top_setup_symbol
    # reflects the post-filter watchlist (without held/cooldowned symbols).
    journal_entry = journal.log_decision(
        decision=decision,
        execution_result=execution_result,
        market_snapshot=market_snapshot_filtered,
        account=account
    )

    # Step 6: Send notification if configured
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        send_telegram_notification(decision, execution_result, account, market_snapshot)

    # Step 6b: Bot-level API failure alert (rate-limited so it doesn't spam).
    # Fires the first time Anthropic rejects a request, then again every 24h
    # if the same error persists, and immediately on a different error kind.
    if decision.get("api_error") and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        from journal.api_alert_state import should_alert, record_alert
        state_path = Path("journal/last_api_alert.json")
        kind = decision.get("api_error_kind", "other")
        if should_alert(state_path, kind):
            detail = (decision.get("reasoning") or "")[:300]
            text = (
                f"🚨 FlowTrader — Anthropic API failure ({kind})\n\n"
                f"The bot could not get a decision from Claude this cycle. "
                f"All sessions will SKIP until this resolves.\n\n"
                f"Detail: {detail}"
            )
            send_telegram_alert(text)
            record_alert(state_path, kind)

    result = {
        "status": "COMPLETE",
        "action": decision.get("action"),
        "symbol": decision.get("symbol"),
        "execution": execution_result.get("status"),
        "signal_score": decision.get("signal_score", 0),
        "account_value": portfolio_value,
        "day_pl": day_pl
    }

    # Log a compact one-liner; the full structured record is in trades.jsonl.
    # The bare JSON print that used to follow this was captured by run_bot.bat
    # into run_bot.log, breaking log parsers (L-2 in audit 2026-05-25).
    logger.info(
        f"Session complete: action={result.get('action')} "
        f"symbol={result.get('symbol') or '—'} "
        f"exec={result.get('execution')} "
        f"score={result.get('signal_score', 0)} "
        f"acct=${result.get('account_value', 0):,.0f}"
    )
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


def _scrub_telegram_token(msg: str, token: str | None) -> str:
    # urllib3 puts the full request URL (with bot token in path) into the
    # str() of MaxRetryError and friends — stringifying the exception into
    # a log line would leak the token. Strip it before any logger.* call.
    return msg.replace(token, "<TELEGRAM_TOKEN>") if token else msg


def send_telegram_alert(text: str) -> None:
    """
    Send a plain-text Telegram alert. Used for bot-level failures
    (e.g. Anthropic API rejected the request).

    Plain text — no parse_mode — so special characters in error messages
    cannot break Telegram's MarkdownV2/HTML parser and silently drop the alert.
    """
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Telegram alert failed: {_scrub_telegram_token(str(e), token)}")


def _send_analyst_telegram_notification(in_count: int, out_count: int):
    """
    Send Telegram notification summarising the analyst run.

    Uses HTML parse mode (matches send_telegram_notification) instead of
    MarkdownV2. MarkdownV2 requires escaping ~16 special characters and
    silently fails the whole message when one slips through; HTML only
    cares about &, <, > and is far more forgiving for free-form text.
    """
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    message = (
        f"<b>FlowTrader Analyst</b> — Daily Review Complete 🧠\n"
        f"In-Strategy: {in_count} suggestion(s)\n"
        f"Out-Strategy: {out_count} suggestion(s)\n"
        f"→ Review on the dashboard"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Analyst Telegram notification failed: {_scrub_telegram_token(str(e), token)}")


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
        # For crypto trades, the executor attaches the venue (Bybit/Binance)
        # account snapshot — show that instead of the Alpaca paper account so
        # the displayed % cap, total, and the "10% of account" rejection
        # message all reference the same balance.
        venue_account = execution.get("venue_account") if isinstance(execution, dict) else None
        if venue_account:
            venue_value = float(venue_account.get("account_value", 0) or 0)
            exchange    = str(venue_account.get("exchange", "crypto")).capitalize()
            # Crypto venues don't expose a session day_pl, so the value would
            # be the Alpaca paper account's P&L — confusing on a Bybit/Binance
            # message. Show the venue balance alone.
            acct_line = f"💼 <b>${venue_value:,.0f}</b> ({exchange})"
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
        # Heartbeat: nothing traded, nothing open — emit a 1-line status so the
        # user can confirm the bot is alive, on the right profile, and seeing
        # current signal scores. This is especially valuable during validation;
        # quiet runs from a working bot still shouldn't be silent.
        wl = (market_snapshot or {}).get("watchlist", []) or []
        scores = [s.get("indicators", {}).get("signal_score", 0) for s in wl]
        top_score = max(scores) if scores else 0
        top_sym   = ""
        if scores:
            top_sym = max(wl, key=lambda s: s.get("indicators", {}).get("signal_score", 0)).get("symbol", "")

        try:
            from agents.executor import load_risk_profile
            profile_name, profile = load_risk_profile()
            min_score = profile.get("min_signal_score", "?")
        except Exception:
            profile_name = "?"
            min_score = "?"

        lines = [
            f"🟢 <b>FlowTrader — Heartbeat</b>",
            f"<i>Idle  ·  {len(wl)} symbols scanned</i>",
            "",
            f"<code>Profile     {_tg_escape(profile_name)} (min {min_score}/6)\n"
            f"Top score   {top_score} ({_tg_escape(top_sym) or '—'})</code>",
            "",
            acct_line,
            f"🕐 {time_str}",
        ]

    message = "\n".join(lines)
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Telegram notification failed: {_scrub_telegram_token(str(e), token)}")


if __name__ == "__main__":
    # Create required directories
    Path("journal").mkdir(exist_ok=True)

    config = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    if mode == "weekly-review":
        review = run_weekly_review(config)
        print(review)
    elif mode == "research-analyst":
        from agents.researcher import ResearchAnalyst, record_refresh
        watchlist        = config.get("watchlist", {}).get("equities", [])
        crypto_watchlist = config.get("watchlist", {}).get("crypto", [])
        analyst = ResearchAnalyst()
        result = analyst.run_full_analysis(
            current_watchlist=watchlist,
            crypto_watchlist=crypto_watchlist,
        )
        record_refresh("scheduled")
        print(json.dumps(result, indent=2))

    elif mode == "research-analyst-if-stale":
        # Cheap check: read memo, fetch VIX, compare. If stale, run full analyst.
        from agents.researcher import (
            ResearchAnalyst, should_refresh_memo, record_refresh,
        )
        refresh, reason = should_refresh_memo()
        logger.info(f"Memo staleness check: refresh={refresh} reason={reason}")
        if not refresh:
            print(json.dumps({"refreshed": False, "reason": reason}))
        else:
            watchlist        = config.get("watchlist", {}).get("equities", [])
            crypto_watchlist = config.get("watchlist", {}).get("crypto", [])
            analyst = ResearchAnalyst()
            result = analyst.run_full_analysis(
                current_watchlist=watchlist,
                crypto_watchlist=crypto_watchlist,
            )
            record_refresh(reason)
            # Telegram ping so you know an out-of-cycle refresh fired.
            try:
                tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
                tg_chat  = os.getenv("TELEGRAM_CHAT_ID")
                if tg_token and tg_chat:
                    import requests as _req
                    _req.post(
                        f"https://api.telegram.org/bot{tg_token}/sendMessage",
                        json={"chat_id": tg_chat,
                              "text": f"🔄 FlowTrader memo refreshed mid-week\nReason: {reason}"},
                        timeout=10,
                    )
            except Exception as e:
                logger.warning(f"Refresh notification failed: {_scrub_telegram_token(str(e), tg_token)}")
            print(json.dumps({"refreshed": True, "reason": reason, **result}))
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
        # Do NOT print(json.dumps(result)) here — run_bot.bat captures stdout
        # into run_bot.log and bare JSON blocks break log parsers (L-2 audit).
        run_trading_session(config, mode)
