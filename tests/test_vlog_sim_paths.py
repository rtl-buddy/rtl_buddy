from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from rtl_buddy.seed_mode import SeedMode
from rtl_buddy.tools import vlog_sim as vlog_sim_module


class DummyBuilderCfg:
  def __init__(self, *, exe="vcs", simv="simv", simulator_family="vcs", compile_opts=None, run_opts=None, seed=1234):
    self.exe = exe
    self.simv = simv
    self.simulator_family = simulator_family
    self.compile_opts = compile_opts or []
    self.run_opts = run_opts or []
    self.seed = seed

  def get_exe(self):
    return self.exe

  def get_simv(self):
    return self.simv

  def get_seed(self):
    return self.seed

  def get_compile_time_opts(self, _mode):
    return list(self.compile_opts)

  def get_run_time_opts(self, _mode, seed=None):
    opts = list(self.run_opts)
    if seed is not None:
      opts.append(f"+seed={seed}")
    return opts

  def get_simulator_family(self):
    return self.simulator_family

  def get_name(self):
    return self.simulator_family


class DummyRootCfg:
  def __init__(self, builder_cfg):
    self.builder_cfg = builder_cfg

  def get_rtl_builder_cfg(self):
    return self.builder_cfg

  def get_use_lcov(self, _simulator_name):
    return False


class DummyModelCfg:
  def __init__(self, model_path):
    self.model_path = str(model_path)

  def get_model_path(self):
    return self.model_path

  def get_filelist(self):
    return []


class DummyTestbenchCfg:
  def get_filelist(self):
    return []


class DummyTestCfg:
  def __init__(self, name, model_path):
    self.name = name
    self.model = DummyModelCfg(model_path)
    self.tb = DummyTestbenchCfg()
    self.pd = None
    self.uvm = None

  def get_name(self):
    return self.name

  def get_model(self):
    return self.model

  def get_testbench(self):
    return self.tb

  def get_plusargs(self):
    return None

  def get_plusdefines(self):
    return {}

  def get_timeout(self):
    return 60, False

  def get_preproc_path(self):
    return None


def _make_sim(tmp_path, monkeypatch, *, test_name="basic", builder_cfg=None):
  monkeypatch.chdir(tmp_path)
  builder_cfg = builder_cfg or DummyBuilderCfg()
  root_cfg = DummyRootCfg(builder_cfg)
  test_cfg = DummyTestCfg(test_name, tmp_path / "models.yaml")
  return vlog_sim_module.VlogSim(
    name="rtl_buddy/vlog_sim",
    root_cfg=root_cfg,
    test_cfg=test_cfg,
    rtl_builder_mode="sim",
    sim_mode={"sim_to_stdout": True},
  )


def test_vlog_sim_paths_are_nested_under_suite_logs(tmp_path, monkeypatch):
  sim = _make_sim(tmp_path, monkeypatch)

  assert sim.suite_work_dir == str(tmp_path)
  assert sim._get_artifact_dir() == str(tmp_path / "artefacts" / "basic")
  assert sim._get_artifact_dir(run_id=1) == str(tmp_path / "artefacts" / "basic" / "run-0001")
  assert sim._get_log_path(run_id=1) == str(tmp_path / "artefacts" / "basic" / "run-0001" / "test.log")
  assert sim._get_err_path(run_id=1) == str(tmp_path / "artefacts" / "basic" / "run-0001" / "test.err")
  assert sim._get_randseed_path(run_id=1) == str(tmp_path / "artefacts" / "basic" / "run-0001" / "test.randseed")
  assert sim._get_cov_path(run_id=1) == str(tmp_path / "artefacts" / "basic" / "run-0001" / "coverage.dat")


def test_vlog_sim_resolves_relative_simv_paths_against_compile_work_dir(tmp_path, monkeypatch):
  sim = _make_sim(
    tmp_path,
    monkeypatch,
    builder_cfg=DummyBuilderCfg(exe="vcs", simv="bin/simv"),
  )

  assert sim._get_simv_path() == str(tmp_path / "artefacts" / "basic" / "bin" / "simv")


def test_vlog_sim_resolves_verilator_simv_from_build_dir(tmp_path, monkeypatch):
  sim = _make_sim(
    tmp_path,
    monkeypatch,
    builder_cfg=DummyBuilderCfg(exe="/usr/bin/verilator", simv="ignored", simulator_family="verilator"),
  )

  assert sim._get_simv_path() == str(tmp_path / "artefacts" / "basic" / "obj_dir_basic" / "simv")


def test_vlog_sim_compile_uses_explicit_filelist_path_and_suite_cwd(tmp_path, monkeypatch):
  captured = {}
  sim = _make_sim(tmp_path, monkeypatch)

  def _fake_run(cmd, capture_output, text, cwd):
    captured["cmd"] = list(cmd)
    captured["cwd"] = cwd
    return SimpleNamespace(returncode=0, stdout="", stderr="")

  monkeypatch.setattr(vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext())
  monkeypatch.setattr(vlog_sim_module.subprocess, "run", _fake_run)

  assert sim.compile() == 0
  assert captured["cwd"] == str(tmp_path / "artefacts" / "basic")
  assert captured["cmd"][-2:] == ["-f", str(tmp_path / "artefacts" / "basic" / "run.f")]
  assert (tmp_path / "artefacts" / "basic" / "run.f").is_file()


