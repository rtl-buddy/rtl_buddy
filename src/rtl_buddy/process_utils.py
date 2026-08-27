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


# ---- the live-process registry.
#
# Every process :func:`run_managed_process` starts is registered here for the
# duration of the call, whatever thread started it. It exists for one caller
# shape: a main thread that must take down tool processes it did not launch
# itself, because worker threads launched them and ``signal.signal`` only
# works on the main thread — so those workers installed no handler and their
# children are in their own sessions (``start_new_session``), out of reach of
# a signal aimed at this process group. The parallel build job (#495) is that
# caller: cancel it (Ctrl-C, or the local-parallel pool's ``cancel_all``) and
# without this the job dies while its Verilations keep burning the node.
#
# Keyed by a monotonic token rather than the pid, so a recycled pid or a
# repeated Popen object can never make one entry evict another's, and both
# halves are O(1) under one short-held lock.
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


# ---- the cancellation latch.
#
# The registry can only reach what is already in it, and the sweep is a
# snapshot: a process between ``Popen`` and its registration is invisible to
# it, and so is a pool worker that has already been handed its next unit of
# work and is about to spawn. Left at that, a cancelled parallel build job
# sweeps what it can see, re-raises, and the executor's
# ``shutdown(wait=True)`` then lets that worker start a *new* compiler — in
# its own session, so nothing left kills it, and the local backend's grace
# period expires on the job while that compiler runs on, orphaned (#496
# review).
#
# So cancellation is also a process-wide latch, set BEFORE the sweep takes
# its snapshot. That ordering is what closes the register-race: a process
# either registers before the snapshot (and is swept) or registers after the
# latch is visible (and terminates itself below). One-way per process —
# nothing resumes a cancelled process — so there is no reset outside tests.
_cancellation_started = threading.Event()


def cancellation_has_started() -> bool:
    """True once :func:`terminate_live_managed_processes` has been entered.

    Callers that launch work in a pool check this before starting anything
    new: a queued worker that wakes after the sweep must not spawn a tool
    process the sweep can no longer see.
    """
    return _cancellation_started.is_set()


def _reset_cancellation_latch() -> None:
    """Clear the latch. Tests only — cancellation is one-way in a real run."""
    _cancellation_started.clear()


def _cancelled_result() -> ManagedProcessResult:
    """The shape a process killed by the sweep would have reported.

    Consistent whether the child was never spawned or was terminated a
    moment after ``Popen``: the caller cannot tell the two apart and must
    not have to, because which side of the race it landed on is timing.
    """
    return ManagedProcessResult(returncode=-signal.SIGTERM)


def terminate_live_managed_processes(
    *,
    terminate_signal: int = signal.SIGTERM,
    kill_timeout: float = DEFAULT_KILL_TIMEOUT,
) -> int:
    """Stop every process :func:`run_managed_process` currently owns.

    A fleet-kill, so it is two-phase for the reason
    :func:`signal_process_group` documents: signal everything first, then
    reap on one shared deadline. Signalling and waiting per process would
    make the grace period scale with the fleet and would leave everything
    past an interruption point unsignalled — the orphans this exists to
    prevent. Tolerates a process that is already gone (the owning thread may
    be reaping it concurrently) and returns how many were signalled.

    Latches cancellation first, so nothing new can be spawned behind the
    snapshot this takes; see the latch comment above.
    """
    _cancellation_started.set()

    with _live_processes_lock:
        victims = list(_live_processes.values())

    # Phase 1 — signal every live group.
    signalled = []
    for proc in victims:
        try:
            if proc.poll() is not None:
                continue
            signal_process_group(proc, terminate_signal)
        except Exception:  # noqa: BLE001 - a fleet-kill must not stop early
            # Whatever one process does on the way out, the rest still have
            # to be signalled: this is the last line of defence against an
            # orphaned fleet.
            continue
        signalled.append(proc)

    # Phase 2 — one grace period for the whole fleet, then SIGKILL the
    # stragglers. The owning thread is inside ``communicate()`` on the same
    # Popen, which is why every wait here is best-effort: whichever of the
    # two reaps it first, the other simply reads the code back.
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

    # Checked after the argument validation above (a programming error is
    # still a programming error while a process is being cancelled) and
    # before the spawn: once the sweep has run, a process started here is one
    # nothing can reach — the sweep's snapshot is already taken and, from a
    # worker thread, no handler of our own will ever be installed.
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
    # Registered before anything else can fail, and released in the
    # ``finally`` below whatever happens — including the worker-thread case,
    # where the registry is the *only* way a cancelling main thread can
    # reach this process (see the registry comment above).
    live_token = _register_live_process(proc)

    # The other half of the register-race. The check above can pass a moment
    # before a concurrent sweep sets the latch, and that sweep's snapshot can
    # be taken a moment before the registration above — a window in which the
    # child is live and in nobody's hands. Re-reading the latch after
    # registering makes the two orders exhaustive: register-then-sweep is
    # swept, sweep-then-register is terminated right here.
    if _cancellation_started.is_set():
        _unregister_live_process(live_token)
        terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        # Drains and closes the pipes of the process just reaped (the
        # ``_timeout_result`` precedent); the bytes are dropped because the
        # result is defined by the cancellation, not by whatever the child
        # managed to emit in its first milliseconds.
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
        # Unregistered first: this process is about to be terminated by its
        # owner, so a concurrent sweeper has nothing left to do with it, and
        # deregistering ahead of the termination keeps a hung terminate from
        # leaving a dead entry in the registry.
        _unregister_live_process(live_token)
        terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        if in_main_thread:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
