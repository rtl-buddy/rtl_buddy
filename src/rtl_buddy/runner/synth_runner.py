import logging

logger = logging.getLogger(__name__)

from ..config.synth import SynthConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..runner.synth_results import SynthResults
from ..tools.synth_openroad import OpenRoadSynth
from ..tools.synth_yosys import YosysSynth


class SynthRunner:
    def __init__(
        self,
        name: str,
        root_cfg,
        synth_cfg: SynthConfig,
        suite_dir: str,
        effort_override: str | None = None,
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.synth_cfg = synth_cfg
        self.suite_dir = suite_dir
        self.effort_override = effort_override

    def run(self) -> SynthResults:
        log_event(
            logger,
            logging.DEBUG,
            "synth_runner.start",
            runner=self.name,
            synth=self.synth_cfg.get_name(),
        )
        tool_name = self.synth_cfg.get_tool_name()
        tool_cfg = self.root_cfg.get_synth_tool_cfg(tool_name)

        effort_name = self.effort_override or self.synth_cfg.get_effort_name()
        effort_cfg = self.root_cfg.get_synth_effort_cfg(effort_name)
        log_event(
            logger,
            logging.DEBUG,
            "synth_runner.effort",
            synth=self.synth_cfg.get_name(),
            effort=effort_cfg.get_name(),
        )

        # When the effort disables OpenROAD, fall back to the Yosys backend
        # even if the synth.yaml selected tool: openroad.
        if tool_name == "openroad" and effort_cfg.get_openroad_run():
            yosys_exe = "yosys"
            try:
                yosys_tool = self.root_cfg.get_synth_tool_cfg("yosys")
                yosys_exe = yosys_tool.get_executable()
            except FatalRtlBuddyError:
                pass
            backend = OpenRoadSynth(
                name=self.name + "/openroad",
                synth_cfg=self.synth_cfg,
                tool_cfg=tool_cfg,
                suite_dir=self.suite_dir,
                root_cfg=self.root_cfg,
                yosys_executable=yosys_exe,
                effort_cfg=effort_cfg,
            )
        else:
            # Yosys-only: either tool: yosys, or tool: openroad with effort.run=False
            if tool_name == "openroad":
                try:
                    tool_cfg = self.root_cfg.get_synth_tool_cfg("yosys")
                except FatalRtlBuddyError:
                    pass
            backend = YosysSynth(
                name=self.name + "/yosys",
                synth_cfg=self.synth_cfg,
                tool_cfg=tool_cfg,
                suite_dir=self.suite_dir,
                root_cfg=self.root_cfg,
                effort_cfg=effort_cfg,
            )
        return backend.run()
