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
import shlex
import socket
import socket as _socket_mod
import subprocess
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..config.surfer import SurferConfig
    from ..config.test import TestConfig

from ..errors import FatalRtlBuddyError
from ..logging_utils import emit_console_text, log_event
from .pywellen_compat import require_random_access_api

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
        self._buf = b""

    def read(self) -> dict:
        while b"\x00" not in self._buf:
            chunk = self._sock.recv(_RECV_BUF)
            if not chunk:
                raise ConnectionError("WCP connection closed by peer")
            self._buf += chunk
        frame, _, self._buf = self._buf.partition(b"\x00")
        return json.loads(frame.decode("utf-8"))


def _send_frame(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8") + b"\x00"
    sock.sendall(data)


# ---------------------------------------------------------------------------
# Waveform value reader
# ---------------------------------------------------------------------------


class WaveformValueReader:
    """
    Look up signal values at a specific FST timestamp using pywellen.

    The pywellen Waveform is loaded lazily on the first query and reused.
    A genuine lookup miss (signal not in the waveform) returns None/empty;
    a missing or unreadable trace, or a pywellen without the random-access
    Waveform API, raises FatalRtlBuddyError instead of silently blanking
    every annotation (#263). WaveLauncher calls check() on the main thread
    before Surfer starts so those failures abort the command up front.
    """

    def __init__(self, fst_path: str):
        self._fst_path = fst_path
        self._waveform = None

    def check(self) -> None:
        """Validate the trace path and the pywellen API surface.

        Cheap (no waveform load). Raises FatalRtlBuddyError on failure.
        """
        if not os.path.isfile(self._fst_path):
            log_event(
                logger,
                logging.ERROR,
                "wave.trace_missing",
                path=self._fst_path,
            )
            raise FatalRtlBuddyError(f"waveform trace not found: {self._fst_path}")
        require_random_access_api("rb wave")

    def _load(self):
        if self._waveform is None:
            self.check()
            import pywellen  # type: ignore[import-untyped]  # noqa: PLC0415

            # pywellen emits terminal capability queries to stderr on load; suppress them
            import sys

            old_stderr = sys.stderr
            sys.stderr = open(os.devnull, "w")  # noqa: WPS515
            try:
                self._waveform = pywellen.Waveform(self._fst_path)
            except Exception as e:
                log_event(
                    logger,
                    logging.ERROR,
                    "wave.trace_open_failed",
                    path=self._fst_path,
                    error=str(e),
                )
                raise FatalRtlBuddyError(
                    f"could not open waveform trace {self._fst_path}: {e}"
                ) from e
            finally:
                sys.stderr.close()
                sys.stderr = old_stderr
        return self._waveform

    def get_value(self, variable: str, timestamp: int) -> str | None:
        """Return the signal value string at *timestamp* (FST ticks).

        Returns None when the signal is not in the waveform; anything else
        (broken trace, API break) propagates loudly.
        """
        wf = self._load()
        try:
            sig = wf.get_signal_from_path(variable)
        except RuntimeError:
            # pywellen lookup miss ("No var at path ...")
            return None
        return str(sig.value_at_time(timestamp))

    def get_scope_signals(self, scope_path: str) -> list[tuple[str, str]]:
        """Return [(signal_name, full_fst_path), ...] for all vars directly under scope_path."""
        wf = self._load()
        h = wf.hierarchy
        results = []
        for scope in h.top_scopes():
            results.extend(self._walk_scope(h, scope, scope_path))
        return results

    def _walk_scope(self, h, scope, target_path: str) -> list[tuple[str, str]]:
        if scope.full_name(h) == target_path:
            return [(v.name(h), v.full_name(h)) for v in scope.vars(h)]
        # recurse into child scopes
        results = []
        for child in scope.scopes(h):
            results.extend(self._walk_scope(h, child, target_path))
        return results

    def get_values_bulk(self, full_paths: list[str], timestamp: int) -> dict[str, str]:
        """Return {full_path: value} for all paths present in the waveform."""
        wf = self._load()
        out = {}
        for path in full_paths:
            try:
                sig = wf.get_signal_from_path(path)
            except RuntimeError:
                # pywellen lookup miss — path not in waveform, omit
                continue
            out[path] = str(sig.value_at_time(timestamp))
        return out


# ---------------------------------------------------------------------------
# Scope annotation cache
# ---------------------------------------------------------------------------


def _instance_name(variable: str) -> str:
    """Return the instance/scope component of a hierarchical signal path.

    'tb_top.i_prog_mon.clk' → 'i_prog_mon'
    'tb_top.clk'            → 'tb_top'
    """
    parts = variable.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


class ScopeAnnotationCache:
    """
    Builds and caches a mapping of {full_fst_path → (file, lineno)} for all
    signals in a given FST scope, using a single bulk grep.

    Built once per goto_declaration event; reused on every cursor_moved.
    """

    def __init__(
        self, scope_path: str, signals: list[tuple[str, str]], sv_files: list[str]
    ):
        # signals: [(name, full_fst_path), ...]
        # path_map: {full_fst_path: (filepath, lineno)}
        self.scope_path = scope_path
        self.path_map: dict[str, tuple[str, int]] = {}
        if signals and sv_files:
            self._build(signals, sv_files)

    def _build(self, signals: list[tuple[str, str]], sv_files: list[str]) -> None:
        names = list({name for name, _ in signals})
        if not names:
            return
        pattern = r"\b(" + "|".join(re.escape(n) for n in names) + r")\b"
        try:
            result = subprocess.run(
                ["grep", "-n", "-E", "-H", "--", pattern, *sv_files],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return
        # file:line:content → first hit per signal name wins
        name_to_loc: dict[str, tuple[str, int]] = {}
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            filepath, lineno_str = parts[0], parts[1]
            try:
                lineno = int(lineno_str)
            except ValueError:
                continue
            content = parts[2]
            for name in names:
                if name not in name_to_loc and re.search(
                    r"\b" + re.escape(name) + r"\b", content
                ):
                    name_to_loc[name] = (filepath, lineno)
        # map full_fst_path → (filepath, lineno)
        for name, full_path in signals:
            if name in name_to_loc:
                self.path_map[full_path] = name_to_loc[name]

    def items(self) -> list[tuple[str, str, int]]:
        """Return [(full_fst_path, filepath, lineno), ...]."""
        return [(p, f, lineno) for p, (f, lineno) in self.path_map.items()]


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

    def __init__(self, test_cfg: "TestConfig", suite_dir: str):
        self._sv_files = self._collect_sv_files(test_cfg, suite_dir)
        log_event(
            logger,
            logging.DEBUG,
            "wcp.resolver_ready",
            files=len(self._sv_files),
            suite=suite_dir,
        )

    def _collect_sv_files(self, test_cfg: "TestConfig", suite_dir: str) -> list[str]:
        from ..tools.vlog_filelist import VlogFilelist

        model_cfg = test_cfg.get_model()
        tb_cfg = test_cfg.get_testbench()
        fl = VlogFilelist(
            name="wcp_resolver", model_cfg=model_cfg, output_path="/dev/null"
        )

        # Model source files (resolved from models.yaml location)
        model_fpath = os.path.abspath(model_cfg.get_model_path() or ".")
        model_entries = fl._extract(
            model_cfg.get_filelist(), unroll=True, fpath=model_fpath
        )

        # Testbench source files (resolved from suite dir)
        tb_fpath = os.path.join(suite_dir, "tests.yaml")
        tb_entries = fl._extract(tb_cfg.get_filelist(), unroll=True, fpath=tb_fpath)

        sv_files = []
        for path, opt in model_entries + tb_entries:
            if opt is None or opt.strip() == "-v":
                if os.path.isfile(path):
                    sv_files.append(path)
        return sv_files

    def resolve(self, variable: str) -> tuple[str, int] | None:
        """
        Resolve a hierarchical variable path to (filepath, lineno).

        Tries the rightmost component (signal name) first, then the second-to-last
        (instance/module component) as a fallback.
        """
        parts = variable.split(".")
        candidates = [parts[-1]]
        if len(parts) >= 2:
            # Strip trailing digits from instance name to approximate module name
            mod_candidate = re.sub(r"_\d+$", "", parts[-2])
            if mod_candidate not in candidates:
                candidates.append(mod_candidate)

        for term in candidates:
            result = self._grep(term)
            if result:
                filepath, lineno = result
                log_event(
                    logger,
                    logging.DEBUG,
                    "wcp.resolve_found",
                    variable=variable,
                    term=term,
                    file=filepath,
                    line=lineno,
                )
                return filepath, lineno

        log_event(
            logger,
            logging.WARNING,
            "wcp.resolve_failed",
            variable=variable,
            searched=len(self._sv_files),
        )
        return None

    def _grep(self, term: str) -> tuple[str, int] | None:
        if not self._sv_files:
            return None
        try:
            result = subprocess.run(
                ["grep", "-n", "-w", "-H", "--", term, *self._sv_files],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
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

    def __init__(self, surfer_cfg: "SurferConfig"):
        self._surfer_cfg = surfer_cfg

    def open(self, filepath: str, lineno: int, value: str | None = None) -> None:
        sock = self._surfer_cfg.resolved_editor_sock
        terminal = self._surfer_cfg.editor_terminal.lower()
        log_event(
            logger,
            logging.DEBUG,
            "editor.open",
            file=filepath,
            line=lineno,
            sock=sock or "",
        )

        if sock and self._nvim_socket_alive(sock):
            self._nvim_remote_update(sock, filepath, lineno, value)
            return

        cmd = self._surfer_cfg.format_editor_cmd(filepath, lineno)
        ctrl_sock = self._surfer_cfg.resolved_ctrl_sock
        if sock:
            # First launch: tell nvim to listen so future calls can reuse it
            os.makedirs(os.path.dirname(sock), exist_ok=True)
            cmd = cmd + f" --listen {shlex.quote(sock)}"

        if terminal == "tmux":
            self._open_tmux(cmd, value, ctrl_sock)
        elif terminal == "iterm2":
            self._open_iterm2(cmd, value, ctrl_sock)
        elif terminal == "terminal":
            self._open_terminal_app(cmd, value, ctrl_sock)
        else:
            env = {**os.environ}
            if value is not None:
                env["WAVE_VALUE"] = value
            if ctrl_sock:
                env["WAVE_CTRL_SOCK"] = ctrl_sock
            subprocess.Popen(
                cmd, shell=True, env=env if len(env) > len(os.environ) else None
            )

    @staticmethod
    def _env_prefix(value: str | None, ctrl_sock: str | None = None) -> str:
        parts = []
        if value is not None:
            parts.append(f"WAVE_VALUE={shlex.quote(value)}")
        if ctrl_sock:
            parts.append(f"WAVE_CTRL_SOCK={shlex.quote(ctrl_sock)}")
        return (" ".join(parts) + " ") if parts else ""

    @staticmethod
    def _nvim_socket_alive(sock_path: str) -> bool:
        """Return True if a live nvim process is listening on sock_path."""
        try:
            s = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect(os.path.expanduser(sock_path))
            s.close()
            return True
        except OSError:
            return False

    @staticmethod
    def _nvim_exec_lua(sock_path: str, lua: str) -> None:
        """Execute a Lua chunk in a running nvim silently via --remote-expr nvim_exec2."""
        expanded = os.path.expanduser(sock_path)
        # Wrap in a double-quoted Vimscript string; escape \ and " for that context.
        vs = lua.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(
            [
                "nvim",
                "--server",
                expanded,
                "--remote-expr",
                f'nvim_exec2("lua {vs}", {{}})',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _nvim_remote_update(
        sock_path: str, filepath: str, lineno: int, value: str | None
    ) -> None:
        """Jump to filepath:lineno in a running nvim and update virtual text."""
        expanded = os.path.expanduser(sock_path)
        vim_path = (
            filepath.replace("\\", "\\\\").replace(" ", "\\ ").replace('"', '\\"')
        )
        # File navigation still needs --remote-send (no expr equivalent for :e)
        subprocess.Popen(
            [
                "nvim",
                "--server",
                expanded,
                "--remote-send",
                f"<Esc>:e +{lineno} {vim_path}<CR>",
            ]
        )
        if value is not None:
            v = value.replace("\\", "\\\\").replace("'", "\\'")
            lua = (
                "local ns=vim.api.nvim_create_namespace('wave_value');"
                "vim.api.nvim_buf_clear_namespace(0,ns,0,-1);"
                f"local l,v={lineno},'{v}';"
                "local lc=vim.api.nvim_buf_line_count(0);"
                "if l>=1 and l<=lc then "
                "vim.api.nvim_buf_set_extmark(0,ns,l-1,0,"
                "{virt_text={{'▶ '..v,'WaveValue'}},virt_text_pos='eol'}) end"
            )
            EditorLauncher._nvim_exec_lua(sock_path, lua)

    @staticmethod
    def _nvim_remote_value(sock_path: str, lineno: int, value: str) -> None:
        """Update virtual text for a single line in a running nvim."""
        v = value.replace("\\", "\\\\").replace("'", "\\'")
        lua = (
            "local ns=vim.api.nvim_create_namespace('wave_value');"
            "vim.api.nvim_buf_clear_namespace(0,ns,0,-1);"
            f"local l,v={lineno},'{v}';"
            "local lc=vim.api.nvim_buf_line_count(0);"
            "if l>=1 and l<=lc then "
            "vim.api.nvim_buf_set_extmark(0,ns,l-1,0,"
            "{virt_text={{'▶ '..v,'WaveValue'}},virt_text_pos='eol'}) end"
        )
        EditorLauncher._nvim_exec_lua(sock_path, lua)

    @staticmethod
    def _nvim_remote_scope(
        sock_path: str, annotations: list[tuple[int, str, str]]
    ) -> None:
        """Push virtual text for all scope signals silently.

        Each annotation is (lineno, value, filepath). Extmarks are set in the
        correct buffer for each file; lines out of range are silently skipped.
        """
        entries = []
        for lineno, value, filepath in annotations:
            v = value.replace("\\", "\\\\").replace("'", "\\'")
            f = filepath.replace("\\", "\\\\").replace("'", "\\'")
            entries.append(f"{{{lineno},'{v}','{f}'}}")
        lua_table = "{" + ",".join(entries) + "}"
        lua = (
            "local ns=vim.api.nvim_create_namespace('wave_value');"
            "for _,b in ipairs(vim.api.nvim_list_bufs()) do "
            "if vim.api.nvim_buf_is_loaded(b) then "
            "vim.api.nvim_buf_clear_namespace(b,ns,0,-1) end end;"
            f"for _,x in ipairs({lua_table}) do "
            "local l,v,f=x[1],x[2],x[3];"
            "local buf=vim.fn.bufnr(f);"
            "if buf~=-1 then "
            "local lc=vim.api.nvim_buf_line_count(buf);"
            "if l>=1 and l<=lc then "
            "vim.api.nvim_buf_set_extmark(buf,ns,l-1,0,"
            "{virt_text={{'▶ '..v,'WaveValue'}},virt_text_pos='eol'}) "
            "end end end"
        )
        EditorLauncher._nvim_exec_lua(sock_path, lua)

    def _open_tmux(
        self, cmd: str, value: str | None = None, ctrl_sock: str | None = None
    ) -> None:
        subprocess.Popen(
            ["tmux", "new-window", self._env_prefix(value, ctrl_sock) + cmd]
        )

    def _open_iterm2(
        self, cmd: str, value: str | None = None, ctrl_sock: str | None = None
    ) -> None:
        safe_cmd = (
            (self._env_prefix(value, ctrl_sock) + cmd)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )
        applescript = f'''
      tell application "iTerm2"
        activate
        create window with default profile
        tell current session of current window
          write text "{safe_cmd}"
        end tell
      end tell
    '''
        subprocess.Popen(["osascript", "-e", applescript])

    def _open_terminal_app(
        self, cmd: str, value: str | None = None, ctrl_sock: str | None = None
    ) -> None:
        safe_cmd = (self._env_prefix(value, ctrl_sock) + cmd).replace('"', '\\"')
        applescript = f'''
      tell application "Terminal"
        activate
        do script "{safe_cmd}"
      end tell
    '''
        subprocess.Popen(["osascript", "-e", applescript])


# ---------------------------------------------------------------------------
# Wave control server (nvim → rtl-buddy → Surfer)
# ---------------------------------------------------------------------------


class WaveControlServer:
    """
    Unix-domain socket server that lets external tools (e.g. nvim) send
    commands to a running rb wave session.

    Accepts newline-delimited JSON on the socket path configured by ctrl-sock.
    Supported commands:
      {"cmd": "add_variable", "name": "<signal_name>"}
        — resolves the signal against the active scope cache and adds it to
          Surfer's waveform view via the live WCP connection.
    """

    def __init__(self, sock_path: str, listener: "SurferWcpListener"):
        self._sock_path = os.path.expanduser(sock_path)
        self._listener = listener
        self._stop = threading.Event()

    def start(self) -> None:
        """Bind and start serving in a daemon thread."""
        os.makedirs(os.path.dirname(self._sock_path), exist_ok=True)
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)
        t = threading.Thread(target=self._serve, daemon=True, name="wave-ctrl")
        t.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            os.unlink(self._sock_path)
        except OSError:
            pass

    def _serve(self) -> None:
        srv = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
        srv.bind(self._sock_path)
        srv.listen(4)
        srv.settimeout(1.0)
        log_event(logger, logging.INFO, "wave.ctrl_listening", path=self._sock_path)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        srv.close()

    def _handle(self, conn: _socket_mod.socket) -> None:
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        finally:
            conn.close()
        for line in buf.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("cmd") == "add_variable":
                name = msg.get("name", "").strip()
                if name:
                    self._listener.add_variable_to_surfer(name)


# ---------------------------------------------------------------------------
# WCP listener (rtl-buddy is the WCP client; Surfer connects via --wcp-initiate)
# ---------------------------------------------------------------------------


class SurferWcpListener:
    """
    TCP listener that accepts a single connection from Surfer (--wcp-initiate).
    Performs the WCP handshake then dispatches goto_declaration events to the
    source resolver and editor launcher.
    """

    def __init__(
        self,
        surfer_cfg: "SurferConfig",
        resolver: SurferSourceResolver,
        editor: EditorLauncher,
        value_reader: WaveformValueReader | None = None,
        scope_annotation: bool = True,
    ):
        self._surfer_cfg = surfer_cfg
        self._resolver = resolver
        self._editor = editor
        self._value_reader = value_reader
        self._scope_annotation = scope_annotation
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self._last_decl: tuple[str, int] | None = (
            None  # (variable, lineno) from last goto_declaration
        )
        self._scope_cache: ScopeAnnotationCache | None = None
        self._last_timestamp: int | None = None
        self._wcp_conn: socket.socket | None = (
            None  # live connection to Surfer for sending commands
        )
        self.event_observer: "Callable[[str, dict], None] | None" = None
        """Optional callback invoked for relevant WCP events. The hub-bridge
        adapter sets this; the listener stays free of hub awareness."""

        # Ordered list of pending reply waiters. WCP has no request IDs, so
        # the only correlation guarantee is "responses arrive in send order".
        # A caller registers a waiter right after sending a command; the WCP
        # reader thread fills the first compatible waiter when a frame lands.
        #
        # Two frame kinds resolve a waiter:
        #   * a ``response`` frame whose ``command`` is in the waiter's
        #     ``commands`` set (surfer tags named responses with the command
        #     name; shared acks carry ``command == "ack"``), or
        #   * an ``error`` frame, which has no command to correlate on — it
        #     fills the first waiter that opted into errors (``accept_error``).
        # Because hub-driven commands are handled serially on the bridge
        # reader thread, at most one error-accepting waiter is outstanding at
        # a time in practice, so first-match is the right correlation.
        self._waiters: list[dict] = []
        self._waiters_lock = threading.Lock()

    def send_to_surfer(self, obj: dict) -> None:
        """Send a WCP command frame to Surfer if connected."""
        if self._wcp_conn is not None:
            try:
                _send_frame(self._wcp_conn, obj)
            except OSError:
                self._wcp_conn = None

    def _register_waiter(self, commands: "set[str]", accept_error: bool) -> dict:
        waiter = {
            "commands": frozenset(commands),
            "accept_error": accept_error,
            "event": threading.Event(),
            "result": None,
        }
        with self._waiters_lock:
            self._waiters.append(waiter)
        return waiter

    def _wait_waiter(self, waiter: dict, timeout: float) -> dict | None:
        if not waiter["event"].wait(timeout):
            # Reclaim the slot so a late frame doesn't fill a stale waiter.
            with self._waiters_lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            return None
        return waiter["result"]

    def await_response(self, command: str, timeout: float = 2.0) -> dict | None:
        """Wait for the next response frame whose ``command`` matches.

        The caller is responsible for calling this *immediately after*
        sending the matching WCP command so the send-order correlation
        across callers stays consistent. Returns the response dict (with
        ``command`` and any payload fields), or ``None`` on timeout. Error
        frames do not resolve this waiter — use :meth:`await_reply` when the
        caller wants to surface surfer-side rejections.
        """
        waiter = self._register_waiter({command}, accept_error=False)
        result = self._wait_waiter(waiter, timeout)
        if result and result.get("kind") == "response":
            return result.get("msg")
        return None

    def await_reply(
        self, commands: "set[str]", timeout: float = 2.0
    ) -> "tuple[str, dict] | None":
        """Wait for the next response (``command`` in *commands*) or error.

        Returns ``("response", msg)`` on a matching response frame,
        ``("error", msg)`` when surfer rejects the command, or ``None`` on
        timeout. The error case has no command correlation (WCP errors carry
        no command field), so this relies on commands being driven serially.
        """
        waiter = self._register_waiter(set(commands), accept_error=True)
        result = self._wait_waiter(waiter, timeout)
        if result is None:
            return None
        return (result["kind"], result["msg"])

    def _dispatch_response(self, msg: dict) -> None:
        command = msg.get("command")
        if not isinstance(command, str):
            return
        with self._waiters_lock:
            target = next((w for w in self._waiters if command in w["commands"]), None)
            if target is None:
                return
            self._waiters.remove(target)
        target["result"] = {"kind": "response", "msg": msg}
        target["event"].set()

    def _dispatch_error(self, msg: dict) -> None:
        with self._waiters_lock:
            target = next((w for w in self._waiters if w["accept_error"]), None)
            if target is None:
                log_event(
                    logger,
                    logging.DEBUG,
                    "wcp.error_unmatched",
                    error=str(msg.get("error", "")),
                    message=str(msg.get("message", "")),
                )
                return
            self._waiters.remove(target)
        target["result"] = {"kind": "error", "msg": msg}
        target["event"].set()

    def add_variable_to_surfer(self, name: str) -> None:
        """Resolve *name* against the active scope cache, add to Surfer, and annotate nvim."""
        if self._scope_cache is None:
            log_event(logger, logging.WARNING, "wcp.no_scope_context", name=name)
            return
        full_path = f"{self._scope_cache.scope_path}.{name}"
        self.send_to_surfer(
            {"type": "command", "command": "add_variables", "variables": [full_path]}
        )
        log_event(logger, logging.INFO, "wcp.add_variable", name=name, path=full_path)
        # Re-annotate all scope signals so existing ones aren't wiped
        if self._last_timestamp is not None and self._value_reader is not None:
            self._push_scope_values(self._last_timestamp)

    def bind(self) -> int:
        """Bind the TCP socket. Returns the actual port (OS-assigned when wcp_port=0).
        Call before launching Surfer so the port is ready when Surfer connects."""
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self._surfer_cfg.wcp_port))
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
                log_event(
                    logger, logging.WARNING, "wcp.connection_lost", reason=str(exc)
                )
            except FatalRtlBuddyError as exc:
                # The lazy waveform open can fail here (present-but-corrupt
                # trace) — tear down gracefully instead of dying with a
                # listener-thread traceback (#263).
                log_event(logger, logging.ERROR, "wcp.fatal_error", error=str(exc))
                self.stop()
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
        self._wcp_conn = conn
        reader = _FrameReader(conn)

        # Send our greeting first — Surfer (WCP server) waits for the client greeting
        # before sending its own. Surfer then sets goto_declaration capability and
        # shows "Go to declaration" in the right-click menu.
        _send_frame(
            conn,
            {
                "type": "greeting",
                "version": _WCP_VERSION,
                "commands": ["goto_declaration"],
            },
        )

        # Receive Surfer's greeting in response
        greeting = reader.read()
        if greeting.get("type") != "greeting":
            raise ConnectionError(f"Expected greeting, got: {greeting.get('type')}")

        # Event loop
        while not self._stop.is_set():
            msg = reader.read()
            if msg.get("type") == "event" and msg.get("event") == "goto_declaration":
                variable = msg.get("variable", "")
                timestamp: int | None = msg.get("timestamp")
                log_event(
                    logger,
                    logging.INFO,
                    "wave.goto_declaration",
                    variable=variable,
                    timestamp=timestamp,
                )
                if timestamp is not None:
                    self._last_timestamp = timestamp
                value = self._emit_value(variable, timestamp)
                result = self._resolver.resolve(variable)
                if result:
                    filepath, lineno = result
                    self._last_decl = (variable, lineno)
                    self._editor.open(filepath, lineno, value)
                    if self._scope_annotation and self._value_reader is not None:
                        self._build_scope_cache(variable, timestamp)
                self._notify_observer("goto_declaration", msg)
            elif msg.get("type") == "event" and msg.get("event") == "cursor_moved":
                timestamp = msg.get("timestamp")
                if timestamp is not None:
                    self._last_timestamp = timestamp
                self._on_cursor_moved(timestamp)
                self._notify_observer("cursor_moved", msg)
            elif msg.get("type") == "event" and msg.get("event") == "scope_changed":
                scope = msg.get("scope", "")
                if scope:
                    self._on_scope_changed(scope)
                self._notify_observer("scope_changed", msg)
            elif msg.get("type") == "response":
                self._dispatch_response(msg)
            elif msg.get("type") == "error":
                self._dispatch_error(msg)

    def _notify_observer(self, event_name: str, msg: dict) -> None:
        observer = self.event_observer
        if observer is None:
            return
        try:
            observer(event_name, msg)
        except Exception:
            logger.exception("wave.event_observer.unhandled_error event=%s", event_name)

    def _emit_value(self, variable: str, timestamp: int | None) -> str | None:
        """Log the signal value at *timestamp* to the console. Returns the display string or None."""
        if self._value_reader is None or timestamp is None:
            return None
        raw = self._value_reader.get_value(variable, timestamp)
        if raw is not None:
            display = f"{raw} [{_instance_name(variable)}]"
            emit_console_text(f"{variable} = {display}  @  t={timestamp}")
            return display
        return None

    def _build_scope_cache(self, variable: str, timestamp: int | None) -> None:
        """Enumerate FST signals in the variable's parent scope and bulk-grep source locations."""
        parts = variable.split(".")
        scope_path = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
        signals = self._value_reader.get_scope_signals(scope_path)  # type: ignore[union-attr]
        sv_files = self._resolver._sv_files
        self._scope_cache = ScopeAnnotationCache(scope_path, signals, sv_files)
        log_event(
            logger,
            logging.DEBUG,
            "wcp.scope_cache_built",
            scope=scope_path,
            signals=len(signals),
            mapped=len(self._scope_cache.path_map),
        )
        if timestamp is not None:
            self._push_scope_values(timestamp)

    def _on_cursor_moved(self, timestamp: int | None) -> None:
        """Update nvim virtual text when the Surfer time cursor moves."""
        if not self._scope_annotation:
            return
        if timestamp is None or self._value_reader is None:
            return
        if self._scope_cache is not None:
            self._push_scope_values(timestamp)
        elif self._last_decl is not None:
            # fallback: single-signal update until cache is ready
            variable, lineno = self._last_decl
            raw = self._value_reader.get_value(variable, timestamp)
            if raw is not None:
                display = f"{raw} [{_instance_name(variable)}]"
                emit_console_text(f"{variable} = {display}  @  t={timestamp}")
                sock = self._surfer_cfg.resolved_editor_sock
                if sock and EditorLauncher._nvim_socket_alive(sock):
                    EditorLauncher._nvim_remote_value(sock, lineno, display)

    def _on_scope_changed(self, scope: str) -> None:
        """Rebuild the scope annotation cache when the user focuses a different scope in Surfer."""
        if not self._scope_annotation or self._value_reader is None:
            return
        if self._scope_cache is not None and self._scope_cache.scope_path == scope:
            return  # same scope, nothing to do
        log_event(logger, logging.DEBUG, "wcp.scope_changed", scope=scope)
        emit_console_text(f"scope: {scope}")
        signals = self._value_reader.get_scope_signals(scope)
        sv_files = self._resolver._sv_files
        self._scope_cache = ScopeAnnotationCache(scope, signals, sv_files)
        log_event(
            logger,
            logging.DEBUG,
            "wcp.scope_cache_built",
            scope=scope,
            signals=len(signals),
            mapped=len(self._scope_cache.path_map),
        )

    def _push_scope_values(self, timestamp: int) -> None:
        """Look up all cached scope signals and push bulk virtual text update to nvim."""
        import time

        assert self._scope_cache is not None
        assert self._value_reader is not None
        sock = self._surfer_cfg.resolved_editor_sock
        if not sock:
            return
        # On first goto_declaration nvim is just starting; wait up to 5s for the socket.
        if not EditorLauncher._nvim_socket_alive(sock):
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                time.sleep(0.1)
                if EditorLauncher._nvim_socket_alive(sock):
                    break
            else:
                log_event(logger, logging.WARNING, "wcp.nvim_socket_timeout", sock=sock)
                return
        full_paths = [p for p, _, _ in self._scope_cache.items()]
        values = self._value_reader.get_values_bulk(full_paths, timestamp)
        inst = self._scope_cache.scope_path.split(".")[-1]

        # Group signals by source line; two signals on the same line are combined
        # into a single annotation: "a=1'b0  b=1'b1 [inst]"
        line_groups: dict[tuple[str, int], list[tuple[str, str]]] = {}
        for full_path, filepath, lineno in self._scope_cache.items():
            if full_path in values:
                sig = full_path.split(".")[-1]
                key = (filepath, lineno)
                line_groups.setdefault(key, []).append((sig, values[full_path]))

        annotations: list[tuple[int, str, str]] = []
        for (filepath, lineno), sigs in line_groups.items():
            if len(sigs) == 1:
                display = f"{sigs[0][1]} [{inst}]"
            else:
                display = "  ".join(f"{n}={v}" for n, v in sigs) + f" [{inst}]"
            annotations.append((lineno, display, filepath))

        if annotations:
            EditorLauncher._nvim_remote_scope(sock, annotations)
