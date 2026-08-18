# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch backend registry (#351, #360).

``local`` is not a backend class: it means "no dispatch" and keeps the
in-process execution path byte-identical to pre-dispatch behavior.
``local-parallel`` and ``slurm`` are both real backends behind the same
ABC — one host's process pool, or a cluster.
"""

from ..errors import FatalRtlBuddyError
from .base import BuildJobSpec, DispatchBackend, JobHandle, TestJobSpec
from .local_parallel import LocalProcessBackend
from .slurm import SlurmDispatchBackend

__all__ = [
    "BuildJobSpec",
    "DispatchBackend",
    "JobHandle",
    "TestJobSpec",
    "LocalProcessBackend",
    "SlurmDispatchBackend",
    "create_dispatch_backend",
    "validate_backend_name",
]

_BACKENDS: dict[str, type[DispatchBackend]] = {
    LocalProcessBackend.name: LocalProcessBackend,
    "slurm": SlurmDispatchBackend,
}


def validate_backend_name(name) -> None:
    """Reject a ``--dispatch`` value no backend answers to.

    Split out of :func:`create_dispatch_backend` so a command can reject
    a typo *before* another message quotes the name back and implies it
    exists (``rb test --list --dispatch slrum``) (#440 review).
    """
    if name is None or name == "local" or name in _BACKENDS:
        return
    known = ", ".join(["local", *sorted(_BACKENDS)])
    raise FatalRtlBuddyError(
        f"unknown dispatch backend {name!r}; choose from [{known}]"
    )


def create_dispatch_backend(name, dispatch_cfg) -> DispatchBackend | None:
    """Instantiate the named backend; ``None``/``local`` → in-process."""
    validate_backend_name(name)
    if name is None or name == "local":
        return None
    return _BACKENDS[name](dispatch_cfg)
