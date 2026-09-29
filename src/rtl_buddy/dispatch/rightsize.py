# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Reservation right-sizing: per-test advice from the per-job ``sacct`` telemetry.

Each finding says which resource is over- or under-reserved, by how much, and
what to set it to, with an ``edit_hint`` naming the field to change. rtl-buddy
never rewrites tests.yaml.

- Utilization is judged per test: the peak across its run_ids in this invocation.
- Below ``over-threshold`` is over-reserved (``reduce``). Above ``near-limit``,
  or after a scheduler TIMEOUT / OUT_OF_MEMORY kill, is under-reserved (``raise``);
  a kill takes precedence over measured numbers. Suggested = peak x ``margin``,
  rounded with floors.
- Time advice is skipped for VCS builders (a ``-licqueue`` wait looks like compute
  time) and when the time limit is unknown.
- Memory advice is skipped for a test whose longest run finished inside one
  accounting interval, because ``MaxRSS`` is a sampled high-water mark and
  under-reports such jobs. The omission is logged. An OOM kill still raises.
- Cpu efficiency is measured against the requested cpus, in preference order: the
  reservation rtl-buddy submitted, ``ReqCPUS``, ``AllocCPUS``. A cpu
  request in ``cfg-dispatch.sbatch-args`` overrides the submitted one. A finding's
  ``reserved`` is the request and ``allocated`` the scheduler's figure when it differs.
- Advice carries the run count and regression level it came from.
- Advice names the field that governs the reservation. For a job that compiles
  inside itself, the allocation is the per-field maximum of the sim and compile
  reservations; where compile won, the hint points at ``cfg-dispatch.compile``
  and the phase is ``compile+sim``.
- The suite's build job gets its own row (``compile``), analysed by
  :func:`analyze_build_reservation`. Its cpus suggestion is divided by
  ``compile.parallel``, and a ``reduce`` requires evidence that a compile ran.
- A suggestion for an in-job compile is clamped to the compile reservation; one
  the clamp turns into no reduction is dropped.
