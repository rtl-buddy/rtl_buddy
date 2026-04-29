import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table


RESULT_LEVEL = 25
RESULT_LEVEL_NAME = "RESULT"
DEFAULT_FILE_LOG = "rtl_buddy.log"


def _result(self, message, *args, **kwargs):
  if self.isEnabledFor(RESULT_LEVEL):
    self._log(RESULT_LEVEL, message, args, **kwargs)


@dataclass
class LoggingState:
  stderr_console: Console
  stdout_console: Console
  color: bool
  machine: bool


_STATE: LoggingState | None = None


# Prevents RESULT-level records from reaching the console handler.
# render_summary() writes the Rich table directly to stderr instead, so
# without this filter the summary would appear twice on the console.
class _ExcludeResultFilter(logging.Filter):
  def filter(self, record: logging.LogRecord) -> bool:
    return record.levelno != RESULT_LEVEL


class JsonLinesFormatter(logging.Formatter):
  def format(self, record: logging.LogRecord) -> str:
    payload = {
      "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
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


def _should_use_rich_console() -> bool:
  return _STATE is not None and not _STATE.machine and _STATE.stderr_console.is_terminal


def setup_logging(
  *,
  debug: bool = False,
  verbose: bool = False,
  color: bool = True,
  machine: bool = False,
  log_path: str = DEFAULT_FILE_LOG,
) -> None:
  register_logging_levels()

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

  console_handler.setLevel(logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING)
  console_handler.addFilter(_ExcludeResultFilter())

  file_handler = logging.FileHandler(log_path, mode="w")
  file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
  if machine:
    file_handler.setFormatter(JsonLinesFormatter())
  else:
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

  root_logger.addHandler(file_handler)
  root_logger.addHandler(console_handler)

  global _STATE
  _STATE = LoggingState(
    stderr_console=stderr_console,
    stdout_console=stdout_console,
    color=color_enabled,
    machine=machine,
  )


def get_stderr_console() -> Console:
  if _STATE is None:
    setup_logging()
  return _STATE.stderr_console


def get_stdout_console() -> Console:
  if _STATE is None:
    setup_logging()
  return _STATE.stdout_console


def emit_console_text(text: str, *, style: str | None = None, stream: str = "stderr") -> None:
  console = get_stdout_console() if stream == "stdout" else get_stderr_console()
  if is_machine_mode():
    console.print(text, highlight=False)
  else:
    console.print(text, style=style, highlight=False)


@contextmanager
def task_status(message: str, *, spinner: str = "dots"):
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


def _format_artifacts(fields: Mapping[str, Any]) -> str:
  artifact_paths = [str(fields[key]) for key in ("log", "err", "randseed", "transcript") if fields.get(key)]
  return ", ".join(artifact_paths)


def _human_message(event: str, fields: Mapping[str, Any]) -> str:
  test = fields.get("test")
  run_id = fields.get("run_id")
  run_suffix = f" #{int(run_id):04d}" if isinstance(run_id, int) else ""
  target = f"{test}{run_suffix}" if test else None

  match event:
    case "cli.start":
      return f'rtl_buddy v{fields.get("version")}'
    case "cli.context_ready":
      return f'Command {fields.get("command")} ready with builder {fields.get("builder")} in mode {fields.get("builder_mode")}'
    case "git.status":
      if fields.get("modified", 0) or fields.get("staged", 0):
        return f'git: {fields.get("branch")} | commit {fields.get("commit")} | mod {fields.get("modified")} | staged {fields.get("staged")}'
      return f'git: {fields.get("branch")} | commit {fields.get("commit")} | clean'
    case "command.test":
      return f'Running test {fields.get("test")}'
    case "command.randtest":
      replay_run_id = fields.get("replay_run_id")
      if replay_run_id is not None:
        return f'Replaying test {fields.get("test")} run #{int(replay_run_id):04d}'
      return f'Running test {fields.get("test")} for {fields.get("iterations")} iterations'
    case "command.regression":
      return f'Running regression with start={fields.get("start_level")} stop={fields.get("reg_level")}'
    case "regression.config_override" | "regression.config_default":
      return f'Using regression config {fields.get("path")}'
    case "regression.suite_start":
      return f'Running suite {fields.get("suite")}'
    case "suite.skip":
      reason = "skip reason unavailable"
      if fields.get("reason") == "above_regression_level":
        reason = f'test level {fields.get("test_level")} above regression level {fields.get("reg_level")}'
      elif fields.get("reason") == "below_start_level":
        reason = f'test level {fields.get("test_level")} below start level {fields.get("start_level")}'
      return f'{fields.get("test")}: skipped ({reason})'
    case "sweep.completed":
      return f'{fields.get("test")}: sweep expanded to {fields.get("expanded")} tests'
    case "sweep.failed":
      return f'{fields.get("test")}: sweep failed ({fields.get("error")})'
    case "preproc.completed":
      return f'{fields.get("test")}: preproc completed'
    case "preproc.failed":
      return f'{fields.get("test")}: preproc failed ({fields.get("error")})'
    case "run.early_stop":
      return f'{target or fields.get("test")}: stopped early after {fields.get("stage")}'
    case "compile.plusdefines":
      return f'{fields.get("test")}: compile plusdefines {fields.get("plusdefines")}'
    case "sim.plusargs":
      return f'{fields.get("test")}: simulation plusargs {fields.get("plusargs")}'
    case "compile.start":
      return f'{target or "compile"}: compile started'
    case "compile.completed":
      return f'{target or "compile"}: compile completed in {_format_duration(fields.get("duration_sec"))}'
    case "compile.failed":
      artifacts = _format_artifacts(fields)
      suffix = f'; artifacts: {artifacts}' if artifacts else ""
      return f'{target or "compile"}: compile failed (returncode {fields.get("returncode")}){suffix}'
    case "compile.builder_missing":
      return f'{fields.get("test")}: builder executable missing ({fields.get("executable")})'
    case "sim.start":
      return f'{target or "sim"}: simulation started'
    case "sim.output_paths":
      return f'{target or "sim"}: writing artifacts to {_format_artifacts(fields)}'
    case "sim.completed":
      return f'{target or "sim"}: simulation completed in {_format_duration(fields.get("duration_sec"))}'
    case "sim.failed":
      artifacts = _format_artifacts(fields)
      suffix = f'; artifacts: {artifacts}' if artifacts else ""
      return f'{target or "sim"}: simulation failed (returncode {fields.get("returncode")}){suffix}'
    case "sim.timeout":
      artifacts = _format_artifacts(fields)
      suffix = f'; artifacts: {artifacts}' if artifacts else ""
      return f'{target or "sim"}: simulation timed out after {fields.get("timeout_sec")}s{suffix}'
    case "sim.replay_seed_missing":
      return f'{fields.get("test")}: replay seed missing at {fields.get("seed_path")}'
    case "sim.hier_seed_missing":
      return f'{target or "sim"}: hierarchical seed file missing at {fields.get("seed_path")}'
    case "sim.seed_generated":
      return f'{target or "sim"}: generated seed {fields.get("seed")}'
    case "sim.timeout_override":
      return f'{target or "sim"}: using timeout override {fields.get("timeout_sec")}s'
    case "postproc.completed":
      return f'{target or "postproc"}: post-processing completed with result {fields.get("result")} ({fields.get("desc")})'
    case "postproc.no_markers":
      return f'{fields.get("test")}: no PASS/FAIL markers found in {fields.get("log")}; result is NA'
    case "filelist.malformed_line":
      return f'{fields.get("file")}: malformed filelist line "{fields.get("line")}"'
    case "filelist.include_missing":
      return f'{fields.get("file")}: included file not found ({fields.get("include")})'
    case "filelist.directory_missing":
      return f'filelist directory missing: {fields.get("path")}'
    case "filelist.source_missing":
      return f'filelist source missing: {fields.get("path")}'
    case "filelist.write_done":
      return f'Wrote filelist to {fields.get("output")}'
    case "verible.path_missing":
      return f'Verible disabled: path not found at {fields.get("path")}'
    case "verible.command":
      return f'Running {fields.get("executable")}'
    case "verible.completed":
      return f'{fields.get("executable")}: completed with returncode {fields.get("returncode")}'
    case "verible.unavailable":
      return "verible binaries unavailable"
    case "verible.command_invalid":
      return f'verible: invalid command "{fields.get("command")}"'
    case "coverage.metric.failed":
      return (
        f'coverage metric "{fields.get("metric")}" failed'
        f' for {fields.get("raw_path")}'
      )
    case "coverage.metric.summary_missing":
      return (
        f'coverage metric "{fields.get("metric")}" summary missing'
        f' for {fields.get("raw_path")}'
      )
    case "coverage.metric.unsupported":
      return (
        f'coverage metric "{fields.get("metric")}" unsupported'
        f' for {fields.get("raw_path")}'
      )
    case "filelist.inline_f_disallowed":
      return f'{fields.get("file")}: -f not allowed (line: "{fields.get("line")}")'
    # -- config / setup errors (logged at ERROR, immediately followed by FatalRtlBuddyError) --
    case "root_config.not_found":
      return f'root_config.yaml not found (searched {fields.get("max_levels")} levels from {fields.get("cwd")})'
    case "root_config.load_failed":
      return f'failed to load root config "{fields.get("path")}": {fields.get("error")}'
    case "regression_config.load_failed":
      return f'failed to load regression config "{fields.get("path")}": {fields.get("error")}'
    case "suite_config.load_failed":
      return f'failed to load suite config "{fields.get("path")}": {fields.get("error")}'
    case "suite_config.testbench_malformed":
      return f'{fields.get("path")}: testbench section malformed: {fields.get("error")}'
    case "suite_config.testbench_missing":
      return f'{fields.get("path")}: requested testbench not found'
    case "suite_config.tests_malformed":
      return f'{fields.get("path")}: tests section malformed: {fields.get("error")}'
    case "suite_config.test_missing":
      return f'test "{fields.get("test")}" not found in suite {fields.get("path")}'
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
      return f'no builder configured for platform (os={fields.get("os")})'
    case "platform.verible_missing":
      return f'verible "{fields.get("verible")}" not found in config (os={fields.get("os")})'
    case "platform.match_missing":
      return f'{fields.get("name")}: no platform config matches uname "{fields.get("uname")}"'
    case "project_path.missing_directory":
      return f'project path is not a directory: {fields.get("path")}'
    case "cocotb.results_missing":
      return f'cocotb results file not found for {target} at {fields.get("path")} — sim may have crashed before writing results'
    case "builder.mode_missing":
      return f'builder "{fields.get("builder")}": mode "{fields.get("mode")}" not in config (stage={fields.get("stage")})'
    case "builder.stage_missing":
      return f'builder "{fields.get("builder")}": stage "{fields.get("stage")}" not in mode "{fields.get("mode")}"'
    case "summary":
      return fields.get("title", "Summary")
    case _:
      # Fallback: converts "foo.bar" → "foo bar" and appends select fields.
      # This is fine for DEBUG/INFO events. Events logged at WARNING or above
      # should have a dedicated case above so the user sees a clear message.
      event_text = event.replace(".", " ")
      detail_parts = []
      for key in ("path", "suite", "builder", "mode", "error", "desc"):
        value = fields.get(key)
        if value is not None:
          detail_parts.append(f"{key}={value}")
      details = f" ({', '.join(detail_parts)})" if detail_parts else ""
      return f"{event_text}{details}"


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> None:
  sanitized_fields = {key: _machine_field_value(value) for key, value in fields.items() if value is not None}
  message = _human_message(event, sanitized_fields)
  logger.log(level, message, extra={"rtl_event": event, "rtl_fields": sanitized_fields})


def _plain_summary_lines(title: str, columns: Iterable[tuple[str, str]], rows: list[Mapping[str, Any]], metadata: list[str] | None = None) -> list[str]:
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
    lines.append("  ".join(f"{str(row.get(key, '')):<{widths[key]}}" for key, _label in cols))
  return lines


def render_summary(
  *,
  title: str,
  columns: Iterable[tuple[str, str]],
  rows: list[Mapping[str, Any]],
  logger: logging.Logger,
  metadata: list[str] | None = None,
) -> None:
  plain_lines = _plain_summary_lines(title, columns, rows, metadata=metadata)

  if is_machine_mode():
    log_event(logger, RESULT_LEVEL, "summary", title=title, metadata=metadata or [], rows=rows)
    emit_console_text("\n".join(plain_lines))
    return

  logger.result("\n" + "\n".join(plain_lines))

  table = Table(title=title)
  if metadata:
    table.caption = "\n".join(metadata)

  for key, label in columns:
    justify = "right" if key in {"run_id"} else "left"
    no_wrap = key in {"result", "run_id"}
    table.add_column(label, justify=justify, no_wrap=no_wrap)

  for row in rows:
    table.add_row(*(str(row.get(key, "")) for key, _label in columns))

  get_stderr_console().print(table)
