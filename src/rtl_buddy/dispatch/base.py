# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch backend interface (#351).

A dispatch backend runs the compile AND the SIM+POST phases of tests as
external jobs — nothing heavy runs on the submit host, which is usually an
interactive login node. The head submits one **build job** per suite that
Verilates the shared executable on a compute node, then one ``rb
_test-job`` per (test, run_id) gated on that build via a scheduler
dependency; each sim job writes a ``result.json`` the head collects.
Backends only launch and await jobs — result collection is
backend-independent (``runner.result_io``).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config.dispatch import JobResources
from ..seed_mode import SeedMode


@dataclass
class BuildJobSpec:
    """Everything a backend needs to launch one suite's build job.

    The job runs ``rb _build-job`` on a compute node — PRE+COMPILE for
    every runnable test in the suite with share-build, so each unique
    compile key Verilates once. Sim jobs depend on its success.
    """

    suite_dir: str
    test_config_path: str
    resources: JobResources = field(default_factory=JobResources)
    reg_level: int | None = None
    start_level: int | None = None
    builder_mode: str = "reg"
    builder_override: str | None = None
    log_path: Path | None = None
    # Dispatch plan manifest (absolute) written by the head after a single
    # sweep expansion; the build job compiles exactly its entries instead
    # of re-running the sweep hook. See ``config.test.TestConfig`` plan
    # (de)serialization and ``dispatch.plan``.
    plan_path: Path | None = None
    # Where the build job records its compile outcome (built/failed test
    # names); the head loads it at collect for compile-fail parity.
    result_json: Path | None = None


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
    # Dispatch plan manifest (absolute); the sim job resolves ``test_name``
    # from it instead of re-running the suite's sweep hook. See BuildJobSpec.
    plan_path: Path | None = None

    def display_name(self) -> str:
        if self.run_id is None:
            return self.test_name
        return f"{self.test_name}:{self.run_id}"


@dataclass
class JobHandle:
    """An accepted submission: the backend's job id plus its spec."""

    job_id: str
    spec: object


class DispatchBackend(ABC):
    """One remote-execution flavor (slurm today; LSF/SGE are future).

    Implementations raise ``FatalRtlBuddyError`` on submission failure
    (the head must not continue half-submitted silently) and make
    ``cancel_all`` best-effort (used on interrupt/teardown).
    """

    name: str = "?"

    # Whether jobs are handed to a batch scheduler that can kill or cancel
    # them on its own. False for a backend that runs jobs itself (the
    # local-parallel pool, #360), where a missing result cannot be explained
    # by scheduler action and diagnostics must not blame one.
    scheduled: bool = True

    @abstractmethod
    def submit_build(self, spec: BuildJobSpec) -> JobHandle:
        """Submit one suite's build job; return its handle without waiting."""

    @abstractmethod
    def submit(self, spec: TestJobSpec, *, dependency: str | None = None) -> JobHandle:
        """Submit one sim job, optionally gated on ``dependency`` (a build
        job id that must succeed first); return its handle without waiting."""

    def submit_array(
        self,
        specs: list[TestJobSpec],
        *,
        array_dir: Path,
        max_parallel: int | None = None,
        dependency: str | None = None,
    ) -> list[JobHandle]:
        """Submit a group of jobs with identical resolved resources.

        Backends with native array support (Slurm) override this to
        submit one array job; the default just loops :meth:`submit`.
        ``array_dir`` is a scratch directory on the shared filesystem
        for the array's manifest/script/logs; ``max_parallel`` caps how
        many elements run concurrently; ``dependency`` (a build-job id)
        gates every element on that job succeeding.
        """
        return [self.submit(spec, dependency=dependency) for spec in specs]

    def advance(self) -> None:
        """Let a self-executing backend make progress, without blocking.

        No-op for a scheduler-backed backend: the scheduler runs the fleet
        whether or not the head is paying attention. A backend that executes
        jobs itself only makes progress when poked, so the head calls this
        while it is doing its own work between submissions — planning the
        next suite can take real time (its sweep hook shells out), and a slot
        freed during that would otherwise sit idle (#360).
        """

    @abstractmethod
    def wait_all(self, handles: list[JobHandle]) -> None:
        """Block until every submitted job has left the queue."""

    @abstractmethod
    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        """Best-effort cancellation of all outstanding jobs.

        Tolerates ``None`` entries: it is the last line of defence against
        an orphaned fleet on a head-side failure, so a caller that let a
        ``None`` slip into the handle list (e.g. a zero-test suite's absent
        build handle, #361) must not disarm it.
        """

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Per-job reserved-vs-used accounting, keyed by job id.

        Returns an empty mapping when the backend has no accounting
        source (right-sizing then degrades gracefully). Values are
        backend-shaped dicts; the Slurm backend documents its fields.
        """
        return {}
