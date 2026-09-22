import random
import math
import statistics

def generate_data(n_samples: int, n_outliers: int, outlier_sign: int, seed: int) -> list[float]:
    """
    Generates n_samples standard normal observations.
    Replaces n_outliers of them with sign * 8.0 to simulate contamination.
    """
    random.seed(seed)
    data = [random.gauss(0.0, 1.0) for _ in range(n_samples)]
    if n_outliers > 0:
        indices_to_replace = random.sample(range(n_samples), n_outliers)
        outlier_value = outlier_sign * 8.0
        for idx in indices_to_replace:
            data[idx] = outlier_value
    return data

def estimate_mean_sats(data: list[float], c: float) -> float:
    """
    Computes the SATS mean estimate using coefficient c.
    """
    n = len(data)
    if n == 0:
        return 0.0
    
    # 1. Compute sample mean and std
    mu = statistics.mean(data)
    # Handle case where std is 0 (all values same)
    try:
        s = statistics.stdev(data)
    except statistics.StatisticsError:
        s = 0.0

    if s == 0:
        return mu

    # 2. Standardize and compute skewness
    # g = (1/n) * sum((xi - mean)/std)^3
    z_values = [(x - mu) / s for x in data]
    g = (1.0 / n) * sum(z ** 3 for z in z_values)
    
    # 3. Clip g to [-5, 5]
    g = max(-5.0, min(5.0, g))

    # 4. Calculate trimming ratios
    # Total trim is fixed at 10% (0.10)
    # alpha_R = 0.05 + c * g
    # alpha_L = 0.05 - c * g
    alpha_r = 0.05 + c * g
    alpha_l = 0.05 - c * g

    # 5. Clip alpha_L and alpha_R to [0, 0.10]
    alpha_r = max(0.0, min(0.10, alpha_r))
    alpha_l = max(0.0, min(0.10, alpha_l))

    # 6. Fallback if either is 0? The algorithm says:
    # "If alpha_R == 0 or alpha_L == 0, use fixed 10% symmetric trimming as fallback"
    # However, symmetric trimming is just alpha_l=0.05, alpha_r=0.05.
    # If the calculation results in 0, it means we've pushed all trim to one side.
    # The prompt says "use fixed 10% symmetric trimming as fallback" which implies
    # if the asymmetric logic fails or degenerates, we stick to the safe symmetric default.
    # But wait, if g is very high, alpha_r becomes > 0.05 and alpha_l becomes < 0.05.
    # If c is large enough that alpha_l hits 0, we trim all 10% from the right.
    # The fallback instruction suggests that if the bounds are hit in a way that one side is 0,
    # we should perhaps revert to symmetric? Or does it mean if the computed values are invalid?
    # Let's re-read: "If α_R == 0 or α_L == 0, use fixed 10% symmetric trimming as fallback"
    # This seems to imply that if the dynamic calculation results in a zero on one side,
    # we discard that dynamic adjustment and use the standard 5/5 split.
    if alpha_r == 0.0 or alpha_l == 0.0:
        alpha_l = 0.05
        alpha_r = 0.05

    # 7. Determine indices to keep
    sorted_data = sorted(data)
    # Number of samples to trim
    n_trim_left = int(round(alpha_l * n))
    n_trim_right = int(round(alpha_r * n))
    
    # Ensure we don't trim more than we have
    if n_trim_left + n_trim_right >= n:
        # If trimming removes everything, fallback to median or plain mean? 
        # For n=128 and 10% total trim, this won't happen.
        # But for robustness:
        n_trim_left = n // 2 - 1
        n_trim_right = n // 2 - 1
        if n_trim_left + n_trim_right >= n:
             return statistics.median(sorted_data)

    start_idx = n_trim_left
    end_idx = n - n_trim_right
    trimmed_data = sorted_data[start_idx:end_idx]
    
    if not trimmed_data:
        return mu
        
    return statistics.mean(trimmed_data)

def estimate_mean_fixed_trimmed(data: list[float]) -> float:
    """
    Computes the standard 10% symmetric trimmed mean.
    """
    n = len(data)
    if n == 0:
        return 0.0
    
    sorted_data = sorted(data)
    # 10% symmetric trim means 5% left, 5% right
    # n_trim = 0.05 * n
    n_trim = int(round(0.05 * n))
    
    if 2 * n_trim >= n:
        return statistics.median(sorted_data)

    start_idx = n_trim
    end_idx = n - n_trim
    trimmed_data = sorted_data[start_idx:end_idx]
    
    return statistics.mean(trimmed_data)

