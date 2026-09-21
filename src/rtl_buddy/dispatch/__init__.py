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


def create_dispatch_backend(
    name, dispatch_cfg, *, config_path=None
) -> DispatchBackend | None:
    """Instantiate the named backend; ``None``/``local`` → in-process.

    ``config_path`` is the root_config.yaml ``dispatch_cfg`` came from. It is
    recorded on the backend beside the arguments it keeps, so advice about an
    `sbatch-args` override can name the file that actually holds it — the
    orchestration config, which in a multi-root regression is not the root a
    later suite resolves (#527).
    """
    validate_backend_name(name)
    if name is None or name == "local":
        return None
    backend = _BACKENDS[name](dispatch_cfg)
    backend.effective_sbatch_args_path = (
        None if config_path is None else str(config_path)
    )
    return backend
