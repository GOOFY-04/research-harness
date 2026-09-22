import math

from models import OnlineMeanModel


class ACPF_UT:
    """
    Adaptive Conformal Prediction with Utility-Tradeoff (ACPF-UT).
    Implements a utility-based alpha selection mechanism that balances
    coverage and width, modulated by drift intensity (CUSUM).
    """

    def __init__(
        self,
        model: OnlineMeanModel,
        candidate_alphas: list,
        lambda_cov: float,
        mu_width: float,
        hard_cov: float,
        window_size: int,
        drift_window: int,
        large_penalty: float = 1e6
    ):
        """
        Initialize ACPF-UT.

        Args:
            model: The synchronized online learner (base model).
            candidate_alphas: List of candidate alpha values to consider.
            lambda_cov: Weight for the coverage term in the utility function.
            mu_width: Weight for the width penalty in the utility function.
            hard_cov: Hard lower bound for coverage (safety constraint).
            window_size: Size of the sliding window for residuals.
            drift_window: Window size for CUSUM drift detection.
            large_penalty: Penalty value when coverage falls below hard_cov.
        """
        self.model = model
        self.candidate_alphas = candidate_alphas
        self.lambda_cov = lambda_cov
        self.mu_width = mu_width
        self.hard_cov = hard_cov
        self.window_size = window_size
        self.drift_window = drift_window
        self.large_penalty = large_penalty

        # Residual window
        self.residual_window = []

        # CUSUM state
        self.cusum = 0.0
        self.cusum_mean = 0.0
        self.cusum_delta = 0.5  # Threshold for reset/normalization

        # Current alpha (starts with the median of candidates or first)
        self.current_alpha = candidate_alphas[0] if candidate_alphas else 0.1

        # Last computed width
        self._width = 0.0

        # Calibration constant for CUSUM drift intensity modulation
        # We estimate a baseline residual standard deviation to scale CUSUM
        self._res_std_est = 1.0

    def _update_cusum(self, residual: float) -> None:
        """
        Update CUSUM statistic to detect drift in residuals.
        """
        # Update running mean and std estimate for scaling
        # This is a simplified online estimate.
        if len(self.residual_window) >= 2:
            residuals_in_window = self.residual_window[-min(self.drift_window, len(self.residual_window)):]
            mean_r = sum(residuals_in_window) / len(residuals_in_window)
            var_r = sum((r - mean_r) ** 2 for r in residuals_in_window) / (len(residuals_in_window) - 1)
            std_r = math.sqrt(var_r) if var_r > 0 else 1.0
            self._res_std_est = std_r

        # Normalize residual for CUSUM
        if self._res_std_est > 1e-6:
            normalized_res = residual / self._res_std_est
        else:
            normalized_res = residual

        # CUSUM update: S_t = max(0, S_{t-1} + normalized_res - delta)
        # This detects positive shifts in residuals (drift).
        # We use a small delta to avoid constant triggering.
        self.cusum_delta = 0.5  # Fixed threshold for step in CUSUM
        self.cusum = max(0.0, self.cusum + normalized_res - self.cusum_delta)

    def _get_modulated_mu(self) -> float:
        """
        Compute the modulated width penalty coefficient mu.
        Higher drift intensity (larger CUSUM) increases mu,
        making the algorithm more aggressive in reducing width
        (allowing lower coverage, as long as above hard_cov).
        """
        # Scale mu based on drift intensity.
        # A simple linear scaling: mu_modulated = mu_width * (1 + drift_factor)
        # drift_factor is a normalized version of CUSUM.
        if self._res_std_est > 1e-6:
            drift_factor = min(1.0, self.cusum / (2.0 * self._res_std_est))
        else:
            drift_factor = 0.0

        return self.mu_width * (1.0 + drift_factor)

    def _compute_quantile(self, data: list, alpha: float) -> float:
        """
        Compute the (1 - alpha) quantile of the data.
        If data is empty, return 0.0.
        """
        if not data:
            return 0.0
        n = len(data)
        sorted_data = sorted(data)
        # Use the same quantile definition as baselines for consistency
        k = max(1, int(n * (1 - alpha)))
        if k > n:
            k = n
        return sorted_data[k - 1]

    def _compute_empirical_coverage(self, data: list, quantile_val: float) -> float:
        """
        Compute empirical coverage of the interval [-quantile_val, quantile_val]
        on the given residual data.
        """
        if not data:
            return 1.0
        covered = sum(1 for r in data if abs(r) <= quantile_val)
        return covered / len(data)

    def step(self, x: float, y: float) -> float:
        """
        Execute one step of the ACPF-UT algorithm.

        Args:
            x: Input feature (used for model compatibility).
            y: Observed target value.

        Returns:
            The interval width for the current prediction.
        """
        # Phase 1: Causal Prediction
        # Use the previous alpha and model state to predict.
        pred = self.model.predict(x)

        # Compute previous quantile using current window and current_alpha
        # Note: The algorithm description says "q_prev <- Quantile(H_{t-1}, 1 - alpha_prev)".
        # Since we update H in phase 2, we must use the window state before adding new residual.
        q_prev = self._compute_quantile(self.residual_window, self.current_alpha)
        
        # The width returned is for the prediction interval C_t = [pred - q, pred + q]
        # But we need to return the width *after* updating the state and selecting the new alpha?
        # The interface says "step ... returns interval width".
        # In baselines, step updates state and returns width based on *updated* window?
        # Let's check baselines:
        # baselines.FixedAlphaConformal.step:
        # 1. predict
        # 2. update model
        # 3. compute residual, update window
        # 4. compute quantile from *new* window
        # 5. return width from *new* window
        #
        # The ACPF-UT algorithm description says:
        # 3. Causal Prediction (uses info up to t-1) ... C_t <- [pred_mean - q_prev, ...]
        # 2. Synchronized Update ...
        # 3. Utility-Based Alpha Selection ...
        #
        # There is a nuance: Does the returned width correspond to the interval used
        # to cover y_t (which is calculated in Phase 1), or the interval that will
        # be used for the next prediction?
        # Usually, "predict" returns the interval for the current point.
        # However, in online conformal prediction, the "calibration" for the current
        # point is done using past data.
        #
        # Let's align with the "Utility-Based Alpha Selection" phase.
        # The alpha selection happens *after* updating the residual window with s_t.
        # This new alpha will be used for the *next* prediction.
        #
        # But the method `step` is called with (x, y). It should ideally:
        # 1. Predict using current state.
        # 2. Update state with y.
        # 3. Return width.
        #
        # If we return the width based on the *newly selected* alpha, it represents
        # the width of the interval that *would have covered* y if we had known the
        # optimal alpha for the next step? Or the width used to cover y?
        #
        # Let's look at the baselines again. FixedAlpha returns width based on the
        # updated window. This width corresponds to the interval centered at the
        # *post-update* model mean? No, the model is updated, so the mean has changed.
        #
        # Let's stick to the causal order:
        # 1. pred = model.predict(x) (pre-update)
        # 2. Update model, compute s_t, update window, update CUSUM.
        # 3. Select new_alpha using utility.
        # 4. Compute width using new_alpha and current window?
        #
        # Actually, the width of the prediction set C_t is determined by q_prev.
        # The algorithm says "C_t <- [pred_mean - q_prev, pred_mean + q_prev]".
        # So the width of C_t is 2 * q_prev.
        #
        # However, if we return 2 * q_prev, we are returning the width used to cover y_t.
        # This is consistent with "evaluation on held-out segments" where we want to know
        # how wide the intervals were that actually covered the points.
        #
        # Let's calculate width = 2 * q_prev for the return value, but also update
        # self._width to reflect the new alpha's width for diagnostics/future steps?
        #
        # The interface says "step ... returns interval width".
        # And "get_width" returns self._width.
        #
        # To be consistent with the baselines (which return the width based on the
        # *updated* window statistics, but applied to the *pre-update* prediction?),
        # let's re-read baselines carefully.
        #
        # Baseline step:
        # pred = model.predict(x)
        # model.update(x, y)
        # pred_after = model.predict(x)  # <-- This is different!
        # s_t = abs(y - pred_after)
        #
        # This baseline calculates residual against the *post-update* model.
        # This is a bit unusual. Standard conformal uses residuals from a fixed model or
        # the model state *before* update for the split.
        #
        # The design document says:
        # "Synchronized Online Learner ... In the pseudocode, model.update(y_t) must be
        # inserted before calculating s_t ... ensuring the conformal interval is built
        # on the error of the 'adapted' model."
        #
        # So s_t = |y_t - M_new(x_t)|.
        #
        # Okay, I will follow the design document strictly:
        # 1. pred_prev = M_old.predict(x)
        # 2. M_new = M_old.update(x, y)
        # 3. s_t = |y - M_new.predict(x)|
        # 4. Update H with s_t.
        # 5. Select alpha_new using H.
        # 6. Return width. Which width?
        #    The width of C_t is 2 * q_prev (from H_old).
        #    But if we want to return the width of the *current* state's potential
        #    prediction, it would be 2 * q_new (from H_new, alpha_new).
        #
        # Most online algorithms return the width of the interval that was used
        # to cover the current point, i.e., 2 * q_prev.
        #
        # Let's compute q_prev, perform updates, select alpha_new, compute q_new.
        # We will store q_new * 2 as self._width (for get_width).
        # We will return 2 * q_prev (the width of the interval that actually covered y).
        #
        # Wait, if H is empty, q_prev is 0. Width 0.
        #
        # Let's trace the utility selection.
        # "q_a <- Quantile(H, 1 - a)" -> Uses H (the *updated* window).
        # "w_a <- 2 * q_a"
        # "c_a <- EmpiricalCoverage(H, q_a)"
        #
        # So the selection is based on the *updated* window H_t.
        
        q_prev = self._compute_quantile(self.residual_window, self.current_alpha)
        
        # Phase 2: Synchronized Update
        self.model.update(x, y)
        pred_after = self.model.predict(x)
        s_t = abs(y - pred_after)
        
        self.residual_window.append(s_t)
        if len(self.residual_window) > self.window_size:
            self.residual_window.pop(0)
            
        # Update CUSUM
        self._update_cusum(s_t)
        
        # Phase 3: Utility-Based Alpha Selection
        mu_mod = self._get_modulated_mu()
        
        best_alpha = self.current_alpha
        best_utility = -float('inf')
        
        for a in self.candidate_alphas:
            q_a = self._compute_quantile(self.residual_window, a)
            w_a = 2.0 * q_a
            c_a = self._compute_empirical_coverage(self.residual_window, q_a)
            
            if c_a < self.hard_cov:
                u_a = -self.large_penalty
            else:
                u_a = self.lambda_cov * c_a - mu_mod * w_a
            
            if u_a > best_utility:
                best_utility = u_a
                best_alpha = a
                
        self.current_alpha = best_alpha
        
        # Compute width for the *current* state (for get_width and potential future use)
        q_current = self._compute_quantile(self.residual_window, self.current_alpha)
        self._width = 2.0 * q_current
        
        # Return the width of the interval that was used to cover y_t (causal width)
        # Or should it return the new width?
        # "step ... returns interval width"
        # In the context of logging/evaluation, we usually log the width of the set
        # that included y_t. That is 2 * q_prev.
        # However, if q_prev was computed from an empty window, it's 0.
        #
        # Let's look at the baseline again. It returns self._width, which is based on
        # the *updated* window.
        # For consistency with the baseline interface, let's return self._width (new).
        # The "causal" aspect is that the *prediction center* was pred_prev.
        # But the *calibration* is always updated.
        #
        # Actually, looking at the design:
        # "C_t <- [pred_mean - q_prev, pred_mean + q_prev]"
        # The width of C_t is 2*q_prev.
        #
        # If we return 2*q_prev, we reflect the actual uncertainty quantified at step t.
        # If we return 2*q_current, we reflect the uncertainty for step t+1.
        #
        # Standard practice in online CP papers is to report the width of the set
        # generated for the current sample. So 2 * q_prev.
        
        return 2.0 * q_prev

    def get_width(self) -> float:
        """
        Return the last computed interval width based on the current state.
        """
        return self._width

    def get_current_alpha(self) -> float:
        """
        Return the currently selected alpha value.
        """
        return self.current_alpha