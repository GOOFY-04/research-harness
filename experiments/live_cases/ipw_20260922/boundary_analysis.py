"""boundary_analysis.py

Aggregates results per scenario and seed, computes MSE, Bias, Variance.
Implements the boundary analysis for the CRIPW-DB experiment.
"""

from synthetic_data_harness import generate_data
from estimator_suite import ht_estimate, capped_ht_estimate, hajek_estimate


def compute_mse(errors: list[float]) -> float:
    """Compute the mean squared error from a list of squared errors."""
    if not errors:
        return 0.0
    return sum(errors) / len(errors)


def compute_bias(ests: list[float], true_mean: float) -> float:
    """Compute the bias of a list of estimates relative to the true mean."""
    if not ests:
        return 0.0
    mean_est = sum(ests) / len(ests)
    return mean_est - true_mean


def compute_var(ests: list[float]) -> float:
    """Compute the sample variance of a list of estimates."""
    if len(ests) < 2:
        return 0.0
    mean = sum(ests) / len(ests)
    sum_sq_dev = sum((x - mean) ** 2 for x in ests)
    return sum_sq_dev / (len(ests) - 1)


def run_experiment(
    a_values: list[float],
    n: int,
    S: int,
    cap: float = 5.0,
) -> dict:
    """
    Run the full synthetic experiment across scenarios and seeds.

    Args:
        a_values: List of missingness mechanism parameters.
        n: Sample size per seed.
        S: Number of independent seeds.
        cap: Cap for the inverse probability weights (default 5.0).

    Returns:
        A dictionary containing per-scenario statistics, aggregated results,
        and boundary check information.
    """
    true_mean = 1.0
    results = {
        "scenarios": {},
        "aggregated_mse": {},
        "boundary_info": {},
    }

    for a in a_values:
        ht_estimates = []
        ht_sq_errors = []
        cht_estimates = []
        cht_sq_errors = []
        hajek_estimates = []
        hajek_sq_errors = []
        hajek_zero_count = 0

        for seed in range(S):
            y, p, R = generate_data(a, n, seed)

            # HT
            est_ht = ht_estimate(y, R, p, n)
            ht_estimates.append(est_ht)
            err_ht = (est_ht - true_mean) ** 2
            ht_sq_errors.append(err_ht)

            # Capped HT
            est_cht = capped_ht_estimate(y, R, p, n, cap)
            cht_estimates.append(est_cht)
            err_cht = (est_cht - true_mean) ** 2
            cht_sq_errors.append(err_cht)

            # Hajek
            est_hajek = hajek_estimate(y, R, p)
            hajek_estimates.append(est_hajek)
            err_hajek = (est_hajek - true_mean) ** 2
            hajek_sq_errors.append(err_hajek)

            # Track zero denominator events for Hajek
            denom = 0.0
            has_obs = False
            for r, p_val in zip(R, p):
                if r == 1:
                    has_obs = True
                    if p_val > 0.0:
                        denom += 1.0 / p_val
            if not has_obs or denom <= 0.0:
                hajek_zero_count += 1

        results["scenarios"][str(a)] = {
            "ht": {
                "estimates": ht_estimates,
                "squared_errors": ht_sq_errors,
                "mse": compute_mse(ht_sq_errors),
                "bias": compute_bias(ht_estimates, true_mean),
                "variance": compute_var(ht_estimates),
            },
            "capped_ht": {
                "estimates": cht_estimates,
                "squared_errors": cht_sq_errors,
                "mse": compute_mse(cht_sq_errors),
                "bias": compute_bias(cht_estimates, true_mean),
                "variance": compute_var(cht_estimates),
                "cap": cap,
            },
            "hajek": {
                "estimates": hajek_estimates,
                "squared_errors": hajek_sq_errors,
                "mse": compute_mse(hajek_sq_errors),
                "bias": compute_bias(hajek_estimates, true_mean),
                "variance": compute_var(hajek_estimates),
                "zero_denom_count": hajek_zero_count,
                "zero_denom_freq": hajek_zero_count / S if S > 0 else 0.0,
            },
        }

    # Aggregated equal-weighted MSE across scenarios
    a_list = a_values
    results["aggregated_mse"] = {
        "ht": compute_mse([results["scenarios"][str(a)]["ht"]["mse"] for a in a_list]),
        "capped_ht": compute_mse(
            [results["scenarios"][str(a)]["capped_ht"]["mse"] for a in a_list]
        ),
        "hajek": compute_mse(
            [results["scenarios"][str(a)]["hajek"]["mse"] for a in a_list]
        ),
    }

    # Boundary check: identify where CHT MSE > min(HT MSE, Hajek MSE)
    boundary_a = None
    for a in a_list:
        key = str(a)
        mse_cht = results["scenarios"][key]["capped_ht"]["mse"]
        mse_ht = results["scenarios"][key]["ht"]["mse"]
        mse_hajek = results["scenarios"][key]["hajek"]["mse"]
        min_baseline = min(mse_ht, mse_hajek)
        if mse_cht > min_baseline:
            boundary_a = a
            break

    results["boundary_info"] = {
        "failing_a": boundary_a,
        "threshold_condition": "MSE(CHT) > min(MSE(HT), MSE(Hajek))",
    }

    return results