"""estimator_suite.py

Implements Horvitz-Thompson, Capped Horvitz-Thompson, and Hajek estimators
for missing data scenarios. Pure functions using only the Python standard
library.
"""


def ht_estimate(y: list[float], R: list[int], p: list[float], n: int) -> float:
    """Horvitz-Thompson estimator.

    Formula: sum(R_i * y_i / p_i) / n
    """
    total = 0.0
    for r, y_val, p_val in zip(R, y, p):
        if r == 1 and p_val > 0.0:
            total += y_val / p_val
    return total / n


def capped_ht_estimate(
    y: list[float], R: list[int], p: list[float], n: int, cap: float = 5.0
) -> float:
    """Capped Horvitz-Thompson estimator with fixed cap.

    Formula: sum(R_i * y_i * min(1/p_i, cap)) / n
    """
    total = 0.0
    for r, y_val, p_val in zip(R, y, p):
        if r == 1:
            if p_val > 0.0:
                w = min(1.0 / p_val, cap)
            else:
                w = cap
            total += y_val * w
    return total / n


def hajek_estimate(y: list[float], R: list[int], p: list[float]) -> float:
    """Hajek (self-normalized) estimator.

    Formula: sum(R_i * y_i / p_i) / sum(R_i / p_i)

    Returns 0.0 if the denominator is zero or if all observed R_i are 0.
    """
    numerator = 0.0
    denominator = 0.0
    has_observed = False

    for r, y_val, p_val in zip(R, y, p):
        if r == 1:
            has_observed = True
            if p_val > 0.0:
                w = 1.0 / p_val
            else:
                w = 0.0  # Treat weight as 0 if p_i is zero (edge case)
            numerator += y_val * w
            denominator += w

    if not has_observed or denominator <= 0.0:
        return 0.0

    return numerator / denominator