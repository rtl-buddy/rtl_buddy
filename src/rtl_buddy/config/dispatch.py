# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Dispatch (remote test execution) configuration: the ``cfg-dispatch`` section of root_config.yaml and the reservation blocks in tests.yaml.

Reservations resolve field by field (test, testbench, ``cfg-dispatch``), then the ``modes:`` block for the run's builder mode is layered on top.
See docs/concepts/dispatch.md.
"""

import dataclasses
import difflib
import logging
import math
import os
import re
from dataclasses import dataclass

from serde import field, serde

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

# Every dispatched job gets an explicit --time: right-sizing computes Elapsed/Timelimit, which is undefined on partitions defaulting to UNLIMITED.
DEFAULT_JOB_TIME = "01:00:00"
DEFAULT_JOB_CPUS = 1

# Verilation is single-threaded: one core for the compiler, one for I/O and child processes. `compile.cpus` sizes the C++ build.
DEFAULT_VERILATE_CPUS = 2

# `warn` names orphaned jobs and submits a fresh fleet beside them. `cancel` and `adopt` act on jobs the user has not inspected, so they are opt-in.
ORPHANS_POLICIES = ("warn", "cancel", "adopt")
ORPHANS_DEFAULT = "warn"

# Slurm --time spellings passed through verbatim.
_TIME_RE = re.compile(r"^\d+(-\d{1,2}(:\d{2}){0,2}|(:\d{2}){1,2})?$")


@serde
class DispatchResourcesFile:
    """Per-job resource reservation fields; ``None`` means inherit.

    ``time`` and ``mem`` accept ``int`` so that YAML 1.1's sexagesimal reading of an unquoted ``4:00:00`` (integer 14400) is rejected at validation instead of reaching Slurm.
    ``modes`` is the raw per-builder-mode override mapping, validated by :func:`validate_modes_block` and read by :func:`mode_override`.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    modes: dict | None = None


@serde
class CompileVerilateFile:
    """``compile.verilate``: the verilate job's own reservation.

    Under Slurm the per-suite compile is a single-threaded verilate job chained to a build job.
    ``cpus`` defaults to :data:`DEFAULT_VERILATE_CPUS`; ``mem`` and ``time`` inherit the resolved compile values.
    ``modes`` is accepted only so it can be refused: write the per-mode override as ``compile.modes.<mode>.verilate``.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    modes: dict | None = None


@serde
class DispatchCompileFile:
    """``cfg-dispatch.compile``: the compile's reservation and its concurrency.

    Separate from :class:`DispatchResourcesFile` because ``parallel`` belongs to the per-suite build job, not to a per-test reservation. An unknown key such as ``parallel`` in a per-test ``resources:`` block is dropped by serde; :func:`warn_unknown_block_keys` warns about it.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # Per-builder-mode overrides layered over the resolved compile value. A mode block may carry `verilate:` but not `parallel` or `split-verilate`.
    modes: dict | None = None
    # Distinct builds the build job compiles concurrently; 1 is serial.
    parallel: int = 1
    # The verilate job's reservation when the compile is split.
    verilate: CompileVerilateFile | None = None
    # True chains a verilate job and a build job with `afterok`; False keeps one `verilator --binary` job. Inert off Slurm.
    split_verilate: bool = field(rename="split-verilate", default=True)


def _validate_time(value):
    """Validate a ``--time`` value and reject the sexagesimal trap."""
    if value is None:
        return None
    if isinstance(value, int):
        raise FatalRtlBuddyError(
            f"dispatch resources: time {value!r} parsed as an integer — an "
            "unquoted HH:MM:SS is read by YAML as sexagesimal (4:00:00 -> "
            '14400). Quote it: time: "4:00:00" (or write bare minutes as a '
            "string)."
        )
    if not _TIME_RE.match(value):
        raise FatalRtlBuddyError(
            f'dispatch resources: time "{value}" is not a valid Slurm time '
            "(expected minutes, MM:SS, HH:MM:SS, or DD-HH[:MM[:SS]])."
        )
    return value


def _validate_mem(value):
    if value is None:
        return None
    return str(value)


# Keys a `modes.<mode>` block may carry. Job-wide keys and a nested `modes:` are rejected because a dropped reservation goes unnoticed until an OOM kill.
_MODE_FIELDS = ("cpus", "mem", "time")
_MODE_JOB_WIDE_KEYS = ("parallel", "split-verilate", "split_verilate")


@dataclass
class ModeOverride:
    """One validated ``modes.<mode>`` block, shaped like the reservation blocks it overrides.

    ``None`` in a field means the mode said nothing and the base value stands.
    """

    cpus: int | None = None
    mem: str | None = None
    time: str | None = None
    verilate: CompileVerilateFile | None = None


def _validate_mode_entry(block, *, where, compile_block, allow_verilate):
    """Validate one ``modes.<mode>`` block; return a normalised dict.

    Keys are strict, unlike the base reservation fields, because a typo would silently keep the base figure.
    The result is stored back on its owner and travels through the JSON dispatch plan manifest.
    """
    if not isinstance(block, dict):
        allowed = ", ".join(_MODE_FIELDS + (("verilate",) if allow_verilate else ()))
        raise FatalRtlBuddyError(
            f"{where} must be a mapping of reservation fields ({allowed}); got "
            f"{block!r}. Write it as {where.rsplit('.', 1)[-1]}: "
            '{mem: 16G, time: "00:30:00"}.'
        )
    allowed = set(_MODE_FIELDS) | ({"verilate"} if allow_verilate else set())
    for key in block:
        if key in allowed:
            continue
        if key == "modes":
            raise FatalRtlBuddyError(
                f"{where}: modes is not accepted inside a mode block; a "
                "builder mode has no sub-modes."
            )
        if key in _MODE_JOB_WIDE_KEYS:
            raise FatalRtlBuddyError(
                f"{where}: {key} is not accepted in a mode block; it sizes "
                "the one build job per suite, which is one job whatever mode "
                "it compiles in. Set it beside the modes: block (suite "
                "compile, or cfg-dispatch.compile)."
            )
        if key == "verilate":
            raise FatalRtlBuddyError(
                f"{where}: verilate is only accepted in a compile mode block, "
                "not in a resources one."
            )
        raise FatalRtlBuddyError(
            f"{where}: unknown key {key!r}; a mode block carries only "
            f"{', '.join(sorted(allowed))}."
        )
    validate_cpus = _validate_compile_cpus if compile_block else _passthrough_cpus
    validate_mem = _validate_compile_mem if compile_block else _validate_mem
    validate_time = _validate_compile_time if compile_block else _validate_time
    try:
        validated = {
            "cpus": validate_cpus(block.get("cpus")),
            "mem": validate_mem(block.get("mem")),
            "time": validate_time(block.get("time")),
        }
        if allow_verilate and block.get("verilate") is not None:
            validated["verilate"] = _validate_mode_entry(
                block["verilate"],
                where=f"{where}.verilate",
                compile_block=True,
                allow_verilate=False,
            )
    except FatalRtlBuddyError as e:
        # Prefix the mode so the message names the block to edit.
        raise FatalRtlBuddyError(f"{where}: {e}") from e
    # Keep stated keys only: absent and null both mean inherit.
    return {key: value for key, value in validated.items() if value is not None}


def _passthrough_cpus(value):
    """``cpus`` for a non-compile block, unvalidated."""
    return value


def validate_modes_block(modes, *, where="", compile_block=False, allow_verilate=False):
    """Validate a raw ``modes:`` mapping; return a normalised copy, or ``None`` for ``None``.

    ``compile_block`` applies the stricter compile ``mem`` and ``time`` rules; ``allow_verilate`` permits a ``verilate:`` sub-block.
    Mode names are free text but must be strings, because YAML 1.1 reads an unquoted ``on`` or ``no`` as a boolean.
    """
    if modes is None:
        return None
    if not isinstance(modes, dict):
        raise FatalRtlBuddyError(
            f"{where}modes must be a mapping of builder mode to a reservation "
            f'block, for example modes: {{cov: {{mem: 16G, time: "00:30:00"}}}} '
            f"(got {type(modes).__name__})."
        )
    validated = {}
    for name, block in modes.items():
        if not isinstance(name, str):
            raise FatalRtlBuddyError(
                f"{where}modes: mode name {name!r} is not a string; a builder "
                "mode is a cfg-rtl-builder builder-opts key. Quote it — YAML "
                "1.1 reads an unquoted on/off/yes/no as a boolean."
            )
        validated[name] = _validate_mode_entry(
            block,
            where=f"{where}modes.{name}",
            compile_block=compile_block,
            allow_verilate=allow_verilate,
        )
    return validated


