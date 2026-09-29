# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Fail early with a clear error when the installed pywellen lacks the random-access Waveform API.

``tests/test_pywellen_api.py`` checks the same surface and the pyproject pin against
:data:`SUPPORTED_SPECIFIER`; keep the three in step.
"""

from __future__ import annotations

import logging
from importlib import metadata

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: Supported pywellen range, mirroring the pyproject pin.
MIN_VERSION = "0.25.6"
MAX_VERSION_EXCLUSIVE = "0.26"
SUPPORTED_SPECIFIER = f">={MIN_VERSION},<{MAX_VERSION_EXCLUSIVE}"

#: The pywellen surface the trace readers call, by class name.
REQUIRED_API: dict[str, tuple[str, ...]] = {
    "Waveform": ("__getitem__", "scopes", "timescale"),
    "Var": ("signal", "name", "full_name", "var_type", "bitwidth"),
    "Signal": ("value_at", "__getitem__", "__len__"),
}


def pywellen_version() -> str:
    """Return the installed pywellen distribution version, or "unknown"."""
    try:
        return metadata.version("pywellen")
    except metadata.PackageNotFoundError:
        return "unknown"


def missing_api(module) -> list[str]:
    """Return the ``Class.attr`` names of :data:`REQUIRED_API` that *module* lacks.

    A class that is missing entirely is reported as ``Class``.
    """
    missing: list[str] = []
    for cls_name, attrs in REQUIRED_API.items():
        cls = getattr(module, cls_name, None)
        if cls is None:
            missing.append(cls_name)
            continue
        missing += [f"{cls_name}.{a}" for a in attrs if not hasattr(cls, a)]
    return missing


def require_random_access_api(tool: str) -> None:
    """Raise FatalRtlBuddyError unless pywellen has the random-access API.

    *tool* names the rb subcommand in the error message.
    """
    import pywellen  # type: ignore[import-untyped]  # noqa: PLC0415

    missing = missing_api(pywellen)
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
        supported=SUPPORTED_SPECIFIER,
    )
    raise FatalRtlBuddyError(
        f"pywellen {version} lacks the random-access Waveform API {tool} "
        f"requires (missing: {', '.join(missing)}; the current surface arrived "
        f"in pywellen 0.25.0) — reinstall with 'pywellen{SUPPORTED_SPECIFIER}' "
        f"(rtl_buddy#263)"
    )
