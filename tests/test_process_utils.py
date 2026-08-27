import signal
import subprocess
import time

import pytest

from rtl_buddy import process_utils


class FakeProcess:
    def __init__(self, *, returncode=0, communicate_exc=None):
        self.pid = 4321
        self.returncode = returncode
        self.communicate_exc = communicate_exc
        self.wait_calls = []
        self.completed = False
        self.killed = False
        self.terminated = False

    def poll(self):
        if self.completed:
            return self.returncode
        if self.killed:
            return -9
        if self.terminated:
            return -15
        return None

    def communicate(self, timeout=None):
        if self.communicate_exc is not None:
            exc = self.communicate_exc
            self.communicate_exc = None
            raise exc
        self.completed = True
        return "", ""

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.poll()

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_run_managed_process_terminates_group_on_keyboard_interrupt(monkeypatch):
    proc = FakeProcess(communicate_exc=KeyboardInterrupt)
    signals_sent = []

    monkeypatch.setattr(
        process_utils.subprocess,
        "Popen",
        lambda *args, **kwargs: proc,
    )
    monkeypatch.setattr(
        process_utils.os,
        "killpg",
        lambda pid, sig: signals_sent.append((pid, sig)),
    )

    with pytest.raises(KeyboardInterrupt):
        process_utils.run_managed_process(["tool"])

    assert signals_sent == [(proc.pid, signal.SIGTERM)]
    assert proc.wait_calls == [5]


def test_run_managed_process_uses_timeout_signal_and_returncode(monkeypatch):
    proc = FakeProcess(
        returncode=0,
        communicate_exc=subprocess.TimeoutExpired(["tool"], timeout=10),
    )
    signals_sent = []

    monkeypatch.setattr(
        process_utils.subprocess,
        "Popen",
        lambda *args, **kwargs: proc,
    )
    monkeypatch.setattr(
        process_utils.os,
        "killpg",
        lambda pid, sig: signals_sent.append((pid, sig)),
    )

    result = process_utils.run_managed_process(
        ["tool"],
        timeout=10,
        timeout_returncode=4444,
        terminate_signal=signal.SIGQUIT,
    )

    assert result.returncode == 4444
    assert result.timed_out
    assert signals_sent == [(proc.pid, signal.SIGQUIT)]


def test_run_managed_process_falls_back_when_group_signal_denied(monkeypatch):
    proc = FakeProcess(communicate_exc=KeyboardInterrupt)
    direct_signals = []

    def _deny_group_signal(_pid, _sig):
        raise PermissionError

    proc.send_signal = direct_signals.append
    monkeypatch.setattr(
        process_utils.subprocess,
        "Popen",
        lambda *args, **kwargs: proc,
    )
    monkeypatch.setattr(process_utils.os, "killpg", _deny_group_signal)

    with pytest.raises(KeyboardInterrupt):
        process_utils.run_managed_process(["tool"])

    assert direct_signals == [signal.SIGTERM]


def test_run_managed_process_restores_signal_handlers(monkeypatch):
    proc = FakeProcess()
    original_int = signal.getsignal(signal.SIGINT)
    original_term = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(
        process_utils.subprocess,
        "Popen",
        lambda *args, **kwargs: proc,
    )

    process_utils.run_managed_process(["tool"])

    assert signal.getsignal(signal.SIGINT) == original_int
    assert signal.getsignal(signal.SIGTERM) == original_term


def test_run_managed_process_works_from_worker_thread(monkeypatch):
    """``signal.signal()`` only works in the main thread, so callers
    invoking ``run_managed_process`` from a worker thread (e.g. the
    hub's ``asyncio.to_thread`` per-model lock in
    ``rb hub start --model``) MUST NOT crash. The helper has to
    detect non-main-thread context and skip the signal-handler
    install/restore entirely.
    """
    import threading

    proc = FakeProcess()
    monkeypatch.setattr(
        process_utils.subprocess,
        "Popen",
        lambda *args, **kwargs: proc,
    )

    result: dict = {}

    def runner() -> None:
        try:
            result["value"] = process_utils.run_managed_process(["tool"])
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = exc

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=5.0)
    assert "error" not in result, result.get("error")
    assert result["value"].returncode == 0