def mode_override(block, builder_mode, *, compile_block=False) -> ModeOverride | None:
    """One block's ``modes.<builder_mode>`` override, or ``None`` when there is nothing to layer.

    Validates on every call because per-test and per-testbench ``resources:`` blocks are raw serde.
    """
    if builder_mode is None or block is None:
        return None
    modes = getattr(block, "modes", None)
    if not modes:
        return None
    validated = validate_modes_block(
        modes,
        compile_block=compile_block,
        allow_verilate=compile_block,
    )
    entry = validated.get(builder_mode)
    if not entry:
        return None
    verilate = entry.get("verilate")
    return ModeOverride(
        cpus=entry.get("cpus"),
        mem=entry.get("mem"),
        time=entry.get("time"),
        verilate=(
            CompileVerilateFile(
                cpus=verilate.get("cpus"),
                mem=verilate.get("mem"),
                time=verilate.get("time"),
            )
            if verilate
            else None
        ),
    )


def effective_compile_block(block, builder_mode=None):
    """One ``compile:`` block with its ``modes.<mode>`` overrides folded in.

    Gives :func:`aggregate_compile_resources` and :func:`verilate_build_block` the per-build shape for the run's mode.
    Returns ``block`` itself when there is nothing to fold.
    """
    override = mode_override(block, builder_mode, compile_block=True)
    if override is None:
        return block

    def pick(name):
        own = getattr(override, name, None)
        return own if own is not None else getattr(block, name, None)

    base_verilate = getattr(block, "verilate", None)
    verilate = override.verilate
    if verilate is not None and base_verilate is not None:
        verilate = CompileVerilateFile(
            cpus=(verilate.cpus if verilate.cpus is not None else base_verilate.cpus),
            mem=(verilate.mem if verilate.mem is not None else base_verilate.mem),
            time=(verilate.time if verilate.time is not None else base_verilate.time),
        )
    return TestbenchCompileFile(
        cpus=pick("cpus"),
        mem=pick("mem"),
        time=pick("time"),
        verilate=verilate if verilate is not None else base_verilate,
    )


def validate_resources_block(res, *, allow_modes=False, where=""):
    """Validate a raw ``{cpus, mem, time}`` block; return a fresh copy, or ``None`` for ``None``.

    The entry point for other config files that carry a reservation block; it rejects the YAML 1.1 sexagesimal trap at load.
    ``modes:`` is refused unless ``allow_modes`` is true, because an owner with no builder mode (an elaboration profile) would ignore it.
    """
    if res is None:
        return None
    modes = getattr(res, "modes", None)
    if modes is not None and not allow_modes:
        raise FatalRtlBuddyError(
            f"{where}modes is not accepted in this reservation block; it is "
            "resolved without a builder mode, so a per-mode override here "
            "would never take effect."
        )
    return DispatchResourcesFile(
        cpus=res.cpus,
        mem=_validate_mem(res.mem),
        time=_validate_time(res.time),
        modes=validate_modes_block(modes, where=where),
    )


def _validate_compile_mem(value):
    """:func:`_validate_mem`, plus a check that the build job can sum it.

    The value must be readable by :func:`mem_to_bytes` and positive.
    """
    text = _validate_mem(value)
    if text is None:
        return None
    parsed = mem_to_bytes(text)
    if parsed is None:
        raise FatalRtlBuddyError(
            f"dispatch resources: mem {value!r} is not a value Slurm "
            "understands (expected bytes, or a number with a K/M/G/T suffix "
            "such as 512M or 16G)."
        )
    if parsed <= 0:
        # A negative value would be subtracted from the sum; zero reserves nothing.
        raise FatalRtlBuddyError(
            f"dispatch resources: mem {value!r} must be greater than zero; a "
            "compile reservation is summed across the builds that run at "
            "once, and a negative one would shrink it."
        )
    return text


def _validate_compile_time(value):
    """:func:`_validate_time`, plus a check that the time is greater than zero."""
    text = _validate_time(value)
    if text is None:
        return None
    if not time_to_seconds(text):
        raise FatalRtlBuddyError(
            f'dispatch resources: time "{value}" must be greater than zero.'
        )
    return text


def _validate_compile_cpus(value):
    """A compile reservation's ``cpus``; ``None`` in, ``None`` out."""
    if value is not None and value < 1:
        raise FatalRtlBuddyError(
            f"dispatch resources: cpus {value!r} must be at least 1."
        )
    return value


def _validate_verilate_block(res):
    """Validate a raw ``compile.verilate`` sub-block; return a fresh copy, or ``None`` for ``None``.

    Uses the compile validators. A ``modes:`` inside it is refused; write ``compile.modes.<mode>.verilate``.
    """
    if res is None:
        return None
    if getattr(res, "modes", None) is not None:
        raise FatalRtlBuddyError(
            "modes is not accepted inside a verilate block; write the "
            "per-mode verilate reservation as "
            "compile.modes.<mode>.verilate instead."
        )
    return CompileVerilateFile(
        cpus=_validate_compile_cpus(res.cpus),
        mem=_validate_compile_mem(res.mem),
        time=_validate_compile_time(res.time),
    )


