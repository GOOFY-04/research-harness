"""train.py

Entry point for the end-to-end synthetic benchmark of the CRIPW-DB experiment.
Runs the full deterministic experiment and writes results to JSON files.
"""

import json
import os

from synthetic_data_harness import generate_data
from estimator_suite import ht_estimate, capped_ht_estimate, hajek_estimate
from boundary_analysis import run_experiment, compute_mse
from results_io import write_pairs_json, write_experiment_contract


def main() -> None:
    # Fixed experiment parameters
    a_values = [0.0, 2.0, 5.0]
    n = 128
    S = 50
    cap = 5.0
    true_mean = 1.0

    # Run the main experiment
    results = run_experiment(a_values, n, S, cap)

    # Prepare the experiment contract
    contract = {
        "version": 1,
        "primary_comparison": "primary",
        "comparisons": [
            {
                "id": "primary",
                "metric_name": "mean squared estimation error against target 1.0",
                "definition": "Per-seed squared error (Est - 1.0)^2 for the capped HT (proposed) and un-capped HT (baseline), averaged over 50 independent trial seeds. Scenario 'a' is chosen as the active evaluation scope.",
                "unit": "squared target units",
                "direction": "minimize",
                "sample_unit": "one independent trial seed",
                "pairing": "Each seed pair provides the exact same y, R, and p data to both estimators. Only the estimator formula differs.",
                "evaluation_scope": "Single active scenario 'a=2' (transition zone) plus aggregated equal-weighted evaluation across a in [0,2,5].",
                "sampling_assumptions": "50 independent seeds, no temporal dependence, iid within seed.",
                "proposed_metric": "proposed_primary",
                "baseline_metric": "baseline_primary",
                "samples_path": "results/primary_pairs.json",
            },
            {
                "id": "secondary_hajek",
                "metric_name": "Hajek estimator squared error",
                "definition": "Per-seed squared error for the Hajek estimator vs target 1.0, over the same 50 seeds as the primary comparison.",
                "unit": "squared target units",
                "direction": "minimize",
                "sample_unit": "one independent trial seed",
                "pairing": "Same seed data used as primary comparison.",
                "evaluation_scope": "Same active scenarios as primary comparison.",
                "sampling_assumptions": "50 independent seeds.",
                "proposed_metric": "hajek_se",
                "baseline_metric": "baseline_primary",
                "samples_path": "results/secondary_hajek_pairs.json",
            },
            {
                "id": "ablation_cap_inf",
                "metric_name": "Capped HT with infinite cap squared error (should equal baseline)",
                "definition": "Per-seed squared error for the capped HT algorithm when cap=infinity (ablation), vs baseline.",
                "unit": "squared target units",
                "direction": "minimize",
                "sample_unit": "one independent trial seed",
                "pairing": "Same seed data used as primary comparison.",
                "evaluation_scope": "Single active scenario.",
                "sampling_assumptions": "50 independent seeds.",
                "proposed_metric": "ablation_cap_inf",
                "baseline_metric": "baseline_primary",
                "samples_path": "results/ablation_cap_inf_pairs.json",
            },
        ],
    }

    # Write experiment contract
    write_experiment_contract("results/experiment_contract.json", contract)

    # Generate paired JSON data for each comparison
    # We need per-seed data for the active scenario a=2.0 (primary evaluation scope)
    active_a = 2.0
    active_key = str(active_a)

    # Primary pairs: capped HT vs un-capped HT
    primary_pairs = []
    # Secondary pairs: Hajek vs un-capped HT
    secondary_pairs = []
    # Ablation pairs: capped HT with cap=inf vs un-capped HT
    ablation_pairs = []

    # Compute sums for means
    sum_proposed_primary = 0.0
    sum_baseline_primary = 0.0
    sum_hajek_se = 0.0
    sum_ablation_cap_inf = 0.0

    for seed in range(S):
        y, p, R = generate_data(active_a, n, seed)

        est_ht = ht_estimate(y, R, p, n)
        est_cht = capped_ht_estimate(y, R, p, n, cap)
        est_hajek = hajek_estimate(y, R, p)
        est_cht_inf = capped_ht_estimate(y, R, p, n, cap=float('inf'))

        sq_err_cht = (est_cht - true_mean) ** 2
        sq_err_ht = (est_ht - true_mean) ** 2
        sq_err_hajek = (est_hajek - true_mean) ** 2
        sq_err_cht_inf = (est_cht_inf - true_mean) ** 2

        sum_proposed_primary += sq_err_cht
        sum_baseline_primary += sq_err_ht
        sum_hajek_se += sq_err_hajek
        sum_ablation_cap_inf += sq_err_cht_inf

        primary_pairs.append({
            "pair_id": f"primary-{seed}",
            "proposed": sq_err_cht,
            "baseline": sq_err_ht,
        })

        secondary_pairs.append({
            "pair_id": f"hajek-{seed}",
            "proposed": sq_err_hajek,
            "baseline": sq_err_ht,
        })

        ablation_pairs.append({
            "pair_id": f"ablation-{seed}",
            "proposed": sq_err_cht_inf,
            "baseline": sq_err_ht,
        })

    write_pairs_json("results/primary_pairs.json", primary_pairs)
    write_pairs_json("results/secondary_hajek_pairs.json", secondary_pairs)
    write_pairs_json("results/ablation_cap_inf_pairs.json", ablation_pairs)

    # Build HARNESS_METRICS string
    agg_mse = results["aggregated_mse"]
    boundary = results["boundary_info"]
    scen = results["scenarios"]

    def _get_mse(key, method):
        return scen[str(key)][method]["mse"]

    # Compute the required numeric metrics
    proposed_primary_val = sum_proposed_primary / S
    baseline_primary_val = sum_baseline_primary / S
    improvement_delta_val = proposed_primary_val - baseline_primary_val

    hajek_se_val = sum_hajek_se / S
    ablation_cap_inf_val = sum_ablation_cap_inf / S

    metrics = {
        "proposed_primary": proposed_primary_val,
        "baseline_primary": baseline_primary_val,
        "improvement_delta": improvement_delta_val,
        "sample_count": S,
        "hajek_se": hajek_se_val,
        "ablation_cap_inf": ablation_cap_inf_val,
        "n": n,
        "cap": cap,
        "a_values": a_values,
        "aggregated_mse_ht": agg_mse["ht"],
        "aggregated_mse_capped_ht": agg_mse["capped_ht"],
        "aggregated_mse_hajek": agg_mse["hajek"],
        "scenario_mse_ht_0": _get_mse(0.0, "ht"),
        "scenario_mse_ht_2": _get_mse(2.0, "ht"),
        "scenario_mse_ht_5": _get_mse(5.0, "ht"),
        "scenario_mse_cht_0": _get_mse(0.0, "capped_ht"),
        "scenario_mse_cht_2": _get_mse(2.0, "capped_ht"),
        "scenario_mse_cht_5": _get_mse(5.0, "capped_ht"),
        "scenario_mse_hajek_0": _get_mse(0.0, "hajek"),
        "scenario_mse_hajek_2": _get_mse(2.0, "hajek"),
        "scenario_mse_hajek_5": _get_mse(5.0, "hajek"),
        "scenario_bias_ht_0": scen["0.0"]["ht"]["bias"],
        "scenario_bias_ht_2": scen["2.0"]["ht"]["bias"],
        "scenario_bias_ht_5": scen["5.0"]["ht"]["bias"],
        "scenario_bias_cht_0": scen["0.0"]["capped_ht"]["bias"],
        "scenario_bias_cht_2": scen["2.0"]["capped_ht"]["bias"],
        "scenario_bias_cht_5": scen["5.0"]["capped_ht"]["bias"],
        "scenario_var_ht_0": scen["0.0"]["ht"]["variance"],
        "scenario_var_ht_2": scen["2.0"]["ht"]["variance"],
        "scenario_var_ht_5": scen["5.0"]["ht"]["variance"],
        "scenario_var_cht_0": scen["0.0"]["capped_ht"]["variance"],
        "scenario_var_cht_2": scen["2.0"]["capped_ht"]["variance"],
        "scenario_var_cht_5": scen["5.0"]["capped_ht"]["variance"],
        "hajek_zero_denom_freq_0": scen["0.0"]["hajek"]["zero_denom_freq"],
        "hajek_zero_denom_freq_2": scen["2.0"]["hajek"]["zero_denom_freq"],
        "hajek_zero_denom_freq_5": scen["5.0"]["hajek"]["zero_denom_freq"],
        "boundary_failing_a": boundary["failing_a"],
    }

    metrics_str = "HARNESS_METRICS=" + json.dumps(metrics, ensure_ascii=False)
    print(metrics_str)


if __name__ == "__main__":
    main()