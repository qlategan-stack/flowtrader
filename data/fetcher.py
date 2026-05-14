"""
data/fetcher.py
Pulls price data, technical indicators, and news from Alpaca and Alpha Vantage.
Outputs a clean JSON object that Claude reads in a single context window.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np
import requests
import pytz
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
logger = logging.getLogger(__name__)

# ── Alpaca client setup ────────────────────────────────────────────────────────
try:
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockLatestBarRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed. Run: pip install alpaca-py")


class MarketDataFetcher:
    """
    Fetches all data needed for the Research and Decision agents.
    Returns a structured JSON snapshot Claude can reason over.
    """

    def __init__(self):
        self.alpaca_key = os.getenv("ALPACA_API_KEY")
        self.alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
        self.alpha_vantage_key = os.getenv("ALPHAVANTAGE_API_KEY")
        self.paper = os.getenv("PAPER_TRADING", "true").lower() == "true"

        if ALPACA_AVAILABLE and self.alpaca_key:
            self.stock_client = StockHistoricalDataClient(
                api_key=self.alpaca_key,
                secret_key=self.alpaca_secret
            )
            self.trading_client = TradingClient(
                api_key=self.alpaca_key,
                secret_key=self.alpaca_secret,
                paper=self.paper
            )
        else:
            self.stock_client = None
            self.trading_client = None

    def get_account_snapshot(self) -> dict:
        """Get current account balance and positions."""
        if not self.trading_client:
            return {"error": "Alpaca client not initialized", "portfolio_value": 10000, "buying_power": 10000}

        try:
            account = self.trading_client.get_account()
            positions = self.trading_client.get_all_positions()

            return {
                "portfolio_value": float(account.portfolio_value),
                "buying_power": float(account.buying_power),
                "cash": float(account.cash),
                "open_positions": len(positions),
                "positions": [
                    {
                        "symbol": p.symbol,
                        "qty": float(p.qty),
                        "avg_entry": float(p.avg_entry_price),
                        "current_price": float(p.current_price),
                        "unrealized_pl": float(p.unrealized_pl),
                        "unrealized_plpc": float(p.unrealized_plpc)
                    }
                    for p in positions
                ],
                "day_pl": float(account.equity) - float(account.last_equity)
            }
        except Exception as e:
            logger.error(f"Account fetch error: {e}")
            return {"error": str(e)}

    def get_bars(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        """Fetch OHLCV bars for a symbol."""
        if not self.stock_client:
            return None

        try:
            end = datetime.now(pytz.UTC)
            start = end - timedelta(days=days)

            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="iex",   # IEX feed is available on free Alpaca plans; SIP requires paid
            )
            bars = self.stock_client.get_stock_bars(request)
            df = bars.df

            if isinstance(df.index, pd.MultiIndex):
                df = df.loc[symbol]

            return df.reset_index()
        except Exception as e:
            logger.error(f"Bars fetch error for {symbol}: {e}")
            return None

    def calculate_indicators(self, df: pd.DataFrame, min_score: int = 3) -> dict:
        """
        Calculate RSI, Bollinger Bands, MA deviation, ADX, ATR.
        min_score is passed from the active risk profile so that
        mean_reversion_eligible correctly reflects the current profile.
        """
        if df is None or len(df) < 20:
            return {"error": "Insufficient data"}

        try:
            import ta

            close = df["close"]
            high = df["high"]
            low = df["low"]

            current_rsi = float(ta.momentum.RSIIndicator(close=close, window=14).rsi().iloc[-1])

            bb = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
            bb_upper = float(bb.bollinger_hband().iloc[-1])
            bb_middle = float(bb.bollinger_mavg().iloc[-1])
            bb_lower = float(bb.bollinger_lband().iloc[-1])
            bb_pct = float(bb.bollinger_pband().iloc[-1])

            current_atr = float(ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range().iloc[-1])
            current_adx = float(ta.trend.ADXIndicator(high=high, low=low, close=close, window=14).adx().iloc[-1])

            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma50 = float(close.rolling(50).mean().iloc[-1])
            current_price = float(close.iloc[-1])

            signals = []
            score = 0

            if current_rsi < 35:
                signals.append("RSI<35 (strong oversold)")
                score += 2
            elif current_rsi < 45:
                signals.append("RSI<45 (mild oversold)")
                score += 1

            if current_price < bb_lower:
                signals.append("BelowLowerBB")
                score += 1

            if ma20 > 0 and current_price < ma20 * 0.99:
                signals.append("BelowMA20>1%")
                score += 1

            if current_adx < 25:
                signals.append("ADX<25 (ranging market)")
                score += 1

            trending = current_adx > 30

            return {
                "current_price": current_price,
                "rsi": round(current_rsi, 2),
                "bollinger": {
                    "upper": round(bb_upper, 2),
                    "middle": round(bb_middle, 2),
                    "lower": round(bb_lower, 2),
                    "pct_b": round(bb_pct, 3)
                },
                "ma20_deviation_pct": round((current_price / ma20 - 1) * 100, 2) if ma20 > 0 else 0,
                "atr": round(current_atr, 2),
                "adx": round(current_adx, 2),
                "ma20": round(ma20, 2),
                "ma50": round(ma50, 2),
                "stop_loss_price": round(current_price - (0.5 * current_atr), 2),
                "take_profit_price": round(ma20, 2),
                "signal_score": score,
                "signals_fired": signals,
                "regime": "TRENDING" if trending else "RANGING",
                "mean_reversion_eligible": not trending and score >= min_score
            }

        except Exception as e:
            logger.error(f"Indicator calculation error: {e}")
            return {"error": str(e)}

    def get_news(self, symbol: str, limit: int = 5) -> list:
        """Fetch recent news headlines from Alpaca News API."""
        if not self.alpaca_key:
            return []

        try:
            url = f"https://data.alpaca.markets/v1beta1/news"
            headers = {
                "APCA-API-KEY-ID": self.alpaca_key,
                "APCA-API-SECRET-KEY": self.alpaca_secret
            }
            params = {
                "symbols": symbol,
                "limit": limit,
                "sort": "desc"
            }
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            news_data = resp.json()

            return [
                {
                    "headline": item.get("headline", ""),
                    "summary": item.get("summary", "")[:200],
                    "source": item.get("source", ""),
                    "published": item.get("created_at", "")
                }
                for item in news_data.get("news", [])
            ]
        except Exception as e:
            logger.error(f"News fetch error for {symbol}: {e}")
            return []

    def get_sentiment_score(self, symbol: str) -> dict:
        """Get news sentiment from Alpha Vantage (free tier)."""
        if not self.alpha_vantage_key:
            return {"sentiment": "neutral", "score": 0.0}

        try:
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "NEWS_SENTIMENT",
                "tickers": symbol,
                "apikey": self.alpha_vantage_key,
                "limit": 10
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            if "feed" not in data:
                return {"sentiment": "neutral", "score": 0.0}

            scores = []
            for article in data["feed"][:10]:
                for ticker_data in article.get("ticker_sentiment", []):
                    if ticker_data.get("ticker") == symbol:
                        scores.append(float(ticker_data.get("ticker_sentiment_score", 0)))

            if not scores:
                return {"sentiment": "neutral", "score": 0.0}

            avg_score = sum(scores) / len(scores)
            label = "positive" if avg_score > 0.15 else "negative" if avg_score < -0.15 else "neutral"

            return {
                "sentiment": label,
                "score": round(avg_score, 3),
                "article_count": len(scores)
            }
        except Exception as e:
            logger.error(f"Sentiment error for {symbol}: {e}")
            return {"sentiment": "neutral", "score": 0.0}

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        Fetch the real-time intraday price for a symbol via Alpaca's latest bar.
        Returns None if unavailable (market closed, no data, etc.).
        Used to override the stale daily-close current_price during market hours.
        """
        if not self.stock_client:
            return None
        try:
            req = StockLatestBarRequest(symbol_or_symbols=symbol, feed="iex")
            bars = self.stock_client.get_stock_latest_bar(req)
            bar = bars.get(symbol)
            if bar:
                return float(bar.close)
        except Exception as e:
            logger.debug(f"Latest price fetch failed for {symbol}: {e}")
        return None

    def build_market_snapshot(self, watchlist: list) -> dict:
        """
        Build the full market snapshot JSON that Claude reads.
        Equity symbols (e.g. NVDA) go via Alpaca; crypto symbols
        (anything containing "/", e.g. BTC/USDT) go via BybitFetcher.
        Both venues' results land in the same watchlist with identical
        schema so the decision agent treats them uniformly.

        Indicators (RSI, BB, ADX, MA) are calculated from daily OHLCV bars —
        they update once per day at market close.  current_price is overridden
        with the latest intraday bar so entry prices and signal comparisons
        (BelowMA20, BelowLowerBB) reflect the live market.
        """
        est = pytz.timezone("America/New_York")
        now_est = datetime.now(est)

        equity_symbols = [s for s in watchlist if "/" not in str(s)]
        crypto_symbols = [s for s in watchlist if "/" in str(s)]

        snapshot = {
            "timestamp": now_est.isoformat(),
            "market_time_est": now_est.strftime("%H:%M"),
            "trading_day": now_est.strftime("%Y-%m-%d"),
            "account": self.get_account_snapshot(),
            "crypto_account": self._fetch_crypto_account_snapshot() if crypto_symbols else None,
            "watchlist": []
        }

        from agents.executor import load_risk_profile
        from strategies.engine import StrategyEngine
        _, active_profile = load_risk_profile()
        min_score = active_profile.get("min_signal_score", 3)

        engine = StrategyEngine()
        active_math = engine.active_strategies()
        if active_math:
            logger.info(f"Math strategies active (equities): {active_math}")

        for symbol in equity_symbols:
            logger.info(f"Fetching equity data for {symbol}...")
            bars = self.get_bars(symbol, days=60)
            indicators = self.calculate_indicators(bars, min_score=min_score)
            news = self.get_news(symbol, limit=5)
            sentiment = self.get_sentiment_score(symbol)

            # Override current_price with real-time intraday price so entry
            # prices, stop distances, and price-level signals are always live.
            live_price = self.get_latest_price(symbol)
            if live_price and "error" not in indicators:
                ma20 = indicators.get("ma20", 0)
                bb_lower = indicators.get("bollinger", {}).get("lower", 0)
                atr = indicators.get("atr", 0)
                indicators["current_price"] = live_price
                indicators["price_source"] = "live_intraday"
                if ma20 > 0:
                    indicators["ma20_deviation_pct"] = round((live_price / ma20 - 1) * 100, 2)
                    # Re-evaluate BelowMA20 signal with live price
                    fired = indicators.get("signals_fired", [])
                    score = indicators.get("signal_score", 0)
                    had_below_ma20 = any("BelowMA20" in s for s in fired)
                    now_below_ma20 = live_price < ma20 * 0.99
                    if not had_below_ma20 and now_below_ma20:
                        fired.append("BelowMA20>1%")
                        score += 1
                    elif had_below_ma20 and not now_below_ma20:
                        fired = [s for s in fired if "BelowMA20" not in s]
                        score -= 1
                    # Re-evaluate BelowLowerBB signal
                    had_below_bb = any("BelowLowerBB" in s for s in fired)
                    now_below_bb = bb_lower > 0 and live_price < bb_lower
                    if not had_below_bb and now_below_bb:
                        fired.append("BelowLowerBB")
                        score += 1
                    elif had_below_bb and not now_below_bb:
                        fired = [s for s in fired if "BelowLowerBB" not in s]
                        score -= 1
                    indicators["signals_fired"] = fired
                    indicators["signal_score"] = max(0, score)
                    indicators["mean_reversion_eligible"] = (
                        indicators.get("adx", 30) <= 30
                        and indicators["signal_score"] >= min_score
                    )
                if atr > 0:
                    indicators["stop_loss_price"] = round(live_price - 0.5 * atr, 2)
            else:
                indicators["price_source"] = "daily_close"

            if sentiment.get("score", 0) > 0.15:
                indicators["signal_score"] = indicators.get("signal_score", 0) + 1
                indicators["signals_fired"] = indicators.get("signals_fired", []) + ["PositiveSentiment"]

            # Apply per-symbol mathematical strategies
            if "error" not in indicators and bars is not None:
                indicators = engine.enrich_symbol(symbol, bars, indicators)

            # Directional gate + momentum scoring + regime router
            from strategies.momentum import (
                apply_directional_gate, compute_momentum, select_strategy_mode,
            )
            apply_directional_gate(indicators)
            if "error" not in indicators and bars is not None:
                compute_momentum(bars, indicators)
            select_strategy_mode(indicators, min_score)

            snapshot["watchlist"].append({
                "symbol": symbol,
                "venue": "alpaca",
                "asset_class": "equity",
                "indicators": indicators,
                "news_sentiment": sentiment,
                "recent_headlines": news,
                "setup_quality": self._rate_setup(indicators, min_score),
            })

        if crypto_symbols:
            try:
                from data.crypto_fetcher import BybitFetcher
                crypto_results = BybitFetcher().build_crypto_snapshot(crypto_symbols)
                for item in crypto_results:
                    item["venue"] = "bybit"
                    item["asset_class"] = "crypto"
                    snapshot["watchlist"].append(item)
            except Exception as e:
                logger.error(f"Crypto snapshot failed (continuing with equities): {e}")

        from strategies.momentum import active_score as _active_score
        snapshot["watchlist"].sort(
            key=lambda x: (
                _active_score(x["indicators"]) if x["indicators"].get("strategy_mode", "NONE") != "NONE"
                else x["indicators"].get("signal_score", 0)
            ),
            reverse=True
        )
        return snapshot

    def _fetch_crypto_account_snapshot(self) -> dict:
        """Pull crypto exchange balance. Prefers Binance if API key is set, else Bybit."""
        try:
            if os.getenv("BINANCE_API_KEY"):
                from data.crypto_fetcher import BinanceFetcher
                return BinanceFetcher().get_balance()
            from data.crypto_fetcher import BybitFetcher
            return BybitFetcher().get_balance()
        except Exception as e:
            logger.warning(f"Crypto account snapshot failed: {e}")
            return {"error": str(e)}

    def _rate_setup(self, indicators: dict, min_score: int = 3) -> str:
        """
        Rate the quality of the currently active strategy's setup. Grade is
        relative to min_signal_score from the active risk profile, and is
        applied to whichever score (mean reversion or momentum) drives the
        chosen strategy_mode. Returns SKIP if no strategy is eligible.
        """
        if "error" in indicators:
            return "NO_DATA"

        from strategies.momentum import active_score
        mode = indicators.get("strategy_mode", "NONE")
        if mode == "NONE":
            return "SKIP"

        score = active_score(indicators)
        if score >= min_score + 2:
            return "A_GRADE"
        if score >= min_score + 1:
            return "B_GRADE"
        if score >= min_score:
            return "C_GRADE"
        return "SKIP"