@serde
class TestbenchCompileFile:
    """A testbench's own ``compile:`` block in tests.yaml.

    ``cpus``, ``mem`` and ``time`` are per build, unlike the whole-job suite block (see :func:`aggregate_compile_resources`).
    ``parallel`` and ``split-verilate`` exist only so :func:`validate_testbench_compile_block` can refuse them; serde would drop them silently.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # Per-builder-mode overrides layered over the resolved compile value.
    modes: dict | None = None
    # This build's verilate reservation.
    verilate: CompileVerilateFile | None = None
    # Refused by the validator.
    parallel: int | None = None
    # Refused by the validator: whether to split is a per-suite property.
    split_verilate: bool | None = field(rename="split-verilate", default=None)


def validate_testbench_compile_block(res):
    """Validate a raw testbench ``compile:`` block; return a fresh copy, or ``None`` for ``None``.

    Applies the compile ``mem`` and ``time`` rules and refuses ``parallel`` and ``split-verilate``. The caller prefixes the testbench name.
    """
    if res is None:
        return None
    if getattr(res, "parallel", None) is not None:
        raise FatalRtlBuddyError(
            "parallel is not accepted on a testbench compile block; set it "
            "at suite level (compile.parallel) or in cfg-dispatch.compile."
        )
    if getattr(res, "split_verilate", None) is not None:
        raise FatalRtlBuddyError(
            "split-verilate is not accepted on a testbench compile block; "
            "set it at suite level (compile.split-verilate) or in "
            "cfg-dispatch.compile."
        )
    return TestbenchCompileFile(
        cpus=_validate_compile_cpus(res.cpus),
        mem=_validate_compile_mem(res.mem),
        time=_validate_compile_time(res.time),
        verilate=_validate_verilate_block(getattr(res, "verilate", None)),
        modes=validate_modes_block(
            getattr(res, "modes", None),
            where="compile.",
            compile_block=True,
            allow_verilate=True,
        ),
    )


@serde
class SuiteCompileFile:
    """A suite's own top-level ``compile:`` block in tests.yaml.

    Its own class because ``parallel`` and ``split-verilate`` default to ``None`` (inherit from ``cfg-dispatch.compile``), so a suite overriding only ``mem`` does not pin them.
    """

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # ``None`` inherits ``cfg-dispatch.compile.parallel``.
    parallel: int | None = None
    modes: dict | None = None
    verilate: CompileVerilateFile | None = None
    # ``None`` inherits ``cfg-dispatch.compile.split-verilate``.
    split_verilate: bool | None = field(rename="split-verilate", default=None)


def validate_compile_block(res):
    """Validate a raw suite-level ``compile:`` block; return a fresh copy, or ``None`` for ``None``.

    Applies :func:`validate_resources_block`'s rules plus ``parallel >= 1``. The caller prefixes the suite path.
    """
    if res is None:
        return None
    parallel = getattr(res, "parallel", None)
    if parallel is not None and parallel < 1:
        raise FatalRtlBuddyError(
            f"compile parallel must be >= 1 (got {parallel}); a build job "
            "allowed zero concurrent builds would compile nothing."
        )
    return SuiteCompileFile(
        cpus=res.cpus,
        mem=_validate_mem(res.mem),
        time=_validate_time(res.time),
        parallel=parallel,
        verilate=_validate_verilate_block(getattr(res, "verilate", None)),
        split_verilate=getattr(res, "split_verilate", None),
        modes=validate_modes_block(
            getattr(res, "modes", None),
            where="compile.",
            compile_block=True,
            allow_verilate=True,
        ),
    )


@serde
class RightsizeConfigFile:
    """``rightsize:`` sub-block: reservation right-sizing thresholds.

    Utilization below ``over-threshold`` marks a resource over-reserved; above ``near-limit`` (or after a TIMEOUT/OOM kill) it is under-reserved.
    The suggestion is the observed peak times ``margin``.
    """

    report: bool = True
    over_threshold: float = field(rename="over-threshold", default=0.5)
    near_limit: float = field(rename="near-limit", default=0.9)
    margin: float = field(rename="margin", default=1.5)


# The list is closed: retrying anything else (a hung testbench, an undersized reservation) would re-run work that failed on its own merits.
# The one classifier is a job killed while its simulation waited in the VCS license queue.
RETRY_CLASSIFIER_LICENSE_QUEUE = "license-queue"
RETRY_CLASSIFIERS = (RETRY_CLASSIFIER_LICENSE_QUEUE,)


@serde
class RetryConfigFile:
    """``retry:`` sub-block: a retry budget for resource-condition kills.

    Inert by default (``attempts`` is 0). ``attempts`` counts extra attempts after the first.
    The delay before attempt *n* is ``min(backoff-max-sec, backoff-sec * 2 ** (n - 1))`` times ``uniform(1 - jitter, 1 + jitter)``; the jitter keeps jobs that lost the same license race from retrying in lockstep.
    """

    attempts: int = 0
    backoff_sec: float = field(rename="backoff-sec", default=60.0)
    backoff_max_sec: float = field(rename="backoff-max-sec", default=600.0)
    jitter: float = 0.5
    # Not named ``on:``: PyYAML (YAML 1.1) reads an unquoted ``on`` key as boolean ``True``.
    classifiers: list[str] = field(
        default_factory=lambda: [RETRY_CLASSIFIER_LICENSE_QUEUE]
    )

    def validated(self) -> "RetryConfigFile":
        """Raise on an invalid budget; return a normalised copy."""
        if self.attempts < 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry attempts must be >= 0 (got {self.attempts}); "
                "0 (or omitting the block) disables retry."
            )
        if self.backoff_sec < 0 or self.backoff_max_sec < 0:
            raise FatalRtlBuddyError(
                "cfg-dispatch retry backoff-sec/backoff-max-sec must be >= 0 "
                f"(got {self.backoff_sec}/{self.backoff_max_sec})."
            )
        if self.backoff_max_sec < self.backoff_sec:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry backoff-max-sec ({self.backoff_max_sec}) is "
                f"below backoff-sec ({self.backoff_sec}); the cap would shorten "
                "the very first delay."
            )
        if not 0 <= self.jitter < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry jitter must be in [0, 1) (got {self.jitter}); "
                "1 or more would allow a zero or negative delay."
            )
        unknown = [c for c in self.classifiers if c not in RETRY_CLASSIFIERS]
        if unknown:
            raise FatalRtlBuddyError(
                f"cfg-dispatch retry classifiers: unknown classifier(s) "
                f"{unknown} — known: {list(RETRY_CLASSIFIERS)}."
            )
        return RetryConfigFile(
            attempts=self.attempts,
            # float(): the runtime object does arithmetic on these.
            backoff_sec=float(self.backoff_sec),
            backoff_max_sec=float(self.backoff_max_sec),
            jitter=float(self.jitter),
            classifiers=list(self.classifiers),
        )

    @property
    def enabled(self) -> bool:
        """Whether this budget would retry anything. An empty ``classifiers`` list retries nothing."""
        return self.attempts > 0 and bool(self.classifiers)


@serde
class DispatchConfigFile:
    """``cfg-dispatch`` section of root_config.yaml (raw serde form)."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    # The coverage tail job's reservation (merge, model build, LCOV exports, manifest) under a scheduler-backed backend; defaults to `resources`.
    # Run-level, one job per invocation, so it has no suite or testbench layer. `modes.<mode>` layers on top like the others.
    coverage: DispatchResourcesFile | None = None
    # Compile reservation; defaults to `resources`. Normally sizes the head-dispatched build job.
    # For a builder that cannot share a build it is folded into each sim job (see combine_for_in_job_compile).
    # Also carries `parallel`, which per-job `resources:` blocks lack.
    compile: DispatchCompileFile | None = None
    sbatch_args: list[str] = field(rename="sbatch-args", default_factory=list)
    poll_interval: float = field(rename="poll-interval", default=10.0)
    # Cadence of the console progress line while the fleet drains, separate from `poll-interval` (the scheduler query pace).
    # 0 silences the console; the log file still records every change at INFO.
    progress_interval: float = field(rename="progress-interval", default=60.0)
    # Wall-clock bound on the collect wait; `None` waits indefinitely.
    # On expiry the failure names the outstanding job ids.
    max_wait: float | None = field(rename="max-wait", default=None)
    # What the next invocation does about a previous run's surviving jobs: `warn` names them and submits a fresh fleet,
    # `cancel` scancels them first, `adopt` collects them instead of submitting.
    # Only consulted for scheduler-backed backends.
    orphans: str = ORPHANS_DEFAULT
    # Cap on concurrently running elements per submitted array (sbatch --array=1-N%cap).
    max_jobs_per_array: int = field(rename="max-jobs-per-array", default=200)
    # The cluster's Slurm ``MaxArraySize`` (slurm.conf), an exclusive bound on the task index.
    # Manifests are 1-based, so 1001 permits ``--array=1-1000``. Larger resource groups are split across arrays.
    # ``None`` reads it from ``scontrol show config``.
    max_array_size: int | None = field(rename="max-array-size", default=None)
    # The cluster's ``SchedulerParameters=max_array_tasks``, an inclusive count of tasks per array.
    # It is a separate field because it is a different limit from ``MaxArraySize``.
    # ``None`` reads it from ``scontrol show config``; the smaller of the two ceilings applies.
    max_array_tasks: int | None = field(rename="max-array-tasks", default=None)
    # Concurrent subprocesses for the `local-parallel` backend (one global pool). `None` means min(4, cpu_count); `--jobs` overrides.
    jobs: int | None = None
    rightsize: RightsizeConfigFile | None = None
    # Retry budget for jobs killed under a resource condition while queueing for a license seat. Absent means off.
    retry: RetryConfigFile | None = None

    def initialise(self) -> "DispatchConfig":
        """Validate and freeze into the runtime :class:`DispatchConfig`."""
        if self.poll_interval <= 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch poll-interval must be > 0 (got {self.poll_interval}); "
                "a zero interval turns collection into a squeue busy-loop."
            )

        def _validated(res):
            if res is None:
                return None
            return DispatchResourcesFile(
                cpus=res.cpus,
                mem=_validate_mem(res.mem),
                time=_validate_time(res.time),
                # Held to the same rules as the base fields; this is the least specific layer of both the sim and compile reservations.
                modes=validate_modes_block(
                    getattr(res, "modes", None), where="cfg-dispatch.resources."
                ),
            )

        def _validated_compile(res):
            """The compile block, through the same mem/time validators."""
            if res is None:
                return None
            return DispatchCompileFile(
                cpus=res.cpus,
                mem=_validate_mem(res.mem),
                time=_validate_time(res.time),
                parallel=res.parallel,
                verilate=_validate_verilate_block(getattr(res, "verilate", None)),
                split_verilate=getattr(res, "split_verilate", True),
                modes=validate_modes_block(
                    getattr(res, "modes", None),
                    where="cfg-dispatch.compile.",
                    compile_block=True,
                    allow_verilate=True,
                ),
            )

        def _validated_coverage(res):
            """The coverage block, through the compile mem/time/cpus validators: one job, sized like a build."""
            if res is None:
                return None
            try:
                cpus = _validate_compile_cpus(res.cpus)
                mem = _validate_compile_mem(res.mem)
                time = _validate_compile_time(res.time)
            except FatalRtlBuddyError as e:
                raise FatalRtlBuddyError(f"cfg-dispatch.coverage: {e}") from e
            return DispatchResourcesFile(
                cpus=cpus,
                mem=mem,
                time=time,
                # Names its own block in any message.
                modes=validate_modes_block(
                    getattr(res, "modes", None),
                    where="cfg-dispatch.coverage.",
                    compile_block=True,
                ),
            )

        if self.progress_interval < 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch progress-interval must be >= 0 "
                f"(got {self.progress_interval}); use 0 to disable console "
                "progress lines."
            )
        if self.max_wait is not None and self.max_wait <= 0:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-wait must be > 0 when set (got {self.max_wait}); "
                "omit it for an unbounded wait."
            )
        if self.max_jobs_per_array < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-jobs-per-array must be >= 1 "
                f"(got {self.max_jobs_per_array})."
            )
        if self.max_array_size is not None and self.max_array_size < 2:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-array-size must be >= 2 when set (got "
                f"{self.max_array_size}); it is Slurm's MaxArraySize, whose "
                "largest task index is one BELOW it, so 2 is the smallest "
                "value that still permits a one-element array."
            )
        if self.max_array_tasks is not None and self.max_array_tasks < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch max-array-tasks must be >= 1 when set (got "
                f"{self.max_array_tasks}); it is Slurm's max_array_tasks, a "
                "COUNT of the tasks one array may hold, so 1 is the smallest "
                "value that still permits an array."
            )
        if self.orphans not in ORPHANS_POLICIES:
            raise FatalRtlBuddyError(
                f"cfg-dispatch orphans must be one of "
                f"{', '.join(ORPHANS_POLICIES)} (got {self.orphans!r})."
            )
        if self.jobs is not None and self.jobs < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch jobs must be >= 1 (got {self.jobs}); a pool of "
                "zero would never start a job."
            )
        if self.compile is not None and self.compile.parallel < 1:
            raise FatalRtlBuddyError(
                f"cfg-dispatch compile parallel must be >= 1 (got "
                f"{self.compile.parallel}); a build job allowed zero concurrent "
                "builds would compile nothing."
            )
        return DispatchConfig(
            backend=self.backend,
            resources=_validated(self.resources),
            compile=_validated_compile(self.compile),
            coverage=_validated_coverage(self.coverage),
            sbatch_args=list(self.sbatch_args),
            poll_interval=self.poll_interval,
            progress_interval=self.progress_interval,
            max_wait=self.max_wait,
            orphans=self.orphans,
            max_jobs_per_array=self.max_jobs_per_array,
            max_array_size=self.max_array_size,
            max_array_tasks=self.max_array_tasks,
            jobs=self.jobs,
            rightsize=self.rightsize,
            retry=self.retry.validated() if self.retry is not None else None,
        )