"""

import logging
import math
from dataclasses import dataclass, field

from ..config.dispatch import (
    compile_parallel_origin,
    format_mem,
    format_time,
    greedy_schedule,
    mem_to_bytes,
    sbatch_arg_sets_cpu_count_directly,
    time_to_seconds,
)
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

_TIME_FLOOR_S = 300  # never suggest a limit under 5 minutes
_MEM_FLOOR_BYTES = 128 * 2**20  # never suggest under 128M
# A reduce must save at least a quarter of the reservation.
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
    # "sim", or "compile+sim" when the compile ran inside the job.
    phase: str = "sim"
    # The scheduler's allocation when it differs from `reserved`; cpus findings only.
    allocated: str | None = None
    # Key that sets this build-job row's `compile.parallel`, for the table footnote.
    # Not in `as_event()`: the machine payload's key set is a contract.
    parallel_origin: str | None = None
    # For an aggregated reservation, `suggested` is one contributor's new value;
    # these give the whole-job figure and the amount added to that contributor.
    suggested_total: str | None = None
    aggregate_delta: str | None = None

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
            "suggested_total": self.suggested_total,
            "aggregate_delta": self.aggregate_delta,
        }


def _is_arg_override(entry: str) -> bool:
    """Did this override come from ``sbatch-args`` rather than the environment?

    Arguments keep their leading dash; environment entries are ``NAME=value``.
    """
    return entry.startswith("-")


def _replaces_the_per_task_cpus(entries: list) -> bool:
    """Does this override replace the generated ``--cpus-per-task``?

    Only a direct cpu count (``-c``/``--cpus-per-task``) does. A task or node
    count multiplies the generated value instead, so the per-task field still
    applies. No environment entry counts.
    """
    return any(
        _is_arg_override(e) and sbatch_arg_sets_cpu_count_directly(e) for e in entries
    )


def _effective_cpus_floor(floor_cpus, cpus_override):
    """The cpus floor a ``reduce`` may not suggest below, or ``None`` if none applies.

    ``floor_cpus`` is per task. A direct cpu count in ``sbatch-args`` replaces
    the generated flag, so the floor is dropped. A task or node count does not,
    so the floor is kept, unscaled: task count is itself a lever, and scaling
    would suppress every reachable reduction.
    """
    if not floor_cpus or not cpus_override:
        return floor_cpus
    return None if _replaces_the_per_task_cpus(cpus_override) else floor_cpus


def _override_source(entries: list) -> str:
    """Where a reader has to go to change the request."""
    from_args = any(_is_arg_override(e) for e in entries)
    from_env = any(not _is_arg_override(e) for e in entries)
    if from_args and from_env:
        return "sbatch-args and the environment"
    return "sbatch-args" if from_args else "the environment"


def _join_args(quoted: list) -> str:
    """``A``, ``B`` and ``C``; a list, not a product.

    The note names the arguments and leaves the combining rule to sbatch.
    """
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def _override_note(
    sbatch_args: list, masked_path: str, *, per_task=None, tasks=None
) -> str:
    """Why a cpus finding names ``sbatch-args`` instead of a YAML field.

    ``sbatch-args`` is appended after the generated flags, so a cpu request
    there decides the job's request and a hint naming the YAML field would
    not retire. Only a single ``-c``/``--cpus-per-task`` can take the suggested
    number. A task or node count is not a cpu count, and several arguments
    combine by sbatch's precedence, so the note hands the decomposition back
    to the reader.
    """
    quoted = [f"`{arg}`" for arg in sbatch_args]
    source = _override_source(sbatch_args)
    if not _replaces_the_per_task_cpus(sbatch_args):
        # Nothing is superseded: the per-task field still applies, so do not say "supersedes".
        verb = "multiplies" if len(quoted) == 1 else "multiply"
        decomposition = (
            f", so the request is {per_task} per task x {tasks} tasks"
            if per_task and tasks
            else ""
        )
        return (
            f"{_join_args(quoted) if len(quoted) > 1 else quoted[0]} "
            f"{verb} this job's cpu request: the generated "
            f"--cpus-per-task from {masked_path} still applies"
            f"{decomposition}. Suggested value is the whole-job cpu count "
            f"— lower {masked_path}, the task count in {source}, or both; "
            "no single one of them takes it."
        )
    if len(quoted) == 1:
        return (
            f"sbatch-args {quoted[0]} sets this job's cpu request, "
            f"superseding {masked_path}; change it there. Suggested value "
            "is the whole-job cpu count."
        )
    return (
        f"{source} supersedes {masked_path}: {_join_args(quoted)} set "
        "this job's cpu request together. Suggested value is the whole-job "
        "cpu count — decompose it across them per sbatch's own "
        "precedence; no single one of them takes it."
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
                # Set for a job that compiles inside itself; governed_by names the layer per field.
                "compile_in_job": bool(row.get("compile_in_job")),
                "governed_by": row.get("governed_by") or {},
                "compile_floor": row.get("compile_floor") or {},
                # Per-row compile attribution: the tests.yaml layer and testbench per field.
                "compile_origins": row.get("compile_origins"),
                "compile_testbench": row.get("compile_testbench"),
                # The `--cpus-per-task` the head submitted; it beats scheduler-reported figures.
                "requested_cpus": row.get("requested_cpus"),
                # `sbatch-args` cpu-request entries that superseded it, if any.
                "cpus_override": row.get("cpus_override"),
                # Still in force under a task-count override; the compile floor bounds it.
                "submitted_cpus_per_task": row.get("submitted_cpus_per_task"),
                # Sim fields governed by the builder mode; hints must name the `modes:` key.
                "resource_modes": row.get("resource_modes") or {},
                # The three cpus-request fields come from the first row with telemetry.
                # True when a later row disagrees (e.g. a retry submitted under a
                # changed environment); the cpus row is then withheld.
                "cpus_request_mixed": False,
            },
        )
        request_key = (
            row.get("requested_cpus"),
            tuple(row.get("cpus_override") or ()),
            row.get("submitted_cpus_per_task"),
        )
        if agg.setdefault("_cpus_request_key", request_key) != request_key:
            agg["cpus_request_mixed"] = True
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
        # A ratio: compute per run and keep the best, so one saturated run vetoes a reduce.
        # Denominator preference: submitted request, scheduler ReqCPUS, AllocCPUS.
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


# Row labels for the build job and the verilate half of a split compile; the
# parentheses keep them from colliding with test names.
BUILD_JOB_ROW = "(build job)"
VERILATE_JOB_ROW = "(verilate job)"


def _compile_origin(origins, field):
    """``(origin, testbench, key)`` for one compile field, or ``(None, None, None)`` if unset.

    ``compile_origins`` is either a flat ``{field: "suite"|"testbench"}`` map (per-test
    rows) or a nested ``{field: {"origin", "testbench", "key"}}`` map (build job).
    ``key`` is the dotted key inside the ``compile:`` block, such as
    ``verilate.mem``; ``None`` means the field's own name.
    """
    value = (origins or {}).get(field)
    if isinstance(value, dict):
        return value.get("origin"), value.get("testbench"), value.get("key")
    return value, None, None


def _compile_key(origins, field):
    """The dotted key inside a ``compile:`` block that holds ``field``."""
    return _compile_origin(origins, field)[2] or field


def _compile_edit_path(origins, field, *, testbench=None):
    """The tests.yaml key holding the winning compile value, or ``None``.

    ``None`` means no tests.yaml layer set the field, so the hint belongs in
    root_config.yaml. ``testbench`` names the testbench when the flat map
    records only that a testbench block won.
    """
    origin, governing_tb, key = _compile_origin(origins, field)
    governing_tb = governing_tb or testbench
    key = key or field
    if origin == "testbench" and governing_tb:
        return f"testbenches[name={governing_tb}].compile.{key}"
    if origin in ("suite", "testbench"):
        # Defensive: a testbench origin without a testbench name still lands in the right file.
        return f"compile.{key}"
    return None


def _compile_paths(
    origins, field, key, *, suite_config_hint=None, root_config_hint=None
):
    """Render a provenance list of one field as ``file:key`` paths.

    ``key`` is ``sources`` (places that independently produce the winning
    number, so lowering one alone moves nothing) or ``contributors`` (builds
    whose values are added). Returns ``[]`` if the map has no such list.
    """
    entry = (origins or {}).get(field)
    if not isinstance(entry, dict):
        return []
    paths = []
    for source in entry.get(key) or []:
        path = _compile_edit_path(
            {field: source}, field, testbench=source.get("testbench")
        )
        # A tie can straddle tests.yaml and root_config.yaml, so name the file too.
        if path is None:
            path = f"cfg-dispatch.compile.{_compile_key({field: source}, field)}"
            config_file = root_config_hint
        else:
            config_file = suite_config_hint
        if config_file:
            path = f"{config_file}:{path}"
        if path not in paths:
            paths.append(path)
    return paths


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
    sbatch_args_config_path=None,
    phase="compile",
):
    """Right-size the build job's own reservation; returns a list of findings.

    The build job is one allocation running up to ``parallel`` concurrent
    Verilations and has no per-test row. There is no memory advice (``MaxRSS``
    under-reports short jobs) and no cpus ``raise``.

    - ``compile_resources`` is the per-build reservation; the submitted cpus were
      multiplied by ``parallel``. Cpus advice is offered only when the effective
      parallel is 1, since above that the efficiency ratio also carries tail
      effects (reason ``parallel-utilization-ambiguous``).
    - ``cpus_override`` is the list of ``sbatch-args`` cpu-request entries, which
      replace the resolved value; the cpus hint then names ``cfg-dispatch.sbatch-args``
      in ``sbatch_args_config_path`` (default ``root_config_hint``).
    - ``compile_work`` is ``{"records", "compiled", "compiled_sec"}`` from the build
      envelope, or ``None``. A ``reduce`` requires ``compiled`` > 0, because a
      re-run of an unchanged suite finishes in seconds on stamps. ``raise`` does not.
      ``accounting_interval_s`` also withholds ``reduce`` for a job shorter than
      one sample.
    - ``compile_origins`` and ``suite_config_hint`` (the suite's tests.yaml) decide
      which file an edit hint names, from
      :func:`~rtl_buddy.config.dispatch.compile_resource_origins`.
    - ``phase`` is ``"compile"`` or ``"verilate"`` (the first half of a split
      compile); it selects the row label and travels on every finding.
    """
    if not build_telemetry:
        return []
    findings = []
    elapsed = build_telemetry.get("elapsed_s")
    compiled = (compile_work or {}).get("compiled") or 0
    # Zero records means unknown (no envelope), which must not be logged as "all reused".
    records = (compile_work or {}).get("records") or 0
    undersampled = (
        accounting_interval_s is not None
        and elapsed is not None
        and elapsed < accounting_interval_s
    )
    may_reduce = bool(compiled) and not undersampled
    if not may_reduce:
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
            builds=(compile_work or {}).get("records"),
            compiled=compiled,
            compiled_sec=(compile_work or {}).get("compiled_sec"),
            elapsed_s=elapsed,
            interval_s=accounting_interval_s,
        )
    # Clamped because every cpus number below divides by it.
    parallel = max(1, parallel)
    state = build_telemetry.get("state")
    states = [state] if state else []

    origins = compile_origins or {}
    # A suite's own `compile:` block wins, so name that key rather than the root one.
    parallel_key = compile_parallel_origin(
        origins.get("parallel") == "suite", suite_config_hint
    )

    alloc_cpus = build_telemetry.get("alloc_cpus")
    # The generated `--cpus-per-task`: per-build cpus x parallel.
    generated_per_task = (
        (compile_resources.cpus or 0) * parallel if compile_resources is not None else 0
    )
    # Preference: submitted value, ReqCPUS, AllocCPUS. Any cpu override makes the
    # generated value not the whole-job request, so it is skipped.
    submitted = 0 if cpus_override else generated_per_task
    cpus = submitted or build_telemetry.get("req_cpus") or alloc_cpus
    build_tasks = (
        cpus // generated_per_task
        if generated_per_task
        and cpus
        and cpus % generated_per_task == 0
        and cpus != generated_per_task
        else None
    )

    def hint(resource_field, note=None):
        # A cpu override in `sbatch-args` masks every cpus field named below.
        if resource_field == "cpus" and cpus_override:
            compile_path = _compile_edit_path(origins, "cpus")
            masked = (
                compile_path
                if compile_path and suite_config_hint
                else f"cfg-dispatch.compile.{_compile_key(origins, 'cpus')}"
            )
            # An environment variable has no file; `sbatch-args` beats it when both are set.
            from_args = any(_is_arg_override(e) for e in cpus_override)
            edit = {
                "path": "cfg-dispatch.sbatch-args" if from_args else "env",
                "note": _override_note(
                    cpus_override,
                    masked,
                    per_task=generated_per_task or None,
                    tasks=build_tasks,
                ),
            }
            # The backend's config, which in a multi-root regression is not this suite's root.
            override_file = sbatch_args_config_path or root_config_hint
            if from_args and override_file:
                edit["file"] = override_file
            return edit
        # Name the file and key that hold the winning value: a testbench block beats
        # the suite block, which beats cfg-dispatch (root_config.yaml).
        compile_path = _compile_edit_path(origins, resource_field)
        if compile_path and suite_config_hint:
            edit = {"file": suite_config_hint, "path": compile_path}
        else:
            edit = {
                "path": f"cfg-dispatch.compile.{_compile_key(origins, resource_field)}"
            }
            if root_config_hint:
                edit["file"] = root_config_hint
        if note:
            edit["note"] = note
        return edit

    common = {
        "suite": suite_display,
        "test": VERILATE_JOB_ROW if phase == "verilate" else BUILD_JOB_ROW,
        "runs": 1,
        "reg_level": None,
        "states": states,
        "phase": phase,
        "parallel_origin": parallel_key,
    }

    def withheld_from_reduce(resource_field):
        """True (and logs the reason) when a `reduce` cannot be written into one config value.

        Reasons: ``compile-aggregate`` (the reservation is a sum of several builds) and
        ``compile-origin-tied`` (several sources produce the same number
        independently). A `raise` is unaffected.
        """
        entry = origins.get(resource_field)
        if not isinstance(entry, dict):
            return False
        # Count list entries, not paths: two builds can share one YAML key and still add up.
        for reason, key in (
            ("compile-aggregate", "contributors"),
            ("compile-origin-tied", "sources"),
        ):
            if len(entry.get(key) or []) <= 1:
                continue
            paths = _compile_paths(
                origins,
                resource_field,
                key,
                suite_config_hint=suite_config_hint,
                root_config_hint=root_config_hint,
            )
            log_event(
                logger,
                logging.INFO,
                "rightsize.build_advice_withheld",
                suite=suite_display,
                reason=reason,
                resource=resource_field,
                paths=paths,
                builds=(compile_work or {}).get("records"),
                compiled=compiled,
                compiled_sec=(compile_work or {}).get("compiled_sec"),
                elapsed_s=elapsed,
                interval_s=accounting_interval_s,
            )
            return True
        return False

    def _rescheduled_total(entry, value):
        """The makespan this field's queue reaches with the key set to ``value``.

        ``None`` when the provenance map has no schedule (e.g. ``mem``, which adds up).
        """
        schedule = entry.get("schedule")
        if not schedule:
            return None
        governing = entry.get("testbench")
        makespan, _, _ = greedy_schedule(
            [value if name == governing else seconds for name, seconds in schedule],
            entry.get("parallel") or 1,
        )
        return makespan

    def raise_fields(resource_field, suggested_value, current_value, render):
        """Turn a whole-job `raise` into the contributor's own new value.

        Writing the whole-job figure into one contributor of a summed reservation
        overshoots, so the key is instead raised by the shortfall, split across
        its occurrences and checked by re-scheduling the queue. Falls back to the
        whole-job figure for non-aggregates and when no schedule reaches the target.
        """
        plain = {"suggested": render(suggested_value)}
        entry = origins.get(resource_field)
        if not isinstance(entry, dict) or not entry.get("aggregated"):
            return plain
        own = entry.get("contributor_value")
        if own is None or not current_value:
            return plain
        delta = suggested_value - current_value
        if delta <= 0:
            return plain
        # One key can be several contributors; each takes its share, rounded up.
        primary = {"origin": entry.get("origin"), "testbench": entry.get("testbench")}
        occurrences = sum(
            1 for source in entry.get("contributors") or [] if source == primary
        )
        candidate = own + -(-delta // max(1, occurrences))

        # Re-schedule the proposal: raising one copy can reorder the queue and absorb
        # part of the raise. An under-delivering raise would time out again.
        reached = _rescheduled_total(entry, candidate)
        rounds = 0
        while reached is not None and reached < suggested_value and rounds < 8:
            candidate += suggested_value - reached
            reached = _rescheduled_total(entry, candidate)
            rounds += 1
        if reached is not None and reached < suggested_value:
            # The whole-job figure over-reserves but cannot under-deliver.
            return plain
        return {
            "suggested": render(candidate),
            "suggested_total": render(suggested_value),
            "aggregate_delta": f"+{render(candidate - own)}",
        }

    def with_aggregate(edit, override):
        """Add a hint note explaining an aggregate-translated suggestion."""
        if not override.get("suggested_total"):
            return edit
        edit = dict(edit)
        note = (
            f"the build job reserves {override['suggested_total']} in total "
            f"across its builds; this value is that key's own "
            f"{override['aggregate_delta']}, so the total reaches it with "
            "the other builds unchanged"
        )
        edit["note"] = f"{edit['note']} {note}" if edit.get("note") else note
        return edit

    # --- time -------------------------------------------------------
    limit = build_telemetry.get("timelimit_s")
    if limit and state == "TIMEOUT":
        target = limit * rightsize_cfg.margin
        raised = raise_fields("time", target, limit, format_time)
        findings.append(
            RightsizeFinding(
                resource="time",
                reserved=format_time(limit),
                peak=f">{format_time(limit)}",
                utilization=1.0,
                direction="raise",
                edit_hint=with_aggregate(hint("time"), raised),
                **common,
                **raised,
            )
        )
    elif limit and elapsed is not None:
        util = elapsed / limit
        # `time` is not scaled by parallel: concurrent builds take the longest one's wall clock.
        suggested_s = max(elapsed * rightsize_cfg.margin, _TIME_FLOOR_S)
        if util > rightsize_cfg.near_limit:
            raised = raise_fields("time", suggested_s, limit, format_time)
            findings.append(
                RightsizeFinding(
                    resource="time",
                    reserved=format_time(limit),
                    peak=format_time(elapsed),
                    utilization=util,
                    direction="raise",
                    edit_hint=with_aggregate(hint("time"), raised),
                    **common,
                    **raised,
                )
            )
        elif (
            may_reduce
            and util < rightsize_cfg.over_threshold
            and suggested_s <= limit * _REDUCE_KEEP_RATIO
            and not withheld_from_reduce("time")
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
    cpu_time = build_telemetry.get("total_cpu_s")
    # Whole-job efficiency is per-build only for one build at a time; above that it
    # includes tail idling and there is no per-compile cpu telemetry, so withhold.
    effective_parallel = min(parallel, records) if records else parallel
    if may_reduce and cpus and cpus > 1 and cpu_time is not None and elapsed:
        efficiency = cpu_time / (elapsed * cpus)
        if efficiency < rightsize_cfg.over_threshold and effective_parallel > 1:
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
                parallel_origin=parallel_key,
            )
        elif efficiency < rightsize_cfg.over_threshold and withheld_from_reduce("cpus"):
            # No single edit lowers the maximum; withheld_from_reduce has logged it.
            pass
        elif efficiency < rightsize_cfg.over_threshold:
            suggested_total = max(
                1, math.ceil(cpus * efficiency * rightsize_cfg.margin)
            )
            # `effective_parallel` is 1 here; the division states the invariant.
            suggested_per_build = max(
                1, math.ceil(suggested_total / effective_parallel)
            )
            # Prefer the head's resolved per-build cpus over AllocCPUS, which can be
            # rounded up by the site and would state a number the YAML never held.
            resolved_per_build = (
                getattr(compile_resources, "cpus", None)
                if compile_resources is not None and not cpus_override
                else None
            )
            per_build_now = resolved_per_build or math.ceil(cpus / parallel)
            requested_total = per_build_now * parallel
            # Give the per-build figure to edit and the allocation to reconcile with sacct.
            alloc_clause = (
                f" (the scheduler reported {alloc_cpus} allocated)"
                if alloc_cpus and alloc_cpus != requested_total
                else ""
            )
            if requested_total == cpus and parallel == 1:
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
            # Only for parallel > 1 with an effective parallel of 1: slots reserved for
            # builds the plan never produced.
            lever = (
                ""
                if parallel == 1
                else (
                    " `parallel` is the other lever: it is capped by the "
                    "suite's planned configs, not by its distinct compile "
                    "keys, so configs that share one key reserve cpus for "
                    f"builds that never run — lower {parallel_key} "
                    "instead when the key count is the smaller number."
                )
            )
            if suggested_per_build < per_build_now:
                findings.append(
                    RightsizeFinding(
                        resource="cpus",
                        reserved=str(cpus),
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
    sbatch_args_config_path=None,
):
    """Produce :class:`RightsizeFinding`s for one suite's dispatched rows.

    - ``simulator_family_of`` maps a builder name to its simulator family, to
      suppress time advice for VCS; ``None`` disables that.
    - ``root_config_path`` is where ``cfg-dispatch`` lives, used for
      ``cfg-dispatch.compile`` hints; without it those fall back to the per-test hint.
    - ``compile_origins`` is the suite-wide fallback for which compile fields the
      suite's ``compile:`` block won, which are then named in its tests.yaml. A
      row's own ``compile_origins`` (in-job compiles) takes precedence and can name
      ``testbenches[name=...].compile.<field>``.
    - ``accounting_interval_s`` is the scheduler's usage-sampling interval, used
      to suppress memory advice from an unsampled peak; ``None`` disables that.
    - ``sbatch_args_config_path`` is the config the backend's ``sbatch-args``
      came from, used as the ``file`` of a cpu-override hint; it differs from
      ``root_config_path`` in a multi-root regression. Falls back to ``root_config_path``.
    """
    findings = []
    unsampled = []
    origins = compile_origins or {}
    for test, agg in _aggregate(suite_results).items():
        governed_by = agg["governed_by"]
        # The row's own compile attribution, else the suite-wide map.
        row_origins = agg.get("compile_origins") or origins
        row_testbench = agg.get("compile_testbench")
        # An in-job compile's allocation is max(sim, compile); suggestions clamp to these floors.
        floor = agg["compile_floor"]
        floor_mem_b = mem_to_bytes(floor.get("mem"))
        floor_time_s = time_to_seconds(floor.get("time"))
        cpus_override = agg.get("cpus_override") or []
        # The cpus floor is per task; the effective floor depends on the override kind.
        floor_cpus_per_task = floor.get("cpus")
        # Resolved before `hint` so the override note can say "N per task x M tasks".
        # `tasks` is observed (request / submitted per-task flag), not derived from sbatch rules.
        alloc_cpus = agg.get("alloc_cpus")
        cpus = agg.get("requested_cpus") or agg.get("req_cpus") or alloc_cpus
        per_task = agg.get("submitted_cpus_per_task")
        tasks = (
            cpus // per_task
            if per_task and cpus and cpus % per_task == 0 and cpus != per_task
            else None
        )
        floor_cpus = _effective_cpus_floor(floor_cpus_per_task, cpus_override)
        common = {
            "suite": suite_display,
            "test": test,
            "runs": agg["runs"],
            "reg_level": reg_level,
            "states": agg["states"],
            "phase": "compile+sim" if agg["compile_in_job"] else "sim",
        }

        # The `resources:` key for a field: its own name, or `modes.<mode>.<field>`.
        row_modes = agg.get("resource_modes") or {}

        def _resources_key(field):
            mode = row_modes.get(field)
            return f"modes.{mode}.{field}" if mode else field

        # The YAML field that a cpu override masks, named in the note.
        compile_cpus_path = _compile_edit_path(
            row_origins, "cpus", testbench=row_testbench
        )
        if governed_by.get("cpus") != "compile":
            masked_cpus_path = f"tests[name={test}].resources.{_resources_key('cpus')}"
        elif compile_cpus_path and suite_config_path:
            masked_cpus_path = compile_cpus_path
        else:
            masked_cpus_path = "cfg-dispatch.compile.cpus"

        def hint(
            resource_field,
            *,
            from_compile=False,
            _governed_by=governed_by,
            _cpus_override=cpus_override,
            _per_task=per_task,
            _tasks=tasks,
            _origins=row_origins,
            _testbench=row_testbench,
            # Bound at definition so a hint rendered later uses this row's key.
            _resources_key=_resources_key,
        ):
            # A cpu override in `sbatch-args` masks every cpus field in the YAML.
            if resource_field == "cpus" and _cpus_override:
                # An environment variable has no file, so name the environment.
                from_args = any(_is_arg_override(e) for e in _cpus_override)
                edit = {
                    "path": "cfg-dispatch.sbatch-args" if from_args else "env",
                    "note": _override_note(
                        _cpus_override,
                        masked_cpus_path,
                        per_task=_per_task,
                        tasks=_tasks,
                    ),
                }
                # The backend's config, which differs from this suite's root in a multi-root regression.
                override_file = sbatch_args_config_path or root_config_path
                if from_args and override_file:
                    edit["file"] = override_file
                return edit
            # A field the compile reservation won is masked by the max.
            from_compile = from_compile or _governed_by.get(resource_field) == "compile"
            # Edit at the layer that won: testbench block, suite block, then cfg-dispatch.
            compile_path = (
                _compile_edit_path(_origins, resource_field, testbench=_testbench)
                if from_compile
                else None
            )
            if compile_path and suite_config_path:
                return {
                    "file": suite_config_path,
                    "path": compile_path,
                }
            if from_compile and root_config_path:
                return {
                    "file": root_config_path,
                    "path": f"cfg-dispatch.compile.{resource_field}",
                }
            return {
                "file": suite_config_path,
                "path": (
                    f"tests[name={test}].resources.{_resources_key(resource_field)}"
                ),
            }

        killed_timeout = "TIMEOUT" in agg["states"]
        killed_oom = "OUT_OF_MEMORY" in agg["states"]

        # --- time -----------------------------------------------------
        # Denylist: only VCS loses utilization-based time advice (-licqueue waits).
        time_util_ok = True
        if simulator_family_of is not None and agg.get("builder"):
            time_util_ok = simulator_family_of(agg["builder"]) != "vcs"
        limit = agg.get("timelimit_s")
        elapsed = agg.get("elapsed_s")
        if limit and killed_timeout:
            # A TIMEOUT kill holds for every simulator family.
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
        # `elapsed` is the peak over runs: unsampled only if even the longest run was short.
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
                # Listed only where advice was really withheld.
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
        # Best per-run efficiency from _aggregate; the ratio and `reserved` use the request.
        efficiency = agg.get("cpu_efficiency")
        if cpus and cpus > 1 and efficiency is not None:
            if efficiency < rightsize_cfg.over_threshold and agg["cpus_request_mixed"]:
                # Runs had different cpu requests, so one `reserved` and `edit_hint` cannot fit.
                log_event(
                    logger,
                    logging.INFO,
                    "rightsize.cpus_advice_withheld",
                    suite=suite_display,
                    test=test,
                    reason="mixed-cpu-requests",
                    runs=agg["runs"],
                )
            elif efficiency < rightsize_cfg.over_threshold:
                suggested_cpus = max(
                    1, math.ceil(cpus * efficiency * rightsize_cfg.margin)
                )
                cpus_floored = floor_cpus is not None and suggested_cpus < floor_cpus
                if cpus_floored:
                    suggested_cpus = floor_cpus
                # Also drops a suggestion the floor pushed back up to the current value.
                if suggested_cpus < cpus:
                    findings.append(
                        RightsizeFinding(
                            resource="cpus",
                            reserved=str(cpus),
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
        # Otherwise the gap reads as "nothing to advise".
        log_event(
            logger,
            logging.WARNING,
            "rightsize.mem_advice_unsampled",
            suite=suite_display,
            tests=sorted(unsampled),
            interval_s=accounting_interval_s,
        )
    return findings
