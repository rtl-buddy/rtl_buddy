"""Runs one CDC analysis with the backend named by the analysis config's ``tool`` field."""

import logging

from ..config.cdc import CdcConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..runner.cdc_results import CdcResults
from ..tools.cdc_rtl_buddy import RtlBuddyCdc
from ..tools.cdc_vivado import VivadoCdc

logger = logging.getLogger(__name__)


# Keyed on the analysis config's ``tool:`` field, which must also name a ``cfg-cdc-tools`` entry.
_CDC_BACKENDS: dict[str, type] = {
    "rtl-buddy-cdc": RtlBuddyCdc,
    "vivado": VivadoCdc,
}


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

        backend_cls = _CDC_BACKENDS.get(tool_name)
        if backend_cls is None:
            # A config error, not a skippable condition.
            raise FatalRtlBuddyError(
                f"CDC analysis '{self.cdc_cfg.get_name()}': unknown tool "
                f"'{tool_name}' (registered: {sorted(_CDC_BACKENDS)})"
            )

        backend = backend_cls(
            name=f"{self.name}/{tool_name}",
            cdc_cfg=self.cdc_cfg,
            tool_cfg=tool_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
        )
        return backend.run()
