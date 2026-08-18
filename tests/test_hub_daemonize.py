"""Tests for ``rb hub start --daemon`` background detach (issue #387).

``--daemon`` used to print "not implemented yet" and then block in the
foreground forever. These cover the three seams of the real detach:

* :func:`build_daemon_argv` — the child command line, and above all
  that it always carries ``--foreground`` (otherwise the daemon
  re-daemonises itself in a loop).
* :func:`wait_for_record` — the readiness handshake: return only on the
  child's *own* record, fail loudly with the log tail otherwise.
* the CLI wiring — ``--daemon`` must hand off *before* the expensive
  start-up work (view.json generation, viewer-bundle discovery, socket
  binds), so all of it runs in the exec'd child rather than in a forked
  copy of the parent interpreter.

Plus one end-to-end run of the real CLI, which is the actual #387
regression: the command has to return promptly, leave ``hub.json``
behind, and leave a live hub serving on the recorded ports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.hub import daemonize, discovery
from rtl_buddy.hub.cli import app as hub_app


# --------------------------------------------------------------------------
# build_daemon_argv
# --------------------------------------------------------------------------


def test_argv_always_forces_foreground():
    """The child must never re-enter the daemon branch."""
    argv = daemonize.build_daemon_argv(python="/usr/bin/python3")
    assert argv[:6] == [
        "/usr/bin/python3",
        "-m",
        "rtl_buddy",
        "hub",
        "start",
        "--foreground",
    ]
    assert "--daemon" not in argv


def test_argv_defaults_to_current_interpreter():
    assert daemonize.build_daemon_argv()[0] == sys.executable


def test_argv_omits_unset_options():
    argv = daemonize.build_daemon_argv(python="py")
    assert argv == ["py", "-m", "rtl_buddy", "hub", "start", "--foreground"]


def test_argv_round_trips_every_option(tmp_path: Path):
    bundle = tmp_path / "spa"
    models = tmp_path / "models.yaml"
    perf = tmp_path / "axi-perf.json"
    argv = daemonize.build_daemon_argv(
        python="py",
        serve_viewer=True,
        viewer_bundle=bundle,
        listen_port=0,
        http_port=8080,
        model="top",
        models_file=models,
        axi_perf_from=perf,
    )
    assert "--serve-viewer" in argv
    for flag, value in (
        ("--viewer-bundle", str(bundle)),
        ("--listen-port", "0"),
        ("--http-port", "8080"),
        ("--model", "top"),
        ("--models-file", str(models)),
        ("--axi-perf-from", str(perf)),
    ):
        assert argv[argv.index(flag) + 1] == value


def test_argv_keeps_port_zero_distinct_from_unset():
    """``--listen-port 0`` (OS-assigned) is not the same as no flag."""
    assert "--listen-port" in daemonize.build_daemon_argv(listen_port=0)
    assert "--listen-port" not in daemonize.build_daemon_argv(listen_port=None)


# --------------------------------------------------------------------------
# spawn_detached
# --------------------------------------------------------------------------


def test_spawn_detached_new_session_devnull_stdin_and_log(tmp_path: Path):
    """The child leads its own session, has no stdin, and logs to file."""
    log = tmp_path / ".rtl-buddy" / "hub.log"
    script = (
        "import os,sys;"
        "print('sid', os.getsid(0));"
        "print('stdin-empty', repr(sys.stdin.read()));"
        "sys.stderr.write('on-stderr\\n')"
    )
    proc = daemonize.spawn_detached(
        [sys.executable, "-c", script], project_root=tmp_path, log_path=log
    )
    assert proc.wait(timeout=30) == 0

    text = log.read_text()
    assert f"sid {os.getsid(0)}" not in text, "child stayed in the parent session"
    assert "stdin-empty ''" in text
    assert "on-stderr" in text


def test_spawn_detached_appends_rather_than_truncates(tmp_path: Path):
    log = tmp_path / "hub.log"
    log.write_text("earlier run\n")
    proc = daemonize.spawn_detached(
        [sys.executable, "-c", "print('later run')"],
        project_root=tmp_path,
        log_path=log,
    )
    assert proc.wait(timeout=30) == 0
    assert log.read_text().splitlines() == ["earlier run", "later run"]


# --------------------------------------------------------------------------
# wait_for_record
# --------------------------------------------------------------------------


class _FakeProc:
    """Minimal Popen stand-in: a pid plus a scripted ``poll()`` sequence."""

    def __init__(self, pid: int, exit_codes: list[int | None] | None = None) -> None:
        self.pid = pid
        self._codes = list(exit_codes or [])
        self.terminated = False

    def poll(self) -> int | None:
        if not self._codes:
            return None
        return self._codes.pop(0)

    def terminate(self) -> None:
        self.terminated = True


def _write_record(project_root: Path, *, pid: int) -> None:
    discovery.write_record(
        project_root,
        pid=pid,
        tcp="127.0.0.1:1234",
        server_version="0.0.0+test",
        http_port=5678,
    )


def test_wait_for_record_returns_the_childs_record(tmp_path: Path):
    _write_record(tmp_path, pid=4242)
    record = daemonize.wait_for_record(
        tmp_path,
        proc=_FakeProc(4242),  # type: ignore[arg-type]
        log_path=tmp_path / "hub.log",
        timeout_s=5.0,
    )
    assert record.pid == 4242
    assert record.http_port == 5678


def test_wait_for_record_ignores_a_stale_record(tmp_path: Path):
    """A leftover hub.json from a dead hub must not read as success."""
    _write_record(tmp_path, pid=1)
    with pytest.raises(daemonize.DaemonStartError) as excinfo:
        daemonize.wait_for_record(
            tmp_path,
            proc=_FakeProc(4242),  # type: ignore[arg-type]
            log_path=tmp_path / "hub.log",
            timeout_s=0.2,
        )
    assert "timed out" in str(excinfo.value)


def test_wait_for_record_reports_a_child_that_died(tmp_path: Path):
    log = tmp_path / "hub.log"
    log.write_text("boom: no models.yaml found\n")
    with pytest.raises(daemonize.DaemonStartError) as excinfo:
        daemonize.wait_for_record(
            tmp_path,
            proc=_FakeProc(4242, exit_codes=[2]),  # type: ignore[arg-type]
            log_path=log,
            timeout_s=5.0,
        )
    assert "exited with code 2" in str(excinfo.value)
    assert "no models.yaml found" in excinfo.value.log_tail


def test_wait_for_record_accepts_a_record_written_just_before_exit(tmp_path: Path):
    """Race: the record lands between our read and the exit poll."""
    proc = _FakeProc(4242, exit_codes=[None])

    original_poll = proc.poll

    def poll_and_publish() -> int | None:
        code = original_poll()
        if code is None:
            _write_record(tmp_path, pid=4242)
            proc._codes = [0]
        return code

    proc.poll = poll_and_publish  # type: ignore[method-assign]
    # First iteration: no record, poll() returns None and publishes one.
    # Second iteration reads it.
    record = daemonize.wait_for_record(
        tmp_path,
        proc=proc,  # type: ignore[arg-type]
        log_path=tmp_path / "hub.log",
        timeout_s=5.0,
    )
    assert record.pid == 4242


def test_start_detached_terminates_a_child_that_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A hung child must not be left orphaned and undiscoverable."""
    fake = _FakeProc(4242)
    monkeypatch.setattr(daemonize, "spawn_detached", lambda *a, **k: fake)
    with pytest.raises(daemonize.DaemonStartError):
        daemonize.start_detached(tmp_path, log_path=tmp_path / "hub.log", timeout_s=0.2)
    assert fake.terminated


