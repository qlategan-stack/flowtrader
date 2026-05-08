"""
strategies/
Mathematical strategy engine for FlowTrader.

Seven unconventional signal enrichers drawn from physics, topology, and
information theory. Each is independently togglable via
journal/math_strategies.json so you can A/B test their impact.

Roles in the pipeline:
  - Pre-processing:  wavelet      (cleans noise before indicators)
  - Per-symbol:      hurst, entropy, levy  (run on each symbol's OHLCV)
  - Multi-asset:     transfer_entropy, rmt, wasserstein, tda
                     (run across all crypto symbols together)
"""

from .engine import StrategyEngine

__all__ = ["StrategyEngine"]
