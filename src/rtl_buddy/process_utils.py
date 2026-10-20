import itertools
import os
import signal
import subprocess
import threading
import time
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

    A caller holding several processes signals all of them before waiting on
    any. Falls back to signalling the single process when the group cannot be
    signalled; does nothing for a process that is already gone.
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
    """Stop ``proc`` and its group: graceful signal, then SIGKILL after ``kill_timeout``.

    Handles one process; for several, use :func:`signal_process_group`.
    """
    if proc.poll() is not None:
        return

    try:
        signal_process_group(proc, terminate_signal)
        proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired:
        signal_process_group(proc, signal.SIGKILL)
        proc.wait()


# Live-process registry: every process `run_managed_process` starts, from any
# thread, for the duration of the call. Lets the main thread terminate tool
# processes that worker threads launched in their own sessions. Keyed by a
# monotonic token, not the pid, so a recycled pid cannot evict another entry.
_live_processes: dict[int, subprocess.Popen] = {}
_live_processes_lock = threading.Lock()
_live_process_tokens = itertools.count()


def _register_live_process(proc: subprocess.Popen) -> int:
    token = next(_live_process_tokens)
    with _live_processes_lock:
        _live_processes[token] = proc
    return token


def _unregister_live_process(token: int) -> None:
    with _live_processes_lock:
        _live_processes.pop(token, None)


# Cancellation latch, set BEFORE the sweep snapshots the registry. A process
# registering before the snapshot is swept; one registering after sees the
# latch and terminates itself. Without it a queued pool worker could start a
# new tool process the sweep cannot see. One-way; only tests reset it.
_cancellation_started = threading.Event()


def cancellation_has_started() -> bool:
    """True once :func:`terminate_live_managed_processes` has been entered.

    Pool workers check this before starting new work.
    """
    return _cancellation_started.is_set()


def _reset_cancellation_latch() -> None:
    """Clear the latch. Tests only."""
    _cancellation_started.clear()


def _cancelled_result() -> ManagedProcessResult:
    """The result of a cancelled process, the same whether it was never spawned or was killed just after ``Popen``."""
    return ManagedProcessResult(returncode=-signal.SIGTERM)


def terminate_live_managed_processes(
    *,
    terminate_signal: int = signal.SIGTERM,
    kill_timeout: float = DEFAULT_KILL_TIMEOUT,
) -> int:
    """Stop every process :func:`run_managed_process` currently owns.

    Sets the cancellation latch, signals every live group, then reaps on one
    shared ``kill_timeout`` deadline and SIGKILLs stragglers. Tolerates
    processes that are already gone. Returns how many were signalled.
    """
    _cancellation_started.set()

    with _live_processes_lock:
        victims = list(_live_processes.values())

    # Phase 1: signal every live group.
    signalled = []
    for proc in victims:
        try:
            if proc.poll() is not None:
                continue
            signal_process_group(proc, terminate_signal)
        except Exception:  # noqa: BLE001 - a fleet-kill must not stop early
            # The remaining processes must still be signalled.
            continue
        signalled.append(proc)

    # Phase 2: one grace period for the fleet, then SIGKILL. The owning thread
    # may reap the same Popen concurrently, so every wait is best-effort.
    deadline = time.monotonic() + kill_timeout
    for proc in signalled:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            signal_process_group(proc, signal.SIGKILL)
            try:
                proc.wait()
            except Exception:  # noqa: BLE001 - reaped elsewhere; nothing to do
                pass
        except Exception:  # noqa: BLE001 - see above
            pass
    return len(signalled)


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

    ``terminate_signal`` is the graceful-stop signal; simulators may need
    SIGQUIT to flush waveforms.

    ``timeout_pauser`` (only with ``timeout``) is polled every tick; while it
    returns ``True`` the tick does not count against ``timeout``, for expected
    waits such as a VCS license queue. It cannot be combined with
    ``capture_output=True`` or a ``subprocess.PIPE`` stream.
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

    # After argument validation, before the spawn: a process started after the
    # sweep is unreachable.
    if _cancellation_started.is_set():
        return _cancelled_result()

    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        text=text,
        cwd=cwd,
        env=env,
        start_new_session=(os.name != "nt"),
    )
    # Released in the ``finally`` below.
    live_token = _register_live_process(proc)

    # Re-check after registering: a sweep can set the latch and snapshot the
    # registry between the first check and the registration.
    if _cancellation_started.is_set():
        _unregister_live_process(live_token)
        terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        # Closes the pipes; the output is dropped.
        proc.communicate()
        return _cancelled_result()

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

    # ``signal.signal`` works only in the main thread; a worker thread skips
    # the handler and relies on the ``finally`` block and the registry sweep.
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
        # Unregister before terminating, so a hung terminate leaves no stale entry.
        _unregister_live_process(live_token)
        terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        if in_main_thread:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
