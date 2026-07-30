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
"""

import math
from dataclasses import dataclass, field

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
            {"runs": 0, "states": [], "builder": row.get("builder")},
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
):
    """Produce :class:`RightsizeFinding`s for one suite's dispatched rows.

    ``simulator_family_of`` maps a builder name to its simulator family
    (used to suppress time advice off Verilator, see module docstring);
    ``None`` disables that suppression.
    """
    findings = []
    for test, agg in _aggregate(suite_results).items():
        common = {
            "suite": suite_display,
            "test": test,
            "runs": agg["runs"],
            "reg_level": reg_level,
            "states": agg["states"],
        }

        def hint(resource_field):
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
                        edit_hint=hint("time"),
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
                            edit_hint=hint("mem"),
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
                if suggested_cpus < cpus:
                    findings.append(
                        RightsizeFinding(
                            resource="cpus",
                            reserved=str(cpus),
                            peak=f"{efficiency:.2f} eff",
                            utilization=efficiency,
                            direction="reduce",
                            suggested=str(suggested_cpus),
                            edit_hint=hint("cpus"),
                            **common,
                        )
                    )
    return findings