def _serde_keys(cls) -> tuple[str, ...]:
    """The YAML keys a serde class reads: each field's ``rename``, else its name."""
    return tuple(
        f.metadata.get("serde_rename", f.name) for f in dataclasses.fields(cls)
    )


# Nested reservation blocks checked by warn_unknown_block_keys: key -> (class, its own nested blocks).
_VERILATE_BLOCK = {"verilate": (CompileVerilateFile, {})}
RESOURCES_BLOCK = (DispatchResourcesFile, {})
SUITE_COMPILE_BLOCK = (SuiteCompileFile, _VERILATE_BLOCK)
TESTBENCH_COMPILE_BLOCK = (TestbenchCompileFile, _VERILATE_BLOCK)
DISPATCH_BLOCK = (
    DispatchConfigFile,
    {
        "resources": RESOURCES_BLOCK,
        "compile": (DispatchCompileFile, _VERILATE_BLOCK),
        "coverage": RESOURCES_BLOCK,
        "retry": (RetryConfigFile, {}),
        "rightsize": (RightsizeConfigFile, {}),
    },
)


# (path, block, key) already warned about: a models.yaml is reloaded for every test that names one of its models.
_WARNED_UNKNOWN_KEYS: set[tuple[str, str, str]] = set()


def warn_unknown_block_keys(raw, spec, *, path, block) -> list[str]:
    """Warn about every key serde would drop from the raw mapping ``raw``; return them as ``block.key`` paths.

    ``spec`` is ``(serde class, {key: nested spec})``. Each unknown key logs ``config.unknown_key`` naming ``path``, the block, the key and its closest known spelling, once per process.
    A non-mapping ``raw`` is left to the typed load, and a ``modes:`` block is not descended into because :func:`validate_modes_block` already rejects its unknown keys.
    """
    if not isinstance(raw, dict):
        return []
    cls, nested = spec
    known = _serde_keys(cls)
    found = []
    for key in raw:
        if key in known:
            continue
        # YAML 1.1 reads an unquoted `on`/`yes` key as a boolean.
        name = str(key)
        found.append(f"{block}.{name}")
        seen = (str(path), block, name)
        if seen in _WARNED_UNKNOWN_KEYS:
            continue
        _WARNED_UNKNOWN_KEYS.add(seen)
        close = difflib.get_close_matches(name, known, n=1)
        log_event(
            logger,
            logging.WARNING,
            "config.unknown_key",
            path=str(path),
            block=block,
            key=name,
            suggestion=close[0] if close else None,
            known=list(known),
        )
    for key, child in nested.items():
        found += warn_unknown_block_keys(
            raw.get(key), child, path=path, block=f"{block}.{key}"
        )
    return found


@dataclass
class DispatchConfig:
    """Validated runtime dispatch configuration (see DispatchConfigFile)."""

    backend: str | None = None
    resources: DispatchResourcesFile | None = None
    compile: DispatchCompileFile | None = None
    coverage: DispatchResourcesFile | None = None
    sbatch_args: list = None
    poll_interval: float = 10.0
    progress_interval: float = 60.0
    max_wait: float | None = None
    orphans: str = ORPHANS_DEFAULT
    max_jobs_per_array: int = 200
    max_array_size: int | None = None
    max_array_tasks: int | None = None
    jobs: int | None = None
    rightsize: RightsizeConfigFile | None = None
    retry: RetryConfigFile | None = None

    def __post_init__(self):
        if self.sbatch_args is None:
            self.sbatch_args = []

    def effective_rightsize(self) -> RightsizeConfigFile:
        return self.rightsize if self.rightsize is not None else RightsizeConfigFile()

    def effective_retry(self) -> RetryConfigFile:
        """The retry budget, or an empty one that retries nothing."""
        return self.retry if self.retry is not None else RetryConfigFile()


@dataclass
class JobResources:
    """Fully resolved reservation for one dispatched job.

    ``mem`` stays optional because clusters where memory is not schedulable reject an unconditional ``--mem``.
    """

    cpus: int = DEFAULT_JOB_CPUS
    mem: str | None = None
    time: str = DEFAULT_JOB_TIME


def resolve_resources(
    dispatch_cfg, test_cfg=None, *, builder_mode=None
) -> JobResources:
    """Resolve a test's effective job reservation.

    Layers apply field by field, most specific first: test ``resources:``, testbench ``resources:``, ``cfg-dispatch.resources``, built-in defaults.
    ``builder_mode`` is the job's ``-M`` value after command defaults. Each layer's ``modes.<builder_mode>`` block is then applied over the resolved value, least specific layer first::

        test.modes[m] > testbench.modes[m] > cfg-dispatch.modes[m]
            > test > testbench > cfg-dispatch > default

    A mode block therefore beats every base field, not only the one on its own layer.
    """
    blocks = [dispatch_cfg.resources if dispatch_cfg is not None else None]
    if test_cfg is not None:
        blocks.append(getattr(test_cfg.get_testbench(), "resources", None))
        blocks.append(getattr(test_cfg, "resources", None))
    resolved = JobResources()
    layers = list(blocks) + [mode_override(block, builder_mode) for block in blocks]
    for layer in layers:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = layer.cpus
        if layer.mem is not None:
            # Raw serde may carry the YAML sexagesimal/int trap; validate as applied.
            resolved.mem = _validate_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_time(layer.time)
    return resolved


def resolve_coverage_resources(dispatch_cfg, *, builder_mode=None) -> JobResources:
    """Resolve the coverage tail job's reservation.

    Layers apply field by field: ``cfg-dispatch.coverage``, ``cfg-dispatch.resources``, built-in defaults.
    ``builder_mode`` then layers each block's ``modes.<builder_mode>`` over the result, least specific first, as :func:`resolve_resources` does.
    The tail exists only for a coverage run, so ``modes.cov`` is the usual place to size it.
    """
    resolved = JobResources()
    blocks = []
    if dispatch_cfg is not None:
        blocks = [(dispatch_cfg.resources, False), (dispatch_cfg.coverage, True)]
    layers = [block for block, _ in blocks] + [
        mode_override(block, builder_mode, compile_block=is_coverage)
        for block, is_coverage in blocks
    ]
    for layer in layers:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = layer.cpus
        if layer.mem is not None:
            resolved.mem = _validate_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_time(layer.time)
    return resolved


def mode_governed_fields(dispatch_cfg, test_cfg=None, *, builder_mode=None) -> dict:
    """Which sim reservation fields a ``modes:`` block won, as ``{field: builder_mode}``.

    Empty when no mode block is in play. Reservation advice uses it to name the mode's own key, since a mode layer beats every base field.
    """
    blocks = [dispatch_cfg.resources if dispatch_cfg is not None else None]
    if test_cfg is not None:
        blocks.append(getattr(test_cfg.get_testbench(), "resources", None))
        blocks.append(getattr(test_cfg, "resources", None))
    governed = {}
    for block in blocks:
        override = mode_override(block, builder_mode)
        if override is None:
            continue
        for name in _MODE_FIELDS:
            if getattr(override, name) is not None:
                governed[name] = builder_mode
    return governed


