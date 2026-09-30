"""Abstract base class for power-analysis backends.

A backend subclasses `BasePower`, implements `run()` and is registered in `runner/power_runner.py::_POWER_BACKENDS`. Activity-source resolution lives on `config.power.PowerConfig`.
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
    def run(self) -> PowerResults:  # pragma: no cover
        ...