def test_vlog_sim_execute_runs_in_artifact_dir_and_updates_symlinks(tmp_path, monkeypatch):
  captured = {}
  sim = _make_sim(tmp_path, monkeypatch, builder_cfg=DummyBuilderCfg(simv="bin/simv"))

  class FakeProcess:
    def __enter__(self):
      return self

    def __exit__(self, exc_type, exc, tb):
      return False

    def wait(self, timeout):
      captured["timeout"] = timeout
      return 0

    def send_signal(self, _signal):
      captured["signal_sent"] = True

  def _fake_popen(cmd, preexec_fn, cwd, stdout, stderr):
    captured["cmd"] = list(cmd)
    captured["cwd"] = cwd
    stdout.write("PASS basic\n")
    stderr.write("")
    return FakeProcess()

  monkeypatch.setattr(vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext())
  monkeypatch.setattr(vlog_sim_module.subprocess, "Popen", _fake_popen)

  assert sim.execute(run_id=1) == 0
  assert captured["cmd"][0] == str(tmp_path / "artefacts" / "basic" / "bin" / "simv")
  assert captured["cwd"] == str(tmp_path / "artefacts" / "basic" / "run-0001")
  assert Path(tmp_path / "test.log").resolve() == Path(sim._get_log_path(run_id=1)).resolve()
  assert Path(tmp_path / "test.err").resolve() == Path(sim._get_err_path(run_id=1)).resolve()
  assert Path(tmp_path / "test.randseed").resolve() == Path(sim._get_randseed_path(run_id=1)).resolve()


def test_vlog_sim_execute_reads_replay_seed_from_nested_run_dir(tmp_path, monkeypatch):
  captured = {}
  sim = _make_sim(tmp_path, monkeypatch)
  Path(sim._ensure_artifact_dir(run_id=3)).mkdir(parents=True, exist_ok=True)
  Path(sim._get_randseed_path(run_id=3)).write_text("4242\n")

  class FakeProcess:
    def __enter__(self):
      return self

    def __exit__(self, exc_type, exc, tb):
      return False

    def wait(self, timeout):
      return 0

    def send_signal(self, _signal):
      captured["signal_sent"] = True

  def _fake_popen(cmd, preexec_fn, cwd, stdout, stderr):
    captured["cmd"] = list(cmd)
    return FakeProcess()

  monkeypatch.setattr(vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext())
  monkeypatch.setattr(vlog_sim_module.subprocess, "Popen", _fake_popen)

  assert sim.execute(run_id=5, seed_mode=SeedMode.REPLAY, replay_run_id=3) == 0
  assert "+seed=4242" in captured["cmd"]


def test_vlog_sim_execute_reads_hier_seed_from_artifact_dir(tmp_path, monkeypatch):
  sim = _make_sim(tmp_path, monkeypatch, builder_cfg=DummyBuilderCfg(run_opts=["hier_inst_seed"]))

  class FakeProcess:
    def __init__(self, cwd):
      self.cwd = Path(cwd)

    def __enter__(self):
      (self.cwd / "HierInstanceSeed.txt").write_text("instance_seed=99\n")
      return self

    def __exit__(self, exc_type, exc, tb):
      return False

    def wait(self, timeout):
      return 0

    def send_signal(self, _signal):
      pass

  def _fake_popen(cmd, preexec_fn, cwd, stdout, stderr):
    return FakeProcess(cwd)

  monkeypatch.setattr(vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext())
  monkeypatch.setattr(vlog_sim_module.subprocess, "Popen", _fake_popen)

  assert sim.execute(run_id=1) == 0
  randseed_text = Path(sim._get_randseed_path(run_id=1)).read_text()
  assert "1234" in randseed_text
  assert "instance_seed=99" in randseed_text


def test_vlog_sim_multiple_runs_keep_runtime_side_files_separate(tmp_path, monkeypatch):
  sim = _make_sim(tmp_path, monkeypatch)
  counter = {"value": 0}

  class FakeProcess:
    def __init__(self, cwd):
      self.cwd = Path(cwd)

    def __enter__(self):
      counter["value"] += 1
      (self.cwd / "wave.vcd").write_text(f"run={counter['value']}\n")
      return self

    def __exit__(self, exc_type, exc, tb):
      return False

    def wait(self, timeout):
      return 0

    def send_signal(self, _signal):
      pass

  def _fake_popen(cmd, preexec_fn, cwd, stdout, stderr):
    return FakeProcess(cwd)

  monkeypatch.setattr(vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext())
  monkeypatch.setattr(vlog_sim_module.subprocess, "Popen", _fake_popen)

  assert sim.execute(run_id=1) == 0
  assert sim.execute(run_id=2) == 0

  assert (tmp_path / "artefacts" / "basic" / "run-0001" / "wave.vcd").read_text() == "run=1\n"
  assert (tmp_path / "artefacts" / "basic" / "run-0002" / "wave.vcd").read_text() == "run=2\n"
