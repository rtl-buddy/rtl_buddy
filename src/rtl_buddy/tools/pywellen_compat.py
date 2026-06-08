# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Guard for pywellen's random-access Waveform API (removed in 0.25).

pywellen 0.25.0 rewrote ``Waveform`` to a streaming-only surface, removing
the random-access API that ``rb wave`` value annotations and ``rb saif``
depend on (#263). The dependency is bounded to ``<0.25`` in pyproject, but
that doesn't protect environments that force-resolved a newer pywellen
(e.g. a stale tool venv). This guard turns that situation into a clear
FatalRtlBuddyError up front instead of blank annotations or an
AttributeError traceback mid-run.

Remove together with the ``<0.25`` bound when the readers are ported to
the streaming API.
"""

from __future__ import annotations

import logging
from importlib import metadata

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: The random-access ``Waveform`` attributes rtl_buddy's trace readers use,
#: present through pywellen 0.24.2 and removed in 0.25.0.
RANDOM_ACCESS_API = ("hierarchy", "get_signal", "get_signal_from_path")


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
        f"requires (missing: {', '.join(missing)}; removed in pywellen 0.25) — "
        f"reinstall with 'pywellen>=0.20.0,<0.25' (rtl_buddy#263)"
    )
