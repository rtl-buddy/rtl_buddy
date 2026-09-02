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
    extra_sim_timeout: int | None = None
    log_path: Path | None = None
    # Dispatch plan manifest (absolute) written by the head after a single
    # sweep expansion; the build job compiles exactly its entries instead
    # of re-running the sweep hook. See ``config.test.TestConfig`` plan
    # (de)serialization and ``dispatch.plan``.
    plan_path: Path | None = None
    # Where the build job records its compile outcome (built/failed test
    # names); the head loads it at collect for compile-fail parity.
    result_json: Path | None = None
    # Distinct builds the job compiles concurrently (#495). The head has
    # already folded in the min() against how many configs it planned, so a
    # backend may size the reservation by this number without re-capping it.
    parallel: int = 1
    # `--rebuild`: compile even where a stamp validates (#494). The build
    # job is where the head puts it — it is the single writer of the shared
    # directory, and its per-process memo turns one user request into
    # exactly one rebuild per build dir for the whole suite.
    rebuild: bool = False


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
    extra_sim_timeout: int | None = None
    share_build: bool = True
    # True when the head gated this job on a build job, so the job knows
    # that compiling means the build's stamp failed to validate and every
    # sibling element is about to compile too (#369).
    expect_prebuilt: bool = False
    # `--rebuild` for a sim job — set ONLY when the suite submitted no build
    # job, so this job owns the directory it would rebuild (#494). A gated
    # job must never carry it: the build job has already rebuilt, its fresh
    # stamp short-circuits this compile, and forcing the compile anyway
    # would put every element of the array into one build directory at once
    # (#369).
    rebuild: bool = False
    # That build job's result envelope, when there is one (#498). It is what
    # lets a gated job tell the two reasons a stamp fails to validate apart:
    # a compile that FAILED for this test is deterministic and must not be
    # retried under the sim's reservation (the retry's own failure would
    # overwrite the build's `compile.log` and hide the real error), while a
    # merely absent or stale stamp still deserves the recompile.
    build_result_json: Path | None = None
    log_path: Path | None = None
    # Dispatch plan manifest (absolute); the sim job resolves ``test_name``
    # from it instead of re-running the suite's sweep hook. See BuildJobSpec.
    plan_path: Path | None = None

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
class JobHandle:
    """An accepted submission: the backend's job id plus its spec.

    ``cluster`` records WHERE the scheduler accepted it, for the backends
    that can submit somewhere other than the local cluster (Slurm's
    ``-M``/``--clusters``, #509). A job id is only unique within its
    cluster, so every later command about this job — cancelling it above
    all — has to be issued against the same one. ``None`` means "the local
    cluster", which is every submission a single-cluster site makes and
    every job the local-parallel pool runs.
    """

    job_id: str
    spec: object
    cluster: str | None = None


def telemetry_key(handle: JobHandle) -> str:
    """Identity of one handle in any per-job mapping the head keeps.

    Named for its first consumer (:meth:`collect_telemetry`), used by
    every mapping that must not merge two jobs: the collector's result and
    the wait's outstanding set / per-suite membership alike.

    A job id is unique only within its cluster, and a run can span
    clusters — ``--clusters=a,b`` places each array wherever it can start
    first — so two handles can legitimately carry the SAME id. Keying
    telemetry by id alone then merges their rows: allocation values
    overwrite each other, step metrics sum across unrelated jobs, and both
    jobs are right-sized from the mixture (#509 review).

    Prefixed with the cluster only where there is one, so a local or
    single-cluster run keys by the bare job id exactly as before.
    """
    cluster = getattr(handle, "cluster", None)
    return f"{cluster}:{handle.job_id}" if cluster else handle.job_id


