from contextlib import nullcontext
from pathlib import Path

from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.seed_mode import SeedMode
from rtl_buddy.tools.artifact_paths import (
    sanitize_artifact_component,
    test_artifact_dir,
    test_build_dir_name,
)
from rtl_buddy.tools.vlog_cov import VlogCov
from rtl_buddy.tools import vlog_sim as vlog_sim_module


class DummyBuilderCfg:
    def __init__(
        self,
        *,
        exe="vcs",
        simv="simv",
        simulator_family="vcs",
        compile_opts=None,
        run_opts=None,
        seed=1234,
    ):
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
    def __init__(self, builder_cfg, builders=None, builder_override=None):
        self.builder_cfg = builder_cfg
        self.builders = builders or {}
        self.builder_override = builder_override

    def get_rtl_builder_cfg(self):
        return self.builder_cfg

    def get_rtl_builder_cfg_by_name(self, name):
        return self.builders[name]

    def resolve_rtl_builder_cfg(self, test_builder_name=None):
        if self.builder_override is None and test_builder_name is not None:
            return self.get_rtl_builder_cfg_by_name(test_builder_name)
        return self.get_rtl_builder_cfg()

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

    def is_cocotb(self):
        return False


class DummyTestCfg:
    def __init__(self, name, model_path, builder_name=None):
        self.name = name
        self.model = DummyModelCfg(model_path)
        self.tb = DummyTestbenchCfg()
        self.pd = None
        self.uvm = None
        self.builder_name = builder_name

    def get_name(self):
        return self.name

    def get_builder_name(self):
        return self.builder_name

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


def _make_sim(
    tmp_path,
    monkeypatch,
    *,
    test_name="basic",
    builder_cfg=None,
    test_builder=None,
    builders=None,
    builder_override=None,
):
    monkeypatch.chdir(tmp_path)
    builder_cfg = builder_cfg or DummyBuilderCfg()
    root_cfg = DummyRootCfg(
        builder_cfg, builders=builders, builder_override=builder_override
    )
    test_cfg = DummyTestCfg(
        test_name, tmp_path / "models.yaml", builder_name=test_builder
    )
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
    assert sim._get_artifact_dir(run_id=1) == str(
        tmp_path / "artefacts" / "basic" / "run-0001"
    )
    assert sim._get_log_path(run_id=1) == str(
        tmp_path / "artefacts" / "basic" / "run-0001" / "test.log"
    )
    assert sim._get_err_path(run_id=1) == str(
        tmp_path / "artefacts" / "basic" / "run-0001" / "test.err"
    )
    assert sim._get_randseed_path(run_id=1) == str(
        tmp_path / "artefacts" / "basic" / "run-0001" / "test.randseed"
    )
    assert sim._get_cov_path(run_id=1) == str(
        tmp_path / "artefacts" / "basic" / "run-0001" / "coverage.dat"
    )


def test_vlog_sim_resolves_relative_simv_paths_against_compile_work_dir(
    tmp_path, monkeypatch
):
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=DummyBuilderCfg(exe="vcs", simv="bin/simv"),
    )

    assert sim._get_simv_path() == str(
        tmp_path / "artefacts" / "basic" / "bin" / "simv"
    )


def test_vlog_sim_resolves_verilator_simv_from_build_dir(tmp_path, monkeypatch):
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=DummyBuilderCfg(
            exe="/usr/bin/verilator", simv="ignored", simulator_family="verilator"
        ),
    )

    assert sim._get_simv_path() == str(
        tmp_path / "artefacts" / "basic" / "obj_dir_basic" / "simv"
    )


