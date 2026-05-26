"""rtl-buddy-axi-profiler tool wrappers.

Drives the standalone ``axi-profiler`` CLI in subprocess-granularity
mode: rtl_buddy is not coupled to the profiler's Python API, and a
profiler release can be picked up via ``uv sync`` (or by re-installing
the standalone binary) without code changes here.

Four wrappers, one per ``rb axi-profile`` subcommand:

* :class:`RtlBuddyAxiProfileDiscover` — ``rb axi-profile discover <model>``:
  parses RTL via ``axi-profiler discover`` and writes
  ``axi-bundles.yaml``. Output defaults to ``model.axi_bundles`` (the
  checked-in manifest path) when set, falling back to
  ``artefacts/axi/<model>/axi-bundles.yaml``.

* :class:`RtlBuddyAxiProfileRun` — ``rb axi-profile run <test>``: ingests
  a per-test FST and writes ``axi-perf.json``. Resolves the model
  (from ``tests.yaml``), the checked-in manifest (from
  ``models.yaml``'s ``axi_bundles``), the FST path
  (``<suite_dir>/artefacts/<test>/dump.fst`` — same convention as
  ``rb wave``), and the testbench top scope (from the test's
  ``tb.name`` in ``tests.yaml``) without further user input. The
  ``tb_prefix`` override lets the user replace the auto-extracted
  value when the wrapping scope name diverges from the testbench
  name (e.g. a custom Verilator wrapper).

* :class:`RtlBuddyAxiProfileGenMonitor` — ``rb axi-profile gen-monitor
  <model>``: emits a SystemVerilog bind-style monitor for the stream
  ingest path. Reads the manifest from ``model.axi_bundles`` and
  writes to ``model.axi_monitor_out`` — both come from the
  ``models.yaml`` entry so the testbench's filelist can pick up the
  generated file without per-test config.

* :class:`RtlBuddyAxiProfileNotebook` — ``rb axi-profile notebook
  <test>``: resolves the per-test ``axi-txns.parquet`` (from the
  ``--emit-txns-parquet`` flag on ``axi-profiler run``) and spawns
  ``marimo edit`` against the packaged notebook template shipped
  inside the ``rtl_buddy_axi_profiler.notebook`` subpackage. The
  parquet path is exported as ``$AXI_TXNS_PARQUET`` so the template
  picks it up; the user gets an interactive deep-dive UI without
  hand-writing pyarrow scripts.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .vlog_filelist import VlogFilelist
from ..config.model import ModelConfig
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)


def _require_axi_profiler(executable: str) -> None:
    """Resolve ``executable`` to a runnable axi-profiler or raise."""
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
    """Generates a filelist + invokes ``axi-profiler discover``.

    Single-shot. Constructed per ``rb axi-profile discover`` invocation.
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
        # Prefer the checked-in manifest path from models.yaml when set;
        # otherwise drop the output under artefacts/ so discover stays
        # usable for models that don't yet have the field configured.
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
            self.model_cfg.name,
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
    """Per-test ingest + aggregate via ``axi-profiler run``.

    Resolves model + manifest + FST + tb_prefix automatically from
    ``tests.yaml`` / ``models.yaml`` / the standard artefact layout —
    the user only types ``rb axi-profile run <test>``. Override hooks
    exist for ``--output`` and ``--tb-prefix`` so unusual setups can
    redirect without editing config files.
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
        # None  → don't emit a parquet (axi-perf.json only, legacy default).
        # str "" → emit at the artefact-dir default (axi-txns.parquet next
        #          to axi-perf.json — convention `rb axi-profile notebook`
        #          looks for).
        # str path → emit at that explicit path.
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

    def _fst_path(self) -> str:
        # Same convention as `rb wave`: artefacts/<test>/dump.fst.
        return os.path.join(self.suite_dir, "artefacts", self.test_name, "dump.fst")

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

    def _resolve_fst_path(self) -> str:
        fst = self._fst_path()
        if not os.path.isfile(fst):
            raise FatalRtlBuddyError(
                f"axi-profile run: FST not found at {fst}. "
                f"Run `rb test {self.test_name}` first to produce it."
            )
        return fst

    def _resolve_tb_prefix(self) -> str:
        if self.tb_prefix_override is not None:
            return self.tb_prefix_override
        # Auto-extract: the testbench wraps the DUT, and Verilator names
        # the top scope after the testbench module — which is what
        # tests.yaml's `testbenches:` section names. Empty if the user
        # explicitly opts out via --tb-prefix=''.
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
        fst: str,
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
            self.model_cfg.name,
            "--input",
            fst,
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
        fst = self._resolve_fst_path()
        tb_prefix = self._resolve_tb_prefix()
        out_path = self.output_override or self._default_output_path()
        # Resolve the parquet destination: empty-string → artefact-dir
        # default (canonical location for `rb axi-profile notebook`).
        parquet_path: str | None
        if self.emit_txns_parquet is None:
            parquet_path = None
        elif self.emit_txns_parquet == "":
            parquet_path = self._default_parquet_path()
        else:
            parquet_path = self.emit_txns_parquet

        fl_path = self._write_filelist()
        cmd = self._build_cmd(fl_path, manifest, fst, out_path, tb_prefix, parquet_path)

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
    """Emit the SV bind-style monitor for a model via ``axi-profiler gen-monitor``.

    Single-shot. Both the manifest input and the SV output path are
    looked up in ``models.yaml`` (``axi_bundles`` /
    ``axi_monitor_out``), so the user just types
    ``rb axi-profile gen-monitor <model>`` and the wrapper handles
    discovery + destination. The user is responsible for adding the
    generated SV to the testbench's filelist once.
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
        # The downstream gen-monitor opens the output for write; make
        # sure the parent directory exists so a typical
        # `../verif/<tb>/gen/...` path doesn't fail on first run.
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
    """Launch the packaged marimo notebook against a test's parquet.

    Resolves three things up front:

    1. The per-test parquet at
       ``<suite_dir>/artefacts/axi/<test>/axi-txns.parquet`` — produced
       by ``rb axi-profile run <test>`` when ``axi-profiler`` is
       installed with the ``[parquet]`` extra. Missing → clear
       ``FatalRtlBuddyError`` pointing at the prerequisite command.
    2. The notebook template via
       ``importlib.resources.files('rtl_buddy_axi_profiler.notebook')
       / 'template.py'`` — always present once axi-profiler is
       installed, regardless of the ``[notebook]`` extra (the extra
       only adds marimo + altair + polars to the dep closure).
    3. The marimo binary on ``$PATH`` — gated by the ``[notebook]``
       extra. Missing → install hint pointing at
       ``rtl-buddy-axi-profiler[notebook]``.

    Spawns ``marimo edit <template>`` with ``$AXI_TXNS_PARQUET``
    exported so the template's first cell reads it. Foreground by
    default (matches ``rb hub start``); ``--daemon`` is accepted but
    falls back to foreground for v1 (background detach is a
    follow-up — same pattern as hub).
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
        # ``headless`` is for the hub-launched flow (Phase 2 of the
        # marimo umbrella) — the SPA opens the URL itself, so marimo
        # shouldn't auto-pop a browser, and the auth token is
        # disabled so the SPA can link directly without juggling
        # secrets across the IPC boundary.
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
        # The notebook subpackage ships inside the axi-profiler wheel,
        # so resources.files() returns a real filesystem path when the
        # wheel is unpacked. We don't need a CM here — marimo just
        # opens the file directly.
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
            # --headless: no auto-browser-pop; the SPA opens the URL.
            # --no-token: the SPA can navigate to the URL without
            # threading a per-session token through the hub → browser
            # handoff. Loopback-only, so the security trade is fine.
            cmd += ["--headless", "--no-token"]
        return cmd

    def run(self) -> int:
        # Resolve the parquet + template + binary up front so failures
        # surface before marimo spins up its tornado server.
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
