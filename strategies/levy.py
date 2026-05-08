"""
strategies/levy.py
Lévy Processes & Jump Detection.

Theory: Crypto markets have jumps — sudden discontinuous price changes that
Gaussian models miss. BTC averages ~3.5 jumps/day on 1-minute data. At daily
timeframes we see fewer but larger structural jumps.

The Lee-Mykland test identifies jumps as returns that exceed a threshold
set by the bipower variation of the surrounding data. Here we use a simpler
but robust z-score approach calibrated against the local volatility window.

A detected recent jump has two signals:
  - Recent jump: the prior candle was a structural break (flag, don't add score)
  - High jump frequency: the market is in a jump-prone regime (reduce size)
  - Low jump frequency + no recent jump: calm drift (score +1 for clean setup)

No exotic dependencies — uses only numpy and scipy.
"""

import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from scipy.stats import norm
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


def compute(prices: np.ndarray, z_threshold: float = 3.5,
            lookback: int = 30) -> dict:
    """
    Detect jumps in the price series using z-score thresholding.

    A return is classified as a jump if its absolute z-score (relative to the
    rolling window) exceeds z_threshold. This approximates the Lee-Mykland
    non-parametric jump test at daily resolution.

    Returns:
        recent_jump: True if the most recent return is a jump
        jump_frequency: fraction of periods classified as jumps (rolling window)
        jump_intensity: average magnitude of detected jumps
        jump_adjusted_vol: volatility estimate with jumps removed
        regime: JUMP_PRONE | CALM | NORMAL
        score_delta: +1 if CALM (clean drift), -1 if JUMP_PRONE
    """
    if len(prices) < lookback + 2:
        return {"error": "insufficient_data", "score_delta": 0}

    try:
        log_returns = np.diff(np.log(prices))

        window = log_returns[-lookback:]
        mean = np.mean(window)
        std = np.std(window)

        if std < 1e-10:
            return {"error": "zero_volatility", "score_delta": 0}

        z_scores = np.abs((window - mean) / std)
        jump_mask = z_scores > z_threshold

        # Remove jumps and recompute volatility on continuous component
        continuous = window[~jump_mask]
        jump_adjusted_vol = float(np.std(continuous) * np.sqrt(252)) if len(continuous) > 2 else float(std * np.sqrt(252))

        recent_jump = bool(jump_mask[-1])
        jump_frequency = float(np.mean(jump_mask))
        jump_magnitudes = np.abs(window[jump_mask])
        jump_intensity = float(np.mean(jump_magnitudes)) if len(jump_magnitudes) > 0 else 0.0

        if jump_frequency > 0.15:
            regime = "JUMP_PRONE"
            score_delta = -1
        elif jump_frequency < 0.05 and not recent_jump:
            regime = "CALM"
            score_delta = 1
        else:
            regime = "NORMAL"
            score_delta = 0

        return {
            "recent_jump":       recent_jump,
            "jump_frequency":    round(jump_frequency, 3),
            "jump_intensity":    round(jump_intensity, 4),
            "jump_adjusted_vol": round(jump_adjusted_vol, 4),
            "regime":            regime,
            "score_delta":       score_delta,
            "interpretation": (
                "Structural jump in last candle - treat signal with caution"
                if recent_jump else
                "Market in calm drift regime - clean mean-reversion conditions"
                if regime == "CALM" else
                "Elevated jump frequency - jumps contaminating signal"
                if regime == "JUMP_PRONE" else
                "Normal jump environment"
            ),
        }

    except Exception as e:
        logger.warning(f"Lévy jump detection failed: {e}")
        return {"error": str(e), "score_delta": 0}