def test_vlog_sim_compile_uses_explicit_filelist_path_and_suite_cwd(
    tmp_path, monkeypatch
):
    captured = {}
    sim = _make_sim(tmp_path, monkeypatch)

    def _fake_run(cmd, capture_output, text, cwd, env=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["env"] = env
        return ManagedProcessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.compile() == 0
    assert captured["cwd"] == str(tmp_path / "artefacts" / "basic")
    assert captured["cmd"][-2:] == [
        "-f",
        str(tmp_path / "artefacts" / "basic" / "run.f"),
    ]
    assert (tmp_path / "artefacts" / "basic" / "run.f").is_file()


def test_vlog_sim_execute_runs_in_artifact_dir_and_updates_symlinks(
    tmp_path, monkeypatch
):
    captured = {}
    sim = _make_sim(tmp_path, monkeypatch, builder_cfg=DummyBuilderCfg(simv="bin/simv"))

    def _fake_run(cmd, cwd, stdout, stderr, timeout, terminate_signal, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        captured["terminate_signal"] = terminate_signal
        stdout.write("PASS basic\n")
        stderr.write("")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.execute(run_id=1) == 0
    assert captured["cmd"][0] == str(tmp_path / "artefacts" / "basic" / "bin" / "simv")
    assert captured["cwd"] == str(tmp_path / "artefacts" / "basic" / "run-0001")
    assert captured["terminate_signal"] == vlog_sim_module.signal.SIGQUIT
    assert (
        Path(tmp_path / "test.log").resolve()
        == Path(sim._get_log_path(run_id=1)).resolve()
    )
    assert (
        Path(tmp_path / "test.err").resolve()
        == Path(sim._get_err_path(run_id=1)).resolve()
    )
    assert (
        Path(tmp_path / "test.randseed").resolve()
        == Path(sim._get_randseed_path(run_id=1)).resolve()
    )


def test_vlog_sim_execute_reads_replay_seed_from_nested_run_dir(tmp_path, monkeypatch):
    captured = {}
    sim = _make_sim(tmp_path, monkeypatch)
    Path(sim._ensure_artifact_dir(run_id=3)).mkdir(parents=True, exist_ok=True)
    Path(sim._get_randseed_path(run_id=3)).write_text("4242\n")

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.execute(run_id=5, seed_mode=SeedMode.REPLAY, replay_run_id=3) == 0
    assert "+seed=4242" in captured["cmd"]


def test_vlog_sim_execute_reads_hier_seed_from_artifact_dir(tmp_path, monkeypatch):
    sim = _make_sim(
        tmp_path, monkeypatch, builder_cfg=DummyBuilderCfg(run_opts=["hier_inst_seed"])
    )

    def _fake_run(cmd, cwd, **kwargs):
        Path(cwd, "HierInstanceSeed.txt").write_text("instance_seed=99\n")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.execute(run_id=1) == 0
    randseed_text = Path(sim._get_randseed_path(run_id=1)).read_text()
    assert "1234" in randseed_text
    assert "instance_seed=99" in randseed_text


def test_vlog_sim_multiple_runs_keep_runtime_side_files_separate(tmp_path, monkeypatch):
    sim = _make_sim(tmp_path, monkeypatch)
    counter = {"value": 0}

    def _fake_run(cmd, cwd, **kwargs):
        counter["value"] += 1
        Path(cwd, "wave.vcd").write_text(f"run={counter['value']}\n")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.execute(run_id=1) == 0
    assert sim.execute(run_id=2) == 0

    assert (
        tmp_path / "artefacts" / "basic" / "run-0001" / "wave.vcd"
    ).read_text() == "run=1\n"
    assert (
        tmp_path / "artefacts" / "basic" / "run-0002" / "wave.vcd"
    ).read_text() == "run=2\n"


def test_simulator_family_recognizes_iverilog():
    from rtl_buddy.config.rtl import RtlBuilderConfig

    cfg = RtlBuilderConfig.__new__(RtlBuilderConfig)
    cfg.exe = "iverilog"
    cfg.simulator_family = None
    assert cfg.get_simulator_family() == "icarus"

    cfg.exe = "/opt/homebrew/bin/iverilog"
    assert cfg.get_simulator_family() == "icarus"


def test_vlog_sim_icarus_simv_path_is_wrapper_in_compile_work_dir(
    tmp_path, monkeypatch
):
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=DummyBuilderCfg(
            exe="iverilog", simv="ignored", simulator_family="icarus"
        ),
    )
    assert sim._get_simv_path() == str(tmp_path / "artefacts" / "basic" / "simv")
    assert sim._get_icarus_snapshot_path() == str(
        tmp_path / "artefacts" / "basic" / "obj_dir_basic" / "simv.vvp"
    )


def test_vlog_sim_icarus_compile_emits_dash_o_snapshot(tmp_path, monkeypatch):
    captured = {}
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=DummyBuilderCfg(
            exe="iverilog", simulator_family="icarus", compile_opts=["-g2012"]
        ),
    )

    def _fake_run(cmd, capture_output, text, cwd, **kwargs):
        captured["cmd"] = list(cmd)
        return ManagedProcessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.compile() == 0
    snapshot = str(tmp_path / "artefacts" / "basic" / "obj_dir_basic" / "simv.vvp")
    assert "-o" in captured["cmd"]
    assert snapshot in captured["cmd"]
    # The wrapper script is materialized on successful compile.
    wrapper = Path(tmp_path / "artefacts" / "basic" / "simv")
    assert wrapper.is_file()
    assert "exec vvp" in wrapper.read_text()
    assert snapshot in wrapper.read_text()
    # And the wrapper is executable so execute()'s existing path works.
    import os as _os

    assert _os.access(wrapper, _os.X_OK)


