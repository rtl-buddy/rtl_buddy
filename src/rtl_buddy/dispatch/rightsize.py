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
- Every suggestion for such a job is *reachable*: a ``reduce`` is clamped
  up to the compile reservation, because the ``max`` will not let the
  allocation go below it however far the test's own ``resources:`` are
  trimmed. A suggestion the clamp pushes back to the current reservation
  saves nothing and is dropped — advising a reduction that cannot happen
  is worse than silence, since the agent loop reruns, sees the advice fail
  to retire, and cannot tell that from a wrong suggestion.
"""

import math
from dataclasses import dataclass, field

from ..config.dispatch import mem_to_bytes, time_to_seconds

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
        cpus = telemetry.get("alloc_cpus")
        cpu_time = telemetry.get("total_cpu_s")
        elapsed = telemetry.get("elapsed_s")
        if cpus and cpu_time is not None and elapsed:
            eff = cpu_time / (elapsed * cpus)
            agg["cpu_efficiency"] = max(agg.get("cpu_efficiency", 0.0), eff)
    return per_test


def analyze_suite_reservations(
    suite_results,
    *,
    suite_display,
    suite_config_path,
    rightsize_cfg,
    reg_level=None,
    simulator_family_of=None,
    root_config_path=None,
):
    """Produce :class:`RightsizeFinding`s for one suite's dispatched rows.

    ``simulator_family_of`` maps a builder name to its simulator family
    (used to suppress time advice off Verilator, see module docstring);
    ``None`` disables that suppression. ``root_config_path`` is where
    ``cfg-dispatch`` lives, needed to hint at ``cfg-dispatch.compile`` for a
    field the compile reservation governs (#358); without it those findings
    fall back to the per-test hint.
    """
    findings = []
    for test, agg in _aggregate(suite_results).items():
        governed_by = agg["governed_by"]
        # An in-job compile's allocation is max(sim, compile), so no `reduce`
        # can take it below the compile side however far the test's own
        # resources: are trimmed. These are the floors each suggestion is
        # clamped to; None where there is nothing to clamp against.
        floor = agg["compile_floor"]
        floor_cpus = floor.get("cpus")
        floor_mem_b = mem_to_bytes(floor.get("mem"))
        floor_time_s = time_to_seconds(floor.get("time"))
        common = {
            "suite": suite_display,
            "test": test,
            "runs": agg["runs"],
            "reg_level": reg_level,
            "states": agg["states"],
            "phase": "compile+sim" if agg["compile_in_job"] else "sim",
        }

        def hint(resource_field, *, from_compile=False, _governed_by=governed_by):
            # A field the compile reservation won is masked by the max, so
            # editing the test's resources: would not move the allocation.
            from_compile = from_compile or _governed_by.get(resource_field) == "compile"
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
        cpus = agg.get("alloc_cpus")
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
                            peak=f"{efficiency:.2f} eff",
                            utilization=efficiency,
                            direction="reduce",
                            suggested=str(suggested_cpus),
                            edit_hint=hint("cpus", from_compile=cpus_floored),
                            **common,
                        )
                    )
    return findings
