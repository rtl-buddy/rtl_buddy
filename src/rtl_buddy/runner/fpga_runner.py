import logging

logger = logging.getLogger(__name__)

from ..config.fpga import FpgaConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..runner.fpga_results import FpgaFailResults, FpgaResults, FpgaSkipResults
from ..tools.fpga_base import BaseFpga
from ..tools.fpga_openxc7 import OpenXc7Fpga
from ..tools.fpga_vivado import VivadoFpga


# Backend registry. Adding another flow (Quartus, ...) is a one-line
# entry here plus a BaseFpga subclass in tools/fpga_<tool>.py. The
# default stays "vivado" (FpgaConfigFile.tool): openxc7 covers only
# 7-series parts, so an open default would break every other platform
# out of the box — select it per run with `tool: openxc7`.
_FPGA_BACKENDS: dict[str, type[BaseFpga]] = {
    "vivado": VivadoFpga,
    "openxc7": OpenXc7Fpga,
}


class FpgaRunner:
    def __init__(
        self,
        name: str,
        root_cfg,
        fpga_cfg: FpgaConfig,
        suite_dir: str,
        reglvl_filter: int | None = None,
        emit_bitstream: bool = False,
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.fpga_cfg = fpga_cfg
        self.suite_dir = suite_dir
        self.reglvl_filter = reglvl_filter
        self.emit_bitstream = emit_bitstream

    def run(self) -> FpgaResults:
        log_event(
            logger,
            logging.DEBUG,
            "fpga_runner.start",
            runner=self.name,
            fpga=self.fpga_cfg.get_name(),
        )

        if self.reglvl_filter is not None:
            cfg_reglvl = self.fpga_cfg.get_reglvl(self.fpga_cfg.get_tool_name())
            if cfg_reglvl > self.reglvl_filter:
                return FpgaSkipResults(
                    name=self.name + "/results",
                    desc=f"reglvl {cfg_reglvl} above filter {self.reglvl_filter}",
                )

        tool_name = self.fpga_cfg.get_tool_name()
        tool_cfg = (
            self.root_cfg.get_fpga_tool_cfg(tool_name)
            if self.root_cfg is not None
            else None
        )
        executable = tool_cfg.get_executable() if tool_cfg is not None else tool_name

        backend_cls = _FPGA_BACKENDS.get(tool_name)
        if backend_cls is None:
            # A typo'd tool name is a config error, not a skippable
            # condition — surface it loudly (exit 2).
            raise FatalRtlBuddyError(
                f"fpga run '{self.fpga_cfg.get_name()}': unknown tool "
                f"'{tool_name}' (registered: {sorted(_FPGA_BACKENDS)})"
            )

        backend = backend_cls(
            name=f"{self.name}/{tool_name}",
            fpga_cfg=self.fpga_cfg,
            suite_dir=self.suite_dir,
            root_cfg=self.root_cfg,
            executable=executable,
            emit_bitstream=self.emit_bitstream,
        )
        return self._apply_timing_gate(backend.run())

    def _apply_timing_gate(self, result: FpgaResults) -> FpgaResults:
        """Convert a passing run with unmet timing into a FAIL when the run
        sets ``require-timing-met``.

        Backend-agnostic so it covers every backend uniformly. Only acts
        when the backend explicitly reported ``timing_met is False`` — a
        ``None`` (backend cannot measure timing) is never gated, since we
        cannot prove timing was missed. The routed metrics ride along on
        the failing result so a closure loop still sees them.
        """
        if not self.fpga_cfg.get_require_timing_met():
            return result
        res = result.results
        if res.get("result") != "PASS" or res.get("timing_met") is not False:
            return result
        wns = res.get("wns_ns")
        metrics = {k: v for k, v in res.items() if k not in ("result", "name", "desc")}
        log_event(
            logger,
            logging.WARNING,
            "fpga.timing_gate_failed",
            fpga=self.fpga_cfg.get_name(),
            wns_ns=wns,
            failing_endpoints=res.get("failing_endpoints"),
        )
        desc = "timing not met"
        if wns is not None:
            desc += f" (WNS={wns} ns)"
        desc += " — require-timing-met is set for this run"
        return FpgaFailResults(name=self.name + "/results", desc=desc, metrics=metrics)