def split_handle_key(key: str) -> tuple[str | None, str]:
    """Inverse of :func:`telemetry_key`: ``(cluster, scheduler job id)``.

    The qualified key is an INTERNAL identity — it keeps two clusters'
    identically numbered jobs apart in the head's own mappings — and a
    scheduler has never heard of it. Anything user-facing (a recovery
    command, an id to paste into ``squeue``) has to take the two halves
    apart again, because Slurm wants the bare id and ``-M <cluster>``
    beside it, not ``alpha:77`` (#509 review).

    Unambiguous by construction: a Slurm job id is digits, underscores and
    brackets, and a cluster name is a bare word — neither contains a colon.
    """
    cluster, sep, job_id = key.partition(":")
    return (cluster, job_id) if sep else (None, key)


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
    def submit(
        self,
        spec: RunnableJobSpec,
        *,
        dependency: str | None = None,
        delay_sec: float = 0.0,
    ) -> JobHandle:
        """Submit one sim job; return its handle without waiting.

        ``dependency`` gates it on a build job id that must succeed first.
        ``delay_sec`` holds it out of contention for that long before it
        may start — the retry backoff (#405). It is the *backend's* job to
        serve that wait, never the head's: a scheduler-backed backend hands
        it to the scheduler (``sbatch --begin``), so a delayed job occupies
        no allocation while it waits and the head stays a planner/poller.
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

        Backends with native array support (Slurm) override this to
        submit the group as one array — or as several, where the group is
        larger than the scheduler's own array limit (#509); the default
        just loops :meth:`submit`. Either way the returned handles are in
        spec order and describe one logical group.
        ``array_dir`` is a scratch directory on the shared filesystem
        for the array's manifest/script/logs; ``max_parallel`` caps how
        many elements run concurrently **per submitted array**;
        ``dependency`` (a build-job id) gates every element on that job
        succeeding.
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
    def wait_all(self, handles: list[JobHandle], *, extra_wait: float = 0.0) -> None:
        """Block until every submitted job has left the queue.

        ``extra_wait`` widens this call's ``cfg-dispatch.max-wait``
        allowance by a delay the head *knowingly* asked the backend to
        serve — the retry backoff (#405). A held job is outstanding for
        the whole backoff (Slurm reports it PENDING/BeginTime, the pool
        keeps it queued), so without this a ``max-wait`` shorter than the
        backoff would trip the deadline every time on a wait that had not
        yet let the job start. ``max-wait`` still bounds each wait; it has
        never bounded their sum, and a run with retry enabled can take up
        to ``attempts × (backoff + max-wait)``.
        """

    @abstractmethod
    def cancel_all(self, handles: Sequence[JobHandle | None]) -> None:
        """Best-effort cancellation of all outstanding jobs.

        Tolerates ``None`` entries: it is the last line of defence against
        an orphaned fleet on a head-side failure, so a caller that let a
        ``None`` slip into the handle list (e.g. a zero-test suite's absent
        build handle, #361) must not disarm it.
        """

    # The scheduler arguments this backend actually appends to every
    # submission, as it holds them. Empty for a backend that has none.
    #
    # Right-sizing reads its cpu-request overrides from HERE and not from
    # the suite's `cfg-dispatch`, because the two can differ: the backend is
    # instantiated once, from the orchestration config, before the suite
    # loop, while `root_cfg` is rebuilt per suite whenever a suite walks up
    # to a different root_config.yaml. Scanning the suite's config would
    # then describe a command that was never submitted — missing an
    # override the backend really passes, or inventing one it does not
    # (#505 review).
    effective_sbatch_args: tuple = ()

    def collect_telemetry(self, handles: list[JobHandle]) -> dict[str, dict]:
        """Per-job reserved-vs-used accounting, keyed by :func:`telemetry_key`.

        That is the bare job id for every backend that cannot submit off
        the local cluster, so a consumer's lookup is unchanged; use the
        helper rather than the id, since a Slurm run spanning clusters
        keys the jobs it accepted elsewhere by ``<cluster>:<job id>``.

        Returns an empty mapping when the backend has no accounting
        source (right-sizing then degrades gracefully). Values are
        backend-shaped dicts; the Slurm backend documents its fields.
        """
        return {}

    def accounting_interval_s(self) -> float | None:
        """Seconds between usage samples, or ``None`` if not known.

        Peak memory in :meth:`collect_telemetry` is a high-water mark over
        samples, so a job that finished inside one interval was measured at
        most once and its peak means nothing. Right-sizing asks for this to
        decide whether a memory number is worth advising from (#365). A
        backend with no accounting at all never reaches that question, since
        it reports no telemetry either.
        """
        return None
