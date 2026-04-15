from contextlib import nullcontext

from rtl_buddy.errors import FilelistError
from rtl_buddy.logging_utils import setup_logging
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.test_results import CompileFailResults, FilelistFailResults, SetupFailResults
from rtl_buddy.runner.test_runner import RunDepth, TestRunner as RtlBuddyTestRunner
from rtl_buddy.tools.vlog_sim import VlogSim


class DummyTestCfg:
  def get_name(self):
    return "basic"


class DummySim:
  def pre(self):
    return "Setup failed in preproc: boom"


class DummyPassingSim:
  def pre(self):
    return None

  def compile(self):
    return 0

  def execute(self, **_kwargs):
    return 0

  def post(self, **_kwargs):
    raise AssertionError("post() should not be called in early-stop tests")


class DummyFilelistFailSim:
  """Simulates a VlogSim whose compile() raises FilelistError due to a bad path."""
  def pre(self):
    return None

  def compile(self):
    raise FilelistError("missing file: src/foo.sv")

  def execute(self, **_kwargs):
    raise AssertionError("execute() should not be called after compile failure")

  def post(self, **_kwargs):
    raise AssertionError("post() should not be called after compile failure")


class DummySweepTest:
  def __init__(self, script_path):
    self.name = "basic"
    self._script_path = script_path

  def get_reglvl(self, _builder):
    return 0

  def get_sweep_path(self):
    return self._script_path


class DummySuiteCfg:
  def __init__(self, tests):
    self._tests = tests

  def get_tests(self, _test_name=None):
    return self._tests


def test_test_runner_returns_setup_fail_on_preproc_error(tmp_path, monkeypatch):
  setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
  runner = RtlBuddyTestRunner(
    name="rtl_buddy/testrunner",
    root_cfg=object(),
    test_cfg=DummyTestCfg(),
    rtl_builder_mode="debug",
    test_runner_mode={"sim_to_stdout": True},
    run_id=None,
    run_depth=RunDepth.POST,
  )
  monkeypatch.setattr(runner, "_create_vlog_sim", lambda: DummySim())

  result = runner.run()

  assert isinstance(result, SetupFailResults)
  assert result.results["result"] == "FAIL"
  assert "Setup failed in preproc" in result.results["desc"]


def test_test_runner_returns_setup_fail_for_all_runs_on_preproc_error(tmp_path, monkeypatch):
  setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
  runner = RtlBuddyTestRunner(
    name="rtl_buddy/testrunner",
    root_cfg=object(),
    test_cfg=DummyTestCfg(),
    rtl_builder_mode="debug",
    test_runner_mode={"sim_to_stdout": True},
    run_id=1,
    run_depth=RunDepth.POST,
  )
  monkeypatch.setattr(runner, "_create_vlog_sim", lambda: DummySim())

  results = runner.run_multiple([1, 2, 3])

  assert len(results) == 3
  assert all(isinstance(result, SetupFailResults) for result in results)


def test_sweep_failure_becomes_setup_fail_result(tmp_path):
  setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
  sweep_script = tmp_path / "sweep.py"
  sweep_script.write_text("raise RuntimeError('broken sweep')\n")

  rb = RtlBuddy(name="rtl_buddy")
  rb.builder = "vcs"
  rb.root_cfg = object()
  rb.run_depth = RunDepth.POST
  rb.rtl_builder_mode = "debug"

  suite_results = rb._do_test_suite(DummySuiteCfg([DummySweepTest(str(sweep_script))]), run_ids=[None])

  assert len(suite_results) == 1
  result = suite_results[0]["results"]
  assert isinstance(result, SetupFailResults)
  assert result.results["result"] == "FAIL"
  assert "Setup failed in sweep" in result.results["desc"]


def test_early_stop_is_logged(tmp_path, monkeypatch):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(color=False, log_path=log_path)
  runner = RtlBuddyTestRunner(
    name="rtl_buddy/testrunner",
    root_cfg=object(),
    test_cfg=DummyTestCfg(),
    rtl_builder_mode="debug",
    test_runner_mode={"sim_to_stdout": True},
    run_id=None,
    run_depth=RunDepth.PRE,
  )
  monkeypatch.setattr(runner, "_create_vlog_sim", lambda: DummyPassingSim())

  result = runner.run()

  assert result.results["desc"] == "Stopped early at preproc"
  assert "basic: stopped early after preproc" in log_path.read_text()


