"""In-process entry-point invocation with separate output streams."""

import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from rtl_buddy.rtl_buddy import RtlBuddy


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        pytest.MonkeyPatch.context() as patch,
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        patch.setattr(sys, "argv", ["rb", *args])
        if cwd is not None:
            patch.chdir(cwd)
        rb = RtlBuddy(name="test_cli")
        try:
            code = rb.run()
        finally:
            rb._artifact_locks.release_all()
    return subprocess.CompletedProcess(args, code, stdout.getvalue(), stderr.getvalue())
