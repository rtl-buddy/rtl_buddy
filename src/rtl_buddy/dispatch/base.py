# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch backend interface (#351).

A dispatch backend runs the SIM+POST phase of tests as external jobs
after the head process has built the sim executable. The unit of
dispatch is one (test, run_id): the backend submits ``rb _test-job``
invocations that each write a ``result.json`` envelope, and the head
process collects those envelopes into the normal aggregation path.
Backends only launch and await jobs — result collection is
backend-independent (``runner.result_io``).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..config.dispatch import JobResources
from ..seed_mode import SeedMode


@dataclass
class TestJobSpec:
    """Everything a backend needs to launch one (test, run_id) job.

    ``test_name`` is the sweep-expanded name — the one ``rb _test-job``
    resolves. ``result_json`` is absolute; the job writes its envelope
    there and the head collects it, so it must live on storage shared
    between them.
    """

    test_name: str
    suite_dir: str
    test_config_path: str
    result_json: Path
    resources: JobResources = field(default_factory=JobResources)
    run_id: int | None = None
    seed_mode: SeedMode = SeedMode.DEFAULT
    replay_run_id: int | None = None
    builder_mode: str = "reg"
    builder_override: str | None = None
    share_build: bool = True
    log_path: Path | None = None

    def display_name(self) -> str:
        if self.run_id is None:
            return self.test_name
        return f"{self.test_name}:{self.run_id}"


@dataclass
class JobHandle:
    """An accepted submission: the backend's job id plus its spec."""

    job_id: str
    spec: TestJobSpec


class DispatchBackend(ABC):
    """One remote-execution flavor (slurm today; LSF/SGE are future).

    Implementations raise ``FatalRtlBuddyError`` on submission failure
    (the head must not continue half-submitted silently) and make
    ``cancel_all`` best-effort (used on interrupt/teardown).
    """

    name: str = "?"

    @abstractmethod
    def submit(self, spec: TestJobSpec) -> JobHandle:
        """Submit one job; return its handle without waiting."""

    @abstractmethod
    def wait_all(self, handles: list[JobHandle]) -> None:
        """Block until every submitted job has left the queue."""

    @abstractmethod
    def cancel_all(self, handles: list[JobHandle]) -> None:
        """Best-effort cancellation of all outstanding jobs."""
