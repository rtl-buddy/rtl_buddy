import json
import io
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import click
import pytest

from rtl_buddy.config.verible import VeribleConfigFile
from rtl_buddy.errors import FatalRtlBuddyError, FilelistError
from rtl_buddy.logging_utils import log_event, render_summary, setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools.verible import Verible
from rtl_buddy.tools.vlog_filelist import VlogFilelist
from rich.logging import RichHandler


def _console_handler():
  root_logger = logging.getLogger()
  for handler in root_logger.handlers:
    if isinstance(handler, RichHandler):
      return handler
  raise AssertionError("RichHandler not configured")


def _root_handlers():
  return list(logging.getLogger().handlers)


def test_setup_logging_default_console_level(tmp_path):
  setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
  assert _console_handler().level == logging.WARNING


def test_setup_logging_verbose_console_level(tmp_path):
  setup_logging(verbose=True, color=False, log_path=tmp_path / "rtl_buddy.log")
  assert _console_handler().level == logging.INFO


def test_setup_logging_debug_console_level(tmp_path):
  setup_logging(debug=True, color=False, log_path=tmp_path / "rtl_buddy.log")
  assert _console_handler().level == logging.DEBUG


def test_setup_logging_machine_uses_plain_console_handler(tmp_path):
  setup_logging(machine=True, log_path=tmp_path / "rtl_buddy.log")
  assert not any(isinstance(handler, RichHandler) for handler in _root_handlers())


def test_human_log_event_is_readable(tmp_path):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(color=False, log_path=log_path)
  logger = logging.getLogger("rtl_buddy.tests")

  log_event(logger, logging.INFO, "compile.start", test="basic")

  file_text = log_path.read_text()
  assert "event=" not in file_text
  assert "basic: compile started" in file_text


def test_machine_log_event_is_jsonl(tmp_path):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(machine=True, log_path=log_path)
  logger = logging.getLogger("rtl_buddy.tests")

  log_event(logger, logging.INFO, "compile.start", test="basic", run_id=1)

  payload = json.loads(log_path.read_text().strip())
  assert payload["event"] == "compile.start"
  assert payload["test"] == "basic"
  assert payload["run_id"] == 1
  assert payload["message"] == "basic #0001: compile started"


def test_human_sim_failure_message_lists_artifacts(tmp_path):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(color=False, log_path=log_path)
  logger = logging.getLogger("rtl_buddy.tests")

  log_event(
    logger,
    logging.ERROR,
    "sim.failed",
    test="basic",
    run_id=1,
    returncode=9,
    log="logs/basic_0001.log",
    err="logs/basic_0001.err",
    randseed="logs/basic_0001.randseed",
  )

  file_text = log_path.read_text()
  assert "basic #0001: simulation failed (returncode 9)" in file_text
  assert "logs/basic_0001.log" in file_text
  assert "logs/basic_0001.err" in file_text
  assert "logs/basic_0001.randseed" in file_text


def test_render_summary_logs_plain_text_once(tmp_path, capsys):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(verbose=True, color=False, log_path=log_path)
  logger = logging.getLogger("rtl_buddy.tests")

  render_summary(
    title="Test Results Summary",
    columns=[("test_name", "Test"), ("result", "Result"), ("desc", "Description")],
    rows=[{"test_name": "basic", "result": "PASS", "desc": "ok"}],
    logger=logger,
    metadata=["Builder: vcs"],
  )

  stderr = capsys.readouterr().err
  assert stderr.count("Test Results Summary") == 1
  assert "basic" in stderr

  file_text = log_path.read_text()
  assert "Test Results Summary" in file_text
  assert "Builder: vcs" in file_text
  assert "basic" in file_text


def test_display_path_prefers_relative_shorter_path():
  rb = RtlBuddy(name="rtl_buddy")
  base_dir = "/tmp/work"
  path = os.path.join(base_dir, "design", "regression", "suite.yaml")

  assert rb._display_path(path, base_dir=base_dir) == os.path.join("design", "regression", "suite.yaml")


def test_run_returns_2_on_fatal_error(monkeypatch):
  rb = RtlBuddy(name="rtl_buddy")

  def _raise_app(*, standalone_mode=False):
    raise FatalRtlBuddyError("boom")

  monkeypatch.setattr(rb, "app", _raise_app)
  assert rb.run() == 2


