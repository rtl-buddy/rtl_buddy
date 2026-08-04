import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import IO, Callable


@dataclass
class ManagedProcessResult:
    returncode: int
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    timed_out: bool = False


DEFAULT_KILL_TIMEOUT = 5


def signal_process_group(proc: subprocess.Popen, sig: int) -> None:
    """Send ``sig`` to ``proc``'s whole group without waiting for it.

    Signalling and reaping are separable because a caller holding *several*
    processes must signal them all before waiting on any: waiting in the
    same loop makes the total grace period scale with the fleet size, and
    leaves anything not yet reached unsignalled if the wait is interrupted
    (the local-parallel dispatch pool, #360). Falls back to signalling the
    single process when the group cannot be signalled, and is a no-op for a
    process that is already gone.
    """
    if os.name == "nt":
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
        return
    try:
        os.killpg(proc.pid, sig)
    except PermissionError:
        proc.send_signal(sig)
    except ProcessLookupError:
        return


def terminate_process_group(
    proc: subprocess.Popen,
    *,
    terminate_signal: int = signal.SIGTERM,
    kill_timeout: float = DEFAULT_KILL_TIMEOUT,
) -> None:
    """Stop ``proc`` and its group: graceful signal, then SIGKILL.

    Owns one process for the duration of the call, so it is right for
    :func:`run_managed_process` and wrong for a caller holding a fleet — see
    :func:`signal_process_group`.
    """
    if proc.poll() is not None:
        return

    try:
        signal_process_group(proc, terminate_signal)
        proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired:
        signal_process_group(proc, signal.SIGKILL)
        proc.wait()


_TIMEOUT_PAUSER_POLL_SEC = 0.5


def _timeout_result(
    proc: subprocess.Popen,
    *,
    timeout_returncode: int | None,
    terminate_signal: int,
    kill_timeout: float,
) -> ManagedProcessResult:
    terminate_process_group(
        proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
    )
    stdout_data, stderr_data = proc.communicate()
    return ManagedProcessResult(
        returncode=(
            timeout_returncode if timeout_returncode is not None else proc.returncode
        ),
        stdout=stdout_data,
        stderr=stderr_data,
        timed_out=True,
    )


def run_managed_process(
    cmd: list[str],
    *,
    stdout: int | IO | None = None,
    stderr: int | IO | None = None,
    capture_output: bool = False,
    text: bool = False,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    timeout_returncode: int | None = None,
    terminate_signal: int = signal.SIGTERM,
    kill_timeout: float = 5,
    timeout_pauser: Callable[[], bool] | None = None,
) -> ManagedProcessResult:
    """Run a long-lived tool process with consistent cleanup.

    Simulators may need a non-default graceful-stop signal, such as SIGQUIT, to
    flush waveform data before exit.

    ``timeout_pauser``, when given, is polled once per tick while waiting for
    the process (only meaningful when ``timeout`` is also set). While it
    returns ``True`` the elapsed tick does not count against ``timeout`` —
    this lets callers pause the timeout clock during expected, non-hanging
    waits (e.g. a VCS license queue). Because nothing drains the child's
    stdout/stderr pipes during the poll loop, ``timeout_pauser`` cannot be
    combined with ``capture_output=True`` or a ``subprocess.PIPE`` stream.
    """
    if capture_output:
        stdout = subprocess.PIPE
        stderr = subprocess.PIPE

    if timeout_pauser is not None and (
        stdout == subprocess.PIPE or stderr == subprocess.PIPE
    ):
        raise ValueError(
            "timeout_pauser cannot be combined with pipe-captured output "
            "(capture_output=True or subprocess.PIPE); nothing drains the "
            "pipes during the poll loop"
        )

    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        text=text,
        cwd=cwd,
        env=env,
        start_new_session=(os.name != "nt"),
    )

    previous_handlers = {}

    def _signal_handler(signum, frame):
        terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        previous = previous_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    # ``signal.signal`` only works in the main thread. When this
    # helper is called from a worker thread (e.g. ``asyncio.to_thread``
    # from the hub's HTTP handler in ``rb hub --model`` switching),
    # we skip the signal-handler install. The main thread's existing
    # SIGINT/SIGTERM handler still fires and the ``finally`` block
    # below still terminates the process group on its way out, so the
    # only thing lost is the in-worker re-raise of KeyboardInterrupt /
    # SystemExit — which the worker thread can't propagate to the
    # main thread anyway.
    in_main_thread = threading.current_thread() is threading.main_thread()
    if in_main_thread:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _signal_handler)

    try:
        if timeout is not None and timeout_pauser is not None:
            elapsed = 0.0
            while True:
                try:
                    proc.wait(timeout=_TIMEOUT_PAUSER_POLL_SEC)
                except subprocess.TimeoutExpired:
                    if not timeout_pauser():
                        elapsed += _TIMEOUT_PAUSER_POLL_SEC
                    if elapsed > timeout:
                        return _timeout_result(
                            proc,
                            timeout_returncode=timeout_returncode,
                            terminate_signal=terminate_signal,
                            kill_timeout=kill_timeout,
                        )
                else:
                    stdout_data, stderr_data = proc.communicate()
                    return ManagedProcessResult(
                        returncode=proc.returncode,
                        stdout=stdout_data,
                        stderr=stderr_data,
                    )
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            return ManagedProcessResult(
                returncode=proc.returncode, stdout=stdout_data, stderr=stderr_data
            )
        except subprocess.TimeoutExpired:
            return _timeout_result(
                proc,
                timeout_returncode=timeout_returncode,
                terminate_signal=terminate_signal,
                kill_timeout=kill_timeout,
            )
    finally:
        terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        if in_main_thread:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
