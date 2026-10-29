"""Run Tcl that is free of OpenROAD commands, for the tests of the flow's Tcl helpers.

The script runs in the interpreter CPython's `tkinter` embeds, or in `tclsh`, in a subprocess: importing `_tkinter` starts a Tcl notifier thread that never exits and can wedge a later fork+exec on macOS.
"""

import shutil
import subprocess
import sys

import pytest

# Runs the script in the interpreter `tkinter` embeds, in a child process, and writes
# the answer on stdout.
# A CPython without a working `_tkinter` fails here and `tclsh` is used instead.
_EMBEDDED_DRIVER = """
import sys
try:
    import tkinter
    interp = tkinter.Tcl()
except Exception:
    raise SystemExit(9)  # no usable embedded Tcl; the caller tries tclsh
sys.stdout.write(str(interp.eval(sys.stdin.read())))
"""


def _tcl_eval(script: str) -> str:
    """Run `script`, which must leave its answer in `result`, and return it.

    Prefers the `tkinter` interpreter, falls back to `tclsh`, and skips when neither is
    available. Both run out of process.
    """
    embedded = subprocess.run(
        [sys.executable, "-c", _EMBEDDED_DRIVER],
        input=script + "\nset result\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if embedded.returncode == 0:
        return embedded.stdout
    if embedded.returncode != 9:  # pragma: no cover - a test bug, not a skip
        raise AssertionError(embedded.stderr.strip())
    tclsh = shutil.which("tclsh")
    if tclsh is None:  # pragma: no cover - depends on the machine
        pytest.skip("no Tcl interpreter (tkinter or tclsh) available")
    proc = subprocess.run(
        [tclsh],
        input=script + "\nputs -nonewline $result\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:  # pragma: no cover - a test bug, not a skip
        raise AssertionError(proc.stderr.strip())
    return proc.stdout
