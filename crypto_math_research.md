# Unconventional Mathematical Approaches to Cryptocurrency Price Prediction

## A Deep Research Report for Trading Bot Enhancement

**Prepared for:** Quintus Lategan / Flowmatic Automation  
**Date:** 7 May 2026  
**Focus:** Pure mathematics and physics-derived methods — beyond standard ML/TA

---

## Executive Summary

The mainstream crypto prediction space is saturated with LSTM, GRU, XGBoost, and standard technical indicators. This report covers **seven unconventional mathematical domains** drawn from physics, topology, information theory, and pure mathematics that offer genuinely different perspectives on crypto price dynamics. Each section covers the theory, how it applies to crypto, what it can do for your trading bot, and practical implementation guidance.

---

## 1. Fractal Market Hypothesis & the Hurst Exponent

### The Theory

The Fractal Market Hypothesis (FMH) is a direct alternative to the Efficient Market Hypothesis. Where EMH assumes prices follow a random walk (Brownian motion), FMH treats price series as **self-affine fractal stochastic fields** — patterns that repeat across different time scales.

The key metrics are:

- **Hurst Exponent (H):** Measures long-term memory in the series.
  - H > 0.5 → persistent (trending) behaviour — momentum trades work
  - H = 0.5 → pure random walk — no predictive edge
  - H < 0.5 → anti-persistent (mean-reverting) behaviour — reversal trades work
- **Fractal Dimension (D_F):** Quantifies the complexity and self-similarity of the price field. Related to H by: D_F ≈ 2 - H
- **Lyapunov Exponent (λ):** Measures the rate of divergence of nearby trajectories — essentially how chaotic the system is and how far ahead prediction is even theoretically possible.

### What the Research Shows

A 2025 paper applying the FMH to BTC-USD found a Hurst exponent of approximately **0.32**, a fractal dimension of about **1.68**, and a Lévy index near **1.22**. This confirms that Bitcoin is **anti-persistent** — it does not conform to Brownian motion and has a tendency to reverse rather than trend at certain scales.

A separate study found BTC price and return series show "low chaos, high randomness, and low predictability" (Hurst around 0.5–0.6 for price returns), but **BTC volumes show strong fractal behaviour** with Hurst values above 0.8 — meaning volume is far more predictable than price.

### Trading Bot Application

1. **Rolling Hurst Exponent** (calculated over sliding windows) as a regime indicator:
   - H rising above 0.5 → switch to momentum/trend-following strategy
   - H falling below 0.5 → switch to mean-reversion strategy
   - H ≈ 0.5 → reduce position size, market is random
2. **Beta-to-Volatility and Lyapunov-to-Volatility ratios:** Changes in the polarity of these ratios signal impending trend reversals. One research team used these to predict BTC trend changes and achieved up to ~10% gains from "micro-trends" that standard methods miss.
3. **Volume Hurst as a leading indicator:** Since volume shows stronger fractal memory than price, use volume's Hurst exponent to predict upcoming volatility regime shifts.

### Implementation

- **Detrended Fluctuation Analysis (DFA):** The most robust method for computing H. Python package `nolds` provides this.
- **MFDFA (Multifractal DFA):** Extends to multiple fractal dimensions — crypto markets are multifractal, meaning different parts of the return distribution scale differently.
- **R/S Analysis (Rescaled Range):** Classic method, simpler but less robust to non-stationarity.

```python
# Conceptual: Rolling Hurst via DFA
import nolds
import numpy as np

def rolling_hurst(prices, window=256):
    """Compute rolling Hurst exponent using DFA"""
    h_values = []
    for i in range(window, len(prices)):
        segment = np.log(prices[i-window:i])  # log prices
        returns = np.diff(segment)
        h = nolds.dfa(returns)
        h_values.append(h)
    return np.array(h_values)
```

---

## 2. Topological Data Analysis (TDA) & Persistent Homology

### The Theory

This is perhaps the most mathematically exotic approach. TDA uses **persistent homology** — a tool from algebraic topology — to extract the "shape" of data. Instead of looking at individual price values, it examines the **topological structure** of the price manifold: connected components, loops, voids, and higher-dimensional holes that form and disappear as you vary a scale parameter.

The output is a **persistence diagram** — a set of (birth, death) points representing when topological features appear and vanish. Features that persist across many scales are considered genuine structure; short-lived features are noise.

