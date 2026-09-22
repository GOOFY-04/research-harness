"""
train.py

Entry point that orchestrates the end-to-end experiment for ACPF-UT.
Trains all methods on synthetic drift data, evaluates coverage and width
on held-out segments, calculates metrics (coverage, width, delta), and
prints results in the required HARNESS_METRICS format.
"""

import json
import random
from models import OnlineMeanModel
from baselines import FixedAlphaConformal, ConformalPID
from acpf_ut import ACPF_UT
from benchmark import generate_drift_sequence


def _compute_metrics(pred_lower, pred_upper, y_true):
    """
    Compute coverage and average width for a sequence of predictions.
    
    Args:
        pred_lower: list of lower bounds
        pred_upper: list of upper bounds
        y_true: list of true values
    
    Returns:
        tuple: (coverage, avg_width, delta_width)
    """
    n = len(y_true)
    if n == 0:
        return 0.0, 0.0, 0.0
    
    covered = 0
    total_width = 0.0
    
    for lo, hi, y in zip(pred_lower, pred_upper, y_true):
        if lo <= y <= hi:
            covered += 1
        total_width += (hi - lo)
    
    coverage = covered / n
    avg_width = total_width / n
    
    # delta_width: difference in average width compared to a reference
    # For standalone computation, we just report avg_width as the metric of interest.
    # The 'delta' in harness metrics is typically relative to a baseline, but here
    # we report the absolute metrics for each method, and the harness will compare them.
    return coverage, avg_width, avg_width


