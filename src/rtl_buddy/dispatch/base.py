# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch backend interface.

A backend launches build jobs (``rb _build-job``) and sim jobs (``rb _test-job``)
outside the submit host and waits for them. Result collection is
backend-independent (``runner.result_io``).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config.dispatch import JobResources
from ..seed_mode import SeedMode


# Which part of the compile a build job runs: the whole of it, or one half of a
# split that lets verilation and make reserve different resources.
BUILD_PHASE_FULL = "full"
BUILD_PHASE_VERILATE = "verilate"
BUILD_PHASE_BUILD = "build"
BUILD_PHASES = (BUILD_PHASE_FULL, BUILD_PHASE_VERILATE, BUILD_PHASE_BUILD)


@dataclass
class BuildJobSpec:
    """Everything a backend needs to launch one suite's build job.

    ``phase`` is one of :data:`BUILD_PHASES`; a split suite submits one spec
    per phase, chained by ``afterok``.
    """

    suite_dir: str
    test_config_path: str
    resources: JobResources = field(default_factory=JobResources)
    reg_level: int | None = None
    start_level: int | None = None
    builder_mode: str = "reg"
    builder_override: str | None = None
    extra_sim_timeout: int | None = None
    log_path: Path | None = None
    # Absolute path of the dispatch plan (see ``dispatch.plan``); the job compiles its entries.
    plan_path: Path | None = None
    # Where the job records its compile outcome for the head to load at collect.
    result_json: Path | None = None
    # Distinct builds compiled concurrently. Already capped by the plan's config count.
    parallel: int = 1
    # The configured value before that cap; None means equal to ``parallel``.
    # Lets the job's console line name the number the config file holds.
    parallel_configured: int | None = None
    # Compile even where a stamp validates. Carried by the build job, the single
    # writer of the shared directory.
    rebuild: bool = False
    # Manifest of which sim job holds which plan index (``dispatch.gates``), so the
    # build job can release a compile key's sims early. Set only for backends that
    # can release a pending job (Slurm).
    gates_json: Path | None = None
    # The shared-build cache root the head resolved. Tri-state: a path enables the
    # cache, "" means the head was told not to cache, None leaves the job to resolve it.
    shared_build_root: str | None = None
    # One of :data:`BUILD_PHASES`; ``full`` sends no ``--phase`` flag.
    phase: str = BUILD_PHASE_FULL
    # The head's `--run-tag` artefact namespace, or None for the flat tree.
    run_tag: str | None = None


@dataclass
class TestJobSpec:
    """Everything a backend needs to launch one (test, run_id) job.

    ``test_name`` is the sweep-expanded name. ``result_json`` is absolute and
    must be on storage shared with the head.
    """

    test_name: str
    suite_dir: str
    test_config_path: str
    result_json: Path
    resources: JobResources = field(default_factory=JobResources)
    run_id: int | None = None
    seed_mode: SeedMode = SeedMode.DEFAULT
    replay_run_id: int | None = None
    master_seed: int | None = None
    resolved_seed: int | None = None
    builder_mode: str = "reg"
    builder_override: str | None = None
    extra_sim_timeout: int | None = None
    share_build: bool = True
    # True when the head gated this job on a build job.
    expect_prebuilt: bool = False
    # Set only when the suite submitted no build job. A gated job must not carry
    # it: every array element would rebuild in one directory.
    rebuild: bool = False
    # The gating build job's result envelope. A compile that failed for this test is
    # not retried under the sim's reservation; an absent or stale stamp is.
    build_result_json: Path | None = None
    # The cache root the build job was given, tri-state as on :class:`BuildJobSpec`.
    shared_build_root: str | None = None
    log_path: Path | None = None
    # Absolute path of the dispatch plan the job resolves ``test_name`` from.
    plan_path: Path | None = None
    # The head's one-off `--plusarg` overrides, already merged into the plan. Forwarded
    # for the fallback expansion and so the job records them in its result envelope.
    plusarg_overrides: dict = field(default_factory=dict)
    # The head's `--run-tag` artefact namespace, or None for the flat tree.
    run_tag: str | None = None

    def display_name(self) -> str:
        if self.run_id is None:
            return self.test_name
        return f"{self.test_name}:{self.run_id}"


@dataclass
class ElabJobSpec:
    """Everything a backend needs to launch one model elaboration."""

    model_name: str
    profile_name: str | None
    suite_dir: str
    model_config_path: str
    result_json: Path
    resources: JobResources = field(default_factory=JobResources)
    log_path: Path | None = None
    builder_mode: str | None = None
    builder_override: str | None = None
    extra_sim_timeout: int | None = None

    def display_name(self) -> str:
        if self.profile_name is None:
            return self.model_name
        return f"{self.model_name}:{self.profile_name}"


RunnableJobSpec = TestJobSpec | ElabJobSpec


@dataclass
class CoverageJobSpec:
    """The coverage tail of one invocation, run as one job (``rb _cov-job``).

    ``suite_dir`` is the head's command root: the job's working directory, where
    ``root_config.yaml`` is found from, and the parent of ``cov_dir/``. ``spec_json``
    and ``result_json`` are absolute, on storage shared with the head
    (:mod:`rtl_buddy.dispatch.coverage_tail`).
    """

    suite_dir: str
    spec_json: Path
    result_json: Path
    resources: JobResources = field(default_factory=JobResources)
    log_path: Path | None = None
    builder_mode: str | None = None
    builder_override: str | None = None
    extra_sim_timeout: int | None = None
    # The head's `--run-tag` artefact namespace, or None for the flat tree.
    run_tag: str | None = None

    def display_name(self) -> str:
        return "coverage"