def test_run_returns_click_usage_error_without_traceback(monkeypatch, capsys):
  rb = RtlBuddy(name="rtl_buddy")

  def _raise_app(*, standalone_mode=False):
    raise click.UsageError("bad usage")

  monkeypatch.setattr(rb, "app", _raise_app)

  assert rb.run() == 2
  captured = capsys.readouterr()
  assert "bad usage" in captured.err
  assert "Traceback" not in captured.err


def test_help_outside_project_root_is_clean():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "test", "--help"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 0
  assert "run a simple test" in result.stdout
  assert "Traceback" not in result.stderr


def test_docs_help_outside_project_root_is_clean():
  result = subprocess.run(
    [sys.executable, "-m", "rtl_buddy", "docs", "--help"],
    cwd="/tmp",
    capture_output=True,
    text=True,
    check=False,
  )

  assert result.returncode == 0
  assert "browse bundled documentation" in result.stdout
  assert "Traceback" not in result.stderr


def test_show_git_rev_is_best_effort(monkeypatch):
  rb = RtlBuddy(name="rtl_buddy")

  class Result:
    def __init__(self, returncode, stdout=""):
      self.returncode = returncode
      self.stdout = stdout

  def _fake_run(*args, **kwargs):
    return Result(1, "")

  monkeypatch.setattr("rtl_buddy.rtl_buddy.subprocess.run", _fake_run)
  rb.show_git_rev()


def test_root_options_ignores_forwarded_help_args_after_double_dash(monkeypatch):
  rb = RtlBuddy(name="rtl_buddy")
  fake_ctx = SimpleNamespace(resilient_parsing=False, invoked_subcommand="verible")

  class FakeRootConfig:
    def __init__(self, name, builder_override=None):
      self.name = name
      self.builder_override = builder_override

    def get_builder_name(self):
      return "vcs"

  monkeypatch.setattr("rtl_buddy.rtl_buddy.setup_logging", lambda **kwargs: None)
  monkeypatch.setattr("rtl_buddy.rtl_buddy.RootConfig", FakeRootConfig)
  monkeypatch.setattr(sys, "argv", ["rtl_buddy", "verible", "syntax", "--", "-h"])

  rb.root_options(fake_ctx)

  assert isinstance(rb.root_cfg, FakeRootConfig)
  assert rb.builder == "vcs"


class DummyModelCfg:
  def __init__(self, model_path, filelist):
    self._model_path = model_path
    self._filelist = filelist

  def get_model_path(self):
    return str(self._model_path)

  def get_filelist(self):
    return list(self._filelist)


def test_vlog_filelist_missing_source_raises_fatal(tmp_path):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(color=False, log_path=log_path)
  model_path = tmp_path / "models.yaml"
  model_path.write_text("models: []\n")
  model_cfg = DummyModelCfg(model_path, ["missing.sv\n"])
  vlog_fl = VlogFilelist(name="rtl_buddy/vlog_filelist", model_cfg=model_cfg, output_path=tmp_path / "run.f")

  with pytest.raises(FilelistError, match="file does not exist"):
    vlog_fl.write_output()

  assert "filelist source missing" in log_path.read_text()


def test_verible_path_missing_is_debug_only(tmp_path):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(color=False, log_path=log_path)
  cfg = VeribleConfigFile(name="verible", path="missing/verible", extra_args={})

  result = cfg.initialise(str(tmp_path / "root_config.yaml"))

  assert result.available is False
  assert "Verible disabled" not in log_path.read_text()


def test_verible_stdout_is_preserved_verbatim(monkeypatch):
  class DummyCfg:
    def get_exe_path(self, exe_name):
      return f"/tmp/{exe_name}"

  class Result:
    def __init__(self):
      self.stdout = "module x;  \nassign y = z;\n"
      self.stderr = ""
      self.returncode = 0

  stdout = io.StringIO()
  stderr = io.StringIO()

  monkeypatch.setattr("rtl_buddy.tools.verible.subprocess.run", lambda *args, **kwargs: Result())
  monkeypatch.setattr("rtl_buddy.tools.verible.sys.stdout", stdout)
  monkeypatch.setattr("rtl_buddy.tools.verible.sys.stderr", stderr)

  verible = Verible(name="rtl_buddy/verible", cfg=DummyCfg())
  assert verible.do_exe("verible-verilog-format", []) == 0
  assert stdout.getvalue() == "module x;  \nassign y = z;\n"
  assert stderr.getvalue() == ""
