import logging

logger = logging.getLogger(__name__)

from ..config.pnr import PnrConfig
from ..logging_utils import log_event
from ..runner.pnr_results import PnrResults, PnrSkipResults
from ..tools.pnr_openroad import (
    DEFAULT_PNG_HEIGHT,
    DEFAULT_PNG_WIDTH,
    OpenRoadPnr,
)


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
        gds_mode: str | None = None,
        accept_stale: bool = False,
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.pnr_cfg = pnr_cfg
        self.suite_dir = suite_dir
        self.reglvl_filter = reglvl_filter
        self.accept_stale = accept_stale
        self.emit_gds = emit_gds
        self.emit_png = emit_png
        # `None` leaves each run to its own `gds-mode:`; `--gds-mode`
        # overrides every run in the invocation.
        self.gds_mode = gds_mode

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
            gds_mode=self.gds_mode,
            accept_stale=self.accept_stale,
        )
        return backend.run()


class PnrExportRunner:
    """Export one saved P&R result's layout, running no P&R at all (#618).

    A sibling of :class:`PnrRunner` rather than a flag on it. The two share
    the run selection and the backend object and nothing else, and what
    keeps `rb pnr-export` from launching OpenROAD or synthesis is
    structural: this calls :meth:`OpenRoadPnr.export_only`, never
    :meth:`OpenRoadPnr.run`, and — unlike the runner above — it never even
    resolves the P&R tool's executable, which is the point on a host where
    the collateral arrived after the P&R did.

    The unsupported-tool skip is kept, because the artefact layout the
    export reads is the OpenROAD backend's.
    """

    def __init__(
        self,
        name: str,
        root_cfg,
        pnr_cfg: PnrConfig,
        suite_dir: str,
        reglvl_filter: int | None = None,
        emit_png: bool = False,
        gds_mode: str | None = None,
        def_path: str | None = None,
        png_only: bool = False,
        klayout_props: str | None = None,
        png_width: int = DEFAULT_PNG_WIDTH,
        png_height: int = DEFAULT_PNG_HEIGHT,
        checkpoint: str | None = None,
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.pnr_cfg = pnr_cfg
        self.suite_dir = suite_dir
        self.reglvl_filter = reglvl_filter
        self.checkpoint = checkpoint
        # A re-render is a PNG by definition; otherwise `--png` asks for one.
        self.emit_png = emit_png or png_only
        self.gds_mode = gds_mode
        self.def_path = def_path
        self.png_only = png_only
        self.klayout_props = klayout_props
        self.png_width = png_width
        self.png_height = png_height

    def run(self) -> PnrResults:
        log_event(
            logger,
            logging.DEBUG,
            "pnr_export_runner.start",
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
        if tool_name != "openroad":
            return PnrSkipResults(
                name=self.name + "/results",
                desc=f"unsupported pnr tool '{tool_name}' (only 'openroad' today)",
            )

        backend = OpenRoadPnr(
            name=self.name + "/export",
            pnr_cfg=self.pnr_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
            emit_gds=True,
            emit_png=self.emit_png,
            gds_mode=self.gds_mode,
            # An export streams the layouts that were routed; whether the
            # blocks' sources have moved on since is the P&R run's question.
            accept_stale=True,
            klayout_props=self.klayout_props,
            png_width=self.png_width,
            png_height=self.png_height,
        )
        return backend.export_only(
            def_path=self.def_path, png_only=self.png_only, checkpoint=self.checkpoint
        )
