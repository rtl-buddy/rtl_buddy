"""Solver version probing + pin enforcement for ``rb fpv``.

When a user pins solver versions via ``cfg-fpv-tools.opts.solver-versions``
(e.g. ``{yices: "2.6.4", z3: "4.13.0"}``), this module probes each
binary on PATH and raises :class:`FatalRtlBuddyError` if the resolved
version does not match the pin exactly.

The motivation is reproducible CI: ``sby`` happily picks whatever
solver binary it finds locally, and a runner with a different version
can silently change proof outcomes (passes at depth N on one machine,
times out on another). Pinning + hard-fail surfaces the drift instead
of letting it ride.
"""

from __future__ import annotations

import logging
import re
import subprocess

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# How to probe each known solver. Each entry: (binary name, args,
# regex that captures the version string from stdout+stderr). Add
# entries here as new engines are exercised.
_PROBES: dict[str, tuple[str, list[str], str]] = {
    "yices": ("yices-smt2", ["--version"], r"Yices\s+(\S+)"),
    "z3": ("z3", ["--version"], r"Z3 version\s+(\S+)"),
    "boolector": ("boolector", ["--version"], r"^(\d+\.\d+\.\d+)"),
    "bitwuzla": ("bitwuzla", ["--version"], r"^(\d+\.\d+\.\d+)"),
    "btormc": ("btormc", ["--version"], r"^(\d+\.\d+\.\d+)"),
    "abc": ("yosys-abc", ["-h"], r"UC Berkeley, ABC\s+(\S+)"),
}


def probe_solver_version(solver: str) -> str | None:
    """Return the installed version of ``solver`` or ``None`` if absent.

    ``solver`` is the short name used in the ``solver-versions`` pin
    map (yices / z3 / boolector / bitwuzla / btormc / abc). Returns
    ``None`` when the binary is missing, the probe times out, or the
    version regex does not match — the caller treats all three as a
    pin-check failure.
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
    """Probe every pinned solver and raise on mismatch.

    Returns a map of resolved versions on success (useful for logging
    into the run artefacts). Raises :class:`FatalRtlBuddyError` with a
    single error listing every mismatch / missing solver — users want
    one report, not N failures across reruns.
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
