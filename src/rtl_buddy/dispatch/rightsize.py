# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Reservation right-sizing analysis (#351 P3).

Consumes the per-job ``sacct`` telemetry the collector attached to
dispatched results (P2) and produces per-test advice: which resource is
over- or under-reserved, by how much, and what to set it to. rtl-buddy
reports and suggests; it never rewrites tests.yaml — each finding carries
an edit hint naming the exact ``resources:`` field so an agent (or
human) can apply it as a reviewable diff.

Semantics:

- Utilization is judged per *test*, not per run: the peak across the
  test's run_ids/seeds in this invocation, so a suggestion covers the
  worst observed run.
- Below ``over-threshold`` → over-reserved (``reduce``); above
  ``near-limit`` or a scheduler TIMEOUT / OUT_OF_MEMORY kill →
  under-reserved (``raise``, with the kill taking precedence over
  whatever numbers were captured). Suggested = peak × ``margin``,
  rounded to scheduler-friendly units with sane floors.
- Time advice needs trustworthy elapsed time: it is skipped for
  non-Verilator simulator families because a VCS ``-licqueue`` wait
  would masquerade as compute time (rtl-buddy/rtl_buddy#329), and
  skipped when the limit is unknown.
- Memory advice needs a peak that was actually sampled. ``MaxRSS`` is a
  high-water mark over accounting samples, so a test whose longest run
  finished inside one sampling interval was measured at most once and
  reports near-zero — 17-27x below the truth on a site running the stock
  30 s ``JobAcctGatherFrequency`` (rtl-buddy/rtl_buddy#365). Utilization-based
  memory advice is suppressed for those tests and the omission is logged,
  because a too-small ``mem`` gets the job OOM-killed: this is the one
  resource where confidently wrong advice costs more than none. An
  ``OUT_OF_MEMORY`` kill still raises, being a fact about the reservation
  rather than a measurement of it — the same rule the ``TIMEOUT`` case
  follows for time.
- Cpu efficiency is measured against the *requested* cpus, not the
  allocated ones (rtl-buddy/rtl_buddy#505). A site that allocates whole
  cores reports ``AllocCPUS=2`` for a job that asked for one, so a
  single-threaded test judged against the allocation cannot beat 0.5
  efficiency and is advised down to the ``cpus: 1`` its tests.yaml already
  says — every run, forever, because no edit can retire it. The request is
  what a ``resources:`` field controls, so it is the denominator and it is
  what a finding reports as ``reserved``; the allocated figure rides along
  in ``allocated`` when it differs, so ``squeue``/``sacct`` still
  reconcile. The denominator is preferably the reservation rtl-buddy itself
  resolved and submitted, which is the request by construction and needs no
  cooperation from the site; ``ReqCPUS`` and then ``AllocCPUS`` are the
  fallbacks, for a caller that cannot supply it, for telemetry predating
  the field, and for a ``cfg-dispatch.sbatch-args`` carrying its own
  ``--cpus-per-task`` — appended after the generated flags, so it
  supersedes the reservation rtl-buddy resolved. ``mem`` and ``time``
  never had that exposure: they are judged against ``ReqMem`` and
  ``TimelimitRaw``, which sacct reports from the allocation an override
  actually produced.
- Advice is labeled with the run count and regression level it was
  derived from — a smoke-level run must not be used to shrink a
  nightly test's reservation.
- Advice names the field that actually *governs* the reservation
  (rtl-buddy/rtl_buddy#358). A job whose builder cannot share a build
  compiles inside itself, so its single allocation covers both phases and
  was resolved as the per-field maximum of the sim and compile
  reservations. Where the compile side won, editing the test's
  ``resources:`` would change nothing — so the hint points at
  ``cfg-dispatch.compile`` instead, and the finding is labeled
  ``compile+sim`` to say the measurement spans both.
- The suite's *build job* gets one row of its own, labeled ``compile``
  (rtl-buddy/rtl_buddy#495). It owns no per-test row, so
  :func:`analyze_build_reservation` reads its ``sacct`` entry directly and
  asks the same two questions of it — wall clock against the limit, cpu
  time against the allocation. Its cpus suggestion is divided back down by
  ``cfg-dispatch.compile.parallel``, because the field a project edits is
  per-build while the reservation the head submitted was the product — and
  its denominator is the requested cpus, for the same reason a test's is. Its
  ``reduce`` needs the build envelope to say a compile actually ran: a
  re-run of an unchanged suite short-circuits every build on its stamp, and
  reading those seconds as "the compile is fast" would advise a limit the
  next real RTL change times out against — which cancels the whole afterok
  fan-out, the failure the build job's exit-0 contract exists to prevent.
- Every suggestion for such a job is *reachable*: a ``reduce`` is clamped
  up to the compile reservation, because the ``max`` will not let the
  allocation go below it however far the test's own ``resources:`` are
  trimmed. A suggestion the clamp pushes back to the current reservation
  saves nothing and is dropped — advising a reduction that cannot happen
  is worse than silence, since the agent loop reruns, sees the advice fail
  to retire, and cannot tell that from a wrong suggestion.
"""

import logging
import math
from dataclasses import dataclass, field

from ..config.dispatch import (
    mem_to_bytes,
    sbatch_arg_sets_cpu_count_directly,
    time_to_seconds,
)
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

_TIME_FLOOR_S = 300  # never suggest a limit under 5 minutes
_MEM_FLOOR_BYTES = 128 * 2**20  # never suggest under 128M
# A reduce suggestion must actually save something, or it is churn.
_REDUCE_KEEP_RATIO = 0.75


@dataclass
class RightsizeFinding:
    suite: str
    test: str
    resource: str  # "time" | "mem" | "cpus"
    reserved: str
    peak: str
    utilization: float
    direction: str  # "reduce" | "raise"
    suggested: str
    runs: int
    reg_level: int | None
    states: list = field(default_factory=list)
    edit_hint: dict = field(default_factory=dict)
    # Which phases the job's single allocation had to cover: "sim" normally,
    # "compile+sim" when the builder could not share a build and the compile
    # therefore ran inside the job (#358).
    phase: str = "sim"
    # What the scheduler actually handed out, when that is not what the
    # reservation asked for — a site allocating whole cores gives a job that
    # requested 1 cpu 2 of them (#505). `reserved` is always the requested
    # figure, because that is the one the named edit hint can move; this is
    # additive, and None whenever the two agree or nothing reported an
    # allocation. Only ever set on a `cpus` finding.
    allocated: str | None = None

    def as_event(self) -> dict:
        return {
            "event": "reservation-advice",
            "suite": self.suite,
            "test": self.test,
            "resource": self.resource,
            "reserved": self.reserved,
            "peak": self.peak,
            "utilization": round(self.utilization, 3),
            "direction": self.direction,
            "suggested": self.suggested,
            "runs": self.runs,
            "reg_level": self.reg_level,
            "states": list(self.states),
            "edit_hint": dict(self.edit_hint),
            "phase": self.phase,
            "allocated": self.allocated,
        }


def format_mem(bytes_val: int) -> str:
    """Bytes → sbatch-friendly integer ``M``/``G`` string (rounded up)."""
    mb = math.ceil(bytes_val / 2**20)
    if mb >= 4096:
        return f"{math.ceil(mb / 1024)}G"
    return f"{mb}M"


def format_time(seconds: float) -> str:
    """Seconds → ``HH:MM:SS`` rounded up to the whole minute."""
    minutes = math.ceil(seconds / 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _override_note(sbatch_args: list, masked_path: str) -> str:
    """Why a cpus finding names ``sbatch-args`` instead of a YAML field.

    ``cfg-dispatch.sbatch-args`` is appended after the generated reservation
    flags, so an argument there decides the job's cpu request — either
    directly (``--cpus-per-task``) or as the task/node count ``ReqCPUS``
    multiplies it by (``--ntasks``, ``--ntasks-per-node``, ...). Advice
    that named the masked field would be unappliable: the edit lands, the
    next job is submitted with the same argument, and the finding returns
    (#505 review).

    Only one shape can be handed the suggested number: a single DIRECT cpu
    count (``-c``/``--cpus-per-task``), which states the
    request outright. Two shapes cannot. Several arguments MULTIPLY —
    ``ReqCPUS`` is *tasks x cpus-per-task* — so none of them alone takes a
    whole-job figure. And a lone task or topology modifier (``--ntasks``,
    ``--ntasks-per-node``, ``--nodes``) is not a cpu count at all:
    writing 3 into ``--ntasks`` asks for
    three tasks, not three cpus. Telling a reader to do either would
    produce exactly the unappliable advice this rule exists to prevent, so
    the note states what the arguments do and hands the decomposition back
    to the reader, who is the only party that knows which factor should
    shrink (#505 review).
    """
    quoted = [f"`{arg}`" for arg in sbatch_args]
    if len(quoted) == 1 and sbatch_arg_sets_cpu_count_directly(sbatch_args[0]):
        return (
            f"sbatch-args {quoted[0]} sets this job's cpu request, "
            f"superseding {masked_path}; change it there. Suggested value "
            "is the whole-job cpu count."
        )
    if len(quoted) == 1:
        return (
            f"sbatch-args supersedes {masked_path}: {quoted[0]} scales "
            "this job's cpu request rather than stating it. Suggested "
            "value is the whole-job cpu count — that argument does not "
            "take it, so work out the reservation that reaches it "
            "yourself."
        )
    return (
        f"sbatch-args supersedes {masked_path}: this job's cpu request is "
        f"the product of {' x '.join(quoted)}. Suggested value is the "
        "whole-job cpu count — decompose it across those arguments "
        "yourself; no single one of them takes it."
    )


def _aggregate(rows):
    """Per-test peaks across runs: {test: {field: value, 'runs': n, ...}}."""
    per_test: dict[str, dict] = {}
    for row in rows:
        results = row.get("results")
        if results is None:
            continue
        telemetry = results.results.get("telemetry")
        if not telemetry:
            continue
        agg = per_test.setdefault(
            row["test_name"],
            {
                "runs": 0,
                "states": [],
                "builder": row.get("builder"),
                # Set by the dispatch head for a job that compiles inside
                # itself; governed_by then says which layer sized each field.
                "compile_in_job": bool(row.get("compile_in_job")),
                "governed_by": row.get("governed_by") or {},
                "compile_floor": row.get("compile_floor") or {},
                # What the head resolved and submitted as `--cpus-per-task`
                # for this test. It IS the request by construction, so it
                # beats anything the scheduler reports back: `AllocCPUS` is
                # post-rounding, and `ReqCPUS` is post-rounding too on a
                # Slurm that normalizes it before accounting (#505).
                "requested_cpus": row.get("requested_cpus"),
                # The `sbatch-args` entries that superseded it, if any: the
                # denominator falls back to the scheduler, and the edit hint
                # has to name them rather than a YAML field they mask. More
                # than one means they multiply, and no single one of them
                # can be handed the suggestion (#505 review).
                "cpus_override": row.get("cpus_override"),
            },
        )
        agg["runs"] += 1
        state = telemetry.get("state")
        if state and state not in agg["states"]:
            agg["states"].append(state)
        for key in (
            "elapsed_s",
            "timelimit_s",
            "alloc_cpus",
            "req_cpus",
            "req_mem_bytes",
            "total_cpu_s",
            "max_rss_bytes",
        ):
            value = telemetry.get(key)
            if value is None:
                continue
            agg[key] = max(agg.get(key, 0), value)
        # CPU efficiency is a RATIO, so it must be computed per run and the
        # best (max) kept — deriving it from independently-maxed numerator
        # and denominator would mix numbers from different seeds and could
        # advise shrinking a reservation the busiest run actually saturated.
        # ...and against the REQUESTED cpus, which is what the reservation
        # asked for and the only number a tests.yaml edit moves. A site
        # allocating whole cores hands out more than that, and rationing a
        # single-threaded job against the surplus advises a reduction to the
        # value already in the YAML (#505). Preference order: what the head
        # submitted, then what the scheduler says was requested, then what it
        # allocated — each step is one remove further from the field a
        # project edits.
        cpus = (
            row.get("requested_cpus")
            or telemetry.get("req_cpus")
            or telemetry.get("alloc_cpus")
        )
        cpu_time = telemetry.get("total_cpu_s")
        elapsed = telemetry.get("elapsed_s")
        if cpus and cpu_time is not None and elapsed:
            eff = cpu_time / (elapsed * cpus)
            agg["cpu_efficiency"] = max(agg.get("cpu_efficiency", 0.0), eff)
    return per_test


# The build job is not a test, but every finding needs a row label. A
# parenthesised name cannot collide with a real test name.
BUILD_JOB_ROW = "(build job)"


def analyze_build_reservation(
    build_telemetry,
    compile_resources,
    parallel,
    rightsize_cfg,
    suite_display,
    root_config_hint,
    *,
    compile_work=None,
    accounting_interval_s=None,
    compile_origins=None,
    suite_config_hint=None,
    cpus_override=None,
):
    """Right-size the *build job's* own reservation (#495).

    A suite's build job is one allocation running up to ``parallel``
    concurrent Verilations, so it is the one job in a dispatched fleet that
    ``analyze_suite_reservations`` cannot see: it owns no ``suite_results``
    row, no test name, and no per-run peak. Its numbers still come from the
    same ``sacct`` fields, so the advice is the same two questions asked of
    one job — did it use its wall clock, and did it use its cpus.

    Two things are deliberately absent. There is no memory advice: MaxRSS
    is a sampled high-water mark and a build job is exactly the kind of
    short job #365 showed it under-reports, and a too-small ``mem`` gets
    the compile OOM-killed. And there is no ``raise`` on cpus: cpu
    efficiency below 1 means slots idled, never that more were needed.

    ``compile_resources`` is the *per-build* reservation
    (``cfg-dispatch.compile``); the head multiplied its cpus by
    ``parallel`` before submitting, and the field a project edits is
    per-build — so cpus advice is only offered for a job that ran an
    effective ``parallel`` of 1 (one slot, or one build record), where the
    whole-job ratio and the per-build one are the same number. Above that
    the ratio also carries the tail (unequal builds; a plan with fewer
    distinct keys than slots), so dividing it by ``parallel`` can advise
    shrinking the cpus the longest compile saturated, and there is no
    per-compile cpu telemetry to tell the causes apart — the row is
    withheld with reason ``parallel-utilization-ambiguous`` (#496 review).
    Its resolved ``cpus`` is what the advice *names* as the current
    per-build value, in preference to dividing AllocCPUS: a site where the
    scheduler reports more cpus than were requested would otherwise be
    shown a decomposition that does not match its YAML. The efficiency
    ratio follows the same rule: its denominator is that resolved value
    scaled by ``parallel`` — the job's own ``--cpus-per-task`` — then
    ``ReqCPUS``, then ``AllocCPUS`` (#505). ``cpus_override`` withdraws
    the first of those: ``cfg-dispatch.sbatch-args`` is appended after
    the generated flags and wins, so an argument written there that sets
    the cpu request means the resolved value was never submitted and may
    state neither the ratio nor the decomposition. It is the LIST of such
    arguments (see
    :func:`~rtl_buddy.config.dispatch.sbatch_args_cpu_request_options`), so
    the cpus row's ``edit_hint`` can name ``cfg-dispatch.sbatch-args``, say
    which field it masks, and — where several of them multiply — decline to
    put the suggestion on any one of them. Empty telemetry (a
    local-parallel backend reports none) yields no advice at all.

    ``compile_work`` is what the build envelope says the job actually did:
    ``{"records": n, "compiled": n, "compiled_sec": float}``, or ``None``
    when the head could not tell (no envelope, or one written before the
    records existed). It is what separates *nothing to compile* from
    *compiled fast* — on any re-run of an unchanged suite every build
    short-circuits on its stamp, so the job is seconds long with near-zero
    cpu time, and a naive reading advises a five-minute limit that the
    next real RTL change TIMEOUTs against, taking the whole afterok
    fan-out with it. So a ``reduce`` needs evidence that a compile ran;
    ``raise`` is unconditional, being a fact about the reservation rather
    than a measurement of the work. ``accounting_interval_s`` withholds
    ``reduce`` for the same reason ``analyze_suite_reservations`` withholds
    memory advice: ``TotalCPU`` is accumulated from usage samples, so a job
    that finished inside one interval was measured at most once.

    ``compile_origins`` says, per field, where the *winning* value came
    from — ``{"mem": "suite"}`` when the suite's own ``compile:`` block set
    it (#497) — and ``suite_config_hint`` is that suite's tests.yaml path.
    Together they decide which file an edit hint names: advice that says
    "shrink ``cfg-dispatch.compile.mem``" is wrong for a field a suite
    block overrides, because editing the root config would not move this
    job's reservation at all. The map is computed by
    :func:`~rtl_buddy.config.dispatch.compile_resource_origins` beside the
    layering it mirrors and handed in — never guessed here from the
    values, which cannot tell an override from a coincidence.
    """
    if not build_telemetry:
        return []
    findings = []
    elapsed = build_telemetry.get("elapsed_s")
    # Only a `reduce` is gated: it is the direction that can shrink a
    # reservation below what the next run needs.
    compiled = (compile_work or {}).get("compiled") or 0
    # Three answers, not two. No records at all means the head could not
    # tell — the build job left no envelope (an OOM kill or a TIMEOUT still
    # leaves the sacct row that got us here), or wrote one predating them.
    # The gating treats unknown as no-reduce either way, but the recorded
    # reason must not claim every build reused a stamp it never saw.
    records = (compile_work or {}).get("records") or 0
    undersampled = (
        accounting_interval_s is not None
        and elapsed is not None
        and elapsed < accounting_interval_s
    )
    may_reduce = bool(compiled) and not undersampled
    if not may_reduce:
        # INFO, not WARNING: an all-reused build job is the normal shape of
        # every re-run, and a warning per re-run is noise. It is still
        # recorded, because "no advice" and "advice withheld" are different
        # answers to look back at.
        log_event(
            logger,
            logging.INFO,
            "rightsize.build_advice_withheld",
            suite=suite_display,
            reason=(
                "undersampled"
                if undersampled
                else ("no-compile-observed" if records else "no-build-records")
            ),
            # None, not 0, when there was no envelope: the field keeps the
            # same three-state meaning `compile_work` has.
            builds=(compile_work or {}).get("records"),
            compiled=compiled,
            # How much of the reservation's wall clock was real compiling —
            # the number that says "the job spent 55s of its 2h building",
            # which sacct's elapsed alone cannot separate from queueing and
            # stamp checks. Same three-state rule as `builds`: None when
            # there was no envelope to measure.
            compiled_sec=(compile_work or {}).get("compiled_sec"),
            elapsed_s=elapsed,
            interval_s=accounting_interval_s,
        )
    # BuildJobSpec's typed field (>= 1); clamped anyway because every
    # cpus number below divides by it.
    parallel = max(1, parallel)
    state = build_telemetry.get("state")
    states = [state] if state else []

    origins = compile_origins or {}

    def hint(resource_field, note=None):
        # `cfg-dispatch.sbatch-args` is appended after the generated
        # reservation flags and wins, so an argument there that sets the cpu
        # request masks every cpus field the layering below could name.
        # Advice that named one would be unappliable, and would come back on
        # the next run (#505 review).
        if resource_field == "cpus" and cpus_override:
            masked = (
                "compile.cpus"
                if origins.get("cpus") == "suite" and suite_config_hint
                else "cfg-dispatch.compile.cpus"
            )
            edit = {
                "path": "cfg-dispatch.sbatch-args",
                "note": _override_note(cpus_override, masked),
            }
            if root_config_hint:
                edit["file"] = root_config_hint
            return edit
        # Point at whichever file holds the value that WON. A suite-level
        # `compile:` block is the most specific layer, so for a field it
        # set, editing cfg-dispatch would move nothing (#497). Otherwise
        # cfg-dispatch lives in root_config.yaml, and without a path to it
        # there is nothing honest to point at — a suite's tests.yaml does
        # not govern a build job it does not override.
        if origins.get(resource_field) == "suite" and suite_config_hint:
            edit = {"file": suite_config_hint, "path": f"compile.{resource_field}"}
        else:
            edit = {"path": f"cfg-dispatch.compile.{resource_field}"}
            if root_config_hint:
                edit["file"] = root_config_hint
        if note:
            edit["note"] = note
        return edit

    common = {
        "suite": suite_display,
        "test": BUILD_JOB_ROW,
        "runs": 1,
        "reg_level": None,
        "states": states,
        "phase": "compile",
    }

    # --- time -------------------------------------------------------
    limit = build_telemetry.get("timelimit_s")
    if limit and state == "TIMEOUT":
        findings.append(
            RightsizeFinding(
                resource="time",
                reserved=format_time(limit),
                peak=f">{format_time(limit)}",
                utilization=1.0,
                direction="raise",
                suggested=format_time(limit * rightsize_cfg.margin),
                edit_hint=hint("time"),
                **common,
            )
        )
    elif limit and elapsed is not None:
        util = elapsed / limit
        # `time` is NOT scaled by parallel — N concurrent builds take the
        # wall clock of the longest, not of their sum — so this suggestion
        # needs no division and says so by carrying no note.
        suggested_s = max(elapsed * rightsize_cfg.margin, _TIME_FLOOR_S)
        if util > rightsize_cfg.near_limit:
            findings.append(
                RightsizeFinding(
                    resource="time",
                    reserved=format_time(limit),
                    peak=format_time(elapsed),
                    utilization=util,
                    direction="raise",
                    suggested=format_time(suggested_s),
                    edit_hint=hint("time"),
                    **common,
                )
            )
        elif (
            may_reduce
            and util < rightsize_cfg.over_threshold
            and suggested_s <= limit * _REDUCE_KEEP_RATIO
        ):
            findings.append(
                RightsizeFinding(
                    resource="time",
                    reserved=format_time(limit),
                    peak=format_time(elapsed),
                    utilization=util,
                    direction="reduce",
                    suggested=format_time(suggested_s),
                    edit_hint=hint("time"),
                    **common,
                )
            )

    # --- cpus (efficiency; only ever suggests reducing) --------------
    alloc_cpus = build_telemetry.get("alloc_cpus")
    # What the head itself submitted, first: the resolved per-build cpus
    # scaled by `parallel` IS this job's `--cpus-per-task`, so it is the
    # request by construction and needs no cooperation from the site. A
    # cluster allocating whole cores hands the job more than that, and
    # rationing against the surplus advises a per-build value the config
    # already holds (#505). `ReqCPUS` is the next best thing for a head that
    # could not resolve the block, and the allocation the last (it is also
    # the only one available to telemetry predating the field).
    # ...unless `cfg-dispatch.sbatch-args` carries its own
    # `--cpus-per-task`: those are appended after the generated flags and
    # win, so the resolved reservation is not what was submitted and cannot
    # stand in for the request. `ReqCPUS` is then the best available answer
    # (#505 review) — and the decomposition below drops the same value for
    # the same reason.
    submitted = (
        0
        if cpus_override or compile_resources is None
        else (compile_resources.cpus or 0) * parallel
    )
    cpus = submitted or build_telemetry.get("req_cpus") or alloc_cpus
    cpu_time = build_telemetry.get("total_cpu_s")
    # Whole-job cpu efficiency is a *per-build* number only when the job ran
    # one build at a time. Above that it also carries the tail — builds of
    # unequal length, and a plan with fewer distinct keys than slots, both
    # leave reserved cpus idle while the longest compile saturates the ones
    # it has — so dividing it by `parallel` can advise shrinking exactly the
    # cpus that compile needed (#496 review). There is no per-compile cpu
    # telemetry to separate the causes with (sacct accounts the job, not the
    # thread group), so the advice is withheld rather than guessed at.
    effective_parallel = min(parallel, records) if records else parallel
    if may_reduce and cpus and cpus > 1 and cpu_time is not None and elapsed:
        # TotalCPU is summed over the job's steps, and elapsed is the wall
        # clock of the whole parallel batch — which is exactly the ratio
        # that says whether the scaled reservation was worth it.
        efficiency = cpu_time / (elapsed * cpus)
        if efficiency < rightsize_cfg.over_threshold and effective_parallel > 1:
            # Same event as the reduce gating above, because it is the same
            # question a reader asks of an empty row: nothing to say, or
            # something withheld? Time advice is unaffected — N concurrent
            # builds take the wall clock of the longest, so `elapsed`
            # against `timelimit` means what it always did.
            log_event(
                logger,
                logging.INFO,
                "rightsize.build_advice_withheld",
                suite=suite_display,
                reason="parallel-utilization-ambiguous",
                builds=(compile_work or {}).get("records"),
                compiled=compiled,
                compiled_sec=(compile_work or {}).get("compiled_sec"),
                elapsed_s=elapsed,
                interval_s=accounting_interval_s,
                parallel=parallel,
                efficiency=round(efficiency, 3),
            )
        elif efficiency < rightsize_cfg.over_threshold:
            suggested_total = max(
                1, math.ceil(cpus * efficiency * rightsize_cfg.margin)
            )
            # `effective_parallel` is 1 here by the branch above, so this is
            # the identity — kept as the division it is because that is what
            # makes the invariant legible: the per-build figure is the
            # whole-job one divided by the builds that were actually in
            # flight, and the only case where those are knowably equal is
            # one build at a time.
            suggested_per_build = max(
                1, math.ceil(suggested_total / effective_parallel)
            )
            # The decomposition is stated in terms of the value the project
            # would edit, so it has to come from the head's own resolved
            # `cfg-dispatch.compile.cpus` and not from AllocCPUS: a site
            # whose sbatch-args or CR_CPU rounding makes Slurm report more
            # cpus than were asked for would otherwise be told it reserved a
            # per-build number it never wrote, and `suggested_per_build`
            # could land on the value already in the YAML — advice that
            # never retires. sacct is the fallback for a head that could not
            # resolve the block at all.
            resolved_per_build = (
                getattr(compile_resources, "cpus", None)
                if compile_resources is not None and not cpus_override
                else None
            )
            per_build_now = resolved_per_build or math.ceil(cpus / parallel)
            requested_total = per_build_now * parallel
            # Say both numbers rather than pick one: the reader needs the
            # per-build figure to edit and the allocated figure to reconcile
            # with `squeue`/`sacct`. Only when they differ — a matching pair
            # explains nothing.
            alloc_clause = (
                f" (the scheduler reported {alloc_cpus} allocated)"
                if alloc_cpus and alloc_cpus != requested_total
                else ""
            )
            if requested_total == cpus and parallel == 1:
                # One build slot, so there is no product to decompose: the
                # reservation IS the per-build figure.
                decomposition = f"the build job reserved {per_build_now}{alloc_clause}"
            elif requested_total == cpus:
                decomposition = (
                    f"the build job reserved {cpus} = {per_build_now} "
                    f"x compile.parallel {parallel}{alloc_clause}"
                )
            else:
                decomposition = (
                    f"the build job asked for {requested_total} = "
                    f"{per_build_now} x compile.parallel {parallel}{alloc_clause}"
                )
            # The `parallel` lever only exists when it is above 1, and this
            # advice is only reachable at an effective 1 — so the sentence
            # is here for the one shape that has both: slots reserved for
            # builds the plan never produced.
            lever = (
                ""
                if parallel == 1
                else (
                    " `parallel` is the other lever: it is capped by the "
                    "suite's planned configs, not by its distinct compile "
                    "keys, so configs that share one key reserve cpus for "
                    "builds that never run — lower "
                    "cfg-dispatch.compile.parallel instead when the key "
                    "count is the smaller number."
                )
            )
            if suggested_per_build < per_build_now:
                findings.append(
                    RightsizeFinding(
                        resource="cpus",
                        # The scaled number the head actually asked for —
                        # the request, not the allocation, so a whole-core
                        # site is not shown a reservation it never wrote.
                        reserved=str(cpus),
                        # ...with what the scheduler gave beside it, so
                        # `squeue`/`sacct` still reconcile (#505).
                        allocated=(
                            str(alloc_cpus)
                            if alloc_cpus and alloc_cpus != cpus
                            else None
                        ),
                        peak=f"{efficiency:.2f} eff",
                        utilization=efficiency,
                        direction="reduce",
                        suggested=str(suggested_per_build),
                        edit_hint=hint(
                            "cpus",
                            note=(
                                f"per-build; {decomposition}. "
                                f"Suggested value is per-build.{lever}"
                            ),
                        ),
                        **common,
                    )
                )
    return findings


def analyze_suite_reservations(
    suite_results,
    *,
    suite_display,
    suite_config_path,
    rightsize_cfg,
    reg_level=None,
    simulator_family_of=None,
    root_config_path=None,
    accounting_interval_s=None,
    compile_origins=None,
):
    """Produce :class:`RightsizeFinding`s for one suite's dispatched rows.

    ``simulator_family_of`` maps a builder name to its simulator family
    (used to suppress time advice off Verilator, see module docstring);
    ``None`` disables that suppression. ``root_config_path`` is where
    ``cfg-dispatch`` lives, needed to hint at ``cfg-dispatch.compile`` for a
    field the compile reservation governs (#358); without it those findings
    fall back to the per-test hint. ``compile_origins`` says which of those
    compile fields the suite's own ``compile:`` block won (#497) — a field
    it set is named in the suite's tests.yaml instead, because
    cfg-dispatch is the layer the suite block overrides and editing it
    would leave the allocation exactly where it is, so the advice would
    never retire. ``accounting_interval_s`` is the
    scheduler's usage-sampling interval, used to suppress memory advice
    derived from a peak that was never sampled (#365); ``None`` disables
    that suppression.
    """
    findings = []
    unsampled = []
    origins = compile_origins or {}
    for test, agg in _aggregate(suite_results).items():
        governed_by = agg["governed_by"]
        # An in-job compile's allocation is max(sim, compile), so no `reduce`
        # can take it below the compile side however far the test's own
        # resources: are trimmed. These are the floors each suggestion is
        # clamped to; None where there is nothing to clamp against.
        floor = agg["compile_floor"]
        floor_mem_b = mem_to_bytes(floor.get("mem"))
        floor_time_s = time_to_seconds(floor.get("time"))
        cpus_override = agg.get("cpus_override")
        # ...but the cpus floor only bounds the reservation rtl-buddy
        # GENERATED, and a `sbatch-args` cpu argument supersedes that
        # whole reservation — the max never reaches sbatch. Clamping to a
        # floor the allocation does not have discards valid advice: an
        # override to 4 under a compile floor of 8 has every suggestion
        # pushed up to 8 and then dropped for not being below 4, so the
        # run reports nothing at all (#505 review).
        floor_cpus = None if cpus_override else floor.get("cpus")
        common = {
            "suite": suite_display,
            "test": test,
            "runs": agg["runs"],
            "reg_level": reg_level,
            "states": agg["states"],
            "phase": "compile+sim" if agg["compile_in_job"] else "sim",
        }

        # The YAML field the override masks — named in the note so a reader
        # can see what was superseded, resolved by the same layering the
        # unmasked hint would have used.
        if governed_by.get("cpus") != "compile":
            masked_cpus_path = f"tests[name={test}].resources.cpus"
        elif origins.get("cpus") == "suite" and suite_config_path:
            masked_cpus_path = "compile.cpus"
        else:
            masked_cpus_path = "cfg-dispatch.compile.cpus"

        def hint(
            resource_field,
            *,
            from_compile=False,
            _governed_by=governed_by,
            _cpus_override=cpus_override,
        ):
            # `cfg-dispatch.sbatch-args` is appended after the generated
            # reservation flags and wins, so an argument written there that
            # sets the cpu request masks every cpus field in the YAML.
            # Naming one of them would be advice that cannot be applied: the
            # edit lands, the next job is submitted with the same override,
            # and the finding comes back — the very shape #505 exists to
            # stop.
            if resource_field == "cpus" and _cpus_override:
                edit = {
                    "path": "cfg-dispatch.sbatch-args",
                    "note": _override_note(_cpus_override, masked_cpus_path),
                }
                if root_config_path:
                    edit["file"] = root_config_path
                return edit
            # A field the compile reservation won is masked by the max, so
            # editing the test's resources: would not move the allocation.
            from_compile = from_compile or _governed_by.get(resource_field) == "compile"
            # ...and of the two files that can hold the compile reservation,
            # the suite's own `compile:` block is the layer that wins, so a
            # field it set is edited there. Sending a project to
            # cfg-dispatch.compile for it would move nothing and the advice
            # would come back every run (#497).
            if (
                from_compile
                and origins.get(resource_field) == "suite"
                and suite_config_path
            ):
                return {
                    "file": suite_config_path,
                    "path": f"compile.{resource_field}",
                }
            if from_compile and root_config_path:
                return {
                    "file": root_config_path,
                    "path": f"cfg-dispatch.compile.{resource_field}",
                }
            return {
                "file": suite_config_path,
                "path": f"tests[name={test}].resources.{resource_field}",
            }

        killed_timeout = "TIMEOUT" in agg["states"]
        killed_oom = "OUT_OF_MEMORY" in agg["states"]

        # --- time -----------------------------------------------------
        # Denylist, not allowlist: a VCS -licqueue wait would masquerade as
        # compute time (#329), so only the vcs family loses util-based time
        # advice — Icarus/Questa/cocotb/future backends keep it. An
        # unresolved builder (family None) is not vcs, so it keeps advice.
        time_util_ok = True
        if simulator_family_of is not None and agg.get("builder"):
            time_util_ok = simulator_family_of(agg["builder"]) != "vcs"
        limit = agg.get("timelimit_s")
        elapsed = agg.get("elapsed_s")
        if limit and killed_timeout:
            # A TIMEOUT kill is a fact about the reservation, not a
            # license-contaminated measurement — fires regardless of family.
            findings.append(
                RightsizeFinding(
                    resource="time",
                    reserved=format_time(limit),
                    peak=f">{format_time(limit)}",
                    utilization=1.0,
                    direction="raise",
                    suggested=format_time(limit * rightsize_cfg.margin),
                    edit_hint=hint("time"),
                    **common,
                )
            )
        elif time_util_ok and limit and elapsed is not None:
            util = elapsed / limit
            suggested_s = max(elapsed * rightsize_cfg.margin, _TIME_FLOOR_S)
            time_floored = floor_time_s is not None and suggested_s < floor_time_s
            if time_floored:
                suggested_s = floor_time_s
            if util > rightsize_cfg.near_limit:
                findings.append(
                    RightsizeFinding(
                        resource="time",
                        reserved=format_time(limit),
                        peak=format_time(elapsed),
                        utilization=util,
                        direction="raise",
                        suggested=format_time(suggested_s),
                        edit_hint=hint("time"),
                        **common,
                    )
                )
            elif (
                util < rightsize_cfg.over_threshold
                and suggested_s <= limit * _REDUCE_KEEP_RATIO
            ):
                findings.append(
                    RightsizeFinding(
                        resource="time",
                        reserved=format_time(limit),
                        peak=format_time(elapsed),
                        utilization=util,
                        direction="reduce",
                        suggested=format_time(suggested_s),
                        edit_hint=hint("time", from_compile=time_floored),
                        **common,
                    )
                )

        # --- memory ---------------------------------------------------
        req_mem = agg.get("req_mem_bytes")
        peak_rss = agg.get("max_rss_bytes")
        # `elapsed` is already the peak across this test's runs, so a test
        # is only unsampled when even its longest run finished inside one
        # interval. Judged per test for the same reason utilization is.
        mem_sampled = not (
            accounting_interval_s
            and elapsed is not None
            and elapsed < accounting_interval_s
        )
        if req_mem:
            if killed_oom:
                findings.append(
                    RightsizeFinding(
                        resource="mem",
                        reserved=format_mem(req_mem),
                        peak=f">{format_mem(req_mem)}",
                        utilization=1.0,
                        direction="raise",
                        suggested=format_mem(int(req_mem * rightsize_cfg.margin)),
                        edit_hint=hint("mem"),
                        **common,
                    )
                )
            elif peak_rss and not mem_sampled:
                # Reported only here, where advice really was withheld. An
                # OOM kill above still raises, a test with no reservation or
                # no peak had nothing to advise from anyway, and naming any
                # of those in "memory advice omitted" would contradict the
                # message.
                unsampled.append(test)
            elif peak_rss:
                util = peak_rss / req_mem
                suggested_b = max(
                    int(peak_rss * rightsize_cfg.margin), _MEM_FLOOR_BYTES
                )
                mem_floored = floor_mem_b is not None and suggested_b < floor_mem_b
                if mem_floored:
                    suggested_b = floor_mem_b
                if util > rightsize_cfg.near_limit:
                    findings.append(
                        RightsizeFinding(
                            resource="mem",
                            reserved=format_mem(req_mem),
                            peak=format_mem(peak_rss),
                            utilization=util,
                            direction="raise",
                            suggested=format_mem(suggested_b),
                            edit_hint=hint("mem"),
                            **common,
                        )
                    )
                elif (
                    util < rightsize_cfg.over_threshold
                    and suggested_b <= req_mem * _REDUCE_KEEP_RATIO
                ):
                    findings.append(
                        RightsizeFinding(
                            resource="mem",
                            reserved=format_mem(req_mem),
                            peak=format_mem(peak_rss),
                            utilization=util,
                            direction="reduce",
                            suggested=format_mem(suggested_b),
                            edit_hint=hint("mem", from_compile=mem_floored),
                            **common,
                        )
                    )

        # --- cpus (efficiency; only ever suggests reducing) -----------
        # Use the best per-run efficiency (computed in _aggregate), so a
        # single fully-utilized run vetoes shrinking the reservation.
        # The requested cpus, falling back to the allocation for telemetry
        # that carries no request. Both the ratio and the reported
        # `reserved` are the request: a site allocating whole cores gives a
        # `cpus: 1` test 2, and advising it down to 1 from a `Reserved 2`
        # the tests.yaml never said is advice that can never retire (#505).
        # The head's own resolved reservation comes first — it is the
        # `--cpus-per-task` that was submitted, so it is the request by
        # construction and needs no cooperation from the site.
        alloc_cpus = agg.get("alloc_cpus")
        cpus = agg.get("requested_cpus") or agg.get("req_cpus") or alloc_cpus
        efficiency = agg.get("cpu_efficiency")
        if cpus and cpus > 1 and efficiency is not None:
            if efficiency < rightsize_cfg.over_threshold:
                suggested_cpus = max(
                    1, math.ceil(cpus * efficiency * rightsize_cfg.margin)
                )
                cpus_floored = floor_cpus is not None and suggested_cpus < floor_cpus
                if cpus_floored:
                    suggested_cpus = floor_cpus
                # `< cpus` also drops the case where the floor pushed the
                # suggestion back up to the current reservation: advising a
                # reduction the allocation cannot make is worse than silence,
                # because the agent loop reruns and sees it fail to retire.
                if suggested_cpus < cpus:
                    findings.append(
                        RightsizeFinding(
                            resource="cpus",
                            reserved=str(cpus),
                            # Additive, and only when the two differ: the
                            # reader needs the requested figure to edit and
                            # the allocated one to reconcile with `squeue`.
                            allocated=(
                                str(alloc_cpus)
                                if alloc_cpus and alloc_cpus != cpus
                                else None
                            ),
                            peak=f"{efficiency:.2f} eff",
                            utilization=efficiency,
                            direction="reduce",
                            suggested=str(suggested_cpus),
                            edit_hint=hint("cpus", from_compile=cpus_floored),
                            **common,
                        )
                    )
    if unsampled:
        # A silent gap reads as "nothing to advise", which is the wrong
        # conclusion to leave an agent (or a person) with.
        log_event(
            logger,
            logging.WARNING,
            "rightsize.mem_advice_unsampled",
            suite=suite_display,
            tests=sorted(unsampled),
            interval_s=accounting_interval_s,
        )
    return findings
