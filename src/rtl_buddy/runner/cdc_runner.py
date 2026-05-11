"""Per-analysis CDC runner — picks the right tool wrapper based on the
analysis config's ``tool`` field and delegates."""

import logging

from ..config.cdc import CdcConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..runner.cdc_results import CdcResults
from ..tools.cdc_rtl_buddy import RtlBuddyCdc

logger = logging.getLogger(__name__)


class CdcRunner:
    def __init__(self, name: str, root_cfg, cdc_cfg: CdcConfig, suite_dir: str):
        self.name = name
        self.root_cfg = root_cfg
        self.cdc_cfg = cdc_cfg
        self.suite_dir = suite_dir

    def run(self) -> CdcResults:
        log_event(
            logger,
            logging.DEBUG,
            "cdc_runner.start",
            runner=self.name,
            cdc=self.cdc_cfg.get_name(),
        )
        tool_name = self.cdc_cfg.get_tool_name()
        try:
            tool_cfg = self.root_cfg.get_cdc_tool_cfg(tool_name)
        except FatalRtlBuddyError:
            raise

        # Today only one backend (rtl-buddy-cdc); structured for easy
        # extension when alternative CDC tools are added.
        backend = RtlBuddyCdc(
            name=self.name + "/rtl_buddy_cdc",
            cdc_cfg=self.cdc_cfg,
            tool_cfg=tool_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
        )
        return backend.run()