def estimate_mean_plain(data: list[float]) -> float:
    """
    Computes the standard arithmetic mean.
    """
    if not data:
        return 0.0
    return statistics.mean(data)

def estimate_mean_median(data: list[float]) -> float:
    """
    Computes the median.
    """
    if not data:
        return 0.0
    return statistics.median(data)

def calibrate_c(n_repeats: int = 20, seed_base: int = 9999) -> float:
    """
    Performs calibration on disjoint synthetic data to find optimal c.
    Ensures disjoint seeds from test evaluation.
    
    Strategy:
    We want to find a 'c' that balances performance on clean data and contaminated data.
    Clean data: g ~ 0, so SATS ~ Symmetric Trimmed.
    Contaminated (Right-tail): g > 0, SATS should increase right trim to remove more positive outliers.
    
    We simulate 'c' values and evaluate MSE on a calibration set consisting of:
    1. Clean samples (expect g to be small, no harm in being symmetric)
    2. Right-tail contaminated samples (expect g to be large, want to trim more right)
    
    We use seeds that are clearly disjoint from the test evaluation (which typically
    uses seeds like 0-49 or specific ranges). We use seed_base + offset.
    
    Since we can't import core's generate_data with specific contamination params 
    that perfectly match the "8.0 intensity" without knowing the exact n_outliers 
    for the test scenarios (which are 0.05 and 0.15 rates for n=128), 
    we will use representative samples for calibration.
    
    Test Scenarios from Manifest:
    - n_samples = 128
    - Contamination rates: 0, 0.05, 0.15
    - Outlier intensity: 8.0
    - Positive outliers for "single-side positive contamination"
    
    We will calibrate on a mix of clean and positive-outlier data to find a c 
    that performs well on the "positive contamination" case without hurting the "clean" case.
    
    The manifest says: "Select or adjust parameters only using calibration repeats 
    disjoint from test seeds".
    
    We will test a grid of c values.
    """
    n_samples = 128
    outlier_intensity = 8.0
    
    # Grid of c values to test
    # c determines how aggressively we shift trim based on skew.
    # alpha_r = 0.05 + c * g
    # If g ~ 2 (strong positive skew), and we want alpha_r to be ~ 0.08, 
    # then 0.08 = 0.05 + c * 2 => c = 0.015.
    # If g ~ 2 and we want alpha_r to be ~ 0.10, c = 0.025.
    c_candidates = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
    
    # Generate calibration data sets
    # Use seeds that are far from 0-49 (test seeds).
    # Test seeds likely iterate 0..49 or similar. 
    # Manifest says "50 independent test seeds". 
    # Let's use seed_base (9999) + i for calibration.
    
    calib_clean_data = []
    calib_contam_data = []
    
    for i in range(n_repeats):
        seed_clean = seed_base + i * 100
        seed_contam = seed_base + i * 100 + 1
        
        # Clean data
        data_clean = generate_data(n_samples, 0, 0, seed_clean)
        calib_clean_data.append(data_clean)
        
        # Contaminated data (Positive outliers, representing the hard case)
        # 0.15 rate of 128 is ~19 outliers.
        n_outliers = int(0.15 * n_samples)
        data_contam = generate_data(n_samples, n_outliers, 1, seed_contam)
        calib_contam_data.append(data_contam)

    best_c = 0.0
    min_total_mse = float('inf')
    
    for c in c_candidates:
        mse_clean = 0.0
        mse_contam = 0.0
        
        for data in calib_clean_data:
            est = estimate_mean_sats(data, c)
            # True mean is 0.0
            mse_clean += (est - 0.0) ** 2
        
        for data in calib_contam_data:
            est = estimate_mean_sats(data, c)
            # True mean of the contaminated sample is not 0. 
            # Wait, the research direction says: "Must not switch target to estimating mean of contaminated mixture."
            # "Do not switch target to estimating the mean of the contaminated mixture distribution."
            # This implies the target is still the underlying clean mean (0.0).
            # Contamination is noise/attack.
            mse_contam += (est - 0.0) ** 2
        
        mse_clean /= n_repeats
        mse_contam /= n_repeats
        
        # We want to minimize a combination of both. 
        # The primary goal is to beat baselines in contamination while keeping clean efficiency.
        # A weighted average or just sum works for ranking c's.
        total_mse = mse_clean + mse_contam
        
        if total_mse < min_total_mse:
            min_total_mse = total_mse
            best_c = c
            
    return best_c