def run_experiment(seed=42, n_samples=500, n_eval=100):
    """
    Run the deterministic experiment comparing all methods.
    
    Args:
        seed: Random seed for reproducibility.
        n_samples: Total number of samples in the synthetic sequence.
        n_eval: Number of held-out evaluation samples (last portion of sequence).
    
    Returns:
        dict: Harness metrics containing results for all methods.
    """
    # Define drift scenario: two shifts at t=100 and t=200
    shift_indices = [100, 200, 300]
    shift_amounts = [5.0, -3.0, 2.0]
    
    # Generate synthetic data
    x_seq, y_seq, ground_truth_alpha_indices = generate_drift_sequence(
        seed=seed,
        n=n_samples,
        shift_indices=shift_indices,
        shift_amounts=shift_amounts
    )
    
    # Define training and evaluation split
    # Train on the first (n_samples - n_eval) samples, evaluate on the last n_eval
    train_size = n_samples - n_eval
    if train_size <= 0:
        raise ValueError("n_samples must be greater than n_eval")
    
    x_train, y_train = x_seq[:train_size], y_seq[:train_size]
    x_eval, y_eval = x_seq[train_size:], y_seq[train_size:]
    
    # Define method configurations
    # Common parameters
    learning_rate = 0.1
    window_size = 50
    nominal_alpha = 0.1  # Target coverage 90%
    hard_cov = 0.85      # Hard coverage floor
    
    # 1. Initialize and train ACPF-UT
    model_acpf = OnlineMeanModel(learning_rate=learning_rate)
    acpf = ACPF_UT(
        model=model_acpf,
        candidate_alphas=[0.05, 0.10, 0.15, 0.20, 0.30],
        lambda_cov=1.0,
        mu_width=0.1,
        hard_cov=hard_cov,
        window_size=window_size,
        drift_window=50
    )
    
    # 2. Initialize and train FixedAlphaConformal (synchronized baseline)
    model_fixed = OnlineMeanModel(learning_rate=learning_rate)
    fixed_alpha = FixedAlphaConformal(
        model=model_fixed,
        alpha=nominal_alpha,
        window_size=window_size
    )
    
    # 3. Initialize and train ConformalPID
    model_pid = OnlineMeanModel(learning_rate=learning_rate)
    pid = ConformalPID(
        model=model_pid,
        nominal_alpha=nominal_alpha,
        window_size=window_size,
        kp=0.5,
        ki=0.1
    )
    
    # --- Training Phase ---
    # All methods see the identical observation sequence during training
    acpf_widths_train = []
    fixed_widths_train = []
    pid_widths_train = []
    
    for t in range(train_size):
        x_t = x_train[t]
        y_t = y_train[t]
        
        # Step each method
        w_acpf = acpf.step(x_t, y_t)
        w_fixed = fixed_alpha.step(x_t, y_t)
        w_pid = pid.step(x_t, y_t)
        
        acpf_widths_train.append(w_acpf)
        fixed_widths_train.append(w_fixed)
        pid_widths_train.append(w_pid)
    
    # --- Evaluation Phase ---
    # Evaluate on held-out set. For evaluation, we need to predict intervals
    # and check coverage.
    
    # Helper function to predict a single point and get interval bounds
    def predict_interval_acpf(model, alpha, window, x):
        """Predict interval for ACPF-UT style evaluation."""
        # For evaluation, we use the current model state and a fixed alpha
        # (the current alpha of the method). We simulate the causal prediction.
        pred = model.predict(x)
        if len(window) == 0:
            q = 0.0
        else:
            k = max(1, int(len(window) * (1 - alpha)))
            if k > len(window):
                k = len(window)
            q = sorted(window)[k - 1]
        return pred - q, pred + q
    
    # For ACPF-UT, the alpha is dynamic. During evaluation, we use the last
    # learned alpha for each step, or we re-run the prediction logic.
    # To keep it simple and consistent with the "held-out" concept, we will:
    # 1. Use the model state after training.
    # 2. For each eval point, predict the center using the model.
    # 3. For the width, we need the residual window. But the residual window
    #    is an internal state of the method. 
    #    
    # The contract says "Evaluates on held-out set". This implies we should
    # continue the online process or use the final state.
    # Standard practice for online methods: continue stepping through the
    # evaluation set, accumulating widths and checking coverage.
    
    acpf_eval_lowers = []
    acpf_eval_uppers = []
    fixed_eval_lowers = []
    fixed_eval_uppers = []
    pid_eval_lowers = []
    pid_eval_uppers = []
    
    for t in range(len(x_eval)):
        x_t = x_eval[t]
        y_t = y_eval[t]
        
        # For evaluation, we need to generate the interval BEFORE observing y_t.
        # The 'step' function in baselines/acpf returns the width AFTER update.
        # To properly evaluate coverage, we must predict the interval using
        # the state *before* seeing y_t, then check if y_t is in that interval.
        #
        # However, the provided interfaces for `step` do `predict`, then `update`,
        # then return width. The width returned is for the *next* prediction or
        # the *current* calibration?
        #
        # Looking at `FixedAlphaConformal.step`:
        # 1. pred = model.predict(x)
        # 2. model.update(x, y)
        # 3. s_t = abs(y - model.predict(x))  # post-update prediction
        # 4. Update window, compute width from window.
        # 5. Return width.
        #
        # The width returned is based on the *updated* window. It is not the width
        # of the interval that covered y_t. The interval that covered y_t would be
        # [pred - q_old, pred + q_old].
        #
        # To correctly evaluate coverage, we need the interval *used* for y_t.
        # We can reproduce the logic:
        # 1. Get current alpha and window state.
        # 2. Predict center.
        # 3. Compute q from window.
        # 4. Interval = [center - q, center + q].
        # 5. Check if y_t in interval.
        # 6. Call step to update state for next iteration.
        
        # ACPF-UT
        # Get current state
        alpha_acpf = acpf.get_current_alpha()
        # We need access to the residual window and model to compute the interval.
        # The `step` function returns the width of the *next* interval or current?
        # Let's assume we need to manually compute the "causal" interval for coverage.
        
        # Center: model state before update
        center_acpf = model_acpf.predict(x_t)
        # Quantile: from current residual window (before update)
        res_window_acpf = acpf.residual_window
        if len(res_window_acpf) > 0:
            k_acpf = max(1, int(len(res_window_acpf) * (1 - alpha_acpf)))
            if k_acpf > len(res_window_acpf):
                k_acpf = len(res_window_acpf)
            q_acpf = sorted(res_window_acpf)[k_acpf - 1]
        else:
            q_acpf = 0.0
        lo_acpf = center_acpf - q_acpf
        hi_acpf = center_acpf + q_acpf
        acpf_eval_lowers.append(lo_acpf)
        acpf_eval_uppers.append(hi_acpf)
        # Now update the method for the next step
        acpf.step(x_t, y_t)
        
        # Fixed Alpha
        center_fixed = model_fixed.predict(x_t)
        res_window_fixed = fixed_alpha.residual_window
        if len(res_window_fixed) > 0:
            k_fixed = max(1, int(len(res_window_fixed) * (1 - nominal_alpha)))
            if k_fixed > len(res_window_fixed):
                k_fixed = len(res_window_fixed)
            q_fixed = sorted(res_window_fixed)[k_fixed - 1]
        else:
            q_fixed = 0.0
        lo_fixed = center_fixed - q_fixed
        hi_fixed = center_fixed + q_fixed
        fixed_eval_lowers.append(lo_fixed)
        fixed_eval_uppers.append(hi_fixed)
        fixed_alpha.step(x_t, y_t)
        
        # PID
        center_pid = model_pid.predict(x_t)
        # PID alpha is internal. We need to access it.
        # The interface doesn't expose get_alpha(), but we can assume the width
        # returned by step is the *new* width. To get the coverage interval
        # for y_t, we must use the *old* width/alpha state.
        # This is a limitation of the simple interface.
        # For PID, `step` returns `self._width` which is computed after update.
        # We will approximate coverage by using the width returned by step,
        # but this is slightly off (it's the width for t+1, not t).
        # Alternatively, we can access `pid.alpha` and `pid.residual_window` 
        # if they are public attributes. They are instance attributes.
        
        alpha_pid = pid.alpha
        res_window_pid = pid.residual_window
        if len(res_window_pid) > 0:
            k_pid = max(1, int(len(res_window_pid) * (1 - alpha_pid)))
            if k_pid > len(res_window_pid):
                k_pid = len(res_window_pid)
            q_pid = sorted(res_window_pid)[k_pid - 1]
        else:
            q_pid = 0.0
        lo_pid = center_pid - q_pid
        hi_pid = center_pid + q_pid
        pid_eval_lowers.append(lo_pid)
        pid_eval_uppers.append(hi_pid)
        pid.step(x_t, y_t)
    
    # Compute metrics
    cov_acpf, wid_acpf, _ = _compute_metrics(acpf_eval_lowers, acpf_eval_uppers, y_eval)
    cov_fixed, wid_fixed, _ = _compute_metrics(fixed_eval_lowers, fixed_eval_uppers, y_eval)
    cov_pid, wid_pid, _ = _compute_metrics(pid_eval_lowers, pid_eval_uppers, y_eval)
    
    # Delta width: relative to fixed alpha baseline (synchronized)
    delta_acpf = wid_acpf - wid_fixed
    delta_pid = wid_pid - wid_fixed
    
    results = {
        "seed": seed,
        "n_samples": n_samples,
        "n_eval": len(y_eval),
        "acpf_ut": {
            "coverage": round(cov_acpf, 4),
            "avg_width": round(wid_acpf, 4),
            "delta_width_vs_fixed": round(delta_acpf, 4),
            "final_alpha": round(acpf.get_current_alpha(), 4)
        },
        "fixed_alpha": {
            "coverage": round(cov_fixed, 4),
            "avg_width": round(wid_fixed, 4)
        },
        "conformal_pid": {
            "coverage": round(cov_pid, 4),
            "avg_width": round(wid_pid, 4),
            "delta_width_vs_fixed": round(delta_pid, 4)
        }
    }
    
    return results


def main():
    """
    Main entry point.
    """
    # Fixed iteration counts for reproducibility
    results = run_experiment(seed=42, n_samples=500, n_eval=100)
    
    # The harness requires specific metric keys in the JSON object:
    # "proposed_primary", "baseline_primary", "improvement_delta", "sample_count"
    # We map our experiment results to these keys.
    # proposed_primary: average width of ACPF-UT (lower is better, but we report the metric)
    # baseline_primary: average width of Fixed Alpha baseline
    # improvement_delta: difference (proposed - baseline)
    # sample_count: number of evaluation samples
    
    harness_metrics = {
        "proposed_primary": results["acpf_ut"]["avg_width"],
        "baseline_primary": results["fixed_alpha"]["avg_width"],
        "improvement_delta": results["acpf_ut"]["delta_width_vs_fixed"],
        "sample_count": results["n_eval"]
    }
    
    print(f"HARNESS_METRICS={json.dumps(harness_metrics)}")


if __name__ == "__main__":
    main()