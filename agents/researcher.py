"""
agents/researcher.py
FlowTrader — Research Analyst Agent

Runs independently on a weekly schedule (Sunday evenings).
Scans broader market conditions beyond the fixed watchlist,
identifies the best opportunities for the coming week, and
writes a structured memo that the Trading Agent reads on Monday.

This is SEPARATE from the intraday scanner in data/fetcher.py.
That scanner ranks your fixed watchlist every 30 minutes.
This analyst scans the broader market and recommends what to
ADD or REMOVE from the watchlist each week.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import requests
import pytz
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
logger = logging.getLogger(__name__)

MEMO_DIR = Path("journal")
MEMO_FILE = MEMO_DIR / "weekly_research_memo.md"
MEMO_JSON = MEMO_DIR / "weekly_research_memo.json"


class ResearchAnalyst:
    """
    Weekly Research Analyst Agent.

    Responsibilities:
    1. Scan macro conditions (VIX, sector performance, market regime)
    2. Identify top mean reversion candidates across a broader universe
    3. Check earnings calendar for the coming week
    4. Produce a structured memo with watchlist recommendations
    5. Flag any risk conditions that should pause trading

    The Trading Agent reads this memo at the start of each Monday session.
    """

    def __init__(self):
        self.client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = "claude-sonnet-4-6"
        self.alpha_vantage_key = os.getenv("ALPHAVANTAGE_API_KEY")
        self.alpaca_key = os.getenv("ALPACA_API_KEY")
        self.alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        MEMO_DIR.mkdir(exist_ok=True)

    # ── DATA COLLECTION ───────────────────────────────────────────────────────

    def get_vix_level(self) -> dict:
        """Fetch VIX (fear index) — key macro indicator."""
        try:
            import yfinance as yf
            vix = round(yf.Ticker("^VIX").fast_info["lastPrice"], 2)

            if vix < 15:
                interpretation = "LOW — market complacent, good for mean reversion"
                risk_level = "LOW"
            elif vix < 20:
                interpretation = "NORMAL — healthy conditions"
                risk_level = "NORMAL"
            elif vix < 30:
                interpretation = "ELEVATED — increased volatility, tighten stops"
                risk_level = "ELEVATED"
            else:
                interpretation = "HIGH — fear in market, reduce position sizes or pause"
                risk_level = "HIGH"

            return {
                "vix": round(vix, 2),
                "interpretation": interpretation,
                "risk_level": risk_level
            }
        except Exception as e:
            logger.error(f"VIX fetch error: {e}")
            return {"vix": "error", "interpretation": str(e)}

    def get_sector_performance(self) -> list:
        """
        Get weekly performance of major sector ETFs.
        Identifies which sectors are leading and lagging.
        """
        sector_etfs = {
            "XLK": "Technology",
            "XLF": "Financials",
            "XLV": "Healthcare",
            "XLE": "Energy",
            "XLI": "Industrials",
            "XLY": "Consumer Discretionary",
            "XLP": "Consumer Staples",
            "XLU": "Utilities",
            "XLB": "Materials",
            "XLRE": "Real Estate",
            "XLC": "Communication Services"
        }

        results = []
        for ticker, sector in sector_etfs.items():
            try:
                perf = self._get_weekly_change(ticker)
                results.append({
                    "etf": ticker,
                    "sector": sector,
                    "weekly_change_pct": perf
                })
            except Exception as e:
                logger.warning(f"Sector data error for {ticker}: {e}")
                results.append({
                    "etf": ticker,
                    "sector": sector,
                    "weekly_change_pct": None
                })

        # Sort by performance — best performers first
        results.sort(
            key=lambda x: x["weekly_change_pct"] if x["weekly_change_pct"] is not None else -999,
            reverse=True
        )
        return results

    def _get_weekly_change(self, symbol: str) -> Optional[float]:
        """Calculate weekly percentage change for a symbol."""
        if not self.alpha_vantage_key:
            return None

        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "compact",
                "apikey": self.alpha_vantage_key
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            ts = data.get("Time Series (Daily)", {})

            if not ts:
                return None

            dates = sorted(ts.keys(), reverse=True)
            if len(dates) < 6:
                return None

            current_close = float(ts[dates[0]]["4. close"])
            week_ago_close = float(ts[dates[5]]["4. close"])

            return round((current_close - week_ago_close) / week_ago_close * 100, 2)
        except Exception:
            return None

    def get_broader_universe_scan(self) -> list:
        """
        Scan a broader universe of stocks beyond the fixed watchlist.
        Looks for mean reversion setups using RSI and price vs moving average.
        Returns top candidates ranked by signal strength.
        """
        # Broader scan universe — liquid, well-known stocks
        scan_universe = [
            # Mega cap tech
            "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD",
            "ORCL", "CRM", "ADBE", "QCOM",
            # Financials
            "JPM", "BAC", "GS", "MS", "V", "MA", "BRK-B", "AXP",
            # Healthcare
            "JNJ", "PFE", "UNH", "ABBV", "MRK", "LLY", "BMY",
            # Consumer
            "WMT", "COST", "MCD", "NKE", "SBUX", "AMZN", "TGT",
            # Energy
            "XOM", "CVX", "COP", "SLB", "HAL",
            # Materials & industrials
            "FCX", "NEM", "AA", "CLF", "CAT", "DE",
            # Broad market & sector ETFs
            "SPY", "QQQ", "IWM", "DIA", "VTI",
            "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLB",
            # Commodities ETFs
            "GLD", "SLV", "USO", "UNG", "DBC", "PDBC",
            "COPX", "PALL", "WEAT",
            # Bonds & rates
            "TLT", "IEF", "SHY", "HYG", "LQD",
            # Crypto proxies (equity)
            "COIN", "MSTR", "RIOT", "MARA", "CLSK", "HUT",
            # Volatility & alternatives
            "UVXY", "VXX",
        ]

        candidates = []
        for symbol in scan_universe:
            try:
                signal = self._quick_signal_check(symbol)
                if signal and signal.get("score", 0) >= 2:
                    candidates.append(signal)
            except Exception as e:
                logger.warning(f"Scan error for {symbol}: {e}")

        # Sort by signal score
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        return candidates[:10]  # Return top 10

    def _quick_signal_check(self, symbol: str) -> Optional[dict]:
        """
        Quick mean reversion signal check for a symbol.
        Uses Alpha Vantage RSI and daily price data.
        """
        if not self.alpha_vantage_key:
            return None

        try:
            # Get RSI
            rsi_url = "https://www.alphavantage.co/query"
            rsi_params = {
                "function": "RSI",
                "symbol": symbol,
                "interval": "daily",
                "time_period": 14,
                "series_type": "close",
                "apikey": self.alpha_vantage_key
            }
            rsi_resp = requests.get(rsi_url, params=rsi_params, timeout=10)
            rsi_data = rsi_resp.json()
            rsi_series = rsi_data.get("Technical Analysis: RSI", {})

            if not rsi_series:
                return None

            latest_date = sorted(rsi_series.keys(), reverse=True)[0]
            current_rsi = float(rsi_series[latest_date]["RSI"])

            # Score the signal
            score = 0
            signals = []

            if current_rsi < 30:
                score += 2
                signals.append(f"RSI={current_rsi:.1f} (strongly oversold)")
            elif current_rsi < 40:
                score += 1
                signals.append(f"RSI={current_rsi:.1f} (mildly oversold)")

            if score == 0:
                return None  # Not interesting

            return {
                "symbol": symbol,
                "rsi": round(current_rsi, 2),
                "score": score,
                "signals": signals,
                "note": "Broader universe scan candidate"
            }

        except Exception as e:
            logger.warning(f"Signal check error for {symbol}: {e}")
            return None

    def get_earnings_calendar(self) -> list:
        """
        Get upcoming earnings announcements for the coming week.
        Stocks with upcoming earnings should be AVOIDED for mean reversion
        (earnings cause volatility that breaks the strategy).
        """
        if not self.alpha_vantage_key:
            return []

        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "EARNINGS_CALENDAR",
                "horizon": "3month",
                "apikey": self.alpha_vantage_key
            }
            resp = requests.get(url, params=params, timeout=15)

            # Alpha Vantage returns CSV for this endpoint
            lines = resp.text.strip().split("\n")
            if len(lines) < 2:
                return []

            headers = lines[0].split(",")
            earnings = []

            # Get earnings for the next 7 days only
            est = pytz.timezone("America/New_York")
            today = datetime.now(est).date()
            next_week = today + timedelta(days=7)

            for line in lines[1:20]:  # Check first 20 entries
                try:
                    parts = line.split(",")
                    if len(parts) < 3:
                        continue

                    symbol = parts[0].strip()
                    report_date_str = parts[2].strip()

                    if not report_date_str:
                        continue

                    report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()

                    if today <= report_date <= next_week:
                        earnings.append({
                            "symbol": symbol,
                            "report_date": report_date_str,
                            "warning": f"AVOID {symbol} — earnings on {report_date_str}"
                        })
                except Exception:
                    continue

            return earnings[:15]  # Return up to 15 upcoming earnings

        except Exception as e:
            logger.error(f"Earnings calendar error: {e}")
            return []

    def get_market_news_summary(self) -> list:
        """Get top market news headlines from Alpaca News API."""
        if not self.alpaca_key:
            return []

        try:
            url = "https://data.alpaca.markets/v1beta1/news"
            headers = {
                "APCA-API-KEY-ID": self.alpaca_key,
                "APCA-API-SECRET-KEY": self.alpaca_secret
            }
            params = {
                "symbols": "SPY,QQQ,BTC/USD",
                "limit": 10,
                "sort": "desc"
            }
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            news_data = resp.json()

            return [
                {
                    "headline": item.get("headline", ""),
                    "source": item.get("source", ""),
                    "published": item.get("created_at", "")
                }
                for item in news_data.get("news", [])[:10]
            ]
        except Exception as e:
            logger.error(f"News fetch error: {e}")
            return []

    # ── CRYPTO RESEARCH ───────────────────────────────────────────────────────

    def get_crypto_macro(self) -> dict:
        """
        Fetch the crypto-market equivalents of VIX + sector rotation:
          - Fear & Greed Index (alternative.me, free, no key)
          - BTC dominance + total market cap (CoinGecko, free, no key)
        These give Claude a read on crypto sentiment and regime
        (BTC-led vs alt-season vs risk-off).
        """
        out: dict = {}

        # Fear & Greed (0–100, plus a label)
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=2", timeout=10)
            data = r.json().get("data", [])
            if data:
                today = data[0]
                yesterday = data[1] if len(data) > 1 else {}
                value = int(today.get("value", 0))
                label = today.get("value_classification", "")
                prev  = int(yesterday.get("value", value)) if yesterday else value
                out["fear_greed"] = {
                    "value": value,
                    "label": label,
                    "change_1d": value - prev,
                }
        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")
            out["fear_greed"] = {"error": str(e)}

        # BTC dominance + total market cap (CoinGecko global endpoint)
        try:
            r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
            data = r.json().get("data", {})
            btc_dom    = round(float(data.get("market_cap_percentage", {}).get("btc", 0)), 2)
            eth_dom    = round(float(data.get("market_cap_percentage", {}).get("eth", 0)), 2)
            total_mcap = float(data.get("total_market_cap", {}).get("usd", 0))
            mcap_24h   = round(float(data.get("market_cap_change_percentage_24h_usd", 0)), 2)
            out["dominance"] = {
                "btc_pct":          btc_dom,
                "eth_pct":          eth_dom,
                "total_mcap_usd":   round(total_mcap, 0),
                "mcap_change_24h":  mcap_24h,
            }
        except Exception as e:
            logger.warning(f"CoinGecko global fetch failed: {e}")
            out["dominance"] = {"error": str(e)}

        # Interpretation hints (Claude will produce the final read)
        try:
            fg_val = out.get("fear_greed", {}).get("value")
            if isinstance(fg_val, int):
                if   fg_val <= 24: out["sentiment_hint"] = "EXTREME_FEAR — historically a contrarian buy zone"
                elif fg_val <= 44: out["sentiment_hint"] = "FEAR — cautious but mean-reversion friendly"
                elif fg_val <= 55: out["sentiment_hint"] = "NEUTRAL — no strong sentiment edge"
                elif fg_val <= 74: out["sentiment_hint"] = "GREED — overheated risk; tighten or skip extensions"
                else:              out["sentiment_hint"] = "EXTREME_GREED — euphoria; mean-reversion shorts often emerge"

            btc_d = out.get("dominance", {}).get("btc_pct")
            if isinstance(btc_d, float):
                if   btc_d >= 55: out["regime_hint"] = "BTC-led — alts underperforming, BTC absorbing flows"
                elif btc_d >= 48: out["regime_hint"] = "Balanced — neither alts nor BTC dominating decisively"
                else:              out["regime_hint"] = "Alt-favourable — BTC dominance falling, capital rotating into alts"
        except Exception:
            pass

        return out

    def get_crypto_setups(self, watchlist: list) -> list:
        """
        Run the existing BybitFetcher snapshot over the configured crypto pairs
        so Claude has the same RSI/BB/ADX/score readings the live bot will use.
        """
        if not watchlist:
            return []
        try:
            from data.crypto_fetcher import BybitFetcher
            results = BybitFetcher().build_crypto_snapshot(watchlist)
            # Strip down to the fields Claude actually needs
            slim = []
            for item in results:
                ind = item.get("indicators", {}) or {}
                tk  = item.get("ticker", {}) or {}
                slim.append({
                    "symbol":          item.get("symbol"),
                    "setup_quality":   item.get("setup_quality"),
                    "signal_score":    ind.get("signal_score"),
                    "signals_fired":   ind.get("signals_fired"),
                    "regime":          ind.get("regime"),
                    "rsi":             ind.get("rsi"),
                    "adx":             ind.get("adx"),
                    "bb_pct":          (ind.get("bollinger") or {}).get("pct_b"),
                    "ma20_dev_pct":    ind.get("ma20_deviation_pct"),
                    "current_price":   ind.get("current_price"),
                    "change_24h_pct":  tk.get("change_pct_24h"),
                    "volume_24h_usdt": tk.get("volume_24h_usdt"),
                })
            return slim
        except Exception as e:
            logger.error(f"Crypto setup scan failed: {e}")
            return []

    # ── CLAUDE ANALYSIS ───────────────────────────────────────────────────────

    def generate_weekly_memo(
        self,
        vix: dict,
        sectors: list,
        candidates: list,
        earnings: list,
        news: list,
        current_watchlist: list,
        crypto_macro: Optional[dict] = None,
        crypto_setups: Optional[list] = None,
        crypto_watchlist: Optional[list] = None,
    ) -> dict:
        """
        Send all research data to Claude and get a structured weekly memo back.
        This is the core output of the Research Analyst agent.
        """

        # Build the research prompt
        earnings_symbols = [e["symbol"] for e in earnings]
        crypto_macro     = crypto_macro     or {}
        crypto_setups    = crypto_setups    or []
        crypto_watchlist = crypto_watchlist or []

        prompt = f"""