@dataclass
class JobHandle:
    """An accepted submission: the backend's job id plus its spec.

    ``cluster`` is the Slurm cluster that accepted the job; job ids are unique
    only within a cluster, so later commands (cancel) must target it. ``None``
    means the local cluster, and is always the value for local-parallel jobs.
    """

    job_id: str
    spec: object
    cluster: str | None = None


def telemetry_key(handle: JobHandle) -> str:
    """Key identifying one handle in the head's per-job mappings.

    The bare job id, or ``<cluster>:<job id>`` when the handle has a cluster,
    because the same id can occur on two clusters.
    """
    cluster = getattr(handle, "cluster", None)
    return f"{cluster}:{handle.job_id}" if cluster else handle.job_id


def split_handle_key(key: str) -> tuple[str | None, str]:
    """Inverse of :func:`telemetry_key`: ``(cluster, scheduler job id)``.

    Use it before showing a key to a user or passing it to a scheduler, which
    wants the bare id plus ``-M <cluster>``.
    """
    cluster, sep, job_id = key.partition(":")
    return (cluster, job_id) if sep else (None, key)


class DispatchBackend(ABC):
    """One remote-execution backend.

    Implementations raise ``FatalRtlBuddyError`` on submission failure and make
    ``cancel_all`` best-effort.
    """

    name: str = "?"

    # False for a backend that runs jobs itself, where a missing result cannot be
    # blamed on a scheduler.
    scheduled: bool = True

    @abstractmethod
    def submit_build(
        self, spec: BuildJobSpec, *, dependency: str | None = None
    ) -> JobHandle:
        """Submit one suite's build job; return its handle without waiting.

        ``dependency`` is a job id this job must not start before (the verilate
        job, for the ``build`` half of a split compile).
        """

    @abstractmethod
    def submit(
        self,
        spec: RunnableJobSpec,
        *,
        dependency: str | None = None,
        delay_sec: float = 0.0,
    ) -> JobHandle:
        """Submit one sim job; return its handle without waiting.

        ``dependency`` is a build job id that must succeed first. ``delay_sec``
        holds the job back for the retry backoff; the backend serves the wait
        (Slurm: ``sbatch --begin``), not the head.
        """

    def submit_array(
        self,
        specs: list[RunnableJobSpec],
        *,
        array_dir: Path,
        max_parallel: int | None = None,
        dependency: str | None = None,
    ) -> list[JobHandle]:
        """Submit a group of jobs with identical resolved resources.

        The default loops :meth:`submit`; Slurm overrides it to submit arrays
        (several, if the group exceeds the scheduler's array limit). Returned
        handles are in spec order. ``array_dir`` is scratch space on the shared
        filesystem, ``max_parallel`` caps concurrency per submitted array, and
        ``dependency`` is a build job id gating every element.
        """
        return [self.submit(spec, dependency=dependency) for spec in specs]

    # True for a backend whose jobs run off the submit host, so moving the coverage tail
    # (merge, model build, LCOV exports) into a job of its own saves the submit host its
    # memory and time. A backend that runs jobs on the head's machine keeps it in-process.
    dispatches_coverage_tail: bool = False

    def submit_coverage(self, spec: CoverageJobSpec) -> JobHandle:
        """Submit the coverage tail job; return its handle without waiting.

        Only called when :attr:`dispatches_coverage_tail` is true. The head submits it
        once the fleet is collected and waits on it with :meth:`wait_all`.
        """
        raise NotImplementedError(
            f"the {self.name} backend does not run the coverage tail as a job"
        )

    def advance(self) -> None:
        """Let a self-executing backend make progress without blocking.

        No-op for scheduler-backed backends.
        """

    @abstractmethod
    def wait_all(self, handles: list[JobHandle], *, extra_wait: float = 0.0) -> None:
        """Block until every submitted job has left the queue.

        ``extra_wait`` extends this call's ``cfg-dispatch.max-wait`` by the retry
        backoff the head asked the backend to serve.
        """

    def live_job_ids(
        self, handles: Sequence[JobHandle | None], *, timeout_s=None
    ) -> set[str]:
        """Ids of these jobs that the backend still holds.

        Asked about jobs an interrupted run submitted, to see whether its fleet
        outlived the head. The default is none: a self-executing backend's jobs
        die with the head. A scheduler-backed override must treat a query it
        could not run as "unknown", never as "gone". ``timeout_s`` bounds one query.
        """
        return set()

    @abstractmethod
    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        """Best-effort cancellation of all outstanding jobs.

        Tolerates ``None`` entries, such as the absent build handle of a
        zero-test suite.
        """

    # The scheduler arguments this backend appends to every submission. Right-sizing
    # reads cpu-request overrides from here, not from the suite's `cfg-dispatch`,
    # because the backend is built once from the orchestration config.
    effective_sbatch_args: tuple = ()
    # The config file those arguments came from, used as the `file` of right-sizing's
    # `edit_hint`. None if the backend was built without one.
    effective_sbatch_args_path: str | None = None

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Per-job reserved-vs-used accounting, keyed by :func:`telemetry_key`.

        Returns an empty mapping when the backend has no accounting source.
        """
        return {}

    def build_outcome(self, handle: JobHandle) -> str | None:
        """How the build job ended, or ``None`` if unknown.

        ``"COMPLETED"`` means it exited 0; anything else names how it did not
        (``"FAILED"``, a scheduler state). The head uses it to tell a job that
        died partway from one that finished but lost its final envelope write.
        ``None`` keeps the conservative reading of an incomplete envelope.
        """
        return None

    def accounting_interval_s(self) -> float | None:
        """Seconds between usage samples, or ``None`` if not known.

        Peak memory is a high-water mark over samples, so right-sizing uses this
        to judge whether a memory number is worth advising from.
        """
        return None
