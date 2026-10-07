import json
import logging
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape as rich_escape
from rich.table import Table


RESULT_LEVEL = 25
RESULT_LEVEL_NAME = "RESULT"
DEFAULT_FILE_LOG = "rtl_buddy.log"

# File-log state shared by setup_logging and attach_file_log.
_FILE_LOG_LEVEL: int | None = None
_FILE_LOG_MACHINE: bool = False
# Paths already opened in this process: the first open truncates, later re-anchors to
# the same path append.
_OPENED_LOG_PATHS: set[str] = set()

# Verdicts hidden by --print-failures-only, and the order a summary tally lists
# verdicts.
_HIDDEN_VERDICTS = frozenset({"PASS", "SKIP", "XFAIL"})
_VERDICT_ORDER = ("PASS", "FAIL", "XFAIL", "XPASS", "SKIP", "NA")
_KNOWN_VERDICTS = frozenset(_VERDICT_ORDER)
_PRINT_FAILURES_ONLY = False


def _result(self, message, *args, **kwargs):
    if self.isEnabledFor(RESULT_LEVEL):
        self._log(RESULT_LEVEL, message, args, **kwargs)


@dataclass
class LoggingState:
    stderr_console: Console
    stdout_console: Console
    color: bool
    machine: bool
    # Level the console handler was set to; log_console_event() skips a record the
    # console already shows.
    console_level: int = logging.WARNING


_STATE: LoggingState | None = None


# Keeps RESULT records off the console; render_summary() prints the table itself.
class _ExcludeResultFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno != RESULT_LEVEL


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }

        event = getattr(record, "rtl_event", None)
        if event is not None:
            payload["event"] = event

        fields = getattr(record, "rtl_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)

        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True)


def register_logging_levels() -> None:
    logging.addLevelName(RESULT_LEVEL, RESULT_LEVEL_NAME)
    if not hasattr(logging, "RESULT"):
        logging.RESULT = RESULT_LEVEL
    if not hasattr(logging.Logger, "result"):
        logging.Logger.result = _result


def is_machine_mode() -> bool:
    return _STATE.machine if _STATE is not None else False


def set_print_failures_only(enabled: bool) -> None:
    global _PRINT_FAILURES_ONLY
    _PRINT_FAILURES_ONLY = enabled


def print_failures_only() -> bool:
    return _PRINT_FAILURES_ONLY


def _should_use_rich_console() -> bool:
    return (
        _STATE is not None and not _STATE.machine and _STATE.stderr_console.is_terminal
    )


def setup_logging(
    *,
    debug: bool = False,
    verbose: bool = False,
    color: bool = True,
    machine: bool = False,
    log_path: str | None = None,
) -> None:
    """Initialize console logging, and the file log when ``log_path`` is given.

    Commands normally call :func:`attach_file_log` later, once the command root is
    known.
    """
    register_logging_levels()

    # A new invocation: the first attach truncates again.
    _OPENED_LOG_PATHS.clear()

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    root_logger.setLevel(logging.DEBUG)

    color_enabled = color and not machine
    stderr_console = Console(stderr=True, no_color=not color_enabled)
    stdout_console = Console(stderr=False, no_color=not color_enabled)

    if machine:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        console_handler = RichHandler(
            console=stderr_console,
            show_time=False,
            show_level=True,
            show_path=debug,
            markup=False,
            rich_tracebacks=debug,
        )
        console_handler.setFormatter(logging.Formatter("%(message)s"))

    console_level = (
        logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING
    )
    console_handler.setLevel(console_level)
    console_handler.addFilter(_ExcludeResultFilter())

    root_logger.addHandler(console_handler)

    global _STATE, _FILE_LOG_LEVEL, _FILE_LOG_MACHINE
    _STATE = LoggingState(
        stderr_console=stderr_console,
        stdout_console=stdout_console,
        color=color_enabled,
        machine=machine,
        console_level=console_level,
    )
    _FILE_LOG_LEVEL = logging.DEBUG if debug else logging.INFO
    _FILE_LOG_MACHINE = machine

    if log_path is not None:
        attach_file_log(log_path)


def attach_file_log(log_path: str | Path) -> None:
    """Attach or re-anchor the file handler at ``log_path``, replacing any previous one."""
    if _FILE_LOG_LEVEL is None:
        raise RuntimeError(
            "attach_file_log() called before setup_logging(); "
            "console handlers must be initialized first"
        )

    resolved = str(Path(log_path).resolve())
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.FileHandler):
            root_logger.removeHandler(handler)
            handler.close()

    # The first open of a path truncates; re-anchors to the same path append so the
    # regression loop keeps earlier events.
    mode = "a" if resolved in _OPENED_LOG_PATHS else "w"
    _OPENED_LOG_PATHS.add(resolved)
    file_handler = logging.FileHandler(resolved, mode=mode)
    file_handler.setLevel(_FILE_LOG_LEVEL)
    if _FILE_LOG_MACHINE:
        file_handler.setFormatter(JsonLinesFormatter())
    else:
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
    root_logger.addHandler(file_handler)


def get_stderr_console() -> Console:
    if _STATE is None:
        setup_logging()
    return _STATE.stderr_console


def get_stdout_console() -> Console:
    if _STATE is None:
        setup_logging()
    return _STATE.stdout_console


def emit_console_text(
    text: str,
    *,
    style: str | None = None,
    stream: str = "stderr",
    markup: bool = True,
    soft_wrap: bool = False,
) -> None:
    console = get_stdout_console() if stream == "stdout" else get_stderr_console()
    # markup=False for text with literal square brackets (``pkg[extra]``), which Rich
    # reads as style tags.
    #
    # soft_wrap=True keeps a log-style line whole; off a terminal Rich hard-wraps at 80
    # columns, splitting job ids and progress lines.
    if is_machine_mode():
        console.print(text, highlight=False, markup=markup, soft_wrap=soft_wrap)
    else:
        console.print(
            text, style=style, highlight=False, markup=markup, soft_wrap=soft_wrap
        )


@contextmanager
def task_status(message: str, *, spinner: str = "dots"):
    """Show a spinner for a long phase, or print one plain line.

    The spinner is confined to the main thread, since a console allows only one Rich
    ``Live``. Worker threads and non-terminal runs print the message instead.
    """
    if threading.current_thread() is threading.main_thread():
        if _should_use_rich_console():
            with get_stderr_console().status(message, spinner=spinner) as status:
                yield status
            return

    emit_console_text(message)
    yield None


def _machine_field_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _machine_field_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_machine_field_value(item) for item in value]
    return str(value)


def _format_duration(duration: Any) -> str | None:
    if duration is None:
        return None
    try:
        return f"{float(duration):.2f}s"
    except (TypeError, ValueError):
        return str(duration)