You are FlowTrader's Research Analyst. It is the start of the trading window.
Your job is to prepare the trading brief covering BOTH US equities AND
crypto for the days ahead.  The bot trades both: equity orders go via
Alpaca, crypto spot orders go via Bybit.

Analyse all the data below and produce a structured memo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EQUITY MACRO CONDITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VIX (Fear Index): {json.dumps(vix, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTOR PERFORMANCE (past week)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(sectors, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BROADER EQUITY UNIVERSE SCAN (mean reversion candidates)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(candidates, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EARNINGS THIS WEEK (AVOID these equity symbols)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(earnings, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT EQUITY WATCHLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(current_watchlist, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRYPTO MACRO CONDITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fear & Greed Index, BTC dominance, total market cap, sentiment / regime hints:
{json.dumps(crypto_macro, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRYPTO SETUPS (current per-pair indicators from the live scanner)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(crypto_setups, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT CRYPTO WATCHLIST
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(crypto_watchlist, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RECENT MARKET NEWS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(news, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR OUTPUT MUST INCLUDE ALL OF THESE SECTIONS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. MARKET REGIME ASSESSMENT (equities)
   - Is the overall market trending or ranging?
   - Should mean reversion strategies be active or paused for equities?
   - VIX interpretation and what it means for position sizing

2. TOP OPPORTUNITIES FOR THE WEEK (equities + crypto, mixed)
   - Up to 3 equity setups and up to 3 crypto setups — flag asset_class
   - Why each one qualifies (signal strength, regime fit, catalysts)
   - Suggested position size adjustment if any

3. WATCHLIST CHANGES RECOMMENDED
   - Equity symbols to ADD / REMOVE / AVOID (earnings: {earnings_symbols})
   - Crypto pairs to ADD / REMOVE / AVOID — base your additions on
     liquidity and mean-reversion fit; don't suggest adding stablecoins,
     wrapped tokens, or pairs that aren't on Binance/Bybit

4. SECTOR FOCUS (equities only)
   - Which 2-3 sectors show the best mean reversion conditions?
   - Which sectors to avoid or underweight?

5. CRYPTO OUTLOOK
   - regime: a short label such as "BTC-led", "Alt-favourable",
     "Risk-off", "Range-bound", "Trending up", "Trending down"
   - mean_reversion_active_crypto: true/false — should the crypto
     mean-reversion strategy be engaged this week?
   - sentiment_read: 1-2 sentences on Fear & Greed + dominance trend
   - dominance_read: 1-2 sentences on what BTC dominance is doing
   - top_crypto_opportunities: 1-3 specific pairs that look strongest,
     each with rationale + direction (LONG only — bot is spot)
   - crypto_risk_warnings: list of {{event, severity, detail}} —
     anything regulatory, macro, exchange-related, or technical that
     could disrupt the crypto book this week

6. RISK WARNINGS (cross-asset)
   - Any macro events this week that could disrupt trading?
   - Specific risk flags the Trading Agent should be aware of
   - Recommended max position size given current VIX level

7. TRADING CONFIDENCE SCORE FOR THE WEEK
   - Score from 1-10 (10 = ideal conditions, 1 = pause trading)
   - One sentence justification — should account for BOTH equity
     and crypto conditions weighted by where the bot has more capital

Return this as a JSON object with these exact keys:
market_regime, top_opportunities, watchlist_changes,
sector_focus, crypto_outlook, risk_warnings, confidence_score,
confidence_reason, generated_at, valid_until

The crypto_outlook value MUST be a JSON object with these keys:
regime, mean_reversion_active_crypto, sentiment_read, dominance_read,
top_crypto_opportunities, crypto_risk_warnings
"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=6000,
                system=(
                    "You are FlowTrader's Research Analyst. You produce structured, "
                    "actionable weekly trading briefs. Be specific and data-driven. "
                    "Your output drives a live trading bot — be accurate and conservative. "
                    "Always return valid JSON."
                ),
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text
            memo_data = self._parse_memo(raw)
            memo_data["raw_analysis"] = raw
            return memo_data

        except Exception as e:
            logger.error(f"Memo generation error: {e}")
            return {
                "error": str(e),
                "market_regime": "UNKNOWN",
                "confidence_score": 0,
                "confidence_reason": "Analysis failed — do not trade until resolved"
            }

    def _parse_memo(self, raw_text: str) -> dict:
        """Extract JSON from Claude's memo response."""
        import re

        patterns = [
            r'```json\s*(.*?)\s*```',
            r'```\s*(.*?)\s*```',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, raw_text, re.DOTALL)
            if matches:
                try:
                    return json.loads(matches[0])
                except json.JSONDecodeError:
                    continue

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return {
                "raw_analysis": raw_text,
                "parse_error": True,
                "market_regime": "PARSE_ERROR",
                "confidence_score": 5,
                "confidence_reason": "Memo generated but could not parse JSON — review raw_analysis"
            }

    # ── MEMO OUTPUT ───────────────────────────────────────────────────────────

    def save_memo(self, memo: dict) -> tuple[str, str]:
        """
        Save the weekly memo in both JSON (for the Trading Agent to read)
        and Markdown (for human review).
        """
        est = pytz.timezone("America/New_York")
        now = datetime.now(est)

        # Save JSON for Trading Agent to read
        memo["generated_at"] = now.isoformat()
        memo["valid_until"] = (now + timedelta(days=7)).isoformat()

        with open(MEMO_JSON, "w") as f:
            json.dump(memo, f, indent=2)

        # Save Markdown for human review
        week_str = now.strftime("Week of %d %B %Y")
        confidence = memo.get("confidence_score", "N/A")
        regime = memo.get("market_regime", "Unknown")

        md_content = f"""# FlowTrader Weekly Research Memo
## {week_str}

**Generated:** {now.strftime("%A, %d %B %Y at %H:%M")} EST
**Valid Until:** Next Sunday

---

## Market Regime
{regime}

## Trading Confidence Score: {confidence}/10
{memo.get("confidence_reason", "")}

---

## Top Opportunities for This Week
{json.dumps(memo.get("top_opportunities", []), indent=2)}

---

## Watchlist Changes Recommended
{json.dumps(memo.get("watchlist_changes", {}), indent=2)}

---

## Sector Focus
{json.dumps(memo.get("sector_focus", {}), indent=2)}

---

## Risk Warnings
{json.dumps(memo.get("risk_warnings", []), indent=2)}

---

## Full Analysis
{memo.get("raw_analysis", "See JSON file for details")}

---
*Generated automatically by FlowTrader Research Analyst*
*Review before market open on Monday*
"""

        with open(MEMO_FILE, "w") as f:
            f.write(md_content)

        logger.info(f"Memo saved: {MEMO_JSON} and {MEMO_FILE}")
        return str(MEMO_JSON), str(MEMO_FILE)

    def load_current_memo(self) -> Optional[dict]:
        """
        Load the most recent weekly memo.
        Called by the Trading Agent every Monday morning.
        Returns None if memo is older than 7 days.
        """
        if not MEMO_JSON.exists():
            return None

        try:
            with open(MEMO_JSON, "r") as f:
                memo = json.load(f)

            # Check if memo is still valid
            valid_until_str = memo.get("valid_until")
            if valid_until_str:
                valid_until = datetime.fromisoformat(valid_until_str)
                est = pytz.timezone("America/New_York")
                now = datetime.now(est).replace(tzinfo=None)
                if now > valid_until.replace(tzinfo=None):
                    logger.warning("Weekly memo has expired — running fresh analysis recommended")
                    memo["expired"] = True

            return memo
        except Exception as e:
            logger.error(f"Memo load error: {e}")
            return None

    # ── TELEGRAM NOTIFICATION ─────────────────────────────────────────────────

    def send_telegram_memo(self, memo: dict):
        """Send the weekly research brief summary to Telegram."""
        if not self.telegram_token or not self.telegram_chat_id:
            return

        confidence = memo.get("confidence_score", "N/A")
        raw_regime = memo.get("market_regime", "Unknown")
        if isinstance(raw_regime, dict):
            regime = raw_regime.get("trend_or_range", raw_regime.get("regime", str(raw_regime)))[:80]
        else:
            regime = str(raw_regime)[:80]
        reason = memo.get("confidence_reason", "")

        opportunities = memo.get("top_opportunities", [])
        if isinstance(opportunities, dict):
            opportunities = list(opportunities.values())
        opp_lines = ""
        for opp in opportunities[:3]:
            if isinstance(opp, dict):
                label = opp.get("rationale", opp.get("reason", opp.get("setup_type", "")))[:80]
                opp_lines += f"\n  • {opp.get('symbol', '?')} — {label}"
            else:
                opp_lines += f"\n  • {str(opp)[:80]}"

        changes = memo.get("watchlist_changes", {})
        if isinstance(changes, dict):
            raw_adds = changes.get("add", changes.get("symbols_to_add", []))
            raw_removes = changes.get("remove", changes.get("symbols_to_remove_or_reduce", []))
            adds    = [a.get("symbol", str(a)) if isinstance(a, dict) else str(a) for a in raw_adds]
            removes = [r.get("symbol", str(r)) if isinstance(r, dict) else str(r) for r in raw_removes]
        else:
            adds, removes = [], []

        warnings = memo.get("risk_warnings", [])
        if isinstance(warnings, dict):
            first_val = next(iter(warnings.values()), None)
            warn_line = (first_val[0] if isinstance(first_val, list) and first_val else first_val) or "None"
        elif isinstance(warnings, list) and warnings:
            warn_line = warnings[0]
        else:
            warn_line = "None"
        if isinstance(warn_line, dict):
            warn_line = warn_line.get("warning", warn_line.get("detail", str(warn_line)))

        message = (
            f"*FlowTrader — Weekly Research Brief*\n"
            f"Confidence: `{confidence}/10` | Regime: `{regime}`\n"
            f"{reason}\n\n"
            f"*Top Opportunities:*{opp_lines or ' None found'}\n\n"
            f"*Watchlist Changes:*\n"
            f"  Add: {', '.join(adds) if adds else 'none'}\n"
            f"  Remove: {', '.join(removes) if removes else 'none'}\n\n"
            f"*Risk Warning:* {str(warn_line)[:120]}\n\n"
            f"_Full memo saved to journal/weekly\\_research\\_memo.md_"
        )

        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": self.telegram_chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=10
            )
            logger.info("Telegram research memo sent.")
        except Exception as e:
            logger.warning(f"Telegram notification failed: {e}")

    # ── MAIN ENTRY POINT ─────────────────────────────────────────────────────

    def run_full_analysis(
        self,
        current_watchlist: list = None,
        crypto_watchlist: list = None,
    ) -> dict:
        """
        Run the complete weekly research analysis.
        This is called by the GitHub Actions Sunday/Wednesday schedule.
        """
        if current_watchlist is None:
            current_watchlist = ["NVDA", "AAPL", "MSFT", "QQQ", "SPY", "AMD", "GLD"]
        if crypto_watchlist is None:
            crypto_watchlist = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

        logger.info("=" * 60)
        logger.info("FlowTrader Research Analyst — Analysis Starting")
        logger.info("=" * 60)

        # Step 1: Equity macro
        logger.info("Fetching VIX...")
        vix = self.get_vix_level()
        logger.info(f"VIX: {vix.get('vix')} — {vix.get('risk_level')}")

        # Step 2: Sector rotation
        logger.info("Fetching sector performance...")
        sectors = self.get_sector_performance()
        logger.info(f"Sector data: {len(sectors)} sectors retrieved")

        # Step 3: Broader universe scan
        logger.info("Scanning broader equity universe for mean reversion setups...")
        candidates = self.get_broader_universe_scan()
        logger.info(f"Found {len(candidates)} candidates in broader scan")

        # Step 4: Earnings calendar
        logger.info("Fetching earnings calendar...")
        earnings = self.get_earnings_calendar()
        logger.info(f"Found {len(earnings)} earnings events this week")

        # Step 5: Crypto macro (Fear & Greed, BTC dominance, total mcap)
        logger.info("Fetching crypto macro (Fear & Greed, BTC dominance)...")
        crypto_macro = self.get_crypto_macro()
        fg = crypto_macro.get("fear_greed", {}).get("value", "—")
        bd = crypto_macro.get("dominance", {}).get("btc_pct", "—")
        logger.info(f"Crypto macro — Fear/Greed: {fg} | BTC dom: {bd}%")

        # Step 6: Crypto per-pair setup snapshot
        logger.info(f"Scanning {len(crypto_watchlist)} crypto pairs for setups...")
        crypto_setups = self.get_crypto_setups(crypto_watchlist)
        logger.info(f"Crypto setup snapshot: {len(crypto_setups)} pairs")

        # Step 7: Market news
        logger.info("Fetching market news...")
        news = self.get_market_news_summary()

        # Step 8: Claude analysis
        logger.info("Sending to Claude for memo generation...")
        memo = self.generate_weekly_memo(
            vix=vix,
            sectors=sectors,
            candidates=candidates,
            earnings=earnings,
            news=news,
            current_watchlist=current_watchlist,
            crypto_macro=crypto_macro,
            crypto_setups=crypto_setups,
            crypto_watchlist=crypto_watchlist,
        )

        # Step 7: Save outputs
        json_path, md_path = self.save_memo(memo)

        confidence = memo.get("confidence_score", "N/A")
        regime = memo.get("market_regime", "Unknown")
        logger.info(f"Weekly memo complete — Confidence: {confidence}/10 | Regime: {regime}")
        logger.info(f"Saved to: {json_path}")

        # Step 8: Notify via Telegram
        self.send_telegram_memo(memo)

        return {
            "status": "COMPLETE",
            "confidence_score": confidence,
            "market_regime": regime,
            "candidates_found": len(candidates),
            "earnings_warnings": len(earnings),
            "memo_path": json_path
        }


# ── CLI entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    )

    # Load watchlists from config
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        watchlist        = config.get("watchlist", {}).get("equities", [])
        crypto_watchlist = config.get("watchlist", {}).get("crypto", [])
    except FileNotFoundError:
        watchlist        = ["NVDA", "AAPL", "MSFT", "QQQ", "SPY", "AMD", "GLD"]
        crypto_watchlist = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    analyst = ResearchAnalyst()
    result  = analyst.run_full_analysis(
        current_watchlist=watchlist,
        crypto_watchlist=crypto_watchlist,
    )

    print("\n" + "=" * 60)
    print("RESEARCH ANALYST COMPLETE")
    print("=" * 60)
    print(json.dumps(result, indent=2))
    print(f"\nMemo saved. Review journal/weekly_research_memo.md before Monday.")
