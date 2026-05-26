import logging

logger = logging.getLogger(__name__)

from ..config.power import PowerConfig
from ..logging_utils import log_event
from ..runner.power_results import PowerResults, PowerSkipResults
from ..tools.power_base import BasePower
from ..tools.power_openroad import OpenRoadPower


# Backend registry. Adding a commercial flow (PrimePower, Joules,
# Voltus, ...) is a one-line entry here plus a BasePower subclass in
# tools/power_<tool>.py.
_POWER_BACKENDS: dict[str, type[BasePower]] = {
    "openroad": OpenRoadPower,
}


class PowerRunner:
    def __init__(
        self,
        name: str,
        root_cfg,
        power_cfg: PowerConfig,
        suite_dir: str,
        reglvl_filter: int | None = None,
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.power_cfg = power_cfg
        self.suite_dir = suite_dir
        self.reglvl_filter = reglvl_filter

    def run(self) -> PowerResults:
        log_event(
            logger,
            logging.DEBUG,
            "power_runner.start",
            runner=self.name,
            power=self.power_cfg.get_name(),
        )

        if self.reglvl_filter is not None:
            cfg_reglvl = self.power_cfg.get_reglvl(self.power_cfg.get_tool_name())
            if cfg_reglvl > self.reglvl_filter:
                return PowerSkipResults(
                    name=self.name + "/results",
                    desc=f"reglvl {cfg_reglvl} above filter {self.reglvl_filter}",
                )

        tool_name = self.power_cfg.get_tool_name()
        tool_cfg = (
            self.root_cfg.get_power_tool_cfg(tool_name)
            if self.root_cfg is not None
            else None
        )
        executable = tool_cfg.get_executable() if tool_cfg is not None else tool_name

        backend_cls = _POWER_BACKENDS.get(tool_name)
        if backend_cls is None:
            return PowerSkipResults(
                name=self.name + "/results",
                desc=(
                    f"unsupported power tool '{tool_name}' "
                    f"(registered: {sorted(_POWER_BACKENDS)})"
                ),
            )

        backend = backend_cls(
            name=f"{self.name}/{tool_name}",
            power_cfg=self.power_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
            executable=executable,
        )
        return backend.run()
