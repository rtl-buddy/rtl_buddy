# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
wave_launcher: orchestrates the `rb wave` workflow.

Sequence:
  1. Check for existing debug FST; run debug sim if absent.
  2. Bind WCP listener socket (must happen before Surfer starts).
  3. Launch Surfer with --wcp-initiate <port>.
  4. Start WCP listener thread.
  5. Block until Ctrl-C or Surfer exits.
"""
import logging
import os
import subprocess
import threading

from ..config.surfer import SurferConfig
from ..config.test import TestConfig
from ..logging_utils import emit_console_text, log_event
from .surfer_wcp import EditorLauncher, SurferSourceResolver, SurferWcpListener, WaveformValueReader

logger = logging.getLogger(__name__)


class WaveLauncher:
  """
  Launches Surfer and manages the WCP client lifecycle for a single test.
  """

  def __init__(self, test_cfg: TestConfig, surfer_cfg: SurferConfig,
               suite_dir: str, fst_path: str, surfer_file: str | None = None):
    self._test_cfg = test_cfg
    self._surfer_cfg = surfer_cfg
    self._suite_dir = suite_dir
    self._fst_path = fst_path
    self._surfer_file = surfer_file

  def launch(self) -> None:
    resolver = SurferSourceResolver(self._test_cfg, self._suite_dir)
    editor = EditorLauncher(self._surfer_cfg)
    value_reader = WaveformValueReader(self._fst_path)
    listener = SurferWcpListener(self._surfer_cfg, resolver, editor, value_reader)

    # Bind before launching Surfer so the port is ready when it connects.
    # actual_port is OS-assigned when wcp_port=0, otherwise matches wcp_port.
    actual_port = listener.bind()

    cmd = [self._surfer_cfg.get_surfer_exe(), self._fst_path]
    if self._surfer_file and os.path.isfile(self._surfer_file):
      cmd += ['-c', self._surfer_file]
    cmd += ['--wcp-initiate', str(actual_port)]

    log_event(logger, logging.INFO, "wave.launched",
              test=self._test_cfg.name, fst=self._fst_path,
              surfer_file=self._surfer_file or '')

    proc = subprocess.Popen(cmd)

    wcp_thread = threading.Thread(target=listener.run, daemon=True, name='wcp-listener')
    wcp_thread.start()

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
      listener.stop()
      wcp_thread.join(timeout=2)

    log_event(logger, logging.INFO, "wave.done", test=self._test_cfg.name)