# sbatch options that change what a job requests in cpus, so `ReqCPUS` (tasks x cpus-per-task) can differ from the resolved cpus-per-task.
# The set is narrow on purpose: a false positive discards a request the head knows and retargets the edit hint.
# Excluded on purpose:
# - `--exclusive`, `--overcommit`: change the allocation, not the request.
# - `--threads-per-core`, `-B`/`--extra-node-info`: node-selection constraints; `--cpus-per-task` still states the request.
# - `--cpus-per-gpu`: mutually exclusive with the `--cpus-per-task` every job carries, so it can never take effect.
# - `--ntasks-per-core`, `--ntasks-per-socket`: placement maxima that request nothing alone.
#   `--ntasks-per-gpu` is also excluded here; see `_gpu_derived_task_count`.
#
# Keyed by long form, valued by short form: `-c 4 --cpus-per-task=8` is one option written twice, and the last wins.
# The direct count is split out because a whole-job suggestion can be written into it; a task or node count only scales the request.
_DIRECT_CPU_COUNT_OPTS = {
    "--cpus-per-task": "-c",
}
_CPU_SCALING_OPTS = {
    # Task and node counts that raise the request above one cpus-per-task.
    # `--ntasks-per-node` counts because it is a request when `--ntasks` is absent (`--nodes=2 --ntasks-per-node=4` asks for eight tasks).
    "--ntasks": "-n",
    "--ntasks-per-node": None,
    "--nodes": "-N",
}
_CPU_REQUEST_OPTS = {**_DIRECT_CPU_COUNT_OPTS, **_CPU_SCALING_OPTS}
_CPU_REQUEST_SHORT_TO_LONG = {
    short: long for long, short in _CPU_REQUEST_OPTS.items() if short
}


def sbatch_arg_sets_cpu_count_directly(arg: str) -> bool:
    """Whether this ``sbatch-args`` entry states the cpu count itself (``-c`` or ``--cpus-per-task``).

    Task and node counts (``--ntasks``, ``--ntasks-per-node``, ``-N``/``--nodes``) only scale the request, so a cpu count cannot be written into them.
    Takes an entry as :func:`sbatch_args_cpu_request_options` renders it: ``--cpus-per-task=4``, ``--cpus-per-task 4``, ``-c 4``, ``-c4`` or ``-c=4``.
    """
    # The option token precedes the first `=` or space.
    token = arg.split("=", 1)[0].split(" ", 1)[0]
    if token in _DIRECT_CPU_COUNT_OPTS:
        return True
    short = _DIRECT_CPU_COUNT_OPTS["--cpus-per-task"]
    # `-c4` keeps its value, which must be numeric, or this is a different option.
    return token == short or (token.startswith(short) and token[len(short) :].isdigit())


# `SBATCH_*` input variables equivalent to the options above. They are inherited by the sbatch subprocess and deliberately not sanitized.
# `SBATCH_CPUS_PER_TASK` is absent: the command line beats the environment and every submit path emits `--cpus-per-task`
# (tests/test_dispatch_slurm.py pins this).
_CPU_REQUEST_ENV_VARS = {
    "SBATCH_NTASKS": "--ntasks",
    "SBATCH_NTASKS_PER_NODE": "--ntasks-per-node",
    "SBATCH_NODES": "--nodes",
}


# With a GPU count and no `--ntasks`, `--ntasks-per-gpu` derives tasks = gpus x ntasks-per-gpu, which multiplies the generated `--cpus-per-task`.
# `--gpus-per-task` is absent because sbatch makes it mutually exclusive with `--ntasks-per-gpu`.
_GPU_COUNT_OPTS = {
    "--gpus": "-G",
    "--gpus-per-node": None,
    "--gpus-per-socket": None,
    # Counts only when it asks for gpus (`--gres=gpu:2`, not `--gres=fs:1`).
    "--gres": None,
}
_GPU_COUNT_ENV_VARS = {
    "SBATCH_GPUS": "--gpus",
    "SBATCH_GPUS_PER_NODE": "--gpus-per-node",
    "SBATCH_GPUS_PER_SOCKET": "--gpus-per-socket",
    "SBATCH_GRES": "--gres",
}
_NTASKS_PER_GPU_OPT = {"--ntasks-per-gpu": None}
_NTASKS_PER_GPU_ENV_VAR = "SBATCH_NTASKS_PER_GPU"


def _gpu_derived_task_count(sbatch_args, env) -> dict[str, str]:
    """The ``--gpus`` + ``--ntasks-per-gpu`` pair, when it sets the task count.

    Either half may come from ``sbatch-args`` or the environment. Returns both, since neither alone causes the override.
    """
    per_gpu = _scan_options(sbatch_args, _NTASKS_PER_GPU_OPT)
    if not per_gpu:
        value = (env.get(_NTASKS_PER_GPU_ENV_VAR) or "").strip()
        if value:
            per_gpu = {"--ntasks-per-gpu": f"{_NTASKS_PER_GPU_ENV_VAR}={value}"}
    if not per_gpu:
        return {}
    gpus = _scan_options(sbatch_args, _GPU_COUNT_OPTS)
    # `--gres` carries many resource kinds; only a gpu one counts.
    gres = _scan_options(sbatch_args, {"--gres": None}, value_must_contain="gpu")
    gpus = {k: v for k, v in gpus.items() if k != "--gres"} | gres
    for var, option in _GPU_COUNT_ENV_VARS.items():
        value = (env.get(var) or "").strip()
        if not value or option in gpus:
            continue
        if option == "--gres" and "gpu" not in value.lower():
            continue
        gpus[option] = f"{var}={value}"
    if not gpus:
        # A lone `--ntasks-per-gpu` only caps placement and requests nothing.
        return {}
    return {**gpus, **per_gpu}


def cpu_request_overrides(sbatch_args, env=None) -> list[str]:
    """Everything that supersedes the cpus reservation the head resolved.

    The union of :func:`sbatch_args_cpu_request_options` and the equivalent ``SBATCH_*`` variables.
    Command line beats environment, so a variable whose option is also in ``sbatch-args`` is not reported; blank variables are ignored.
    Environment entries are rendered ``NAME=value`` and argument entries keep their leading dash.
    """
    found = _scan_cpu_request_args(sbatch_args)
    env = os.environ if env is None else env
    for var, option in _CPU_REQUEST_ENV_VARS.items():
        value = (env.get(var) or "").strip()
        if not value or option in found:
            continue
        found[option] = f"{var}={value}"
    # With `--ntasks` present, sbatch reads `--ntasks-per-gpu` as the GPU count to satisfy instead.
    if "--ntasks" not in found:
        found.update(_gpu_derived_task_count(sbatch_args, env))
    return list(found.values())


def sbatch_args_cpu_request_options(sbatch_args) -> list[str]:
    """The ``sbatch-args`` entries that decide the job's cpu request.

    ``sbatch-args`` is appended after the generated reservation flags, so an entry there wins.
    A non-empty result means the resolved ``cpus`` is not what the job was submitted with: right-sizing falls back to the scheduler's ``ReqCPUS`` and the ``cpus`` edit hint names ``sbatch-args`` instead of the masked field.

    Qualifying options are the cpu count (``-c``/``--cpus-per-task``) and the task and node counts that raise it (``-n``/``--ntasks``, ``--ntasks-per-node``, ``-N``/``--nodes``); the comment on ``_CPU_REQUEST_OPTS`` lists what is excluded.
    Returns one entry per distinct option in order of first appearance, as written; the last occurrence of an option wins. Several entries mean the request is their product.
    Only ``cpus`` needs this: ``mem`` and ``time`` advice uses ``ReqMem`` and ``TimelimitRaw``, which reflect any override.
    """
    return list(_scan_cpu_request_args(sbatch_args).values())


def _scan_cpu_request_args(sbatch_args) -> dict[str, str]:
    """Canonical long option to the entry that set it, as written."""
    return _scan_options(sbatch_args, _CPU_REQUEST_OPTS)


def _scan_options(sbatch_args, long_to_short, *, value_must_contain=None):
    """Match a table of sbatch options against a verbatim argument list.

    Handles ``--long=value``, ``--long value``, ``-x value`` and ``-x4``. ``value_must_contain`` limits matches to values containing a substring (used to count ``--gres`` only for gpus).
    """
    args = list(sbatch_args or [])
    short_to_long = {short: long for long, short in long_to_short.items() if short}
    # Insertion-ordered: a repeated option keeps its first position and takes its last value.
    found: dict[str, str] = {}

    def keep(value):
        return value_must_contain is None or value_must_contain in (value or "").lower()

    for index, arg in enumerate(args):
        if arg in long_to_short or arg in short_to_long:
            # Value in the next argument. A trailing flag with no value is still reported as an override.
            following = args[index + 1 : index + 2]
            if following and not keep(following[0]):
                continue
            canonical = short_to_long.get(arg, arg)
            found[canonical] = f"{arg} {following[0]}" if following else arg
            continue
        for long in long_to_short:
            if arg.startswith(f"{long}=") and keep(arg.split("=", 1)[1]):
                found[long] = arg
                break
        else:
            for short, long in short_to_long.items():
                # A numeric value is required so an unrelated `-cfoo` does not match.
                value = arg[len(short) :].lstrip("=") if arg.startswith(short) else ""
                if value and value[0].isdigit() and keep(value):
                    found[long] = arg
                    break
    return found


