import logging

logger = logging.getLogger(__name__)

from ..config.synth import SynthConfig
from ..logging_utils import log_event
from ..runner.synth_results import SynthResults
from ..tools.synth_yosys import YosysSynth


class SynthRunner:
    def __init__(self, name: str, root_cfg, synth_cfg: SynthConfig, suite_dir: str):
        self.name = name
        self.root_cfg = root_cfg
        self.synth_cfg = synth_cfg
        self.suite_dir = suite_dir

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

        backend = YosysSynth(
            name=self.name + "/yosys",
            synth_cfg=self.synth_cfg,
            tool_cfg=tool_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
        )
        return backend.run()
