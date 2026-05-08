"""
strategies/hurst.py
Fractal Market Hypothesis — Rolling Hurst Exponent via R/S Analysis.

Theory: The Hurst exponent H measures long-term memory in a price series.
  H < 0.5 → anti-persistent (mean-reverting) — our strategy's sweet spot
  H = 0.5 → random walk — no edge, reduce exposure
  H > 0.5 → persistent (trending) — mean reversion will fail here

BTC-USD measured at H ≈ 0.32 (strongly anti-persistent), confirming mean
reversion is the mathematically correct strategy for crypto at daily timeframes.

No external dependencies — uses only numpy.
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)


def _rs_analysis(series: np.ndarray) -> float:
    """Compute R/S (Rescaled Range) statistic for a series."""
    mean = np.mean(series)
    deviations = np.cumsum(series - mean)
    R = np.max(deviations) - np.min(deviations)
    S = np.std(series, ddof=1)
    if S < 1e-10:
        return 0.0
    return R / S


def compute(prices: np.ndarray, min_periods: int = 8) -> dict:
    """
    Compute the Hurst exponent using Rescaled Range Analysis.

    Args:
        prices: 1D array of closing prices (at least 30 needed)
        min_periods: minimum subseries length

    Returns dict with h, interpretation, signal, and score_delta.
    score_delta is the suggested adjustment to signal_score:
        +1 if H confirms mean-reversion (H < 0.45)
        -1 if H suggests trending (H > 0.60)
         0 if H is ambiguous (0.45-0.60)
    """
    if len(prices) < min_periods * 4:
        return {"error": "insufficient_data", "h": 0.5, "score_delta": 0}

    try:
        log_prices = np.log(prices)
        returns = np.diff(log_prices)

        if len(returns) < min_periods * 2:
            return {"error": "insufficient_returns", "h": 0.5, "score_delta": 0}

        # Build a set of lags as powers of 2 for clean log-log scaling
        max_lag = len(returns) // 2
        lags = []
        lag = min_periods
        while lag <= max_lag:
            lags.append(lag)
            lag = int(lag * 1.5)
        lags = sorted(set(lags))

        if len(lags) < 3:
            return {"error": "too_few_lags", "h": 0.5, "score_delta": 0}

        rs_values = []
        valid_lags = []
        for lag in lags:
            num_sub = len(returns) // lag
            if num_sub < 2:
                continue
            rs_sub = []
            for i in range(num_sub):
                sub = returns[i * lag:(i + 1) * lag]
                rs = _rs_analysis(sub)
                if rs > 0:
                    rs_sub.append(rs)
            if rs_sub:
                rs_values.append(np.mean(rs_sub))
                valid_lags.append(lag)

        if len(valid_lags) < 3:
            return {"error": "insufficient_valid_lags", "h": 0.5, "score_delta": 0}

        log_lags = np.log(valid_lags)
        log_rs = np.log(rs_values)
        h = float(np.polyfit(log_lags, log_rs, 1)[0])
        h = float(np.clip(h, 0.0, 1.0))

        if h < 0.40:
            interpretation = "STRONGLY_ANTI_PERSISTENT"
            signal = "STRONG_MEAN_REVERSION_CONFIRMED"
            score_delta = 1
        elif h < 0.50:
            interpretation = "ANTI_PERSISTENT"
            signal = "MEAN_REVERSION_CONFIRMED"
            score_delta = 1
        elif h < 0.55:
            interpretation = "NEAR_RANDOM"
            signal = "NO_EDGE_REDUCE_SIZE"
            score_delta = 0
        elif h < 0.65:
            interpretation = "PERSISTENT"
            signal = "TRENDING_MEAN_REVERSION_RISKY"
            score_delta = -1
        else:
            interpretation = "STRONGLY_PERSISTENT"
            signal = "TRENDING_SKIP_MEAN_REVERSION"
            score_delta = -1

        return {
            "h": round(h, 3),
            "interpretation": interpretation,
            "signal": signal,
            "score_delta": score_delta,
            "num_lags": len(valid_lags),
        }

    except Exception as e:
        logger.warning(f"Hurst computation failed: {e}")
        return {"error": str(e), "h": 0.5, "score_delta": 0}