def compile_parallel(dispatch_cfg, suite_compile=None) -> int:
    """How many distinct builds one build job compiles concurrently.

    Not a field of :class:`JobResources`: the resolved compile reservation also sizes an in-job compile's sim job and the right-sizing floor, both of which are one serial build.
    ``suite_compile``'s ``parallel`` wins where set, as in :func:`resolve_compile_resources`; a block without the attribute reads as inherit.
    """
    suite_parallel = getattr(suite_compile, "parallel", None)
    if suite_parallel is not None:
        return suite_parallel
    if dispatch_cfg is None or dispatch_cfg.compile is None:
        return 1
    return dispatch_cfg.compile.parallel


def compile_split_verilate(dispatch_cfg, suite_compile=None) -> bool:
    """Whether this suite's compile is submitted as a verilate job chained to a build job.

    Layered like :func:`compile_parallel`: the suite's ``split_verilate`` wins where set. Only Slurm acts on the answer.
    """
    suite_split = getattr(suite_compile, "split_verilate", None)
    if suite_split is not None:
        return bool(suite_split)
    if dispatch_cfg is None or dispatch_cfg.compile is None:
        return True
    return bool(getattr(dispatch_cfg.compile, "split_verilate", True))


def mem_to_bytes(value) -> int | None:
    """Parse an sbatch ``--mem`` spelling to bytes; ``None`` if unparseable.

    Slurm's default unit is megabytes, so a bare number is MB — not bytes.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    scale = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
    unit = text[-1].upper()
    if unit in scale:
        text, factor = text[:-1], scale[unit]
    else:
        factor = 2**20
    try:
        return int(float(text) * factor)
    except ValueError:
        return None


def _compile_mem_bytes(value, *, testbench=None):
    """Parse a compile reservation's ``mem`` to bytes; raise on an unreadable or non-positive value.

    An unreadable value cannot be dropped from :func:`aggregate_compile_resources`'s sum without shrinking the reservation. ``None`` in, ``None`` out.
    """
    if value is None:
        return None
    parsed = mem_to_bytes(value)
    whose = f"testbench {testbench!r}: " if testbench else ""
    if parsed is None:
        raise FatalRtlBuddyError(
            f"{whose}compile mem {value!r} is not a value Slurm understands "
            "(expected bytes, or a number with a K/M/G/T suffix such as 512M "
            "or 16G); the build job's reservation is summed from these, so an "
            "unparseable one cannot be sized around."
        )
    if parsed <= 0:
        # Backstop for the load-time rule: a negative value would be subtracted from the sum.
        raise FatalRtlBuddyError(
            f"{whose}compile mem {value!r} must be greater than zero; the "
            "build job's reservation is summed from these, and a negative "
            "one would shrink it."
        )
    return parsed


def format_mem(bytes_val: int) -> str:
    """Bytes to an sbatch ``M``/``G`` string, rounded up. The inverse of :func:`mem_to_bytes`."""
    mb = math.ceil(bytes_val / 2**20)
    if mb >= 4096:
        return f"{math.ceil(mb / 1024)}G"
    return f"{mb}M"


def format_time(seconds: float) -> str:
    """Seconds → ``HH:MM:SS`` rounded up to the whole minute."""
    minutes = math.ceil(seconds / 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def time_to_seconds(value) -> int | None:
    """Parse an sbatch ``--time`` spelling to seconds; ``None`` if unparseable.

    Two colons is ``HH:MM:SS``, one is ``MM:SS``, and a bare number is minutes.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not _TIME_RE.match(text):
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        days = int(day_part)
        # After DD-, the fields read left-to-right from hours: DD-HH[:MM[:SS]].
        fields = [int(p) for p in text.split(":")] if text else [0]
        fields += [0] * (3 - len(fields))
        hours, minutes, seconds = fields
    else:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 1:
            hours, minutes, seconds = 0, parts[0], 0  # bare number = minutes
        elif len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]  # MM:SS
        else:
            hours, minutes, seconds = parts  # HH:MM:SS
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def combine_for_in_job_compile(
    sim: JobResources, compile_: JobResources
) -> tuple[JobResources, dict]:
    """Reservation for a sim job that also compiles, and which layer governs each field.

    The two share one allocation when the builder cannot share a build, so the reservation is the element-wise maximum.
    The second return value maps each field to ``"compile"`` or ``"test"`` so advice can name the field that governs.
    """
    combined = JobResources(cpus=sim.cpus, mem=sim.mem, time=sim.time)
    governed_by = {"cpus": "test", "mem": "test", "time": "test"}

    if compile_.cpus > sim.cpus:
        combined.cpus = compile_.cpus
        governed_by["cpus"] = "compile"

    # An absent sim mem means no --mem; a compile mem must still take effect.
    sim_mem, compile_mem = mem_to_bytes(sim.mem), mem_to_bytes(compile_.mem)
    if compile_.mem is not None and compile_mem is None:
        # An unparseable compile mem must not silently drop out of the max.
        log_event(
            logger,
            logging.WARNING,
            "dispatch.compile_mem_unparseable",
            mem=compile_.mem,
        )
    if compile_mem is not None and (sim_mem is None or compile_mem > sim_mem):
        combined.mem = compile_.mem
        governed_by["mem"] = "compile"

    sim_time, compile_time = time_to_seconds(sim.time), time_to_seconds(compile_.time)
    if compile_time is not None and (sim_time is None or compile_time > sim_time):
        combined.time = compile_.time
        governed_by["time"] = "compile"

    return combined, governed_by


def resolve_compile_resources(
    dispatch_cfg, suite_compile=None, tb_compile=None, *, builder_mode=None
) -> JobResources:
    """Resolve the compile reservation for one testbench.

    Layers apply field by field, most specific first: the testbench's ``compile:`` block (``tb_compile``), the suite's (``suite_compile``, a :class:`SuiteCompileFile`), ``cfg-dispatch.compile``, ``cfg-dispatch.resources``, built-in defaults.
    Either block is ``None`` when absent.

    ``parallel`` is not resolved here; see :func:`compile_parallel`.
    ``builder_mode`` layers each block's ``modes.<builder_mode>`` over the result, least specific first, as :func:`resolve_resources` does; ``cfg-dispatch.resources.modes`` is included.
    The result is a scheduling fact and does not reach the compile fingerprint or shared-build key.
    """
    resolved = JobResources()
    # (block, is a compile block): compile layers' mode entries get the stricter compile rules.
    blocks = []
    if dispatch_cfg is not None:
        blocks += [(dispatch_cfg.resources, False), (dispatch_cfg.compile, True)]
    blocks += [(suite_compile, True), (tb_compile, True)]
    layers = [block for block, _ in blocks] + [
        mode_override(block, builder_mode, compile_block=is_compile)
        for block, is_compile in blocks
    ]
    for layer in layers:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = layer.cpus
        if layer.mem is not None:
            resolved.mem = _validate_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_time(layer.time)
    return resolved


def compile_resource_origins(
    suite_compile, tb_compile=None, *, builder_mode=None
) -> dict:
    """Which tests.yaml layer won each resolved compile field.

    Returns ``{field: "suite"}`` or ``{field: "testbench"}``, the latter winning as in :func:`resolve_compile_resources`; absent fields are governed by cfg-dispatch or defaults.
    ``parallel`` is included for the suite layer only, since it has no testbench layer.
    A field won by a ``modes.<mode>`` block records ``{"origin", "key"}`` with the key spelled ``modes.<mode>.<field>``, so advice names the key that governs.
    """
    origins = {}
    for name in ("cpus", "mem", "time", "parallel"):
        if getattr(suite_compile, name, None) is not None:
            origins[name] = "suite"
    for name in ("cpus", "mem", "time"):
        if getattr(tb_compile, name, None) is not None:
            origins[name] = "testbench"
    # Mode blocks apply after every base field, as in resolve_compile_resources.
    for block, origin in ((suite_compile, "suite"), (tb_compile, "testbench")):
        override = mode_override(block, builder_mode, compile_block=True)
        for name in ("cpus", "mem", "time"):
            if getattr(override, name, None) is not None:
                origins[name] = {
                    "origin": origin,
                    "key": f"modes.{builder_mode}.{name}",
                }
    return origins


def _verilate_layers(dispatch_cfg, suite_compile=None, tb_compile=None):
    """The ``compile.verilate`` blocks, least specific first."""
    layers = [getattr(getattr(dispatch_cfg, "compile", None), "verilate", None)]
    layers += [
        getattr(layer, "verilate", None) for layer in (suite_compile, tb_compile)
    ]
    return layers


