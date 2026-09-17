class AlwaysHoldPolicy:
    def predict(self, observation, deterministic=True):
        return 0