def test_test_runner_returns_compile_fail_on_filelist_error(tmp_path, monkeypatch):
  setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
  runner = RtlBuddyTestRunner(
    name="rtl_buddy/testrunner",
    root_cfg=object(),
    test_cfg=DummyTestCfg(),
    rtl_builder_mode="debug",
    test_runner_mode={"sim_to_stdout": True},
    run_id=None,
    run_depth=RunDepth.POST,
  )
  monkeypatch.setattr(runner, "_create_vlog_sim", lambda: DummyFilelistFailSim())

  result = runner.run()

  assert isinstance(result, FilelistFailResults)
  assert "missing file: src/foo.sv" in result.results["desc"]


def test_test_runner_run_multiple_returns_compile_fail_on_filelist_error(tmp_path, monkeypatch):
  setup_logging(color=False, log_path=tmp_path / "rtl_buddy.log")
  runner = RtlBuddyTestRunner(
    name="rtl_buddy/testrunner",
    root_cfg=object(),
    test_cfg=DummyTestCfg(),
    rtl_builder_mode="debug",
    test_runner_mode={"sim_to_stdout": True},
    run_id=1,
    run_depth=RunDepth.POST,
  )
  monkeypatch.setattr(runner, "_create_vlog_sim", lambda: DummyFilelistFailSim())

  results = runner.run_multiple([1, 2, 3])

  assert len(results) == 3
  assert all(isinstance(result, FilelistFailResults) for result in results)
  assert all("missing file: src/foo.sv" in result.results["desc"] for result in results)


class DummyBuilderCfg:
  def get_exe(self):
    return "vcs"

  def get_name(self):
    return "vcs"

  def get_compile_time_opts(self, _mode):
    return []

  def get_simulator_family(self):
    return "vcs"

  def get_simv(self):
    return "simv"

  def get_seed(self):
    return 31310

  def get_run_time_opts(self, _mode, seed):
    return ["-xlrm", "hier_inst_seed", f"+ntb_random_seed={seed}"]


class DummyRootCfg:
  def get_rtl_builder_cfg(self):
    return DummyBuilderCfg()


class DummyTestbench:
  def get_filelist(self):
    return []


class DummyExecuteTestCfg:
  pd = None
  uvm = None

  def get_name(self):
    return "basic"

  def get_testbench(self):
    return DummyTestbench()

  def get_timeout(self):
    return (1, False)

  def get_plusargs(self):
    return None


class DummyProcess:
  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, tb):
    return False

  def wait(self, _timeout):
    return 0

  def send_signal(self, _sig):
    return None


def test_vlog_sim_missing_hier_seed_file_is_nonfatal(tmp_path, monkeypatch):
  log_path = tmp_path / "rtl_buddy.log"
  setup_logging(color=False, log_path=log_path)
  monkeypatch.chdir(tmp_path)
  monkeypatch.setattr("rtl_buddy.tools.vlog_sim.task_status", lambda *args, **kwargs: nullcontext())
  monkeypatch.setattr("rtl_buddy.tools.vlog_sim.signal.signal", lambda *_args, **_kwargs: None)
  monkeypatch.setattr("rtl_buddy.tools.vlog_sim.os.setpgrp", lambda: None)
  monkeypatch.setattr("rtl_buddy.tools.vlog_sim.subprocess.Popen", lambda *args, **kwargs: DummyProcess())

  sim = VlogSim(
    name="rtl_buddy/vlog_sim",
    root_cfg=DummyRootCfg(),
    test_cfg=DummyExecuteTestCfg(),
    rtl_builder_mode="reg",
    sim_mode={"sim_to_stdout": False},
  )

  returncode = sim.execute()

  assert returncode == 0
  assert (tmp_path / "logs" / "basic.randseed").read_text() == "31310\n"
  assert "hierarchical seed file missing at HierInstanceSeed.txt" in log_path.read_text()
