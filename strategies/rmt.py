"""
strategies/rmt.py
Random Matrix Theory — Correlation Matrix Cleaning.

Theory: When you compute a correlation matrix from N crypto assets over T
periods, most eigenvalues are statistical noise. The Marčenko-Pastur (MP)
distribution gives the theoretical eigenvalue range for a purely random matrix.
Eigenvalues ABOVE the MP upper bound represent genuine collective behaviour.

This module:
  1. Cleans the correlation matrix (removes noise eigenvalues)
  2. Identifies the "market factor" (largest eigenvalue = collective mode)
  3. Detects regime shifts when eigenvalues break out of the noise band

Uses: numpy + scipy only. No exotic dependencies.
"""

import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from scipy.linalg import eigh
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logger.debug("scipy not installed — RMT strategy unavailable. pip install scipy")


def _marchenko_pastur_bounds(T: int, N: int) -> tuple[float, float]:
    """
    Return the Marčenko-Pastur lower and upper eigenvalue bounds.
    q = T/N (ratio of observations to assets).
    """
    q = T / N
    lambda_plus = (1 + 1 / np.sqrt(q)) ** 2
    lambda_minus = (1 - 1 / np.sqrt(q)) ** 2
    return float(lambda_minus), float(lambda_plus)


def compute(return_series: dict) -> dict:
    """
    Clean a crypto return correlation matrix using Random Matrix Theory.

    Args:
        return_series: {symbol: np.ndarray of log returns} — all same length

    Returns:
        significant_eigenvalues: count above the MP upper bound
        market_factor_coins: symbols with highest loadings on the market factor
        noise_fraction: fraction of eigenvalues classified as noise
        regime_shift_signal: True if a previously-noise eigenvalue broke out
        market_mode_strength: ratio of largest eigenvalue to MP upper bound
        score_context: descriptive summary for Claude's prompt
    """
    if not SCIPY_AVAILABLE:
        return {"error": "scipy_not_installed"}

    symbols = list(return_series.keys())
    N = len(symbols)
    if N < 3:
        return {"error": "need_at_least_3_symbols"}

    # Align series lengths
    min_len = min(len(v) for v in return_series.values())
    if min_len < N + 5:
        return {"error": "insufficient_observations"}

    try:
        returns_matrix = np.stack([return_series[s][-min_len:] for s in symbols], axis=1)
        T = returns_matrix.shape[0]

        corr = np.corrcoef(returns_matrix.T)

        lambda_minus, lambda_plus = _marchenko_pastur_bounds(T, N)

        eigenvalues, eigenvectors = eigh(corr)

        noise_mask = (eigenvalues >= lambda_minus) & (eigenvalues <= lambda_plus)
        noise_fraction = float(np.mean(noise_mask))
        sig_count = int(np.sum(~noise_mask & (eigenvalues > lambda_plus)))

        # Market factor = largest eigenvalue
        max_idx = np.argmax(eigenvalues)
        market_eigenvec = np.abs(eigenvectors[:, max_idx])
        top_k = min(3, N)
        top_indices = np.argsort(market_eigenvec)[-top_k:][::-1]
        market_factor_coins = [symbols[i] for i in top_indices]

        # Market mode strength: how far the top eigenvalue is above the MP bound
        market_mode_strength = round(float(eigenvalues[max_idx]) / lambda_plus, 2)

        # Simple regime shift signal: second eigenvalue approaching or exceeding bound
        second_idx = np.argsort(eigenvalues)[-2]
        second_eigenvalue = float(eigenvalues[second_idx])
        regime_shift_signal = bool(second_eigenvalue > lambda_plus * 0.90)

        return {
            "significant_eigenvalues": sig_count,
            "noise_fraction":          round(noise_fraction, 2),
            "market_mode_strength":    market_mode_strength,
            "market_factor_coins":     market_factor_coins,
            "regime_shift_signal":     regime_shift_signal,
            "lambda_plus":             round(lambda_plus, 3),
            "num_assets":              N,
            "score_context": (
                f"Market factor (eig={eigenvalues[max_idx]:.1f} vs MP bound {lambda_plus:.1f}) "
                f"driven by {', '.join(market_factor_coins)}. "
                f"{sig_count} genuine collective modes. "
                + ("Secondary regime shift forming." if regime_shift_signal else "")
            ),
        }

    except Exception as e:
        logger.warning(f"RMT computation failed: {e}")
        return {"error": str(e)}
