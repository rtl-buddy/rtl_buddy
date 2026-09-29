"""Wrappers around the standalone ``axi-profiler`` CLI, one per ``rb axi-profile`` subcommand.

The profiler runs as a subprocess. The wrappers are :class:`RtlBuddyAxiProfileDiscover`
(writes ``axi-bundles.yaml``), :class:`RtlBuddyAxiProfileRun` (writes ``axi-perf.json``),
:class:`RtlBuddyAxiProfileGenMonitor` (emits the SystemVerilog monitor) and
:class:`RtlBuddyAxiProfileNotebook` (opens the marimo notebook on a test's parquet).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .vlog_filelist import VlogFilelist
from .wave_trace import TRACE_CANDIDATES
from ..config.model import ModelConfig
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)


def _require_axi_profiler(executable: str) -> None:
    """Raise FatalRtlBuddyError unless ``executable`` is a runnable axi-profiler."""
    if os.sep in executable or (os.altsep and os.altsep in executable):
        if not (os.path.isfile(executable) and os.access(executable, os.X_OK)):
            raise FatalRtlBuddyError(
                f"axi-profile: axi-profiler not found or not executable: {executable}"
            )
        return
    if shutil.which(executable) is None:
        raise FatalRtlBuddyError(
            f"axi-profile: '{executable}' not found on PATH; "
            f"install rtl-buddy-axi-profiler "
            f"(e.g. `uv tool install rtl-buddy-axi-profiler`)."
        )


class RtlBuddyAxiProfileDiscover:
    """Write a filelist and run ``axi-profiler discover`` for a model.

    The output is ``model.axi_bundles`` (models.yaml) when set, else
    ``artefacts/axi/<model>/axi-bundles.yaml``; ``output`` overrides both.
    """

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        output: str | None = None,
        amend: str | None = None,
        executable: str = "axi-profiler",
    ):
        self.name = name
        self.model_cfg = model_cfg
        self.output_override = output
        self.amend = amend
        self.executable = executable

        artefact_root = Path(suite_dir) / "artefacts" / "axi" / model_cfg.name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-profile-discover.log")

    def _resolve_output_path(self) -> str:
        if self.output_override:
            return self.output_override
        configured = self.model_cfg.get_axi_bundles_path()
        if configured:
            os.makedirs(os.path.dirname(configured), exist_ok=True)
            return configured
        return os.path.join(self.artefact_dir, "axi-bundles.yaml")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.model_cfg,
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _build_cmd(self, fl_path: str, out_path: str) -> list[str]:
        cmd = [
            self.executable,
            "discover",
            "--filelist",
            fl_path,
            "--top",
            self.model_cfg.get_top(),
            "--output",
            out_path,
        ]
        if self.amend:
            cmd += ["--amend", self.amend]
        return cmd

    def run(self) -> int:
        _require_axi_profiler(self.executable)

        fl_path = self._write_filelist()
        out_path = self._resolve_output_path()
        cmd = self._build_cmd(fl_path, out_path)
        log_event(
            logger,
            logging.INFO,
            "axi_profile_discover.run",
            model=self.model_cfg.name,
            cmd=" ".join(cmd),
            output=out_path,
        )

        log_path = self._log_path()
        with task_status(f"axi-profile discover {self.model_cfg.name}"):
            with open(log_path, "w") as log_f:
                log_f.write("$ " + " ".join(cmd) + "\n")
                log_f.flush()
                proc = run_managed_process(
                    cmd, stdout=None, stderr=log_f, cwd=self.artefact_dir
                )

        log_event(
            logger,
            logging.INFO,
            "axi_profile_discover.done",
            model=self.model_cfg.name,
            output=out_path,
            returncode=proc.returncode,
        )
        return proc.returncode


class RtlBuddyAxiProfileRun:
    """Run ``axi-profiler run`` on a test's trace.

    The model, manifest (``axi_bundles`` in models.yaml), trace and testbench scope
    prefix come from the test config and the artefact layout; ``output`` and
    ``tb_prefix_override`` replace the defaults.

    The trace is the newest of these under ``artefacts/<test>/``:

    * ``dump.fst`` (Verilator) and ``dump.vcd`` are ingested directly.
    * ``vcdplus.vpd`` (VCS) is converted with ``vpd2vcd`` then ``vcd2fst`` to a cached
      ``vcdplus.fst``, skipped when the cache is newer than the VPD. Without ``vcd2fst``
      the VCD is kept and ingested as is (about 15x larger).
    """

    def __init__(
        self,
        name: str,
        test_cfg: TestConfig,
        *,
        suite_dir: str,
        output: str | None = None,
        tb_prefix_override: str | None = None,
        emit_txns_parquet: str | None = None,
        executable: str = "axi-profiler",
    ):
        self.name = name
        self.test_cfg = test_cfg
        self.test_name = test_cfg.get_name()
        self.model_cfg = test_cfg.get_model()
        self.suite_dir = os.path.abspath(suite_dir)
        self.output_override = output
        self.tb_prefix_override = tb_prefix_override
        # None: no parquet. "": axi-txns.parquet in the artefact dir. Otherwise: that path.
        self.emit_txns_parquet = emit_txns_parquet
        self.executable = executable

        artefact_root = Path(self.suite_dir) / "artefacts" / "axi" / self.test_name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-profile-run.log")

    def _default_output_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-perf.json")

    def _default_parquet_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-txns.parquet")

    # Shared with `rb wave` (tools/wave_trace.py); the newest mtime wins.
    _TRACE_CANDIDATES = TRACE_CANDIDATES

    def _trace_dir(self) -> str:
        return os.path.join(self.suite_dir, "artefacts", self.test_name)

    def _resolve_manifest_path(self) -> str:
        manifest = self.model_cfg.get_axi_bundles_path()
        if manifest is None:
            raise FatalRtlBuddyError(
                f"axi-profile run: model '{self.model_cfg.name}' has no "
                "`axi_bundles:` in models.yaml. Add the field pointing at "
                "the checked-in axi-bundles.yaml manifest, then run "
                f"`rb axi-profile discover {self.model_cfg.name}` to "
                "generate one if it doesn't exist."
            )
        if not os.path.isfile(manifest):
            raise FatalRtlBuddyError(
                f"axi-profile run: manifest not found at {manifest}. "
                f"Run `rb axi-profile discover {self.model_cfg.name}` first."
            )
        return manifest

    def _resolve_input_path(self) -> str:
        trace_dir = self._trace_dir()
        candidates = [
            p
            for p in (os.path.join(trace_dir, n) for n in self._TRACE_CANDIDATES)
            if os.path.isfile(p)
        ]
        if not candidates:
            names = " / ".join(self._TRACE_CANDIDATES)
            raise FatalRtlBuddyError(
                f"axi-profile run: no trace found under {trace_dir} "
                f"(looked for {names}). "
                f"Run `rb -M debug test {self.test_name}` first to produce one."
            )
        newest = max(candidates, key=os.path.getmtime)
        if newest.endswith(".vpd"):
            return self._convert_vpd(newest)
        return newest

    def _convert_vpd(self, vpd: str) -> str:
        """Convert a VCS VPD dump to FST and return the path to ingest.

        ``vpd2vcd`` ships with VCS. The cached ``vcdplus.fst`` (or ``vcdplus.vcd`` when
        ``vcd2fst`` is missing) sits next to the VPD in ``artefacts/<test>/`` so that
        `rb wave` can open it; the conversion log is in ``self.artefact_dir``.
        """
        trace_dir = os.path.dirname(vpd)
        cached_fst = os.path.join(trace_dir, "vcdplus.fst")
        cached_vcd = os.path.join(trace_dir, "vcdplus.vcd")
        for cached in (cached_fst, cached_vcd):
            if os.path.isfile(cached) and os.path.getmtime(cached) >= os.path.getmtime(
                vpd
            ):
                log_event(
                    logger,
                    logging.INFO,
                    "axi_profile_run.vpd_convert_cached",
                    test=self.test_name,
                    vpd=vpd,
                    cached=cached,
                )
                return cached

        if shutil.which("vpd2vcd") is None:
            raise FatalRtlBuddyError(
                f"axi-profile run: newest trace is a VCS VPD ({vpd}) but "
                "`vpd2vcd` is not on PATH. It ships with VCS — source "
                "your Synopsys environment, or convert manually and "
                "re-run."
            )

        log_path = os.path.join(self.artefact_dir, "vpd-convert.log")
        tmp_vcd = os.path.join(trace_dir, "vcdplus.tmp.vcd")
        with task_status(f"axi-profile vpd2vcd {self.test_name}"):
            with open(log_path, "w") as log_f:
                # -full64 first: 64-bit-only VCS installs have no 32-bit vpd2vcd.exe.
                proc = None
                for argv in (
                    ["vpd2vcd", "-full64", vpd, tmp_vcd],
                    ["vpd2vcd", vpd, tmp_vcd],
                ):
                    log_f.write("$ " + " ".join(argv) + "\n")
                    log_f.flush()
                    proc = run_managed_process(
                        argv, stdout=log_f, stderr=log_f, cwd=trace_dir
                    )
                    if proc.returncode == 0 and os.path.isfile(tmp_vcd):
                        break
                if proc is None or proc.returncode != 0 or not os.path.isfile(tmp_vcd):
                    raise FatalRtlBuddyError(
                        f"axi-profile run: vpd2vcd failed on {vpd}; see {log_path}."
                    )

                if shutil.which("vcd2fst") is None:
                    os.replace(tmp_vcd, cached_vcd)
                    log_event(
                        logger,
                        logging.WARNING,
                        "axi_profile_run.vcd2fst_missing",
                        test=self.test_name,
                        vcd=cached_vcd,
                    )
                    return cached_vcd

                argv = ["vcd2fst", tmp_vcd, cached_fst]
                log_f.write("$ " + " ".join(argv) + "\n")
                log_f.flush()
                proc = run_managed_process(
                    argv, stdout=log_f, stderr=log_f, cwd=trace_dir
                )
                if proc.returncode != 0 or not os.path.isfile(cached_fst):
                    raise FatalRtlBuddyError(
                        f"axi-profile run: vcd2fst failed on {tmp_vcd}; see {log_path}."
                    )
                os.unlink(tmp_vcd)

        log_event(
            logger,
            logging.INFO,
            "axi_profile_run.vpd_converted",
            test=self.test_name,
            vpd=vpd,
            fst=cached_fst,
        )
        return cached_fst

    def _resolve_tb_prefix(self) -> str:
        if self.tb_prefix_override is not None:
            return self.tb_prefix_override
        # Verilator names the top scope after the testbench module.
        tb = self.test_cfg.get_testbench()
        return tb.get_name() if tb is not None else ""

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.model_cfg,
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=True, deduplicate=True
        )
        return fl_path

    def _build_cmd(
        self,
        fl_path: str,
        manifest: str,
        trace: str,
        out_path: str,
        tb_prefix: str,
        parquet_path: str | None,
    ) -> list[str]:
        cmd = [
            self.executable,
            "run",
            "--filelist",
            fl_path,
            "--top",
            self.model_cfg.get_top(),
            "--input",
            trace,
            "--manifest",
            manifest,
            "--output",
            out_path,
        ]
        if tb_prefix:
            cmd += ["--tb-prefix", tb_prefix]
        if parquet_path is not None:
            cmd += ["--emit-txns-parquet", parquet_path]
        return cmd

    def run(self) -> int:
        _require_axi_profiler(self.executable)

        manifest = self._resolve_manifest_path()
        trace = self._resolve_input_path()
        tb_prefix = self._resolve_tb_prefix()
        out_path = self.output_override or self._default_output_path()
        parquet_path: str | None
        if self.emit_txns_parquet is None:
            parquet_path = None
        elif self.emit_txns_parquet == "":
            parquet_path = self._default_parquet_path()
        else:
            parquet_path = self.emit_txns_parquet

        fl_path = self._write_filelist()
        cmd = self._build_cmd(
            fl_path, manifest, trace, out_path, tb_prefix, parquet_path
        )

        log_event(
            logger,
            logging.INFO,
            "axi_profile_run.start",
            test=self.test_name,
            model=self.model_cfg.name,
            cmd=" ".join(cmd),
            output=out_path,
            tb_prefix=tb_prefix,
        )

        log_path = self._log_path()
        with task_status(f"axi-profile run {self.test_name}"):
            with open(log_path, "w") as log_f:
                log_f.write("$ " + " ".join(cmd) + "\n")
                log_f.flush()
                proc = run_managed_process(
                    cmd, stdout=None, stderr=log_f, cwd=self.artefact_dir
                )

        log_event(
            logger,
            logging.INFO,
            "axi_profile_run.done",
            test=self.test_name,
            output=out_path,
            returncode=proc.returncode,
        )
        return proc.returncode


class RtlBuddyAxiProfileGenMonitor:
    """Emit the SystemVerilog bind-style monitor with ``axi-profiler gen-monitor``.

    The manifest and output path come from ``axi_bundles`` and ``axi_monitor_out`` in
    models.yaml; ``output`` overrides the latter. Add the generated file to the
    testbench filelist once.
    """

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        output: str | None = None,
        time_precision: str | None = None,
        buffer_cap: int | None = None,
        executable: str = "axi-profiler",
    ):
        self.name = name
        self.model_cfg = model_cfg
        self.output_override = output
        self.time_precision = time_precision
        self.buffer_cap = buffer_cap
        self.executable = executable

        artefact_root = Path(suite_dir) / "artefacts" / "axi" / model_cfg.name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-profile-gen-monitor.log")

    def _resolve_manifest_path(self) -> str:
        manifest = self.model_cfg.get_axi_bundles_path()
        if manifest is None:
            raise FatalRtlBuddyError(
                f"axi-profile gen-monitor: model '{self.model_cfg.name}' "
                "has no `axi_bundles:` in models.yaml. Add the field "
                "pointing at the checked-in axi-bundles.yaml manifest, "
                "then run "
                f"`rb axi-profile discover {self.model_cfg.name}` to "
                "generate one if it doesn't exist."
            )
        if not os.path.isfile(manifest):
            raise FatalRtlBuddyError(
                f"axi-profile gen-monitor: manifest not found at "
                f"{manifest}. "
                f"Run `rb axi-profile discover {self.model_cfg.name}` first."
            )
        return manifest

    def _resolve_output_path(self) -> str:
        if self.output_override:
            return self.output_override
        configured = self.model_cfg.get_axi_monitor_out_path()
        if configured is None:
            raise FatalRtlBuddyError(
                f"axi-profile gen-monitor: model '{self.model_cfg.name}' "
                "has no `axi_monitor_out:` in models.yaml. Add the "
                "field pointing at the SV path inside your testbench "
                "tree (e.g. `../verif/<tb>/gen/axi_perf_mon.sv`) or "
                "pass `--output <path>` explicitly."
            )
        return configured

    def _build_cmd(self, manifest: str, out_path: str) -> list[str]:
        cmd = [
            self.executable,
            "gen-monitor",
            manifest,
            "--output",
            out_path,
        ]
        if self.time_precision:
            cmd += ["--time-precision", self.time_precision]
        if self.buffer_cap is not None:
            cmd += ["--buffer-cap", str(self.buffer_cap)]
        return cmd

    def run(self) -> int:
        _require_axi_profiler(self.executable)

        manifest = self._resolve_manifest_path()
        out_path = self._resolve_output_path()
        # gen-monitor does not create the output directory.
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

        cmd = self._build_cmd(manifest, out_path)
        log_event(
            logger,
            logging.INFO,
            "axi_profile_gen_monitor.start",
            model=self.model_cfg.name,
            cmd=" ".join(cmd),
            output=out_path,
        )

        log_path = self._log_path()
        with task_status(f"axi-profile gen-monitor {self.model_cfg.name}"):
            with open(log_path, "w") as log_f:
                log_f.write("$ " + " ".join(cmd) + "\n")
                log_f.flush()
                proc = run_managed_process(
                    cmd, stdout=None, stderr=log_f, cwd=self.artefact_dir
                )

        log_event(
            logger,
            logging.INFO,
            "axi_profile_gen_monitor.done",
            model=self.model_cfg.name,
            output=out_path,
            returncode=proc.returncode,
        )
        return proc.returncode


class RtlBuddyAxiProfileNotebook:
    """Run ``marimo edit`` on the packaged notebook template for a test's parquet.

    Requires ``artefacts/axi/<test>/axi-txns.parquet`` (from ``rb axi-profile run
    --emit-txns-parquet``, needs the axi-profiler ``[parquet]`` extra), the template in the
    ``rtl_buddy_axi_profiler.notebook`` package and ``marimo`` (the ``[notebook]`` extra)
    on PATH. The parquet path is passed as ``$AXI_TXNS_PARQUET``. Always runs in the
    foreground; ``foreground=False`` only logs a warning.
    """

    def __init__(
        self,
        name: str,
        test_cfg: TestConfig,
        *,
        suite_dir: str,
        port: int | None = None,
        foreground: bool = True,
        headless: bool = False,
        marimo_executable: str = "marimo",
    ):
        self.name = name
        self.test_cfg = test_cfg
        self.test_name = test_cfg.get_name()
        self.suite_dir = os.path.abspath(suite_dir)
        self.port = port
        self.foreground = foreground
        # headless: the hub launches this and the SPA opens the URL, so no browser and no auth token.
        self.headless = headless
        self.marimo_executable = marimo_executable

        self.artefact_dir = os.path.join(
            self.suite_dir, "artefacts", "axi", self.test_name
        )

    def _parquet_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-txns.parquet")

    def _resolve_parquet_path(self) -> str:
        p = self._parquet_path()
        if not os.path.isfile(p):
            raise FatalRtlBuddyError(
                f"axi-profile notebook: parquet not found at {p}. "
                f"Run `rb axi-profile run {self.test_name} --emit-txns-parquet` "
                "first to produce it (requires the axi-profiler "
                "[parquet] extra)."
            )
        return p

    def _resolve_template_path(self) -> str:
        try:
            from importlib import resources

            ref = resources.files("rtl_buddy_axi_profiler.notebook") / "template.py"
            path = str(ref)
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
            return path
        except (ModuleNotFoundError, FileNotFoundError) as e:
            raise FatalRtlBuddyError(
                "axi-profile notebook: notebook template not found in the "
                "installed rtl-buddy-axi-profiler wheel "
                f"({type(e).__name__}: {e}). Reinstall with the "
                "[notebook] extra: "
                "`uv pip install 'rtl-buddy-axi-profiler[notebook]'`."
            ) from None

    def _require_marimo(self) -> None:
        if os.sep in self.marimo_executable or (
            os.altsep and os.altsep in self.marimo_executable
        ):
            if not (
                os.path.isfile(self.marimo_executable)
                and os.access(self.marimo_executable, os.X_OK)
            ):
                raise FatalRtlBuddyError(
                    "axi-profile notebook: marimo not found or not "
                    f"executable: {self.marimo_executable}"
                )
            return
        if shutil.which(self.marimo_executable) is None:
            raise FatalRtlBuddyError(
                f"axi-profile notebook: '{self.marimo_executable}' not on "
                "PATH. Install the notebook extra: "
                "`uv pip install 'rtl-buddy-axi-profiler[notebook]'` "
                "(pulls marimo + altair + polars)."
            )

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "axi-profile-notebook.log")

    def _build_cmd(self, template: str) -> list[str]:
        cmd = [self.marimo_executable, "edit", template]
        if self.port is not None:
            cmd += ["--port", str(self.port)]
        if self.headless:
            # --no-token is acceptable because marimo listens on loopback only.
            cmd += ["--headless", "--no-token"]
        return cmd

    def run(self) -> int:
        # Resolve inputs first so failures surface before marimo starts.
        parquet = self._resolve_parquet_path()
        template = self._resolve_template_path()
        self._require_marimo()

        if not self.foreground:
            log_event(
                logger,
                logging.WARNING,
                "axi_profile_notebook.daemon_fallback",
                test=self.test_name,
                reason=(
                    "background detach not implemented yet; running in foreground."
                ),
            )

        os.makedirs(self.artefact_dir, exist_ok=True)
        cmd = self._build_cmd(template)
        env = {**os.environ, "AXI_TXNS_PARQUET": parquet}

        log_event(
            logger,
            logging.INFO,
            "axi_profile_notebook.start",
            test=self.test_name,
            cmd=" ".join(cmd),
            parquet=parquet,
            template=template,
            port=self.port,
        )

        log_path = self._log_path()
        with task_status(f"axi-profile notebook {self.test_name}"):
            with open(log_path, "w") as log_f:
                log_f.write(f"$ AXI_TXNS_PARQUET={parquet} " + " ".join(cmd) + "\n")
                log_f.flush()
                proc = run_managed_process(
                    cmd, stdout=None, stderr=log_f, env=env, cwd=self.artefact_dir
                )

        log_event(
            logger,
            logging.INFO,
            "axi_profile_notebook.done",
            test=self.test_name,
            returncode=proc.returncode,
        )
        return proc.returncode
