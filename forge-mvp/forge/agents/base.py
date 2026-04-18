from forge.config import ForgeConfig
from forge.state import ForgeState


class BaseAgent:
    def __init__(self, config: ForgeConfig):
        self.config = config

    def run(self, state: ForgeState) -> ForgeState:
        raise NotImplementedError
