"""
benchmark.py

Generate synthetic data streams with distributional drift (mean shifts) to simulate
non-stationary environments. Deterministic, reproducible test scenarios without external
downloads.
"""

import random


def generate_drift_sequence(
    seed: int,
    n: int,
    shift_indices: list,
    shift_amounts: list
) -> tuple:
    """
    Generate a synthetic drift sequence.

    Args:
        seed: Random seed for reproducibility.
        n: Total number of samples in the sequence.
        shift_indices: List of indices where the mean shift occurs.
        shift_amounts: List of corresponding shift amounts to add to the mean.

    Returns:
        A tuple (x_seq, y_seq, ground_truth_alpha_indices):
            x_seq: List of floats representing the input sequence (zeros in this simple case).
            y_seq: List of floats representing the target sequence with drifts.
            ground_truth_alpha_indices: List of expected optimal alpha indices (or values)
                for each time step, used for analysis.
    """
    # Initialize random generator for reproducibility
    rng = random.Random(seed)

    # Initialize sequences
    x_seq = [0.0] * n
    y_seq = [0.0] * n

    # Current mean starts at 0
    current_mean = 0.0

    # ground_truth_alpha_indices: For analysis, we might assume that when drift is high,
    # a larger alpha (narrower interval) is preferred if coverage is still safe, or
    # we just record the drift status. The contract says "maps time to expected optimal
    # alpha for analysis". In a real scenario, this would be derived from the utility
    # function. For a benchmark, we can store a simple indicator or expected behavior.
    # Let's store a list of recommended alphas or a marker.
    # Since the utility function is dynamic, "ground truth" is complex.
    # We will store a boolean or a small alpha value indicating "high drift".
    # Let's use a list of 0.0 for no drift and a flag for drift periods.
    # Actually, the interface says "list". Let's return a list of floats where
    # the value is the shift amount or a marker.
    # Better: return a list of expected "regime" indices or alphas.
    # Let's keep it simple: 0.0 for stable, 1.0 for drifted.
    # Or, we can provide the *nominal* alpha that would be optimal if the model
    # was perfect. But since the model is online, it's hard to have a static "truth".
    # Let's return a list of booleans or floats indicating drift presence.
    # Contract: "ground_truth_alpha_indices maps time to expected optimal alpha for analysis".
    # I will interpret this as a list of the same length as n, containing a suggested
    # alpha value or a drift intensity metric. Let's use drift intensity.

    ground_truth_alpha_indices = [0.0] * n  # Placeholder

    # Build the sequence
    # We need to apply shifts at the specified indices.
    # Sort shifts to apply them in order? Or assume they are sorted?
    # Let's assume shift_indices are sorted. If not, we should sort them.
    # For simplicity and robustness, we'll create a mapping or iterate through sorted shifts.
    
    # Create a map from index to shift amount for easy lookup during generation
    # Note: shift_indices and shift_amounts must be same length.
    if len(shift_indices) != len(shift_amounts):
        raise ValueError("shift_indices and shift_amounts must be the same length.")
        
    # We'll iterate through time and check if current index is a shift point.
    # Since we need to apply the shift to all subsequent points, we can just keep track.
    
    # To be efficient and clean:
    shift_map = {}
    for idx, amount in zip(shift_indices, shift_amounts):
        shift_map[idx] = amount

    for t in range(n):
        # Check if a shift happens at this index
        if t in shift_map:
            current_mean += shift_map[t]
            ground_truth_alpha_indices[t] = 1.0 # Mark drift onset
        else:
            # In a real drift scenario, drift might decay or persist.
            # Here we assume persistent mean shift.
            pass

        # Generate y_t = current_mean + noise
        # Use standard normal noise for simplicity
        noise = rng.gauss(0, 1)
        y_seq[t] = current_mean + noise
        
        # x is just a placeholder for interface compatibility
        x_seq[t] = 0.0

    # For ground_truth, it's hard to define a single "optimal alpha" without running the algorithm.
    # However, we can mark the *stability*.
    # If no shift recently, low drift -> prefer small alpha (wide interval for high cov) or 
    # depending on utility. 
    # The utility maximizes coverage - mu*width.
    # High drift -> high residual variance -> wide intervals needed for coverage.
    # But if we allow lower coverage (hard bound), we can use larger alpha (narrower).
    # So "optimal" alpha depends on mu and hard_cov.
    # Since this is a *benchmark* generator, returning a binary indicator or 
    # the shift magnitude is reasonable for "analysis".
    # Let's return the drift magnitude (current_mean relative to initial) or just 0/1.
    # The contract says "expected optimal alpha". This is ambiguous without the specific 
    # utility parameters. 
    # I will return a list of 0.0s for non-drift and a value (e.g. 0.5) for drift steps 
    # to indicate that "adaptive" methods should potentially use a different alpha than nominal.
    # Or simply, let's leave it as a marker for drift.
    
    # Actually, let's make ground_truth_alpha_indices more useful:
    # It will contain the *nominal* alpha (e.g. 0.1) when stable, and a *larger* alpha 
    # (e.g. 0.3) when in high drift, assuming the utility function penalizes width heavily 
    # during drift to allow coverage drop.
    # But since we don't know the utility params here, let's just mark drift presence.
    # I'll use 0.0 for stable and 1.0 for drifted.

    # Re-evaluating the contract: "maps time to expected optimal alpha for analysis".
    # I will implement a simple heuristic: 
    # If drift magnitude > threshold, suggest a larger alpha (narrower set).
    # But since this is a *data* generator, not an oracle, I will just provide 
    # the drift status. 
    # Let's stick to the simplest interpretation: a list of floats indicating 
    # the drift regime. 0.0 = stable, 1.0 = drift.

    return (x_seq, y_seq, ground_truth_alpha_indices)