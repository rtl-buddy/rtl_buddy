import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_filelist import VlogFilelist
from ..config.synth import SynthConfig, SynthToolConfig
from ..errors import FilelistError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.synth_results import SynthFailResults, SynthPassResults, SynthResults

# Default ABC script for liberty without timing constraint
_ABC_SCRIPT_NO_TIMING = (
    "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
    "&get -n; &dch -f; &nf {D}; &put"
)
# Same but with stime -p appended to report critical-path delay
_ABC_SCRIPT_WITH_TIMING = _ABC_SCRIPT_NO_TIMING + "; stime -p"


class YosysSynth:
    def __init__(
        self,
        name: str,
        synth_cfg: SynthConfig,
        tool_cfg: SynthToolConfig,
        suite_dir: str,
        root_cfg=None,
    ):
        self.name = name
        self.synth_cfg = synth_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg

        artefact_root = Path(suite_dir) / "artefacts" / synth_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)
        self._period_ps: int | None = None

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.f")

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.ys")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.log")

    def _netlist_path(self, mapped: bool = False) -> str:
        if mapped:
            return os.path.join(self.artefact_dir, "synth_netlist.v")
        return os.path.join(self.artefact_dir, "synth.rtlil")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.synth_cfg.get_model(),
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _source_files_from_filelist(self, fl_path: str) -> list[str]:
        """Return absolute source file paths from a (possibly stripped) filelist."""
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        _SKIP = ("+incdir+", "+libext+", "-y ", "-F ", "-f ")
        _SOURCE_PREFIX = "-v "
        paths = []
        with open(fl_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                if any(line.startswith(opt) for opt in _SKIP):
                    continue
                if line.startswith(_SOURCE_PREFIX):
                    line = line[len(_SOURCE_PREFIX) :]
                paths.append(os.path.normpath(os.path.join(fl_dir, line)))
        return paths

    def _parse_clock_period_ps(self, sdc_path: str) -> int | None:
        """Extract create_clock periods from SDC and return the minimum in picoseconds.

        ABC -D takes a single timing window; for multi-clock designs this is a
        workaround — the minimum period is used, which over-constrains slower domains.
        """
        periods = []
        try:
            with open(sdc_path) as f:
                for line in f:
                    m = re.search(r"create_clock\s+.*-period\s+([\d.]+)", line)
                    if m:
                        periods.append(float(m.group(1)))
        except OSError:
            return None
        if not periods:
            return None
        if len(periods) > 1:
            log_event(
                logger,
                logging.WARNING,
                "synth.sdc_multi_clock",
                synth=self.synth_cfg.get_name(),
                clocks=len(periods),
                periods_ns=periods,
                used_ns=min(periods),
                sdc=sdc_path,
            )
        return int(min(periods) * 1000)

    def _resolve_lib_paths(self) -> list[str]:
        libraries = self.synth_cfg.get_libraries()
        if not libraries or self.root_cfg is None:
            return []
        return [self.root_cfg.get_synth_lib_cfg(name).get_path() for name in libraries]

    def _parse_area_um2(self, log_text: str) -> float | None:
        m = re.search(r"Chip area for module[^:]*:\s*([\d.]+)", log_text)
        return float(m.group(1)) if m else None

    def _parse_gate_count(self, log_text: str) -> int | None:
        matches = re.findall(
            r"^\s+(\d+)\s+(?:[\d.]+\s+)?cells$", log_text, re.MULTILINE
        )
        return int(matches[-1]) if matches else None

    def _parse_critical_path_ps(self, log_text: str) -> float | None:
        m = re.search(r"Delay\s*=\s*([\d.]+)\s*ps", log_text)
        return float(m.group(1)) if m else None

    def _write_script(self, fl_path: str) -> str:
        top = self.synth_cfg.get_top()
        overrides = self.synth_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        opts = self.tool_cfg.get_opts(overrides)
        params = self.synth_cfg.get_params()
        lib_paths = self._resolve_lib_paths()
        mapped = bool(lib_paths)

        defines = self.synth_cfg.get_defines()
        define_flags = ""
        if defines:
            define_flags = " " + " ".join(f"-D {k}={v}" for k, v in defines.items())

        lines = []
        for lib in lib_paths:
            lines.append(f"read_liberty -lib {lib}")

        source_files = self._source_files_from_filelist(fl_path)
        for src in source_files:
            lines.append(f"read_verilog -sv -defer{define_flags} {src}")

        if params:
            for key, value in params.items():
                lines.append(f"chparam -set {key} {value} {top}")

        synth_cmd = f"synth -top {top}"
        if opts.synth_args:
            synth_cmd += f" {opts.synth_args}"
        lines.append(synth_cmd)

        if mapped:
            for lib in lib_paths:
                lines.append(f"dfflibmap -liberty {lib}")

            abc_cmd = f"abc -liberty {lib_paths[0]}"
            constraints = self.synth_cfg.get_constraints()
            period_ps = None
            if constraints:
                period_ps = self._parse_clock_period_ps(constraints)
                if period_ps is not None:
                    abc_cmd += f" -D {period_ps}"
                    log_event(
                        logger,
                        logging.DEBUG,
                        "synth.sdc_period",
                        synth=self.synth_cfg.get_name(),
                        period_ps=period_ps,
                        sdc=constraints,
                    )
                else:
                    log_event(
                        logger,
                        logging.WARNING,
                        "synth.sdc_no_clock",
                        synth=self.synth_cfg.get_name(),
                        sdc=constraints,
                    )
            self._period_ps = period_ps

            abc_script = (
                _ABC_SCRIPT_WITH_TIMING
                if period_ps is not None
                else _ABC_SCRIPT_NO_TIMING
            )
            abc_cmd += f' -script "+{abc_script}"'
            lines.append(abc_cmd)
            lines.append(f"write_verilog {self._netlist_path(mapped=True)}")
            lines.append(f"stat -liberty {lib_paths[0]}")
        else:
            if opts.abc_args:
                lines.append(f"abc {opts.abc_args}")
            lines.append(f"write_rtlil {self._netlist_path()}")

        script = "\n".join(lines) + "\n"
        script_path = self._script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def run(self) -> SynthResults:
        log_event(
            logger,
            logging.INFO,
            "synth.start",
            synth=self.synth_cfg.get_name(),
            tool=self.tool_cfg.get_executable(),
            top=self.synth_cfg.get_top(),
        )

        try:
            fl_path = self._write_filelist()
        except FilelistError as e:
            log_event(
                logger,
                logging.ERROR,
                "synth.filelist_failed",
                synth=self.synth_cfg.get_name(),
                error=str(e),
            )
            return SynthFailResults(
                name=self.name + "/results", desc=f"Filelist error: {e}"
            )

        script_path = self._write_script(fl_path)
        log_path = self._log_path()

        cmd = [self.tool_cfg.get_executable(), "-s", script_path]
        log_event(
            logger,
            logging.DEBUG,
            "synth.run_cmd",
            synth=self.synth_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        with task_status(f"synth {self.synth_cfg.get_name()}"):
            with open(log_path, "w") as log_f:
                result = run_managed_process(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                )

        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "synth.failed",
                synth=self.synth_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc=f"Tool exited with code {result.returncode}",
            )

        try:
            with open(log_path, "r") as f:
                log_text = f.read()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("ERROR:")]
        if error_lines:
            log_event(
                logger,
                logging.WARNING,
                "synth.errors_in_log",
                synth=self.synth_cfg.get_name(),
                count=len(error_lines),
                log=log_path,
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc=f"{len(error_lines)} ERROR(s) in synthesis log",
            )

        area_um2 = self._parse_area_um2(log_text)
        gate_count = self._parse_gate_count(log_text)
        crit_path_ps = self._parse_critical_path_ps(log_text)
        wns_ps = (
            self._period_ps - crit_path_ps
            if self._period_ps is not None and crit_path_ps is not None
            else None
        )

        log_event(
            logger,
            logging.INFO,
            "synth.passed",
            synth=self.synth_cfg.get_name(),
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
            log=log_path,
        )
        return SynthPassResults(
            name=self.name + "/results",
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
        )
