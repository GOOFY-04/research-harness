class OnlineMeanModel:
    def __init__(self, learning_rate: float = 0.1):
        self.learning_rate = learning_rate
        self.mean = 0.0
        self._count = 0

    def predict(self, x: float) -> float:
        # Causal prediction: uses only the current state (exponential moving average of y's).
        # For a simple mean model, the prediction is the current estimated mean.
        # The input x is provided for interface compatibility but is not used in a basic mean estimator,
        # as the model learns the marginal distribution of y.
        return self.mean

    def update(self, x: float, y: float) -> None:
        """
        Synchronizes the model state with the new observation y.
        Uses an exponential moving average (EMA) to update the mean.
        The first update initializes the mean, subsequent updates use the EMA formula.
        """
        if self._count == 0:
            self.mean = y
        else:
            # Exponential moving average update:
            # mean_{t} = (1 - lr) * mean_{t-1} + lr * y_t
            self.mean = (1 - self.learning_rate) * self.mean + self.learning_rate * y
        self._count += 1

    def get_mean(self) -> float:
        """Exposes the current state of the model for diagnostics."""
        return self.mean