### What the Research Shows

Multiple studies confirm that TDA provides genuine early-warning signals for crypto:

- **Crash prediction:** Persistent homology applied to BTC data from 2016–2018 detected strong warning signs before crashes using clustering on topological features. Fuzzy c-means and k-medoids clustering on persistence features performed best.
- **Cross-market early warning:** A study using TDA on the top 10 cryptocurrencies found that topological phase transitions in the crypto system precede extreme fluctuations in the U.S. stock market by **0–5 calendar days**. The crypto market's topological structure serves as a leading indicator for traditional markets.
- **Enhanced forecasting:** Research from early 2025 showed that adding TDA-derived features (entropy, amplitude, number of points from persistence diagrams) to baseline forecasting models improved prediction accuracy by providing supplementary information that existing models overlook.
- **Blockchain graph analysis (ChainNet):** A novel approach that applies persistent homology directly to the blockchain transaction graph. They introduced the concept of a **"Betti derivative"** — the rate of change of topological features in the blockchain network — as a predictor of price movements.

### Trading Bot Application

1. **TDA-based regime detection:** Compute persistence diagrams on rolling windows of multi-crypto return data. Track the total persistence (sum of all feature lifespans) — sharp changes indicate regime transitions.
2. **Crash early warning:** Monitor the L1 and L2 "persistence norms" across the crypto system. A rapid increase in these norms signals building systemic stress.
3. **Feature augmentation:** Extract TDA features (persistence entropy, number of significant features, max persistence) and feed them as additional inputs to your existing prediction model.

### Implementation

- **Python libraries:** `ripser` (fast persistent homology), `persim` (persistence diagram utilities), `giotto-tda` (full ML-TDA pipeline)
- **Key workflow:** Time-delay embedding → Vietoris-Rips filtration → Persistence computation → Feature extraction

```python
# Conceptual: TDA feature extraction for crypto
from ripser import ripser
from persim import plot_diagrams
import numpy as np

def takens_embedding(series, dim=3, delay=1):
    """Time-delay embedding of a 1D series into higher dimensions"""
    n = len(series) - (dim-1)*delay
    return np.array([series[i:i+dim*delay:delay] for i in range(n)])

def tda_features(prices, window=100, dim=3, delay=1):
    """Extract TDA features from a price window"""
    log_returns = np.diff(np.log(prices[-window:]))
    embedded = takens_embedding(log_returns, dim=dim, delay=delay)
    diagrams = ripser(embedded, maxdim=1)['dgms']
    
    # H0 features (connected components)
    h0 = diagrams[0][:-1]  # remove infinite feature
    # H1 features (loops)
    h1 = diagrams[1] if len(diagrams) > 1 else np.array([])
    
    features = {
        'h0_max_persistence': np.max(h0[:,1] - h0[:,0]) if len(h0) > 0 else 0,
        'h0_mean_persistence': np.mean(h0[:,1] - h0[:,0]) if len(h0) > 0 else 0,
        'h1_count': len(h1),
        'h1_max_persistence': np.max(h1[:,1] - h1[:,0]) if len(h1) > 0 else 0,
        'persistence_entropy': -np.sum(p * np.log(p+1e-10) 
            for p in (h0[:,1]-h0[:,0])/np.sum(h0[:,1]-h0[:,0]+1e-10)) if len(h0) > 0 else 0,
    }
    return features
```

---

## 3. Information Theory: Entropy & Transfer Entropy

### The Theory

Information theory provides tools that capture **non-linear dependencies** that correlation and standard statistics miss entirely:

- **Shannon Entropy:** Measures the uncertainty/information content of a return distribution. High entropy = unpredictable; low entropy = structured.
- **Tsallis Entropy (q-entropy):** A generalisation of Shannon entropy with a parameter q that controls sensitivity to tail events. For q > 1, it penalises concentration and is more robust to fat tails — exactly what crypto needs. For q < 1, it emphasises rare events.
- **Rényi Entropy:** Another generalisation, parameterised by α. By choosing α, you can control which part of the distribution you're measuring information from.
- **Transfer Entropy (TE):** Measures the **directional information flow** from one time series to another — essentially a non-linear, model-free version of Granger causality. It quantifies how much knowing the past of series X reduces uncertainty about the future of series Y.
- **Rényi Transfer Entropy:** Combines Rényi entropy with transfer entropy. By tuning α, you can specifically measure information flow during **extreme tail events** (black swans). For α → 0, the measure is dominated by the most extreme events.

