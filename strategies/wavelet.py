"""
strategies/wavelet.py
Wavelet Transform & Multi-Resolution Analysis.

Theory: DWT decomposes prices into trend (low-freq) and noise (high-freq)
components simultaneously. By thresholding the detail coefficients we remove
noise and expose the underlying signal. The energy ratio tells us which
frequency band dominates — if trend energy dominates, the signal is clean;
if noise energy dominates, the market is choppy.

Requires: pywt (PyWavelets). Gracefully degrades if not installed.
Install:  pip install PyWavelets
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)

try:
    import pywt
    PYWT_AVAILABLE = True
except ImportError:
    PYWT_AVAILABLE = False
    logger.debug("PyWavelets not installed — wavelet strategy will be skipped. pip install PyWavelets")


def compute(prices: np.ndarray, wavelet: str = "db8", level: int = 3) -> dict:
    """
    Wavelet denoising and frequency-energy analysis.

    Returns:
        denoised_price: The noise-stripped current price estimate
        noise_ratio: Fraction of price variance that is high-freq noise (0–1)
        frequency_regime: TREND_DOMINANT | NOISE_DOMINANT | BALANCED
        score_delta: +1 if trend energy dominates (clean signal), 0 otherwise
    """
    if not PYWT_AVAILABLE:
        return {"error": "pywt_not_installed", "score_delta": 0}

    if len(prices) < 2 ** (level + 1) + 2:
        return {"error": "insufficient_data", "score_delta": 0}

    try:
        log_prices = np.log(prices)

        # Clamp level to the maximum supported by the series length to avoid
        # the "Level value too high — boundary effects" warning from PyWavelets.
        max_level = pywt.dwt_max_level(len(log_prices), wavelet)
        effective_level = min(level, max_level)
        if effective_level < 1:
            return {"error": "series_too_short_for_wavelet", "score_delta": 0}

        # Decompose
        coeffs = pywt.wavedec(log_prices, wavelet, level=effective_level)

        # Estimate noise std via median absolute deviation on finest detail level
        sigma = np.median(np.abs(coeffs[-1])) / 0.6745
        threshold = sigma * np.sqrt(2 * np.log(len(log_prices)))

        # Threshold detail coefficients — keep approximation unchanged
        denoised_coeffs = [coeffs[0]]
        for c in coeffs[1:]:
            denoised_coeffs.append(pywt.threshold(c, threshold, mode="soft"))

        # Reconstruct
        denoised_log = pywt.waverec(denoised_coeffs, wavelet)[:len(prices)]
        denoised_price = float(np.exp(denoised_log[-1]))

        # Frequency energy ratio (trend vs noise)
        trend_energy = float(np.sum(coeffs[0] ** 2))
        detail_energy = sum(float(np.sum(c ** 2)) for c in coeffs[1:])
        total_energy = trend_energy + detail_energy + 1e-10
        noise_ratio = round(detail_energy / total_energy, 3)
        trend_ratio = round(trend_energy / total_energy, 3)

        if noise_ratio < 0.35:
            frequency_regime = "TREND_DOMINANT"
            score_delta = 1
        elif noise_ratio > 0.65:
            frequency_regime = "NOISE_DOMINANT"
            score_delta = -1
        else:
            frequency_regime = "BALANCED"
            score_delta = 0

        current_price = float(prices[-1])
        denoised_gap_pct = round((denoised_price - current_price) / current_price * 100, 2)

        return {
            "denoised_price": round(denoised_price, 6),
            "current_price":  round(current_price, 6),
            "denoised_gap_pct": denoised_gap_pct,
            "noise_ratio":    noise_ratio,
            "trend_ratio":    trend_ratio,
            "frequency_regime": frequency_regime,
            "score_delta":    score_delta,
        }

    except Exception as e:
        logger.warning(f"Wavelet computation failed: {e}")
        return {"error": str(e), "score_delta": 0}
