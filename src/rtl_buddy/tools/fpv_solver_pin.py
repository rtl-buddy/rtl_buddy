"""Probe solver binaries and enforce ``cfg-fpv-tools.opts.solver-versions`` pins for ``rb fpv``."""

from __future__ import annotations

import logging
import re
import subprocess

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# solver -> (binary, args, regex capturing the version from stdout+stderr)
_PROBES: dict[str, tuple[str, list[str], str]] = {
    "yices": ("yices-smt2", ["--version"], r"Yices\s+(\S+)"),
    "z3": ("z3", ["--version"], r"Z3 version\s+(\S+)"),
    "boolector": ("boolector", ["--version"], r"^(\d+\.\d+\.\d+)"),
    "bitwuzla": ("bitwuzla", ["--version"], r"^(\d+\.\d+\.\d+)"),
    "btormc": ("btormc", ["--version"], r"^(\d+\.\d+\.\d+)"),
    "abc": ("yosys-abc", ["-h"], r"UC Berkeley, ABC\s+(\S+)"),
}


def probe_solver_version(solver: str) -> str | None:
    """Return the installed version of ``solver`` (a ``solver-versions`` key), or ``None``.

    ``None`` means the solver is unknown, the binary is missing, the probe timed out
    or the version output did not match.
    """
    if solver not in _PROBES:
        log_event(
            logger,
            logging.WARNING,
            "fpv.solver_probe_unknown",
            solver=solver,
        )
        return None
    binary, args, pattern = _PROBES[solver]
    try:
        res = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log_event(
            logger,
            logging.WARNING,
            "fpv.solver_probe_failed",
            solver=solver,
            binary=binary,
            error=str(e),
        )
        return None
    out = (res.stdout or "") + (res.stderr or "")
    m = re.search(pattern, out, re.MULTILINE)
    return m.group(1) if m else None


def check_solver_pins(pins: dict[str, str]) -> dict[str, str]:
    """Return the resolved version of every pinned solver.

    Raises:
      FatalRtlBuddyError: one error listing every mismatched or unprobeable solver.
    """
    resolved: dict[str, str] = {}
    failures: list[str] = []
    for solver, expected in pins.items():
        got = probe_solver_version(solver)
        if got is None:
            failures.append(
                f"  {solver}: pinned {expected!r}, but probe failed "
                f"(binary missing or unrecognized output)"
            )
            continue
        if got != expected:
            failures.append(f"  {solver}: pinned {expected!r}, found {got!r}")
            continue
        resolved[solver] = got
    if failures:
        raise FatalRtlBuddyError(
            "FPV solver version pin check failed:\n" + "\n".join(failures)
        )
    return resolved