### What the Research Shows

- Shannon and Tsallis entropy-based portfolio optimisation **outperforms mean-variance (Markowitz) methods** in crypto markets, especially during extreme market conditions. Tsallis entropy with q=2 provides the strongest protection against tail risk.
- Rényi transfer entropy with appropriate α can detect **lead-lag causal relationships** between cryptocurrencies that are invisible to linear Granger causality — particularly useful for identifying which coins lead market movements during stress periods.
- Fisher Information (the dual of Shannon entropy) provides complementary insights — it increases as Shannon entropy decreases, giving a different view of the same underlying structure.

### Trading Bot Application

1. **Entropy regime indicator:** Compute rolling Shannon entropy of returns. Low entropy periods (the market is "ordered") often precede breakouts. High entropy (chaos) suggests reducing exposure.
2. **Transfer entropy network:** Build a directed graph of information flow between the cryptos you trade. Coins that are net information exporters tend to lead price movements.
3. **Rényi TE for tail-event prediction:** Use α < 1 to measure information flow specifically during extreme events. If tail-event information flow from BTC to your target coin is increasing, prepare for a large correlated move.
4. **Tsallis-optimal position sizing:** Replace variance-based risk measures with Tsallis entropy for position sizing — it naturally accounts for fat tails.

### Implementation

```python
# Conceptual: Transfer Entropy between crypto pairs
from scipy.stats import entropy
import numpy as np

def transfer_entropy(source, target, k=1, l=1, bins=16):
    """
    Estimate transfer entropy from source to target.
    TE(X→Y) = H(Y_future | Y_past) - H(Y_future | Y_past, X_past)
    """
    # Discretize
    source_d = np.digitize(source, np.linspace(source.min(), source.max(), bins))
    target_d = np.digitize(target, np.linspace(target.min(), target.max(), bins))
    
    n = len(source_d) - max(k, l)
    
    # Build joint distributions
    # Y_future, Y_past, X_past
    y_fut = target_d[max(k,l):]
    y_past = target_d[max(k,l)-l:len(target_d)-l] if l > 0 else np.zeros(n)
    x_past = source_d[max(k,l)-k:len(source_d)-k] if k > 0 else np.zeros(n)
    
    # Compute conditional entropies via joint entropy decomposition
    # TE = H(Y_fut, Y_past) + H(Y_past, X_past) - H(Y_past) - H(Y_fut, Y_past, X_past)
    
    def joint_entropy(*arrays):
        combined = np.column_stack(arrays)
        _, counts = np.unique(combined, axis=0, return_counts=True)
        probs = counts / counts.sum()
        return entropy(probs)
    
    te = (joint_entropy(y_fut, y_past) + joint_entropy(y_past, x_past)
          - joint_entropy(y_past) - joint_entropy(y_fut, y_past, x_past))
    
    return max(te, 0)  # TE should be non-negative for Shannon
```

---

## 4. Wavelet Transform & Multi-Resolution Analysis

### The Theory

Wavelet transforms decompose a signal into components at **different frequency scales simultaneously** — unlike Fourier transforms which lose temporal localisation. For crypto, this means separating:

- **Low-frequency components:** Long-term trends (macro sentiment, halving cycles)
- **Mid-frequency components:** Medium-term patterns (weekly/monthly cycles)
- **High-frequency components:** Short-term noise and micro-structure

The Discrete Wavelet Transform (DWT) produces **approximation coefficients** (low-frequency trend) and **detail coefficients** (high-frequency fluctuations) at each level of decomposition.

### What the Research Shows

- **Wavelet denoising + LSTM** significantly outperforms standard LSTM for both Bitcoin and gold price prediction. The wavelet removes high-frequency noise before the model trains on the cleaner signal.
- **DecoKAN (2025):** A cutting-edge framework combining multi-level DWT with Kolmogorov-Arnold Networks (KANs). It decomposes crypto series into frequency components, processes each with a KAN mixer (which learns interpretable spline-based functions), and produces **symbolic analytical expressions** of the patterns it finds. This means the model can tell you *why* it's predicting what it is, in equation form.
- **Hankel matrix denoising** outperforms wavelet denoising for volatility prediction in crypto — it better preserves systemic impulses while removing noise.
- **Critical caveat:** A rigorous 2026 study found that at the **1-day horizon**, even a sophisticated 2.1M-parameter wavelet-transformer model **fails to beat a naive persistence baseline** (just predicting tomorrow = today). The wavelet-transformer required 3,909x more computation for statistically worse results at short horizons. This suggests wavelet methods are better for **medium-to-long horizon** predictions and for **feature preprocessing** rather than direct short-term prediction.

