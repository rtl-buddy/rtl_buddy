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
    # What the CONFIG asked for, before that min() (#547 review). `parallel`
    # is what this job gets; when the plan capped it, the two differ and
    # only this one names a number the reader can find in a file. Carried so
    # the job's own console line can say "compile.parallel is 4, capped to 2"
    # instead of attributing the capped 2 to a key that says 4. `None` means
    # "not stated" — read it as equal to `parallel`, which is what every
    # caller that does not set it means.
    parallel_configured: int | None = None
    # `--rebuild`: compile even where a stamp validates (#494). The build
    # job is where the head puts it — it is the single writer of the shared
    # directory, and its per-process memo turns one user request into
    # exactly one rebuild per build dir for the whole suite.
    rebuild: bool = False
    # Where the head records which sim job holds which plan index (#548),
    # so this job can clear the `afterok` dependency of one compile key's
    # sims the moment that key is built instead of holding them for the
    # slowest key in the plan. Set only for a backend that can be told to
    # release a pending job (Slurm); ``None`` everywhere else keeps the
    # argv — and the gate — exactly as it was. See ``dispatch.gates``.
    gates_json: Path | None = None
    # The persistent shared-build cache root the head resolved (#542), or
    # None for the in-tree default. Carried rather than re-derived in the
    # job: the head's precedence (CLI over environment over config) is not
    # reproducible from the job's own environment alone, and a build job
    # that picked a different root from its simulation jobs would compile
    # where none of them looks.
    #
    # Tri-state: a path enables the cache, `""` says the head was explicitly
    # told NOT to cache (and travels as an empty `--shared-build-root`, which
    # the job parses the same way), and None says nothing at all — leaving
    # the job to resolve its own, which is what keeps an unconfigured
    # project's job script byte-identical.
    shared_build_root: str | None = None


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
    master_seed: int | None = None
    resolved_seed: int | None = None
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
    # The same resolved cache root the build job was given (#542): both
    # sides derive the shared build directory from it, so they must agree.
    # Tri-state, as on :class:`BuildJobSpec`.
    shared_build_root: str | None = None
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

    def live_job_ids(
        self, handles: Sequence[JobHandle | None], *, timeout_s=None
    ) -> set[str]:
        """Which of these jobs the backend still holds — ids, not handles.

        Unlike :meth:`wait_all` this is asked about jobs THIS process never
        submitted: an interrupted run's manifest names them, and the
        question is whether its fleet outlived the head that launched it
        (#521). The default answer is "none", which is the truth for every
        backend that executes jobs itself — a local-parallel pool's
        subprocesses are children of the head and die with it, so an
        interrupted run leaves nothing to find.

        A scheduler-backed backend overrides this and must be conservative
        about a query it could not run: reporting "gone" from a broken
        ``squeue`` would let a second fleet be submitted alongside a first
        one that is still running. ``timeout_s`` bounds one such query for
        a caller that is itself on a deadline; running out of time is one
        more way not to know, never a drain.
        """
        return set()

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
    # ...and the config file those arguments were read from, travelling
    # beside them for the same reason. An `sbatch-args` cpu override makes
    # right-sizing point its machine-readable `edit_hint` at
    # `cfg-dispatch.sbatch-args`, and the `file` half has to be the config
    # the INSTANTIATED backend reads: in a multi-root regression the
    # suite's own root_config.yaml is a different file, and an agent
    # applying a hint that named it would edit a `cfg-dispatch` nothing
    # submits with, leaving the override — and the advice — in place
    # (#527). ``None`` where the backend was built without one.
    effective_sbatch_args_path: str | None = None

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

    def build_outcome(self, handle: JobHandle) -> str | None:
        """How this suite's build job ENDED, or ``None`` if not knowable.

        ``"COMPLETED"`` means it ran to the end and exited 0; anything else
        is a name for how it did not (``"FAILED"``, a scheduler state).
        Not a substitute for the build-result envelope, which says what it
        built — this says only whether it got to the end of saying it.

        The head asks exactly one question with it (#548). A build job
        that releases a compile key's simulations early rewrites its
        envelope as it goes, so an incomplete envelope can mean two very
        different things: a job that died partway, whose unnamed tests
        were never compiled and whose jobs were cancelled behind it, or a
        job that finished and lost only the write that would have marked
        the envelope complete. The first must keep those tests' gates shut
        — reopening them would let the retry round resubmit, ungated, jobs
        the head deliberately skipped (#405) — and the second must not,
        since every test really was compiled.

        ``None`` (the default, and what a backend answers for a job it
        cannot speak for) keeps the conservative reading, so a backend
        that never implements this loses nothing it had.
        """
        return None

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
