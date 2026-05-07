import signal
import subprocess

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