def test_sweeper_kills_a_process_started_from_a_worker_thread(tmp_path):
    """The registry is the main thread's only handle on a worker's child (#495).

    A worker thread installs no signal handler (``signal.signal`` is
    main-thread-only) and the child is in its own session, so a signal aimed
    at this process group never reaches it. Without the sweeper a cancelled
    parallel build job dies and leaves its compilers running.
    """
    import threading

    out_path = tmp_path / "out.log"
    started = threading.Event()
    result: dict = {}

    def runner() -> None:
        with open(out_path, "w") as out_fp:
            try:
                started.set()
                result["value"] = process_utils.run_managed_process(
                    ["sleep", "30"], stdout=out_fp, stderr=out_fp
                )
            except Exception as exc:  # pragma: no cover - failure path
                result["error"] = exc

    t = threading.Thread(target=runner)
    t.start()
    assert started.wait(timeout=5.0)
    # The registration happens right after Popen, which is a moment after the
    # event above; poll rather than sleep a fixed amount.
    deadline = time.monotonic() + 5.0
    while not process_utils._live_processes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process_utils._live_processes

    start = time.perf_counter()
    assert process_utils.terminate_live_managed_processes() == 1
    t.join(timeout=10.0)
    elapsed = time.perf_counter() - start

    assert not t.is_alive()
    assert "error" not in result, result.get("error")
    assert result["value"].returncode != 0  # signalled, not a clean exit
    assert elapsed < 9.0  # nowhere near the 30s sleep
    assert process_utils._live_processes == {}


def test_sweeper_is_a_no_op_for_an_already_exited_process(monkeypatch):
    """An exited process is skipped, not signalled, and never counted."""
    proc = FakeProcess()
    proc.completed = True
    signals_sent = []
    monkeypatch.setattr(
        process_utils.os, "killpg", lambda pid, sig: signals_sent.append((pid, sig))
    )
    monkeypatch.setattr(process_utils, "_live_processes", {0: proc})

    assert process_utils.terminate_live_managed_processes() == 0
    assert signals_sent == []


def test_registry_is_empty_after_a_normal_run(tmp_path):
    """Registration is scoped to the call, on every exit path."""
    out_path = tmp_path / "out.log"
    with open(out_path, "w") as out_fp:
        result = process_utils.run_managed_process(
            ["true"], stdout=out_fp, stderr=out_fp
        )
    assert result.returncode == 0
    assert process_utils._live_processes == {}


def test_timeout_pauser_true_lets_process_finish(tmp_path):
    out_path = tmp_path / "out.log"
    with open(out_path, "w") as out_fp:
        start = time.perf_counter()
        result = process_utils.run_managed_process(
            ["sleep", "2"],
            stdout=out_fp,
            stderr=out_fp,
            timeout=0.5,
            timeout_pauser=lambda: True,
        )
        elapsed = time.perf_counter() - start

    assert result.timed_out is False
    assert result.returncode == 0
    assert elapsed >= 1.5  # the sim ran to completion, not cut short at 0.5s


def test_timeout_pauser_false_times_out_quickly(tmp_path):
    out_path = tmp_path / "out.log"
    with open(out_path, "w") as out_fp:
        start = time.perf_counter()
        result = process_utils.run_managed_process(
            ["sleep", "2"],
            stdout=out_fp,
            stderr=out_fp,
            timeout=0.5,
            timeout_returncode=4444,
            timeout_pauser=lambda: False,
        )
        elapsed = time.perf_counter() - start

    assert result.timed_out is True
    assert result.returncode == 4444
    assert elapsed < 1.9  # well under the 2s sleep duration


def test_timeout_pauser_rejects_capture_output(monkeypatch):
    def _fail_popen(*args, **kwargs):
        raise AssertionError("Popen should not be called before validation")

    monkeypatch.setattr(process_utils.subprocess, "Popen", _fail_popen)

    with pytest.raises(ValueError):
        process_utils.run_managed_process(
            ["true"],
            capture_output=True,
            timeout=1,
            timeout_pauser=lambda: True,
        )


def test_timeout_pauser_rejects_pipe_stdout(monkeypatch):
    def _fail_popen(*args, **kwargs):
        raise AssertionError("Popen should not be called before validation")

    monkeypatch.setattr(process_utils.subprocess, "Popen", _fail_popen)

    with pytest.raises(ValueError):
        process_utils.run_managed_process(
            ["true"],
            stdout=subprocess.PIPE,
            timeout=1,
            timeout_pauser=lambda: True,
        )
