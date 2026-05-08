"""
strategies/wasserstein.py
Optimal Transport — Wasserstein Distance Regime Detection.

Theory: Unlike correlation or variance-based methods, the Wasserstein distance
measures the 'cost' of transforming one probability distribution into another.
It captures distributional shape changes — tails, skew, multimodality — that
mean/variance comparisons completely miss.

Application:
  Compare the current 20-period return distribution to the historical baseline.
  When the Wasserstein distance spikes, the market's distributional character
  has changed — a regime shift, often before it shows in price or volatility.

Uses: scipy.stats.wasserstein_distance (1D Earth Mover's Distance).
No exotic dependencies.
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)

try:
    from scipy.stats import wasserstein_distance
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logger.debug("scipy not installed — Wasserstein strategy unavailable. pip install scipy")


def compute(prices: np.ndarray, recent_window: int = 20,
            historical_window: int = 60) -> dict:
    """
    Detect distribution regime shifts via rolling Wasserstein distance.

    Compares the recent return distribution to the historical baseline.
    A high distance means the market's distributional character has changed.

    Returns:
        wasserstein_distance: Earth mover's distance between distributions
        regime: STABLE | SHIFTING | SHIFTED
        shift_direction: TAIL_EXPANDING | MEAN_SHIFTING | NORMAL
        score_delta: -1 if regime is shifting/shifted, 0 if stable
    """
    if not SCIPY_AVAILABLE:
        return {"error": "scipy_not_installed", "score_delta": 0}

    total_needed = recent_window + historical_window
    if len(prices) < total_needed:
        return {"error": "insufficient_data", "score_delta": 0}

    try:
        log_returns = np.diff(np.log(prices))

        recent_ret = log_returns[-recent_window:]
        hist_ret = log_returns[-(recent_window + historical_window):-recent_window]

        dist = float(wasserstein_distance(hist_ret, recent_ret))

        # Adaptive threshold based on historical volatility
        hist_std = float(np.std(hist_ret))
        threshold_moderate = hist_std * 0.5
        threshold_high = hist_std * 1.0

        # Characterise the shift direction
        recent_mean = np.mean(recent_ret)
        hist_mean = np.mean(hist_ret)
        recent_std = np.std(recent_ret)

        if recent_std > hist_std * 1.5:
            shift_direction = "TAIL_EXPANDING"
        elif abs(recent_mean - hist_mean) > hist_std * 0.5:
            shift_direction = "MEAN_SHIFTING"
        else:
            shift_direction = "NORMAL"

        if dist > threshold_high:
            regime = "SHIFTED"
            score_delta = -1
        elif dist > threshold_moderate:
            regime = "SHIFTING"
            score_delta = -1
        else:
            regime = "STABLE"
            score_delta = 0

        return {
            "wasserstein_distance": round(dist, 6),
            "threshold_moderate":   round(threshold_moderate, 6),
            "threshold_high":       round(threshold_high, 6),
            "regime":               regime,
            "shift_direction":      shift_direction,
            "score_delta":          score_delta,
            "interpretation": (
                "Return distribution stable - mean reversion conditions intact"
                if regime == "STABLE" else
                "Distribution shifting - regime changing, reduce confidence"
                if regime == "SHIFTING" else
                "Distribution has shifted significantly - different market regime"
            ),
        }

    except Exception as e:
        logger.warning(f"Wasserstein computation failed: {e}")
        return {"error": str(e), "score_delta": 0}


def compute_portfolio_regimes(return_series: dict,
                               recent_window: int = 15,
                               historical_window: int = 30) -> dict:
    """
    Compute Wasserstein distances across all symbols and identify which
    are in stable vs shifting regimes.

    Returns:
        stable_symbols: list of symbols with stable distributions
        shifting_symbols: list with regime in transition
        portfolio_stability: fraction of symbols that are stable (0-1)
    """
    if not SCIPY_AVAILABLE:
        return {"error": "scipy_not_installed"}

    results = {}
    for sym, ret in return_series.items():
        # Auto-scale windows to series length so we don't fail on short series.
        avail = len(ret)
        rw = min(recent_window, max(10, avail // 4))
        hw = min(historical_window, max(15, avail - rw - 1))
        if rw + hw + 1 > avail:
            continue
        result = compute(
            np.exp(np.cumsum(np.concatenate([[0], ret]))),
            recent_window=rw,
            historical_window=hw,
        )
        if "error" not in result:
            results[sym] = result

    if not results:
        return {"error": "no_results"}

    stable = [s for s, r in results.items() if r["regime"] == "STABLE"]
    shifting = [s for s, r in results.items() if r["regime"] in ("SHIFTING", "SHIFTED")]
    stability = round(len(stable) / len(results), 2)

    return {
        "stable_symbols":      stable,
        "shifting_symbols":    shifting,
        "portfolio_stability": stability,
        "symbol_regimes":      {s: r["regime"] for s, r in results.items()},
    }
