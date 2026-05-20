"""SymbiYosys (``sby``) tool wrapper for ``rb fpv``.

Generates a ``fpv.sby`` config from the per-run :class:`FpvConfig`,
shells out to ``sby -f -d <workdir>``, then reads the workdir
``status`` file plus the process exit code to populate
:class:`FpvResults`. Counterexample VCDs (when the proof fails) stay
inside the engine subdirectory of the workdir for the user to inspect.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_filelist import VlogFilelist
from ..config.fpv import FpvConfig, FpvToolConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process
from ..runner.fpv_results import FpvFailResults, FpvPassResults, FpvResults


# Sby's compatibility check requires a minimum yosys; we surface the
# version we probe for the same reason `pnr_openroad.py` does — gives
# users a clear signal when their toolchain is older than what we test
# against.
MIN_SBY_VERSION = "0.40"


_INCDIR_PREFIX = "+incdir+"
_LIBEXT_PREFIX = "+libext+"
_SOURCE_OPT_PREFIX = "-v "
_FILELIST_SKIP_PREFIXES = ("-y ", "-F ", "-f ")


class SbyFpv:
    def __init__(
        self,
        name: str,
        fpv_cfg: FpvConfig,
        tool_cfg: FpvToolConfig,
        suite_dir: str,
        root_cfg=None,
    ):
        self.name = name
        self.fpv_cfg = fpv_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg

        artefact_root = Path(suite_dir) / "artefacts" / fpv_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # --- artefact paths -----------------------------------------------------

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.f")

    def _sby_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.sby")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "fpv.log")

    def _workdir_path(self) -> str:
        # Sby creates this dir; we hand it `-f` to overwrite on rerun.
        return os.path.join(self.artefact_dir, "sby_workdir")

    # --- helpers ------------------------------------------------------------

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.fpv_cfg.get_model(),
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _parse_filelist(self, fl_path: str) -> tuple[list[str], list[str]]:
        """Return (source paths, include dirs) from a stripped filelist."""
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        sources: list[str] = []
        incdirs: list[str] = []
        with open(fl_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("//"):
                    continue
                if line.startswith(_INCDIR_PREFIX):
                    inc = line[len(_INCDIR_PREFIX) :]
                    incdirs.append(os.path.normpath(os.path.join(fl_dir, inc)))
                    continue
                if line.startswith(_LIBEXT_PREFIX):
                    continue
                if any(line.startswith(opt) for opt in _FILELIST_SKIP_PREFIXES):
                    continue
                if line.startswith(_SOURCE_OPT_PREFIX):
                    line = line[len(_SOURCE_OPT_PREFIX) :]
                sources.append(os.path.normpath(os.path.join(fl_dir, line)))
        return sources, incdirs

    def _probe_sby_version(self, executable: str) -> str | None:
        try:
            res = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log_event(
                logger,
                logging.WARNING,
                "fpv.sby_version_probe_failed",
                tool=executable,
                error=str(e),
            )
            return None
        out = (res.stdout or "") + (res.stderr or "")
        # Output looks like "sby 0.42+12 (yosys-0.51) ..."
        m = re.search(r"sby\s+(\S+)", out)
        return m.group(1) if m else None

    def _write_sby_file(self, sources: list[str], incdirs: list[str]) -> str:
        """Render the ``fpv.sby`` config that sby consumes."""
        sby_path = self._sby_path()
        cfg = self.fpv_cfg
        opts = self.tool_cfg.get_opts(
            cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )

        lines: list[str] = []

        # [options]
        lines.append("[options]")
        lines.append(f"mode {cfg.get_mode()}")
        lines.append(f"depth {cfg.get_depth()}")
        if opts.timeout is not None:
            lines.append(f"timeout {opts.timeout}")
        lines.append("")

        # [engines]
        lines.append("[engines]")
        for engine in cfg.get_engines():
            lines.append(engine)
        lines.append("")

        # [script]
        lines.append("[script]")
        for inc in incdirs:
            lines.append(f"verilog_defaults -add -I {inc}")
        all_sources = list(sources) + list(cfg.get_properties())
        for src in all_sources:
            # Use basename — files are dropped into the sby workdir under [files].
            lines.append(f"read -sv -formal {os.path.basename(src)}")
        lines.append(f"prep -top {cfg.get_top()}")
        lines.append("")

        # [files]
        lines.append("[files]")
        for src in all_sources:
            lines.append(src)
        lines.append("")

        with open(sby_path, "w") as f:
            f.write("\n".join(lines))
        return sby_path

    # --- run ----------------------------------------------------------------

    def run(self) -> FpvResults:
        cfg = self.fpv_cfg

        fl_path = self._write_filelist()
        sources, incdirs = self._parse_filelist(fl_path)
        if not sources and not cfg.get_properties():
            raise FatalRtlBuddyError(
                f"{cfg.get_name()}: filelist {fl_path} produced no sources and "
                f"no `properties:` entries are listed"
            )

        # Validate that all explicit property files exist before we
        # bother spinning up sby.
        for prop in cfg.get_properties():
            if not os.path.isfile(prop):
                raise FatalRtlBuddyError(
                    f"{cfg.get_name()}: property file not found: {prop}"
                )

        opts = self.tool_cfg.get_opts(
            cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        )
        if opts.solver_versions:
            # Raises FatalRtlBuddyError on any mismatch so the user
            # sees the drift before sby produces a wrong-looking
            # PASS or timeout on a different solver version.
            from .fpv_solver_pin import check_solver_pins

            resolved = check_solver_pins(opts.solver_versions)
            log_event(
                logger,
                logging.INFO,
                "fpv.solver_pins_resolved",
                verification=cfg.get_name(),
                resolved=resolved,
            )

        sby_path = self._write_sby_file(sources, incdirs)
        log_path = self._log_path()
        workdir = self._workdir_path()
        executable = self.tool_cfg.get_executable() or "sby"

        # Surface the sby version once per run so the log captures it.
        version = self._probe_sby_version(executable)
        if version is not None:
            log_event(
                logger,
                logging.INFO,
                "fpv.sby_version",
                tool=executable,
                version=version,
            )

        # `-f` overwrites the workdir if it already exists; `-d <path>`
        # selects the workdir location so we keep all artefacts under
        # `<suite>/artefacts/<run>/sby_workdir/`.
        cmd = [executable, "-f", "-d", workdir, sby_path]

        with task_status(f"Running FPV {cfg.get_name()}"):
            log_event(
                logger,
                logging.INFO,
                "fpv.start",
                verification=cfg.get_name(),
                tool=executable,
                top=cfg.get_top(),
                mode=cfg.get_mode(),
                depth=cfg.get_depth(),
            )
            start = time.monotonic()
            proc = self._run(cmd, log_path)
            runtime_s = time.monotonic() - start

        status = self._read_status(workdir)

        # Sby exit code conventions:
        #   0 -> PASS, 1 -> FAIL, 2 -> UNKNOWN/timeout, other -> ERROR.
        # We prefer the workdir `status` file when present; the exit
        # code is the fallback signal when sby crashed before writing.
        if status == "PASS" or (status is None and proc.returncode == 0):
            return FpvPassResults(
                name=cfg.get_name(),
                mode=cfg.get_mode(),
                depth=cfg.get_depth(),
                engines=cfg.get_engines(),
                runtime_s=round(runtime_s, 2),
            )

        if status == "FAIL":
            return FpvFailResults(
                name=cfg.get_name(),
                mode=cfg.get_mode(),
                depth=cfg.get_depth(),
                engines=cfg.get_engines(),
                runtime_s=round(runtime_s, 2),
                desc=self._counterexample_desc(workdir),
            )

        desc_status = status or f"sby exit code {proc.returncode}"
        return FpvFailResults(
            name=cfg.get_name(),
            mode=cfg.get_mode(),
            depth=cfg.get_depth(),
            engines=cfg.get_engines(),
            runtime_s=round(runtime_s, 2),
            desc=f"sby reported {desc_status} (see {log_path})",
        )

    # --- result helpers -----------------------------------------------------

    @staticmethod
    def _read_status(workdir: str) -> str | None:
        """Return the contents of ``<workdir>/status`` if present.

        Sby writes one of ``PASS``, ``FAIL``, ``UNKNOWN``, or ``ERROR``
        to that file after each run. The file is missing when sby died
        before completing setup.
        """
        path = os.path.join(workdir, "status")
        if not os.path.isfile(path):
            return None
        text = Path(path).read_text().strip()
        # Status lines look like "PASS" or "PASS (engine_0)" — keep the
        # first whitespace-delimited token.
        return text.split()[0] if text else None

    @staticmethod
    def _counterexample_desc(workdir: str) -> str:
        """Compose a short failure description pointing at the trace dir."""
        engine_dir = None
        for entry in sorted(os.listdir(workdir)) if os.path.isdir(workdir) else []:
            if entry.startswith("engine_") and os.path.isdir(
                os.path.join(workdir, entry)
            ):
                engine_dir = entry
                break
        if engine_dir is None:
            return "property disproved (no counterexample dir)"
        trace = os.path.join(workdir, engine_dir, "trace.vcd")
        if os.path.isfile(trace):
            return f"property disproved (counterexample: {trace})"
        return f"property disproved (engine dir: {os.path.join(workdir, engine_dir)})"

    # --- subprocess helper --------------------------------------------------

    def _run(self, cmd: list[str], log_path: str):
        with open(log_path, "w") as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            return run_managed_process(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
