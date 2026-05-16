"""Abstract contract for power-analysis backends.

Adding a new backend (PrimePower, Joules, Voltus) is:
  1. Subclass `BasePower` and implement `run()` returning a `PowerResults`.
  2. Register the class in `runner/power_runner.py::_POWER_BACKENDS`.

Shared resolution logic (activity-source selection, results envelope)
lives on `config.power.PowerConfig` so every backend agrees on what the
user asked for and only diverges on tool-specific command emission.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config.power import PowerConfig
from ..runner.power_results import PowerResults


class BasePower(ABC):
    def __init__(
        self,
        name: str,
        power_cfg: PowerConfig,
        suite_dir: str,
        root_cfg,
        executable: str,
    ):
        self.name = name
        self.power_cfg = power_cfg
        self.suite_dir = suite_dir
        self.root_cfg = root_cfg
        self.executable = executable

    @abstractmethod
    def run(self) -> PowerResults:  # pragma: no cover - abstract
        ...