def _format_elapsed(seconds: Any) -> str:
    """Compact wall-clock duration: ``45s`` / ``12m34s`` / ``1h02m03s``."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return str(seconds)
    total = max(0, total)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _format_artifacts(fields: Mapping[str, Any]) -> str:
    artifact_paths = [
        str(fields[key])
        for key in ("log", "err", "randseed", "transcript")
        if fields.get(key)
    ]
    return ", ".join(artifact_paths)


def _build_location(fields: Mapping[str, Any]) -> str:
    """The build directory spelling the compile events show.

    A shared build shows the basename (``obj_dir_<key>``); an unshared build shows the
    full path, since its basename repeats the test name.
    """
    if fields.get("shared", True):
        return str(fields.get("build_dir") or fields.get("build_path"))
    return str(fields.get("build_path") or fields.get("build_dir"))


def _sim_exit_phrase(fields: Mapping[str, Any]) -> str:
    """``exited 1`` / ``killed by signal 6`` for a sim-failure line.

    Mirrors ``tools.vlog_post.describe_sim_exit``, which this module cannot import.
    """
    code = fields.get("returncode")
    if isinstance(code, int) and code < 0:
        return f"killed by signal {-code}"
    return f"exited {code}"


def _human_message(event: str, fields: Mapping[str, Any]) -> str:
    test = fields.get("test")
    run_id = fields.get("run_id")
    run_suffix = f" #{int(run_id):04d}" if isinstance(run_id, int) else ""
    target = f"{test}{run_suffix}" if test else None

    match event:
        case "cli.start":
            return f"rtl_buddy v{fields.get('version')}"
        case "cli.context_ready":
            return f"Command {fields.get('command')} ready with builder {fields.get('builder')} in mode {fields.get('builder_mode')}"
        case "git.status":
            if fields.get("modified", 0) or fields.get("staged", 0):
                return f"git: {fields.get('branch')} | commit {fields.get('commit')} | mod {fields.get('modified')} | staged {fields.get('staged')}"
            return (
                f"git: {fields.get('branch')} | commit {fields.get('commit')} | clean"
            )
        case "artifact_lock.contended":
            # Deferred: artifact_lock imports log_event from this module.
            from .artifact_lock import _describe_holder

            holder = _describe_holder(
                {
                    "pid": fields.get("holder_pid"),
                    "command": fields.get("holder_command"),
                    "started": fields.get("holder_started"),
                }
            )
            return (
                f"Another rtl-buddy run is already using {fields.get('path')}{holder}"
            )
        case "command.test":
            return f"Running test {fields.get('test')}"
        case "command.randtest":
            replay_run_id = fields.get("replay_run_id")
            if replay_run_id is not None:
                return (
                    f"Replaying test {fields.get('test')} run #{int(replay_run_id):04d}"
                )
            return f"Running test {fields.get('test')} for {fields.get('iterations')} iterations"
        case "command.regression":
            return f"Running regression with start={fields.get('start_level')} stop={fields.get('reg_level')}"
        case "regression.config_override" | "regression.config_default":
            return f"Using regression config {fields.get('path')}"
        case "regression.suite_start":
            return f"Running suite {fields.get('suite')}"
        case "build_job.compile_failed":
            return (
                f"{fields.get('test')}: compile failed in the dispatch build "
                "job (its sim job will retry the compile and fail there)"
            )
        case "build_job.pool_configured":
            parallel = fields.get("parallel")
            requested = fields.get("parallel_requested")
            groups = fields.get("groups")
            # Configs that reached the pool, and those that never got a compile key.
            configs = fields.get("configs")
            unprepared = fields.get("unprepared")
            msg = f"Compiling {groups} distinct build(s), up to {parallel} at a time"
            notes = []
            if (
                isinstance(configs, int)
                and isinstance(groups, int)
                and configs > groups
            ):
                # Fewer builds than configs is the normal --share-build case; say so, or
                # "3 distinct builds" for 20 configs reads as 17 dropped. Only configs
                # that joined a group count.
                notes.append(
                    f"{configs} configs share {groups} keys; siblings adopt "
                    "the leader's build"
                )
            if isinstance(unprepared, int) and unprepared > 0:
                notes.append(f"{unprepared} failed preparation")
            if notes:
                msg += f" ({'; '.join(notes)})"
            if isinstance(requested, int) and isinstance(parallel, int):
                if requested > parallel:
                    # The head reserved cpus for `requested` concurrent builds but the
                    # suite has fewer distinct compile keys, so the surplus is
                    # deliberate.
                    #
                    # Name the governing key: a suite's `compile.parallel` beats
                    # cfg-dispatch's. The cfg-dispatch spelling is the fallback for
                    # older job logs.
                    origin = fields.get("parallel_origin")
                    if not isinstance(origin, str) or not origin:
                        origin = "cfg-dispatch.compile.parallel"
                    # Quote the number the named key holds. The head caps the configured
                    # value by the planned configs, so `requested` can be below the
                    # file's value.
                    configured = fields.get("parallel_configured")
                    capped = isinstance(configured, int) and configured > requested
                    msg += f" ({origin} is {configured if capped else requested}"
                    if capped:
                        # When the cap bit, `requested` is the planned-config count.
                        msg += (
                            f", capped to {requested} by the {requested} "
                            "planned configs"
                        )
                    msg += (
                        ", so the build job's cpus reservation is sized for "
                        f"{requested} — effective parallelism here is "
                        f"{parallel})"
                    )
            return msg
        case "build_job.compile_worker_error":
            return (
                f"{fields.get('test')}: the build job's compile raised "
                f"({fields.get('error')}) — counted as a compile failure so "
                "the job still exits 0 and its afterok dependents run; the "
                "test's own sim job will retry the compile"
            )
        case "build_job.build_records_failed":
            return (
                "build job: could not record per-compile telemetry in the "
                f"build result ({fields.get('error')}) — the built/failed "
                "outcome was written without it, so compile failures still "
                "map correctly; only the compile durations are missing"
            )
        case "build_job.result_json_failed":
            return (
                "build job: could not write the build result "
                f"{fields.get('path')} ({fields.get('error')}) — the job "
                "still exits 0 so its afterok dependents run, but the head "
                "cannot map a compile failure to its test; each affected "
                "simulation job recompiles and reports the failure itself"
            )
        case "build_job.machine_result_failed":
            return (
                "build job: could not emit the machine-result envelope "
                f"({fields.get('error')}) — the compiles themselves are "
                "unaffected and the job still exits 0 so its afterok "
                "dependents run, but the head sees no envelope from it"
            )
        case "build_job.failure_detail_failed":
            return (
                f"{fields.get('test')}: the build job could not record why "
                f"its compile failed ({fields.get('error')}) — the failure "
                "itself is still reported, so the test's sim job still "
                "declines to recompile; only the error text is missing from "
                "the build result"
            )
        case "compile.build_job_failed":
            run = fields.get("run_id")
            run_note = "" if run is None else f" (run {run})"
            rc = fields.get("returncode")
            rc_note = "" if rc is None else f" with exit {rc}"
            return (
                f"{fields.get('test')}{run_note}: not compiling — the build "
                f"job's compile for this test already failed{rc_note}. "
                "Recompiling here would fail the same way under the "
                "simulation reservation and overwrite the build's own "
                f"{fields.get('transcript')}, which is where the error is."
            )
        case "compile.build_stamp_rejected":
            run = fields.get("run_id")
            run_note = "" if run is None else f" (run {run})"
            reason = fields.get("reason")
            why = "" if not reason else f" ({reason})"
            what = (
                "but could not write its build stamp"
                if fields.get("stamp_unwritten")
                else (
                    "from different compile inputs than this job derived"
                    if fields.get("inputs_differ")
                    else "and its stamp still does not validate here"
                )
            )
            fix = (
                "Give the build directory's filesystem room and permissions "
                "to hold a stamp, and re-run."
                if fields.get("stamp_unwritten")
                else (
                    "Fix what drifted — a preproc generating different bytes "
                    "on this node, an edit that landed mid-run — and re-run."
                )
            )
            return (
                f"{fields.get('test')}{run_note}: not compiling — the build "
                f"job built this test {what}{why}. Recompiling would run into "
                f"{fields.get('build_dir')} under the SIMULATION reservation, "
                "which the scheduler kills for memory, and that kill would "
                f"hide the reason above. {fix}"
            )
        case "compile.stamp_write_failed":
            return (
                f"{fields.get('test')}: the compile succeeded but its build "
                f"stamp {fields.get('stamp')} could not be written "
                f"({fields.get('error')}). The build in "
                f"{fields.get('build_dir')} is usable and this compile still "
                "reports success; with nothing on disk to vouch for it, the "
                "next run recompiles, and under dispatch the gated "
                "simulation jobs decline to run rather than recompile it "
                "under their own reservation. Check the build directory's "
                "permissions and free space."
            )
        case "build_job.group_leader_unstamped":
            return (
                f"{fields.get('test')}: compiled {fields.get('group')} but "
                "left no build stamp, so same-key configs behind it cannot "
                "adopt that build and each compiles it again. See "
                "compile.stamp_write_failed above for why the stamp is "
                "missing."
            )
        case "compile.build_phase_fallback":
            reason = {
                "marker-missing": (
                    "the verilate job left no record of this compile key"
                ),
                "marker-stale": (
                    "the verilate job's record is for different compile inputs"
                ),
                "no-verilate-unsupported": (
                    "this Verilator does not support --no-verilate"
                ),
            }.get(fields.get("reason"), str(fields.get("reason")))
            return (
                f"{fields.get('test')}: verilating as well as building, "
                f"because {reason}. The compile is correct, but it runs the "
                "front end under the BUILD job's reservation while the "
                "verilate job's was paid for and unused — check "
                "cfg-dispatch.compile.verilate, or set "
                "compile.split-verilate: false."
            )
        case "compile.verilate_failed":
            return (
                f"{fields.get('test')}: the verilate job failed to verilate "
                "this test, so the build job reports it rather than running "
                "the same verilation again under its own reservation. The "
                f"errors are in {fields.get('transcript')}."
            )
        case "compile.verilate_marker_write_failed":
            return (
                f"{fields.get('test')}: the verilate job could not write "
                f"{fields.get('marker')} ({fields.get('error')}), so the "
                "build job will verilate this key as well as building it. "
                "Check the build directory's permissions and free space."
            )
        case "build_job.group_adoption_declined":
            return (
                f"{fields.get('test')}: could not adopt the build "
                f"{fields.get('leader')} made for their shared compile key "
                f"({fields.get('reason')}), so it compiles the key again. "
                "One key compiled twice in one build job is what "
                "cfg-dispatch.compile.parallel is sized against."
            )
        case "build_job.preproc_changed_compile_key":
            changed = ", ".join(str(f) for f in fields.get("changed") or []) or "?"
            return (
                f"{fields.get('test')}: declares preproc-sets-plusdefines: false, "
                f"but its preproc hook changed its compile key ({changed}). The "
                "build job's reservation counted this build with its compile key's "
                "other tests, so it may be too small; remove the declaration or "
                "stop the hook changing the key."
            )
        case "build_job.group_failure_adoption_declined":
            return (
                f"{fields.get('test')}: shares a compile key with "
                f"{fields.get('leader')}, whose compile failed, but could not "
                f"adopt that failure ({fields.get('reason')}), so it compiles "
                "the key again."
            )
        case "compile.group_failure_adopted":
            return (
                f"{fields.get('test')}: not compiling — {fields.get('leader')} "
                "has the same compile key and inputs and its compile failed "
                f"with exit {fields.get('returncode')}; see "
                f"{fields.get('transcript')}"
            )
        case "compile.build_stamp_refresh_failed":
            return (
                f"{fields.get('test')}: could not rewrite the shared build "
                f"stamp {fields.get('stamp')} ({fields.get('error')}), so this "
                "config compiles instead of adopting its group's build. Check "
                "the shared build directory's permissions and free space."
            )
        case "build_job.group_input_drift":
            return (
                f"{fields.get('test')}: shares a compile key with "
                f"{fields.get('leader')} but compiles a different "
                f"{fields.get('dependency')}. Under --share-build one key is "
                "one binary, so whichever config compiled last would decide "
                "what both of them simulate. Give this test its own compile "
                "key, or fix the preproc that rewrites a consumed input per "
                "test."
            )
        case "dispatch.binary_mismatch":
            return (
                f"{fields.get('tests')}: runs of one shared build "
                f"({fields.get('build_dir')}) did not all simulate the "
                f"same binary — {fields.get('binaries')} distinct executables "
                f"were stamped for it, over {fields.get('fingerprints')} "
                "distinct input digests. One of those runs recompiled the shared "
                "build instead of reusing it, so its neighbours may have "
                "simulated a binary that was replaced under them. Look for "
                "compile.prebuilt_stamp_invalid in those jobs' logs."
            )
        case "compile.prebuilt_stamp_invalid":
            run = fields.get("run_id")
            run_note = "" if run is None else f" (run {run})"
            reason = fields.get("reason")
            why = "" if not reason else f" ({reason})"
            return (
                f"{fields.get('test')}{run_note}: compiling despite being gated "
                "on a build job — that build's stamp did not validate"
                f"{why}, so every element of this fan-out queues on "
                f"{fields.get('build_dir')} to do the same. This compile runs "
                "under the SIMULATION reservation, which is the usual reason "
                "one is killed for memory. It writes compile.retry.log, "
                "leaving the build job's compile.log intact."
            )
        case "dispatch.compile_failed_in_build":
            return (
                f"{fields.get('test')}: compile failed in build job "
                f"{fields.get('build_job')} — counting it as a compile failure"
            )
        case "rightsize.build_advice_withheld":
            # INFO level, but the fallback renderer would drop the reason.
            reason = fields.get("reason")
            if reason == "undersampled":
                why = (
                    f"it ran for {fields.get('elapsed_s')}s, inside one "
                    f"{fields.get('interval_s')}s accounting interval, so its "
                    "cpu time was sampled at most once"
                )
            elif reason == "parallel-utilization-ambiguous":
                # Only the cpus row is withheld: a whole-job ratio is not per-build once
                # slots idle.
                #
                # Name the key that governs this job (a suite's `compile.parallel` beats
                # cfg-dispatch's); the root spelling is the fallback for older job logs.
                origin = fields.get("parallel_origin")
                if not isinstance(origin, str) or not origin:
                    origin = "cfg-dispatch.compile.parallel"
                return (
                    f"{fields.get('suite')}: no cpus advice for the build "
                    f"job: it ran up to {fields.get('parallel')} builds at "
                    f"once at {fields.get('efficiency')} cpu efficiency, and "
                    "an idle slot and an under-used compile look the same "
                    f"from outside — size {origin} "
                    "against the suite's distinct compile keys first"
                )
            elif reason == "compile-aggregate":
                # The reservation sums the planned builds, so a whole-job suggestion in
                # one of them leaves the total unchanged.
                return (
                    f"{fields.get('suite')}: no {fields.get('resource')} "
                    "reduce advice for the build job: its reservation adds "
                    "up the planned builds "
                    f"({', '.join(fields.get('paths') or [])}), and a "
                    "whole-job figure written into any one of them would "
                    "not lower the total"
                )
            elif reason == "compile-origin-tied":
                # The reservation aggregates over planned testbenches, so `max`/`sum`
                # can land on one number from two editable places.
                return (
                    f"{fields.get('suite')}: no {fields.get('resource')} "
                    "reduce advice for the build job: its reservation is "
                    "produced by more than one config value at once "
                    f"({', '.join(fields.get('paths') or [])}), and lowering "
                    "any one of them alone would leave it exactly where it is"
                )
            elif reason == "no-build-records":
                # The job was accounted for but left no envelope, as after a build job
                # killed mid-compile.
                why = (
                    "it left no record of what it built, so its elapsed time "
                    "cannot be read as the cost of a compile"
                )
            else:
                why = (
                    f"none of its {fields.get('builds')} build(s) actually "
                    "compiled — they reused their stamps, so its elapsed time "
                    "says nothing about what a real compile costs"
                )
            return f"{fields.get('suite')}: no reduce advice for the build job: {why}"
        case "result_io.annotate_failed":
            return (
                f"could not write the {fields.get('what')} into "
                f"{fields.get('path')} ({fields.get('error')}); the result "
                "itself is unaffected — the envelope keeps the content it "
                "already had"
            )
        case "rightsize.advice":
            return (
                f"{fields.get('test')}: {fields.get('resource')} reserved "
                f"{fields.get('reserved')}, peak {fields.get('peak')} "
                f"({fields.get('utilization')}) → {fields.get('direction')} "
                f"to {fields.get('suggested')}"
            )
        case "dispatch.job_tag_invalid":
            return (
                f"{fields.get('env')}={fields.get('value')!r} cannot prefix a "
                "Slurm job name: use 1-64 characters from A-Z a-z 0-9 . _ -, "
                "or unset it for untagged names."
            )
        case "dispatch.accounting_frequency_unusable":
            return (
                f"cfg-dispatch.sbatch-args sets `{fields.get('sbatch_arg')}`, "
                "which says nothing about task sampling — the rate memory "
                "right-sizing depends on. Requesting "
                f"`{fields.get('default')}` anyway; set an explicit "
                "`--acctg-freq=task=<seconds>` to control it."
            )
        case "rightsize.request_from_scheduler":
            args = fields.get("overrides") or []
            # Listed, not multiplied: sbatch combines several by its own precedence,
            # which this line does not reproduce.
            quoted = [f"`{a}`" for a in args]
            named = (
                quoted[0]
                if len(quoted) == 1
                else f"{', '.join(quoted[:-1])} and {quoted[-1]}"
            )
            return (
                f"{named} sets this job's cpu request — cfg-dispatch."
                "sbatch-args is appended after the generated flags, and the "
                "SBATCH_* environment reaches sbatch too, so the resolved "
                "cpus is not what the job asks for. CPU-efficiency advice "
                "for this suite is measured against the scheduler's own "
                "ReqCPUS instead."
            )
        case "rightsize.cpus_advice_withheld":
            return (
                f"{fields.get('suite')}: no cpus advice for "
                f"{fields.get('test')} — its {fields.get('runs')} run(s) were "
                "not all submitted with the same cpu request (a retry went "
                "out after the ambient SBATCH_* environment changed), so no "
                "single reservation or edit hint describes them all"
            )
        case "rightsize.mem_advice_unsampled":
            tests = fields.get("tests") or []
            interval = fields.get("interval_s")
            # Reached only when the user's --acctg-freq set the rate (dispatch otherwise
            # requests task=1); name that as the cause.
            if interval == float("inf"):
                cause = "task accounting is disabled (--acctg-freq task=0)"
            else:
                interval_str = (
                    f"{interval:g}" if isinstance(interval, float) else str(interval)
                )
                cause = (
                    f"they ran shorter than the {interval_str}s accounting "
                    "interval set by your cfg-dispatch.sbatch-args --acctg-freq"
                )
            return (
                f"{fields.get('suite')}: memory advice omitted for "
                f"{len(tests)} test(s) ({', '.join(map(str, tests))}) — "
                f"{cause}, so their MaxRSS was never sampled. Lower it, or "
                "drop the override for the --acctg-freq=task=1 default"
            )
        case "randtest.dispatch_ignored_for_replay":
            jobs = fields.get("jobs")
            also = f" (and --jobs {jobs})" if jobs is not None else ""
            return (
                f"--dispatch {fields.get('backend')}{also} ignored for replay "
                f"(-r {fields.get('replay_run_id')}): a single-seed replay "
                "runs locally"
            )
        case "dispatch.cancelled":
            # Job ids are the only route to a post-mortem once the head is gone.
            ids = fields.get("job_ids") or []
            id_note = f": {' '.join(map(str, ids))}" if ids else ""
            return (
                f"Cancelled {fields.get('jobs')} outstanding dispatch job(s) "
                f"on the {fields.get('backend')} backend{id_note}"
            )
        case "dispatch.orphans_found":
            # Default response to an interrupted run. It changes nothing, so the message
            # carries the evidence and the two commands that act on it.
            ids = fields.get("job_ids") or []
            return (
                f"dispatch: {fields.get('jobs')} job(s) from an earlier run of "
                f"{fields.get('suite_dir')} are still queued or running: "
                f"{' '.join(map(str, ids))} (run token "
                f"{fields.get('run_token')}, submitted by pid "
                f"{fields.get('pid')}, recorded in {fields.get('manifest')}). "
                "This run submits its own jobs beside them — re-run with "
                "--orphans adopt to collect those instead, or --orphans "
                "cancel to scancel them first"
            )
        case "dispatch.coverage_orphan_found":
            ids = fields.get("job_ids") or []
            return (
                f"dispatch: the coverage job of an earlier run of "
                f"{fields.get('command_root')} is still queued or running: "
                f"{' '.join(map(str, ids))} (run token {fields.get('run_token')}, "
                f"submitted by pid {fields.get('pid')}, recorded in "
                f"{fields.get('manifest')}). This run's coverage tail waits for it "
                "before writing cov_dir/ — re-run with --orphans cancel to "
                "scancel it first"
            )
        case "coverage.tail_awaiting_orphan":
            ids = fields.get("job_ids") or []
            return (
                f"coverage: waiting for an earlier run's coverage job "
                f"({' '.join(map(str, ids))}) to leave the {fields.get('backend')} "
                "queue before writing cov_dir/"
            )
        case "dispatch.orphans_cancelled":
            ids = fields.get("job_ids") or []
            return (
                f"dispatch: cancelled {fields.get('jobs')} job(s) left by an "
                f"earlier run of {fields.get('suite_dir')} (run token "
                f"{fields.get('run_token')}, pid {fields.get('pid')}): "
                f"{' '.join(map(str, ids))}"
            )
        case "dispatch.orphans_cancel_failed":
            # The run stops here, so the line carries what a manual `scancel` needs: the
            # jobs and how long the wait lasted.
            ids = fields.get("job_ids") or []
            return (
                f"dispatch: {fields.get('jobs')} job(s) of the interrupted "
                f"run recorded in {fields.get('manifest')} are still queued "
                f"or running {fields.get('waited_sec')}s after scancel (or "
                f"the scheduler could not be asked): "
                f"{' '.join(map(str, ids))}"
            )
        case "dispatch.orphans_adopted":
            # The build job is a structured field but is not repeated in the text; it is
            # already one of the listed live ids.
            ids = fields.get("job_ids") or []
            return (
                f"dispatch: adopting {fields.get('jobs')} job(s) from an "
                f"earlier run of {fields.get('suite_dir')} instead of "
                f"submitting new ones: {' '.join(map(str, ids))} (run token "
                f"{fields.get('run_token')}, pid {fields.get('pid')})"
            )
        case "dispatch.orphans_ignored":
            return (
                f"dispatch: ignoring --orphans {fields.get('orphans')} on the "
                f"{fields.get('backend')} backend — {fields.get('reason')}"
            )
        case "dispatch.suite_submitted":
            ids = fields.get("job_ids") or []
            build = fields.get("build_job")
            verilate = fields.get("verilate_job")
            build_note = f"build job {build}, " if build else "no build job needed, "
            if verilate:
                build_note = f"verilate job {verilate}, {build_note}"
            count = fields.get("jobs")
            plural = "" if count == 1 else "s"
            return (
                f"dispatch: {fields.get('suite')} → {build_note}"
                f"sim jobs {' '.join(map(str, ids))} "
                f"({count} job{plural} on {fields.get('backend')})"
            )
        case "dispatch.progress":
            running, pending = fields.get("running"), fields.get("pending")
            split = (
                f" ({running} running, {pending} pending)"
                if running is not None and pending is not None
                else ""
            )
            longest_job, longest_s = fields.get("longest_job"), fields.get("longest_s")
            longest = (
                f", longest running {longest_job} {_format_elapsed(longest_s)}"
                if longest_job is not None
                else ""
            )
            return (
                f"dispatch: {fields.get('remaining')}/{fields.get('total')} jobs "
                f"remaining{split}, "
                f"{_format_elapsed(fields.get('elapsed_s'))} elapsed{longest}"
            )
        case "dispatch.suite_drained":
            # "finished", never "passed": results are collected afterwards.
            return (
                f"dispatch: {fields.get('suite')} — all {fields.get('jobs')} "
                f"jobs finished ({_format_elapsed(fields.get('elapsed_s'))})"
            )
        case "dispatch.max_wait_exceeded":
            ids = fields.get("jobs") or []
            return (
                f"dispatch: still waiting on {fields.get('remaining')} of "
                f"{fields.get('total')} job(s) after cfg-dispatch.max-wait "
                f"({_format_elapsed(fields.get('max_wait'))}) on the "
                f"{fields.get('backend')} backend — cancelling the fleet: "
                f"{' '.join(map(str, ids))}"
            )
        case "dispatch.retry":
            # Name the classifier: only a license-queue kill is retried.
            target_job = fields.get("test")
            if fields.get("run_id") is not None:
                target_job = f"{target_job}:{fields.get('run_id')}"
            return (
                f"dispatch: retrying {target_job} (job {fields.get('job_id')}) "
                f"in {_format_elapsed(fields.get('delay_sec'))} — "
                f"{fields.get('classifier')}, attempt {fields.get('attempt')} "
                f"of {fields.get('attempts')}"
            )
        case "dispatch.retry_abandoned":
            # The run is still scored; only the second chance was lost.
            return (
                f"dispatch: giving up on retry attempt {fields.get('attempt')} for "
                f"{fields.get('jobs')} job(s) on the {fields.get('backend')} "
                "backend — keeping the results already collected "
                f"({fields.get('error')})"
            )
        case "dispatch.result_missing":
            state = fields.get("scheduler_state")
            state_note = f" (scheduler state {state})" if state else ""
            attempt = fields.get("attempt")
            attempt_note = f" on attempt {attempt}" if attempt and attempt > 1 else ""
            # A row about to be resubmitted is not counted as a failure yet, consistent
            # with the dispatch.retry line that follows.
            classifier = fields.get("retry_classifier")
            tail = (
                f" — {classifier}, retrying"
                if classifier
                else " — counting it as a failure"
            )
            return (
                f"Dispatch job {fields.get('job_id')} for "
                f"{fields.get('test')} produced no result{state_note}"
                f"{attempt_note}{tail}"
            )
        case "dispatch.test_artifact_collision":
            return (
                "dispatch: expanded tests "
                f"{fields.get('first_test')!r} ({fields.get('first_suite')}) and "
                f"{fields.get('second_test')!r} ({fields.get('second_suite')}) "
                f"share {fields.get('artifact_dir')}"
            )
        case "suite.skip":
            reason = "skip reason unavailable"
            if fields.get("reason") == "above_regression_level":
                reason = f"test level {fields.get('test_level')} above regression level {fields.get('reg_level')}"
            elif fields.get("reason") == "below_start_level":
                reason = f"test level {fields.get('test_level')} below start level {fields.get('start_level')}"
            return f"{fields.get('test')}: skipped ({reason})"
        case "sweep.completed":
            return f"{fields.get('test')}: sweep expanded to {fields.get('expanded')} tests"
        case "sweep.failed":
            return f"{fields.get('test')}: sweep failed ({fields.get('error')})"
        case "preproc.completed":
            return f"{fields.get('test')}: preproc completed"
        case "preproc.failed":
            return f"{fields.get('test')}: preproc failed ({fields.get('error')})"
        case "hook.stdout":
            # A hook's print(), re-framed with a script prefix because it
            # interleaves with rtl_buddy output on stderr.
            stage = fields.get("stage")
            script = fields.get("script")
            name = Path(str(script)).name if script else "hook"
            label = f"{stage} {name}" if stage else name
            return f"[{label}] {fields.get('line')}"
        case "preproc.import_collision":
            return (
                f"{fields.get('test')}: preproc import collision "
                f"({fields.get('error')})"
            )
        case "run.early_stop":
            return f"{target or fields.get('test')}: stopped early after {fields.get('stage')}"
        case "compile.plusdefines":
            return (
                f"{fields.get('test')}: compile plusdefines {fields.get('plusdefines')}"
            )
        case "sim.plusargs":
            return f"{fields.get('test')}: simulation plusargs {fields.get('plusargs')}"
        case "compile.start":
            return f"{target or 'compile'}: compile started"
        case "compile.completed":
            return f"{target or 'compile'}: compile completed in {_format_duration(fields.get('duration_sec'))}"
        case "compile.failed":
            artifacts = _format_artifacts(fields)
            suffix = f"; artifacts: {artifacts}" if artifacts else ""
            return f"{target or 'compile'}: compile failed (returncode {fields.get('returncode')}){suffix}"
        case "compile.builder_missing":
            return f"{fields.get('test')}: builder executable missing ({fields.get('executable')})"
        case "compile.build_reused":
            # Age first: a stale reuse raises the question of whether the build predates
            # an edit. An unknown age is stated.
            age = fields.get("stamp_age_sec")
            built = (
                f"built {_format_elapsed(age)} ago"
                if age is not None
                else "age unknown"
            )
            toolchain = fields.get("toolchain")
            if toolchain is not None:
                built = f"{built}, {toolchain}"
            shared = "" if fields.get("shared", True) else "un"
            return (
                f"{target or 'compile'}: reused {shared}shared build "
                f"{_build_location(fields)} ({built}); nothing compiled"
            )
        case "compile.verilate_reused":
            # Verilate-job counterpart of build_reused: there is no stamp yet, so the
            # marker shows this key's front end already ran.
            return (
                f"{target or 'compile'}: already verilated into "
                f"{_build_location(fields)}; nothing to verilate"
            )
        case "compile.rebuild_forced":
            # Counterpart of build_reused under --rebuild: says whether the build
            # recompiled (once per build dir).
            return (
                f"{target or 'compile'}: --rebuild given, compiling "
                f"{_build_location(fields)} even though a stamp may validate"
            )
        case "compile.hash_root":
            # DEBUG: the root that gates content hashing decides whether an out-of-suite
            # edit invalidates a stamp.
            origin = (
                "from root_config" if fields.get("derived") else "suite-dir fallback"
            )
            return (
                f"{target or 'compile'}: content-hash root "
                f"{fields.get('project_root')} ({origin})"
            )
        case "compile.build_lock_wait":
            # Logged before the wait so a long compile does not look like a hang. The
            # holder comes from the lock file: advisory, possibly stale or absent, so
            # the sentence stands without it.
            #
            # Deferred: artifact_lock imports log_event from this module.
            from .artifact_lock import _describe_holder

            holder = _describe_holder(
                {
                    "pid": fields.get("holder_pid"),
                    "test": fields.get("holder_test"),
                    "started": fields.get("holder_started"),
                }
            )
            # Repeated every few minutes; elapsed time is appended from the second line
            # on.
            waited = fields.get("waited_sec") or 0
            return (
                f"{target or 'compile'}: waiting for another rtl-buddy "
                f"process{holder} to finish compiling "
                f"{_build_location(fields)}"
                + (f" ({waited}s so far)" if waited else "")
            )
        case "compile.build_lock_unavailable":
            # A filesystem that cannot flock (read-only, some NFS) must not fail the
            # build; say which guarantee is lost.
            return (
                f"{target or 'compile'}: could not lock "
                f"{_build_location(fields)} ({fields.get('error')}); "
                "compiling without it — concurrent rtl-buddy processes "
                "populating this build directory are not serialised"
            )
        case "compile.build_dir_scrubbed":
            # The objects were dropped, not the directory, so the C++ build that follows
            # is a full one.
            why = {
                "rebuild": "--rebuild given",
                "toolchain-changed": (
                    f"built by {fields.get('was')}, now {fields.get('now')}"
                ),
                "toolchain-unrecorded": "no record of the Verilator that built it",
            }.get(fields.get("reason"), fields.get("reason"))
            return (
                f"{target or 'compile'}: dropped {fields.get('removed')} stale "
                f"object/dependency files from {fields.get('build_path')} "
                f"({why}); the C++ build starts clean"
            )
        case "compile.build_toolchain_changed":
            return (
                f"{target or 'compile'}: the shared build was compiled by "
                f"{fields.get('was')} but this run resolves "
                f"{fields.get('now')} — rebuilding rather than reusing it"
            )
        case "compile.share_build_unsupported":
            return (
                f"{fields.get('test')}: --share-build cannot share this build "
                f"({fields.get('reason') or fields.get('simulator')}); "
                "it compiles per test"
            )
        case "compile.toplevel_conflict":
            # WARNING: a builder opt naming a different top than the testbench silently
            # elaborates the wrong design.
            #
            # `configured` is absent for a bare flag; say so instead of printing None.
            configured = fields.get("configured")
            pin = (
                f"{fields.get('flag')} {configured}"
                if configured is not None
                else f"{fields.get('flag')} with no value"
            )
            return (
                f"{target or 'compile'}: builder opts pin {pin}, which "
                f"overrides this testbench's toplevel: "
                f"{fields.get('toplevel')} — the configured top is used"
            )
        case "dispatch.compile_mem_unparseable":
            return (
                f"cfg-dispatch compile mem {fields.get('mem')!r} is not a "
                "value Slurm understands (expected e.g. 512M / 16G), so it "
                "cannot be folded into an in-job compile's reservation"
            )
        case "dispatch.cancel_failed":
            return (
                f"dispatch: scancel failed for {fields.get('jobs')} "
                f"(rc {fields.get('returncode')}) — those jobs are still "
                f"queued and need cancelling by hand: {fields.get('error')}"
            )
        case "dispatch.build_submitted":
            # `parallel` is mentioned only above 1.
            parallel = fields.get("parallel") or 1
            concurrency = f" ({parallel} builds at a time)" if parallel > 1 else ""
            return (
                f"Submitted shared-build job {fields.get('job_id')} for "
                f"{fields.get('suite_dir')}{concurrency}"
            )
        case "dispatch.verilate_submitted":
            # First half of a split compile; same shape as the build-job line.
            parallel = fields.get("parallel") or 1
            concurrency = f" ({parallel} builds at a time)" if parallel > 1 else ""
            return (
                f"Submitted verilate job {fields.get('job_id')} for "
                f"{fields.get('suite_dir')}{concurrency}"
            )
        case "dispatch.build_job_deduped":
            # WARNING, and the only line explaining why this run's build job is PENDING
            # behind a job from another invocation.
            ids = fields.get("job_ids")
            joined = ", ".join(ids) if isinstance(ids, list) else str(ids)
            # "reuses it if the inputs are unchanged", not "reuses it": the waiting job
            # revalidates the stamp under the build lock, and --rebuild, an edit or
            # another builder makes it compile.
            return (
                f"A shared build for {fields.get('suite_dir')} is already queued or "
                f"running as job {joined}; this run's build job "
                f"{fields.get('job_id')} waits for it, then revalidates the shared "
                "build and reuses it if the inputs are unchanged, rather than "
                "compiling into the same directory alongside it"
            )
        case "dispatch.submitted":
            gate = fields.get("dependency")
            target = fields.get("test")
            if fields.get("run_id") is not None:
                target = f"{target}:{fields.get('run_id')}"
            return f"Submitted job {fields.get('job_id')} for {target}" + (
                f", gated on build {gate}" if gate else ""
            )
        case "dispatch.array_submitted":
            slices = fields.get("slices") or 1
            # A group too big for one array is split; say which slice this is.
            piece = f" (slice {fields.get('slice')}/{slices})" if slices > 1 else ""
            return (
                f"Submitted array job {fields.get('job_id')} "
                f"[{fields.get('array')}] for {fields.get('jobs')} test(s){piece}"
            )
        case "dispatch.max_array_size_unknown":
            return (
                "dispatch: could not read the cluster's array limits "
                f"({fields.get('error')}), so a resource group is submitted as "
                "one array — sbatch refuses a group larger than either limit; "
                "set cfg-dispatch.max-array-size (and cfg-dispatch."
                "max-array-tasks, where the cluster caps tasks per array below "
                "it) to have such groups split"
            )
        case "dispatch.wait_poll_failed":
            where = (
                f" on cluster {fields.get('cluster')}" if fields.get("cluster") else ""
            )
            return (
                f"dispatch: squeue could not be polled{where} "
                f"({fields.get('error')}); {fields.get('jobs')} job(s) are still "
                "assumed outstanding — a failed poll is not proof that they "
                "finished, so the wait keeps asking"
            )
        case "dispatch.wait_states_unfiltered":
            return (
                "dispatch: squeue rejected the job-state filter "
                f"({fields.get('dropped') or 'naming no state'}), so the wait "
                "now sees only squeue's default states (pending, running, "
                "completing); a job held in another state may be reported "
                "finished early"
            )
        case "dispatch.wait_states_narrowed":
            return (
                f"dispatch: squeue does not know the job state(s) "
                f"{fields.get('dropped')}, so the wait filters on "
                f"{fields.get('states')} instead"
            )
        case "dispatch.drained":
            return (
                f"All {fields.get('jobs')} dispatched job(s) finished on the "
                f"{fields.get('backend')} backend"
            )
        case "dispatch.job_started":
            return (
                f"Started {fields.get('kind')} job {fields.get('job_id')} "
                f"(pid {fields.get('pid')}), logging to {fields.get('log')}"
            )
        case "dispatch.job_exited":
            return (
                f"{fields.get('kind')} job {fields.get('job_id')} exited with "
                f"returncode {fields.get('returncode')}"
            )
        case "dispatch.pool_configured":
            return (
                f"Dispatching up to {fields.get('jobs')} job(s) concurrently on "
                f"the {fields.get('backend')} backend "
                f"({fields.get('cpus')} CPUs detected)"
            )
        case "dispatch.reservations_ignored":
            reserved = ", ".join(
                f"{key}={fields.get(key)}"
                for key in ("cpus", "mem", "time")
                if fields.get(key) is not None
            )
            return (
                f"Resource reservations ({reserved}) are NOT enforced by the "
                f"{fields.get('backend')} backend — one host has no portable "
                "per-process cap, so --jobs is the only limit; size it for the "
                "memory the heaviest tests need"
            )
        case "dispatch.dependency_failed":
            return (
                f"dispatch: skipping {len(fields.get('jobs') or [])} job(s) whose "
                f"shared build failed ({fields.get('jobs')}) — see the build log"
            )
        case "dispatch.dependency_never_satisfied":
            return (
                f"dispatch: cancelling {len(fields.get('jobs') or [])} job(s) whose "
                "build never succeeded — Slurm would leave them pending forever "
                f"({fields.get('jobs')})"
            )
        case "dispatch.key_released":
            tests = fields.get("tests") or []
            job_ids = fields.get("job_ids") or []
            return (
                f"dispatch: compile key {fields.get('group')} is built — "
                f"released {len(job_ids)} simulation job(s) for "
                f"{', '.join(str(name) for name in tests)} "
                f"({', '.join(str(job_id) for job_id in job_ids)}); they start "
                "now instead of waiting for the rest of the build job"
            )
        case "build_job.partial_result_failed":
            return (
                f"build job: could not update {fields.get('path')} with the "
                f"compile keys built so far ({fields.get('error')}). The "
                "simulation jobs released for those keys will find no verdict "
                "for themselves and recompile if their stamp does not "
                "validate; check the directory's permissions and free space."
            )
        case "dispatch.build_result_partial":
            state = fields.get("scheduler_state")
            state_note = "" if not state else f" (scheduler state {state})"
            return (
                f"dispatch: the build job {fields.get('job_id')} for "
                f"{fields.get('suite_dir')} did not finish{state_note} — its "
                f"result names {fields.get('decided')} of "
                f"{fields.get('planned')} planned test(s). Those ran; the "
                "rest were never compiled and their jobs were cancelled with "
                "the build. See the build log."
            )
        case "dispatch.release_skipped":
            tests = fields.get("tests") or []
            return (
                f"dispatch: compile key {fields.get('group')} is built, but "
                "its build record could not be written "
                f"({fields.get('error')}), so its {len(tests)} simulation "
                "job(s) were NOT released: they keep the dependency on this "
                "build job and start when it ends, as they did before early "
                "release existed. Nothing recompiles under a simulation "
                "reservation — holding the gate is what prevents it, and it "
                "held."
            )
        case "dispatch.gates_skipped":
            return (
                f"dispatch: {fields.get('suite_dir')}: {fields.get('reason')}. "
                "Slurm takes a dependency expression whole, so a release "
                f"would drop {fields.get('dependency')} along with this run's "
                "own gate; every simulation job waits for its build job "
                "instead."
            )
        case "dispatch.env_dependency_overridden":
            return (
                f"dispatch: {fields.get('suite_dir')}: the exported "
                f"SBATCH_DEPENDENCY ({fields.get('dependency')}) is not what "
                "gates these jobs — a --dependency on the command line "
                "overrides it, and every job gated on a build job carries "
                "one. Early release stays on; put the expression in "
                "cfg-dispatch.sbatch-args if it is meant to hold them."
            )
        case "dispatch.build_result_final_write_lost":
            return (
                f"dispatch: the build job {fields.get('job_id')} for "
                f"{fields.get('suite_dir')} finished, but the write that "
                "completes its result was lost — the file still names only "
                f"{fields.get('decided')} of {fields.get('planned')} planned "
                "test(s). Every test was compiled, so a missing simulation "
                "result here is an ordinary missing result; only the "
                "compile-reservation advice is dropped. Check the "
                "filesystem's free space and permissions."
            )
        case "dispatch.gates_unavailable":
            return (
                "dispatch: no gates manifest at "
                f"{fields.get('path')} ({fields.get('reason')}), so each "
                "compile key's simulation jobs wait for the whole build job "
                "as before. The head writes it after its last submission; a "
                "head that was killed mid-fan-out never got there."
            )
        case "dispatch.gates_write_failed":
            return (
                f"dispatch: could not write the gates manifest "
                f"{fields.get('path')} for {fields.get('suite_dir')} "
                f"({fields.get('error')}), so this suite's simulation jobs "
                "stay gated on its whole build job. The jobs themselves are "
                "submitted and correct; only the early start is lost."
            )
        case "dispatch.release_unavailable":
            return (
                f"dispatch: {fields.get('reason')}. `scontrol` is an optional "
                "Slurm binary and it has to be on the PATH of the compute "
                "node running the build job, not just the submit host."
            )
        case "dispatch.release_failed":
            skipped = fields.get("skipped")
            job_note = (
                "" if fields.get("job_id") is None else f" of {fields.get('job_id')}"
            )
            tail = (
                ""
                if not skipped
                else (
                    f" {skipped} further job(s) were not attempted, and no "
                    "later compile key in this build job will try either — "
                    "they all keep their afterok gate and start when it ends."
                )
            )
            return (
                f"dispatch: could not clear the dependency{job_note} for "
                f"{fields.get('group')} ({fields.get('error')})."
                f"{tail}"
            )
        case "compile.share_build_opts_overridden":
            return (
                f"{fields.get('test')}: shared build owns the output location, "
                f"so {fields.get('dropped')} from builder-opts was dropped in "
                f"favour of {fields.get('build_dir')}"
            )
        case "compile.share_build_simv_overridden":
            return (
                f"{fields.get('test')}: shared build owns the output location, "
                f"so builder-simv {fields.get('configured')!r} was not used — "
                f"the executable is {fields.get('used')}"
            )
        case "compile.license_queued":
            # The transcript is best-effort; an absent one drops the clause instead of
            # printing "transcript: None".
            transcript = fields.get("transcript")
            return (
                f"{fields.get('test')}: compile waited in the VCS license queue — "
                f"its {_format_duration(fields.get('duration_sec'))} is not all "
                "compile work" + (f"; transcript: {transcript}" if transcript else "")
            )
        case "sim.start":
            return f"{target or 'sim'}: simulation started"
        case "sim.output_paths":
            return (
                f"{target or 'sim'}: writing artifacts to {_format_artifacts(fields)}"
            )
        case "sim.completed":
            return f"{target or 'sim'}: simulation completed in {_format_duration(fields.get('duration_sec'))}"
        case "sim.failed":
            artifacts = _format_artifacts(fields)
            suffix = f"; artifacts: {artifacts}" if artifacts else ""
            return f"{target or 'sim'}: simulation failed (returncode {fields.get('returncode')}){suffix}"
        case "sim.timeout":
            artifacts = _format_artifacts(fields)
            suffix = f"; artifacts: {artifacts}" if artifacts else ""
            license_queue_sec = fields.get("license_queue_sec")
            if license_queue_sec is not None:
                suffix = (
                    f"; license queue wait {license_queue_sec}s exceeded cap{suffix}"
                )
            return f"{target or 'sim'}: simulation timed out after {fields.get('timeout_sec')}s{suffix}"
        case "sim.license_queue":
            return f"{target or 'sim'}: queuing for a VCS license; sim timeout paused"
        case "sim.license_granted":
            return f"{target or 'sim'}: VCS license granted after {fields.get('queued_sec')}s in queue; sim timeout resumed"
        case "sim.replay_seed_missing":
            return f"{fields.get('test')}: replay seed missing at {fields.get('seed_path')}"
        case "sim.hier_seed_missing":
            return f"{target or 'sim'}: hierarchical seed file missing at {fields.get('seed_path')}"
        case "sim.seed_generated":
            return f"{target or 'sim'}: generated seed {fields.get('seed')}"
        case "sim.timeout_override":
            return f"{target or 'sim'}: using timeout override {fields.get('timeout_sec')}s"
        case "sim.timeout_extended":
            return (
                f"{target or 'sim'}: timeout extended to {fields.get('timeout_sec')}s "
                f"(+{fields.get('extra_sec')}s for builder {fields.get('builder')})"
            )
        case "postproc.completed":
            return f"{target or 'postproc'}: post-processing completed with result {fields.get('result')} ({fields.get('desc')})"
        case "postproc.no_markers":
            return f"{fields.get('test')}: no PASS/FAIL markers found in {fields.get('log')}; result is NA"
        case "sim.unknown_verdict":
            return (
                f"{fields.get('test')}: simulator {_sim_exit_phrase(fields)} and "
                "the transcript has no PASS/FAIL verdict; result is FAIL (#546)"
            )
        case "sim.stage_failed":
            return (
                f"{fields.get('test')}: simulator {_sim_exit_phrase(fields)} before "
                f"the --early-stop {fields.get('stage')} stop; result is FAIL "
                "(transcript not post-processed)"
            )
        case "postproc.conflicting_markers":
            return (
                f"{fields.get('test')}: both PASS and FAIL markers found in "
                f"{fields.get('log')}; result is {fields.get('chosen')}"
            )
        case "filelist.malformed_line":
            return (
                f'{fields.get("file")}: malformed filelist line "{fields.get("line")}"'
            )
        case "filelist.include_missing":
            return f"{fields.get('file')}: included file not found ({fields.get('include')})"
        case "filelist.directory_missing":
            return f"filelist directory missing: {fields.get('path')}"
        case "filelist.source_missing":
            return f"filelist source missing: {fields.get('path')}"
        case "filelist.path_escapes_root":
            return (
                f"{fields.get('count')} filelist source(s) resolve outside the "
                f"project root ({fields.get('root')}): {fields.get('paths')}. "
                "The sim will compile those out-of-tree files, not any copy "
                "inside your working tree — a false-green risk in nested git "
                "worktrees. Verify these paths are intended."
            )
        case "filelist.incdir_unrepresentable":
            return (
                f"{fields.get('count')} include director(ies) contain '+' and "
                f"cannot be pinned to an absolute path: {fields.get('paths')}. "
                "Filelist parsers read `+incdir+a+b` as two directories and "
                "quoting does not help, so these entries keep their spelling "
                "relative to the generated filelist and resolve against the "
                "builder's working directory instead. Remove '+' from the "
                "path to make them checkout-independent."
            )
        case "release.map_entries_dropped":
            return (
                f"{fields.get('count')} name(s) from the previous release's map "
                "are now preserved or collide with a preserved name, so their "
                f"released spelling changes: {fields.get('names')}"
            )
        case "release.iface_lexical":
            return (
                f"the obfuscator cannot parse external file {fields.get('path')}; "
                "its module, port and parameter names are read from the module "
                "headers instead"
            )
        case "release.token_paste_preserved":
            return (
                f"{fields.get('count')} name(s) formed by macro token pasting "
                f"ship unobfuscated (patterns: {fields.get('patterns')})"
            )
        case "release.names_forced_clear":
            return (
                f"{fields.get('count')} design unit name(s) ship unobfuscated "
                "because a file left in the clear uses them: "
                f"{fields.get('names')}"
            )
        case "release.verify_unconfigured":
            return (
                "release.yaml has no `verify:` section: the package is built "
                "but never run, and it is not archived as a release"
            )
        case "release.not_archived":
            return (
                f"trial release ({fields.get('reason')}): the name map and "
                f"manifest stay in {fields.get('path')} and are not archived"
            )
        case "release.verify_stage":
            verdict = "passed" if fields.get("passed") else "FAILED"
            return (
                f"release verification {verdict} in stage {fields.get('stage')} "
                f"(exit {fields.get('returncode')}); log: {fields.get('log')}"
            )
        case "release.done":
            return (
                f"release written to {fields.get('path')}; name map {fields.get('map')}"
            )
        case "fpv.filelist_define_reserved":
            return (
                f"ignoring `+define+{fields.get('define')}` from the model "
                f"filelist: `{fields.get('name')}` is set by rtl-buddy for "
                "every formal run and cannot be overridden"
            )
        case "fpv.filelist_define_unquotable":
            return (
                f"ignoring `+define+{fields.get('define')}` from the model "
                "filelist: the value contains whitespace, and a yosys script "
                "line is tokenised on whitespace with no quoting that "
                "survives, so the define cannot be expressed"
            )
        case "fpv.filelist_define_redefined":
            return (
                f"`{fields.get('name')}` is defined more than once in the "
                f"model filelist: keeping `{fields.get('kept')}`, dropping "
                f"`{fields.get('dropped')}` — the two frontends disagree "
                "about which duplicate wins, so the last definition is used "
                "for both"
            )
        case "filelist.write_done":
            return f"Wrote filelist to {fields.get('output')}"
        case "verible.path_missing":
            return f"Verible disabled: path not found at {fields.get('path')}"
        case "verible.path_fallback":
            return (
                f"cfg-verible[{fields.get('name')}]: configured path "
                f"{fields.get('configured_path')} does not exist; falling back to "
                f"{fields.get('resolved_path')} from PATH. A deliberate pin is not "
                f"being honoured — fix the path or drop it to silence this."
            )
        case "verible.path_incomplete":
            resolved = fields.get("resolved_path")
            tail = (
                f"falling back to {resolved} from PATH"
                if resolved
                else f"and {fields.get('exe')} is not on PATH either"
            )
            return (
                f"cfg-verible[{fields.get('name')}]: configured path "
                f"{fields.get('configured_path')} exists but does not contain "
                f"{fields.get('exe')}; {tail}. A deliberate pin is not being "
                f"honoured — fix the path or drop it to silence this."
            )
        case "verible.exe_fallback":
            return (
                f"cfg-verible[{fields.get('name')}]: {fields.get('exe')} not found at "
                f"{fields.get('configured_path')}; using "
                f"{fields.get('resolved_path')} from PATH instead."
            )
        case "verible.command":
            return f"Running {fields.get('executable')}"
        case "verible.completed":
            return f"{fields.get('executable')}: completed with returncode {fields.get('returncode')}"
        case "verible.unavailable":
            return "verible binaries unavailable"
        case "verible.command_invalid":
            return f'verible: invalid command "{fields.get("command")}"'
        case "verible.model_files":
            return (
                f"--model {', '.join(fields.get('models', []))}: "
                f"{fields.get('files')} source file(s)"
                f" ({fields.get('excluded')} excluded)"
            )
        case "verible.model_files_empty":
            return (
                f"--model {', '.join(fields.get('models', []))} expanded to no"
                " source files — every entry was a -v/-y library file, a"
                " +directive, or matched an exclude glob"
            )
        case "lint_suite_config.load_failed":
            return f"failed to load lint.yaml at {fields.get('path')}: {fields.get('error')}"
        case "lint_suite_config.duplicate_check":
            return (
                f"{fields.get('path')}: duplicate lint check name "
                f'"{fields.get("name")}"'
            )
        case "lint_suite_config.checks_malformed":
            return (
                f"{fields.get('path')}: checks section malformed: {fields.get('error')}"
            )
        case "lint_suite_config.check_missing":
            return (
                f'lint check "{fields.get("check")}" not found in {fields.get("path")}'
            )
        case "lint_reg_config.load_failed":
            return (
                f"failed to load lint regression config at {fields.get('path')}: "
                f"{fields.get('error')}"
            )
        case "verible.exclude_without_model":
            return "--exclude only filters --model expansion; no --model given, so it has no effect"
        case "wave.nvim_plugin_missing":
            return (
                'nvim plugin not installed — run "rb nvim-install" to enable the hub'
                " connection and wave annotations"
                f" (expected: {fields.get('path')})"
            )
        case "wave.trace_missing":
            return f"waveform trace not found: {fields.get('path')}"
        case "wave.trace_open_failed":
            return f"could not open waveform trace {fields.get('path')}: {fields.get('error')}"
        case "pywellen.api_missing":
            return (
                f"pywellen {fields.get('version')} lacks the random-access Waveform API "
                f"{fields.get('tool')} requires (missing: {fields.get('missing')}) — "
                f"reinstall with 'pywellen{fields.get('supported')}' (#263)"
            )
        case "wave.value_reader.api_error":
            return (
                "waveform value reader failed on "
                f"{fields.get('path')} ({fields.get('error')}) — annotations will"
                " be blank; check the installed pywellen (#263)"
            )
        case "saif.read_failed":
            return (
                f"could not read waveform from {fields.get('path')}: "
                f"{fields.get('error')}"
            )
        case "wcp.resolve_failed":
            return f'WCP: could not find source for "{fields.get("variable")}" (searched {fields.get("searched")} files)'
        case "wcp.fatal_error":
            return (
                f"WCP: fatal error — wave annotations disabled ({fields.get('error')})"
            )
        case "wcp.connection_lost":
            return f"WCP: connection lost ({fields.get('reason')}); waiting for Surfer to reconnect"
        case "synth.sdc_multi_clock":
            periods = fields.get("periods_ns", [])
            used = fields.get("used_ns")
            return (
                f"multi-clock SDC ({len(periods)} clocks: {periods} ns) — "
                f"abc constraint set to minimum {used} ns as a workaround; "
                "consider separate synth entries per clock domain"
            )
        case "synth.single_unit_ignored":
            return (
                f'single_unit: true has no effect with frontend "{fields.get("frontend")}" '
                "— it only applies to the slang frontend; set frontend: slang to use it"
            )
        case "synth.best_effort_hierarchy_ignored":
            return (
                "best_effort_hierarchy: true has no effect with frontend "
                f'"{fields.get("frontend")}" — it only applies to the slang '
                "frontend; set frontend: slang to use it"
            )
        case "synth.static_functions":
            findings = fields.get("findings") or []
            listed = "; ".join(str(f) for f in findings)
            truncated = fields.get("truncated") or 0
            if truncated:
                listed += f"; and {truncated} more"
            # Only slang miscompiles these; the legacy verilog frontend inlines per call
            # site, so `error` there is a portability notice.
            if fields.get("frontend") == "slang":
                why = (
                    "the slang frontend shares one storage location per formal "
                    "across every call site, which silently merges registers"
                )
            else:
                why = (
                    f'the "{fields.get("frontend")}" frontend inlines each call '
                    "site, so this design is correct here but not portable — "
                    "the slang frontend silently merges registers"
                )
            return (
                f'synthesis "{fields.get("synth")}": {fields.get("count")} '
                "function/task declaration(s) without an explicit automatic "
                f"lifetime — {listed}; {why}. Add `automatic`, or set synth "
                "option static-functions: warn|allow to proceed"
            )
        case "synth.static_function":
            return (
                f"{fields.get('path')}:{fields.get('line')}: "
                f"{fields.get('kind')} {fields.get('subroutine')} has static "
                "lifetime (no explicit `automatic`); its formals are shared "
                "storage and are not portable across synthesis frontends"
            )
        case "synth.filelist_defines_overridden":
            overridden = fields.get("overridden") or []
            return (
                f'synthesis "{fields.get("synth")}": {fields.get("count")} '
                "+define+ entr(ies) in the generated filelist are overridden "
                "by the synth.yaml entry's `defines:` — "
                + ", ".join(str(o) for o in overridden)
                + ". Synthesis elaborates with the synth.yaml value, the "
                "simulation flow with the filelist's; a bare filelist entry "
                "has no single value to compare (empty under Verilator and "
                "read_verilog, 1 under Icarus and slang). Drop one of the two "
                "if the flows are meant to agree"
            )
        case "synth.conflicting_drivers":
            return (
                f'synthesis "{fields.get("synth")}": {fields.get("count")} '
                '"multiple conflicting drivers" warning(s) in '
                f"{fields.get('log')} — a net with incompatible drivers folds "
                "to x and takes its downstream logic with it. Fix the design, "
                "or set synth option conflicting-drivers: allow to proceed"
            )
        case "synth.unresolved_interfaces":
            listed = ", ".join(str(i) for i in (fields.get("instances") or []))
            return (
                f'synthesis "{fields.get("synth")}": {fields.get("count")} '
                f"interface instance(s) Yosys could not bind to the interface "
                f"port they are passed to — {listed} (see "
                f"{fields.get('log')}). The instance's own port connections "
                "are dropped from the netlist, so an interface carrying its "
                "clock or reset leaves them undriven. Elaborate with "
                "frontend: slang, or set synth option unresolved-interfaces: "
                "warn|allow to proceed"
            )
        case "synth.unresolved_interface":
            return (
                f"interface instance {fields.get('module')}."
                f"{fields.get('instance')} could not be bound to the interface "
                f"port it is passed to; its own port connections "
                "are dropped from the netlist, leaving any clock or reset it "
                "carries undriven. frontend: slang binds it correctly"
            )
        case "synth.abc_delay_preset":
            return (
                f'synth-args of "{fields.get("synth")}" request a +/choices/ carry map, so its '
                "Liberty-mapped run uses the delay ABC preset (no &dch -f); set abc-script: default "
                "to keep the area-oriented script"
            )
        case "synth.abc_args_ignored":
            return (
                f'abc-args "{fields.get("abc_args")}" has no effect on the '
                f'Liberty-mapped run "{fields.get("synth")}"; set abc-script to '
                "change its ABC script"
            )
        case "synth.sdc_no_clock":
            return f'no create_clock found in SDC "{fields.get("sdc")}"; abc runs unconstrained'
        case "synth.sdc_period_unevaluated":
            return (
                f"create_clock -period {fields.get('value')} in SDC "
                f'"{fields.get("sdc")}" line {fields.get("line")} did not '
                "evaluate to a number (the tokenizer backend evaluates no Tcl "
                "at all; the interp cannot resolve a design query); that clock "
                "is skipped for the abc timing constraint"
            )
        case "constraints.tokenizer_skipped":
            return (
                f'constraint file "{fields.get("source")}" line {fields.get("line")}: '
                f'"{fields.get("command")}" uses Tcl the constraint reader does not '
                f"evaluate ({', '.join(fields.get('features', []))}); commands that "
                "depend on it may be read incompletely"
            )
        case "constraints.tcl_unavailable":
            return (
                "no Tcl interpreter is reachable from this Python "
                f"({fields.get('error')}), so SDC/XDC files are read with the "
                "word tokenizer instead of a Tcl interpreter — $variables and "
                "[expr] stay unevaluated. "
                f"{'; '.join(fields.get('hints', []))}"
            )
        case "constraints.tcl_error":
            return (
                f'constraint file "{fields.get("source")}" line {fields.get("line")}: '
                f"Tcl refused to evaluate it ({fields.get('message')}); falling "
                "back to the word tokenizer, which does not evaluate "
                "$variables or [expr]"
            )
        case "constraints.include_unsupported":
            return (
                f'constraint file "{fields.get("source")}" line {fields.get("line")}: '
                f"`source {fields.get('included')}` is not supported — the "
                "constraint reader evaluates one file in a safe interpreter and "
                "includes are not followed; read the included file directly"
            )
        case "synth.openroad.no_lef":
            return (
                f'OpenROAD synthesis "{fields.get("synth")}" requires LEF files; '
                "set tech-lef / macro-lef on the referenced cfg-pdks entry "
                "or lef-paths on the synth.yaml entry"
            )
        case "synth.openroad.no_library":
            return (
                f'OpenROAD synthesis "{fields.get("synth")}" requires a mapped library; '
                "set platform: <name> in synth.yaml and define a cfg-synth-platforms "
                "entry pointing at a cfg-pdks corner"
            )
        # A whole-suite `rb pnr` skips a run whose blocks failed: its abstract would be
        # missing or stale.
        case "pnr_suite.blocked":
            blocks = ", ".join(f"'{b}'" for b in fields.get("blocks") or [])
            return (
                f'pnr "{fields.get("pnr")}": not run — block {blocks} did not '
                "pass, so there is no abstract of it to assemble"
            )
        case "pnr_suite.run_error":
            return (
                f'pnr "{fields.get("pnr")}": did not finish — '
                f"{fields.get('error')}; the other runs' results are kept"
            )
        # `rb pnr-export`: each of these stops the export before KLayout launches and
        # names the piece of the saved result at fault.
        case "pnr_export.no_def":
            return (
                f'pnr export "{fields.get("pnr")}": no routed DEF at '
                f"{fields.get('path')} — run rb pnr for this entry first, or "
                "point --def at the DEF to export"
            )
        case "pnr_export.empty_def":
            return (
                f'pnr export "{fields.get("pnr")}": the routed DEF '
                f"{fields.get('path')} is empty — the run that wrote it did "
                "not finish"
            )
        case "pnr_export.def_unreadable":
            return (
                f'pnr export "{fields.get("pnr")}": {fields.get("path")} has '
                "no DESIGN statement, so it is not a DEF this can stream out"
            )
        case "pnr_export.def_stale":
            return (
                f'pnr export "{fields.get("pnr")}": {fields.get("path")} holds '
                f"design '{fields.get('found')}', not '{fields.get('expected')}' "
                "— the saved result does not belong to this run; rerun rb pnr "
                "rather than exporting it"
            )
        case "pnr_export.no_design":
            return (
                f'pnr export "{fields.get("pnr")}": cannot resolve the design '
                f'name from synth entry "{fields.get("synth")}" '
                f"({fields.get('error')}) — the export needs the synth "
                "configuration, though none of its artefacts"
            )
        case "pnr_export.no_lyp":
            return (
                f'pnr export "{fields.get("pnr")}": no layer properties file '
                f"at {fields.get('path')} (--lyp)"
            )
        case "pnr_export.no_gds":
            return (
                f'pnr export "{fields.get("pnr")}": no GDS to re-render at '
                f"{fields.get('path')} — export one before --png-only"
            )
        case "pnr_export.rerender_unverified":
            return (
                f'pnr export "{fields.get("pnr")}": no stream-out report '
                f"beside {fields.get('gds')}, so nothing vouches for that "
                "layout being complete; re-rendering it anyway"
            )
        case "pnr_export.rerender_incomplete":
            cells = fields.get("cells", [])
            named = (
                ", ".join(str(c) for c in cells) if isinstance(cells, list) else cells
            )
            return (
                f'pnr export "{fields.get("pnr")}": the layout being '
                f"re-rendered is incomplete — {fields.get('count')} cell(s) "
                f"with no layout ({named})"
            )
        case "pnr.blockages_unsupported":
            return (
                f'P&R "{fields.get("pnr")}": floorplan.blockages needs '
                f"OpenROAD's create_blockage (26Q1 or newer), which "
                f"{fields.get('exe')} does not have; upgrade OpenROAD or drop "
                "the blockages"
            )
        # Hardened-block abstracts.
        case "pnr.harden_multi_corner":
            return (
                f'P&R "{fields.get("pnr")}": harden: needs a single-corner '
                f"platform, and '{fields.get('platform')}' declares corners — "
                "an abstract carries one corner's timing model"
            )
        case "pnr.block_unresolved" | "synth.block_unresolved":
            flow = "P&R" if event.startswith("pnr") else "synthesis"
            run = fields.get("pnr") or fields.get("synth")
            return f'{flow} "{run}": {fields.get("reason")}'
        case "pnr.liberty_time_unit_error" | "synth.liberty_time_unit_error":
            flow = "P&R" if event.startswith("pnr") else "synthesis"
            run = fields.get("pnr") or fields.get("synth")
            return (
                f'{flow} "{run}": {fields.get("error")}; the run stops because '
                "OpenSTA reports, and reads the SDC, in a single time unit"
            )
        case "pnr.block_params_unrecorded":
            return (
                f'P&R "{fields.get("pnr")}": the hardened block\'s parameter values '
                f"could not be recorded ({fields.get('error')}); parents fall back "
                "to checking its synthesis params:"
            )
        case "blocks.check_warning":
            return f"blocks: {fields.get('warning')}"
        case "pnr.block_netlist_failed":
            return f'P&R "{fields.get("pnr")}": {fields.get("error")}'
        case "pnr.block_power_failed":
            errors = fields.get("errors") or []
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            first = errors[0] if errors else ""
            return f'P&R "{fields.get("pnr")}": block power: {first}{more}'
        case "pnr.block_power_warning":
            return f'P&R "{fields.get("pnr")}": block power: {fields.get("detail")}'
        case "pnr.block_params_mismatch" | "synth.block_params_mismatch":
            flow = "P&R" if event.startswith("pnr") else "synthesis"
            run = fields.get("pnr") or fields.get("synth")
            return f'{flow} "{run}": {fields.get("error")}'
        case "pnr.block_stale_accepted" | "synth.block_stale_accepted":
            flow = "P&R" if event.startswith("pnr") else "synthesis"
            run = fields.get("pnr") or fields.get("synth")
            changes = "; ".join(str(c) for c in (fields.get("changes") or [])[:3])
            return (
                f'{flow} "{run}": using stale abstract of block '
                f"{fields.get('block')!r} (--accept-stale): {changes}"
            )
        case "pnr.abstract_failed":
            return (
                f'P&R "{fields.get("pnr")}": no abstract published — '
                f"{fields.get('reason')}; see {fields.get('log')}"
            )
        case "pnr_export.no_checkpoint":
            return (
                f'pnr export "{fields.get("pnr")}": --checkpoint '
                f"{fields.get('checkpoint')} cannot be exported: "
                f"{fields.get('reason')}"
            )
        # Stage checkpoints: the retained-checkpoint line says where they are and which
        # step the run stopped in.
        case "pnr.checkpoints_retained":
            stages = fields.get("stages") or []
            saved = (
                f"last checkpoint {stages[-1]}" if stages else "no checkpoint written"
            )
            # A step that finished "ok" is the last one the flow got through; an
            # untraced command after it (a blockage, a user Tcl snippet) stopped the
            # run.
            step = fields.get("step") or "unknown"
            status = fields.get("step_status") or "unknown"
            where = (
                f"after step {step}" if status == "ok" else f"in step {step} ({status})"
            )
            return (
                f'P&R "{fields.get("pnr")}" failed {where}; {saved}; '
                f"checkpoints kept in {fields.get('dir')}"
            )
        case "pnr.checkpoint_manifest_failed":
            return (
                f'P&R "{fields.get("pnr")}": could not complete the checkpoint '
                f"manifest in {fields.get('dir')} ({fields.get('error')}); "
                "progress.jsonl there still records every event"
            )
        case "pnr.checkpoint_latest_failed":
            return (
                f'P&R "{fields.get("run")}": could not point checkpoints/latest '
                f"at {fields.get('dir')} ({fields.get('error')}); the "
                "checkpoints are still written — name them as <run-id>/<stage>"
            )
        case "pnr.checkpoint_setup_failed":
            return (
                f'P&R "{fields.get("pnr")}": checkpoints were requested but '
                f"could not be set up ({fields.get('error')}); OpenROAD was "
                "not started"
            )
        case "pnr_export.failed":
            return (
                f'pnr export "{fields.get("pnr")}" did not deliver '
                f"({fields.get('status')}): {fields.get('desc')}"
            )
        # The stale half could not be withdrawn and its reports are already cleared. The
        # flows fail the run on this: the artefact directory would publish rows over
        # missing files.
        case "synth.phys_half_stale":
            return (
                f'synthesis "{fields.get("synth")}": the previous run\'s '
                "module rows could not be withdrawn from phys-model.json "
                f"({fields.get('error')}) — the synth_stat.json behind them "
                "has already been cleared, so this run stops rather than "
                "leave them standing over it"
            )
        # A macro library the configuration named but the disk lacks, and a macro no
        # library covered.
        case "power.missing_macro_inputs":
            missing = fields.get("missing") or []
            return (
                f'power run "{fields.get("power")}": {fields.get("count")} '
                "configured macro input(s) not on disk — "
                + ", ".join(str(p) for p in missing)
                + ". These come from the referenced synth/pnr run's "
                "lef-paths / lib-paths and this run's own lib-paths; a "
                "read_liberty of a path that is not there leaves the macro "
                "reporting 0 W, so the run stops instead"
            )
        # A configured OpenROAD thread count above the CPUs granted, and a `threads:`
        # value that is not a thread count.
        case "openroad.threads_capped":
            return (
                f'{fields.get("flow")} run "{fields.get("run")}": threads: '
                f"{fields.get('requested')} exceeds the {fields.get('allocation')} "
                f"CPU(s) allocated ({fields.get('allocation_source')}); OpenROAD "
                f"runs with {fields.get('allocation')} thread(s) instead. Raise "
                "the reservation, lower threads:, or use threads: auto"
            )
        case "openroad_threads.invalid":
            return (
                f"{fields.get('where')}: threads: {fields.get('value')} is not "
                "a positive integer or 'auto'"
            )
        case "power.unpowered_instances":
            cells = fields.get("cells") or []
            return (
                f'power run "{fields.get("power")}": {fields.get("count")} '
                "instance(s) have no Liberty power data and report 0 W — "
                + ", ".join(str(c) for c in cells)
                + ". The reported total covers everything else; supply the "
                "cell's Liberty through the referenced pnr/synth run's "
                "lib-paths, or through lib-paths on this power.yaml entry"
            )
        case "power.phys_half_stale":
            return (
                f'power run "{fields.get("power")}": the previous run\'s '
                "per-instance rows could not be withdrawn from "
                f"phys-model.json ({fields.get('error')}) — the per-instance "
                "report behind them has already been cleared, so this run "
                "stops rather than leave them standing over it"
            )
        # Two opposite outcomes share this event. A null `error` is a publication that
        # came out short of its per-row half. A set `error` is no publication at all
        # (lock timeout, write failure), so nothing may be said about what
        # phys-model.json holds.
        case "synth.phys_model_incomplete" if fields.get("error"):
            return (
                f'synthesis "{fields.get("synth")}": phys-model.json was not '
                f"written — publishing the physical model failed "
                f"({fields.get('error')}). The design totals are reported with "
                "the run either way; any phys-model.json and phys-manifest.json "
                "in the run's artefact directory are an earlier publication's "
                "and do not describe this run"
            )
        case "synth.phys_model_incomplete":
            return (
                f'synthesis "{fields.get("synth")}": phys-model.json has no '
                "per-module breakdown — the stat -json dump "
                f"{fields.get('stats')} was not produced or could not be read. "
                "The design totals scraped from the log are still "
                "recorded; what is missing is this run's per-module synthesis "
                "rows, so `rb phys module` has no cells or area for it — any "
                "per-instance power rows in the same model still answer"
            )
        case "power.phys_model_incomplete" if fields.get("error"):
            return (
                f'power run "{fields.get("power")}": phys-model.json was not '
                f"written — publishing the physical model failed "
                f"({fields.get('error')}). The design totals are reported with "
                "the run either way; any phys-model.json and phys-manifest.json "
                "in the run's artefact directory are an earlier publication's "
                "and do not describe this run"
            )
        case "power.phys_model_incomplete":
            return (
                f'power run "{fields.get("power")}": phys-model.json has no '
                "per-instance breakdown — the per-instance report "
                f"{fields.get('instances')} was not produced or could not be "
                "read. The design totals from the report_power Total row "
                "are still recorded; what is missing is this run's "
                "per-instance power rows, so `rb phys instance` has nothing to "
                "answer from — any per-module synthesis rows in the same model "
                "still answer"
            )
        case "synth_config.tool_overrides_unused":
            unused = fields.get("unused") or []
            return (
                f'synthesis "{fields.get("synth")}": tool_overrides key(s) '
                f"{', '.join(repr(str(k)) for k in unused)} ignored; a "
                f"tool: {fields.get('tool')} run reads only tool_overrides."
                f"{fields.get('tool')} and tool_overrides.yosys"
            )
        case "synth_config.yosys_strategy_ignored":
            return (
                f'synthesis "{fields.get("synth")}": tool_overrides.yosys.strategy '
                "is ignored; strategy selects OpenROAD resynthesis, so set it "
                "under tool_overrides.openroad"
            )
        case "synth_config.openroad_yosys_opts_ignored":
            keys = fields.get("keys") or []
            return (
                f'synthesis "{fields.get("synth")}": cfg-synth-tools openroad '
                f"opts {', '.join(str(k) for k in keys)} ignored; the Yosys stage "
                "of a tool: openroad run reads the yosys entry's opts when one "
                "exists, so set them there"
            )
        case "synth_tool_config.unknown_override":
            unknown = fields.get("unknown") or []
            accepted = fields.get("accepted") or []
            hints = fields.get("hints") or []
            msg = (
                f"tool_overrides.{fields.get('tool')} in synth.yaml: unknown key(s) "
                f"{', '.join(repr(str(k)) for k in unknown)} ignored; accepted keys "
                f"are {', '.join(str(k) for k in accepted)}"
            )
            if hints:
                msg += f" (did you mean {', '.join(str(h) for h in hints)}?)"
            return msg + (
                " — tool_overrides keys are snake_case attribute names, not the "
                "kebab-case spelling used under cfg-synth-tools.opts"
            )
        case "synth_tool_config.override_type":
            return (
                f"tool_overrides.{fields.get('tool')}.{fields.get('key')} must be "
                f"{fields.get('expected')}, got {fields.get('got')}"
            )
        case "synth_tool_config.override_not_mapping":
            return (
                f"tool_overrides.{fields.get('tool')} must be a mapping of option "
                f"name to value, got {fields.get('got')}"
            )
        case "coverage.merge.failed":
            how = (
                f"ran past cfg-coverage merge-timeout ({fields.get('timeout')} s) "
                "and was stopped"
                if fields.get("timeout") is not None
                else f"exited {fields.get('returncode')}"
            )
            return (
                f"coverage merge failed: verilator_coverage --write {how} and wrote no "
                f"{fields.get('merged_path')}; toggle, expression and "
                "functional coverage have no other source and are reported "
                "as FAIL, not UNSP"
            )
        case "coverage.tail_submitted":
            return (
                f"coverage: merge, model and LCOV exports submitted as job "
                f"{fields.get('job_id')} on {fields.get('backend')}; waiting "
                f"(log {fields.get('log')})"
            )
        case "coverage.tail_cleared_previous":
            return (
                "coverage: removed the previous run's manifest and model before "
                f"submitting the tail: {', '.join(fields.get('paths') or [])}"
            )
        case "coverage.tail_failed":
            job = fields.get("job_id")
            where = f"job {job}" if job is not None else "the job was not submitted"
            return (
                f"coverage tail failed ({where}): {fields.get('reason')}; no merge, "
                "model or manifest was written (the previous run's were removed), "
                "every test result was, and the run exits 1"
                + (f" — see {fields.get('log')}" if job is not None else "")
            )
        case "coverage.merge.degraded":
            failed = fields.get("failed_metrics") or []
            lost = ", ".join(str(metric) for metric in failed) or "no metric"
            return (
                f"coverage merge produced no merged database, so {lost} was "
                "not measured; the artefacts and every test result were "
                "written, and the run exits 1 because the requested "
                "measurement is incomplete"
            )
        case "coverage.metric.failed":
            return (
                f'coverage metric "{fields.get("metric")}" failed'
                f" for {fields.get('raw_path')}"
            )
        case "coverage.metric.summary_missing":
            return (
                f'coverage metric "{fields.get("metric")}" summary missing'
                f" for {fields.get('raw_path')}"
            )
        case "coverage.metric.unsupported":
            return (
                f'coverage metric "{fields.get("metric")}" unsupported'
                f" for {fields.get('raw_path')}"
            )
        case "filelist.inline_f_disallowed":
            return (
                f'{fields.get("file")}: -f not allowed (line: "{fields.get("line")}")'
            )
        # -- config / setup errors (logged at ERROR, immediately followed by FatalRtlBuddyError) --
        case "root_config.not_found":
            return f"root_config.yaml not found (searched {fields.get('max_levels')} levels from {fields.get('cwd')})"
        case "root_config.load_failed":
            return f'failed to load root config "{fields.get("path")}": {fields.get("error")}'
        case "root_config.reg_cfg_block_unreadable":
            return (
                f"{fields.get('path')}: cfg-rtl-reg block could not be read "
                f"({fields.get('error')}) — configured regression-manifest paths "
                "ignored, falling back to the ./<flow>_regression.yaml filename "
                "convention"
            )
        case "config.unknown_key":
            msg = (
                f"{fields.get('path')}: unknown key {fields.get('key')!r} in "
                f"{fields.get('block')} ignored"
            )
            if fields.get("suggestion"):
                msg += f" (did you mean {fields.get('suggestion')!r}?)"
            known = fields.get("known") or []
            if known:
                msg += f"; known keys are {', '.join(str(k) for k in known)}"
            return msg + (
                ". The block is read without it; a later major release will "
                "make an unknown key fatal"
            )
        case "root_config.reg_cfg_unknown_keys":
            return (
                f"{fields.get('path')}: cfg-rtl-reg has unknown key(s) "
                f"{fields.get('keys')} — ignored; the manifest-path keys are "
                f"{fields.get('known')}"
            )
        case "regression_config.load_failed":
            return f'failed to load regression config "{fields.get("path")}": {fields.get("error")}'
        case "suite_config.load_failed":
            return f'failed to load suite config "{fields.get("path")}": {fields.get("error")}'
        case "suite_config.testbench_malformed":
            return f"{fields.get('path')}: testbench section malformed: {fields.get('error')}"
        case "suite_config.testbench_missing":
            return f"{fields.get('path')}: requested testbench not found"
        case "suite_config.tests_malformed":
            return (
                f"{fields.get('path')}: tests section malformed: {fields.get('error')}"
            )
        case "suite_config.test_missing":
            return (
                f'test "{fields.get("test")}" not found in suite {fields.get("path")}'
            )
        case "model_config.load_failed":
            return f'failed to load model config "{fields.get("path")}": {fields.get("error")}'
        case "model_config.model_not_found":
            return f'model "{fields.get("model")}" not found in {fields.get("path")}'
        case "test_config.reglvl_malformed":
            return f'{fields.get("test")}: malformed reglvl (specify reglvl for builder "{fields.get("builder")}" or default)'
        case "platform.builder_missing":
            return f'builder "{fields.get("builder")}" not found in root config (os={fields.get("os")})'
        case "platform.builder_override_missing":
            return f'builder override "{fields.get("builder")}" not found in root config (os={fields.get("os")})'
        case "platform.builder_unset":
            return f"no builder configured for platform (os={fields.get('os')})"
        case "platform.verible_missing":
            return f'verible "{fields.get("verible")}" not found in config (os={fields.get("os")})'
        case "platform.tool_missing":
            return (
                f"cfg-platforms[{fields.get('os')}].{fields.get('block')}: "
                f'"{fields.get("entry")}" is not a configured entry '
                f"(available: {fields.get('available') or 'none'})"
            )
        case "platform.tool_not_routable":
            return (
                f"cfg-platforms[{fields.get('os')}].{fields.get('block')}: this "
                "block cannot be routed per platform; pin the path in the entry "
                "itself with a candidate list"
            )
        case "tool_path.unresolved_var":
            return (
                f"{fields.get('block')}[{fields.get('name')}].{fields.get('field')}: "
                f"every candidate references an unset environment variable "
                f"({fields.get('candidates')}); using it literally, which will "
                f"almost certainly fail. Set the variable (e.g. in "
                f".rtl-buddy/.env) or add a fallback candidate."
            )
        case "tool_version.platform_unknown":
            # The one cfg-tools error a typo produces: rtl_buddy.log must carry the
            # console's FatalRtlBuddyError text, not the dotted-event fallback.
            return (
                f"cfg-tools[{fields.get('name')}].platform: "
                f'"{fields.get("entry_platform")}" is not a configured '
                f"cfg-platforms os (available: {fields.get('available') or 'none'})"
            )
        case "platform.match_missing":
            return f'{fields.get("name")}: no platform config matches uname "{fields.get("uname")}"'
        case "project_path.missing_directory":
            return f"project path is not a directory: {fields.get('path')}"
        case "axi_profile_run.vcd2fst_missing":
            return (
                f"{target}: vcd2fst not on PATH — keeping the converted VCD "
                f"at {fields.get('vcd')} (works, but ~15x larger than FST; "
                "install GTKWave to get vcd2fst)"
            )
        case "cocotb.results_missing":
            return f"cocotb results file not found for {target} at {fields.get('path')} — sim may have crashed before writing results"
        case "systemc.cfg_missing":
            return f"SystemC testbench '{target}' requires cfg-systemc block in root_config.yaml"
        case "systemc.home_unresolved":
            return f"SystemC testbench '{target}' could not resolve home (set cfg-systemc.home or $SYSTEMC_HOME)"
        case "builder.mode_missing":
            return f'builder "{fields.get("builder")}": mode "{fields.get("mode")}" not in config (stage={fields.get("stage")})'
        case "builder.stage_missing":
            return f'builder "{fields.get("builder")}": stage "{fields.get("stage")}" not in mode "{fields.get("mode")}"'
        case "mut_runner.scope_graph_failed":
            return (
                f"rb mut: scope graph-ingestion for model "
                f"'{fields.get('model')}' needs rtl-buddy-view on PATH "
                f"(rtl-buddy-view exited rc={fields.get('rc')})"
            )
        case "xplr.record_missing":
            return (
                f"xplr: experiment dir '{fields.get('id')}' has no record.json "
                f"({fields.get('path')}); skipped in listing"
            )
        case "xplr.worktree_not_ignored":
            return (
                f"xplr: worktree {fields.get('path')} is inside the repo but "
                f"not gitignored — {fields.get('hint')}"
            )
        case "xplr.ledger_not_ignored":
            return (
                f"xplr: {fields.get('path')} is inside the repo but not "
                f"gitignored — {fields.get('hint')}"
            )
        case "summary":
            return fields.get("title", "Summary")
        case "cdc.emit.no_maps":
            return (
                f'cdc emit "{fields.get("analysis")}": rtl-buddy-cdc produced no '
                "domain map — cannot generate constraints (check the cdc log / tool version)"
            )
        case "cdc.emit.done":
            dst = fields.get("output") or "stdout"
            return (
                f'cdc emit "{fields.get("analysis")}": {fields.get("exceptions")} '
                f"{str(fields.get('format', '')).upper()} exception(s) -> {dst}"
            )
        case "cdc.check_xdc.no_maps":
            return (
                f'cdc check-xdc "{fields.get("analysis")}": rtl-buddy-cdc produced '
                "no domain map — cannot audit (check the cdc log / tool version)"
            )
        case "cdc.check_xdc.done":
            nb = fields.get("blockers", 0)
            verdict = "clean" if not nb else f"{nb} blocker(s)"
            return (
                f'cdc check-xdc "{fields.get("analysis")}": {verdict} '
                f"({fields.get('findings')} finding(s) vs {fields.get('xdc')})"
            )
        case "fpga.no_vivado":
            return (
                f'fpga "{fields.get("fpga")}": {fields.get("exe")!r} not found — '
                "skipping; run `rb tool-check --explain vivado` for install instructions"
            )
        case "fpga.no_openxc7":
            missing = fields.get("missing", [])
            names = ", ".join(missing) if isinstance(missing, list) else missing
            return (
                f'fpga "{fields.get("fpga")}": openXC7 toolchain incomplete '
                f"(missing: {names}) — skipping; see `rb tool-check --required-for fpga`"
            )
        case "fpga.filelist_failed":
            return (
                f'fpga "{fields.get("fpga")}": filelist error — {fields.get("error")}'
            )
        case "fpga.script_failed":
            return (
                f'fpga "{fields.get("fpga")}": flow-script generation failed — '
                f"{fields.get('error')}"
            )
        case "fpga.failed":
            return (
                f'fpga "{fields.get("fpga")}": Vivado exited with code '
                f"{fields.get('returncode')} (log: {fields.get('log')})"
            )
        case "fpga.stage_failed":
            return (
                f'fpga "{fields.get("fpga")}": stage {fields.get("stage")!r} exited '
                f"with code {fields.get('returncode')} (log: {fields.get('log')})"
            )
        case "fpga.errors_in_log":
            stage = fields.get("stage")
            where = f" in {stage}" if stage else ""
            return (
                f'fpga "{fields.get("fpga")}": {fields.get("count")} ERROR line(s)'
                f"{where} — first: {fields.get('first')} (log: {fields.get('log')})"
            )
        case "fpga.timing_gate_failed":
            wns = fields.get("wns_ns")
            wns_text = f" (WNS={wns} ns)" if wns is not None else ""
            return (
                f'fpga "{fields.get("fpga")}": timing not met{wns_text}, '
                f"{fields.get('failing_endpoints')} failing endpoint(s) — failing the "
                "run because require-timing-met is set"
            )
        case "cdc.no_vivado":
            return (
                f'cdc "{fields.get("analysis")}": {fields.get("exe")!r} not found — '
                "skipping; run `rb tool-check --explain vivado` for install instructions"
            )
        case "cdc.filelist_incdirs_unsupported":
            incdirs = fields.get("incdirs") or []
            return (
                f'cdc "{fields.get("analysis")}": {fields.get("count")} '
                "filelist +incdir+ entr(ies) cannot reach rtl-buddy-cdc, "
                "which has no include-path option: "
                + ", ".join(str(d) for d in incdirs)
                + ". A header found only through them fails with "
                "'Cannot find include file'; spell the `include relative to "
                "the including file or use the vivado cdc tool"
            )
        case "cdc.vivado_waivers_unsupported":
            return (
                f'cdc "{fields.get("analysis")}": rtl-buddy-cdc waiver files do not '
                "translate to the Vivado backend — waivers ignored; findings still "
                "carry full detail for downstream filtering"
            )
        case "hier.tool_too_old":
            installed = fields.get("installed") or "an older build"
            return (
                f"hier: the renderer rejected {fields.get('option')} — "
                f"{installed} is installed, and that option needs "
                f"rtl-buddy-sch >= {fields.get('required')}"
            )
        case "graph_config.suite_load_failed":
            return (
                f"graph: could not load {fields.get('path')} — its tests, "
                "testbenches and coverage links are missing from the graph"
            )
        case "graph_config.regression_load_failed":
            return (
                f"graph: could not load {fields.get('path')} — the "
                f"{fields.get('flow')} flow's suites are missing from the graph "
                "and their tests are not flow-stamped"
            )
        case "graph_config.node_id_conflict":
            return (
                f"graph: node id {fields.get('node')!r} claimed by both "
                f"{fields.get('first_type')} and {fields.get('second_type')} — "
                "keeping the first; rename one so the id is unique"
            )
        case "model_config.invalid_model_top":
            return (
                f"{fields.get('path')}: model {fields.get('name')!r} declares "
                f"top {fields.get('top')!r}, which is not a simple "
                "SystemVerilog identifier — the top is elaborated by every "
                "backend and also lands in artefact names and generated Tcl, "
                "so it must start with a letter or underscore and contain "
                "only letters, digits or underscore ('$' is legal SV but "
                "substitutes in the generated Tcl)"
            )
        case "fpv_config.invalid_top" | "mut_config.invalid_top":
            subject = (
                "verification" if event == "fpv_config.invalid_top" else "campaign"
            )
            return (
                f"{fields.get('path')}: {subject} {fields.get('name')!r} "
                f"declares top {fields.get('top')!r}, which is not a simple "
                "SystemVerilog identifier — this top wins over the model's "
                "and is written into the generated yosys and sby scripts, so "
                "it must start with a letter or underscore and contain only "
                "letters, digits or underscore ('$' is legal SV but "
                "substitutes in the generated Tcl)"
            )
        case "mut_config.top_override_unused":
            return (
                f"campaign {fields.get('name')!r} declares top "
                f"{fields.get('top')!r} but configures no fpv oracle — only "
                "the fpv oracle elaborates a top, so the sim oracle scores "
                "mutants through the test suite's own testbenches and this "
                "value has no effect"
            )
        case "mut_runner.fpv_top_override":
            return (
                f"campaign {fields.get('campaign')!r} elaborates the fpv "
                f"oracle at top {fields.get('top')!r} instead of "
                f"{fields.get('fpv_top')!r} declared by verification "
                f"{fields.get('verification')!r} — the campaign top wins for "
                "the baseline and every mutant"
            )
        case "model_config.invalid_model_name":
            return (
                f"{fields.get('path')}: model name {fields.get('name')!r} is "
                "not usable as a directory name — it must start with a "
                "letter, digit or underscore and contain only letters, "
                "digits, underscore, dot or hyphen"
            )
        case "graph_build.design_export_failed":
            return (
                f"graph build: rtl-buddy-view graph exited "
                f"{fields.get('returncode')} for model {fields.get('model')} — "
                f"that model's modules, instances and ports are missing from "
                f"the graph; see {fields.get('log')}"
            )
        case "graph_build.tb_export_failed":
            return (
                f"graph build: rtl-buddy-view graph exited "
                f"{fields.get('returncode')} for testbench "
                f"{fields.get('testbench')} (--tb-top {fields.get('tb_top')}) — "
                f"that testbench's own hierarchy is missing from the graph, "
                f"the DUT's is not; see {fields.get('log')}"
            )
        case "graph_build.run_export_failed":
            return (
                f"graph build: rtl-buddy-view graph exited "
                f"{fields.get('returncode')} for flow run {fields.get('run')} "
                f"(--tb-top {fields.get('top')}) — that run's checker hierarchy "
                f"is missing from the graph and its `targets` edge is left "
                f"dangling, the DUT's hierarchy is not; see {fields.get('log')}"
            )
        case "graph_build.stale_export_escapes":
            return (
                f"graph build: refusing to retract the export of model "
                f"{fields.get('model')} — {fields.get('path')} resolves to "
                f"{fields.get('resolved')}, outside "
                f"{fields.get('design_root')}; a model name is a directory "
                "name, so fix it in models.yaml or remove the symlink "
                "standing in for that directory"
            )
        case "graph_build.stale_export_not_dropped":
            return (
                f"graph build: model {fields.get('model')} declares "
                "`graph: false`, but its previous design-tier export at "
                f"{fields.get('path')} could not be removed "
                f"({fields.get('error')}) — that stale hierarchy would keep "
                "being served; fix the directory's permissions or delete it "
                "by hand"
            )
        case "graph_build.duplicate_design_model":
            return (
                f"graph build: more than one selected model is named "
                f"{fields.get('model')} ({fields.get('paths')}) — every "
                "per-model artefact path, and every selector that names a "
                "model, is keyed on that name, so their exports would "
                "overwrite each other and a lookup by name would silently "
                "pick one; rename one of them (`graph: false` does not "
                "resolve a name collision)"
            )
        case "graph_build.duplicate_design_top":
            return (
                f"graph build: models {fields.get('models')} "
                f"({fields.get('paths')}) are all rooted at top "
                f"{fields.get('top')} — `module:<top>` is a global id, so "
                "their exports would merge into one hybrid hierarchy; give "
                "them distinct `top:` roots in models.yaml, or set "
                "`graph: false` on the one that is not the design of record"
            )
        case "graph_build.tb_id_collision":
            return (
                f"graph build: {fields.get('ids')} design-tier id(s) are "
                f"claimed by more than one file (e.g. {fields.get('example')}) — "
                "the testbench copies were qualified with their suite so the "
                "merged graph keeps them apart; rename the duplicated module "
                "to make the qualification unnecessary"
            )
        case "graph_build.extract_failed":
            return (
                f"graph build: the extractor's binding tier failed "
                f"({fields.get('detail')}) — the design + config tiers were "
                "still merged and written"
            )
        case "graph_build.extract_merge_mismatch":
            return (
                f"graph build: the extractor's `merge-graphs` disagrees with "
                f"the internal merge ({fields.get('only_internal')} nodes only "
                f"ours, {fields.get('only_extract')} only theirs) — the "
                "internal merge is what was written; see graph-meta.json "
                "merge.extract_cross_check"
            )
        case "graph_bind.cocotb_module_not_found":
            return (
                f"graph build: test {fields.get('test')} names cocotb module "
                f"{fields.get('module')!r} but no {fields.get('expected')} "
                "exists — the test still binds to the DUT, but nothing was "
                "scanned for dut.<signal> accesses or golden-model imports"
            )
        case "spec_trace.fpv_reg_load_failed":
            return (
                f"{fields.get('path')}: fpv_regression.yaml would not load "
                f"({fields.get('error')}) — no formal run's `covers:` is "
                "counted, so `rb spec check-coverage` may report items as "
                "uncovered that a property does verify"
            )
        case "graph_bind.dpi_symbol_not_found":
            return (
                f"graph build: DPI import {fields.get('symbol')!r} "
                f"({fields.get('node')}) is defined by no C/C++/Python source "
                "under verif/ or spec/ — the function node stays in the graph "
                "with no implemented_by edge"
            )
        case "graph_results.overlay_rejected":
            return (
                f"graph: {fields.get('path')} is not a readable results "
                f"overlay (filetype {fields.get('filetype')!r}, schema "
                f"{fields.get('schema_version')!r}) — querying the graph "
                "without result status; re-run `rb graph results`"
            )
        case "test.result_json_write_failed":
            return (
                f"could not write the result record for {fields.get('test')} to "
                f"{fields.get('path')} ({fields.get('error')}) — the run itself "
                "is unaffected, but `rb graph results` will report it as UNKNOWN"
            )
        case "test.stale_output_unremovable":
            return (
                f"{fields.get('test')}: could not remove the previous run's "
                f"{fields.get('path')} ({fields.get('error')}) — if this run "
                "stops before rewriting it, that file is from an earlier run"
            )
        case "elab.result_json_write_failed":
            name = fields.get("model")
            if fields.get("profile") is not None:
                name = f"{name}:{fields.get('profile')}"
            return (
                f"could not write the elaboration result record for {name} to "
                f"{fields.get('path')} ({fields.get('error')}) — the run itself "
                "is unaffected"
            )
        case "test.result_json_refresh_failed":
            return (
                f"could not refresh the result record for {fields.get('test')} at "
                f"{fields.get('path')} after coverage post-processing "
                f"({fields.get('error')}) — the run itself is unaffected and the "
                "record still exists, but it names none of the coverage artefacts"
            )
        case "graph_merge.node_type_conflict":
            return (
                f"graph: node id {fields.get('node')!r} is a "
                f"{fields.get('first_type')} in one tier and a "
                f"{fields.get('second_type')} in {fields.get('tier')} — "
                "keeping the first; the two tiers disagree about what that id means"
            )
        case "pnr.dont_use_unmatched":
            patterns = ", ".join(str(p) for p in fields.get("patterns") or [])
            return (
                f"pnr {fields.get('pnr')}: dont-use-cells pattern(s) {patterns} "
                "matched no Liberty cell (STA-0122), so they exclude nothing — "
                "check them for typos"
            )
        case "pnr.no_wire_rc":
            causes = ([] if fields.get("layer_rc_tcl") else ["no layer-rc-tcl"]) + [
                str(c) for c in fields.get("codes") or []
            ]
            return (
                f"pnr {fields.get('pnr')}: CTS, placement parasitics and hold repair "
                f"saw no wire RC ({', '.join(causes)}), so the routed timing can "
                f"miss hold they never repaired; set the PDK's layer-rc-tcl, "
                f"see {fields.get('docs')}"
            )
        case "pnr.dont_use_instantiated":
            shown = [
                f"{inst} ({master})"
                for inst, master, _pattern in (fields.get("instances") or [])[:3]
            ]
            more = int(fields.get("count") or 0) - len(shown)
            tail = f" and {more} more" if more > 0 else ""
            return (
                f"pnr {fields.get('pnr')}: {fields.get('count')} instance(s) of "
                f"dont-use-cells in the routed design: {', '.join(shown)}{tail}; "
                f"every one is listed as RB-DONT-USE-VIOLATION in {fields.get('log')}"
            )
        case _:
            # Fallback for DEBUG/INFO events: "foo.bar" becomes "foo bar" plus select
            # fields. WARNING and above need a dedicated case above.
            event_text = event.replace(".", " ")
            detail_parts = []
            for key in ("path", "suite", "builder", "mode", "error", "desc"):
                value = fields.get(key)
                if value is not None:
                    detail_parts.append(f"{key}={value}")
            details = f" ({', '.join(detail_parts)})" if detail_parts else ""
            return f"{event_text}{details}"


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> str:
    sanitized_fields = {
        key: _machine_field_value(value)
        for key, value in fields.items()
        if value is not None
    }
    message = _human_message(event, sanitized_fields)
    logger.log(
        level, message, extra={"rtl_event": event, "rtl_fields": sanitized_fields}
    )
    return message


def console_level() -> int:
    """Level the console handler shows, or WARNING before setup_logging()."""
    return _STATE.console_level if _STATE is not None else logging.WARNING


def log_console_event(
    logger: logging.Logger, level: int, event: str, /, **fields: Any
) -> None:
    """Like :func:`log_event`, and also print the human message on the console.

    The console handler sits at WARNING unless ``-v``/``--debug`` raised it, so an INFO
    event never reaches a CI console, where a dispatched run's log may be the only
    artifact. Use this only for liveness events (progress, submitted job ids) and for
    output that was already on stdout and is being re-framed, such as hook ``print()``
    capture. New chatter goes through ``log_event()``.

    The print is skipped when the console would show ``level`` anyway, so ``-v`` shows
    one line. ``--machine`` behaves the same: the console stays WARNING-gated and shows
    the human message, and the JSON Lines go to the file log.
    """
    message = log_event(logger, level, event, **fields)
    if level < console_level():
        # markup=False: job ids like `1235_[1-40]` contain brackets Rich reads as style
        # tags.
        emit_console_text(message, markup=False, soft_wrap=True)


def _plain_summary_lines(
    title: str,
    columns: Iterable[tuple[str, str]],
    rows: list[Mapping[str, Any]],
    metadata: list[str] | None = None,
    footer: list[str] | None = None,
) -> list[str]:
    cols = list(columns)
    widths = {}
    for key, label in cols:
        widths[key] = len(label)
    for row in rows:
        for key, _label in cols:
            widths[key] = max(widths[key], len(str(row.get(key, ""))))

    lines = [title]
    if metadata:
        lines.extend(metadata)
    header = "  ".join(f"{label:<{widths[key]}}" for key, label in cols)
    divider = "  ".join("-" * widths[key] for key, _label in cols)
    lines.extend([header, divider])
    for row in rows:
        lines.append(
            "  ".join(f"{str(row.get(key, '')):<{widths[key]}}" for key, _label in cols)
        )
    if footer:
        lines.extend(footer)
    return lines


def _verdict_column(
    columns: list[tuple[str, str]], rows: list[Mapping[str, Any]]
) -> str | None:
    keys = {key for key, _label in columns}
    for candidate in ("result", "status"):
        if candidate in keys and any(
            _verdict_of(row, candidate).upper() in _KNOWN_VERDICTS for row in rows
        ):
            return candidate
    return None


def _verdict_of(row: Mapping[str, Any], key: str) -> str:
    return str(row.get(key, "")).strip()


def _verdict_counts(rows: list[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        verdict = _verdict_of(row, key) or "-"
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _tally_line(counts: Mapping[str, int]) -> str:
    def order(verdict: str) -> tuple[int, str]:
        upper = verdict.upper()
        if upper in _VERDICT_ORDER:
            return (_VERDICT_ORDER.index(upper), "")
        return (len(_VERDICT_ORDER), upper)

    parts = ", ".join(
        f"{counts[verdict]} {verdict}" for verdict in sorted(counts, key=order)
    )
    return f"Results: {parts} ({sum(counts.values())} total)"


def render_summary(
    *,
    title: str,
    columns: Iterable[tuple[str, str]],
    rows: list[Mapping[str, Any]],
    logger: logging.Logger,
    metadata: list[str] | None = None,
) -> None:
    """Console render honours print_failures_only; log and event keep every row."""
    cols = list(columns)
    verdict_key = _verdict_column(cols, rows)
    counts = _verdict_counts(rows, verdict_key) if verdict_key else {}

    footer = [_tally_line(counts)] if counts else []

    if verdict_key is not None and print_failures_only():
        console_rows = [
            row
            for row in rows
            if _verdict_of(row, verdict_key).upper() not in _HIDDEN_VERDICTS
        ]
    else:
        console_rows = rows

    if is_machine_mode():
        log_event(
            logger,
            RESULT_LEVEL,
            "summary",
            title=title,
            metadata=metadata or [],
            rows=rows,
            counts=counts or None,
        )
        # markup=False, as for the table cells: these lines carry user strings with
        # brackets (`tests[name=alpha].resources.cpus`).
        emit_console_text(
            "\n".join(
                _plain_summary_lines(
                    title, cols, console_rows, metadata=metadata, footer=footer
                )
            ),
            markup=False,
        )
        return

    logger.result(
        "\n"
        + "\n".join(
            _plain_summary_lines(title, cols, rows, metadata=metadata, footer=footer)
        )
    )

    # Cells, titles and captions are data, not markup: user strings such as
    # `[name=alpha]` would be parsed as style tags, so escape them.
    caption = list(metadata or []) + footer
    table = Table(title=rich_escape(title))
    if caption:
        table.caption = rich_escape("\n".join(caption))

    for key, label in cols:
        justify = "right" if key in {"run_id"} else "left"
        no_wrap = key in {"result", "run_id"}
        table.add_column(rich_escape(label), justify=justify, no_wrap=no_wrap)

    for row in console_rows:
        table.add_row(*(rich_escape(str(row.get(key, ""))) for key, _label in cols))

    get_stderr_console().print(table)
