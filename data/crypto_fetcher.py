"""
data/crypto_fetcher.py
CCXT + Bybit integration for crypto market data and order execution.
Produces the same indicator/snapshot structure as MarketDataFetcher
so Claude's decision agent works identically for equities and crypto.

Public market data (OHLCV, tickers) uses Bybit's public REST API directly
as the primary path — no API key, no ccxt market loading required.
ccxt is used only for private operations (balance, orders).
"""

import os
import logging
import requests
from pathlib import Path
from typing import Optional
import pandas as pd
import pytz
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
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

BYBIT_REST_BASE   = "https://api.bybit.com"
BINANCE_REST_BASE = "https://api.binance.com"


class BybitFetcher:
    """
    Fetches crypto market data from Bybit and calculates the same technical
    indicators as MarketDataFetcher so Claude's decision agent works
    identically for equities and crypto.

    Public data (OHLCV, tickers): uses Bybit REST API directly — no API key,
    no ccxt market-loading required. Works on any hosting environment.
    Private data (balance, orders): BYBIT_API_KEY + BYBIT_SECRET_KEY required.
    """

    def __init__(self):
        self.api_key    = os.getenv("BYBIT_API_KEY", "")
        self.api_secret = os.getenv("BYBIT_SECRET_KEY", "")
        self.testnet    = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
        self.exchange        = None   # ccxt — public market data (optional)
        self.exchange_priv   = None   # ccxt testnet/live — orders & balance
        self._connected      = False  # ccxt public connection succeeded
        self._connect_error  = None   # last ccxt connection error (for UI display)
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
            self._connect_error = str(e)
            logger.error(f"Bybit public connection failed (market data will use REST): {e}")

        # Private exchange — demo/live, for orders & balance only
        # load_markets() is best-effort — balance/orders still work without it.
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
                self.exchange_priv.set_sandbox_mode(True)  # → api-testnet.bybit.com
            mode = "TESTNET orders" if self.testnet else "LIVE orders"
            key_status = "API key loaded" if self._has_private else "no key — balance/orders unavailable"
            logger.info(f"Bybit private exchange ready ({mode}) — {key_status}")
        except Exception as e:
            self.exchange_priv = None
            self._has_private = False
            self._connect_error = str(e)
            logger.warning(f"Bybit private exchange init failed: {e}")

        if self.exchange_priv is not None and self._has_private:
            try:
                self.exchange_priv.load_markets()
            except Exception as e:
                logger.warning(f"Bybit private load_markets failed (continuing): {e}")

    # ── DATA FETCHING ─────────────────────────────────────────────────────────

    @staticmethod
    def _bybit_symbol(symbol: str) -> str:
        """Convert 'BTC/USDT' → 'BTCUSDT' for Bybit REST API."""
        return symbol.replace("/", "")

    def get_ohlcv(self, symbol: str, days: int = 60) -> Optional[pd.DataFrame]:
        """
        Fetch daily OHLCV candles.
        Priority: Binance REST → Bybit REST → ccxt.
        Binance is first because it has no cloud-provider IP restrictions.
        """
        df = self._get_ohlcv_binance(symbol, days)
        if df is not None:
            return df
        df = self._get_ohlcv_bybit(symbol, days)
        if df is not None:
            return df
        if self._connected:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=min(days + 5, 200))
                df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                return df.tail(days).reset_index(drop=True)
            except Exception as e:
                logger.error(f"ccxt OHLCV fallback failed for {symbol}: {e}")
        return None

    def _get_ohlcv_binance(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        """Binance REST — GET /api/v3/klines. No auth required. Works from all cloud IPs."""
        try:
            r = requests.get(
                f"{BINANCE_REST_BASE}/api/v3/klines",
                params={
                    "symbol":   self._bybit_symbol(symbol),  # BTCUSDT format matches Binance
                    "interval": "1d",
                    "limit":    min(days + 5, 200),
                },
                timeout=10,
            )
            data = r.json()
            if isinstance(data, dict) and data.get("code"):
                logger.warning(f"Binance OHLCV error for {symbol}: {data.get('msg')}")
                return None
            # Binance kline: [openTime, open, high, low, close, volume, closeTime, ...]
            df = pd.DataFrame(data, columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ])
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df[["timestamp", "open", "high", "low", "close", "volume"]].tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"Binance OHLCV failed for {symbol}: {e}")
            return None

    def _get_ohlcv_bybit(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        """Bybit v5 REST — GET /v5/market/kline. No auth required."""
        try:
            r = requests.get(
                f"{BYBIT_REST_BASE}/v5/market/kline",
                params={
                    "category": "spot",
                    "symbol":   self._bybit_symbol(symbol),
                    "interval": "D",
                    "limit":    min(days + 5, 200),
                },
                timeout=10,
            )
            data = r.json()
            if data.get("retCode") != 0:
                logger.warning(f"Bybit REST OHLCV error for {symbol}: {data.get('retMsg')}")
                return None
            items = list(reversed(data["result"]["list"]))  # Bybit returns newest-first
            if not items:
                return None
            df = pd.DataFrame(
                items,
                columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"Bybit REST OHLCV failed for {symbol}: {e}")
            return None

    def get_ticker(self, symbol: str) -> dict:
        """
        Fetch live ticker — price, 24h change, volume.
        Priority: Binance REST → Bybit REST → ccxt.
        """
        ticker = self._get_ticker_binance(symbol)
        if ticker:
            return ticker
        ticker = self._get_ticker_bybit(symbol)
        if ticker:
            return ticker
        if self._connected:
            try:
                t = self.exchange.fetch_ticker(symbol)
                return {
                    "price":             t.get("last", 0),
                    "change_pct_24h":    round(t.get("percentage", 0) or 0, 2),
                    "volume_24h_usdt":   round(t.get("quoteVolume", 0) or 0, 2),
                    "high_24h":          t.get("high", 0),
                    "low_24h":           t.get("low", 0),
                    "bid":               t.get("bid", 0),
                    "ask":               t.get("ask", 0),
                }
            except Exception as e:
                logger.error(f"ccxt ticker fallback failed for {symbol}: {e}")
        return {}

    def _get_ticker_binance(self, symbol: str) -> dict:
        """Binance REST — GET /api/v3/ticker/24hr. No auth required."""
        try:
            r = requests.get(
                f"{BINANCE_REST_BASE}/api/v3/ticker/24hr",
                params={"symbol": self._bybit_symbol(symbol)},
                timeout=10,
            )
            data = r.json()
            if isinstance(data, dict) and data.get("code"):
                return {}
            return {
                "price":             float(data.get("lastPrice", 0) or 0),
                "change_pct_24h":    round(float(data.get("priceChangePercent", 0) or 0), 2),
                "volume_24h_usdt":   round(float(data.get("quoteVolume", 0) or 0), 2),
                "high_24h":          float(data.get("highPrice", 0) or 0),
                "low_24h":           float(data.get("lowPrice", 0) or 0),
                "bid":               float(data.get("bidPrice", 0) or 0),
                "ask":               float(data.get("askPrice", 0) or 0),
            }
        except Exception as e:
            logger.error(f"Binance ticker failed for {symbol}: {e}")
            return {}

    def _get_ticker_bybit(self, symbol: str) -> dict:
        """Bybit v5 REST — GET /v5/market/tickers. No auth required."""
        try:
            r = requests.get(
                f"{BYBIT_REST_BASE}/v5/market/tickers",
                params={"category": "spot", "symbol": self._bybit_symbol(symbol)},
                timeout=10,
            )
            data = r.json()
            if data.get("retCode") != 0:
                return {}
            items = data["result"].get("list", [])
            if not items:
                return {}
            item = items[0]
            return {
                "price":             float(item.get("lastPrice", 0) or 0),
                "change_pct_24h":    round(float(item.get("price24hPcnt", 0) or 0) * 100, 2),
                "volume_24h_usdt":   round(float(item.get("turnover24h", 0) or 0), 2),
                "high_24h":          float(item.get("highPrice24h", 0) or 0),
                "low_24h":           float(item.get("lowPrice24h", 0) or 0),
                "bid":               float(item.get("bid1Price", 0) or 0),
                "ask":               float(item.get("ask1Price", 0) or 0),
            }
        except Exception as e:
            logger.error(f"Bybit REST ticker failed for {symbol}: {e}")
            return {}

    # ── INDICATORS ────────────────────────────────────────────────────────────

    def calculate_indicators(self, df: Optional[pd.DataFrame], min_score: int = 3) -> dict:
        """
        Calculate RSI, Bollinger Bands, ADX, MA, ATR, and signal score.
        Identical logic to MarketDataFetcher.calculate_indicators.
        min_score is passed in from the active risk profile so that
        mean_reversion_eligible correctly reflects the current profile.
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
            price     = float(close.iloc[-1])

            signals, score = [], 0
            if rsi_val < 35:
                signals.append("RSI<35 (strong oversold)"); score += 2
            elif rsi_val < 45:
                signals.append("RSI<45 (mild oversold)"); score += 1
            if price < bb_lower:
                signals.append("BelowLowerBB"); score += 1
            if ma20 > 0 and price < ma20 * 0.99:
                signals.append("BelowMA20>1%"); score += 1
            if adx_val < 25:
                signals.append("ADX<25 (ranging market)"); score += 1

            trending = adx_val > 30
            return {
                "current_price":          price,
                "rsi":                    round(rsi_val, 2),
                "bollinger": {
                    "upper":  round(bb_upper, 2),
                    "middle": round(bb_middle, 2),
                    "lower":  round(bb_lower, 2),
                    "pct_b":  round(bb_pct, 3),
                },
                "ma20_deviation_pct":     round((price / ma20 - 1) * 100, 2) if ma20 > 0 else 0,
                "atr":                    round(atr_val, 2),
                "adx":                    round(adx_val, 2),
                "ma20":                   round(ma20, 2),
                "ma50":                   round(ma50, 2),
                "stop_loss_price":        round(price - 0.5 * atr_val, 2),
                "take_profit_price":      round(ma20, 2),
                "signal_score":           score,
                "signals_fired":          signals,
                "regime":                 "TRENDING" if trending else "RANGING",
                "mean_reversion_eligible": not trending and score >= min_score,
            }
        except Exception as e:
            logger.error(f"Indicator calculation error: {e}")
            return {"error": str(e)}

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """
        Fetch wallet balances across Unified (trading) and Funding accounts.
        Bybit testnet faucet drops USDT into Funding by default — UTA only
        sees coins moved into Unified, so we aggregate both.
        """
        if self.exchange_priv is None:
            return {"error": "Bybit not connected"}
        if not self._has_private:
            return {"error": "No API key — add BYBIT_API_KEY to .env to see balance"}
        try:
            unified = self._fetch_unified_balance()
            funding = self._fetch_funding_balance()

            by_coin: dict = {}
            for src, label in ((unified, "unified"), (funding, "funding")):
                for coin, qty in src.items():
                    if qty <= 1e-9:
                        continue
                    rec = by_coin.setdefault(coin, {"unified": 0.0, "funding": 0.0})
                    rec[label] += qty

            usdt_rec   = by_coin.pop("USDT", {"unified": 0.0, "funding": 0.0})
            usdt_total = round(usdt_rec["unified"] + usdt_rec["funding"], 2)
            usdt_free  = round(unified.get("USDT", 0.0), 2)

            positions = []
            for coin, rec in sorted(by_coin.items()):
                amount = rec["unified"] + rec["funding"]
                price  = self._coin_usd_price(coin)
                positions.append({
                    "currency":  coin,
                    "amount":    round(amount, 8),
                    "unified":   round(rec["unified"], 8),
                    "funding":   round(rec["funding"], 8),
                    "price_usd": round(price, 2) if price else None,
                    "value_usd": round(amount * price, 2) if price else None,
                })

            position_value = sum((p["value_usd"] or 0) for p in positions)

            return {
                "account_value":  round(usdt_total + position_value, 2),
                "total_usdt":     usdt_total,
                "free_usdt":      usdt_free,
                "funding_usdt":   round(usdt_rec["funding"], 2),
                "position_value": round(position_value, 2),
                "open_positions": len(positions),
                "positions":      positions,
                "exchange":       "bybit",
                "testnet":        self.testnet,
            }
        except Exception as e:
            logger.error(f"Bybit balance error: {e}")
            return {"error": str(e)}

    def _fetch_unified_balance(self) -> dict:
        try:
            raw = self.exchange_priv.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"})
            if int(raw.get("retCode", -1)) != 0:
                return {}
            coins = (raw.get("result", {}).get("list") or [{}])[0].get("coin", [])
            out = {}
            for coin in coins:
                sym = coin.get("coin", "")
                qty = float(coin.get("walletBalance", 0) or 0)
                if sym == "USDT":
                    qty = float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0)
                if qty > 0:
                    out[sym] = qty
            return out
        except Exception as e:
            logger.warning(f"Unified balance fetch failed: {e}")
            return {}

    def _fetch_funding_balance(self) -> dict:
        try:
            raw = self.exchange_priv.privateGetV5AssetTransferQueryAccountCoinsBalance({"accountType": "FUND"})
            if int(raw.get("retCode", -1)) != 0:
                return {}
            balances = raw.get("result", {}).get("balance", []) or []
            out = {}
            for b in balances:
                sym = b.get("coin", "")
                qty = float(b.get("walletBalance", 0) or 0)
                if qty > 0:
                    out[sym] = qty
            return out
        except Exception as e:
            logger.warning(f"Funding balance fetch failed: {e}")
            return {}

    def _coin_usd_price(self, coin: str) -> float:
        if coin in ("USDT", "USDC", "USD", "DAI"):
            return 1.0
        try:
            t = self.get_ticker(f"{coin}/USDT")
            return float(t.get("price", 0) or 0)
        except Exception:
            return 0.0

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
        if self.exchange_priv is None:
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

    def _rate_setup(self, indicators: dict, min_score: int = 3) -> str:
        """
        Rate the active strategy's setup quality. Grade is computed against
        whichever score (mean reversion or momentum) drives the chosen
        strategy_mode. Returns SKIP if no strategy is eligible.
        """
        if "error" in indicators:
            return "NO_DATA"
        from strategies.momentum import active_score
        mode = indicators.get("strategy_mode", "NONE")
        if mode == "NONE":
            return "SKIP"
        score = active_score(indicators)
        if score >= min_score + 2: return "A_GRADE"
        if score >= min_score + 1: return "B_GRADE"
        if score >= min_score:     return "C_GRADE"
        return "SKIP"

    def build_crypto_snapshot(self, symbols: list) -> list:
        """
        Build a watchlist snapshot for all crypto symbols.
        Returns a list in the same format as MarketDataFetcher.build_market_snapshot
        so the dashboard can render both with the same code.
        """
        from agents.executor import load_risk_profile
        from strategies.engine import StrategyEngine
        _, active_profile = load_risk_profile()
        min_score = active_profile.get("min_signal_score", 3)

        engine = StrategyEngine()
        active = engine.active_strategies()
        if active:
            logger.info(f"Math strategies active: {active}")

        ohlcv_cache: dict = {}  # {symbol: df} — used by multi-asset strategies
        results = []
        for symbol in symbols:
            logger.info(f"Fetching Bybit data for {symbol}...")
            df         = self.get_ohlcv(symbol, days=60)
            if df is not None:
                ohlcv_cache[symbol] = df
            indicators = self.calculate_indicators(df, min_score=min_score)
            ticker     = self.get_ticker(symbol)

            # Use live ticker price if indicator calc failed
            if "current_price" not in indicators and ticker.get("price"):
                indicators["current_price"] = ticker["price"]

            # Apply per-symbol mathematical strategies (Hurst, wavelet, entropy, Lévy)
            if "error" not in indicators and df is not None:
                indicators = engine.enrich_symbol(symbol, df, indicators)

            # Always override current_price with the live ticker price so
            # intraday moves are reflected even though indicators use daily bars.
            live_price = ticker.get("price")
            if live_price and "error" not in indicators:
                ma20     = indicators.get("ma20", 0)
                bb_lower = indicators.get("bollinger", {}).get("lower", 0)
                atr      = indicators.get("atr", 0)
                indicators["current_price"] = live_price
                indicators["price_source"]  = "live_intraday"
                if ma20 > 0:
                    indicators["ma20_deviation_pct"] = round((live_price / ma20 - 1) * 100, 2)
                    fired = indicators.get("signals_fired", [])
                    score = indicators.get("signal_score", 0)
                    had_ma20 = any("BelowMA20" in s for s in fired)
                    now_ma20 = live_price < ma20 * 0.99
                    if not had_ma20 and now_ma20:
                        fired.append("BelowMA20>1%"); score += 1
                    elif had_ma20 and not now_ma20:
                        fired = [s for s in fired if "BelowMA20" not in s]; score -= 1
                    had_bb = any("BelowLowerBB" in s for s in fired)
                    now_bb = bb_lower > 0 and live_price < bb_lower
                    if not had_bb and now_bb:
                        fired.append("BelowLowerBB"); score += 1
                    elif had_bb and not now_bb:
                        fired = [s for s in fired if "BelowLowerBB" not in s]; score -= 1
                    indicators["signals_fired"] = fired
                    indicators["signal_score"]  = max(0, score)
                    indicators["mean_reversion_eligible"] = (
                        indicators.get("adx", 30) <= 30 and indicators["signal_score"] >= min_score
                    )
                if atr > 0:
                    indicators["stop_loss_price"] = round(live_price - 0.5 * atr, 6)
            elif live_price and "current_price" not in indicators:
                indicators["current_price"] = live_price
                indicators["price_source"]  = "live_ticker_fallback"
            else:
                indicators["price_source"] = "daily_close"

            # Directional gate + momentum scoring + regime router
            from strategies.momentum import (
                apply_directional_gate, compute_momentum, select_strategy_mode,
                active_score as _active_score,
            )
            apply_directional_gate(indicators)
            if "error" not in indicators and df is not None:
                compute_momentum(df, indicators)
            select_strategy_mode(indicators, min_score)

            results.append({
                "symbol":        symbol,
                "indicators":    indicators,
                "ticker":        ticker,
                "news_sentiment": {"sentiment": "neutral", "score": 0.0, "source": "none"},
                "recent_headlines": [],
                "setup_quality": self._rate_setup(indicators, min_score=min_score),
                "exchange":      "bybit",
            })

        results.sort(
            key=lambda x: (
                _active_score(x["indicators"]) if x["indicators"].get("strategy_mode", "NONE") != "NONE"
                else x["indicators"].get("signal_score", 0)
            ),
            reverse=True,
        )

        # Apply multi-asset mathematical strategies (Transfer Entropy, RMT, Wasserstein, TDA)
        portfolio_math = engine.enrich_portfolio(results, ohlcv_cache)
        if portfolio_math:
            for item in results:
                item["portfolio_math_signals"] = portfolio_math

        return results


class BinanceFetcher:
    """
    Binance Spot trading via ccxt — authenticated operations only.
    Public market data (OHLCV, tickers) is handled by the static Binance REST
    methods already present in BybitFetcher; no duplication needed here.

    Set BINANCE_TESTNET=true to target testnet.binance.vision (default).
    Set BINANCE_TESTNET=false for the live exchange.
    """

    def __init__(self):
        self.api_key      = os.getenv("BINANCE_API_KEY", "")
        self.api_secret   = os.getenv("BINANCE_SECRET_KEY", "")
        self.testnet      = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
        self.exchange     = None
        self._has_private = bool(self.api_key and self.api_secret)
        self._markets_loaded = False

        if CCXT_AVAILABLE and self._has_private:
            self._connect()

    def _connect(self):
        try:
            self.exchange = ccxt.binance({
                "apiKey":  self.api_key,
                "secret":  self.api_secret,
                "options": {"defaultType": "spot"},
                "enableRateLimit": True,
            })
            if self.testnet:
                self.exchange.set_sandbox_mode(True)
                # testnet.binance.vision uses a CA not in Python's certifi bundle;
                # ccxt passes verify=exchange.verify to requests — session.verify has no effect.
                self.exchange.verify = False
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            # load_markets() is deferred to _ensure_markets() — it takes ~2 min on testnet
            # and is only needed for precision methods used in order placement.
            self._markets_loaded = False
            mode = "TESTNET" if self.testnet else "LIVE"
            logger.info(f"Binance connected ({mode}) — API key loaded")
        except Exception as e:
            self.exchange = None
            self._has_private = False
            logger.error(f"Binance connection failed: {e}")

    def _ensure_markets(self):
        """Load markets once, lazily — only needed before order placement."""
        if not self._markets_loaded and self.exchange:
            logger.info("Binance: loading markets (one-time, needed for order precision)…")
            self.exchange.load_markets()
            self._markets_loaded = True

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """Fetch Binance spot USDT balance and open coin positions.

        Ticker lookups are batched in one fetch_tickers() call (capped at 20
        symbols) to avoid the per-coin request avalanche that testnet accounts
        accumulate as dust after many test trades.
        """
        if not self.exchange or not self._has_private:
            return {"error": "Binance not connected — add BINANCE_API_KEY to .env"}
        try:
            raw = self.exchange.fetch_balance()
            usdt_free  = float((raw.get("USDT") or {}).get("free",  0) or 0)
            usdt_total = float((raw.get("USDT") or {}).get("total", 0) or 0)

            # Collect non-USDT coins with a non-trivial amount (filter out dust
            # and testnet fake tokens that have non-ASCII names).
            coin_amounts: dict[str, float] = {}
            for coin, data in (raw.get("total") or {}).items():
                if coin == "USDT":
                    continue
                amount = float(data or 0)
                if amount > 1e-4 and coin.isascii() and coin.isalnum() and not coin.isdigit():
                    coin_amounts[coin] = amount

            # Batch-fetch tickers for up to 20 coins in a single API call.
            prices: dict[str, float] = {}
            if coin_amounts:
                symbols = [f"{c}/USDT" for c in list(coin_amounts)[:20]]
                try:
                    tickers = self.exchange.fetch_tickers(symbols)
                    for sym, ticker in tickers.items():
                        coin = sym.split("/")[0]
                        prices[coin] = float(ticker.get("last", 0) or 0)
                except Exception as te:
                    logger.warning(f"Binance batch ticker fetch failed: {te}")

            positions = []
            for coin, amount in list(coin_amounts.items())[:20]:
                price = prices.get(coin)
                value = round(amount * price, 2) if price else None
                positions.append({
                    "currency":  coin,
                    "amount":    round(amount, 8),
                    "price_usd": round(price, 2) if price else None,
                    "value_usd": value,
                })

            position_value = sum((p["value_usd"] or 0) for p in positions)
            return {
                "account_value":  round(usdt_total + position_value, 2),
                "total_usdt":     round(usdt_total, 2),
                "free_usdt":      round(usdt_free, 2),
                "position_value": round(position_value, 2),
                "open_positions": len(coin_amounts),
                "positions":      positions,
                "exchange":       "binance",
                "testnet":        self.testnet,
            }
        except Exception as e:
            logger.error(f"Binance balance error: {e}")
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
        Place a Binance spot market order + stop-loss limit order.
        Uses quoteOrderQty for BUY so Binance handles lot-size precision automatically.
        """
        if not self.exchange or not self._has_private:
            return {"status": "ERROR", "reason": "Binance not connected"}

        self._ensure_markets()

        try:
            if side.upper() == "BUY":
                # quoteOrderQty lets Binance convert USDT → base internally,
                # avoiding lot-size rounding issues on our side.
                order = self.exchange.create_market_buy_order(
                    symbol, None,
                    params={"quoteOrderQty": usdt_amount},
                )
                fill_price  = float(order.get("average") or order.get("price") or current_price)
                base_filled = float(order.get("filled") or (usdt_amount / fill_price))

                # Attach stop-loss as a STOP_LOSS_LIMIT order (GTC)
                try:
                    sl_price = self.exchange.price_to_precision(symbol, stop_loss_price)
                    self.exchange.create_order(
                        symbol, "STOP_LOSS_LIMIT", "sell",
                        base_filled,
                        price=float(sl_price),
                        params={"stopPrice": float(sl_price), "timeInForce": "GTC"},
                    )
                except Exception as sl_err:
                    logger.warning(f"Binance stop-loss order failed (main order still placed): {sl_err}")

                return {
                    "status":      "SUBMITTED",
                    "exchange":    "binance",
                    "testnet":     self.testnet,
                    "symbol":      symbol,
                    "side":        "BUY",
                    "base_amount": round(base_filled, 6),
                    "usdt_spent":  round(usdt_amount, 2),
                    "fill_price":  round(fill_price, 6),
                    "stop_loss":   stop_loss_price,
                    "take_profit": take_profit_price,
                    "order_id":    order.get("id"),
                }

            else:  # SELL — close position using base amount from decision
                base_amount = float(usdt_amount / current_price)
                base_amount = float(self.exchange.amount_to_precision(symbol, base_amount))
                order = self.exchange.create_market_sell_order(symbol, base_amount)
                fill_price = float(order.get("average") or order.get("price") or current_price)
                return {
                    "status":        "SUBMITTED",
                    "exchange":      "binance",
                    "testnet":       self.testnet,
                    "symbol":        symbol,
                    "side":          "SELL",
                    "base_amount":   round(base_amount, 6),
                    "usdt_received": round(base_amount * fill_price, 2),
                    "fill_price":    round(fill_price, 6),
                    "order_id":      order.get("id"),
                }

        except Exception as e:
            logger.error(f"Binance order error: {e}")
            return {"status": "ERROR", "reason": str(e), "symbol": symbol}
