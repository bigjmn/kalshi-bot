"""
BTC Kalshi YES-probability calculator under driftless arithmetic Brownian motion.

Settlement: YES pays $1 if the mean price over the last `window_s` seconds
before T exceeds strike K.
"""

import math

import numpy as np

def estimate_sigma_abm(prices):
    """
    prices: equally spaced 1-second BTC prices
    returns sigma in dollars / sqrt(second)
    """
    diffs = np.diff(prices)
    return float(np.std(diffs, ddof=1))


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (no SciPy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def yes_probability(
    t_ms: float,
    T_ms: float,
    X_t: float,
    K: float,
    sigma: float,
    A_t: float | None = None,
    window_s: float = 60.0,
) -> float:
    """
    P(mean of X over [T - window_s, T]  >  K  |  F_t) under dX = sigma dW.

    Two regimes:
      * tau := T - t  >=  window_s  (before the averaging window opens)
            m_t = X_t
            v_t = sigma^2 * (tau - 2*W/3)
      * 0  <  tau  <  window_s     (inside the averaging window)
            m_t = (A_t + X_t * tau) / W
            v_t = sigma^2 * tau^3 / (3 * W^2)
        where A_t = integral of X_s over [T - W, t]  (units: USD * s)

    Parameters
    ----------
    t_ms, T_ms : float
        Current and expiry timestamps in milliseconds (any consistent base).
    X_t : float
        Current BTC price in USD.
    K : float
        Strike price in USD.
    sigma : float
        Volatility in USD per sqrt(second).
    A_t : float, optional
        Realized integral of X_s from T - window_s to t, in USD*seconds.
        Required when the current time is inside the averaging window.
        Ignored otherwise.
    window_s : float, default 60.0
        Length of the averaging window in seconds.

    Returns
    -------
    float in [0, 1].
    """
    tau = (T_ms - t_ms) / 1000.0  # seconds remaining
    W = window_s

    # Past expiry: deterministic. Use A_t if supplied, else fall back to X_t.
    if tau <= 0.0:
        if A_t is not None:
            return 1.0 if (A_t / W) > K else 0.0
        return 1.0 if X_t > K else 0.0

    if tau >= W:
        # Case A: before settlement window
        m = X_t
        v = sigma * sigma * (tau - 2.0 * W / 3.0)
    else:
        # Case B: inside settlement window — A_t required
        if A_t is None:
            raise ValueError(
                f"Inside settlement window (tau={tau:.3f}s < W={W:g}s); "
                "must pass A_t = integral of X_s over [T - W, t] in USD*s."
            )
        m = (A_t + X_t * tau) / W
        v = sigma * sigma * tau ** 3 / (3.0 * W * W)

    if v <= 0.0:
        return 1.0 if m > K else 0.0

    z = (m - K) / math.sqrt(v)
    return _norm_cdf(z)


# ------------------------------------------------------------------
# Quick sanity checks
# ------------------------------------------------------------------
if __name__ == "__main__":
    # Setup: T is 15 minutes from t=0
    t0_ms = 0
    t5_ms = 5*60*1000
    T_ms = 15 * 60 * 1000
    X = 70_000.0
    sigma = 8.0  # USD / sqrt(s)  -> ~$387/sqrt(15min), realistic-ish

    # 1) At-the-money, full 15 min remaining: should be just under 0.5
    #    (just under because the variance from the unfinished averaging is
    #     slightly less than the variance of X_T itself)
    p = yes_probability(t0_ms, T_ms, X, X, sigma)
    print(f"ATM, 15 min out:           p = {p:.4f}  (expect ~0.50)")

    # 2) Strike $300 above spot, 10 min out
    p = yes_probability(t5_ms, T_ms, X, X - 300, sigma)
    print(f"K = X + 300, 10 min out:  p = {p:.4f}")

    # 3) Same strike, 1 min before window opens (tau = 2 min)
    p = yes_probability(13 * 60 * 1000, T_ms, X, X + 1000, sigma)
    print(f"K = X + 1000, tau = 2 min: p = {p:.4f}")

    # 4) Inside the window, 30 s left, ATM, with A_t consistent with X_t = K
    A_t = X * 30.0  # constant price assumption: integral over 30 s
    p = yes_probability(
        T_ms - 30_000, T_ms, X, X, sigma, A_t=A_t
    )
    print(f"In window, 30 s left, ATM: p = {p:.4f}  (expect ~0.50)")

    # 5) In window, 30 s left, but realized average so far was K + 50 above
    A_t = (X + 50) * 30.0
    p = yes_probability(
        T_ms - 30_000, T_ms, X, X, sigma, A_t=A_t
    )
    print(f"In window, ahead by $50:   p = {p:.4f}  (expect > 0.5)")

    # 6) Continuity check: tau just above and below W
    eps = 1e-3
    p_just_before = yes_probability(
        T_ms - int((W := 60) * 1000) - 1, T_ms, X, X + 100, sigma
    )
    p_just_inside = yes_probability(
        T_ms - W * 1000 + 1, T_ms, X, X + 100, sigma, A_t=0.0  # A_t -> 0 at t = T-W
    )
    print(f"Continuity at window edge: {p_just_before:.6f} vs {p_just_inside:.6f}")