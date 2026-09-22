from models import OnlineMeanModel


class FixedAlphaConformal:
    """Fixed-alpha conformal predictor with synchronized model updates."""

    def __init__(self, model: OnlineMeanModel, alpha: float, window_size: int):
        self.model = model
        self.alpha = alpha
        self.window_size = window_size
        self.residual_window = []
        self._width = 0.0

    def step(self, x: float, y: float) -> float:
        """
        Perform one step of the conformal prediction loop:
        1. Predict using current model state (causal).
        2. Observe y, update model (synchronized).
        3. Compute residual, update window, recompute width.
        Returns the interval width for evaluation.
        """
        # 1. Causal prediction
        pred = self.model.predict(x)

        # 2. Synchronized update: observe y, update model
        self.model.update(x, y)

        # 3. Compute residual after model update
        pred_after = self.model.predict(x)
        s_t = abs(y - pred_after)

        # Update sliding window of residuals
        self.residual_window.append(s_t)
        if len(self.residual_window) > self.window_size:
            self.residual_window.pop(0)

        # Compute quantile-based width
        if len(self.residual_window) == 0:
            self._width = 0.0
        else:
            n = len(self.residual_window)
            k = max(1, int(n * (1 - self.alpha)))
            quantile = sorted(self.residual_window)[k - 1]
            self._width = 2 * quantile

        return self._width

    def get_width(self) -> float:
        return self._width


class ConformalPID:
    """Conformal PID controller (Angelopoulos et al., 2023) as advanced baseline."""

    def __init__(self, model: OnlineMeanModel, nominal_alpha: float, window_size: int, kp: float, ki: float):
        self.model = model
        self.nominal_alpha = nominal_alpha
        self.window_size = window_size
        self.kp = kp
        self.ki = ki
        self.alpha = nominal_alpha
        self.residual_window = []
        self.integral_error = 0.0
        self._width = 0.0
        self._coverage_history = []

    def step(self, x: float, y: float) -> float:
        """
        One step of PID-controlled conformal prediction:
        1. Predict with current alpha and model.
        2. Observe y, update model.
        3. Compute residual, update window, measure coverage.
        4. Adjust alpha via PID control to track nominal_alpha.
        Returns the interval width.
        """
        # 1. Causal prediction with current alpha
        pred = self.model.predict(x)

        # 2. Synchronized model update
        self.model.update(x, y)

        # 3. Compute residual after model update
        pred_after = self.model.predict(x)
        s_t = abs(y - pred_after)

        # Update residual window
        self.residual_window.append(s_t)
        if len(self.residual_window) > self.window_size:
            self.residual_window.pop(0)

        # 4. Compute current quantile and width for coverage check
        n = len(self.residual_window)
        if n == 0:
            self._width = 0.0
        else:
            k = max(1, int(n * (1 - self.alpha)))
            quantile = sorted(self.residual_window)[k - 1]
            self._width = 2 * quantile

        # 5. Check if y is covered by the current interval
        lower = pred - quantile
        upper = pred + quantile
        covered = 1.0 if (lower <= y <= upper) else 0.0

        # 6. Track empirical coverage over the window for PID feedback
        self._coverage_history.append(covered)
        if len(self._coverage_history) > self.window_size:
            self._coverage_history.pop(0)

        empirical_coverage = sum(self._coverage_history) / len(self._coverage_history) if self._coverage_history else 1.0
        target_coverage = 1.0 - self.nominal_alpha
        error = target_coverage - empirical_coverage

        # PID update for alpha
        self.integral_error += error
        pid_signal = self.kp * error + self.ki * self.integral_error
        self.alpha -= pid_signal

        # Clamp alpha to valid range
        self.alpha = max(0.01, min(0.99, self.alpha))

        return self._width

    def get_width(self) -> float:
        return self._width