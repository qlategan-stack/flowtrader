"""
data/crypto_fetcher.py
CCXT + Bybit integration for crypto market data and order execution.
Produces the same indicator/snapshot structure as MarketDataFetcher
so Claude's decision agent works identically for equities and crypto.
"""

import os
import logging
from typing import Optional
import pandas as pd
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    logger.warning("ccxt not installed — run: pip install ccxt")

try:
    import ta
    TA_AVAILABLE = True
except ImportError:
    TA_AVAILABLE = False
    logger.warning("ta not installed — run: pip install ta")


class BybitFetcher:
    """
    Fetches crypto OHLCV data from Bybit via CCXT and calculates the same
    technical indicators as MarketDataFetcher so the decision agent needs
    no changes to handle crypto vs equities.

    Public data (OHLCV, tickers): no API key required.
    Private data (balance, orders): BYBIT_API_KEY + BYBIT_SECRET_KEY required.
    """

    def __init__(self):
        self.api_key    = os.getenv("BYBIT_API_KEY", "")
        self.api_secret = os.getenv("BYBIT_SECRET_KEY", "")
        self.testnet    = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
        self.exchange        = None   # live — public market data
        self.exchange_priv   = None   # testnet (if enabled) — orders & balance
        self._connected      = False
        self._has_private    = bool(self.api_key and self.api_secret)

        if CCXT_AVAILABLE:
            self._connect()

    def _connect(self):
        # Public exchange — live Bybit, no auth, full symbol set + real prices
        try:
            self.exchange = ccxt.bybit({
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                },
                "enableRateLimit": True,
            })
            self.exchange.load_markets()
            self._connected = True
            logger.info("Bybit public exchange connected (live market data)")
        except Exception as e:
            self._connected = False
            logger.error(f"Bybit public connection failed: {e}")
            return  # no point setting up private exchange if public failed

        # Private exchange — testnet or live, for orders & balance only
        try:
            self.exchange_priv = ccxt.bybit({
                "apiKey":  self.api_key  or None,
                "secret":  self.api_secret or None,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                },
                "enableRateLimit": True,
            })
            if self.testnet:
                self.exchange_priv.set_sandbox_mode(True)
            if self._has_private:
                self.exchange_priv.load_markets()

            mode = "TESTNET orders" if self.testnet else "LIVE orders"
            key_status = "API key loaded" if self._has_private else "no key — balance/orders unavailable"
            logger.info(f"Bybit private exchange ready ({mode}) — {key_status}")
        except Exception as e:
            self.exchange_priv = None
            self._has_private = False
            logger.warning(f"Bybit private exchange failed (market data unaffected): {e}")

    # ── DATA FETCHING ─────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, days: int = 60) -> Optional[pd.DataFrame]:
        """Fetch daily OHLCV candles for a crypto pair. No API key needed."""
        if not self._connected:
            return None
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=min(days + 5, 200))
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"OHLCV fetch error {symbol}: {e}")
            return None

    def get_ticker(self, symbol: str) -> dict:
        """Fetch live ticker — price, 24h change, volume. No API key needed."""
        if not self._connected:
            return {}
        try:
            t = self.exchange.fetch_ticker(symbol)
            return {
                "price":         t.get("last", 0),
                "change_pct_24h": round(t.get("percentage", 0) or 0, 2),
                "volume_24h_usdt": round(t.get("quoteVolume", 0) or 0, 2),
                "high_24h":       t.get("high", 0),
                "low_24h":        t.get("low", 0),
                "bid":            t.get("bid", 0),
                "ask":            t.get("ask", 0),
            }
        except Exception as e:
            logger.error(f"Ticker error {symbol}: {e}")
            return {}

    # ── INDICATORS ────────────────────────────────────────────────────────────

    def calculate_indicators(self, df: Optional[pd.DataFrame]) -> dict:
        """
        Calculate RSI, Bollinger Bands, ADX, VWAP, ATR, and signal score.
        Identical logic to MarketDataFetcher.calculate_indicators.
        """
        if df is None or len(df) < 20:
            return {"error": "Insufficient data"}
        if not TA_AVAILABLE:
            return {"error": "ta library not installed"}

        try:
            close  = df["close"]
            high   = df["high"]
            low    = df["low"]
            volume = df["volume"]

            rsi_val   = float(ta.momentum.RSIIndicator(close=close, window=14).rsi().iloc[-1])
            bb        = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
            bb_upper  = float(bb.bollinger_hband().iloc[-1])
            bb_middle = float(bb.bollinger_mavg().iloc[-1])
            bb_lower  = float(bb.bollinger_lband().iloc[-1])
            bb_pct    = float(bb.bollinger_pband().iloc[-1])
            atr_val   = float(ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range().iloc[-1])
            adx_val   = float(ta.trend.ADXIndicator(high=high, low=low, close=close, window=14).adx().iloc[-1])
            ma20      = float(close.rolling(20).mean().iloc[-1])
            ma50      = float(close.rolling(50).mean().iloc[-1]) if len(df) >= 50 else ma20
            vwap      = float(((((high + low + close) / 3) * volume).rolling(20).sum() / volume.rolling(20).sum()).iloc[-1])
            price     = float(close.iloc[-1])

            signals, score = [], 0
            if rsi_val < 32:
                signals.append("RSI<32 (strong oversold)"); score += 2
            elif rsi_val < 40:
                signals.append("RSI<40 (mild oversold)"); score += 1
            if price < bb_lower:
                signals.append("BelowLowerBB"); score += 1
            if vwap > 0 and price < vwap * 0.99:
                signals.append("BelowVWAP>1%"); score += 1
            if adx_val < 20:
                signals.append("ADX<20 (ranging market)"); score += 1

            trending = adx_val > 25
            return {
                "current_price":          price,
                "rsi":                    round(rsi_val, 2),
                "bollinger": {
                    "upper":  round(bb_upper, 2),
                    "middle": round(bb_middle, 2),
                    "lower":  round(bb_lower, 2),
                    "pct_b":  round(bb_pct, 3),
                },
                "vwap":                   round(vwap, 2),
                "atr":                    round(atr_val, 2),
                "adx":                    round(adx_val, 2),
                "ma20":                   round(ma20, 2),
                "ma50":                   round(ma50, 2),
                "stop_loss_price":        round(price - 0.5 * atr_val, 2),
                "take_profit_price":      round(ma20, 2),
                "signal_score":           score,
                "signals_fired":          signals,
                "regime":                 "TRENDING" if trending else "RANGING",
                "mean_reversion_eligible": not trending and score >= 3,
            }
        except Exception as e:
            logger.error(f"Indicator calculation error: {e}")
            return {"error": str(e)}

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """Fetch spot wallet balances. Requires BYBIT_API_KEY."""
        if not self._connected:
            return {"error": "Bybit not connected"}
        if not self._has_private:
            return {"error": "No API key — add BYBIT_API_KEY to .env to see balance"}
        try:
            bal = self.exchange_priv.fetch_balance()
            usdt_total = float((bal.get("USDT") or {}).get("total", 0))
            usdt_free  = float((bal.get("USDT") or {}).get("free",  0))

            positions = []
            for currency, info in (bal.get("total") or {}).items():
                if currency == "USDT":
                    continue
                amt = float(info) if info else 0
                if amt > 1e-6:
                    positions.append({"currency": currency, "amount": round(amt, 8)})

            return {
                "total_usdt":    round(usdt_total, 2),
                "free_usdt":     round(usdt_free,  2),
                "open_positions": len(positions),
                "positions":     positions,
                "exchange":      "bybit",
                "testnet":       self.testnet,
            }
        except Exception as e:
            logger.error(f"Bybit balance error: {e}")
            return {"error": str(e)}

    # ── ORDER PLACEMENT ───────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,
        usdt_amount: float,
        current_price: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> dict:
        """
        Place a spot market order + stop-loss trigger on Bybit.
        Requires BYBIT_API_KEY and BYBIT_SECRET_KEY.
        """
        if not self._connected:
            return {"status": "ERROR", "reason": "Bybit not connected"}
        if not self._has_private:
            return {"status": "ERROR", "reason": "No API key — add BYBIT_API_KEY to .env"}

        try:
            base_amount = usdt_amount / current_price

            if side.upper() == "BUY":
                order = self.exchange_priv.create_market_buy_order(symbol, base_amount)
                fill_price = float(order.get("average") or order.get("price") or current_price)

                # Attach stop-loss as a separate trigger order
                try:
                    self.exchange_priv.create_order(
                        symbol, "limit", "sell", base_amount, stop_loss_price,
                        params={"triggerPrice": str(stop_loss_price), "orderType": "Limit"}
                    )
                except Exception as sl_err:
                    logger.warning(f"Stop-loss order failed (main order still placed): {sl_err}")

                return {
                    "status":      "SUBMITTED",
                    "exchange":    "bybit",
                    "testnet":     self.testnet,
                    "symbol":      symbol,
                    "side":        "BUY",
                    "base_amount": round(base_amount, 6),
                    "usdt_spent":  round(usdt_amount, 2),
                    "fill_price":  round(fill_price, 6),
                    "stop_loss":   stop_loss_price,
                    "take_profit": take_profit_price,
                    "order_id":    order.get("id"),
                }

            else:  # SELL
                order = self.exchange_priv.create_market_sell_order(symbol, base_amount)
                fill_price = float(order.get("average") or order.get("price") or current_price)
                return {
                    "status":         "SUBMITTED",
                    "exchange":       "bybit",
                    "testnet":        self.testnet,
                    "symbol":         symbol,
                    "side":           "SELL",
                    "base_amount":    round(base_amount, 6),
                    "usdt_received":  round(base_amount * fill_price, 2),
                    "fill_price":     round(fill_price, 6),
                    "order_id":       order.get("id"),
                }

        except Exception as e:
            logger.error(f"Bybit order error: {e}")
            return {"status": "ERROR", "reason": str(e), "symbol": symbol}

    # ── SNAPSHOT ─────────────────────────────────────────────────────────────

    def _rate_setup(self, indicators: dict) -> str:
        if "error" in indicators:
            return "NO_DATA"
        score    = indicators.get("signal_score", 0)
        eligible = indicators.get("mean_reversion_eligible", False)
        if not eligible:
            return "SKIP"
        if score >= 5:  return "A_GRADE"
        if score >= 4:  return "B_GRADE"
        if score >= 3:  return "C_GRADE"
        return "SKIP"

    def build_crypto_snapshot(self, symbols: list) -> list:
        """
        Build a watchlist snapshot for all crypto symbols.
        Returns a list in the same format as MarketDataFetcher.build_market_snapshot
        so the dashboard can render both with the same code.
        """
        results = []
        for symbol in symbols:
            logger.info(f"Fetching Bybit data for {symbol}...")
            df         = self.get_ohlcv(symbol, days=60)
            indicators = self.calculate_indicators(df)
            ticker     = self.get_ticker(symbol)

            # Use live ticker price if indicator calc failed
            if "current_price" not in indicators and ticker.get("price"):
                indicators["current_price"] = ticker["price"]

            results.append({
                "symbol":        symbol,
                "indicators":    indicators,
                "ticker":        ticker,
                "news_sentiment": {"sentiment": "neutral", "score": 0.0, "source": "none"},
                "recent_headlines": [],
                "setup_quality": self._rate_setup(indicators),
                "exchange":      "bybit",
            })

        results.sort(key=lambda x: x["indicators"].get("signal_score", 0), reverse=True)
        return results