def test_ready_timeout_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(daemonize.READY_TIMEOUT_ENV, "7.5")
    assert daemonize.ready_timeout_s() == 7.5
    monkeypatch.setenv(daemonize.READY_TIMEOUT_ENV, "not-a-number")
    assert daemonize.ready_timeout_s() == daemonize.DEFAULT_READY_TIMEOUT_S
    monkeypatch.delenv(daemonize.READY_TIMEOUT_ENV)
    assert daemonize.ready_timeout_s() == daemonize.DEFAULT_READY_TIMEOUT_S


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_daemon_defers_all_start_work_to_the_child(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """The ordering guarantee behind the fix.

    ``--daemon`` must not run the server loop (nor the ``--model``
    view.json build, nor viewer-bundle discovery, all of which live
    behind it) in this process — that work belongs to the exec'd child,
    after the detach.
    """
    from rtl_buddy.hub import cli as hub_cli

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("serve() ran in the --daemon parent")

    monkeypatch.setattr(hub_cli.hub_loop, "serve", _boom)
    monkeypatch.setattr(
        hub_cli.hub_view_builder, "build_view_json", _boom, raising=False
    )

    seen: dict[str, object] = {}

    def _fake_start(root, **kwargs):
        seen["root"] = root
        seen.update(kwargs)
        return discovery.HubRecord(
            v=1,
            pid=999,
            tcp="127.0.0.1:5555",
            server_version="0.0.0+test",
            project_root=str(root),
            started_at="2026-01-01T00:00:00+00:00",
            http_port=6666,
        )

    monkeypatch.setattr(daemonize, "start_detached", _fake_start)

    result = CliRunner().invoke(
        hub_app, ["start", "--daemon", "--serve-viewer", "--http-port", "6666"]
    )
    assert result.exit_code == 0, result.output
    assert seen["serve_viewer"] is True
    assert seen["http_port"] == 6666
    assert seen["log_path"] == (project_root / ".rtl-buddy" / "hub.log").resolve()
    assert "pid 999" in result.output
    assert "127.0.0.1:6666" in result.output


def test_cli_daemon_reports_a_failed_child_with_the_log_tail(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
):
    def _fail(root, **kwargs):
        raise daemonize.DaemonStartError(
            "the detached hub (pid 5) exited with code 2",
            log_tail="no models.yaml found under /x",
        )

    monkeypatch.setattr(daemonize, "start_detached", _fail)
    result = CliRunner().invoke(hub_app, ["start", "--daemon"])
    assert result.exit_code == 1
    assert "exited with code 2" in result.output
    assert "no models.yaml found" in result.output


def test_cli_start_help_documents_the_detach():
    result = CliRunner().invoke(hub_app, ["start", "--help"])
    assert result.exit_code == 0, result.output
    assert "not implemented" not in result.output


# --------------------------------------------------------------------------
# end-to-end (#387 regression)
# --------------------------------------------------------------------------


def _http_status(url: str, *, timeout: float = 5.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:  # pragma: no cover - defensive
        return exc.code


@pytest.fixture
def reap_daemon(tmp_path: Path):
    """Kill whatever hub ``tmp_path``'s hub.json names, pass or fail.

    Registered *before* the assertions, not around them: a test that
    fails on its first assertion would otherwise leak a live daemon
    into the developer's session.
    """
    yield
    record_path = tmp_path / ".rtl-buddy" / "hub.json"
    if not record_path.is_file():
        return
    try:
        pid = json.loads(record_path.read_text())["pid"]
    except (OSError, ValueError, KeyError):  # pragma: no cover - defensive
        return
    for sig in (15, 9):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(100):
            if not discovery._pid_is_live(pid):  # noqa: SLF001
                return
            time.sleep(0.1)


def test_e2e_daemon_serve_viewer_returns_and_serves(tmp_path: Path, reap_daemon: None):
    """#387: ``--daemon --serve-viewer`` from a non-tty must not hang.

    stdin/stdout are pipes here (never a tty), which is the exact shape
    of the agent-shell invocation in the bug report.
    """
    (tmp_path / "root_config.yaml").write_text(
        "rtl-buddy-filetype: project_root_config\n"
    )
    bundle = tmp_path / "spa"
    bundle.mkdir()
    (bundle / "index.html").write_text("<html><body>spa</body></html>")

    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "hub",
            "start",
            "--daemon",
            "--serve-viewer",
            "--viewer-bundle",
            str(bundle),
            "--listen-port",
            "0",
            "--http-port",
            "0",
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - started
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output

    record_path = tmp_path / ".rtl-buddy" / "hub.json"
    assert record_path.is_file(), f"no hub.json after {elapsed:.1f}s: {output}"
    record = json.loads(record_path.read_text())
    # It really detached: the `rb` invocation has returned, and the hub
    # it started is a different, still-live process.
    assert record["pid"] != os.getpid()
    assert discovery._pid_is_live(record["pid"])  # noqa: SLF001
    assert (tmp_path / ".rtl-buddy" / "hub.log").is_file()

    base = f"http://127.0.0.1:{record['http_port']}"
    assert _http_status(f"{base}/") == 200
    assert _http_status(f"{base}/sch") == 200
