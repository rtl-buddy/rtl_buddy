# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Orchestrates `rb wave`: binds a WCP listener, launches Surfer against it, and blocks until Surfer exits."""

import logging
import os
import shutil
import subprocess
import threading

from ..config.surfer import SurferConfig
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import emit_console_text, log_event, task_status
from ..process_utils import run_managed_process
from .surfer_wcp import (
    EditorLauncher,
    SurferSourceResolver,
    SurferWcpListener,
    WaveControlServer,
    WaveformValueReader,
)
from .wave_hub_bridge import maybe_connect_bridge

logger = logging.getLogger(__name__)


def prepare_surfer_trace(
    trace_path: str, wave_format: str | None, test_name: str
) -> str:
    """Return a trace path Surfer can open.

    A ``.vpd`` raises. With ``wave-format: fst-postproc`` a ``.vcd`` is converted to ``.fst`` via ``vcd2fst``; other traces pass through.
    """
    if trace_path.endswith(".vpd"):
        raise FatalRtlBuddyError(
            f"rb wave: newest trace for '{test_name}' is a VCS VPD "
            f"({trace_path}), which Surfer cannot open. Re-run the debug "
            "sim with a Verilator (FST) or Icarus (VCD) builder, or use "
            "`rb axi-profile` which converts VPD via vpd2vcd."
        )

    if wave_format == "fst-postproc" and trace_path.endswith(".vcd"):
        return _vcd_to_fst(trace_path, test_name)

    return trace_path


def _vcd_to_fst(vcd_path: str, test_name: str) -> str:
    """Convert ``vcd_path`` to a sibling ``.fst`` via ``vcd2fst``, reusing a newer existing one.

    Returns the VCD unchanged when ``vcd2fst`` is missing or fails.
    """
    fst_path = os.path.splitext(vcd_path)[0] + ".fst"
    if os.path.isfile(fst_path) and os.path.getmtime(fst_path) >= os.path.getmtime(
        vcd_path
    ):
        log_event(
            logger,
            logging.INFO,
            "wave.fst_postproc_cached",
            test=test_name,
            fst=fst_path,
        )
        return fst_path

    if shutil.which("vcd2fst") is None:
        log_event(
            logger,
            logging.WARNING,
            "wave.vcd2fst_missing",
            test=test_name,
            vcd=vcd_path,
        )
        return vcd_path

    with task_status(f"rb wave vcd2fst {test_name}"):
        result = run_managed_process(
            ["vcd2fst", vcd_path, fst_path],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(vcd_path),
        )
    if result.returncode != 0 or not os.path.isfile(fst_path):
        log_event(
            logger,
            logging.WARNING,
            "wave.vcd2fst_failed",
            test=test_name,
            vcd=vcd_path,
            returncode=result.returncode,
        )
        return vcd_path

    log_event(
        logger, logging.INFO, "wave.fst_postproc_done", test=test_name, fst=fst_path
    )
    return fst_path


class WaveLauncher:
    """Launches Surfer for one test and runs the WCP listener until Surfer exits."""

    def __init__(
        self,
        test_cfg: TestConfig,
        surfer_cfg: SurferConfig,
        suite_dir: str,
        fst_path: str,
        surfer_file: str | None = None,
        scope_annotation: bool = True,
    ):
        self._test_cfg = test_cfg
        self._surfer_cfg = surfer_cfg
        self._suite_dir = suite_dir
        self._fst_path = fst_path
        self._surfer_file = surfer_file
        self._scope_annotation = scope_annotation

    def _check_nvim_plugin(self) -> None:
        """Warn if editor-sock is configured but the nvim plugin is not installed."""
        if not self._surfer_cfg.resolved_editor_sock:
            return
        cmd = self._surfer_cfg.editor_cmd.strip()
        if not (cmd.startswith("nvim") or "/nvim" in cmd):
            return
        from .nvim_install import is_installed, pack_dir

        if not is_installed():
            log_event(
                logger,
                logging.WARNING,
                "wave.nvim_plugin_missing",
                path=str(pack_dir()),
            )

    def launch(self) -> None:
        self._check_nvim_plugin()
        resolver = SurferSourceResolver(self._test_cfg, self._suite_dir)
        editor = EditorLauncher(self._surfer_cfg)
        value_reader = WaveformValueReader(self._fst_path)
        # Check on the main thread so failures surface before Surfer starts.
        value_reader.check()
        listener = SurferWcpListener(
            self._surfer_cfg,
            resolver,
            editor,
            value_reader,
            scope_annotation=self._scope_annotation,
        )

        # Bind before launching Surfer so the port is ready when it connects.
        actual_port = listener.bind()

        cmd = [self._surfer_cfg.get_surfer_exe(), self._fst_path]
        if self._surfer_file and os.path.isfile(self._surfer_file):
            cmd += ["-c", self._surfer_file]
        cmd += ["--wcp-initiate", str(actual_port)]

        log_event(
            logger,
            logging.INFO,
            "wave.launched",
            test=self._test_cfg.name,
            fst=self._fst_path,
            surfer_file=self._surfer_file or "",
        )

        proc = subprocess.Popen(cmd)

        wcp_thread = threading.Thread(
            target=listener.run, daemon=True, name="wcp-listener"
        )
        wcp_thread.start()

        ctrl = None
        if self._surfer_cfg.resolved_ctrl_sock:
            ctrl = WaveControlServer(self._surfer_cfg.resolved_ctrl_sock, listener)
            ctrl.start()

        # None when no hub is reachable.
        hub_bridge = maybe_connect_bridge(listener=listener)

        emit_console_text(
            f"Surfer open (PID {proc.pid}). "
            f"Right-click a signal → Go to declaration. "
            f"Press Ctrl-C to exit.",
        )

        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        finally:
            if hub_bridge is not None:
                hub_bridge.stop()
            listener.stop()
            if ctrl:
                ctrl.stop()
            wcp_thread.join(timeout=2)

        log_event(logger, logging.INFO, "wave.done", test=self._test_cfg.name)
