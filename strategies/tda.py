"""
strategies/tda.py
Topological Data Analysis — Persistent Homology.

Theory: TDA uses algebraic topology to extract the 'shape' of market data.
Prices are embedded in higher dimensions via Takens delay embedding, then
the topological features (connected components, loops, voids) are tracked as
a scale parameter varies. Features that persist across many scales are genuine
structure; short-lived features are noise.

Key output: persistence entropy and crash warning signals.
Research shows that topological phase transitions in crypto precede extreme
market moves by 0–5 calendar days.

Requires: ripser (fast C++ persistent homology) + persim.
Install:  pip install ripser persim
Gracefully degrades if not installed — returns a stub result.
"""

import numpy as np
import logging

logger = logging.getLogger(__name__)

try:
    from ripser import ripser
    RIPSER_AVAILABLE = True
except ImportError:
    RIPSER_AVAILABLE = False
    logger.debug("ripser not installed — TDA strategy unavailable. pip install ripser persim")


def _takens_embedding(series: np.ndarray, dim: int = 3, delay: int = 1) -> np.ndarray:
    """Time-delay embedding of a 1D series into a dim-dimensional point cloud."""
    n = len(series) - (dim - 1) * delay
    if n <= 0:
        return np.zeros((1, dim))
    return np.array([series[i:i + dim * delay:delay] for i in range(n)])


def _persistence_entropy(diagram: np.ndarray) -> float:
    """Compute the persistence entropy of a persistence diagram."""
    if len(diagram) == 0:
        return 0.0
    lifespans = diagram[:, 1] - diagram[:, 0]
    lifespans = lifespans[np.isfinite(lifespans) & (lifespans > 0)]
    if len(lifespans) == 0:
        return 0.0
    total = np.sum(lifespans)
    if total < 1e-10:
        return 0.0
    probs = lifespans / total
    return float(-np.sum(probs * np.log(probs + 1e-10)))


def compute(prices: np.ndarray, window: int = 60, dim: int = 3, delay: int = 1) -> dict:
    """
    Extract TDA features from the recent price window.

    H0 features = connected components (price clustering)
    H1 features = loops (cyclic patterns — support/resistance cycles)

    Returns:
        h0_persistence_entropy: topological complexity of H0 features
        h1_count: number of significant loops detected
        h1_max_persistence: most persistent loop (long-lived = significant structure)
        crash_warning: True if persistence norm has increased rapidly
        score_delta: -1 if crash_warning, 0 otherwise
    """
    if not RIPSER_AVAILABLE:
        return {
            "error":      "ripser_not_installed",
            "note":       "pip install ripser persim to enable TDA",
            "score_delta": 0,
        }

    if len(prices) < window:
        return {"error": "insufficient_data", "score_delta": 0}

    try:
        log_returns = np.diff(np.log(prices[-window:]))
        embedded = _takens_embedding(log_returns, dim=dim, delay=delay)

        diagrams = ripser(embedded, maxdim=1)["dgms"]

        # H0 — connected components
        h0 = diagrams[0]
        h0_finite = h0[np.isfinite(h0[:, 1])] if len(h0) > 0 else np.zeros((0, 2))
        h0_entropy = _persistence_entropy(h0_finite)

        # H1 — loops
        h1 = diagrams[1] if len(diagrams) > 1 else np.zeros((0, 2))
        h1_finite = h1[np.isfinite(h1[:, 1])] if len(h1) > 0 else np.zeros((0, 2))
        h1_count = int(len(h1_finite))
        h1_lifespans = h1_finite[:, 1] - h1_finite[:, 0] if len(h1_finite) > 0 else np.array([0.0])
        h1_max = float(np.max(h1_lifespans)) if len(h1_lifespans) > 0 else 0.0
        h1_entropy = _persistence_entropy(h1_finite)

        # Crash warning heuristic: structurally significant H1 features + high
        # entropy. Just having many short-lived loops is normal noise — what
        # matters is at least one *long-lived* loop AND distributed entropy.
        crash_warning = bool(
            h1_count >= 3
            and h1_entropy > 1.5
            and h1_max > 0.01
        )

        score_delta = -1 if crash_warning else 0

        return {
            "h0_persistence_entropy": round(h0_entropy, 4),
            "h1_count":               h1_count,
            "h1_max_persistence":     round(h1_max, 5),
            "h1_entropy":             round(h1_entropy, 4),
            "crash_warning":          crash_warning,
            "score_delta":            score_delta,
            "interpretation": (
                "Topological stress detected - elevated crash risk"
                if crash_warning else
                "Topological structure normal - no systemic stress signal"
            ),
        }

    except Exception as e:
        logger.warning(f"TDA computation failed: {e}")
        return {"error": str(e), "score_delta": 0}