def _verilate_mode_layers(
    dispatch_cfg, suite_compile=None, tb_compile=None, builder_mode=None
):
    """The ``modes.<mode>.verilate`` blocks, least specific first; applied after :func:`_verilate_layers` so a mode key beats every base key."""
    blocks = [getattr(dispatch_cfg, "compile", None), suite_compile, tb_compile]
    return [
        getattr(
            mode_override(block, builder_mode, compile_block=True), "verilate", None
        )
        for block in blocks
    ]


def resolve_verilate_resources(
    dispatch_cfg, suite_compile=None, tb_compile=None, *, builder_mode=None
) -> JobResources:
    """Resolve the verilate reservation for one testbench.

    Two stages: the fully resolved compile reservation supplies ``mem`` and ``time``, then every ``compile.verilate`` block (testbench, suite, ``cfg-dispatch.compile``) layers over them field by field.
    ``cpus`` does not inherit, because verilation is single-threaded; it starts at :data:`DEFAULT_VERILATE_CPUS`.

    ``builder_mode`` applies to both stages, so within each a mode block beats a base field, and any ``verilate`` key beats any ``compile`` key.
    The result is a scheduling fact and does not reach the compile fingerprint or shared-build key.
    """
    compile_resources = resolve_compile_resources(
        dispatch_cfg, suite_compile, tb_compile, builder_mode=builder_mode
    )
    resolved = JobResources(
        cpus=DEFAULT_VERILATE_CPUS,
        mem=compile_resources.mem,
        time=compile_resources.time,
    )
    layers = _verilate_layers(dispatch_cfg, suite_compile, tb_compile)
    layers += _verilate_mode_layers(
        dispatch_cfg, suite_compile, tb_compile, builder_mode
    )
    for layer in layers:
        if layer is None:
            continue
        if layer.cpus is not None:
            resolved.cpus = _validate_compile_cpus(layer.cpus)
        if layer.mem is not None:
            resolved.mem = _validate_compile_mem(layer.mem)
        if layer.time is not None:
            resolved.time = _validate_compile_time(layer.time)
    return resolved


def verilate_resource_origins(
    suite_compile, tb_compile=None, *, builder_mode=None
) -> dict:
    """Which tests.yaml layer and which key won each verilate field.

    Like :func:`compile_resource_origins`, but each entry is ``{"origin": "suite"|"testbench", "key": ...}``, because a field can be won by ``verilate.mem`` or by the ``compile.mem`` it falls back to.
    Fields no tests.yaml layer won are absent; ``cfg-dispatch`` governs them, and the key to write there is the ``verilate`` one. ``cpus`` has no ``compile:`` fallback.

    Under a ``builder_mode`` the stages follow :func:`resolve_verilate_resources`: base compile fields, their mode blocks, base ``verilate`` blocks, their mode blocks.
    The key is spelled ``mem``, ``modes.cov.mem``, ``verilate.mem`` or ``modes.cov.verilate.mem`` accordingly.
    """
    origins = {}
    layers = ((suite_compile, "suite"), (tb_compile, "testbench"))
    # Stage 1: the compile fields the verilate reservation inherits.
    for layer, origin in layers:
        for name in ("mem", "time"):
            if getattr(layer, name, None) is not None:
                origins[name] = {"origin": origin, "key": name}
    # Stage 2: their mode blocks, which beat every base compile field.
    overrides = {
        origin: mode_override(layer, builder_mode, compile_block=True)
        for layer, origin in layers
    }
    for _, origin in layers:
        for name in ("mem", "time"):
            if getattr(overrides[origin], name, None) is not None:
                origins[name] = {
                    "origin": origin,
                    "key": f"modes.{builder_mode}.{name}",
                }
    # Stage 3: the verilate blocks, which beat every compile layer.
    for layer, origin in layers:
        verilate = getattr(layer, "verilate", None)
        for name in ("cpus", "mem", "time"):
            if getattr(verilate, name, None) is not None:
                origins[name] = {"origin": origin, "key": f"verilate.{name}"}
    # Stage 4: ...and their mode blocks, which beat all of the above.
    for _, origin in layers:
        verilate = getattr(overrides[origin], "verilate", None)
        for name in ("cpus", "mem", "time"):
            if getattr(verilate, name, None) is not None:
                origins[name] = {
                    "origin": origin,
                    "key": f"modes.{builder_mode}.verilate.{name}",
                }
    return origins


def verilate_build_block(tb_compile, *, builder_mode=None) -> DispatchResourcesFile:
    """One testbench's per-build verilate reservation, with this run's ``modes.<mode>`` folded in.

    The block :func:`aggregate_verilate_resources` combines. ``mem`` and ``time`` fall back to the testbench's own ``compile:`` block, since a build sized there was sized for its verilation. ``cpus`` has no fallback.
    """
    tb_compile = effective_compile_block(tb_compile, builder_mode)
    verilate = getattr(tb_compile, "verilate", None)

    def stated(name):
        own = getattr(verilate, name, None)
        return own if own is not None else getattr(tb_compile, name, None)

    return DispatchResourcesFile(
        cpus=getattr(verilate, "cpus", None),
        mem=stated("mem"),
        time=stated("time"),
    )


def aggregate_verilate_resources(
    dispatch_cfg, suite_compile=None, testbenches=(), parallel=1, *, builder_mode=None
) -> tuple[JobResources, dict]:
    """:func:`aggregate_compile_resources` for the verilate job.

    Same arithmetic over the same builds, read from :func:`verilate_build_block` and floored at :func:`resolve_verilate_resources`. ``builder_mode`` applies to the blocks, the floor and the source keys.
    """
    blocks = [
        (name, verilate_build_block(block, builder_mode=builder_mode))
        for name, block in testbenches
    ]
    tb_blocks = {}
    for name, block in testbenches:
        tb_blocks.setdefault(name, block)

    def source_key(name, field_name):
        # Which of four spellings holds this source's value, keyed by testbench name (two builds of one testbench read one block).
        if name is None:
            return f"verilate.{field_name}"
        block = tb_blocks.get(name)
        override = mode_override(block, builder_mode, compile_block=True)
        prefix = f"modes.{builder_mode}."
        # Same order as verilate_build_block: mode verilate, base verilate, mode compile, base field.
        if getattr(getattr(override, "verilate", None), field_name, None) is not None:
            return f"{prefix}verilate.{field_name}"
        if getattr(getattr(block, "verilate", None), field_name, None) is not None:
            return f"verilate.{field_name}"
        if field_name == "cpus":
            # No `compile:` fallback for cpus, so nothing below can hold it.
            return f"verilate.{field_name}"
        if getattr(override, field_name, None) is not None:
            return f"{prefix}{field_name}"
        return field_name

    return _aggregate_resources(
        resolve_verilate_resources(
            dispatch_cfg, suite_compile, builder_mode=builder_mode
        ),
        blocks,
        parallel,
        floor_origins=verilate_resource_origins(
            suite_compile, builder_mode=builder_mode
        ),
        source_key=source_key,
    )


def compile_parallel_origin(suite_owned: bool, suite_path=None) -> str:
    """How to spell the key that governs ``compile.parallel``.

    ``suite_owned`` is whether the suite's ``compile:`` block set ``parallel``; ``suite_path`` is then named by basename. Without a path the root key ``cfg-dispatch.compile.parallel`` is returned.
    The build job's pool line and the advice text all use it so they agree.
    """
    if suite_owned and suite_path:
        return f"{os.path.basename(str(suite_path))} compile.parallel"
    return "cfg-dispatch.compile.parallel"


def greedy_schedule(durations, parallel):
    """Schedule ``durations`` over ``parallel`` workers in list order, each build going to the first free worker.

    Returns ``(makespan, workers, finish)``: the worker lists hold indices into ``durations`` and ``finish`` is each worker's end time.
    Right-sizing re-runs it on a proposed edit, because raising one repeated build can move less wall clock than the arithmetic predicts.
    """
    durations = list(durations)
    if not durations:
        return 0, [], []
    slots = max(1, min(int(parallel or 1), len(durations)))
    workers = [[] for _ in range(slots)]
    finish = [0] * slots
    for index, value in enumerate(durations):
        slot = min(range(slots), key=lambda k: finish[k])
        workers[slot].append(index)
        finish[slot] += value
    return max(finish), workers, finish


