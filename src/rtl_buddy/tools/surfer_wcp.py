# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
surfer_wcp: WCP client for Surfer waveform viewer.

rtl-buddy acts as the WCP client (TCP listener). Surfer connects out using
--wcp-initiate <port>. After handshake, Surfer sends goto_declaration events
when the user right-clicks a signal; rtl-buddy resolves the variable to a
source file and opens it in the configured editor. If the event includes a
cursor timestamp, the signal value is read from the FST/VCD waveform and
printed to the console before the editor opens.
"""
import json
import logging
import os
import re
import socket
import subprocess
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from ..config.surfer import SurferConfig
  from ..config.test import TestConfig

from ..logging_utils import emit_console_text, log_event

logger = logging.getLogger(__name__)

_WCP_VERSION = "0"  # Surfer only accepts version "0"
_RECV_BUF = 4096


# ---------------------------------------------------------------------------
# Frame I/O helpers
# ---------------------------------------------------------------------------

class _FrameReader:
  """Read null-byte delimited JSON frames from a socket."""

  def __init__(self, sock: socket.socket):
    self._sock = sock
    self._buf = b''

  def read(self) -> dict:
    while b'\x00' not in self._buf:
      chunk = self._sock.recv(_RECV_BUF)
      if not chunk:
        raise ConnectionError("WCP connection closed by peer")
      self._buf += chunk
    frame, _, self._buf = self._buf.partition(b'\x00')
    return json.loads(frame.decode('utf-8'))


def _send_frame(sock: socket.socket, obj: dict) -> None:
  data = json.dumps(obj).encode('utf-8') + b'\x00'
  sock.sendall(data)


# ---------------------------------------------------------------------------
# Waveform value reader
# ---------------------------------------------------------------------------

class WaveformValueReader:
  """
  Look up signal values at a specific FST timestamp using pywellen.

  The pywellen Waveform is loaded lazily on the first query and reused.
  Errors (file missing, signal not in waveform, etc.) return None silently.
  """

  def __init__(self, fst_path: str):
    self._fst_path = fst_path
    self._waveform = None

  def _load(self):
    if self._waveform is None:
      import pywellen  # type: ignore[import-untyped]  # noqa: PLC0415
      if not os.path.isfile(self._fst_path):
        raise FileNotFoundError(self._fst_path)
      self._waveform = pywellen.Waveform(self._fst_path)
    return self._waveform

  def get_value(self, variable: str, timestamp: int) -> str | None:
    """Return the signal value string at *timestamp* (FST ticks), or None."""
    try:
      wf = self._load()
      sig = wf.get_signal_from_path(variable)
      return str(sig.value_at_time(timestamp))
    except Exception:
      return None


# ---------------------------------------------------------------------------
# Source resolver
# ---------------------------------------------------------------------------

class SurferSourceResolver:
  """
  Resolve a WCP variable path (e.g. "tb_top.i_dut_2.z_bus") to a source
  file and line number by grepping the model's SV source files.

  Source files are derived from the test's ModelConfig filelist, not from
  root_config, so the search is scoped to the relevant design block.
  """

  def __init__(self, test_cfg: 'TestConfig', suite_dir: str):
    self._sv_files = self._collect_sv_files(test_cfg, suite_dir)
    log_event(logger, logging.DEBUG, "wcp.resolver_ready",
              files=len(self._sv_files), suite=suite_dir)

  def _collect_sv_files(self, test_cfg: 'TestConfig', suite_dir: str) -> list[str]:
    from ..tools.vlog_filelist import VlogFilelist

    model_cfg = test_cfg.get_model()
    tb_cfg = test_cfg.get_testbench()
    fl = VlogFilelist(name='wcp_resolver', model_cfg=model_cfg, output_path='/dev/null')

    # Model source files (resolved from models.yaml location)
    model_fpath = os.path.abspath(model_cfg.get_model_path() or '.')
    model_entries = fl._extract(model_cfg.get_filelist(), unroll=True, fpath=model_fpath)

    # Testbench source files (resolved from suite dir)
    tb_fpath = os.path.join(suite_dir, 'tests.yaml')
    tb_entries = fl._extract(tb_cfg.get_filelist(), unroll=True, fpath=tb_fpath)

    sv_files = []
    for path, opt in model_entries + tb_entries:
      if opt is None or opt.strip() == '-v':
        if os.path.isfile(path):
          sv_files.append(path)
    return sv_files

  def resolve(self, variable: str) -> tuple[str, int] | None:
    """
    Resolve a hierarchical variable path to (filepath, lineno).

    Tries the rightmost component (signal name) first, then the second-to-last
    (instance/module component) as a fallback.
    """
    parts = variable.split('.')
    candidates = [parts[-1]]
    if len(parts) >= 2:
      # Strip trailing digits from instance name to approximate module name
      mod_candidate = re.sub(r'_\d+$', '', parts[-2])
      if mod_candidate not in candidates:
        candidates.append(mod_candidate)

    for term in candidates:
      result = self._grep(term)
      if result:
        filepath, lineno = result
        log_event(logger, logging.DEBUG, "wcp.resolve_found",
                  variable=variable, term=term, file=filepath, line=lineno)
        return filepath, lineno

    log_event(logger, logging.WARNING, "wcp.resolve_failed",
              variable=variable, searched=len(self._sv_files))
    return None

  def _grep(self, term: str) -> tuple[str, int] | None:
    if not self._sv_files:
      return None
    try:
      result = subprocess.run(
        ['grep', '-n', '-w', '-H', '--', term, *self._sv_files],
        capture_output=True, text=True, timeout=5,
      )
      for line in result.stdout.splitlines():
        parts = line.split(':', 2)
        if len(parts) >= 2:
          try:
            return parts[0], int(parts[1])
          except ValueError:
            continue
    except (subprocess.TimeoutExpired, OSError):
      pass
    return None


# ---------------------------------------------------------------------------
# Editor launcher
# ---------------------------------------------------------------------------

class EditorLauncher:
  """Open a source file at a given line in the configured editor."""

  def __init__(self, surfer_cfg: 'SurferConfig'):
    self._surfer_cfg = surfer_cfg

  def open(self, filepath: str, lineno: int, value: str | None = None) -> None:
    cmd = self._surfer_cfg.format_editor_cmd(filepath, lineno)
    terminal = self._surfer_cfg.editor_terminal.lower()
    log_event(logger, logging.DEBUG, "editor.open",
              cmd=cmd, terminal=terminal, file=filepath, line=lineno)

    if terminal == 'tmux':
      self._open_tmux(cmd, value)
    elif terminal == 'iterm2':
      self._open_iterm2(cmd, value)
    elif terminal == 'terminal':
      self._open_terminal_app(cmd, value)
    else:
      env = {**os.environ, 'WAVE_VALUE': value} if value is not None else None
      subprocess.Popen(cmd, shell=True, env=env)

  @staticmethod
  def _env_prefix(value: str | None) -> str:
    """Shell prefix that exports WAVE_VALUE, empty string when value is None."""
    if value is None:
      return ''
    import shlex
    return f'WAVE_VALUE={shlex.quote(value)} '

  def _open_tmux(self, cmd: str, value: str | None = None) -> None:
    subprocess.Popen(['tmux', 'new-window', self._env_prefix(value) + cmd])

  def _open_iterm2(self, cmd: str, value: str | None = None) -> None:
    safe_cmd = (self._env_prefix(value) + cmd).replace('\\', '\\\\').replace('"', '\\"')
    applescript = f'''
      tell application "iTerm2"
        activate
        create window with default profile
        tell current session of current window
          write text "{safe_cmd}"
        end tell
      end tell
    '''
    subprocess.Popen(['osascript', '-e', applescript])

  def _open_terminal_app(self, cmd: str, value: str | None = None) -> None:
    safe_cmd = (self._env_prefix(value) + cmd).replace('"', '\\"')
    applescript = f'''
      tell application "Terminal"
        activate
        do script "{safe_cmd}"
      end tell
    '''
    subprocess.Popen(['osascript', '-e', applescript])


# ---------------------------------------------------------------------------
# WCP listener (rtl-buddy is the WCP client; Surfer connects via --wcp-initiate)
# ---------------------------------------------------------------------------

class SurferWcpListener:
  """
  TCP listener that accepts a single connection from Surfer (--wcp-initiate).
  Performs the WCP handshake then dispatches goto_declaration events to the
  source resolver and editor launcher.
  """

  def __init__(self, surfer_cfg: 'SurferConfig', resolver: SurferSourceResolver,
               editor: EditorLauncher,
               value_reader: WaveformValueReader | None = None):
    self._surfer_cfg = surfer_cfg
    self._resolver = resolver
    self._editor = editor
    self._value_reader = value_reader
    self._stop = threading.Event()
    self._srv: socket.socket | None = None

  def bind(self) -> int:
    """Bind the TCP socket. Returns the actual port (OS-assigned when wcp_port=0).
    Call before launching Surfer so the port is ready when Surfer connects."""
    self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self._srv.bind(('127.0.0.1', self._surfer_cfg.wcp_port))
    self._srv.listen(1)
    self._srv.settimeout(1.0)
    actual_port = self._srv.getsockname()[1]
    log_event(logger, logging.INFO, "wave.wcp_listening", port=actual_port)
    return actual_port

  def run(self) -> None:
    """Accept connections and handle events. Reconnects if Surfer drops."""
    while not self._stop.is_set():
      try:
        conn, addr = self._srv.accept()  # type: ignore[union-attr]
      except TimeoutError:
        continue
      except OSError:
        break
      log_event(logger, logging.INFO, "wave.wcp_connected", addr=str(addr))
      try:
        self._handle_connection(conn)
      except ConnectionError as exc:
        log_event(logger, logging.WARNING, "wcp.connection_lost", reason=str(exc))
      finally:
        conn.close()

  def stop(self) -> None:
    self._stop.set()
    if self._srv:
      try:
        self._srv.close()
      except OSError:
        pass

  def _handle_connection(self, conn: socket.socket) -> None:
    reader = _FrameReader(conn)

    # Send our greeting first — Surfer (WCP server) waits for the client greeting
    # before sending its own. Surfer then sets goto_declaration capability and
    # shows "Go to declaration" in the right-click menu.
    _send_frame(conn, {
      'type': 'greeting',
      'version': _WCP_VERSION,
      'commands': ['goto_declaration'],
    })

    # Receive Surfer's greeting in response
    greeting = reader.read()
    if greeting.get('type') != 'greeting':
      raise ConnectionError(f"Expected greeting, got: {greeting.get('type')}")

    # Event loop
    while not self._stop.is_set():
      msg = reader.read()
      if msg.get('type') == 'event' and msg.get('event') == 'goto_declaration':
        variable = msg.get('variable', '')
        timestamp: int | None = msg.get('timestamp')
        log_event(logger, logging.INFO, "wave.goto_declaration",
                  variable=variable, timestamp=timestamp)
        value = self._emit_value(variable, timestamp)
        result = self._resolver.resolve(variable)
        if result:
          filepath, lineno = result
          self._editor.open(filepath, lineno, value)

  def _emit_value(self, variable: str, timestamp: int | None) -> str | None:
    """Log the signal value at *timestamp* to the console. Returns the value string or None."""
    if self._value_reader is None or timestamp is None:
      return None
    value = self._value_reader.get_value(variable, timestamp)
    if value is not None:
      emit_console_text(f"{variable} = {value}  @  t={timestamp}")
    return value
