import json
import time
import sys
import os
import random
from core import generate_data, estimate_mean_sats, estimate_mean_fixed_trimmed, estimate_mean_plain, estimate_mean_median, calibrate_c

def run_experiment() -> dict:
    """
    Executes the full experiment.
    """
    N_SAMPLES = 128
    N_TESTS = 50
    OUTLIER_INTENSITY = 8.0

    def generate_sym_outliers_data(n_samples, n_outliers, seed):
        data = generate_data(n_samples, 0, 0, seed)
        rng = random.Random(seed)
        indices = rng.sample(range(n_samples), n_outliers)
        for i, idx in enumerate(indices):
            if i < n_outliers // 2:
                data[idx] = 8.0
            else:
                data[idx] = -8.0
        return data

    scenarios = [
        {"name": "Clean", "rate": 0.0, "type": "clean"},
        {"name": "Sym_Contam_5pct", "rate": 0.05, "type": "sym"},
        {"name": "Pos_Contam_5pct", "rate": 0.05, "type": "pos"},
        {"name": "Sym_Contam_15pct", "rate": 0.15, "type": "sym"},
        {"name": "Pos_Contam_15pct", "rate": 0.15, "type": "pos"},
    ]

    c_val = calibrate_c(n_repeats=20, seed_base=9999)
    
    methods = {
        "Plain Mean": estimate_mean_plain,
        "Median": estimate_mean_median,
        "Fixed Trimmed": estimate_mean_fixed_trimmed,
        "SATS (Proposed)": lambda x: estimate_mean_sats(x, c_val),
        "SATS (Ablation c=0)": lambda x: estimate_mean_sats(x, 0.0),
    }
    
    results = {}
    total_samples = 0
    all_seed_mses = []
    
    for scenario in scenarios:
        scenario_name = scenario["name"]
        rate = scenario["rate"]
        stype = scenario["type"]
        
        n_outliers = int(round(rate * N_SAMPLES))
        
        scenario_data = []
        
        for i in range(N_TESTS):
            seed = i
            
            if stype == "clean":
                data = generate_data(N_SAMPLES, 0, 0, seed)
            elif stype == "pos":
                data = generate_data(N_SAMPLES, n_outliers, 1, seed)
            elif stype == "sym":
                data = generate_sym_outliers_data(N_SAMPLES, n_outliers, seed)
            
            seed_results = {}
            for m_name, m_func in methods.items():
                est = m_func(data)
                mses = (est - 0.0) ** 2
                seed_results[m_name] = mses
                total_samples += 1
                
            scenario_data.append(seed_results)
            
        scenario_mses = {m: [] for m in methods.keys()}
        for seed_res in scenario_data:
            for m in methods.keys():
                scenario_mses[m].append(seed_res[m])
                
        scenario_metrics = {}
        for m in methods.keys():
            vals = scenario_mses[m]
            mean_mse = sum(vals) / len(vals)
            n = len(vals)
            if n > 1:
                mean_sq = sum(v * v for v in vals) / n
                sq_mean = mean_mse * mean_mse
                var = mean_sq - sq_mean
                var = max(0.0, var)
                se = (var / n) ** 0.5
            else:
                se = 0.0
            scenario_metrics[m] = {
                "mean_mse": mean_mse,
                "se": se
            }
            
        results[scenario_name] = {
            "metrics": scenario_metrics,
            "raw_mses": scenario_mses
        }
        
    proposed_method_name = "SATS (Proposed)"
    baseline_method_name = "Fixed Trimmed"
    
    overall_mean_mses = {}
    for m in methods.keys():
        scen_mses = [results[s]["metrics"][m]["mean_mse"] for s in [sc["name"] for sc in scenarios]]
        overall_mean_mses[m] = sum(scen_mses) / len(scen_mses)
        
    improvement_delta = overall_mean_mses[proposed_method_name] - overall_mean_mses[baseline_method_name]
    
    sample_count = N_TESTS
    if sample_count < 30:
        raise ValueError("Sample count too low")
        
    metrics_obj = {
        "proposed_primary": overall_mean_mses[proposed_method_name],
        "baseline_primary": overall_mean_mses[baseline_method_name],
        "improvement_delta": improvement_delta,
        "sample_count": sample_count,
        "n_tests_per_scenario": N_TESTS,
        "n_scenarios": len(scenarios),
        "calibrated_c": c_val,
        "overall_mean_mse": overall_mean_mses,
        "per_scenario_metrics": {
            sc["name"]: results[sc["name"]]["metrics"] for sc in scenarios
        },
        "per_seed_raw_data": {
            sc["name"]: results[sc["name"]]["raw_mses"] for sc in scenarios
        }
    }
    
    print(f"HARNESS_METRICS={json.dumps(metrics_obj)}")
    
    return metrics_obj

if __name__ == '__main__':
    run_experiment()