def aggregate_compile_resources(
    dispatch_cfg, suite_compile=None, testbenches=(), parallel=1, *, builder_mode=None
) -> tuple[JobResources, dict]:
    """The build job's reservation over the testbenches it will compile.

    One build job per suite compiles every planned config. The suite-level ``compile:`` block is whole-job and floors the result; testbench ``compile:`` blocks are per build. Per field:

    - ``cpus``: the largest per-build value; the head multiplies it by ``parallel`` afterwards.
    - ``mem``: the sum of the largest ``min(parallel, n)`` per-build values. Once any build states a ``mem``, builds that state none count as the figure the whole-job value implies for one build.
    - ``time``: the makespan of the work queue over ``parallel`` workers (see :func:`greedy_schedule`).

    Builds with no block of their own add no ``cpus`` or ``time``.
    ``testbenches`` is ``(name, tb_compile)`` once per planned build, not per testbench in the file or per selected test; ``()`` resolves the whole-job figure alone.
    ``builder_mode`` resolves each block's ``modes.<mode>`` first. Raises :class:`FatalRtlBuddyError` for an unparseable ``mem``.

    Returns the reservation and the provenance of each field::

        {field: {"origin": "testbench"|"suite"|"cfg-dispatch",
                 "testbench": name|None,
                 "sources": [{"origin": ..., "testbench": ...}, ...],
                 "aggregated": bool,
                 "contributors": [{"origin": ..., "testbench": ...}, ...]}}

    ``sources`` lists every source that independently produces the winning value and ``aggregated`` marks a sum of several builds; right-sizing withholds a ``reduce`` for either.
    """
    return _aggregate_resources(
        resolve_compile_resources(
            dispatch_cfg, suite_compile, builder_mode=builder_mode
        ),
        [
            (name, effective_compile_block(block, builder_mode))
            for name, block in testbenches
        ],
        parallel,
        floor_origins=compile_resource_origins(
            suite_compile, builder_mode=builder_mode
        ),
    )


def _aggregate_resources(
    floor, testbenches, parallel, *, floor_origins=None, source_key=None
):
    """Combine one reservation field-wise over the builds one job runs.

    The body of :func:`aggregate_compile_resources`, with the floor and provenance labels passed in so the verilate job (:func:`aggregate_verilate_resources`) shares the arithmetic.

    ``floor_origins`` is the ``{field: origin}`` map of the whole-job layer from :func:`compile_resource_origins`, or the ``{field: {"origin", "key"}}`` form from :func:`verilate_resource_origins`.
    ``source_key(name, field)`` spells the key inside a ``compile:`` block that holds a source's value (``name`` is ``None`` for the whole-job layer); the default is the field name.
    """
    parallel = max(1, int(parallel or 1))
    # Builds without a block of their own ride on the whole-job floor.
    blocks = [
        (name, block)
        for name, block in testbenches
        if any(getattr(block, f, None) is not None for f in ("cpus", "mem", "time"))
    ]
    floor_origins = floor_origins or {}
    resolved = JobResources(cpus=floor.cpus, mem=floor.mem, time=floor.time)
    origins = {}

    def _source(origin, name, field_name, key=None):
        source = {"origin": origin, "testbench": name}
        key = key or (source_key(name, field_name) if source_key else None)
        if key is not None:
            # Only where there is one, so compile sources keep their two-key shape.
            source["key"] = key
        return source

    def _floor_source(field_name):
        entry = floor_origins.get(field_name)
        if isinstance(entry, dict):
            return _source(
                entry.get("origin") or "cfg-dispatch",
                None,
                field_name,
                key=entry.get("key"),
            )
        return _source(entry or "cfg-dispatch", None, field_name)

    def _tb_source(name, field_name="mem"):
        return _source("testbench", name, field_name)

    def _dedupe(sources):
        """Collapse repeats: two planned builds can share one YAML key."""
        out = []
        for source in sources:
            if source not in out:
                out.append(source)
        return out

    def _record(field_name, winners, contributors=(), primary_value=None, **extra):
        """Record what produced this field's value.

        ``winners`` independently produce it (several means no single edit lowers it). ``contributors`` were added to reach it (several means it cannot be decomposed into one edit).
        """
        winners = _dedupe(winners) or [_floor_source(field_name)]
        # Not deduplicated: two builds sharing a YAML key still add up twice.
        contributors = list(contributors)
        origins[field_name] = {
            "origin": winners[0]["origin"],
            "testbench": winners[0]["testbench"],
            # The key inside the `compile:` block, where it is not the field name.
            **({"key": winners[0]["key"]} if "key" in winners[0] else {}),
            # Every source producing the same number, so an unappliable `reduce` is withheld.
            "sources": winners,
            # Whether the number is a sum: a whole-job suggestion written into one contributor would leave the total unchanged.
            "aggregated": len(contributors) > 1,
            "contributors": contributors,
            # The primary contributor's own value in native units (bytes, seconds), so a whole-job suggestion can be translated into the number to write.
            "contributor_value": primary_value,
            **extra,
        }

    # --- cpus: the widest single build, never below the whole-job value ---
    cpus_bids = [(block.cpus, name) for name, block in blocks if block.cpus is not None]
    resolved.cpus = max([value for value, _ in cpus_bids] + [floor.cpus])
    cpus_winners = [_tb_source(n, "cpus") for v, n in cpus_bids if v == resolved.cpus]
    if floor.cpus == resolved.cpus:
        cpus_winners.append(_floor_source("cpus"))
    _record("cpus", cpus_winners)

    # --- mem: the builds that can overlap each hold their own peak ------
    floor_mem = _compile_mem_bytes(floor.mem)
    stated_mem = [
        (_compile_mem_bytes(block.mem, testbench=name), name, str(block.mem))
        for name, block in testbenches
        if getattr(block, "mem", None) is not None
    ]
    # Once any planned build states a mem, builds that state none take the figure the whole-job value implies for one build.
    # Gated on a stated one: a suite with no per-testbench mem keeps `compile.mem` as the whole-job figure, and multiplying it would double-size it.
    implicit_mem = (
        [(floor_mem, None, floor.mem)]
        * sum(1 for _, block in testbenches if getattr(block, "mem", None) is None)
        if stated_mem and floor_mem
        else []
    )
    mem_bids = sorted(
        stated_mem + implicit_mem,
        key=lambda bid: bid[0],
        reverse=True,
    )
    # Only `parallel` builds are in flight together, so only the widest that many add up.
    overlapping = mem_bids[:parallel]
    summed_mem = sum(value for value, _, _ in overlapping)
    winning_mem = max(summed_mem, floor_mem or 0)
    mem_winners = []
    mem_contributors = []
    mem_primary = None
    if overlapping and summed_mem == winning_mem:
        # The largest contributor is the lever, but the whole set is recorded.
        top = overlapping[0][0]
        mem_winners = [
            _tb_source(n, "mem") if n else _floor_source("mem")
            for v, n, _ in overlapping
            if v == top
        ]
        mem_primary = top
        mem_contributors = [
            _tb_source(n, "mem") if n else _floor_source("mem")
            for _, n, _ in overlapping
        ]
    floor_binds_mem = floor_mem is not None and floor_mem == winning_mem
    if floor_binds_mem:
        mem_winners.append(_floor_source("mem"))
        # Keep the config's own spelling where the whole-job value binds.
        resolved.mem = floor.mem
    elif len(overlapping) == 1:
        resolved.mem = overlapping[0][2]
    elif overlapping:
        resolved.mem = format_mem(winning_mem)
    _record("mem", mem_winners, mem_contributors, mem_primary)

    # --- time: the makespan of the build job's own work queue -----------
    time_bids = [
        (time_to_seconds(block.time), name, str(block.time))
        for name, block in blocks
        if block.time is not None
    ]
    time_bids = [bid for bid in time_bids if bid[0] is not None]
    floor_time = time_to_seconds(floor.time)
    # The build job hands groups to a ThreadPool in plan order: a greedy list schedule.
    makespan, workers, finish = greedy_schedule(
        [value for value, _, _ in time_bids], parallel
    )
    # No separate longest-build floor: the makespan already contains it.
    winning_time = max(makespan, floor_time or 0)
    time_winners = []
    time_contributors = []
    time_primary = None
    if time_bids and makespan == winning_time:
        # The builds on the last-finishing worker are what the job waits for.
        # Several workers can tie at the makespan; each one's longest build is a tied source, so `reduce` is withheld.
        critical_workers = [
            group for group, done in zip(workers, finish) if done == makespan
        ]
        critical = critical_workers[0]
        time_contributors = [_tb_source(time_bids[i][1], "time") for i in critical]
        longest = max(time_bids[i][0] for i in critical)
        for group in critical_workers:
            group_longest = max(time_bids[i][0] for i in group)
            time_winners += [
                _tb_source(time_bids[i][1], "time")
                for i in group
                if time_bids[i][0] == group_longest
            ]
        time_primary = longest
        if len(critical) == 1:
            # One build decides the wall clock: keep its own spelling.
            resolved.time = time_bids[critical[0]][2]
        else:
            resolved.time = format_time(winning_time)
    if floor_time is not None and floor_time == winning_time:
        time_winners.append(_floor_source("time"))
        # The whole-job value binds: keep its spelling.
        resolved.time = floor.time
    _record(
        "time",
        time_winners,
        time_contributors,
        time_primary,
        # The queue in plan order, so right-sizing can re-run the schedule on a proposed edit. Internal to provenance.
        schedule=[(name, value) for value, name, _ in time_bids],
        parallel=parallel,
    )
    return resolved, origins
