from forge.config import ForgeConfig
from forge.state import ForgeState


class BaseReviewer:
    def __init__(self, config: ForgeConfig):
        self.config = config

    def review(self, state: ForgeState) -> ForgeState:
        raise NotImplementedError