### Trading Bot Application

1. **Wavelet denoising as preprocessing:** Apply DWT, threshold the detail coefficients to remove noise, reconstruct the denoised signal, then feed it to your prediction model. This alone can improve accuracy.
2. **Multi-resolution strategy selection:** Use wavelet decomposition to identify which frequency band is currently dominant. If low-frequency (trend) energy is high, use trend-following. If high-frequency energy dominates, the market is choppy — reduce exposure or use mean-reversion.
3. **Wavelet coherence between pairs:** Continuous Wavelet Transform (CWT) coherence analysis reveals time-varying correlations between assets at different time scales — useful for pairs trading and hedging.

### Implementation

```python
# Conceptual: Wavelet denoising for crypto signals
import pywt
import numpy as np

def wavelet_denoise(prices, wavelet='db8', level=4, threshold_mode='soft'):
    """Multi-level wavelet denoising of price series"""
    log_prices = np.log(prices)
    
    # Decompose
    coeffs = pywt.wavedec(log_prices, wavelet, level=level)
    
    # Threshold detail coefficients (not approximation)
    sigma = np.median(np.abs(coeffs[-1])) / 0.6745  # MAD estimator
    threshold = sigma * np.sqrt(2 * np.log(len(log_prices)))
    
    denoised_coeffs = [coeffs[0]]  # keep approximation
    for c in coeffs[1:]:
        denoised_coeffs.append(pywt.threshold(c, threshold, mode=threshold_mode))
    
    # Reconstruct
    denoised = pywt.waverec(denoised_coeffs, wavelet)
    return np.exp(denoised[:len(prices)])

def frequency_energy_ratio(prices, wavelet='db8', level=4):
    """Ratio of trend energy to noise energy"""
    coeffs = pywt.wavedec(np.log(prices), wavelet, level=level)
    trend_energy = np.sum(coeffs[0]**2)
    detail_energy = sum(np.sum(c**2) for c in coeffs[1:])
    return trend_energy / (detail_energy + 1e-10)
```

---

## 5. Random Matrix Theory (RMT) & Spectral Analysis

### The Theory

RMT, originally from nuclear physics, provides tools to separate **genuine correlation structure** from **noise** in large correlation matrices. When you compute a correlation matrix from N crypto assets over T time periods, much of what you see is statistical noise. RMT tells you exactly how much.

The key tool is the **Marčenko-Pastur distribution** — the theoretical eigenvalue distribution of a random correlation matrix (Wishart matrix). Eigenvalues that fall within this distribution are noise. Eigenvalues that exceed the Marčenko-Pastur upper bound represent **genuine collective market behaviour**.

### What the Research Shows

- Applied to crypto correlation matrices, most eigenvalues fall outside the random prediction — crypto markets show much more collective structure than traditional markets.
- A December 2025 study combining RMT with ResNet-based covariance estimation on 89 cryptocurrencies produced portfolios that remained **robust across bull and bear market regime shifts**. The two-step approach (hierarchical filtering + neural network correction) outperformed all other methods.
- The **BBP (Baik–Ben Arous–Péché) phase transition** from RMT determines the threshold at which weak signals become statistically detectable — below this threshold, you're trading on noise.
- Multifractal cross-correlation analysis combined with RMT on 140 Binance cryptocurrencies revealed that standard correlation methods are **inadequate** — you need detrended, fluctuation-weighted approaches.

### Trading Bot Application

1. **Correlation matrix cleaning:** Before computing any multi-asset strategy (pairs trading, portfolio allocation), clean your correlation matrix using RMT. Remove eigenvalues within the Marčenko-Pastur bounds and reconstruct. This gives you the "true" correlation structure.
2. **Market mode detection:** The largest eigenvalue typically represents the "market factor" — its eigenvector tells you each coin's loading on the overall market. The second and third eigenvalues represent sector/narrative rotations.
3. **Regime detection via eigenvalue dynamics:** Track how eigenvalues evolve over time. When eigenvalues that were previously within the noise band break out, a new collective mode is forming — potentially signalling a sector rotation or correlated movement.

