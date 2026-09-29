import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from ..config.fpga import FpgaConfig
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.fpga_results import (
    FpgaFailResults,
    FpgaPassResults,
    FpgaResults,
    FpgaSkipResults,
)
from .artifact_paths import clear_managed_outputs
from .fpga_base import BaseFpga, resolve_target
from .fpga_openxc7_reports import parse_nextpnr_log

# Part prefix -> prjxray database family directory.
_PRJXRAY_FAMILIES: dict[str, str] = {
    "xc7a": "artix7",
    "xc7k": "kintex7",
    "xc7s": "spartan7",
    "xc7v": "virtex7",
    "xc7z": "zynq7",
}


# Suffixes of the outputs named after the design's top.
_MANAGED_OUTPUT_SUFFIXES = (".json", ".fasm", ".frames", ".bit")


class OpenXc7Fpga(BaseFpga):
    """Open-source FPGA backend (openXC7, 7-series parts only).

    Runs in ``artefacts/<run>/``:

    1. ``yosys -s synth.ys`` (``synth_xilinx``) writes a JSON netlist.
    2. ``nextpnr-xilinx`` places and routes to FASM; utilization and Fmax/WNS come from
       its log (:mod:`.fpga_openxc7_reports`).
    3. With ``--bitstream``, prjxray ``fasm2frames`` and ``xc7frames2bit`` write ``<top>.bit``.

    Results use :class:`FpgaPassResults`; metrics the flow cannot produce (TNS/WHS, power,
    DRC, methodology, failing endpoint counts) are ``None``.

    Inputs:

    * chipdb: ``tool_overrides.openxc7.chipdb`` (the ``.bin`` file) or ``$CHIPDB`` (a
      directory holding ``<part>.bin``).
    * prjxray database (bitstream only): ``tool_overrides.openxc7.prjxray_db`` or
      ``$PRJXRAY_DB_DIR``.

    Binaries default to ``yosys``, ``nextpnr-xilinx``, ``fasm2frames`` and ``xc7frames2bit``;
    the same-named ``tool_overrides.openxc7`` keys override them, and a ``cfg-fpga-tools``
    ``openxc7`` entry overrides the nextpnr binary.
    """

    def __init__(
        self,
        name: str,
        fpga_cfg: FpgaConfig,
        suite_dir: str,
        root_cfg,
        executable: str = "openxc7",
        emit_bitstream: bool = False,
    ):
        super().__init__(
            name=name,
            fpga_cfg=fpga_cfg,
            suite_dir=suite_dir,
            root_cfg=root_cfg,
            executable=executable,
            emit_bitstream=emit_bitstream,
        )
        overrides = fpga_cfg.get_tool_overrides_for("openxc7") or {}
        self._overrides = overrides
        # `executable` is the registry name "openxc7" unless cfg-fpga-tools set a path,
        # which then names the nextpnr-xilinx binary.
        nextpnr_default = (
            executable if executable not in ("", "openxc7") else "nextpnr-xilinx"
        )
        self._yosys = overrides.get("yosys", "yosys")
        self._nextpnr = overrides.get("nextpnr", nextpnr_default)
        self._fasm2frames = overrides.get("fasm2frames", "fasm2frames")
        self._frames2bit = overrides.get("xc7frames2bit", "xc7frames2bit")

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.ys")

    def _bitstream_path(self) -> str:
        return os.path.join(self.artefact_dir, f"{self.fpga_cfg.get_top()}.bit")

    def _resolve_chipdb(self, part: str) -> str | None:
        """Return the chipdb ``.bin`` path for ``part``, or ``None`` if not configured."""
        if self._overrides.get("chipdb"):
            return str(self._overrides["chipdb"])
        chipdb_dir = os.environ.get("CHIPDB")
        if chipdb_dir:
            return os.path.join(chipdb_dir, f"{part}.bin")
        return None

    def _resolve_prjxray_db(self) -> str | None:
        """Return the prjxray database root (containing ``artix7/``, ``zynq7/``, ...), or ``None``."""
        if self._overrides.get("prjxray_db"):
            return str(self._overrides["prjxray_db"])
        return os.environ.get("PRJXRAY_DB_DIR")

    def _prjxray_family_dir(self, db_root: str, part: str) -> str:
        family = _PRJXRAY_FAMILIES.get(part.lower()[:4])
        if family is None:
            raise FatalRtlBuddyError(
                f"fpga run '{self.fpga_cfg.get_name()}': no prjxray "
                f"database family known for part '{part}'"
            )
        return os.path.join(db_root, family)

    def _write_script(self, fl_path: str) -> str:
        top = self.fpga_cfg.get_top()
        lines = ["# openXC7 synthesis script -- templated by rb fpga."]
        inc_flags = "".join(
            f" -I {shlex.quote(inc)}" for inc in self._incdirs_from_filelist(fl_path)
        )
        for src in self._source_files_from_filelist(fl_path):
            if src.lower().endswith(".sv"):
                lines.append(f"read_verilog -sv{inc_flags} {src}")
            else:
                lines.append(f"read_verilog{inc_flags} {src}")
        lines.append(f"synth_xilinx -flatten -abc9 -arch xc7 -top {top}")
        lines.append(f"write_json {top}.json")
        lines.append("")
        script_path = self._script_path()
        Path(script_path).write_text("\n".join(lines))
        return script_path

    def _run_stage(
        self, stage: str, cmd: list[str], log_name: str, stdout_path: str | None = None
    ) -> FpgaFailResults | None:
        """Run one stage; return a FAIL result, or None on success.

        With ``stdout_path``, stdout goes to that file and the log gets stderr only;
        otherwise both go to the log.
        """
        log_path = os.path.join(self.artefact_dir, log_name)
        log_event(
            logger,
            logging.DEBUG,
            "fpga.run_cmd",
            fpga=self.fpga_cfg.get_name(),
            stage=stage,
            cmd=" ".join(cmd),
            cwd=self.artefact_dir,
        )
        with open(log_path, "w") as log_f:
            if stdout_path is not None:
                with open(stdout_path, "wb") as out_f:
                    result = run_managed_process(
                        cmd, stdout=out_f, stderr=log_f, cwd=self.artefact_dir
                    )
            else:
                result = run_managed_process(
                    cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=self.artefact_dir
                )
        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "fpga.stage_failed",
                fpga=self.fpga_cfg.get_name(),
                stage=stage,
                returncode=result.returncode,
                log=log_path,
            )
            return FpgaFailResults(
                name=self.name + "/results",
                desc=f"{stage} exited with code {result.returncode}",
            )
        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""
        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("ERROR:")]
        if error_lines:
            log_event(
                logger,
                logging.WARNING,
                "fpga.errors_in_log",
                fpga=self.fpga_cfg.get_name(),
                stage=stage,
                count=len(error_lines),
                first=error_lines[0],
                log=log_path,
            )
            return FpgaFailResults(
                name=self.name + "/results",
                desc=f"{len(error_lines)} ERROR(s) in {stage} log",
            )
        return None

    def _clear_managed_outputs(self) -> None:
        """Remove the netlist, FASM, frames and bitstream left by a previous run.

        Stages pass files to each other by name, so a stale file would be picked up by a
        stage that exits 0 without writing. Files are matched by suffix so a changed top
        leaves nothing behind. This run's own `<top>.*` names are passed as `own` so they
        are cleared even when they match another flow's protected name (e.g. top `graph`).
        """
        top = self.fpga_cfg.get_top()
        stale = clear_managed_outputs(
            self.artefact_dir,
            _MANAGED_OUTPUT_SUFFIXES,
            owner=self.fpga_cfg.get_name(),
            own=[f"{top}{suffix}" for suffix in _MANAGED_OUTPUT_SUFFIXES],
            own_flow="fpga-openxc7",
        )
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "fpga.stale_artefacts_removed",
                fpga=self.fpga_cfg.get_name(),
                paths=stale,
            )

    def _fail_after_stage(self, result: FpgaFailResults) -> FpgaFailResults:
        """Clear the run's outputs and return ``result``; every failure after a stage starts goes through here."""
        self._clear_managed_outputs()
        return result

    def run(self) -> FpgaResults:
        top = self.fpga_cfg.get_top()

        # Config errors are raised before the toolchain skip and clear the old outputs.
        try:
            target = resolve_target(self.fpga_cfg, self.root_cfg)
            part = target.part
            if not part.lower().startswith("xc7"):
                raise FatalRtlBuddyError(
                    f"fpga run '{self.fpga_cfg.get_name()}': backend 'openxc7' "
                    f"supports 7-series parts only (names starting with 'xc7'), "
                    f"got '{part}' — use tool: vivado for other device families"
                )
        except Exception:
            # Catch everything (FilelistError is not a FatalRtlBuddyError); re-raised.
            self._clear_managed_outputs()
            raise

        log_event(
            logger,
            logging.INFO,
            "fpga.start",
            fpga=self.fpga_cfg.get_name(),
            tool="openxc7",
            top=top,
            part=part,
            bitstream=self.emit_bitstream,
        )

        required = {self._yosys: "yosys", self._nextpnr: "nextpnr-xilinx"}
        if self.emit_bitstream:
            required[self._fasm2frames] = "prjxray"
            required[self._frames2bit] = "prjxray"
        missing = sorted(
            {spec for binary, spec in required.items() if not shutil.which(binary)}
        )
        if missing:
            log_event(
                logger,
                logging.WARNING,
                "fpga.no_openxc7",
                fpga=self.fpga_cfg.get_name(),
                missing=missing,
            )
            return FpgaSkipResults(
                name=self.name + "/results",
                desc=(
                    "openxc7 toolchain incomplete — run "
                    + " / ".join(f"`rb tool-check --explain {m}`" for m in missing)
                    + " for install instructions"
                ),
            )

        # After the skip: a host without the toolchain must not delete another host's outputs.
        self._clear_managed_outputs()

        chipdb = self._resolve_chipdb(part)
        if chipdb is None:
            return FpgaSkipResults(
                name=self.name + "/results",
                desc=(
                    "nextpnr-xilinx chipdb not configured — set "
                    "tool_overrides.openxc7.chipdb in fpga.yaml or point "
                    "$CHIPDB at the directory holding the per-device .bin "
                    "files (see `rb tool-check --explain nextpnr-xilinx`)"
                ),
            )
        db_root = self._resolve_prjxray_db() if self.emit_bitstream else None
        if self.emit_bitstream and db_root is None:
            return FpgaSkipResults(
                name=self.name + "/results",
                desc=(
                    "prjxray database not configured — set "
                    "tool_overrides.openxc7.prjxray_db in fpga.yaml or point "
                    "$PRJXRAY_DB_DIR at the database root (see "
                    "`rb tool-check --explain prjxray`)"
                ),
            )

        try:
            fl_path = self._write_filelist()
        except FilelistError as e:
            log_event(
                logger,
                logging.ERROR,
                "fpga.filelist_failed",
                fpga=self.fpga_cfg.get_name(),
                error=str(e),
            )
            return FpgaFailResults(
                name=self.name + "/results",
                desc=f"Filelist error: {e}",
                fail_stage="setup",
            )

        script_path = self._write_script(fl_path)

        with task_status(f"fpga {self.fpga_cfg.get_name()} [openxc7]"):
            fail = self._run_stage(
                "yosys",
                [self._yosys, "-s", os.path.basename(script_path)],
                "yosys.log",
            )
            if fail is not None:
                return self._fail_after_stage(fail)

            nextpnr_cmd = [self._nextpnr, "--chipdb", chipdb]
            for xdc in target.xdc_files:
                nextpnr_cmd += ["--xdc", xdc]
            nextpnr_cmd += ["--json", f"{top}.json", "--fasm", f"{top}.fasm"]
            fail = self._run_stage("nextpnr-xilinx", nextpnr_cmd, "nextpnr.log")
            if fail is not None:
                return self._fail_after_stage(fail)

            bitstream: str | None = None
            if self.emit_bitstream:
                family_dir = self._prjxray_family_dir(db_root, part)
                frames_path = os.path.join(self.artefact_dir, f"{top}.frames")
                fail = self._run_stage(
                    "fasm2frames",
                    [
                        self._fasm2frames,
                        "--part",
                        part,
                        "--db-root",
                        family_dir,
                        f"{top}.fasm",
                    ],
                    "fasm2frames.log",
                    stdout_path=frames_path,
                )
                if fail is not None:
                    return self._fail_after_stage(fail)
                fail = self._run_stage(
                    "xc7frames2bit",
                    [
                        self._frames2bit,
                        "--part_file",
                        os.path.join(family_dir, part, "part.yaml"),
                        "--part_name",
                        part,
                        "--frm_file",
                        f"{top}.frames",
                        "--output_file",
                        f"{top}.bit",
                    ],
                    "xc7frames2bit.log",
                )
                if fail is not None:
                    return self._fail_after_stage(fail)
                bit_path = self._bitstream_path()
                if not os.path.isfile(bit_path):
                    return self._fail_after_stage(
                        FpgaFailResults(
                            name=self.name + "/results",
                            desc=f"bitstream not produced at {bit_path}",
                        )
                    )
                bitstream = bit_path

        nextpnr_log = os.path.join(self.artefact_dir, "nextpnr.log")
        try:
            metrics = parse_nextpnr_log(Path(nextpnr_log).read_text())
        except (OSError, ValueError) as e:
            return self._fail_after_stage(
                FpgaFailResults(
                    name=self.name + "/results",
                    desc=f"failed to parse nextpnr log: {e}",
                )
            )

        log_event(
            logger,
            logging.INFO,
            "fpga.passed",
            fpga=self.fpga_cfg.get_name(),
            wns_ns=metrics.get("wns_ns"),
            fmax_mhz=metrics.get("fmax_mhz"),
            timing_met=metrics.get("timing_met"),
            bitstream=bitstream,
            log=nextpnr_log,
        )
        return FpgaPassResults(
            name=self.name + "/results",
            lut=metrics.get("lut"),
            ff=metrics.get("ff"),
            bram=metrics.get("bram"),
            dsp=metrics.get("dsp"),
            wns_ns=metrics.get("wns_ns"),
            timing_met=metrics.get("timing_met"),
            fmax_mhz=metrics.get("fmax_mhz"),
            failing_paths=metrics.get("failing_paths") or None,
            bitstream=bitstream,
        )
