# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Guard for the pywellen >=0.25 random-access Waveform API.

pywellen is pre-1.0 and its minor bumps rewrite the public API: 0.25.0
replaced the 0.20-0.24 random-access surface (``get_signal_from_path`` /
``value_at_time``) with the current ``wf[path]`` / ``Signal.value_at`` one
that ``rb wave`` value annotations and ``rb saif`` now depend on (#263). The
dependency is bounded to ``>=0.25.2,<0.26`` in pyproject, but that doesn't
protect environments that force-resolved an out-of-range pywellen (e.g. a
stale tool venv pinning <0.25, or a future incompatible bump). This guard
turns that situation into a clear FatalRtlBuddyError up front instead of
blank annotations or an AttributeError traceback mid-run.

``tests/test_pywellen_api.py`` is the CI-time half of the same guard; keep
the two in sync when the depended-on surface changes.
"""

from __future__ import annotations

import logging
from importlib import metadata

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: The random-access ``Waveform`` attributes rtl_buddy's trace readers use,
#: introduced in pywellen 0.25.0 (``wf[path]`` lookup, top-scope enumeration,
#: and the timescale getter).
RANDOM_ACCESS_API = ("__getitem__", "scopes", "timescale")


def pywellen_version() -> str:
    """Return the installed pywellen distribution version, or "unknown"."""
    try:
        return metadata.version("pywellen")
    except metadata.PackageNotFoundError:
        return "unknown"


def require_random_access_api(tool: str) -> None:
    """Raise FatalRtlBuddyError unless pywellen has the random-access API.

    *tool* names the rb subcommand for the error message (e.g. "rb wave").
    """
    import pywellen  # type: ignore[import-untyped]  # noqa: PLC0415

    missing = [a for a in RANDOM_ACCESS_API if not hasattr(pywellen.Waveform, a)]
    if not missing:
        return
    version = pywellen_version()
    log_event(
        logger,
        logging.ERROR,
        "pywellen.api_missing",
        tool=tool,
        version=version,
        missing=",".join(missing),
    )
    raise FatalRtlBuddyError(
        f"pywellen {version} lacks the random-access Waveform API {tool} "
        f"requires (missing: {', '.join(missing)}; the current surface arrived "
        f"in pywellen 0.25.0) — reinstall with 'pywellen>=0.25.2,<0.26' "
        f"(rtl_buddy#263)"
    )
