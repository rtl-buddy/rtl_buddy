import os
import signal
import subprocess
from dataclasses import dataclass
from typing import IO


@dataclass
class ManagedProcessResult:
    returncode: int
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    timed_out: bool = False


def _terminate_process_group(
    proc: subprocess.Popen,
    *,
    terminate_signal: int,
    kill_timeout: float,
) -> None:
    if proc.poll() is not None:
        return

    def _send_signal(sig: int) -> None:
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

    try:
        _send_signal(terminate_signal)
        proc.wait(timeout=kill_timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        _send_signal(signal.SIGKILL)
        proc.wait()


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
) -> ManagedProcessResult:
    """Run a long-lived tool process with consistent cleanup.

    Simulators may need a non-default graceful-stop signal, such as SIGQUIT, to
    flush waveform data before exit.
    """
    if capture_output:
        stdout = subprocess.PIPE
        stderr = subprocess.PIPE

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
        _terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        previous = previous_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _signal_handler)

    try:
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            return ManagedProcessResult(
                returncode=proc.returncode, stdout=stdout_data, stderr=stderr_data
            )
        except subprocess.TimeoutExpired:
            _terminate_process_group(
                proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
            )
            stdout_data, stderr_data = proc.communicate()
            return ManagedProcessResult(
                returncode=(
                    timeout_returncode
                    if timeout_returncode is not None
                    else proc.returncode
                ),
                stdout=stdout_data,
                stderr=stderr_data,
                timed_out=True,
            )
    finally:
        _terminate_process_group(
            proc, terminate_signal=terminate_signal, kill_timeout=kill_timeout
        )
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