### Implementation

```python
# Conceptual: RMT-based correlation cleaning
import numpy as np
from scipy.linalg import eigh

def clean_correlation_rmt(returns, q_ratio=None):
    """
    Clean a correlation matrix using Random Matrix Theory.
    Remove eigenvalues within the Marchenko-Pastur bounds.
    
    returns: (T x N) matrix of asset returns
    """
    T, N = returns.shape
    if q_ratio is None:
        q_ratio = T / N
    
    # Compute correlation matrix
    corr = np.corrcoef(returns.T)
    
    # Marchenko-Pastur bounds
    lambda_plus = (1 + 1/np.sqrt(q_ratio))**2
    lambda_minus = (1 - 1/np.sqrt(q_ratio))**2
    
    # Eigendecomposition
    eigenvalues, eigenvectors = eigh(corr)
    
    # Clean: replace noise eigenvalues with their average
    noise_mask = (eigenvalues >= lambda_minus) & (eigenvalues <= lambda_plus)
    noise_avg = np.mean(eigenvalues[noise_mask]) if noise_mask.any() else 1.0
    
    cleaned_eigenvalues = eigenvalues.copy()
    cleaned_eigenvalues[noise_mask] = noise_avg
    
    # Reconstruct
    cleaned_corr = eigenvectors @ np.diag(cleaned_eigenvalues) @ eigenvectors.T
    
    # Normalize diagonal to 1
    d = np.sqrt(np.diag(cleaned_corr))
    cleaned_corr = cleaned_corr / np.outer(d, d)
    
    return cleaned_corr, eigenvalues, lambda_plus, lambda_minus
```

---

## 6. Optimal Transport & Wasserstein Distance

### The Theory

Optimal transport asks: what is the cheapest way to transform one probability distribution into another? The cost of this transformation is the **Wasserstein distance** — a metric on the space of probability distributions that is far more meaningful than Euclidean distance or KL divergence for financial data.

Unlike correlation or simple moment comparisons, Wasserstein distance captures the **full distributional shape** — tails, skewness, multimodality, and all.

### What the Research Shows

- **Wasserstein k-means (WK-means)** clustering of return distributions successfully detects bull/bear market regimes without any modelling assumptions. It significantly outperforms standard k-means on non-Gaussian data (like crypto), precisely because it captures distributional features beyond mean and variance.
- **Sliced Wasserstein k-means (sWK-means)** extends this to multidimensional time series — allowing regime detection across multiple assets simultaneously.
- A Python implementation reproducing the paper results is available on GitHub (Horvath et al., 2021), tested on SPY data and synthetic Merton Jump Diffusion processes.
- **Causal optimal transport** has been used to build generative models (TC-VAE) for financial time series that respect the causal structure of markets — useful for scenario generation and strategy backtesting.

### Trading Bot Application

1. **Distribution-aware regime detection:** Instead of clustering returns by their values, cluster by their **distributions** using Wasserstein distance. This catches regime changes that moment-based methods miss — e.g., when the mean is the same but the tail structure has changed.
2. **Change point detection:** Use the Wasserstein two-sample test on rolling windows. When the test statistic exceeds a threshold, the return distribution has shifted — flag a regime change.
3. **Strategy allocation by regime:** Once you detect the current regime, allocate to the strategy that performed best in historically similar regimes (same Wasserstein cluster).

---

## 7. Lévy Processes & Jump-Diffusion Models

### The Theory

Standard models assume prices follow geometric Brownian motion (continuous, Gaussian increments). Crypto markets have **jumps** — sudden, large price changes that are not captured by continuous models. Lévy processes generalise Brownian motion to include these jumps.

The **Merton Jump Diffusion (MJD)** model adds a compound Poisson jump process to the standard diffusion:

dS(t)/S(t) = μdt + σdW(t) + dJ(t)

where J(t) is the jump process. This captures both the continuous fluctuation (σdW) and the sudden spikes (dJ).

### What the Research Shows

