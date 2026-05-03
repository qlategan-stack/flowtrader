"""
data/fetcher.py
Pulls price data, technical indicators, and news from Alpaca and Alpha Vantage.
Outputs a clean JSON object that Claude reads in a single context window.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import numpy as np
import requests
import pytz
from dotenv import load_dotenv

load_dotenv()
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
                end=end
            )
            bars = self.stock_client.get_stock_bars(request)
            df = bars.df

            if isinstance(df.index, pd.MultiIndex):
                df = df.loc[symbol]

            return df.reset_index()
        except Exception as e:
            logger.error(f"Bars fetch error for {symbol}: {e}")
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> dict:
        """
        Calculate RSI, Bollinger Bands, VWAP, moving averages, and ADX.
        Uses the 'ta' library for accuracy.
        """
        if df is None or len(df) < 20:
            return {"error": "Insufficient data"}

        try:
            import ta

            close = df["close"]
            high = df["high"]
            low = df["low"]
            volume = df["volume"]

            # RSI
            rsi = ta.momentum.RSIIndicator(close=close, window=14).rsi()
            current_rsi = float(rsi.iloc[-1])

            # Bollinger Bands
            bb = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
            bb_upper = float(bb.bollinger_hband().iloc[-1])
            bb_middle = float(bb.bollinger_mavg().iloc[-1])
            bb_lower = float(bb.bollinger_lband().iloc[-1])
            bb_pct = float(bb.bollinger_pband().iloc[-1])  # % position in band

            # ATR for stop sizing
            atr = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14)
            current_atr = float(atr.average_true_range().iloc[-1])

            # ADX for regime detection
            adx = ta.trend.ADXIndicator(high=high, low=low, close=close, window=14)
            current_adx = float(adx.adx().iloc[-1])

            # Moving averages
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma50 = float(close.rolling(50).mean().iloc[-1])

            # VWAP (approximated from daily data)
            typical_price = (high + low + close) / 3
            vwap = float((typical_price * volume).rolling(20).sum() / volume.rolling(20).sum()).iloc[-1]

            current_price = float(close.iloc[-1])

            # Signal scoring
            signals = []
            score = 0

            if current_rsi < 32:
                signals.append("RSI<32 (strong oversold)")
                score += 2
            elif current_rsi < 40:
                signals.append("RSI<40 (mild oversold)")
                score += 1

            if current_price < bb_lower:
                signals.append("BelowLowerBB")
                score += 1

            if vwap > 0 and current_price < vwap * 0.99:
                signals.append("BelowVWAP>1%")
                score += 1

            if current_adx < 20:
                signals.append("ADX<20 (ranging market)")
                score += 1

            # Regime flag
            trending = current_adx > 25

            return {
                "current_price": current_price,
                "rsi": round(current_rsi, 2),
                "bollinger": {
                    "upper": round(bb_upper, 2),
                    "middle": round(bb_middle, 2),
                    "lower": round(bb_lower, 2),
                    "pct_b": round(bb_pct, 3)
                },
                "vwap": round(vwap, 2),
                "atr": round(current_atr, 2),
                "adx": round(current_adx, 2),
                "ma20": round(ma20, 2),
                "ma50": round(ma50, 2),
                "stop_loss_price": round(current_price - (0.5 * current_atr), 2),
                "take_profit_price": round(ma20, 2),
                "signal_score": score,
                "signals_fired": signals,
                "regime": "TRENDING" if trending else "RANGING",
                "mean_reversion_eligible": not trending and score >= 3
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

    def build_market_snapshot(self, watchlist: list) -> dict:
        """
        Build the full market snapshot JSON that Claude reads.
        This is the main output of the data layer.
        """
        est = pytz.timezone("America/New_York")
        now_est = datetime.now(est)

        snapshot = {
            "timestamp": now_est.isoformat(),
            "market_time_est": now_est.strftime("%H:%M"),
            "trading_day": now_est.strftime("%Y-%m-%d"),
            "account": self.get_account_snapshot(),
            "watchlist": []
        }

        for symbol in watchlist:
            logger.info(f"Fetching data for {symbol}...")

            bars = self.get_bars(symbol, days=60)
            indicators = self.calculate_indicators(bars)
            news = self.get_news(symbol, limit=5)
            sentiment = self.get_sentiment_score(symbol)

            # Add sentiment signal if positive
            if sentiment.get("score", 0) > 0.15:
                indicators["signal_score"] = indicators.get("signal_score", 0) + 1
                indicators["signals_fired"] = indicators.get("signals_fired", []) + ["PositiveSentiment"]

            symbol_data = {
                "symbol": symbol,
                "indicators": indicators,
                "news_sentiment": sentiment,
                "recent_headlines": news,
                "setup_quality": self._rate_setup(indicators)
            }

            snapshot["watchlist"].append(symbol_data)

        # Sort by setup quality (best setups first)
        snapshot["watchlist"].sort(
            key=lambda x: x["indicators"].get("signal_score", 0),
            reverse=True
        )

        return snapshot

    def _rate_setup(self, indicators: dict) -> str:
        """Rate the quality of a mean reversion setup."""
        if "error" in indicators:
            return "NO_DATA"

        score = indicators.get("signal_score", 0)
        eligible = indicators.get("mean_reversion_eligible", False)

        if not eligible:
            return "SKIP"
        elif score >= 5:
            return "A_GRADE"
        elif score >= 4:
            return "B_GRADE"
        elif score >= 3:
            return "C_GRADE"
        else:
            return "SKIP"
