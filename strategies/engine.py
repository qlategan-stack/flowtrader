"""
strategies/engine.py
StrategyEngine — Orchestrates all mathematical strategies.

Reads journal/math_strategies.json to determine which strategies are active.
Each strategy can be toggled independently so you can A/B test their impact.

Pipeline roles:
  enrich_symbol()    — per-symbol strategies run on each OHLCV series
  enrich_portfolio() — multi-asset strategies run across all symbols

Score augmentation:
  Enabled strategies can add/subtract from the existing signal_score.
  The total is capped at MAX_SIGNAL_SCORE (6) and floored at 0 so the
  existing risk profile min_signal_score thresholds remain meaningful.
"""

import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Where the engine looks for math_strategies.json (same search order as risk_profile.json)
_BOT_ROOT        = Path(__file__).resolve().parent.parent
_LOCAL_STRATEGIES_FILE     = _BOT_ROOT / "journal" / "math_strategies.json"
_DASHBOARD_STRATEGIES_FILE = _BOT_ROOT.parent.parent / "flowtrader-dashboard" / "journal" / "math_strategies.json"

MAX_SIGNAL_SCORE = 6

_STRATEGY_DEFAULTS = {
    "wavelet_denoising":  {"enabled": False, "label": "Wavelet Denoising",   "role": "per_symbol",    "deps": "pywt"},
    "hurst_exponent":     {"enabled": False, "label": "Hurst Exponent",       "role": "per_symbol",    "deps": "none"},
    "entropy_regime":     {"enabled": False, "label": "Entropy Regime",       "role": "per_symbol",    "deps": "none"},
    "levy_jump":          {"enabled": False, "label": "Lévy Jump Detection",  "role": "per_symbol",    "deps": "none"},
    "transfer_entropy":   {"enabled": False, "label": "Transfer Entropy",     "role": "multi_asset",   "deps": "none"},
    "rmt_correlation":    {"enabled": False, "label": "RMT Correlation",      "role": "multi_asset",   "deps": "scipy"},
    "wasserstein_regime": {"enabled": False, "label": "Wasserstein Regime",   "role": "multi_asset",   "deps": "scipy"},
    "tda_features":       {"enabled": False, "label": "TDA Homology",         "role": "multi_asset",   "deps": "ripser"},
}