def test_artifact_path_helpers_match_existing_sanitization():
    assert sanitize_artifact_component("basic") == "basic"
    assert (
        sanitize_artifact_component("with spaces/slash:punct")
        == "with_spaces_slash_punct"
    )
    assert test_artifact_dir("/tmp/suite", "with spaces/slash:punct") == Path(
        "/tmp/suite/artefacts/with_spaces_slash_punct"
    )
    assert test_artifact_dir("/tmp/suite", "basic", run_id=7) == Path(
        "/tmp/suite/artefacts/basic/run-0007"
    )
    assert (
        test_build_dir_name("with spaces/slash:punct")
        == "obj_dir_with_spaces_slash_punct"
    )
    assert (
        VlogCov(simulator_name="vcs")._sanitize_artifact_name("with spaces/slash:punct")
        == "with_spaces_slash_punct"
    )


def test_vlog_sim_per_test_builder_overrides_platform_default(tmp_path, monkeypatch):
    """A per-test `builder:` name resolves an alternate cfg-rtl-builder entry."""
    platform_default = DummyBuilderCfg(exe="verilator", simulator_family="verilator")
    icarus = DummyBuilderCfg(exe="iverilog", simulator_family="icarus")
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=platform_default,
        builders={"icarus": icarus},
        test_builder="icarus",
    )
    assert sim.rtl_builder_cfg is icarus
    assert sim._get_simulator_family() == "icarus"


def test_vlog_sim_no_builder_field_keeps_platform_default(tmp_path, monkeypatch):
    platform_default = DummyBuilderCfg(exe="verilator", simulator_family="verilator")
    sim = _make_sim(tmp_path, monkeypatch, builder_cfg=platform_default)
    assert sim.rtl_builder_cfg is platform_default


def test_vlog_sim_cli_builder_override_wins_over_per_test_builder(
    tmp_path, monkeypatch
):
    """`--builder` (builder_override) forces the builder for every test."""
    forced = DummyBuilderCfg(exe="verilator", simulator_family="verilator")
    icarus = DummyBuilderCfg(exe="iverilog", simulator_family="icarus")
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        builder_cfg=forced,
        builders={"icarus": icarus},
        test_builder="icarus",
        builder_override="verilator",
    )
    assert sim.rtl_builder_cfg is forced