- Applied to BTC 1-minute data across 2021–2022, researchers found an average of **3.5 jumps per day**. A path-dependent Monte Carlo simulation using the MJD model produced realistic price path forecasts.
- The **Lévy-GJR-GARCH** model family — which combines Lévy jump processes with asymmetric GARCH volatility — provides significantly better risk estimates for BTC, ETH, and XRP than standard models. Models capturing extreme tails and asymmetric jumps are best suited for crypto.
- BTC-USD exchange data confirms **Lévy distributions with index ~1.22** — not Gaussian. This means standard statistical tools systematically underestimate risk.

### Trading Bot Application

1. **Jump detection:** Use the Lee-Mykland test on high-frequency data to detect jumps in real-time. A detected jump is actionable — it indicates a structural break.
2. **Jump-adjusted volatility:** Standard volatility estimates are contaminated by jumps. Separate continuous and jump components of volatility for more accurate risk sizing.
3. **Monte Carlo with MJD:** For scenario analysis and option-like position sizing, simulate price paths using MJD rather than GBM. The jump component generates realistic "what if" scenarios.

---

## 8. Synthesis: A Multi-Layer Mathematical Architecture

The real power comes from combining these approaches. Here is a conceptual architecture for your trading bot's mathematical engine:

### Layer 1 — Signal Preprocessing
- **Wavelet denoising** to clean the raw price signal
- **Hankel matrix denoising** for volatility estimation

### Layer 2 — Regime Detection
- **Rolling Hurst exponent** (DFA) → trending vs mean-reverting
- **Wasserstein k-means** on return distributions → bull/bear/transition regimes
- **RMT eigenvalue dynamics** → collective market mode shifts

### Layer 3 — Causal & Information Structure
- **Transfer entropy network** → which coins are leading information flow
- **Rényi TE** (low α) → tail-event information flow for crash detection
- **TDA persistence norms** → systemic stress accumulation

### Layer 4 — Prediction & Risk
- **Lévy-GARCH** volatility model → jump-aware risk sizing
- **Tsallis entropy** → fat-tail-aware position sizing
- **Fractal dimension** → theoretical prediction horizon limit

### Layer 5 — Emerging Frontier
- **Kolmogorov-Arnold Networks (KANs)** with symbolic regression → discover interpretable mathematical formulas directly from price data
- **DecoKAN** architecture → wavelet-decomposed KAN that produces readable equations

---

## Key References

1. "Optimisation of Cryptocurrency Trading Using the Fractal Market Hypothesis with Symbolic Regression" — Preprints.org, 2025
2. "Can topological transitions in cryptocurrency systems serve as early warning signals" — Physica A, 2024
3. "ChainNet: Learning on Blockchain Graphs with Topological Features" — NSF, persistent homology on blockchain
4. "Entropy-Based Portfolio Optimization in Cryptocurrency Markets: A Unified Maximum Entropy Framework" — Entropy, March 2026
5. "Causal Inference in Time Series in Terms of Rényi Transfer Entropy" — PMC, 2022
6. "Denoising Complex Covariance Matrices with Hybrid ResNet and Random Matrix Theory" — arXiv, December 2025
7. "Clustering Market Regimes Using the Wasserstein Distance" — Horvath et al., 2021
8. "Prediction of Cryptocurrency Prices through a Path Dependent Monte Carlo Simulation" — arXiv, 2024
9. "DecoKAN: Interpretable Decomposition for Forecasting Cryptocurrency Market Dynamics" — arXiv, December 2025
10. "Fractional and fractal processes applied to cryptocurrencies price series" — PMC, 2021
11. "On the topology of cryptocurrency markets" — ScienceDirect, July 2023
12. "Cryptocurrency risk management using Lévy processes and time-varying volatility" — Springer, March 2025

---

## Implementation Priority Recommendation

For immediate impact on your trading bot, prioritise in this order:

1. **Wavelet denoising** — easiest to implement, immediate accuracy gain on existing models
2. **Rolling Hurst exponent** — straightforward regime indicator, switches between strategies
3. **Transfer entropy network** — identifies lead-lag relationships for entry timing
4. **RMT correlation cleaning** — critical if trading multiple assets
5. **Wasserstein regime detection** — robust regime classification
6. **TDA features** — powerful but requires more computation; use as supplementary features
7. **Lévy-GARCH / KAN symbolic regression** — longer-term R&D items

---

*This report represents a survey of current academic research and does not constitute financial advice. All trading strategies carry risk. The mathematical methods described require careful calibration and validation before deployment.*