def _file_updated_at(path: Path) -> str:
    """Return the updated_at timestamp from a strategies JSON file, or empty."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("updated_at", "")
    except Exception:
        return ""


def _find_strategies_file() -> Optional[Path]:
    """
    Pick whichever strategies file has the newer `updated_at` stamp.
    The dashboard repo file (flowtrader-dashboard) is the user-facing control
    surface; the local file may be stale from local testing. Newer wins so the
    dashboard's GitHub-synced toggles always take effect on the next bot run.
    """
    local_exists = _LOCAL_STRATEGIES_FILE.exists()
    dash_exists  = _DASHBOARD_STRATEGIES_FILE.exists()
    if not local_exists and not dash_exists:
        return None
    if local_exists and not dash_exists:
        return _LOCAL_STRATEGIES_FILE
    if dash_exists and not local_exists:
        return _DASHBOARD_STRATEGIES_FILE
    # Both exist — prefer whichever was updated more recently
    if _file_updated_at(_DASHBOARD_STRATEGIES_FILE) >= _file_updated_at(_LOCAL_STRATEGIES_FILE):
        return _DASHBOARD_STRATEGIES_FILE
    return _LOCAL_STRATEGIES_FILE


class StrategyEngine:
    """
    Reads the active strategy config and applies enabled mathematical
    strategies as signal enrichers on top of the existing technical indicators.
    """

    def __init__(self):
        self._config = self._load_config()

    def _load_config(self) -> dict:
        path = _find_strategies_file()
        if path is None:
            logger.debug("math_strategies.json not found — all strategies disabled")
            return {k: v.copy() for k, v in _STRATEGY_DEFAULTS.items()}

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            strategies = raw.get("strategies", {})
            config = {}
            for key, defaults in _STRATEGY_DEFAULTS.items():
                entry = strategies.get(key, {})
                config[key] = {**defaults, "enabled": entry.get("enabled", False)}
            enabled_keys = [k for k, v in config.items() if v["enabled"]]
            updated_at = raw.get("updated_at", "?")
            logger.info(
                f"Math strategies loaded from {path.name} (updated {updated_at}): "
                f"{enabled_keys if enabled_keys else 'none'}"
            )
            return config
        except Exception as e:
            logger.warning(f"Could not load math_strategies.json: {e} — all disabled")
            return {k: v.copy() for k, v in _STRATEGY_DEFAULTS.items()}

    def is_enabled(self, name: str) -> bool:
        return self._config.get(name, {}).get("enabled", False)

    def active_strategies(self) -> list[str]:
        return [k for k, v in self._config.items() if v.get("enabled")]

    def all_strategies(self) -> dict:
        return self._config.copy()

    # ── Per-symbol enrichment ─────────────────────────────────────────────────

    def enrich_symbol(self, symbol: str, df: Optional[pd.DataFrame],
                       indicators: dict) -> dict:
        """
        Apply all enabled per-symbol strategies to the indicators dict.

        Modifies indicators in-place and returns it. Adds:
          indicators["math_signals"] — dict of per-strategy results
          Adjusts indicators["signal_score"] — capped at MAX_SIGNAL_SCORE
          Appends to indicators["signals_fired"] — human-readable signal names
        """
        if df is None or len(df) < 20:
            return indicators

        any_active = any(
            self.is_enabled(k)
            for k in ("wavelet_denoising", "hurst_exponent", "entropy_regime", "levy_jump")
        )
        if not any_active:
            return indicators

        prices = df["close"].values.astype(float)
        math_signals: dict = {}
        total_delta = 0

        # 1. Wavelet denoising
        if self.is_enabled("wavelet_denoising"):
            from strategies.wavelet import compute as wavelet_compute
            result = wavelet_compute(prices)
            math_signals["wavelet"] = result
            if "score_delta" in result and "error" not in result:
                total_delta += result["score_delta"]
                if result["score_delta"] > 0:
                    indicators.setdefault("signals_fired", []).append(
                        f"Wavelet:{result.get('frequency_regime','?')}"
                    )
                elif result["score_delta"] < 0:
                    indicators.setdefault("signals_fired", []).append(
                        f"Wavelet:NOISE_DOMINANT (-1)"
                    )

        # 2. Hurst exponent
        if self.is_enabled("hurst_exponent"):
            from strategies.hurst import compute as hurst_compute
            result = hurst_compute(prices)
            math_signals["hurst"] = result
            if "score_delta" in result and "error" not in result:
                total_delta += result["score_delta"]
                h_val = result.get("h", 0.5)
                if result["score_delta"] > 0:
                    indicators.setdefault("signals_fired", []).append(
                        f"Hurst={h_val:.2f}(anti-persistent+1)"
                    )
                elif result["score_delta"] < 0:
                    indicators.setdefault("signals_fired", []).append(
                        f"Hurst={h_val:.2f}(trending-1)"
                    )

        # 3. Entropy regime
        if self.is_enabled("entropy_regime"):
            from strategies.entropy import compute_entropy_regime
            result = compute_entropy_regime(prices)
            math_signals["entropy"] = result
            if "score_delta" in result and "error" not in result:
                total_delta += result["score_delta"]
                regime = result.get("regime", "NORMAL")
                if regime == "ORDERED":
                    indicators.setdefault("signals_fired", []).append("Entropy:ORDERED(+1)")
                elif regime == "CHAOTIC":
                    indicators.setdefault("signals_fired", []).append("Entropy:CHAOTIC(-1)")

        # 4. Lévy jump detection
        if self.is_enabled("levy_jump"):
            from strategies.levy import compute as levy_compute
            result = levy_compute(prices)
            math_signals["levy"] = result
            if "score_delta" in result and "error" not in result:
                total_delta += result["score_delta"]
                if result.get("recent_jump"):
                    indicators.setdefault("signals_fired", []).append("LevyJump:RECENT_JUMP(flag)")
                elif result.get("regime") == "CALM":
                    indicators.setdefault("signals_fired", []).append("LevyJump:CALM(+1)")
                elif result.get("regime") == "JUMP_PRONE":
                    indicators.setdefault("signals_fired", []).append("LevyJump:JUMP_PRONE(-1)")

        # Apply clamped delta to signal_score
        if total_delta != 0:
            current_score = indicators.get("signal_score", 0)
            new_score = int(np.clip(current_score + total_delta, 0, MAX_SIGNAL_SCORE))
            indicators["signal_score"] = new_score

        if math_signals:
            indicators["math_signals"] = math_signals

        return indicators

    # ── Multi-asset portfolio enrichment ──────────────────────────────────────

    def enrich_portfolio(self, watchlist_items: list, ohlcv_cache: dict) -> dict:
        """
        Apply multi-asset strategies across all symbols.

        Args:
            watchlist_items: list of snapshot items (symbol + indicators)
            ohlcv_cache: {symbol: pd.DataFrame} — pre-fetched OHLCV data

        Returns:
            portfolio_math_signals dict to be added to the market snapshot.
        """
        multi_active = any(
            self.is_enabled(k)
            for k in ("transfer_entropy", "rmt_correlation", "wasserstein_regime", "tda_features")
        )
        if not multi_active:
            return {}

        # Build return series from cached OHLCV
        return_series: dict = {}
        for symbol, df in ohlcv_cache.items():
            if df is not None and len(df) >= 25:
                prices = df["close"].values.astype(float)
                log_ret = np.diff(np.log(prices + 1e-10))
                return_series[symbol] = log_ret

        if len(return_series) < 2:
            return {}

        portfolio_signals: dict = {}

        # Transfer Entropy network
        if self.is_enabled("transfer_entropy"):
            try:
                from strategies.entropy import compute_transfer_entropy_network
                result = compute_transfer_entropy_network(return_series)
                portfolio_signals["transfer_entropy"] = result
                logger.debug(f"Transfer entropy leaders: {result.get('leaders', [])}")
            except Exception as e:
                logger.warning(f"Transfer entropy portfolio failed: {e}")

        # RMT Correlation cleaning
        if self.is_enabled("rmt_correlation"):
            try:
                from strategies.rmt import compute as rmt_compute
                result = rmt_compute(return_series)
                portfolio_signals["rmt"] = result
                logger.debug(f"RMT: {result.get('significant_eigenvalues', '?')} significant modes")
            except Exception as e:
                logger.warning(f"RMT portfolio failed: {e}")

        # Wasserstein regime detection
        if self.is_enabled("wasserstein_regime"):
            try:
                from strategies.wasserstein import compute_portfolio_regimes
                result = compute_portfolio_regimes(return_series)
                portfolio_signals["wasserstein"] = result
                logger.debug(f"Wasserstein stability: {result.get('portfolio_stability', '?')}")
            except Exception as e:
                logger.warning(f"Wasserstein portfolio failed: {e}")

        # TDA features — requires ripser
        if self.is_enabled("tda_features"):
            try:
                from strategies.tda import compute as tda_compute
                # Run on BTC as the bellwether
                btc_key = next((k for k in return_series if "BTC" in k), None)
                if btc_key:
                    btc_prices = np.exp(np.cumsum(
                        np.concatenate([[np.log(1.0)], return_series[btc_key]])
                    ))
                    result = tda_compute(btc_prices)
                    portfolio_signals["tda"] = result
                    if result.get("crash_warning"):
                        logger.warning("⚠️ TDA crash warning signal detected")
            except Exception as e:
                logger.warning(f"TDA portfolio failed: {e}")

        return portfolio_signals

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def save_config(strategies_dict: dict, path: Optional[Path] = None) -> None:
        """Write an updated strategy config to disk (called by dashboard)."""
        if path is None:
            path = _LOCAL_STRATEGIES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategies": strategies_dict,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info(f"Math strategies saved to {path}")

    @staticmethod
    def get_defaults() -> dict:
        """Return the default strategy config (all disabled)."""
        return {k: {"enabled": v["enabled"]} for k, v in _STRATEGY_DEFAULTS.items()}

    @staticmethod
    def get_strategy_metadata() -> dict:
        """Return full metadata for dashboard display."""
        return _STRATEGY_DEFAULTS.copy()
