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
dependency is bounded to :data:`SUPPORTED_SPECIFIER` in pyproject, but that
doesn't protect environments that force-resolved an out-of-range pywellen
(e.g. a stale tool venv pinning <0.25, or a future incompatible bump). This
guard turns that situation into a clear FatalRtlBuddyError up front instead
of blank annotations or an AttributeError traceback mid-run.

``tests/test_pywellen_api.py`` is the CI-time half of the same guard: it
asserts this surface against a real trace and that the pyproject pin still
matches :data:`SUPPORTED_SPECIFIER`. Keep the three in step.
"""

from __future__ import annotations

import logging
from importlib import metadata

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: Supported pywellen range, mirroring the pyproject pin. The floor is the
#: release carrying the upstream streaming fix plus its follow-up patches;
#: the cap stops the next pre-1.0 rewrite reaching the field (#263).
MIN_VERSION = "0.25.6"
MAX_VERSION_EXCLUSIVE = "0.26"
SUPPORTED_SPECIFIER = f">={MIN_VERSION},<{MAX_VERSION_EXCLUSIVE}"

#: The pywellen surface rtl_buddy's trace readers call, by class name:
#: ``wf[path]`` lookup + top-scope enumeration + timescale on Waveform, the
#: zero-arg getters on Var (they took a hierarchy argument before 0.25), and
#: the point query plus change-vector slicing on Signal.
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
    """Return the ``Class.attr`` names of :data:`REQUIRED_API` *module* lacks.

    A class missing outright is reported as ``Class`` — that is the shape a
    wholesale rewrite takes, and naming the attributes of a type that isn't
    there would be noise.
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

    *tool* names the rb subcommand for the error message (e.g. "rb wave").
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
