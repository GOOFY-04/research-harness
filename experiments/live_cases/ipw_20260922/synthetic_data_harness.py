"""synthetic_data_harness.py

Generates synthetic data for missing data mechanisms and estimator inputs.
Uses only the Python standard library.
"""

import math
import random


def sigmoid(x: float) -> float:
    """Compute the sigmoid (logistic) function.

    Avoids overflow by handling large positive and negative values explicitly.
    """
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


def generate_p(x: float, a: float) -> float:
    """Compute propensity score p(x) = 0.05 + 0.9 * sigmoid(a * x).

    Note: sigmoid(-a * x) in the design is equivalent to sigmoid(a * x) only if
    we use the identity sigmoid(-z) = 1 - sigmoid(z). However, the design
    specification says p(x) = 0.05 + 0.9*sigmoid(-a*x). Let's follow the
    specification exactly.
    """
    # Design says: p(x) = 0.05 + 0.9 * sigmoid(-a * x)
    return 0.05 + 0.9 * sigmoid(-a * x)


def generate_data(a: float, n: int, seed: int) -> tuple[list[float], list[float], list[int]]:
    """Generate synthetic data for a given scenario.

    Args:
        a: The missingness mechanism parameter.
        n: The sample size.
        seed: The random seed for reproducibility.

    Returns:
        A tuple (y, p, R) where:
        y: The observed values (list of floats).
        p: The propensity scores (list of floats).
        R: The missingness indicators (list of ints, 1 if observed, 0 if missing).
    """
    rng = random.Random(seed)

    y = []
    p = []
    R = []

    for _ in range(n):
        # x ~ N(0, 1)
        x = rng.gauss(0.0, 1.0)
        # epsilon ~ N(0, 1)
        eps = rng.gauss(0.0, 1.0)

        # y = 1 + 2*x + eps
        y_val = 1.0 + 2.0 * x + eps

        # p(x) = 0.05 + 0.9 * sigmoid(-a * x)
        p_val = generate_p(x, a)

        # R ~ Bernoulli(p(x))
        r_val = 1 if rng.random() < p_val else 0

        y.append(y_val)
        p.append(p_val)
        R.append(r_val)

    return y, p, R