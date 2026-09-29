# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch backend registry.

``local`` is not a backend: it means no dispatch and runs in-process.
``local-parallel`` and ``slurm`` are backends behind the same ABC.
"""

from ..errors import FatalRtlBuddyError
from .base import (
    BuildJobSpec,
    DispatchBackend,
    ElabJobSpec,
    JobHandle,
    TestJobSpec,
    split_handle_key,
    telemetry_key,
)
from .local_parallel import LocalProcessBackend
from .slurm import SlurmDispatchBackend

__all__ = [
    "BuildJobSpec",
    "DispatchBackend",
    "ElabJobSpec",
    "JobHandle",
    "TestJobSpec",
    "LocalProcessBackend",
    "SlurmDispatchBackend",
    "create_dispatch_backend",
    "split_handle_key",
    "telemetry_key",
    "validate_backend_name",
]

_BACKENDS: dict[str, type[DispatchBackend]] = {
    LocalProcessBackend.name: LocalProcessBackend,
    "slurm": SlurmDispatchBackend,
}


def validate_backend_name(name) -> None:
    """Raise ``FatalRtlBuddyError`` for a ``--dispatch`` value no backend answers to."""
    if name is None or name == "local" or name in _BACKENDS:
        return
    known = ", ".join(["local", *sorted(_BACKENDS)])
    raise FatalRtlBuddyError(
        f"unknown dispatch backend {name!r}; choose from [{known}]"
    )


def create_dispatch_backend(
    name, dispatch_cfg, *, config_path=None
) -> DispatchBackend | None:
    """Instantiate the named backend; ``None`` or ``local`` returns ``None`` (in-process).

    ``config_path`` is the root_config.yaml that ``dispatch_cfg`` came from. It
    is recorded on the backend so `sbatch-args` advice can name that file.
    """
    validate_backend_name(name)
    if name is None or name == "local":
        return None
    backend = _BACKENDS[name](dispatch_cfg)
    backend.effective_sbatch_args_path = (
        None if config_path is None else str(config_path)
    )
    return backend
