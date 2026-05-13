import logging

logger = logging.getLogger(__name__)

from ..config.pnr import PnrConfig
from ..logging_utils import log_event
from ..runner.pnr_results import PnrResults, PnrSkipResults
from ..tools.pnr_openroad import OpenRoadPnr


class PnrRunner:
    def __init__(
        self,
        name: str,
        root_cfg,
        pnr_cfg: PnrConfig,
        suite_dir: str,
        reglvl_filter: int | None = None,
        emit_gds: bool = False,
        emit_png: bool = False,
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.pnr_cfg = pnr_cfg
        self.suite_dir = suite_dir
        self.reglvl_filter = reglvl_filter
        self.emit_gds = emit_gds
        self.emit_png = emit_png

    def run(self) -> PnrResults:
        log_event(
            logger,
            logging.DEBUG,
            "pnr_runner.start",
            runner=self.name,
            pnr=self.pnr_cfg.get_name(),
        )

        if self.reglvl_filter is not None:
            cfg_reglvl = self.pnr_cfg.get_reglvl(self.pnr_cfg.get_tool_name())
            if cfg_reglvl > self.reglvl_filter:
                return PnrSkipResults(
                    name=self.name + "/results",
                    desc=(f"reglvl {cfg_reglvl} above filter {self.reglvl_filter}"),
                )

        tool_name = self.pnr_cfg.get_tool_name()
        tool_cfg = (
            self.root_cfg.get_pnr_tool_cfg(tool_name)
            if self.root_cfg is not None
            else None
        )
        executable = tool_cfg.get_executable() if tool_cfg is not None else tool_name
        if tool_name != "openroad":
            return PnrSkipResults(
                name=self.name + "/results",
                desc=f"unsupported pnr tool '{tool_name}' (only 'openroad' today)",
            )

        backend = OpenRoadPnr(
            name=self.name + "/openroad",
            pnr_cfg=self.pnr_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
            openroad_executable=executable,
            emit_gds=self.emit_gds,
            emit_png=self.emit_png,
        )
        return backend.run()
