"""Runs one FPV verification with the tool backend (currently only sby)."""

import logging

from ..config.fpv import FpvConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..runner.fpv_results import FpvResults
from ..tools.sby_fpv import SbyFpv

logger = logging.getLogger(__name__)


class FpvRunner:
    def __init__(self, name: str, root_cfg, fpv_cfg: FpvConfig, suite_dir: str):
        self.name = name
        self.root_cfg = root_cfg
        self.fpv_cfg = fpv_cfg
        self.suite_dir = suite_dir

    def run(self) -> FpvResults:
        log_event(
            logger,
            logging.DEBUG,
            "fpv_runner.start",
            runner=self.name,
            fpv=self.fpv_cfg.get_name(),
        )
        tool_name = self.fpv_cfg.get_tool_name()
        try:
            tool_cfg = self.root_cfg.get_fpv_tool_cfg(tool_name)
        except FatalRtlBuddyError:
            raise

        backend = SbyFpv(
            name=self.name + "/sby",
            fpv_cfg=self.fpv_cfg,
            tool_cfg=tool_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
        )
        return backend.run()
