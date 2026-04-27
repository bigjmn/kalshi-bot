"""
BTC Kalshi YES-probability calculator under driftless arithmetic Brownian motion.

Settlement: YES pays $1 if the mean price over the last `window_s` seconds
before T exceeds strike K.
"""

import math


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

