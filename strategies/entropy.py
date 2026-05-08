"""
strategies/entropy.py
Information Theory — Shannon Entropy Regime Detection & Transfer Entropy.

Shannon Entropy:
  High entropy = disordered/random market = unpredictable, reduce exposure
  Low entropy  = ordered market = structured behaviour, potential breakout or trend

Transfer Entropy (TE):
  TE(X→Y) = how much knowing X's past reduces uncertainty about Y's future.
  Applied across crypto pairs to find which coins lead information flow.
  Net exporters of information tend to lead price movements.

Uses only scipy + numpy. No exotic dependencies.
"""

import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from scipy.stats import entropy as scipy_entropy
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logger.debug("scipy not installed — entropy strategies degraded. pip install scipy")


def _shannon_entropy(returns: np.ndarray, bins: int = 20) -> float:
    """Compute Shannon entropy of the return distribution."""
    counts, _ = np.histogram(returns, bins=bins)
    probs = counts / (counts.sum() + 1e-10)
    probs = probs[probs > 0]
    return float(-np.sum(probs * np.log(probs + 1e-10)))


def compute_entropy_regime(prices: np.ndarray, window: int = 30, bins: int = 20) -> dict:
    """
    Compute rolling Shannon entropy regime indicator.

    Low entropy in the most recent window compared to historical average
    signals that the market has become 'ordered' — often preceding a breakout
    or directional move. High entropy signals chaos/randomness.

    Returns:
        entropy_current: entropy of recent returns
        entropy_historical: baseline entropy
        entropy_ratio: current / historical (< 1 = more ordered than usual)
        regime: ORDERED | NORMAL | CHAOTIC
        score_delta: +1 if ordered (possible setup), -1 if chaotic
    """
    if len(prices) < window + 10:
        return {"error": "insufficient_data", "score_delta": 0}

    try:
        log_returns = np.diff(np.log(prices))

        recent = log_returns[-window:]
        historical = log_returns[:-window] if len(log_returns) > window else log_returns

        h_recent = _shannon_entropy(recent, bins=bins)
        h_hist = _shannon_entropy(historical, bins=bins)

        ratio = h_recent / (h_hist + 1e-10)

        if ratio < 0.70:
            regime = "ORDERED"
            score_delta = 1
        elif ratio > 1.30:
            regime = "CHAOTIC"
            score_delta = -1
        else:
            regime = "NORMAL"
            score_delta = 0

        return {
            "entropy_current":    round(h_recent, 4),
            "entropy_historical": round(h_hist, 4),
            "entropy_ratio":      round(ratio, 3),
            "regime":             regime,
            "score_delta":        score_delta,
            "interpretation":     (
                "Market more ordered than usual - watch for directional move"
                if regime == "ORDERED" else
                "Market more random than usual - reduce exposure"
                if regime == "CHAOTIC" else
                "Market entropy within normal range"
            ),
        }

    except Exception as e:
        logger.warning(f"Entropy regime computation failed: {e}")
        return {"error": str(e), "score_delta": 0}


def compute_transfer_entropy(source: np.ndarray, target: np.ndarray,
                              k: int = 1, bins: int = 10) -> float:
    """
    Compute Transfer Entropy TE(source → target).
    TE = H(Y_fut|Y_past) - H(Y_fut|Y_past, X_past)
       = H(Y_fut, Y_past) + H(Y_past, X_past) - H(Y_past) - H(Y_fut, Y_past, X_past)

    Higher value = more information flows from source to target.
    """
    n = min(len(source), len(target)) - k
    if n < 20:
        return 0.0

    try:
        # Discretise both series into bins
        def discretise(arr: np.ndarray) -> np.ndarray:
            edges = np.linspace(arr.min() - 1e-9, arr.max() + 1e-9, bins + 1)
            return np.digitize(arr, edges) - 1

        src = discretise(source[-n-k:])
        tgt = discretise(target[-n-k:])

        y_fut  = tgt[k:]
        y_past = tgt[:-k]
        x_past = src[:-k]

        def joint_entropy(*arrs) -> float:
            combined = np.stack(arrs, axis=1)
            _, counts = np.unique(combined, axis=0, return_counts=True)
            probs = counts / counts.sum()
            return float(-np.sum(probs * np.log(probs + 1e-10)))

        te = (joint_entropy(y_fut, y_past)
              + joint_entropy(y_past, x_past)
              - joint_entropy(y_past)
              - joint_entropy(y_fut, y_past, x_past))

        return max(float(te), 0.0)

    except Exception as e:
        logger.warning(f"Transfer entropy failed: {e}")
        return 0.0


def compute_transfer_entropy_network(return_series: dict) -> dict:
    """
    Compute the Transfer Entropy network across all crypto symbols.

    Args:
        return_series: {symbol: np.ndarray of log returns}

    Returns:
        te_matrix: dict of {(source, target): te_value}
        leaders: symbols that export more information than they receive
        followers: symbols that receive more than they export
    """
    symbols = list(return_series.keys())
    if len(symbols) < 2:
        return {"error": "need_at_least_2_symbols", "leaders": [], "followers": []}

    try:
        te_matrix: dict = {}
        net_flow: dict = {s: 0.0 for s in symbols}

        for i, src in enumerate(symbols):
            for j, tgt in enumerate(symbols):
                if i == j:
                    continue
                src_ret = return_series[src]
                tgt_ret = return_series[tgt]
                min_len = min(len(src_ret), len(tgt_ret))
                if min_len < 20:
                    continue
                te = compute_transfer_entropy(src_ret[-min_len:], tgt_ret[-min_len:])
                te_matrix[(src, tgt)] = round(te, 5)
                net_flow[src] += te
                net_flow[tgt] -= te

        sorted_by_flow = sorted(net_flow.items(), key=lambda x: x[1], reverse=True)
        leaders = [s for s, f in sorted_by_flow if f > 0]
        followers = [s for s, f in sorted_by_flow if f < 0]

        return {
            "leaders":   leaders,
            "followers": followers,
            "net_flow":  {s: round(f, 4) for s, f in net_flow.items()},
        }

    except Exception as e:
        logger.warning(f"Transfer entropy network failed: {e}")
        return {"error": str(e), "leaders": [], "followers": []}
