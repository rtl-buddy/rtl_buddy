import errno
import fcntl
import json
import logging
import os
import shutil
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from rtl_buddy import artifact_lock as artifact_lock_module
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.runner.test_results import CompileFailResults, TestResults
from rtl_buddy.runner.test_runner import RunDepth, TestRunner as RtlBuddyTestRunner
from rtl_buddy.tools.artifact_paths import atomic_tmp_name, shared_build_dir
from rtl_buddy.tools import vlog_sim as vlog_sim_module


@pytest.fixture(autouse=True)
def _forget_rebuild_claims():
    """Reset the once-per-process ``--rebuild`` claim for each build dir between tests."""
    vlog_sim_module._reset_rebuilt_dirs()
    yield
    vlog_sim_module._reset_rebuilt_dirs()


@pytest.fixture(autouse=True)
def _forget_content_hashes():
    """Clear the per-process content-hash memo, keyed on (path, size, mtime_ns), between tests."""
    with vlog_sim_module._CONTENT_HASH_LOCK:
        vlog_sim_module._CONTENT_HASH_CACHE.clear()
    yield
    with vlog_sim_module._CONTENT_HASH_LOCK:
        vlog_sim_module._CONTENT_HASH_CACHE.clear()


@pytest.fixture(autouse=True)
def _forget_reuse_announcements():
    """Reset the once-per-process ``compile.build_reused`` console announcement between tests."""
    vlog_sim_module._reset_reuse_announcements()
    yield
    vlog_sim_module._reset_reuse_announcements()


@pytest.fixture(autouse=True)
def _forget_lock_degrade_warnings():
    """Reset the once-per-process ``compile.build_lock_unavailable`` warning between tests."""
    artifact_lock_module._reset_degrade_warnings()
    yield
    artifact_lock_module._reset_degrade_warnings()


class DummyBuilderCfg:
    def __init__(
        self,
        *,
        exe="verilator",
        simv="simv",
        simulator_family="verilator",
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
        return list(self.run_opts)

    def get_simulator_family(self):
        return self.simulator_family

    def get_name(self):
        return self.simulator_family


class DummyRootCfg:
    def __init__(self, builder_cfg, project_root=None):
        self.builder_cfg = builder_cfg
        if project_root is not None:
            # Only a root config with a project root gets the accessor; the fallback without it is also exercised.
            self.get_project_rootdir = lambda: str(project_root)

    def get_rtl_builder_cfg(self):
        return self.builder_cfg

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
        return self.builder_cfg

    def get_use_lcov(self, _simulator_name):
        return False


class DummyModelCfg:
    def __init__(self, model_path, filelist=None):
        self.model_path = str(model_path)
        self.filelist = filelist or []

    def get_model_path(self):
        return self.model_path

    def get_filelist(self):
        return list(self.filelist)


class DummyTestbenchCfg:
    def get_filelist(self):
        return []

    def is_cocotb(self):
        return False

    def is_systemc(self):
        return False


class DummyTestCfg:
    def __init__(self, name, model_cfg, pd=None, pa=None):
        self.name = name
        self.model = model_cfg
        self.tb = DummyTestbenchCfg()
        self.pd = pd
        self.pa = pa
        self.uvm = None

    def get_name(self):
        return self.name

    def get_builder_name(self):
        return None

    def get_model(self):
        return self.model

    def get_testbench(self):
        return self.tb

    def get_plusargs(self):
        return None if self.pa is None else dict(self.pa)

    def get_plusdefines(self):
        return dict(self.pd or {})

    def get_timeout(self):
        return 60, False

    def get_preproc_path(self):
        return None


def _write_source(tmp_path, content="module top; endmodule\n"):
    src = tmp_path / "src" / "top.sv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(content)
    return src


def _make_sim(
    tmp_path,
    monkeypatch,
    *,
    test_name,
    share_build=True,
    shared_build_root=None,
    pd=None,
    pa=None,
    exe="verilator",
    family="verilator",
    simv="simv",
    compile_opts=None,
    suite_dir=None,
    project_root=None,
    model_path=None,
    filelist=None,
    rebuild=False,
    run_id=None,
    build_phase="full",
    run_tag=None,
):
    monkeypatch.chdir(tmp_path)
    builder_cfg = DummyBuilderCfg(
        exe=exe, simulator_family=family, simv=simv, compile_opts=compile_opts
    )
    model_cfg = DummyModelCfg(
        model_path or (tmp_path / "models.yaml"), filelist=filelist or ["src/top.sv"]
    )
    test_cfg = DummyTestCfg(test_name, model_cfg, pd=pd, pa=pa)
    return vlog_sim_module.VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=DummyRootCfg(builder_cfg, project_root=project_root),
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        suite_dir=str(suite_dir) if suite_dir is not None else None,
        share_build=share_build,
        shared_build_root=(
            str(shared_build_root) if shared_build_root is not None else None
        ),
        rebuild=rebuild,
        run_id=run_id,
        build_phase=build_phase,
        run_tag=run_tag,
    )


def _install_fake_builder(
    monkeypatch,
    calls,
    *,
    stdout="",
    returncode=0,
    depends=None,
    phony_tail=True,
    simv="simv",
):
    """Fake ``run_managed_process`` that drops a simv where the flags say.

    Follows each family's output convention: Verilator's ``--Mdir <dir>`` and the ``-o <path>`` of VCS and Icarus.
    ``depends`` (Verilator only) is written to a ``V<prefix>__ver.d`` beside the build, with absolute targets,
    prerequisites relative to the compile cwd, and the phony ``<prerequisite>:`` tail that ``--MP`` adds.
    """

    def _fake_run(cmd, capture_output, text, cwd, env=None):
        calls.append({"cmd": list(cmd), "cwd": cwd})

        def _resolve(raw):
            path = Path(raw)
            return path if path.is_absolute() else Path(cwd) / path

        if "--Mdir" in cmd:
            mdir = _resolve(cmd[cmd.index("--Mdir") + 1])
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "simv").write_text("binary\n")
            if depends is not None:
                targets = " ".join(str(mdir / name) for name in ("Vtop.cpp", "Vtop.mk"))
                text_out = f"{targets}  : {' '.join(depends)} \n"
                if phony_tail:
                    text_out += "\n" + "".join(f"{dep}:\n" for dep in depends)
                (mdir / "Vtop__ver.d").write_text(text_out)
        elif "-o" in cmd:
            out = _resolve(cmd[cmd.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("binary\n")
        else:
            # A builder rtl_buddy cannot redirect writes its executable where `builder-simv:` says, relative to the compile dir.
            out = _resolve(simv)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("binary\n")
        return ManagedProcessResult(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)


def test_share_build_reuses_simv_across_tests_with_identical_inputs(
    tmp_path, monkeypatch
):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")

    assert sim_a.compile() == 0
    assert len(calls) == 1
    assert sim_b.compile() == 0
    assert len(calls) == 1  # second compile reused the shared build

    assert sim_a._get_simv_path() == sim_b._get_simv_path()
    shared_root = tmp_path / "artefacts" / ".shared-builds"
    assert Path(sim_a._get_simv_path()).parent.parent == shared_root
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--Mdir") + 1] == str(Path(sim_a._get_simv_path()).parent)

    # The compile record: a real compile is timed, a reuse is 0.0, and both name the builder.
    assert sim_a.last_compile["reused"] is False
    # Timed around the builder; 0.0 here only because the fake builder returns instantly.
    assert isinstance(sim_a.last_compile["duration_sec"], float)
    assert sim_a.last_compile["builder"] == "verilator"
    assert sim_b.last_compile == {
        "duration_sec": 0.0,
        "builder": "verilator",
        "reused": True,
    }


def test_an_unshareable_builder_also_records_its_reuse(tmp_path, monkeypatch):
    """The per-test-stamp reuse path also stamps the compile record."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    first = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert first.compile() == 0
    assert first.last_compile["reused"] is False
    assert first.last_compile["builder"] == "questa"

    second = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert second.compile() == 0
    assert len(calls) == 1  # reused, not recompiled
    assert second.last_compile == {
        "duration_sec": 0.0,
        "builder": "questa",
        "reused": True,
    }


def test_a_probe_records_the_builder_without_claiming_a_compile(tmp_path, monkeypatch):
    """Probing names the builder but leaves duration and reuse flag unknown."""
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [])

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.last_compile is None
    sim.compile_group_dir()
    assert sim.last_compile == {
        "duration_sec": None,
        "builder": "verilator",
        "reused": None,
    }


def test_a_failed_compile_still_records_what_it_cost(tmp_path, monkeypatch):
    """A failed compile still records its duration and builder."""
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [], returncode=1)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() != 0
    assert sim.last_compile["builder"] == "verilator"
    assert sim.last_compile["reused"] is False
    assert sim.last_compile["duration_sec"] is not None


def test_share_build_recompiles_when_plusdefines_differ(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b", pd={"WIDTH": 8})

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert sim_a._get_simv_path() != sim_b._get_simv_path()


def test_a_plusarg_override_never_moves_the_compile_key(tmp_path, monkeypatch):
    """`rb test --plusarg` does not force a rebuild; plusargs reach only the run command line."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b", pa={"mutate": "1"})

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 1  # one compile: the key did not move
    assert sim_a._get_simv_path() == sim_b._get_simv_path()


def test_share_build_recompiles_in_place_when_source_changes(tmp_path, monkeypatch):
    src = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    src.write_text("module top; /* edited */ endmodule\n")
    os.utime(src, ns=(os.stat(src).st_atime_ns, os.stat(src).st_mtime_ns + 1_000_000))

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2  # stale stamp forced a rebuild
    assert sim_a._get_simv_path() == sim_b._get_simv_path()

    sim_c = _make_sim(tmp_path, monkeypatch, test_name="test_c")
    assert sim_c.compile() == 0
    assert len(calls) == 2  # fresh stamp valid again


def test_share_build_ignores_missing_stamp_simv_pair(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    stamp = (
        Path(sim_a._get_simv_path()).parent / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    )
    assert stamp.is_file()
    stamp.unlink()

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2  # simv without a stamp is never trusted


def test_share_build_reuses_simv_across_tests_on_vcs(tmp_path, monkeypatch):
    """VCS shares one build like Verilator."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="vcs", family="vcs"
    )
    sim_b = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="vcs", family="vcs"
    )

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    # Second test short-circuits on the stamp.
    assert len(calls) == 1
    assert sim_a._get_simv_path() == sim_b._get_simv_path()
    shared = Path(sim_a._get_simv_path()).parent
    assert shared.parent == tmp_path / "artefacts" / ".shared-builds"
    # The executable and its intermediate C tree land in the shared dir.
    assert "-o" in calls[0]["cmd"]
    assert calls[0]["cmd"][calls[0]["cmd"].index("-o") + 1] == str(shared / "simv")
    assert f"-Mdir={shared / 'csrc'}" in calls[0]["cmd"]


def test_share_build_on_vcs_overrides_configured_output_opts(tmp_path, monkeypatch):
    """A configured -o / -Mdir does not override the shared build's own."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe="vcs",
        family="vcs",
        compile_opts=["-sverilog", "-o", "mysimv", "-Mdir=mycsrc", "-full64"],
    )
    assert sim.compile() == 0
    cmd = calls[0]["cmd"]
    shared = Path(sim._get_simv_path()).parent
    assert "mysimv" not in cmd
    assert "-Mdir=mycsrc" not in cmd
    assert cmd.count("-o") == 1
    assert cmd[cmd.index("-o") + 1] == str(shared / "simv")
    assert "-sverilog" in cmd and "-full64" in cmd


def test_share_build_reuses_snapshot_across_tests_on_icarus(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="iverilog", family="icarus"
    )
    sim_b = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="iverilog", family="icarus"
    )

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 1
    shared = Path(sim_a._get_simv_path()).parent
    assert sim_a._get_icarus_snapshot_path() == str(shared / "simv.vvp")
    assert Path(sim_a._get_simv_path()).is_file()
    assert sim_b._get_simv_path() == sim_a._get_simv_path()


def test_share_build_falls_back_for_unsupported_builders(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    sim_b = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="qrun", family="questa"
    )

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert "--Mdir" not in calls[0]["cmd"]
    assert sim_a._get_simv_path() == str(tmp_path / "artefacts" / "test_a" / "simv")


def test_unshareable_builder_still_stamps_its_own_build(tmp_path, monkeypatch):
    """A build that cannot be shared is still reused by the next process that asks for the same test."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    first = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert first.compile() == 0
    assert len(calls) == 1
    # The stamp lands beside the test's compile outputs.
    stamp = tmp_path / "artefacts" / "test_a" / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    assert stamp.is_file()

    second = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert second.compile() == 0
    assert len(calls) == 1  # reused, not recompiled

    # It is still not shared: a different test with identical inputs compiles for itself.
    other = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="qrun", family="questa"
    )
    assert other.compile() == 0
    assert len(calls) == 2
    assert other._get_simv_path() != first._get_simv_path()


def test_unshareable_builder_rebuilds_when_a_source_changes(tmp_path, monkeypatch):
    src = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    first = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert first.compile() == 0
    _touch(src, "module top; /* edited */ endmodule\n")

    second = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert second.compile() == 0
    assert len(calls) == 2


def test_no_stamp_is_written_without_share_build(tmp_path, monkeypatch):
    """Reuse is opt-in: plain `rb test` compiles every time."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    for _ in range(2):
        sim = _make_sim(
            tmp_path,
            monkeypatch,
            test_name="test_a",
            exe="qrun",
            family="questa",
            share_build=False,
        )
        assert sim.compile() == 0
    assert len(calls) == 2
    assert not (
        tmp_path / "artefacts" / "test_a" / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    ).exists()


def test_share_build_declines_absolute_builder_simv(tmp_path, monkeypatch):
    """An absolute builder-simv pins the executable, so the build is not shared."""
    _write_source(tmp_path)
    calls = []
    pinned = str(tmp_path / "pinned" / "simv")
    _install_fake_builder(monkeypatch, calls, simv=pinned)

    sim_a = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe="vcs",
        family="vcs",
        simv=pinned,
    )
    sim_b = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_b",
        exe="vcs",
        family="vcs",
        simv=pinned,
    )

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert sim_a._get_simv_path() == pinned


def test_a_pinned_simv_overwritten_by_another_test_invalidates_the_stamp(
    tmp_path, monkeypatch
):
    """A stamp is invalidated when another test overwrites the executable at the same absolute `builder-simv:` path."""
    _write_source(tmp_path)
    calls = []
    pinned = str(tmp_path / "pinned" / "simv")
    _install_fake_builder(monkeypatch, calls, simv=pinned)

    def _sim(name, pd=None):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=name,
            exe="qrun",
            family="questa",
            simv=pinned,
            pd=pd,
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1
    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    assert _sim("test_b", pd={"WIDTH": 8}).compile() == 0
    assert len(calls) == 2
    _touch(Path(pinned), "test_b's binary\n")

    assert _sim("test_a").compile() == 0
    assert len(calls) == 3


def test_configs_pinned_to_one_absolute_simv_land_in_one_group(tmp_path, monkeypatch):
    """Tests that write the same pinned executable path form one group, even with different compile dirs."""
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [])
    pinned = str(tmp_path / "pinned" / "simv")

    def _sim(name, simv):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=name,
            exe="qrun",
            family="questa",
            simv=simv,
        )

    sim_a = _sim("test_a", pinned)
    sim_b = _sim("test_b", pinned)
    assert sim_a.compile_group_dir() == pinned
    assert sim_b.compile_group_dir() == pinned

    # A different pinned path is a different output and may compile concurrently.
    other = _sim("test_c", str(tmp_path / "elsewhere" / "simv"))
    assert other.compile_group_dir() != pinned

    # A relative builder-simv resolves inside the test's own compile dir: one group per test.
    rel_a = _sim("test_d", "simv")
    rel_b = _sim("test_e", "simv")
    assert rel_a.compile_group_dir() != rel_b.compile_group_dir()

    # `--no-share-build` writes the same pinned path, so grouping does not depend on sharing.
    unshared = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_f",
        exe="qrun",
        family="questa",
        simv=pinned,
        share_build=False,
    )
    assert unshared.compile_group_dir() == pinned


def test_a_relative_simv_escaping_the_workspace_lands_in_one_group(
    tmp_path, monkeypatch
):
    """`builder-simv: ../shared/simv` collides like an absolute pin; the group key is the normalized resolved output."""
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [])

    def _sim(name, simv):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=name,
            exe="qrun",
            family="questa",
            simv=simv,
        )

    # artefacts/<test>/../shared/simv collapses to artefacts/shared/simv for every test: one group.
    sim_a = _sim("test_a", "../shared/simv")
    sim_b = _sim("test_b", "../shared/simv")
    meeting_point = sim_a.compile_group_dir()
    assert ".." not in meeting_point
    assert sim_b.compile_group_dir() == meeting_point

    # A relative path that stays inside the workspace resolves per test.
    inside_a = _sim("test_c", "sub/simv")
    inside_b = _sim("test_d", "sub/simv")
    assert inside_a.compile_group_dir() != inside_b.compile_group_dir()

    # A symlinked parent can alias two spellings; the group key is the `realpath`.
    (tmp_path / "alias").symlink_to(tmp_path / "artefacts" / "shared")
    via_link = _sim("test_e", str(tmp_path / "alias" / "simv"))
    assert via_link.compile_group_dir() == meeting_point


def test_verilator_ignores_an_absolute_builder_simv_for_grouping(tmp_path, monkeypatch):
    """Verilator and Icarus output comes from `--Mdir` and is not pinned, so their configs are separate groups."""
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [])
    pinned = str(tmp_path / "pinned" / "simv")

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a", simv=pinned)
    sim_b = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", simv=pinned, pd={"WIDTH": 8}
    )
    assert sim_a.compile_group_dir() != pinned
    assert sim_a.compile_group_dir() != sim_b.compile_group_dir()
    assert vlog_sim_module.pinned_simv_path(DummyBuilderCfg(simv=pinned)) is None


def test_share_build_supported_is_the_single_capability_source():
    assert vlog_sim_module.share_build_supported("verilator")
    assert vlog_sim_module.share_build_supported("vcs")
    assert vlog_sim_module.share_build_supported("icarus")
    assert not vlog_sim_module.share_build_supported("questa")
    assert not vlog_sim_module.share_build_supported(None)


def test_vcs_compile_license_queue_is_reported(tmp_path, monkeypatch):
    """A licqueue wait makes compile elapsed time untrustworthy, and the compile record says so."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, stdout="Queuing for License...\nParsing design\n"
    )

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe="vcs", family="vcs")
    assert sim.compile() == 0
    transcript = Path(sim._get_compile_transcript_path())
    assert transcript.is_file()
    assert "Queuing for License" in transcript.read_text()


def test_verilator_compile_never_reports_license_queue(tmp_path, monkeypatch, caplog):
    """Only VCS queues, so the marker in another family's output is treated as text."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, stdout="Queuing for License...\n")

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0
    assert not [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.license_queued"
    ]


def test_share_build_disabled_keeps_per_test_build_dirs(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a", share_build=False)
    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b", share_build=False)

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert sim_a._get_simv_path() == str(
        tmp_path / "artefacts" / "test_a" / "obj_dir_test_a" / "simv"
    )
    assert sim_b._get_simv_path() == str(
        tmp_path / "artefacts" / "test_b" / "obj_dir_test_b" / "simv"
    )


# --- include-dir headers in the reuse stamp ---------------------


def _write_header(tmp_path, content="`define W 8\n"):
    header = tmp_path / "inc" / "w.svh"
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text(content)
    return header


def _touch(path, text):
    path.write_text(text)
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def _stamp_of(sim):
    return Path(sim._get_simv_path()).parent / vlog_sim_module.SHARED_BUILD_STAMP_NAME


def test_share_build_invalidates_when_an_include_header_changes(tmp_path, monkeypatch):
    """A header reachable only through +incdir+ invalidates the stamp when edited."""
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    # Verilator lists the header among its inputs, relative to the compile cwd.
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 1  # unchanged header: still one verilation

    _touch(header, "`define W 16\n")

    sim_c = _make_sim(tmp_path, monkeypatch, test_name="test_c")
    assert sim_c.compile() == 0
    assert len(calls) == 2  # the edit invalidated the stamp
    assert sim_c._get_simv_path() == sim_a._get_simv_path()  # rebuilt in place


def test_share_build_stamp_records_the_consumed_inputs(tmp_path, monkeypatch):
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../inc/w.svh"])

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0

    deps = json.loads(_stamp_of(sim).read_text())["deps"]
    assert [entry[0] for entry in deps] == [str((tmp_path / "inc" / "w.svh").resolve())]
    size, mtime = deps[0][1], deps[0][2]
    assert size == (tmp_path / "inc" / "w.svh").stat().st_size
    assert mtime == (tmp_path / "inc" / "w.svh").stat().st_mtime_ns


def test_share_build_deps_exclude_the_regenerated_filelist(tmp_path, monkeypatch):
    """`run.f` is rewritten on every compile and is excluded from the stamp, so it does not invalidate its own build."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["run.f", "../../src/top.sv"])

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    deps = json.loads(_stamp_of(sim_a).read_text())["deps"]
    assert not any(entry[0].endswith("run.f") for entry in deps)

    again = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert again.compile() == 0
    assert len(calls) == 1  # the rewritten run.f did not invalidate anything


def test_share_build_records_no_tracking_when_the_builder_emits_no_depfile(
    tmp_path, monkeypatch
):
    """VCS and Icarus emit no dependency file; reuse still works."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="vcs", family="vcs"
    )
    sim_b = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="vcs", family="vcs"
    )

    assert sim_a.compile() == 0
    assert json.loads(_stamp_of(sim_a).read_text())["deps"] is None
    assert sim_b.compile() == 0
    assert len(calls) == 1


def test_share_build_rejects_a_stamp_predating_dependency_tracking(
    tmp_path, monkeypatch
):
    """A stamp without `deps` cannot validate a reuse."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    stamp = _stamp_of(sim_a)
    legacy = json.loads(stamp.read_text())
    legacy.pop("deps")
    stamp.write_text(json.dumps(legacy, sort_keys=True))

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert "deps" in json.loads(stamp.read_text())


def test_share_build_invalidates_when_a_tracked_input_disappears(tmp_path, monkeypatch):
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../inc/w.svh"])

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    header.unlink()

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2


def _write_lib(tmp_path, name, content="module lib_mod; endmodule\n"):
    """A file inside the ``-y`` library directory the tests below compile with."""
    lib = tmp_path / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    path = lib / name
    path.write_text(content)
    return path


def _dir_entry_of(sources, prefix):
    """The ``sources`` entry for the line starting with ``prefix``."""
    return next(entry for entry in sources if entry[0].startswith(prefix))


def _dir_entry(sim, prefix):
    """The stamp's ``sources`` entry for the line starting with ``prefix``."""
    return _dir_entry_of(json.loads(_stamp_of(sim).read_text())["sources"], prefix)


def test_an_incdir_header_edit_invalidates_a_stamp_with_no_depfile(
    tmp_path, monkeypatch
):
    """VCS and Icarus emit no dependency file, so a header edit reachable only through `+incdir+` invalidates the stamp via the directory listing."""
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)  # vcs writes no `.d`

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1
    assert json.loads(_stamp_of(sim_a).read_text())["deps"] is None

    assert _sim("test_b").compile() == 0
    assert len(calls) == 1  # unchanged header: still one compile

    _touch(header, "`define W 16\n")

    sim_c = _sim("test_c")
    assert sim_c.compile() == 0
    assert len(calls) == 2  # the edit invalidated the stamp
    assert sim_c.compile() == 0
    assert len(calls) == 2  # ...once, not once per run


def test_a_header_added_to_an_incdir_invalidates_a_stamp_with_no_depfile(
    tmp_path, monkeypatch
):
    """A header that appears in an include dir invalidates the stamp."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    (tmp_path / "inc" / "extra.svh").write_text("`define X 1\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_file_appearing_in_a_library_dir_invalidates_the_stamp(tmp_path, monkeypatch):
    """A file that appears in a `-y` library directory invalidates the stamp, including for Verilator with a `.d` present."""
    _write_source(tmp_path)
    _write_lib(tmp_path, "bar.sv")
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            filelist=["src/top.sv", "-y lib"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1
    assert json.loads(_stamp_of(sim_a).read_text())["deps"]  # a .d exists

    _write_lib(tmp_path, "foo.sv")  # shadows nothing today; could tomorrow

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_library_dir_listing_is_unfiltered_by_suffix(tmp_path, monkeypatch):
    """Every file in a `-y` directory is listed, because `+libext+` can be set in `builder-opts.compile-time` without reaching run.f.
    `+libext+` itself is a suffix, not a path, and keeps the untracked entry shape.
    """
    _write_source(tmp_path)
    _write_lib(tmp_path, "bar.sv")
    _write_lib(tmp_path, "notes.txt", "not verilog\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            filelist=["src/top.sv", "+libext+.sv", "-y lib"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    listing = _dir_entry(sim_a, "-y ")[-1]
    assert [entry[0] for entry in listing] == ["bar.sv", "notes.txt"]
    assert _dir_entry(sim_a, "+libext+") == ["+libext+.sv", None, None, None]

    # The suffix run.f never mentions; only an unfiltered listing sees it.
    _write_lib(tmp_path, "newmod.vp", "module newmod; endmodule\n")
    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_library_dir_listing_stays_flat(tmp_path, monkeypatch):
    """`-y` resolves module names to files in the directory itself, so subdirectories are not listed."""
    _write_source(tmp_path)
    _write_lib(tmp_path, "bar.sv")
    (tmp_path / "lib" / "vendor").mkdir()
    (tmp_path / "lib" / "vendor" / "deep.sv").write_text("module deep; endmodule\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        filelist=["src/top.sv", "-y lib"],
    )
    assert sim.compile() == 0
    assert [entry[0] for entry in _dir_entry(sim, "-y ")[-1]] == ["bar.sv"]


def test_an_incdir_listing_is_recursive_and_keeps_dot_files(tmp_path, monkeypatch):
    """An include dir is listed unfiltered and recursively; dot-files are input, while dot-directories and editor/VCS bookkeeping names are dropped."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    (tmp_path / "inc" / "table.txt").write_text("0\n")
    (tmp_path / "inc" / ".config.svh").write_text("`define C 1\n")
    (tmp_path / "inc" / "nested").mkdir()
    (tmp_path / "inc" / "nested" / "deep.svh").write_text("`define D 1\n")
    for name in (".DS_Store", ".gitignore", ".w.svh.swp", "w.svh~", ".#w.svh"):
        (tmp_path / "inc" / name).write_text("bookkeeping\n")
    (tmp_path / "inc" / ".git").mkdir()
    (tmp_path / "inc" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        filelist=["src/top.sv", "+incdir+inc"],
    )
    assert sim.compile() == 0
    listing = _dir_entry(sim, "+incdir+")[-1]
    assert [entry[0] for entry in listing] == [
        ".config.svh",
        "nested/deep.svh",
        "table.txt",
        "w.svh",
    ]


def test_a_dot_header_edit_inside_an_incdir_invalidates_the_stamp(
    tmp_path, monkeypatch
):
    """`.config.svh` is a legal include and is tracked."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    dot_header = tmp_path / "inc" / ".config.svh"
    dot_header.write_text("`define C 1\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    _touch(dot_header, "`define C 2\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_an_incdir_above_the_artefact_dir_does_not_stamp_rtl_buddys_output(
    tmp_path, monkeypatch
):
    """An `artefacts/` directory inside an include dir (`+incdir+.` or `+incdir+..`) is excluded from the listing, since its contents are written after the fingerprint."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+."],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    listing = [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]
    assert "src/top.sv" in listing  # the walk did happen
    assert not [name for name in listing if name.startswith("artefacts/")], listing

    assert _sim("test_b").compile() == 0
    assert len(calls) == 1
    assert _sim("test_c").compile() == 0
    assert len(calls) == 1


def test_a_generated_header_under_the_artefact_dir_is_tracked(tmp_path, monkeypatch):
    """A `preproc`-generated header under a managed `+incdir+artefacts/<test>/gen` is tracked, while rtl_buddy's own outputs beside it are not."""
    _write_source(tmp_path)
    gen = tmp_path / "artefacts" / "test_a" / "gen"
    gen.mkdir(parents=True)
    generated = gen / "gen_w.svh"
    generated.write_text("`define GW 8\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+artefacts/test_a"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    listing = [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]
    assert "gen/gen_w.svh" in listing, listing
    for output in ("run.f", "compile.log", "result.json", "rb-compile-stamp.json"):
        assert not any(name.endswith(output) for name in listing), listing

    assert _sim("test_b").compile() == 0
    assert len(calls) == 1
    assert _sim("test_c").compile() == 0
    assert len(calls) == 1

    _touch(generated, "`define GW 16\n")
    assert _sim("test_d").compile() == 0
    assert len(calls) == 2


def test_rtl_buddys_own_outputs_are_never_listed(tmp_path, monkeypatch):
    """Managed outputs (run directory logs and envelopes) are excluded by name, pinned to the constants the writers use."""
    _write_source(tmp_path)
    inc = tmp_path / "inc"
    inc.mkdir(parents=True, exist_ok=True)
    (inc / "w.svh").write_text("`define W 8\n")
    for name in (
        vlog_sim_module.FILELIST_NAME,
        vlog_sim_module.COMPILE_TRANSCRIPT_NAME,
        vlog_sim_module.COMPILE_RETRY_TRANSCRIPT_NAME,
        vlog_sim_module.TEST_LOG_NAME,
        vlog_sim_module.TEST_ERR_NAME,
        vlog_sim_module.TEST_RANDSEED_NAME,
        vlog_sim_module.COVERAGE_DAT_NAME,
        vlog_sim_module.SIMV_NAME,
        vlog_sim_module.ICARUS_SNAPSHOT_NAME,
        vlog_sim_module.SHARED_BUILD_STAMP_NAME,
        "result.json",
        "result-1234.json",
        "rtl_buddy-1234.log",
    ):
        (inc / name).write_text("rtl_buddy output\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        filelist=["src/top.sv", "+incdir+inc"],
    )
    assert sim.compile() == 0
    assert [entry[0] for entry in _dir_entry(sim, "+incdir+")[-1]] == ["w.svh"]


def test_a_build_directory_beside_the_sources_is_pruned_too(tmp_path, monkeypatch):
    """`obj_dir*` build directories are excluded wherever they land, including an unshared build next to the sources."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    stray = tmp_path / "inc" / "obj_dir_test_a"
    stray.mkdir()
    (stray / "Vtop.cpp").write_text("generated\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]] == ["w.svh"]

    (stray / "Vtop.cpp").write_text("regenerated\n")
    assert _sim("test_b").compile() == 0
    assert len(calls) == 1


def test_a_header_nested_under_an_incdir_invalidates_the_stamp(tmp_path, monkeypatch):
    """`` `include "nested/deep.svh" `` resolves below the include directory, so the listing is recursive."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    nested = tmp_path / "inc" / "nested"
    nested.mkdir()
    deep = nested / "deep.svh"
    deep.write_text("`define D 1\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    _touch(deep, "`define D 2\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_dot_file_appearing_in_an_incdir_does_not_invalidate(tmp_path, monkeypatch):
    """Names no simulator reads, such as `.DS_Store`, are excluded from the listing."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    for name in (".DS_Store", ".gitignore", ".w.svh.swp", "w.svh~", "#w.svh#"):
        (tmp_path / "inc" / name).write_text("bookkeeping\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 1


def test_an_edit_outside_every_listed_directory_still_reuses(tmp_path, monkeypatch):
    """The listing covers only the directories the filelist names."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    unrelated = tmp_path / "docs"
    unrelated.mkdir()
    (unrelated / "notes.md").write_text("nothing to do with the build\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 1


def test_a_stamp_written_before_directory_listings_rebuilds_once(tmp_path, monkeypatch):
    """A stamp written without the listing element is not reused; one rebuild rewrites it in the readable shape."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    stamp = _stamp_of(sim_a)
    legacy = json.loads(stamp.read_text())
    legacy["sources"] = [entry[:4] for entry in legacy["sources"]]
    stamp.write_text(json.dumps(legacy, sort_keys=True))

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2
    assert any(len(entry) == 5 for entry in json.loads(stamp.read_text())["sources"])

    assert _sim("test_c").compile() == 0
    assert len(calls) == 2, "the rebuild happened once, not once per run"


def test_a_directory_listing_never_reaches_the_compile_key():
    """An edit inside an include dir rebuilds in place instead of creating another obj_dir."""
    fingerprint = {
        "cmd": ["verilator", "--binary", "-f", "run.f"],
        "env": {},
        "sources": [
            ["src/top.sv", 31, 1_700_000_000_000_000_000, "0123456789abcdef"],
            ["+incdir+inc", None, None, None, [["w.svh", 9, 17, "aaaa"]]],
        ],
        "toolchain": {
            "exe": "/opt/verilator/bin/verilator",
            "version": "5.020",
            "size": 12,
            "mtime_ns": 7,
        },
    }
    edited = dict(
        fingerprint,
        sources=[
            fingerprint["sources"][0],
            ["+incdir+inc", None, None, None, [["w.svh", 11, 23, "bbbb"]]],
        ],
    )
    added = dict(
        fingerprint,
        sources=[
            fingerprint["sources"][0],
            [
                "+incdir+inc",
                None,
                None,
                None,
                [["extra.svh", 4, 5, "cccc"], ["w.svh", 9, 17, "aaaa"]],
            ],
        ],
    )
    key = vlog_sim_module.VlogSim._compile_config_key(fingerprint)
    assert key == vlog_sim_module.VlogSim._compile_config_key(edited)
    assert key == vlog_sim_module.VlogSim._compile_config_key(added)

    sha = vlog_sim_module._fingerprint_sha(fingerprint)
    assert sha != vlog_sim_module._fingerprint_sha(edited)
    assert sha != vlog_sim_module._fingerprint_sha(added)


def test_an_edit_inside_an_include_dir_rebuilds_in_place(tmp_path, monkeypatch):
    """End to end: the second compile reuses the same obj_dir."""
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    _touch(header, "`define W 16\n")
    sim_b = _sim("test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert sim_b._get_simv_path() == sim_a._get_simv_path()


def _source_changed_entries(caplog):
    return [
        record.rtl_fields["entry"]
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "compile.build_source_changed"
    ]


def test_a_changed_directory_entry_names_the_file_that_changed(
    tmp_path, monkeypatch, caplog
):
    """`compile.build_source_changed` names the file inside a changed directory entry, for an edit, an addition and a removal."""
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    _touch(header, "`define W 16\n")

    with caplog.at_level(logging.DEBUG, logger="rtl_buddy.tools.vlog_sim"):
        assert _sim("test_b").compile() == 0
    entries = _source_changed_entries(caplog)
    assert entries, "no compile.build_source_changed event was logged"
    assert any("+incdir+" in entry and entry.endswith(":: w.svh") for entry in entries)

    added = tmp_path / "inc" / "extra.svh"
    added.write_text("`define X 1\n")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="rtl_buddy.tools.vlog_sim"):
        assert _sim("test_c").compile() == 0
    assert any(
        entry.endswith(":: +extra.svh") for entry in _source_changed_entries(caplog)
    )

    added.unlink()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="rtl_buddy.tools.vlog_sim"):
        assert _sim("test_d").compile() == 0
    assert any(
        entry.endswith(":: -extra.svh") for entry in _source_changed_entries(caplog)
    )


def test_a_vanished_include_directory_invalidates_the_stamp(tmp_path, monkeypatch):
    """A `+incdir+` whose directory is gone is not reused; one rebuild."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1
    assert _dir_entry(sim_a, "+incdir+")[-1]  # a listing was recorded

    # The filelist writer refuses a missing directory, so the stamp is revalidated directly.
    stored = json.loads(_stamp_of(sim_a).read_text())["sources"]
    run_f = sim_a._get_filelist_path()
    shutil.rmtree(tmp_path / "inc")
    current = sim_a._fingerprint_filelist_sources(run_f)
    assert _dir_entry_of(current, "+incdir+") == [
        next(entry[0] for entry in current if entry[0].startswith("+incdir+")),
        None,
        None,
        None,
    ]
    assert not vlog_sim_module._entry_lists_match(stored, current)


def test_an_unreadable_directory_degrades_to_untracked(tmp_path, monkeypatch):
    """A directory that cannot be listed records the untracked entry, never an empty listing."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    real_scandir = os.scandir

    def _refuse(path, *args, **kwargs):
        if os.path.basename(str(path)) == "inc":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    # `os.walk` swallows an unopenable directory by default, which would make it look empty.
    monkeypatch.setattr(vlog_sim_module.os, "scandir", _refuse)
    monkeypatch.setattr(os, "scandir", _refuse)

    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe="vcs",
        family="vcs",
        filelist=["src/top.sv", "+incdir+inc"],
    )
    assert sim.compile() == 0
    assert _dir_entry(sim, "+incdir+")[1:] == [None, None, None]


def test_a_corrupt_directory_listing_fails_closed():
    """A stamp shape this version cannot read means "rebuild" and never raises."""
    good = ["+incdir+inc", None, None, None, [["w.svh", 9, 17, "aaaa"]]]
    assert vlog_sim_module._entry_matches(good, list(good))
    for corrupt in (
        ["+incdir+inc", None, None, None, "not-a-list"],
        ["+incdir+inc", None, None, None, [["w.svh", 9, 17]]],
        ["+incdir+inc", None, None, None, ["w.svh"]],
    ):
        assert not vlog_sim_module._entry_matches(corrupt, good)
        assert not vlog_sim_module._entry_matches(good, corrupt)


# --- what the run itself writes into a stamped directory ---


def _incdir_dot_sim(tmp_path, monkeypatch, test_name, *, family="vcs"):
    """A sim whose filelist puts the working directory on the include path with `+incdir+.`."""
    return _make_sim(
        tmp_path,
        monkeypatch,
        test_name=test_name,
        exe=family,
        family=family,
        filelist=["src/top.sv", "+incdir+."],
    )


def test_the_suite_log_is_never_listed_in_an_include_directory(tmp_path, monkeypatch):
    """The suite-level `rtl_buddy.log` is excluded from the listing."""
    _write_source(tmp_path)
    log = tmp_path / "rtl_buddy.log"
    log.write_text("dispatch: 4/5 jobs remaining\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a")
    assert sim_a.compile() == 0
    listing = [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]
    assert "src/top.sv" in listing  # the walk did happen
    assert "rtl_buddy.log" not in listing, listing

    _touch(log, "dispatch: 4/5 jobs remaining\ndispatch: 3/5 jobs remaining\n")
    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_b").compile() == 0
    assert len(calls) == 1


def test_a_real_input_named_like_the_suite_log_stays_tracked(tmp_path, monkeypatch):
    """Only the suite's own `rtl_buddy.log` is skipped; a same-named file in another include directory is a compile input."""
    _write_source(tmp_path)
    (tmp_path / "rtl_buddy.log").write_text("dispatch: 4/5 jobs remaining\n")
    image = tmp_path / "inc" / "rtl_buddy.log"
    image.parent.mkdir()
    image.write_text("@0000 DEADBEEF\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            exe="vcs",
            family="vcs",
            filelist=["src/top.sv", "+incdir+.", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    suite_listing = [e[0] for e in _dir_entry(sim_a, f"+incdir+{tmp_path}")[-1]]
    assert "rtl_buddy.log" not in suite_listing, suite_listing
    assert "inc/rtl_buddy.log" in suite_listing, suite_listing
    inc_listing = [e[0] for e in _dir_entry(sim_a, f"+incdir+{tmp_path / 'inc'}")[-1]]
    assert inc_listing == ["rtl_buddy.log"], inc_listing

    _touch(image, "@0000 CAFEF00D\n")
    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_pycache_beside_a_preproc_helper_is_never_listed(tmp_path, monkeypatch):
    """Python bytecode written beside a `preproc` helper module during fingerprinting is excluded."""
    _write_source(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "helper.cpython-313.pyc").write_bytes(b"\x00\x01")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a")
    assert sim_a.compile() == 0
    listing = [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]
    assert not [name for name in listing if name.startswith("__pycache__/")], listing

    (cache / "other.cpython-313.pyc").write_bytes(b"\x00\x02")
    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_b").compile() == 0
    assert len(calls) == 1


def _tmp_log_names(tmp_path):
    """The temp names the suite-level log writers build, taken from :func:`atomic_tmp_name` so the test tracks the writer."""
    return [
        atomic_tmp_name(str(tmp_path / name))
        for name in (
            vlog_sim_module.TEST_LOG_NAME,
            vlog_sim_module.TEST_ERR_NAME,
            vlog_sim_module.TEST_RANDSEED_NAME,
            vlog_sim_module.FILELIST_NAME,
        )
    ]


def test_a_transient_log_temp_file_appearing_does_not_invalidate(tmp_path, monkeypatch):
    """A `<name>.<pid>.<uuid>.tmp` file from a `test.log`/`test.err` symlink repoint does not change the listing under `+incdir+.`."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1
    listing = [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]
    assert "src/top.sv" in listing  # the walk did happen

    for name in _tmp_log_names(tmp_path):
        Path(name).write_text("in flight\n")

    sim_b = _incdir_dot_sim(tmp_path, monkeypatch, "test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 1
    assert sim_b.last_compile["reused"] is True
    later = [entry[0] for entry in _dir_entry(sim_b, "+incdir+")[-1]]
    assert not [name for name in later if name.endswith(".tmp")], later


def test_a_transient_log_temp_file_disappearing_does_not_invalidate(
    tmp_path, monkeypatch
):
    """A temp file that existed at build fingerprint time and is gone at sim check time does not affect the result either."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    in_flight = [Path(name) for name in _tmp_log_names(tmp_path)]
    for path in in_flight:
        path.write_text("in flight\n")

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1
    listing = [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]
    assert not [name for name in listing if name.endswith(".tmp")], listing

    for path in in_flight:
        path.unlink()

    sim_b = _incdir_dot_sim(tmp_path, monkeypatch, "test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 1
    assert sim_b.last_compile["reused"] is True


def test_a_settled_managed_output_temp_name_is_excluded_for_every_output():
    """Every managed output pattern is excluded from the listing."""
    for pattern in vlog_sim_module._MANAGED_OUTPUT_FILE_PATTERNS:
        name = pattern.replace("*", "job1")
        assert vlog_sim_module._is_non_input_file(name), name
        assert vlog_sim_module._is_non_input_file(f"{name}.tmp"), name
        assert vlog_sim_module._is_non_input_file(atomic_tmp_name(name)), name


def test_a_real_header_named_like_a_temp_file_stays_tracked(tmp_path, monkeypatch):
    """Exclusion is anchored to managed output basenames; a project header ending in `.tmp` is an ordinary input."""
    _write_source(tmp_path)
    header = tmp_path / "defs.tmp"
    header.write_text("`define W 8\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a")
    assert sim_a.compile() == 0
    assert "defs.tmp" in [entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]]

    _touch(header, "`define W 16\n")
    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_b").compile() == 0
    assert len(calls) == 2


def test_a_real_header_in_a_suite_incdir_still_moves_the_fingerprint(
    tmp_path, monkeypatch
):
    """Adding, editing and removing a genuine header in the same directory each rebuild."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_a").compile() == 0
    assert len(calls) == 1

    header = tmp_path / "w.svh"
    header.write_text("`define W 8\n")  # added
    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_b").compile() == 0
    assert len(calls) == 2

    _touch(header, "`define W 16\n")  # modified
    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_c").compile() == 0
    assert len(calls) == 3

    header.unlink()  # removed
    assert _incdir_dot_sim(tmp_path, monkeypatch, "test_d").compile() == 0
    assert len(calls) == 4


def test_a_regenerated_file_the_build_never_read_does_not_invalidate(
    tmp_path, monkeypatch
):
    """A file the dependency file never listed is excluded from hashing, so a per-test `preproc` regenerating its directory does not force a recompile."""
    _write_source(tmp_path)
    prog = tmp_path / "prog_a"
    prog.mkdir()
    (prog / "data.txt").write_text("first run\n")
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a", family="verilator")
    assert sim_a.compile() == 0
    assert json.loads(_stamp_of(sim_a).read_text())["deps"]  # a .d exists
    assert "prog_a/data.txt" in [
        entry[0] for entry in _dir_entry(sim_a, "+incdir+")[-1]
    ]

    _touch(prog / "data.txt", "second run\n")

    sim_b = _incdir_dot_sim(tmp_path, monkeypatch, "test_b", family="verilator")
    assert sim_b.compile() == 0
    assert len(calls) == 1
    assert sim_b.last_compile["reused"] is True


def test_an_edit_inside_an_incdir_still_invalidates_when_the_build_read_it(
    tmp_path, monkeypatch
):
    """A file the build consumed stays in `deps`, where content decides."""
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    _touch(header, "`define W 16\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_file_appearing_in_an_incdir_still_invalidates_with_a_depfile(
    tmp_path, monkeypatch
):
    """A newly appeared name that could shadow a consumed file is still decided by the listing."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    (tmp_path / "inc" / "extra.svh").write_text("`define X 1\n")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_same_key_siblings_reuse_when_every_probe_precedes_every_compile(
    tmp_path, monkeypatch
):
    """Build-job order: PRE and the compile-key probe run for every config before the first compile, and members of one group compile once."""
    _write_source(tmp_path)
    for name in ("test_a", "test_b"):
        prog = tmp_path / f"prog_{name}"
        prog.mkdir()
        (prog / "data.txt").write_text("first run\n")
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a", family="verilator")
    sim_b = _incdir_dot_sim(tmp_path, monkeypatch, "test_b", family="verilator")

    # PRE, then the probe, one config at a time, then the compiles.
    _touch(tmp_path / "prog_test_a" / "data.txt", "second run\n")
    group_a = sim_a.compile_group_dir()
    _touch(tmp_path / "prog_test_b" / "data.txt", "second run\n")
    group_b = sim_b.compile_group_dir()
    assert group_a == group_b  # one compile key, so one group

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 1
    assert sim_b.last_compile["reused"] is True


def _cold_tree_group_pair(tmp_path, monkeypatch, calls, *, family="verilator"):
    """Return a sibling of a leader that compiled, on the same compile key, in cold-tree build-job order.
    PRE writes the sibling's program directory under the stamped ``+incdir+.`` before the compile-key probe, after the leader's stamp was taken.
    """
    leader = _incdir_dot_sim(tmp_path, monkeypatch, "test_a", family=family)
    (tmp_path / "prog_test_a").mkdir()
    (tmp_path / "prog_test_a" / "data.txt").write_text("a\n")
    group_a = leader.compile_group_dir()
    assert leader.compile() == 0
    assert len(calls) == 1

    sibling = _incdir_dot_sim(tmp_path, monkeypatch, "test_b", family=family)
    (tmp_path / "prog_test_b").mkdir()
    (tmp_path / "prog_test_b" / "data.txt").write_text("b\n")
    assert sibling.compile_group_dir() == group_a  # one compile key
    return sibling


def test_a_group_sibling_adopts_the_leaders_build_on_a_cold_tree(tmp_path, monkeypatch):
    """A name that appeared during the job does not force a recompile.
    The sibling adopts the leader's build when the leader's ``deps`` are unchanged.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])
    sibling = _cold_tree_group_pair(tmp_path, monkeypatch, calls)

    plan = sibling._compile_plan()
    assert not sibling._shared_build_is_valid(
        plan.shared_dir, plan.fingerprint, quiet=True
    ), "the stamp was supposed to lose; this test proves nothing otherwise"

    assert sibling.adopt_group_build() == ("adopted", None)
    assert len(calls) == 1  # nothing recompiled
    assert sibling.last_compile["reused"] is True
    assert sibling.last_build_stamp["fingerprint_sha"]


def test_a_group_sibling_that_rewrote_a_consumed_input_is_reported(
    tmp_path, monkeypatch
):
    """One compile key with two sets of bytes is a misconfiguration: it is reported against the drifted config and its sim job declines to recompile."""
    source = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])
    sibling = _cold_tree_group_pair(tmp_path, monkeypatch, calls)

    _touch(source, "module top; wire w; endmodule\n")

    verdict, dependency = sibling.adopt_group_build()
    assert verdict == "drift"
    assert dependency.endswith("src/top.sv")
    assert len(calls) == 1
    assert "same compile key, different compiled input" in sibling.compile_fail_desc
    # The build job's envelope record carries a returncode, so the gated sim job reads a verdict.
    failure = sibling.last_compile_failure
    assert failure["returncode"] == 1
    assert "src/top.sv" in failure["error_tail"][0]


def test_a_group_sibling_declines_to_adopt_without_a_dependency_file(
    tmp_path, monkeypatch
):
    """VCS and Icarus report no dependency file, so nothing is narrowed and the sibling falls back to the leader's full stamp comparison."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sibling = _cold_tree_group_pair(tmp_path, monkeypatch, calls, family="vcs")

    assert sibling.adopt_group_build() == (
        None,
        "no dependency list (builder reports none)",
    )
    assert sibling.last_compile["reused"] is None  # nothing decided yet
    assert sibling.compile() == 0
    assert len(calls) == 2


def test_every_adoption_decline_names_its_own_reason(tmp_path, monkeypatch):
    """Each remaining `(None, <reason>)` adoption return, one setup each; the reason appears in `build_job.group_adoption_declined`."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])
    sibling = _cold_tree_group_pair(tmp_path, monkeypatch, calls)
    stamp_path = (
        Path(sibling.compile_group_dir()) / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    )
    stamped = json.loads(stamp_path.read_text())

    def _restamp(**overrides):
        stamp_path.write_text(json.dumps({**stamped, **overrides}, sort_keys=True))
        # Consumed by the previous attempt; a fresh plan re-stats the tree.
        sibling._compile_plan_cache = None

    _restamp(deps_format=1)
    assert sibling.adopt_group_build() == (
        None,
        f"stamp dependency format 1 != {vlog_sim_module._DEPS_FORMAT}",
    )

    # The compile key fixes the command, so this covers a toolchain replaced under a running job.
    _restamp(toolchain="verilator 4.999 from somewhere else")
    assert sibling.adopt_group_build() == (None, "stamp inputs differ")

    _restamp(deps=[["src/top.sv", 1]])
    assert sibling.adopt_group_build() == (None, "unreadable dependency entry")

    stamp_path.unlink()
    sibling._compile_plan_cache = None
    assert sibling.adopt_group_build() == (None, "no stamp")
    assert len(calls) == 1, "a decline compiled something"


def _group_pair_with_incdirs(
    tmp_path, monkeypatch, calls, *, filelist, depends, sibling_pre
):
    """Return a leader that compiled and a same-key sibling; ``sibling_pre`` runs after the leader's compile and before the sibling's probe."""
    _install_fake_builder(monkeypatch, calls, depends=depends)

    def _sim(test_name):
        return _make_sim(tmp_path, monkeypatch, test_name=test_name, filelist=filelist)

    leader = _sim("test_a")
    group = leader.compile_group_dir()
    assert leader.compile() == 0
    assert len(calls) == 1
    sibling_pre()
    sibling = _sim("test_b")
    assert sibling.compile_group_dir() == group
    return sibling


def test_a_group_sibling_declines_a_build_whose_include_is_now_shadowed(
    tmp_path, monkeypatch
):
    """A header appearing under the name of a consumed include in an earlier ``+incdir+`` is not adoptable; the sibling defers to the stamp."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    header = _write_header(tmp_path)  # inc/w.svh
    calls = []
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=["src/top.sv", "+incdir+gen", "+incdir+inc"],
        depends=["../../src/top.sv", "../../inc/w.svh"],
        sibling_pre=lambda: (tmp_path / "gen" / "w.svh").write_text("`define W 16\n"),
    )
    assert header.exists()

    verdict, reason = sibling.adopt_group_build()
    assert verdict is None
    assert reason.startswith("resolution changed: +incdir+")
    assert reason.endswith("/gen :: +w.svh")
    assert sibling.last_compile["reused"] is None
    assert sibling.compile() == 0
    assert len(calls) == 2, "the shadowed include was adopted as unchanged"


def test_a_group_sibling_declines_a_build_when_a_library_file_appeared(
    tmp_path, monkeypatch
):
    """A file appearing in a ``-y`` directory is not adoptable."""
    _write_source(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "keep.sv").write_text("module keep; endmodule\n")
    calls = []
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=["src/top.sv", "-y lib"],
        depends=["../../src/top.sv"],
        sibling_pre=lambda: (lib / "top.sv").write_text("module top; endmodule\n"),
    )

    verdict, reason = sibling.adopt_group_build()
    assert verdict is None
    assert reason.startswith("resolution changed: -y ")
    assert reason.endswith("/lib :: +top.sv")
    assert sibling.compile() == 0
    assert len(calls) == 2, "a new library file was adopted as unchanged"


def _listing(*names):
    return [[name, 1, 1, "sha"] for name in names]


@pytest.mark.parametrize(
    ("line", "listing", "expected"),
    [
        ("-y lib", _listing("keep.sv"), "-y lib :: +keep.sv"),
        ("+incdir+gen", _listing("w.svh"), "+incdir+gen :: +w.svh"),
        ("+incdir+gen", _listing("params_test_b.svh"), None),
    ],
)
def test_a_stamped_directory_that_did_not_exist_counts_as_all_appeared(
    line, listing, expected
):
    """A directory absent at stamp time and present now counts as every file in it appearing; a `-y` member or shadowing include declines adoption, an unrelated header does not."""
    stored = [["src/top.sv", 1, 1, "sha"], [line, None, None, None]]
    current = [["src/top.sv", 1, 1, "sha"], [line, None, None, None, listing]]
    deps = [["src/top.sv", 1, 1, "sha"], ["inc/w.svh", 1, 1, "sha"]]

    assert vlog_sim_module._first_resolution_change(stored, current, deps) == expected


def test_a_group_sibling_still_adopts_past_an_unrelated_new_file(tmp_path, monkeypatch):
    """A new file under `+incdir+` that shadows nothing the leader consumed is adopted."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    _write_header(tmp_path)
    calls = []
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=["src/top.sv", "+incdir+gen", "+incdir+inc"],
        depends=["../../src/top.sv", "../../inc/w.svh"],
        sibling_pre=lambda: (tmp_path / "gen" / "params_test_b.svh").write_text(
            "`define B 1\n"
        ),
    )

    assert sibling.adopt_group_build() == ("adopted", None)
    assert len(calls) == 1


def test_an_adoption_leaves_a_stamp_the_gated_jobs_validate(tmp_path, monkeypatch):
    """Adoption rewrites the stamp's listing to the tree as it now stands, so gated sim jobs that list the populated tree validate."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    _write_header(tmp_path)
    calls = []
    filelist = ["src/top.sv", "+incdir+gen", "+incdir+inc"]
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=filelist,
        depends=["../../src/top.sv", "../../inc/w.svh"],
        sibling_pre=lambda: (tmp_path / "gen" / "params_test_b.svh").write_text(
            "`define B 1\n"
        ),
    )
    assert sibling.adopt_group_build() == ("adopted", None)

    for test_name in ("test_a", "test_b"):
        gated = _make_sim(tmp_path, monkeypatch, test_name=test_name, filelist=filelist)
        gated.expect_prebuilt = True
        assert gated.compile() == 0
        assert gated.last_compile["reused"] is True
    assert len(calls) == 1


def test_an_adoption_validates_the_stamp_it_rewrites_under_the_lock(
    tmp_path, monkeypatch
):
    """A stamp replaced by a concurrent rebuild between the unlocked check and the locked rewrite is not adoptable and is not rewritten."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    _write_header(tmp_path)
    calls = []
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=["src/top.sv", "+incdir+gen", "+incdir+inc"],
        depends=["../../src/top.sv", "../../inc/w.svh"],
        sibling_pre=lambda: (tmp_path / "gen" / "params_test_b.svh").write_text(
            "`define B 1\n"
        ),
    )
    shared_dir = Path(sibling.compile_group_dir())
    stamp_path = shared_dir / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    before = stamp_path.read_text()
    real_lock = vlog_sim_module.build_dir_lock

    @contextmanager
    def _rebuilt_under_us(build_dir, **kwargs):
        with real_lock(build_dir, **kwargs) as held:
            simv = Path(sibling._get_simv_path())
            simv.write_bytes(simv.read_bytes() + b"\n# rebuilt by a neighbour\n")
            yield held

    monkeypatch.setattr(vlog_sim_module, "build_dir_lock", _rebuilt_under_us)

    assert sibling.adopt_group_build() == (None, "simv changed")
    assert stamp_path.read_text() == before, "a stamp nobody validated was rewritten"


def test_an_adoption_lists_the_tree_under_the_lock(tmp_path, monkeypatch):
    """The listing is taken under the lock, so a header dropped in while waiting for the lock and shadowing a consumed include declines adoption."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    _write_header(tmp_path)
    calls = []
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=["src/top.sv", "+incdir+gen", "+incdir+inc"],
        depends=["../../src/top.sv", "../../inc/w.svh"],
        sibling_pre=lambda: (tmp_path / "gen" / "params_test_b.svh").write_text(
            "`define B 1\n"
        ),
    )
    shared_dir = Path(sibling.compile_group_dir())
    stamp_path = shared_dir / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    before = stamp_path.read_text()
    real_lock = vlog_sim_module.build_dir_lock

    @contextmanager
    def _shadowed_under_us(build_dir, **kwargs):
        with real_lock(build_dir, **kwargs) as held:
            (tmp_path / "gen" / "w.svh").write_text("`define W 2\n")
            yield held

    monkeypatch.setattr(vlog_sim_module, "build_dir_lock", _shadowed_under_us)

    verdict, reason = sibling.adopt_group_build()
    assert verdict is None
    assert reason.startswith("resolution changed: +incdir+")
    assert stamp_path.read_text() == before, "a stale listing was stamped"


def test_an_adoption_whose_stamp_refresh_fails_is_not_an_adoption(
    tmp_path, monkeypatch
):
    """A stamp that cannot be rewritten (read-only, ``ENOSPC``) leaves the config undecided and sends it to the compile path."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    _write_header(tmp_path)
    calls = []
    sibling = _group_pair_with_incdirs(
        tmp_path,
        monkeypatch,
        calls,
        filelist=["src/top.sv", "+incdir+gen", "+incdir+inc"],
        depends=["../../src/top.sv", "../../inc/w.svh"],
        sibling_pre=lambda: (tmp_path / "gen" / "params_test_b.svh").write_text(
            "`define B 1\n"
        ),
    )
    stamp_path = (
        Path(sibling.compile_group_dir()) / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    )
    before = stamp_path.read_text()

    def _no_space(self, path, text):
        raise OSError(errno.ENOSPC, "No space left on device", str(path))

    monkeypatch.setattr(vlog_sim_module.VlogSim, "_replace_text", _no_space)

    assert sibling.adopt_group_build() == (None, "stamp refresh failed")
    assert stamp_path.read_text() == before
    assert sibling.last_compile["reused"] is None


def test_the_build_stamp_identity_names_the_key_and_the_binary(tmp_path, monkeypatch):
    """The run reports the digest of the fingerprint it simulated, the stamp's own ``simv``, and the shared ``build_dir``.
    The digest is the ``_fingerprint_sha`` of a live fingerprint, so build-job and sim-job records are comparable.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    builder = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    plan = builder._compile_plan()
    fingerprint = plan.fingerprint
    assert builder.compile() == 0
    stamp = builder.last_build_stamp
    assert stamp["build_dir"] == os.path.realpath(plan.shared_dir)
    assert stamp["fingerprint_sha"] == vlog_sim_module._fingerprint_sha(fingerprint)
    assert stamp["simv"] == vlog_sim_module._stat_entry(builder._get_simv_path())

    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert reader.compile() == 0
    assert len(calls) == 1
    assert reader.last_build_stamp == stamp


def test_a_run_names_the_executable_it_launched(tmp_path, monkeypatch):
    """The run reports the executable it launched, not the one the stamp check validated."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0
    simv = Path(sim._get_simv_path())
    vouched = sim.last_build_stamp["simv"]
    assert vouched == vlog_sim_module._stat_entry(str(simv))

    simv.write_bytes(simv.read_bytes() + b"\n# rebuilt by a neighbour\n")

    sim._record_launched_simv(str(simv))
    assert sim.last_build_stamp["simv"] == vlog_sim_module._stat_entry(str(simv))
    assert sim.last_build_stamp["simv"] != vouched


def test_a_leaders_recorded_digest_follows_the_stamp_a_sibling_refreshed(
    tmp_path, monkeypatch
):
    """Writing the envelope refreshes each member's recorded digest from the current stamp, since adoption rewrites the listing."""
    _write_source(tmp_path)
    (tmp_path / "gen").mkdir()
    _write_header(tmp_path)
    calls = []
    filelist = ["src/top.sv", "+incdir+gen", "+incdir+inc"]
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )
    leader = _make_sim(tmp_path, monkeypatch, test_name="test_a", filelist=filelist)
    assert leader.compile() == 0
    cold = dict(leader.last_build_stamp)
    (tmp_path / "gen" / "params_test_b.svh").write_text("`define B 1\n")
    sibling = _make_sim(tmp_path, monkeypatch, test_name="test_b", filelist=filelist)
    assert sibling.adopt_group_build() == ("adopted", None)
    assert sibling.last_build_stamp["fingerprint_sha"] != cold["fingerprint_sha"]

    leader.refresh_build_stamp()
    assert leader.last_build_stamp == sibling.last_build_stamp

    # A vanished stamp does not clear the recorded value.
    (Path(cold["build_dir"]) / vlog_sim_module.SHARED_BUILD_STAMP_NAME).unlink()
    leader.refresh_build_stamp()
    assert leader.last_build_stamp == sibling.last_build_stamp


def test_each_of_a_runners_runs_keeps_the_executable_it_launched(tmp_path, monkeypatch):
    """`run_multiple` reports, for each seed, the launch that produced it."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")

    launched = []

    def _execute(*, run_id, **kwargs):
        simv = Path(sim._get_simv_path())
        simv.write_bytes(simv.read_bytes() + b"\n# rebuilt by a neighbour\n")
        sim._record_launched_simv(str(simv))
        launched.append(sim.last_build_stamp["simv"])
        return 0

    monkeypatch.setattr(sim, "execute", _execute)
    monkeypatch.setattr(sim, "pre", lambda **kwargs: None)
    monkeypatch.setattr(
        sim,
        "post",
        # `sim_returncode` is sent with every post() call.
        lambda *, run_id, sim_returncode=None: TestResults(
            "results", {"run_id": run_id}
        ),
    )
    runner = RtlBuddyTestRunner(
        name="rtl_buddy/testrunner",
        root_cfg=sim.root_cfg,
        test_cfg=sim.test_cfg,
        rtl_builder_mode="sim",
        test_runner_mode={"sim_to_stdout": True},
        suite_dir=str(tmp_path),
        share_build=True,
    )
    monkeypatch.setattr(runner, "_create_vlog_sim", lambda: sim)

    results = runner.run_multiple([1, 2, 3])

    assert len(calls) == 1
    assert [res.results["build_stamp"]["simv"] for res in results] == launched
    assert (
        results[0].results["build_stamp"]["simv"]
        != results[2].results["build_stamp"]["simv"]
    )


def test_a_gated_retry_says_what_drifted(tmp_path, monkeypatch, caplog):
    """The single INFO line from a dispatched job names the file that invalidated the stamp."""
    _write_source(tmp_path)
    source = tmp_path / "src" / "top.sv"
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    _touch(source, "module top; wire w; endmodule\n")

    gated = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    gated.expect_prebuilt = True
    with caplog.at_level(logging.INFO):
        assert gated.compile() == 0
    invalid = _events(caplog, "compile.prebuilt_stamp_invalid")
    assert invalid, caplog.text
    assert "src/top.sv" in invalid[0]["reason"]


def test_parse_depend_prerequisites_drops_targets_and_joins_continuations():
    text = "obj/Vtop.cpp obj/Vtop.mk : \\\n  ../src/top.sv \\\n  ../inc/w.svh\n"
    assert vlog_sim_module.parse_depend_prerequisites(text) == [
        "../src/top.sv",
        "../inc/w.svh",
    ]


def test_parse_depend_prerequisites_handles_attached_colon_and_escaped_spaces():
    text = "obj/Vtop.mk: /opt/my\\ tools/verilator ../src/top.sv\n"
    assert vlog_sim_module.parse_depend_prerequisites(text) == [
        "/opt/my tools/verilator",
        "../src/top.sv",
    ]


def test_parse_depend_prerequisites_ignores_mp_phony_rules():
    """`--MP` phony `<prerequisite>:` rules are not collected as inputs. Shape copied from a real `V<prefix>__ver.d` (Verilator 5.048, `--cc --MP`)."""
    text = (
        "obj/Vtop.cpp obj/Vtop.mk  : /opt/verilator_bin ../src/top.sv ../inc/w.svh \n"
        "\n"
        "../src/top.sv:\n"
        "../inc/w.svh:\n"
        "/opt/verilator_bin:\n"
    )
    assert vlog_sim_module.parse_depend_prerequisites(text) == [
        "/opt/verilator_bin",
        "../src/top.sv",
        "../inc/w.svh",
    ]


def test_share_build_stamp_ignores_the_mp_phony_tail(tmp_path, monkeypatch):
    """End to end: the `--MP` tail does not double the stamp entries."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0

    deps = json.loads(_stamp_of(sim).read_text())["deps"]
    assert [entry[0] for entry in deps] == [
        str((tmp_path / "inc" / "w.svh").resolve()),
        str((tmp_path / "src" / "top.sv").resolve()),
    ]
    # Every tracked input exists; a colon-suffixed shadow would stat as absent.
    assert all(entry[1] is not None for entry in deps)


def test_parse_depend_prerequisites_returns_nothing_without_a_separator():
    """A target list is not read as an input list."""
    assert (
        vlog_sim_module.parse_depend_prerequisites("obj/Vtop.cpp obj/Vtop.mk\n") == []
    )


# --- content-hashed stamps ---------------------------------------


def _edit_behind_a_stale_stat(path, text):
    """Rewrite ``path``'s content while keeping its size and mtime, as an NFS client with cached attributes would report."""
    stat = os.stat(path)
    assert len(text.encode()) == stat.st_size, "an equal-size edit is the repro"
    path.write_text(text)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _as_a_fresh_process():
    """Drop the per-process content-hash memo, as when a later process revalidates the stamp."""
    vlog_sim_module._CONTENT_HASH_CACHE.clear()


def test_an_edit_hidden_by_an_unchanged_stat_still_invalidates_the_stamp(
    tmp_path, monkeypatch
):
    """A source whose recorded size and mtime still match but whose bytes changed does not validate the build."""
    src = _write_source(tmp_path, "module top; /* aaa */ endmodule\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    _edit_behind_a_stale_stat(src, "module top; /* bbb */ endmodule\n")
    _as_a_fresh_process()

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2, "the stamp validated against a stale stat"
    # An edit rebuilds the same dir in place.
    assert sim_b._get_simv_path() == sim_a._get_simv_path()


def test_an_edited_header_hidden_by_an_unchanged_stat_invalidates_the_stamp(
    tmp_path, monkeypatch
):
    """The same holds for a header reached only through +incdir+."""
    _write_source(tmp_path)
    header = _write_header(tmp_path, "`define W 08\n")
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    _edit_behind_a_stale_stat(header, "`define W 16\n")
    _as_a_fresh_process()

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2, "a tracked dependency's content was never read"


def test_restoring_identical_content_no_longer_forces_a_rebuild(tmp_path, monkeypatch):
    """Content decides: a changed mtime with identical bytes (`git checkout`, `touch`, regeneration) still validates the stamp."""
    src = _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1

    _touch(src, src.read_text())  # same bytes, new mtime
    _touch(header, header.read_text())

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 1, "identical content should not have rebuilt"


def test_a_dependency_outside_the_project_root_stays_stat_only(tmp_path, monkeypatch):
    """Verilator's own std includes stay on the stat comparison, so a moved mtime alone invalidates them."""
    _write_source(tmp_path)
    outside_dir = tmp_path.parent / f"{tmp_path.name}-toolchain"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "verilated_std.sv"
    outside.write_text("// toolchain-owned\n")
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", str(outside)]
    )

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    deps = {
        entry[0]: entry for entry in json.loads(_stamp_of(sim_a).read_text())["deps"]
    }
    assert deps[os.path.realpath(outside)][3] is None  # never hashed
    assert deps[os.path.realpath(tmp_path / "src" / "top.sv")][3] is not None

    # With no hash, stats decide.
    _touch(outside, outside.read_text())

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2


def test_a_stamp_written_before_content_hashing_rebuilds_once(tmp_path, monkeypatch):
    """A stamp entry without the hash element is not reused; one rebuild rewrites it in the readable shape."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim_a.compile() == 0
    stamp = _stamp_of(sim_a)
    legacy = json.loads(stamp.read_text())
    legacy["sources"] = [entry[:3] for entry in legacy["sources"]]
    legacy["deps"] = [entry[:3] for entry in legacy["deps"]]
    stamp.write_text(json.dumps(legacy, sort_keys=True))

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2

    fresh = json.loads(stamp.read_text())
    assert all(len(entry) == 4 for entry in fresh["sources"])
    assert all(len(entry) == 4 for entry in fresh["deps"])

    sim_c = _make_sim(tmp_path, monkeypatch, test_name="test_c")
    assert sim_c.compile() == 0
    assert len(calls) == 2, "the rebuild happened once, not once per run"


def test_the_compile_key_never_reads_the_content_hash():
    """The hash is stored in the stamp, not in the key; the key is pinned against the exact input set."""
    fingerprint = {
        "cmd": ["verilator", "--binary", "-f", "run.f"],
        "env": {"VERILATOR_ROOT": "/opt/verilator"},
        "sources": [["src/top.sv", 31, 1_700_000_000_000_000_000, "0123456789abcdef"]],
        "toolchain": {
            "exe": "/opt/verilator/bin/verilator",
            "version": "5.020",
            "size": 12,
            "mtime_ns": 7,
        },
    }
    legacy_shape = dict(
        fingerprint, sources=[entry[:3] for entry in fingerprint["sources"]]
    )
    edited = dict(
        fingerprint,
        sources=[entry[:3] + ["fedcba9876543210"] for entry in fingerprint["sources"]],
    )

    key = vlog_sim_module.VlogSim._compile_config_key(fingerprint)
    assert key == vlog_sim_module.VlogSim._compile_config_key(legacy_shape)
    assert key == vlog_sim_module.VlogSim._compile_config_key(edited)
    assert key == "ca6417efe2823758"  # pinned: this input set names this dir


def test_a_file_is_hashed_once_per_process(tmp_path, monkeypatch):
    """A suite validating N stamps over one source set reads each file once."""
    src = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    _as_a_fresh_process()

    reads = []
    real_open = open

    def _counting_open(path, *args, **kwargs):
        if os.path.realpath(str(path)) == os.path.realpath(src):
            reads.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(vlog_sim_module, "open", _counting_open, raising=False)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert _make_sim(tmp_path, monkeypatch, test_name="test_b").compile() == 0
    assert _make_sim(tmp_path, monkeypatch, test_name="test_c").compile() == 0
    assert len(calls) == 1  # one build, two reuses
    assert len(reads) == 1, f"the source was read {len(reads)} times, not memoised"


def test_rtl_above_the_suite_is_hashed_because_the_root_is_the_project_root(
    tmp_path, monkeypatch
):
    """The hashing scope is the project root, not the suite: a source outside the suite gets a hash, and an edit that a stale ``stat`` hides invalidates the build."""
    suite = tmp_path / "verif" / "blk"
    suite.mkdir(parents=True)
    rtl = tmp_path / "rtl" / "a.sv"
    rtl.parent.mkdir(parents=True)
    rtl.write_text("module top; /* aaa */ endmodule\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            suite_dir=suite,
            project_root=tmp_path,
            model_path=suite / "models.yaml",
            filelist=["../../rtl/a.sv"],
        )

    sim_a = _sim("test_a")
    assert sim_a._project_root == os.path.realpath(tmp_path)
    assert sim_a.compile() == 0
    assert len(calls) == 1

    sources = json.loads(_stamp_of(sim_a).read_text())["sources"]
    hashed = [entry for entry in sources if entry[0].endswith("a.sv")]
    assert hashed, f"the source never reached the stamp: {sources}"
    assert all(entry[3] is not None for entry in hashed), (
        "RTL above the suite was recorded stat-only, so a stale NFS stat "
        "still validates a stale build"
    )

    _edit_behind_a_stale_stat(rtl, "module top; /* bbb */ endmodule\n")
    _as_a_fresh_process()

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2, "the stamp validated against a stale stat"


def test_a_vendored_toolchain_under_the_project_root_is_still_not_hashed(
    tmp_path, monkeypatch
):
    """A vendored toolchain under the project root (``verilator_bin``, ``verilated.h``) is not content-hashed."""
    src = _write_source(tmp_path)
    install = tmp_path / "tools" / "verilator"
    bindir = install / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "vendored-verilator"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    header = install / "share" / "verilator" / "include" / "verilated.h"
    header.parent.mkdir(parents=True)
    header.write_text("// toolchain-owned\n")
    monkeypatch.setenv("PATH", str(bindir))

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe="vendored-verilator")
    assert sim._get_toolchain_prefix() == os.path.realpath(install)
    assert sim._tracked_entry(str(header))[3] is None
    assert sim._tracked_entry(str(src))[3] is not None


def test_a_toolchain_at_the_project_root_itself_excludes_nothing(tmp_path, monkeypatch):
    """The toolchain exclusion applies only to a proper subdirectory; a simulator in ``<root>/bin`` does not exclude the design."""
    src = _write_source(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "rooted-verilator"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe="rooted-verilator")
    assert sim._get_toolchain_prefix() is None
    assert sim._tracked_entry(str(src))[3] is not None


def test_a_source_symlinked_in_from_outside_the_project_is_still_hashed(
    tmp_path, monkeypatch
):
    """A symlinked-in RTL tree counts as in-project by the name ``run.f`` declares, not by where the name resolves."""
    external = tmp_path.parent / f"{tmp_path.name}-ip"
    external.mkdir(exist_ok=True)
    ip = external / "ip.sv"
    ip.write_text("module top; /* aaa */ endmodule\n")
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    link = tmp_path / "src" / "ip.sv"
    link.symlink_to(ip)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _sim(test_name):
        return _make_sim(
            tmp_path, monkeypatch, test_name=test_name, filelist=["src/ip.sv"]
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    assert len(calls) == 1
    sources = json.loads(_stamp_of(sim_a).read_text())["sources"]
    hashed = [entry for entry in sources if entry[0].endswith("ip.sv")]
    assert hashed, f"the source never reached the stamp: {sources}"
    assert all(entry[3] is not None for entry in hashed), (
        "a symlinked-in source was recorded stat-only, so a stale NFS stat "
        "still validates a stale build"
    )

    _edit_behind_a_stale_stat(ip, "module top; /* bbb */ endmodule\n")
    _as_a_fresh_process()

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2, "the stamp validated against a stale stat"


def test_a_symlinked_in_dependency_is_hashed_like_a_source(tmp_path, monkeypatch):
    """A header reached through ``+incdir+`` in a symlinked-in tree keeps its declared in-project name and qualifies for hashing."""
    external = tmp_path.parent / f"{tmp_path.name}-inc"
    external.mkdir(exist_ok=True)
    header = external / "w.svh"
    header.write_text("`define W 8\n")
    (tmp_path / "inc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "inc" / "w.svh").symlink_to(header)
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0

    deps = {e[0]: e for e in json.loads(_stamp_of(sim).read_text())["deps"]}
    assert str(tmp_path / "inc" / "w.svh") in deps, sorted(deps)
    assert deps[str(tmp_path / "inc" / "w.svh")][3] is not None


def test_retargeting_an_include_symlink_invalidates_the_stamp(tmp_path, monkeypatch):
    """A retargeted symlink invalidates the stamp because the dependency list re-resolves the link on validation."""
    _write_source(tmp_path)
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "v1.svh").write_text("`define W 8\n")
    (versions / "v2.svh").write_text("`define W 16\n")
    (tmp_path / "inc").mkdir()
    link = tmp_path / "inc" / "w.svh"
    link.symlink_to(versions / "v1.svh")
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            filelist=["src/top.sv", "+incdir+inc"],
        )

    assert _sim("test_a").compile() == 0
    assert _sim("test_b").compile() == 0
    assert len(calls) == 1

    link.unlink()
    link.symlink_to(versions / "v2.svh")
    assert _sim("test_c").compile() == 0
    assert len(calls) == 2, "the include symlink was retargeted but the simv was reused"


def test_a_stamp_with_resolved_dependency_paths_is_rebuilt_once(tmp_path, monkeypatch):
    """A stamp written before deps were keyed by declared path is rebuilt once and then carries the format marker."""
    _write_source(tmp_path)
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "v1.svh").write_text("`define W 8\n")
    (versions / "v2.svh").write_text("`define W 16\n")
    (tmp_path / "inc").mkdir()
    link = tmp_path / "inc" / "w.svh"
    link.symlink_to(versions / "v1.svh")
    calls = []
    _install_fake_builder(
        monkeypatch, calls, depends=["../../src/top.sv", "../../inc/w.svh"]
    )

    def _sim(test_name):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name=test_name,
            filelist=["src/top.sv", "+incdir+inc"],
        )

    sim_a = _sim("test_a")
    assert sim_a.compile() == 0
    stamp_path = _stamp_of(sim_a)
    stamp = json.loads(stamp_path.read_text())
    assert stamp["deps_format"] == vlog_sim_module._DEPS_FORMAT
    assert any(entry[0] == str(link) for entry in stamp["deps"])

    # Rewrite the stamp in the previous format: no marker, realpaths.
    del stamp["deps_format"]
    stamp["deps"] = [
        [os.path.realpath(entry[0]), *entry[1:]] for entry in stamp["deps"]
    ]
    stamp_path.write_text(json.dumps(stamp, sort_keys=True))
    link.unlink()
    link.symlink_to(versions / "v2.svh")

    assert _sim("test_b").compile() == 0
    assert len(calls) == 2, (
        "a pre-format stamp validated a retargeted link's old target"
    )
    assert json.loads(stamp_path.read_text())["deps_format"] == (
        vlog_sim_module._DEPS_FORMAT
    )
    assert _sim("test_c").compile() == 0
    assert len(calls) == 2


def test_an_oversized_input_stays_stat_only_and_says_which(
    tmp_path, monkeypatch, caplog
):
    """A file over the size cap keeps the stat comparison and logs that once."""
    import logging as _logging

    monkeypatch.setattr(vlog_sim_module, "_CONTENT_HASH_MAX_BYTES", 8)
    monkeypatch.setattr(vlog_sim_module, "_HASH_SKIPPED_LARGE", set())
    src = _write_source(tmp_path)
    small = tmp_path / "src" / "tiny.sv"
    small.write_text("//\n")
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")

    with caplog.at_level(_logging.DEBUG):
        assert sim._tracked_entry(str(src))[3] is None
        assert sim._tracked_entry(str(src))[3] is None
    assert sim._tracked_entry(str(small))[3] is not None

    skipped = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.hash_skipped_large"
    ]
    assert len(skipped) == 1, "once per path per process, not once per validation"
    assert os.path.realpath(src) in caplog.text


def test_deps_validation_fails_closed_on_every_shape_it_cannot_read(
    tmp_path, monkeypatch
):
    """Every stamp shape this version cannot read answers "rebuild" and never raises."""
    _write_source(tmp_path)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")

    def _refuse(path, **kwargs):
        raise AssertionError(f"an unreadable stamp shape reached os.stat: {path!r}")

    # The guards decide before anything is stat'd: `os.stat` would take an int path for a file descriptor.
    monkeypatch.setattr(vlog_sim_module, "_hashed_stat_entry", _refuse)

    assert sim._deps_unchanged("test_a", [["/x", 1, 2]]) is False
    assert sim._deps_unchanged("test_a", [[5, 1, 2, "abcd"]]) is False
    assert sim._deps_unchanged("test_a", ["not-an-entry"]) is False
    assert sim._deps_unchanged("test_a", 5) is False
    assert sim._deps_unchanged("test_a", {"/x": [1, 2, "abcd"]}) is False
    assert sim._deps_unchanged("test_a", None) is False


def test_nothing_but_a_regular_file_is_ever_opened_for_hashing(tmp_path, monkeypatch):
    """Stats decide whether to read a dep, before it is opened; a FIFO would block forever on open."""
    _write_source(tmp_path)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    a_directory = tmp_path / "src"

    opened = []
    real_open = open

    def _recording_open(path, *args, **kwargs):
        opened.append(os.path.realpath(str(path)))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(vlog_sim_module, "open", _recording_open, raising=False)

    entry = sim._tracked_entry(str(a_directory))
    assert entry[1] is not None, "still tracked, still stat'd"
    assert entry[3] is None, "no content hash for something that is not a file"
    assert os.path.realpath(a_directory) not in opened


def test_entry_comparison_fails_closed_on_empty_entries():
    """Two empty entries are the same length and index into nothing."""
    assert vlog_sim_module._entry_lists_match([[]], [[]]) is False
    assert vlog_sim_module._entry_matches([], []) is False


def test_shared_build_dir_helper_layout():
    assert shared_build_dir("/tmp/suite", "cafe0123") == Path(
        "/tmp/suite/artefacts/.shared-builds/obj_dir_cafe0123"
    )


def test_test_runner_threads_share_build_to_vlog_sim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model_cfg = DummyModelCfg(tmp_path / "models.yaml")
    test_cfg = DummyTestCfg("basic", model_cfg)
    runner = RtlBuddyTestRunner(
        name="rtl_buddy/testrunner",
        root_cfg=DummyRootCfg(DummyBuilderCfg()),
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        test_runner_mode={"sim_to_stdout": True},
        suite_dir=str(tmp_path),
        share_build=True,
    )
    assert runner._create_vlog_sim().share_build is True


def test_share_build_on_vcs_strips_output_opts_from_extra_compile_flags(
    tmp_path, monkeypatch
):
    """A subclass-injected -o does not override the shared build's own output; `_get_extra_compile_flags()` is appended after the shared-build argv."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe="vcs", family="vcs")
    monkeypatch.setattr(
        sim, "_get_extra_compile_flags", lambda: ["-o", "sneaky", "-Mdir=sneakier"]
    )
    assert sim.compile() == 0

    cmd = calls[0]["cmd"]
    shared = Path(sim._get_simv_path()).parent
    assert "sneaky" not in cmd and "-Mdir=sneakier" not in cmd
    assert cmd.count("-o") == 1
    assert cmd[cmd.index("-o") + 1] == str(shared / "simv")
    assert (shared / "simv").is_file()
    assert sim._shared_build_is_valid(shared, None) is False  # wrong fingerprint
    assert Path(sim._get_simv_path()).is_file()


def test_icarus_wrapper_args_separate_the_compile_key(tmp_path, monkeypatch):
    """The shared `simv` wrapper includes _icarus_vvp_extra_args(), so tests that differ only in those args get different builds.
    CocotbSim adds the VPI module there without adding Icarus compile flags.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    plain = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="iverilog", family="icarus"
    )
    vpi = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="iverilog", family="icarus"
    )
    monkeypatch.setattr(
        vpi, "_icarus_vvp_extra_args", lambda: ["-M", "/libs", "-m", "libcocotbvpi"]
    )

    assert plain.compile() == 0
    assert vpi.compile() == 0
    assert len(calls) == 2
    assert plain._get_simv_path() != vpi._get_simv_path()
    assert "libcocotbvpi" in Path(vpi._get_simv_path()).read_text()
    assert "libcocotbvpi" not in Path(plain._get_simv_path()).read_text()


def test_relative_builder_simv_override_is_logged(tmp_path, monkeypatch, caplog):
    """The shared build warns when it discards a relative builder-simv."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe="vcs",
        family="vcs",
        simv="bin/mysim",
    )
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0
    assert "builder-simv" in caplog.text
    assert "bin/mysim" in caplog.text


def test_default_builder_simv_override_is_not_logged(tmp_path, monkeypatch, caplog):
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe="vcs", family="vcs")
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0
    assert "builder-simv" not in caplog.text


def test_a_gated_job_that_compiles_anyway_says_so(tmp_path, monkeypatch, caplog):
    """The build-job gate orders the elements but does not exclude them; a stamp that fails to validate produces a WARNING, since the resulting `Compile failed` would read as a design error."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    sim.expect_prebuilt = True
    with caplog.at_level(_logging.WARNING):
        assert sim.compile() == 0

    assert "compiling despite being gated on a build job" in caplog.text


def test_a_gated_job_that_reuses_the_build_is_silent(tmp_path, monkeypatch, caplog):
    """The normal path does not warn."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    reader.expect_prebuilt = True
    with caplog.at_level(_logging.WARNING):
        assert reader.compile() == 0

    assert len(calls) == 1
    assert "compiling despite being gated" not in caplog.text


def test_clear_run_outputs_unlinks_every_named_run(tmp_path, monkeypatch):
    """`run_multiple`'s one compile clears the previous outputs of runs 1..N, and keeps the seed `--replay` reads."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=1)
    stale, kept = [], []
    for run_id in (1, 2, 3):
        run_dir = Path(sim._get_artifact_dir(run_id=run_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "test.log",
            "test.err",
            "coverage.dat",
            "compile.retry.log",
            "result.json",
        ):
            (run_dir / name).write_text("an old run's output\n")
            stale.append(run_dir / name)
        (run_dir / "test.randseed").write_text("41\n")
        kept.append(run_dir / "test.randseed")

    sim.clear_run_outputs([1, 2, 3])
    assert [p for p in stale if p.exists()] == []
    assert all(p.exists() for p in kept)


def test_clear_run_outputs_logs_a_file_it_cannot_remove(tmp_path, monkeypatch, caplog):
    """An unremovable output is reported and does not stop the run."""
    import logging as _logging

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    # A directory where test.log belongs: unlink() raises.
    (Path(sim._get_artifact_dir()) / "test.log").mkdir(parents=True)
    (Path(sim._get_artifact_dir()) / "test.err").write_text("old\n")

    with caplog.at_level(_logging.WARNING):
        sim.clear_run_outputs([None])

    assert not (Path(sim._get_artifact_dir()) / "test.err").exists()
    [event] = _events(caplog, "test.stale_output_unremovable")
    assert event["path"].endswith("test.log")


def _runner_for(sim, monkeypatch, *, run_id=None):
    runner = RtlBuddyTestRunner(
        name="rtl_buddy/testrunner",
        root_cfg=sim.root_cfg,
        test_cfg=sim.test_cfg,
        rtl_builder_mode="sim",
        test_runner_mode={"sim_to_stdout": True},
        run_id=run_id,
        run_depth=RunDepth.POST,
        share_build=True,
    )
    monkeypatch.setattr(runner, "_create_vlog_sim", lambda: sim)
    return runner


def _seed_passing_run(sim, run_id=None):
    """Leave the outputs of an earlier passing run in the artifact directory."""
    run_dir = Path(sim._get_artifact_dir(run_id=run_id))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test.log").write_text("PASS\n")
    (run_dir / "test.err").write_text("")
    return run_dir


def test_a_failed_compile_leaves_no_earlier_test_log(tmp_path, monkeypatch):
    """A run whose compile fails removes the previous run's test.log, so its PASS banner cannot be read as this run's."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    run_dir = _seed_passing_run(sim)

    result = _runner_for(sim, monkeypatch).run()

    assert isinstance(result, CompileFailResults)
    assert len(calls) == 1
    assert not (run_dir / "test.log").exists()
    assert not (run_dir / "test.err").exists()


def test_a_fanned_out_failed_compile_leaves_no_earlier_test_log(tmp_path, monkeypatch):
    """`run_multiple` clears every run's directory, not only the first run's."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=1)
    run_dirs = [_seed_passing_run(sim, run_id) for run_id in (1, 2)]

    results = _runner_for(sim, monkeypatch, run_id=1).run_multiple([1, 2])

    assert all(isinstance(res, CompileFailResults) for res in results)
    assert not any((d / "test.log").exists() for d in run_dirs)


def test_the_build_jobs_prepare_clears_the_earlier_test_log(tmp_path, monkeypatch):
    """The dispatched build job drives `prepare()` directly; it clears the test's outputs too, so a sim job that never starts leaves none behind."""
    _write_source(tmp_path)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    run_dir = _seed_passing_run(sim)
    runner = _runner_for(sim, monkeypatch)

    assert runner.prepare() is None
    assert not (run_dir / "test.log").exists()


def test_a_gated_job_whose_build_failed_leaves_no_earlier_test_log(
    tmp_path, monkeypatch
):
    """Under dispatch, the sim job that reports the build job's compile failure also clears the earlier run's test.log."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[{"test": "test_a", "returncode": 1, "error_tail": ["%Error: x"]}],
    )
    run_dir = _seed_passing_run(sim)

    result = _runner_for(sim, monkeypatch).run()

    assert isinstance(result, CompileFailResults)
    assert calls == []
    assert not (run_dir / "test.log").exists()


def test_a_stale_retry_log_is_cleared_before_a_failing_pre(tmp_path, monkeypatch):
    """A PRE failure does not leave the previous invocation's `compile.retry.log` paired with the new SetupFail envelope."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=1)
    stale = Path(sim._get_retry_transcript_path())
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("%Error: a previous invocation's retry\n")
    hook = tmp_path / "boom_preproc.py"
    hook.write_text("raise RuntimeError('pre exploded')\n")
    monkeypatch.setattr(sim.test_cfg, "get_preproc_path", lambda: str(hook))

    assert sim.pre() is not None
    assert not stale.exists()


def test_a_stale_retry_log_does_not_survive_the_next_compile(tmp_path, monkeypatch):
    """`compile.retry.log` describes exactly one run's retry and is removed when a later run does not retry."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    reader.expect_prebuilt = True
    stale = Path(reader._get_compile_work_dir()) / "compile.retry.log"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("%Error: a previous run's retry\n")

    assert reader.compile() == 0
    assert not stale.exists()


# --- a gated job vs. a failed build -------------------------------

# The build job's compile.log; every test asserts it is unchanged byte for byte afterwards.
_BUILD_TRANSCRIPT = (
    "Command: verilator --Mdir obj_dir -f run.f\n\n"
    "=== stderr ===\n"
    "%Error: src/top.sv:3:7: Signal is not driven: 'q'\n"
    "%Error: Exiting due to 1 error(s)\n"
    "\n=== stdout ===\n"
)


def _seed_build_transcript(sim):
    """Put the build job's compile.log where this sim looks for it."""
    path = Path(sim._get_compile_transcript_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BUILD_TRANSCRIPT)
    return path


def _write_build_envelope(tmp_path, *, failed, builds=None, built=None, partial=False):
    """Write a build job's envelope. ``built`` defaults to "test_a, if it passed".
    Pass ``built`` explicitly to omit the test from both lists (a config the build job never reached).
    ``partial`` writes the mid-run envelope that released keys' simulation jobs read.
    """
    from rtl_buddy.runner.result_io import write_build_result_json

    if built is None:
        built = [name for name in ("test_a",) if name not in failed]
    return write_build_result_json(
        tmp_path / "artefacts" / ".dispatch" / "build-result-1.json",
        built=built,
        failed=failed,
        builds=builds,
        partial=partial,
    )


def _events(caplog, name):
    return [
        record.rtl_fields
        for record in caplog.records
        if getattr(record, "rtl_event", None) == name
    ]


def test_a_gated_job_does_not_retry_a_compile_the_build_job_already_failed(
    tmp_path, monkeypatch, caplog
):
    """A deterministic compile error is not retried in the sim job; the build's exit status and error lines are reported and its transcript is left alone."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[
            {
                "test": "test_a",
                "returncode": 1,
                "transcript": os.path.join("artefacts", "test_a", "compile.log"),
                "error_tail": [
                    "%Error: src/top.sv:3:7: Signal is not driven: 'q'",
                    "%Error: Exiting due to 1 error(s)",
                ],
            }
        ],
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    # No builder ran, and neither transcript was touched.
    assert calls == []
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    assert not (compile_log.parent / "compile.retry.log").exists()
    desc = sim.compile_fail_desc
    assert "Signal is not driven" in desc
    assert "(exit 1)" in desc
    assert str(compile_log) in desc
    assert "\n" not in desc
    # The retry WARNING does not fire, since nothing is compiling.
    assert _events(caplog, "compile.prebuilt_stamp_invalid") == []
    assert _events(caplog, "compile.build_job_failed")[0]["returncode"] == 1


def test_a_gated_job_retries_a_failure_recorded_without_compiler_evidence(
    tmp_path, monkeypatch, caplog
):
    """A `failed` entry alone is not a compile verdict; only a per-build record with a `returncode` stops the retry."""
    import logging as _logging

    cases = (
        # An older build job: listed as failed, with no builds records.
        ("no builds record", {"failed": ["test_a"]}),
        # A worker exception or setup failure: a record without a returncode.
        (
            "record without returncode",
            {
                "failed": ["test_a"],
                "builds": [{"test": "test_a", "error_tail": ["hook exploded"]}],
            },
        ),
    )
    _write_source(tmp_path)
    for case_i, (label, shape) in enumerate(cases):
        calls = []
        _install_fake_builder(monkeypatch, calls)
        # A distinct define per case avoids reuse of an earlier stamp.
        sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", pd={"CASE": case_i})
        compile_log = _seed_build_transcript(sim)
        sim.expect_prebuilt = True
        sim.build_result_json = _write_build_envelope(tmp_path, **shape)

        caplog.clear()
        with caplog.at_level(_logging.DEBUG):
            assert sim.compile() == 0, label

        assert len(calls) == 1, label
        assert _events(caplog, "compile.prebuilt_stamp_invalid"), label
        assert _events(caplog, "compile.build_job_failed") == [], label
        assert compile_log.read_text() == _BUILD_TRANSCRIPT, label
        assert sim.compile_fail_desc is None, label


def test_a_no_evidence_retry_that_fails_writes_the_retry_log(
    tmp_path, monkeypatch, caplog
):
    """A retry without build-side evidence writes to `compile.retry.log` beside the build job's `compile.log`, and its failure is the sim job's own."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[{"test": "test_a", "error_tail": ["hook exploded"]}],
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert len(calls) == 1
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    retry_log = compile_log.parent / "compile.retry.log"
    assert retry_log.is_file()
    assert _events(caplog, "compile.prebuilt_stamp_invalid")
    assert sim.compile_fail_desc is None


def test_a_siblings_retry_log_survives_another_runs_compile(tmp_path, monkeypatch):
    """Retry logs are per run: run 2 neither removes nor overwrites run 1's `run-0001/compile.retry.log`,
    and each retry that runs records a transcript in its own run directory.
    """
    _write_source(tmp_path)

    calls1 = []
    _install_fake_builder(monkeypatch, calls1, returncode=1)
    run1 = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=1)
    compile_log = _seed_build_transcript(run1)
    run1.expect_prebuilt = True
    run1.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[{"test": "test_a", "error_tail": ["hook exploded"]}],
    )
    assert run1.compile() == 1
    assert len(calls1) == 1
    run1_retry = Path(run1._get_artifact_dir(run_id=1)) / "compile.retry.log"
    assert run1_retry.is_file()
    run1_evidence = run1_retry.read_text()
    assert not (compile_log.parent / "compile.retry.log").exists()

    calls2 = []
    _install_fake_builder(monkeypatch, calls2, stdout="run 2 recompiled\n")
    run2 = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=2)
    run2.expect_prebuilt = True
    run2.build_result_json = run1.build_result_json
    assert run2.compile() == 0
    assert len(calls2) == 1

    assert run1_retry.read_text() == run1_evidence
    run2_retry = Path(run2._get_artifact_dir(run_id=2)) / "compile.retry.log"
    assert run2_retry.is_file()
    assert "run 2 recompiled" in run2_retry.read_text()
    assert "run 2 recompiled" not in run1_evidence
    assert not (compile_log.parent / "compile.retry.log").exists()
    assert compile_log.read_text() == _BUILD_TRANSCRIPT


def test_the_no_retry_verdict_holds_only_for_the_same_inputs(
    tmp_path, monkeypatch, caplog
):
    """A recorded failure of a different compile (a `fingerprint_sha` mismatch) earns the retry; a match, or a record without one, keeps the verdict."""
    import logging as _logging

    from rtl_buddy.tools.vlog_sim import _fingerprint_sha

    _write_source(tmp_path)

    # The build job records the sha of a real failing compile.
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)
    build_sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert build_sim.compile() == 1
    recorded = build_sim.last_compile_failure
    assert recorded["fingerprint_sha"]

    # Same inputs: the gated job honours the verdict and does not retry.
    calls2 = []
    _install_fake_builder(monkeypatch, calls2)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[
            {
                "test": "test_a",
                "returncode": 1,
                "fingerprint_sha": recorded["fingerprint_sha"],
            }
        ],
    )
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1
    assert calls2 == []  # suppressed: same compile, known verdict
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    assert _events(caplog, "compile.build_failure_inputs_changed") == []
    assert _fingerprint_sha(None) is None

    # The inputs moved: the same record with a different sha retries.
    caplog.clear()
    calls3 = []
    _install_fake_builder(monkeypatch, calls3)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[{"test": "test_a", "returncode": 1, "fingerprint_sha": "0" * 64}],
    )
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0  # the retry ran, and the new inputs passed
    assert len(calls3) == 1
    assert _events(caplog, "compile.build_failure_inputs_changed")
    assert _events(caplog, "compile.prebuilt_stamp_invalid")
    assert _events(caplog, "compile.build_job_failed") == []
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    assert sim.compile_fail_desc is None


def test_the_fingerprint_sha_agrees_with_the_stamp_comparison(tmp_path, monkeypatch):
    """`_fingerprint_sha` agrees with the stamp comparison: a new mtime with identical bytes does not move the sha, and an edited byte does."""
    from rtl_buddy.tools.vlog_sim import (
        _entry_lists_match,
        _fingerprint_sha,
    )

    src = _write_source(tmp_path)

    def fingerprint():
        sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
        return sim._compile_plan().fingerprint

    before = fingerprint()
    assert before["sources"][0][3], "the source must be content-hashed at all"

    # Same bytes, new mtime: the sha does not move.
    os.utime(src, (0, 0))
    touched = fingerprint()
    assert touched["sources"] != before["sources"]  # the mtimes really moved
    assert _entry_lists_match(before["sources"], touched["sources"])
    assert _fingerprint_sha(touched) == _fingerprint_sha(before)

    src.write_text("module top; wire q; endmodule\n")
    edited = fingerprint()
    assert not _entry_lists_match(before["sources"], edited["sources"])
    assert _fingerprint_sha(edited) != _fingerprint_sha(before)


def test_a_gated_retry_writes_beside_the_build_log_never_over_it(
    tmp_path, monkeypatch, caplog
):
    """A config the build job never reached earns a retry, written to `compile.retry.log`, and `compile.failed` names the file written."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(tmp_path, failed=[], built=[])

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert len(calls) == 1  # the retry really ran
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    retry_log = compile_log.parent / "compile.retry.log"
    assert retry_log.is_file()
    assert "Command: " in retry_log.read_text()
    assert _events(caplog, "compile.prebuilt_stamp_invalid")
    assert _events(caplog, "compile.failed")[0]["transcript"] == str(retry_log)
    assert sim.compile_fail_desc is None


def test_a_gated_job_does_not_recompile_a_build_the_build_job_made(
    tmp_path, monkeypatch, caplog
):
    """A successful build record ends the retry whatever the stamp says: the test fails once with what drifted, without recompiling."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    own_sha = vlog_sim_module._fingerprint_sha(sim._compile_plan().fingerprint)
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=[],
        builds=[{"test": "test_a", "reused": False, "fingerprint_sha": own_sha}],
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == []  # no builder ran
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    assert not (compile_log.parent / "compile.retry.log").exists()
    assert _events(caplog, "compile.prebuilt_stamp_invalid") == []
    rejected = _events(caplog, "compile.build_stamp_rejected")
    assert rejected and rejected[0]["inputs_differ"] is False
    # The reason the stamp check recorded reaches the summary row in one line and says nothing is compiling.
    desc = sim.compile_fail_desc
    assert "no stamp or no simv" in desc
    assert "not recompiling" in desc
    assert "\n" not in desc


def test_a_released_job_declines_on_a_partial_envelope(tmp_path, monkeypatch, caplog):
    """A partial envelope that names the test as built gives the same verdict as a complete one."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    own_sha = vlog_sim_module._fingerprint_sha(sim._compile_plan().fingerprint)
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=[],
        builds=[{"test": "test_a", "reused": False, "fingerprint_sha": own_sha}],
        partial=True,
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == []  # no builder ran
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    assert not (compile_log.parent / "compile.retry.log").exists()
    rejected = _events(caplog, "compile.build_stamp_rejected")
    assert rejected and rejected[0]["inputs_differ"] is False
    assert "not recompiling" in sim.compile_fail_desc


def test_a_partial_envelope_that_has_not_reached_this_test_still_retries(
    tmp_path, monkeypatch, caplog
):
    """A test listed in neither list is "never reached" under a partial envelope too, and earns the retry."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path, failed=[], built=["test_b"], builds=[], partial=True
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0  # the retry ran, and passed

    assert len(calls) == 1
    assert _events(caplog, "compile.prebuilt_stamp_invalid")
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    assert sim.compile_fail_desc is None


def test_a_complete_envelope_is_byte_identical_to_a_pre_partial_one(tmp_path):
    """The `partial` key is only ever written true."""
    import json

    from rtl_buddy.runner.result_io import (
        load_build_result_json,
        write_build_result_json,
    )

    complete = write_build_result_json(
        tmp_path / "complete.json", built=["a"], failed=[], builds=[{"test": "a"}]
    )
    raw = json.loads(complete.read_text())
    assert "partial" not in raw
    assert load_build_result_json(complete)["partial"] is False

    partial = write_build_result_json(
        tmp_path / "partial.json",
        built=["a"],
        failed=[],
        builds=[{"test": "a"}],
        partial=True,
    )
    assert json.loads(partial.read_text())["partial"] is True
    assert load_build_result_json(partial)["partial"] is True
    # Additive: the schema version does not change.
    assert (
        json.loads(partial.read_text())["schema_version"]
        == (json.loads(complete.read_text())["schema_version"])
    )


def _stamp_path(sim):
    """Return the shared stamp this sim's compile key validates against."""
    return Path(sim.compile_group_dir()) / vlog_sim_module.SHARED_BUILD_STAMP_NAME


def _gated(tmp_path, monkeypatch, name, *, envelope, **kwargs):
    sim = _make_sim(tmp_path, monkeypatch, test_name=name, **kwargs)
    _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = envelope
    return sim


def test_a_declining_gated_job_leaves_the_stamp_for_its_siblings(
    tmp_path, monkeypatch, caplog
):
    """One element's drift does not remove the shared stamp, so sibling elements on the same key still validate."""
    import logging as _logging

    source = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    stamp = _stamp_path(_make_sim(tmp_path, monkeypatch, test_name="test_a"))
    stamped = stamp.read_bytes()
    calls.clear()

    envelope = _write_build_envelope(tmp_path, failed=[], built=["test_a", "test_b"])
    # test_a's stamp check passes...
    reuser = _gated(tmp_path, monkeypatch, "test_a", envelope=envelope)
    reuser._compile_plan()
    # ...while test_b's follows an edit and fails.
    _touch(source, "module top; wire drifted; endmodule\n")
    decliner = _gated(tmp_path, monkeypatch, "test_b", envelope=envelope)

    with caplog.at_level(_logging.DEBUG):
        assert decliner.compile() == 1
    assert _events(caplog, "compile.build_stamp_rejected")
    assert stamp.read_bytes() == stamped, "a declining job removed the shared stamp"

    caplog.clear()
    with caplog.at_level(_logging.DEBUG):
        assert reuser.compile() == 0
    assert reuser.last_compile["reused"] is True
    assert _events(caplog, "compile.prebuilt_stamp_invalid") == []
    assert calls == [], "the fan-out recompiled after one element declined"


def test_a_gated_job_whose_build_failed_also_leaves_the_stamp(
    tmp_path, monkeypatch, caplog
):
    """A recorded compile failure keeps the shared stamp too, since it compiles nothing."""
    import logging as _logging

    source = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    stamp = _stamp_path(_make_sim(tmp_path, monkeypatch, test_name="test_a"))
    stamped = stamp.read_bytes()
    calls.clear()

    _touch(source, "module top; wire drifted; endmodule\n")
    sim = _gated(
        tmp_path,
        monkeypatch,
        "test_b",
        envelope=_write_build_envelope(
            tmp_path,
            failed=["test_b"],
            built=["test_a"],
            builds=[{"test": "test_b", "returncode": 3}],
        ),
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 3

    assert calls == []
    assert _events(caplog, "compile.build_job_failed")
    assert stamp.read_bytes() == stamped, "a failed-verdict job removed the stamp"


def test_a_gated_job_declines_on_a_missing_stamp_beside_a_real_simv(
    tmp_path, monkeypatch, caplog
):
    """A missing stamp, as well as a mismatching one, defers to the build job's envelope instead of recompiling."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    calls.clear()
    sim = _gated(
        tmp_path,
        monkeypatch,
        "test_b",
        envelope=_write_build_envelope(tmp_path, failed=[], built=["test_b"]),
    )
    stamp = _stamp_path(sim)
    simv = Path(sim._get_simv_path())
    stamp.unlink()
    assert simv.is_file(), "the build the job is gated on has to still be there"

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == [], "a gated job recompiled a build whose stamp went missing"
    (rejected,) = _events(caplog, "compile.build_stamp_rejected")
    assert rejected["reason"] == "no stamp or no simv in the build dir"
    assert "not recompiling" in sim.compile_fail_desc


def _break_stamp_writes(monkeypatch, *, error=errno.EROFS):
    """Fail every stamp write and leave other artefact writes alone."""
    real = vlog_sim_module.VlogSim._replace_text

    def _refuse(self, path, text):
        if Path(path).name == vlog_sim_module.SHARED_BUILD_STAMP_NAME:
            raise OSError(error, os.strerror(error), str(path))
        return real(self, path, text)

    monkeypatch.setattr(vlog_sim_module.VlogSim, "_replace_text", _refuse)


def test_a_stamp_that_cannot_be_written_does_not_fail_a_passing_compile(
    tmp_path, monkeypatch, caplog
):
    """When only the stamp write fails, the builder's status is kept, the failure is reported, and it is recorded for the gated jobs."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    _break_stamp_writes(monkeypatch)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0

    assert len(calls) == 1
    (failed,) = _events(caplog, "compile.stamp_write_failed")
    assert failed["test"] == "test_a"
    assert failed["stamp"].endswith(vlog_sim_module.SHARED_BUILD_STAMP_NAME)
    assert failed["error"]
    assert _events(caplog, "compile.build_stamp_written") == []
    assert sim.stamp_write_failed is True
    assert sim.last_build_stamp is None
    assert not _stamp_path(sim).exists()
    assert Path(sim._get_simv_path()).is_file()


def test_a_stamp_write_that_fails_leaves_no_partial_file(tmp_path, monkeypatch):
    """The stamp is written atomically, and a temp file that cannot be moved into place is removed."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    _break_stamp_writes(monkeypatch)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0
    build_dir = Path(sim.compile_group_dir())
    assert [p.name for p in build_dir.glob("*.tmp")] == []
    assert not (build_dir / vlog_sim_module.SHARED_BUILD_STAMP_NAME).exists()


def test_a_gated_job_declines_when_the_build_job_could_not_stamp(
    tmp_path, monkeypatch, caplog
):
    """`stamp_written: false` is read as "built" with its own reason, and no recompile."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    _break_stamp_writes(monkeypatch)
    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    calls.clear()

    sim = _gated(
        tmp_path,
        monkeypatch,
        "test_a",
        envelope=_write_build_envelope(
            tmp_path,
            failed=[],
            builds=[{"test": "test_a", "reused": False, "stamp_written": False}],
        ),
    )
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == [], "a gated job recompiled a build the job could not stamp"
    (rejected,) = _events(caplog, "compile.build_stamp_rejected")
    assert rejected["stamp_unwritten"] is True
    assert rejected["reason"] == "the build job could not write the build stamp"
    desc = sim.compile_fail_desc
    assert "could not write its build stamp" in desc
    assert "not recompiling" in desc
    assert "\n" not in desc


def test_a_gated_job_with_a_stamped_build_is_not_told_the_stamp_failed(
    tmp_path, monkeypatch, caplog
):
    """An envelope without a `stamp_written` key means stamped."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _gated(
        tmp_path,
        monkeypatch,
        "test_a",
        envelope=_write_build_envelope(
            tmp_path, failed=[], builds=[{"test": "test_a", "reused": False}]
        ),
    )
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == []
    (rejected,) = _events(caplog, "compile.build_stamp_rejected")
    assert rejected["stamp_unwritten"] is False
    assert "no stamp or no simv" in sim.compile_fail_desc


def test_a_gated_job_says_when_its_inputs_are_not_the_build_jobs(
    tmp_path, monkeypatch, caplog
):
    """A recorded digest that differs from this job's derives a different diagnosis but still no recompile."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=[],
        builds=[{"test": "test_a", "reused": False, "fingerprint_sha": "0" * 64}],
    )

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == []
    rejected = _events(caplog, "compile.build_stamp_rejected")
    assert rejected[0]["inputs_differ"] is True
    assert rejected[0]["recorded_sha"] == "0" * 64
    assert "different compile inputs" in sim.compile_fail_desc


def test_a_gated_job_declines_on_a_built_record_that_carries_no_digest(
    tmp_path, monkeypatch, caplog
):
    """A build record without a digest still counts as built; the `built` list decides."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(tmp_path, failed=[])

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert calls == []
    assert _events(caplog, "compile.build_stamp_rejected")[0]["inputs_differ"] is False


def test_a_gated_job_that_validates_the_stamp_never_asks_the_envelope(
    tmp_path, monkeypatch, caplog
):
    """A healthy fan-out reuses the build silently; the envelope verdict is reached only after the stamp check fails."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(tmp_path, failed=[])
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0

    assert len(calls) == 1
    assert _events(caplog, "compile.build_stamp_rejected") == []
    assert sim.compile_fail_desc is None


def test_a_gated_retry_falls_back_to_todays_behaviour_without_an_envelope(
    tmp_path, monkeypatch, caplog
):
    """A missing, corrupt or non-passing envelope retries; only one that names this test as failed stops the retry."""
    import logging as _logging

    _write_source(tmp_path)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    cases = (
        ("absent", tmp_path / "nope.json"),
        ("corrupt", corrupt),
        ("not passed at all", None),
    )
    for case_i, (label, envelope) in enumerate(cases):
        calls = []
        _install_fake_builder(monkeypatch, calls)
        # A distinct define per case avoids reuse of an earlier stamp.
        sim = _make_sim(
            tmp_path, monkeypatch, test_name=f"test_{case_i}", pd={"CASE": case_i}
        )
        compile_log = _seed_build_transcript(sim)
        sim.expect_prebuilt = True
        sim.build_result_json = envelope

        caplog.clear()
        with caplog.at_level(_logging.DEBUG):
            assert sim.compile() == 0, label

        assert len(calls) == 1, label
        assert _events(caplog, "compile.prebuilt_stamp_invalid"), label
        assert _events(caplog, "compile.build_job_failed") == [], label
        # The retry's transcript goes to compile.retry.log, so the build job's compile.log is unchanged.
        assert compile_log.read_text() == _BUILD_TRANSCRIPT, label
        assert (
            Path(sim._get_artifact_dir(run_id=sim.run_id)) / "compile.retry.log"
        ).is_file(), label


def test_an_ungated_compile_still_writes_compile_log(tmp_path, monkeypatch):
    """The `.retry.` name is used only for gated retries; local runs and dispatched jobs without a build job write `compile.log`."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 1

    transcript = Path(sim._get_compile_transcript_path())
    assert transcript.name == "compile.log"
    assert transcript.is_file()
    assert not (transcript.parent / "compile.retry.log").exists()
    # The record a dispatched build job puts in its envelope, including the `fingerprint_sha` of the inputs that failed.
    failure = sim.last_compile_failure
    assert failure["returncode"] == 1
    assert failure["transcript"] == str(transcript)
    sha = failure["fingerprint_sha"]
    assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")


def test_a_gated_build_failure_becomes_the_compile_fail_desc(tmp_path, monkeypatch):
    """The failure desc reaches the run summary through TestRunner, not only VlogSim."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    sim.build_result_json = _write_build_envelope(
        tmp_path,
        failed=["test_a"],
        builds=[
            {
                "test": "test_a",
                "returncode": 2,
                "error_tail": ["%Error: src/top.sv:3:7: Signal is not driven: 'q'"],
            }
        ],
    )

    runner = RtlBuddyTestRunner(
        name="rtl_buddy/testrunner",
        root_cfg=sim.root_cfg,
        test_cfg=sim.test_cfg,
        rtl_builder_mode="sim",
        test_runner_mode={"sim_to_stdout": False},
        share_build=True,
        expect_prebuilt=True,
        build_result_json=sim.build_result_json,
        suite_dir=str(tmp_path),
    )
    monkeypatch.setattr(runner, "_run_pre", lambda **_kwargs: None)
    runner._vlog_sim = sim

    results = runner.compile_prepared()
    assert results.results["result"] == "FAIL"
    assert "Signal is not driven" in results.results["desc"]
    assert "(exit 2)" in results.results["desc"]
    assert runner.last_compile_failure["returncode"] == 2
    assert calls == []


def test_share_build_unsupported_reason_is_the_predicate_the_head_uses():
    """The head plans reservations and gating from this predicate, and the job takes the unshared path from it; an absolute `builder-simv:` declines sharing as well as the family."""
    reason = vlog_sim_module.share_build_unsupported_reason

    assert reason(DummyBuilderCfg(simulator_family="verilator")) is None
    assert reason(DummyBuilderCfg(simulator_family="vcs")) is None
    assert "no shared-build support" in reason(
        DummyBuilderCfg(simulator_family="questa")
    )
    # The two predicates agree here.
    assert "builder-simv is an absolute path" in reason(
        DummyBuilderCfg(simulator_family="vcs", simv="/pinned/simv")
    )
    # Verilator and Icarus are redirected wholesale, so a pinned simv is overridden and sharing still applies.
    assert (
        reason(DummyBuilderCfg(simulator_family="verilator", simv="/pinned/simv"))
        is None
    )


# --- toolchain identity -----------------------------------------
# The stamp records the resolved toolchain (path, version banner), so pointing the project at a different install invalidates it.


def _fake_toolchain(tmp_path, name, version, binary="verilator"):
    """An executable that answers `--version` / `-V` and nothing else."""
    exe = tmp_path / name / binary
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f'#!/bin/sh\necho "{version}"\n')
    exe.chmod(0o755)
    return exe


def test_share_build_keeps_a_separate_build_per_toolchain(tmp_path, monkeypatch):
    """Two installs get two build dirs, so neither overwrites the other's simv."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    old = _fake_toolchain(tmp_path, "tc-a", "Verilator 5.048 2024-01-01")
    new = _fake_toolchain(tmp_path, "tc-b", "Verilator 5.049 devel rev vBBBB")

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(old))
    assert sim_a.compile() == 0
    assert len(calls) == 1

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(new))
    assert sim_b.compile() == 0
    assert len(calls) == 2, "the second toolchain must not reuse the first's build"
    assert sim_a._get_simv_path() != sim_b._get_simv_path()

    sim_c = _make_sim(tmp_path, monkeypatch, test_name="test_c", exe=str(old))
    assert sim_c.compile() == 0
    assert len(calls) == 2
    assert sim_c._get_simv_path() == sim_a._get_simv_path()


def test_share_build_rebuilds_when_one_install_is_upgraded_in_place(
    tmp_path, monkeypatch, caplog
):
    """A new binary at the same path rebuilds in the same dir and logs why."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.048 2024-01-01")

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    assert sim_a.compile() == 0
    assert len(calls) == 1

    # Upgrade the install the project points at; the mtime bump avoids the version probe's (path, mtime) cache.
    _touch(exe, '#!/bin/sh\necho "Verilator 5.049 devel rev vBBBB"\n')

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(exe))
    with caplog.at_level(_logging.WARNING):
        assert sim_b.compile() == 0
    assert len(calls) == 2
    assert sim_b._get_simv_path() == sim_a._get_simv_path()  # rebuilt in place
    assert "5.048" in caplog.text and "5.049" in caplog.text
    assert "rebuilding rather than reusing it" in caplog.text


def test_a_wrapper_whose_size_and_mtime_survive_an_upgrade_still_rebuilds(
    tmp_path, monkeypatch
):
    """`bin/verilator` can stay byte-identical with the same mtime across a binary upgrade; the version banner detects it."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.048 aaaa")

    assert (
        _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe)).compile()
        == 0
    )
    assert len(calls) == 1
    before = os.stat(exe)

    # Same length, same mtime; only the banner moves.
    exe.write_text('#!/bin/sh\necho "Verilator 5.049 bbbb"\n')
    exe.chmod(0o755)
    os.utime(exe, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(exe).st_size == before.st_size
    assert os.stat(exe).st_mtime_ns == before.st_mtime_ns
    # The probe memoises on (path, mtime), so the cache is cleared as a fresh process would.
    vlog_sim_module._TOOLCHAIN_VERSION_CACHE.clear()

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(exe))
    assert sim_b.compile() == 0
    assert len(calls) == 2


def test_the_stamp_records_which_toolchain_built_it(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.049 devel rev vBBBB")

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    assert sim.compile() == 0

    toolchain = json.loads(_stamp_of(sim).read_text())["toolchain"]
    assert toolchain["exe"] == str(exe)
    assert toolchain["version"] == "Verilator 5.049 devel rev vBBBB"
    assert toolchain["size"] == exe.stat().st_size
    assert toolchain["mtime_ns"] == exe.stat().st_mtime_ns


def test_reusing_a_build_names_the_toolchain_that_produced_it(
    tmp_path, monkeypatch, caplog
):
    """`compile skipped` names the toolchain whose output will be simulated."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.049 devel rev vBBBB")

    assert (
        _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe)).compile()
        == 0
    )
    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(exe))
    with caplog.at_level(_logging.INFO):
        assert reader.compile() == 0

    assert len(calls) == 1
    assert "Verilator 5.049 devel rev vBBBB" in caplog.text


def test_an_unshareable_builder_also_rebuilds_when_the_toolchain_changes(
    tmp_path, monkeypatch
):
    """The unshared stamp uses the same fingerprint and follows the toolchain identity too."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, simv="simv")
    old = _fake_toolchain(tmp_path, "tc-a", "Some Simulator 1.0", binary="qrun")
    new = _fake_toolchain(tmp_path, "tc-b", "Some Simulator 2.0", binary="qrun")

    sim_a = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe=str(old),
        family="questa",
    )
    assert sim_a.compile() == 0
    assert len(calls) == 1
    # Warm: the same toolchain short-circuits on its own stamp.
    assert (
        _make_sim(
            tmp_path, monkeypatch, test_name="test_a", exe=str(old), family="questa"
        ).compile()
        == 0
    )
    assert len(calls) == 1

    sim_b = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe=str(new),
        family="questa",
    )
    assert sim_b.compile() == 0
    assert len(calls) == 2


def test_a_version_probe_that_fails_never_fails_the_compile(tmp_path, monkeypatch):
    """A simulator whose banner cannot be read is still built."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = tmp_path / "tc" / "verilator"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 3\n")
    exe.chmod(0o755)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    assert sim.compile() == 0
    assert json.loads(_stamp_of(sim).read_text())["toolchain"]["version"] is None


def test_a_builder_that_is_not_on_path_still_fingerprints(tmp_path, monkeypatch):
    """A `which` miss does not raise in the fingerprint; the compile reports it."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="verilator-does-not-exist"
    )
    assert sim.compile() == 0
    toolchain = json.loads(_stamp_of(sim).read_text())["toolchain"]
    assert toolchain == {
        "exe": "verilator-does-not-exist",
        "size": None,
        "mtime_ns": None,
        "version": None,
    }


def test_a_stamp_predating_the_toolchain_entry_does_not_warn(
    tmp_path, monkeypatch, caplog
):
    """An rtl_buddy upgrade rebuilds without reporting a toolchain change."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.049 devel rev vBBBB")

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    assert sim.compile() == 0
    stamp = _stamp_of(sim)
    stored = json.loads(stamp.read_text())
    del stored["toolchain"]
    stamp.write_text(json.dumps(stored, sort_keys=True))

    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(exe))
    with caplog.at_level(_logging.WARNING):
        assert reader.compile() == 0
    assert len(calls) == 2  # rebuilt, as it must be
    assert "rebuilding rather than reusing it" not in caplog.text


# --- the compile-key probe -------------------------------------
# The build job groups configs by the directory each compile writes; that key is derived in one place and obtainable without compiling.


def test_compile_group_dir_is_the_shared_build_dir_when_sharing_applies(
    tmp_path, monkeypatch
):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    group_dir = sim.compile_group_dir()

    # Probing compiles nothing...
    assert calls == []
    # ...and names the shared dir the compile then writes into.
    assert Path(group_dir).parent == tmp_path / "artefacts" / ".shared-builds"
    assert sim.compile() == 0
    assert Path(sim._get_simv_path()).parent == Path(group_dir)


def test_compile_group_dir_is_the_test_dir_when_sharing_is_unsupported(
    tmp_path, monkeypatch
):
    """An unshared build's output stays in its per-test workspace, and the group is the resolved output path."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qverilog", family="questa"
    )
    sim_b = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", exe="qverilog", family="questa"
    )
    assert sim_a.compile_group_dir().startswith(sim_a._get_compile_work_dir())
    assert sim_a.compile_group_dir() != sim_b.compile_group_dir()


def test_compile_group_dir_without_share_build_is_the_test_dir(tmp_path, monkeypatch):
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [])

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", share_build=False)
    assert sim.compile_group_dir().startswith(sim._get_compile_work_dir())


def test_probe_and_compile_derive_the_plan_once(tmp_path, monkeypatch):
    """Probe then compile derive the plan once; counting ``_write_filelist`` counts derivations."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    writes = []
    real_write = sim._write_filelist
    monkeypatch.setattr(
        sim, "_write_filelist", lambda path: (writes.append(path), real_write(path))[1]
    )

    group_dir = sim.compile_group_dir()
    assert len(writes) == 1
    assert sim.compile() == 0
    assert len(writes) == 1, "compile() re-derived the plan the probe already made"
    assert len(calls) == 1
    assert Path(sim._get_simv_path()).parent == Path(group_dir)


def test_a_second_compile_re_derives_the_plan(tmp_path, monkeypatch):
    """The cached plan serves one compile only, so a source edited between two compiles invalidates the stamp."""
    src = _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0
    assert len(calls) == 1

    src.write_text("module top; wire w; endmodule\n")
    os.utime(src, (0, 0))
    assert sim.compile() == 0
    assert len(calls) == 2, "the second compile reused a stale fingerprint"


def test_identical_inputs_group_together_and_plusdefines_split_them(
    tmp_path, monkeypatch
):
    _write_source(tmp_path)
    _install_fake_builder(monkeypatch, [])

    same_a = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    same_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    other = _make_sim(tmp_path, monkeypatch, test_name="test_c", pd={"WIDTH": 8})

    assert same_a.compile_group_dir() == same_b.compile_group_dir()
    assert other.compile_group_dir() != same_a.compile_group_dir()


# --- visible reuse and --rebuild ---------------------------------


def _compile_log_of(sim):
    return Path(sim._get_compile_transcript_path())


def _console_events(monkeypatch):
    """Collect the events sent through ``log_console_event``; ``caplog`` cannot distinguish that channel from ``log_event``."""
    seen = []
    real = vlog_sim_module.log_console_event

    def _spy(spy_logger, level, event, **fields):
        seen.append(event)
        return real(spy_logger, level, event, **fields)

    monkeypatch.setattr(vlog_sim_module, "log_console_event", _spy)
    return seen


def _logged_events(monkeypatch):
    """Collect the events sent through ``log_event``; ``caplog`` contents depend on which tests ran earlier in the process."""
    seen = []
    real = vlog_sim_module.log_event

    def _spy(spy_logger, level, event, **fields):
        seen.append(event)
        return real(spy_logger, level, event, **fields)

    monkeypatch.setattr(vlog_sim_module, "log_event", _spy)
    return seen


def test_a_reuse_says_so_on_the_console_with_the_age_of_what_it_reused(
    tmp_path, monkeypatch, caplog
):
    """A reuse names the directory and the age of its stamp, and is sent through ``log_console_event`` so it shows at WARNING console level."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.049 devel rev vBBBB")

    writer = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    assert writer.compile() == 0

    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(exe))
    console = _console_events(monkeypatch)
    with caplog.at_level(_logging.INFO):
        assert reader.compile() == 0
    assert len(calls) == 1
    # Through the console channel, not only the log file.
    assert console == ["compile.build_reused"]

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.build_reused"
    )
    shared_dir = Path(writer._get_simv_path()).parent
    # The basename is what a reader compares against `ls artefacts/.shared-builds/`; the absolute path rides alongside.
    assert record.rtl_fields["build_dir"] == shared_dir.name
    assert record.rtl_fields["build_path"] == str(shared_dir)
    assert record.rtl_fields["stamp_age_sec"] >= 0
    assert record.rtl_fields["toolchain"] == "Verilator 5.049 devel rev vBBBB"
    # The rendered line is checked as well as the fields.
    assert shared_dir.name in record.getMessage()
    assert "ago" in record.getMessage()


def test_a_reuse_leaves_a_compile_log_naming_what_it_reused(tmp_path, monkeypatch):
    """A skipped compile writes a transcript naming the reused directory, the stamp time and the command a rebuild would run."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    writer = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert writer.compile() == 0
    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert reader.compile() == 0
    assert len(calls) == 1

    text = _compile_log_of(reader).read_text()
    shared_dir = Path(writer._get_simv_path()).parent
    assert str(shared_dir) in text
    assert "Compile skipped" in text
    assert "Stamp written:" in text
    # The command that would have run, from the same assembly the real compile uses.
    assert f"--Mdir {shared_dir}" in text
    assert "--rebuild" in text


def test_a_reuse_over_a_non_utf8_transcript_degrades_instead_of_raising(
    tmp_path, monkeypatch
):
    """The breadcrumb of a reuse carries an old transcript with undecodable bytes replaced."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    writer = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert writer.compile() == 0
    reader = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    # A prior transcript with bytes no ambient encoding decodes.
    log = _compile_log_of(reader)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"Command: x\n\xff\xfe raw sim bytes\n")
    assert reader.compile() == 0

    text = _compile_log_of(reader).read_text()
    assert "Compile skipped" in text
    assert "raw sim bytes" in text  # carried, with bad bytes replaced


def test_a_compile_that_ran_leaves_a_transcript_even_when_it_passed(
    tmp_path, monkeypatch
):
    """A reuse that writes ``compile.log`` says so, so the file's presence does not mean "nothing compiled"."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, stdout="Parsing design\n")

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0
    text = _compile_log_of(sim).read_text()
    assert text.startswith("Command: ")
    assert "Parsing design" in text
    assert "Compile skipped" not in text


def test_a_transcript_that_cannot_be_written_does_not_fail_a_passing_compile(
    tmp_path, monkeypatch, caplog
):
    """A failure to write the compile transcript does not turn a successful build into a failed one."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")

    def _refuse(path, text):
        raise OSError("read-only file system")

    monkeypatch.setattr(type(sim), "_replace_text", staticmethod(_refuse))
    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 0
    assert len(calls) == 1
    assert [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.transcript_unwritable"
    ]


def test_a_reuse_keeps_the_compile_transcript_it_writes_over(tmp_path, monkeypatch):
    """The breadcrumb of a reuse carries earlier compile output, such as a VCS ``-licqueue`` wait, forward."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, stdout="Queuing for License...\n")

    def _vcs(test_name):
        return _make_sim(
            tmp_path, monkeypatch, test_name=test_name, exe="vcs", family="vcs"
        )

    builder = _vcs("test_a")
    assert builder.compile() == 0
    assert "Queuing for License" in _compile_log_of(builder).read_text()

    # Same test name, so the same compile.log: the gated element's shape.
    first_reuse = _vcs("test_a")
    assert first_reuse.compile() == 0
    assert len(calls) == 1
    text = _compile_log_of(first_reuse).read_text()
    assert text.startswith("Compile skipped")
    assert "Queuing for License" in text

    # A reuse over a reuse carries the transcript forward instead of nesting breadcrumbs.
    second_reuse = _vcs("test_a")
    assert second_reuse.compile() == 0
    text = _compile_log_of(second_reuse).read_text()
    assert text.count("Compile skipped") == 1
    assert "Queuing for License" in text


def test_an_unshareable_builders_reuse_also_leaves_a_breadcrumb(
    tmp_path, monkeypatch, caplog
):
    """The per-test-stamp reuse, which a dispatched fan-out takes for every element, is visible too."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    first = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert first.compile() == 0
    second = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    with caplog.at_level(_logging.INFO):
        assert second.compile() == 0
    assert len(calls) == 1

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.build_reused"
    )
    assert record.rtl_fields["shared"] is False
    assert "unshared build" in record.getMessage()
    # An unshared build's directory is `artefacts/<test>`, so only the path identifies it.
    build_dir = Path(second._get_compile_work_dir())
    assert record.rtl_fields["build_path"] == str(build_dir)
    assert record.rtl_fields["build_dir"] == build_dir.name
    assert str(build_dir) in record.getMessage()
    assert "Compile skipped" in _compile_log_of(second).read_text()


def test_rebuild_recompiles_over_a_warm_valid_stamp(tmp_path, monkeypatch):
    """``--rebuild`` forces a recompile even when ``--share-build`` is off."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert len(calls) == 1
    assert (
        _make_sim(tmp_path, monkeypatch, test_name="test_b", rebuild=True).compile()
        == 0
    )
    assert len(calls) == 2


def test_rebuild_forces_one_rebuild_per_build_dir_per_process(tmp_path, monkeypatch):
    """One ``--rebuild`` request rebuilds the shared directory once: the first test claims it and the rest reuse the stamp it wrote."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert len(calls) == 1

    first = _make_sim(tmp_path, monkeypatch, test_name="test_b", rebuild=True)
    second = _make_sim(tmp_path, monkeypatch, test_name="test_c", rebuild=True)
    assert first.compile() == 0
    assert second.compile() == 0
    assert len(calls) == 2, "the shared build was rebuilt once per test"


def test_rebuild_claims_the_directory_through_a_second_spelling_of_it(
    tmp_path, monkeypatch
):
    """The rebuild claim is keyed on the ``realpath``, so a symlinked suite dir that maps to the same ``obj_dir_<key>`` is one claim.
    The claim is read off ``compile.rebuild_forced``, which fires when a claim is granted.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    suite = tmp_path / "suite"
    suite.mkdir()
    link = tmp_path / "link"
    link.symlink_to(suite, target_is_directory=True)

    direct = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", suite_dir=suite, rebuild=True
    )
    through_link = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", suite_dir=link, rebuild=True
    )
    assert (
        Path(through_link.compile_group_dir()).resolve()
        == Path(direct.compile_group_dir()).resolve()
    )
    assert direct.compile_group_dir() != through_link.compile_group_dir()

    console = _console_events(monkeypatch)
    assert direct.compile() == 0
    assert through_link.compile() == 0
    assert console.count("compile.rebuild_forced") == 1, (
        "the same directory was claimed twice under two spellings"
    )


def test_a_repeated_compile_on_one_instance_does_not_re_rebuild(tmp_path, monkeypatch):
    """A second ``compile()`` on one instance validates the stamp, since the first call's claim stands."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_b", rebuild=True)
    assert sim.compile() == 0
    assert len(calls) == 2
    assert sim.compile() == 0
    assert len(calls) == 2


def test_rebuild_also_overrides_an_unshareable_builders_own_stamp(
    tmp_path, monkeypatch
):
    """``--rebuild`` also reaches the per-test stamp of families that cannot share."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert (
        _make_sim(
            tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
        ).compile()
        == 0
    )
    assert len(calls) == 1
    assert (
        _make_sim(
            tmp_path,
            monkeypatch,
            test_name="test_a",
            exe="qrun",
            family="questa",
            rebuild=True,
        ).compile()
        == 0
    )
    assert len(calls) == 2


def test_without_rebuild_a_warm_stamp_is_still_reused(tmp_path, monkeypatch):
    """``--rebuild`` is off by default and changes nothing then."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert _make_sim(tmp_path, monkeypatch, test_name="test_b").compile() == 0
    assert len(calls) == 1


def test_a_forced_rebuild_says_which_directory_it_is_recompiling(
    tmp_path, monkeypatch, caplog
):
    """``--rebuild`` emits its own event saying the build was recompiled."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    writer = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert writer.compile() == 0
    shared_dir = Path(writer._get_simv_path()).parent

    forced = _make_sim(tmp_path, monkeypatch, test_name="test_b", rebuild=True)
    console = _console_events(monkeypatch)
    with caplog.at_level(_logging.INFO):
        assert forced.compile() == 0
    assert len(calls) == 2

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.rebuild_forced"
    )
    # The same field schema as `compile.build_reused`: basename in `build_dir`, absolute in `build_path`.
    assert record.rtl_fields["build_dir"] == shared_dir.name
    assert record.rtl_fields["build_path"] == str(shared_dir)
    assert "--rebuild given" in record.getMessage()
    assert shared_dir.name in record.getMessage()
    # Also on the console, like the reuse line.
    assert console == ["compile.rebuild_forced"]


# --- cross-process build lock -------------------------------------
# A flock around the stamp check keeps concurrent processes from compiling into one fresh shared directory. These drive VlogSim directly; the cross-process half is in tests/test_artifact_lock.py.


def _lock_events(monkeypatch):
    """Collect and forward the build lock's console events."""
    seen = []
    real = artifact_lock_module.log_console_event

    def _spy(spy_logger, level, event, **fields):
        seen.append((event, fields))
        return real(spy_logger, level, event, **fields)

    monkeypatch.setattr(artifact_lock_module, "log_console_event", _spy)
    return seen


@pytest.mark.parametrize("cached", [False, True], ids=["in-tree", "cache-root"])
def test_a_compile_blocked_on_the_build_lock_reuses_what_it_waited_for(
    tmp_path, monkeypatch, cached
):
    """Double-checked locking: a compile waiting on the lock validates the stamp the first compile wrote and reuses it, so the builder runs once.
    Threads stand in for processes because the fake builder lives in this process; flock treats separate ``open()`` calls as separate holders.
    Events are read off the console spies because the first ``log_console_event`` of a pytest process detaches pytest's capture handler.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    compiling = threading.Event()
    finish = threading.Event()
    waiting = threading.Event()
    inner_builder = vlog_sim_module.run_managed_process

    def _held_open(*args, **kwargs):
        compiling.set()
        assert finish.wait(60), "the waiter never reached the lock"
        return inner_builder(*args, **kwargs)

    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _held_open)
    lock_events = _lock_events(monkeypatch)
    forwarding_spy = artifact_lock_module.log_console_event

    def _note_wait(spy_logger, level, event, **fields):
        if event == "compile.build_lock_wait":
            waiting.set()
        return forwarding_spy(spy_logger, level, event, **fields)

    monkeypatch.setattr(artifact_lock_module, "log_console_event", _note_wait)
    compile_events = _console_events(monkeypatch)

    # Parametrised over the cache root because in cache mode the lock lives in a directory shared with other checkouts, where the two-writer case is normal.
    cache_root = tmp_path / "cache" if cached else None
    first = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", shared_build_root=cache_root
    )
    second = _make_sim(
        tmp_path, monkeypatch, test_name="test_b", shared_build_root=cache_root
    )
    results = {}

    def _compile(key, sim):
        results[key] = sim.compile()

    builder = threading.Thread(target=_compile, args=("first", first))
    builder.start()
    assert compiling.wait(60)
    waiter = threading.Thread(target=_compile, args=("second", second))
    waiter.start()
    # The wait line is emitted immediately before the blocking flock, so no sleep is needed.
    assert waiting.wait(60), "the second compile did not queue on the lock"
    finish.set()
    builder.join(60)
    waiter.join(60)

    assert results == {"first": 0, "second": 0}
    assert len(calls) == 1, "the waiter recompiled instead of reusing"
    assert [event for event, _ in lock_events] == ["compile.build_lock_wait"]
    # Only the waiter reports a reuse.
    assert compile_events == ["compile.build_reused"]

    _, fields = lock_events[0]
    shared_dir = Path(first._get_simv_path()).parent
    assert (shared_dir.parent.parent == cache_root) is cached
    # The same directory-field schema as the other compile.* build events.
    assert {key: fields[key] for key in ("build_dir", "build_path")} == (
        vlog_sim_module._build_dir_fields(shared_dir, shared=True)
    )
    assert fields["test"] == "test_b"
    assert fields["holder_pid"] == os.getpid()
    assert fields["holder_test"] == "test_a"


def test_a_warm_shared_build_is_reused_without_taking_the_lock(tmp_path, monkeypatch):
    """The reuse fast path never takes the lock."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0

    locked = []
    real_lock = vlog_sim_module.build_dir_lock

    def _spy(build_dir, **kwargs):
        locked.append(str(build_dir))
        return real_lock(build_dir, **kwargs)

    monkeypatch.setattr(vlog_sim_module, "build_dir_lock", _spy)
    events = _console_events(monkeypatch)

    second = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert second.compile() == 0
    assert len(calls) == 1, "the warm build was recompiled"
    assert events == ["compile.build_reused"]
    assert locked == [], "a reuse took the build lock"


@pytest.mark.skipif(os.name != "posix", reason="flock(2) is POSIX")
def test_a_reuse_does_not_wait_for_a_process_holding_the_lock(tmp_path, monkeypatch):
    """The same claim against a lock held from a separate file description; a reuse that took the lock would block until the timeout."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    first = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert first.compile() == 0
    shared_dir = Path(first._get_simv_path()).parent

    fd = os.open(
        shared_dir / artifact_lock_module.BUILD_LOCK_FILENAME,
        os.O_RDWR | os.O_CREAT,
        0o644,
    )
    result = {}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second = _make_sim(tmp_path, monkeypatch, test_name="test_b")
        reuse = threading.Thread(target=lambda: result.update(rc=second.compile()))
        reuse.start()
        reuse.join(60)
        assert not reuse.is_alive(), "the reuse queued behind the lock holder"
    finally:
        os.close(fd)
    assert result == {"rc": 0}
    assert len(calls) == 1


def test_a_forced_rebuild_still_takes_the_lock(tmp_path, monkeypatch):
    """``--rebuild`` skips the fast path but still takes the lock."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0

    locked = []
    real_lock = vlog_sim_module.build_dir_lock

    def _spy(build_dir, **kwargs):
        locked.append(str(build_dir))
        return real_lock(build_dir, **kwargs)

    monkeypatch.setattr(vlog_sim_module, "build_dir_lock", _spy)
    second = _make_sim(tmp_path, monkeypatch, test_name="test_b", rebuild=True)
    assert second.compile() == 0
    assert len(calls) == 2, "--rebuild reused the warm build"
    assert locked == [str(Path(second._get_simv_path()).parent)]


def test_a_stale_stamp_explains_itself_once_across_both_checks(tmp_path, monkeypatch):
    """The pre-check is advisory; ``compile.build_toolchain_changed`` is reported once, from the in-lock check."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.048 2024-01-01")
    assert (
        _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe)).compile()
        == 0
    )
    _touch(exe, '#!/bin/sh\necho "Verilator 5.049 devel rev vBBBB"\n')

    events = _logged_events(monkeypatch)
    assert (
        _make_sim(tmp_path, monkeypatch, test_name="test_b", exe=str(exe)).compile()
        == 0
    )
    assert len(calls) == 2, "the upgraded toolchain reused the old build"
    changed = [e for e in events if e == "compile.build_toolchain_changed"]
    assert len(changed) == 1, f"the stale-stamp diagnostics fired {len(changed)} times"


def test_a_build_lock_the_filesystem_refuses_still_compiles(
    tmp_path, monkeypatch, caplog
):
    """A lock that cannot be taken (ENOLCK, EROFS) costs the serialisation only and does not fail the build."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    def _no_locks(fd, operation):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(artifact_lock_module.fcntl, "flock", _no_locks)

    with caplog.at_level(_logging.WARNING):
        assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert len(calls) == 1

    warnings = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.build_lock_unavailable"
    ]
    assert len(warnings) == 1
    assert "not serialised" in warnings[0].getMessage()


def test_an_unshared_build_takes_no_build_lock(tmp_path, monkeypatch):
    """Per-test build directories take no lock."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", share_build=False)
    assert sim.compile() == 0
    work_dir = Path(sim._get_compile_work_dir())
    assert list(work_dir.rglob(artifact_lock_module.BUILD_LOCK_FILENAME)) == []


def test_the_lock_lives_in_the_shared_build_directory_it_guards(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0
    shared_dir = Path(sim._get_simv_path()).parent
    assert (shared_dir / artifact_lock_module.BUILD_LOCK_FILENAME).is_file()


def test_a_wait_line_stands_up_when_the_holder_is_unknown():
    """Missing or stale holder metadata still yields a sentence."""
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "compile.build_lock_wait",
        {"build_dir": "obj_dir_abc", "build_path": "/w/obj_dir_abc"},
    )
    assert "another rtl-buddy process to finish compiling obj_dir_abc" in message
    assert "so far" not in message


def test_a_repeated_wait_line_says_how_long_it_has_been():
    """A repeated wait line differs from the first."""
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "compile.build_lock_wait",
        {"build_dir": "obj_dir_abc", "build_path": "/w/obj_dir_abc", "waited_sec": 600},
    )
    assert "(600s so far)" in message


def test_a_reuse_line_reports_the_stamp_age_before_the_toolchain():
    """The reuse line leads with the stamp age, written as a wall-clock duration."""
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "compile.build_reused",
        {
            "test": "test_a",
            "build_dir": "obj_dir_abc",
            "build_path": "/w/obj_dir_abc",
            "stamp_age_sec": 3723,
            "toolchain": "Verilator 5.049 devel rev vBBBB",
        },
    )
    assert message == (
        "test_a: reused shared build obj_dir_abc "
        "(built 1h02m03s ago, Verilator 5.049 devel rev vBBBB); nothing compiled"
    )


def test_an_unknown_stamp_age_says_so_rather_than_going_quiet():
    """A stamp that vanishes before stat still produces a reuse line, stating that the age is unknown."""
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "compile.build_reused",
        {
            "test": "test_a",
            "build_dir": "obj_dir_abc",
            "build_path": "/w/obj_dir_abc",
            "stamp_age_sec": None,
            "toolchain": None,
        },
    )
    assert message == (
        "test_a: reused shared build obj_dir_abc (age unknown); nothing compiled"
    )


# Persistent shared-build cache root


def _write_checkout(root, *, rtl="module top; endmodule\n"):
    """One checkout of a project: a suite under ``verif/`` over RTL above it."""
    suite = root / "verif" / "blk"
    suite.mkdir(parents=True, exist_ok=True)
    src = root / "rtl" / "a.sv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(rtl)
    return suite


def _cache_sim(checkout, monkeypatch, *, cache_root, test_name, compile_opts=None):
    suite = checkout / "verif" / "blk"
    return _make_sim(
        checkout,
        monkeypatch,
        test_name=test_name,
        suite_dir=suite,
        project_root=checkout,
        model_path=suite / "models.yaml",
        filelist=["../../rtl/a.sv"],
        shared_build_root=cache_root,
        compile_opts=compile_opts,
    )


_ROOT_WAIVER_OPTS = ["--binary", "${RTL_BUDDY_PROJECT_ROOT}/rtl/waive.vlt"]


@pytest.mark.parametrize("run_tag", [None, "demo"])
def test_a_project_root_path_in_compile_opts_names_one_file_under_any_tag(
    tmp_path, monkeypatch, run_tag
):
    """`${RTL_BUDDY_PROJECT_ROOT}` survives the deeper cwd `--run-tag` gives."""
    _write_checkout(tmp_path)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="t",
        suite_dir=tmp_path / "verif" / "blk",
        project_root=tmp_path,
        model_path=tmp_path / "verif" / "blk" / "models.yaml",
        filelist=["../../rtl/a.sv"],
        compile_opts=_ROOT_WAIVER_OPTS,
        run_tag=run_tag,
    )
    plan = sim._build_compile_plan()
    waiver = str(tmp_path.resolve() / "rtl" / "waive.vlt")
    assert waiver in plan.builder_opts
    assert waiver in sim._compile_argv(plan, quiet=True)


def test_a_project_root_path_in_compile_opts_keeps_the_cache_key_portable(
    tmp_path, monkeypatch
):
    """An absolute expansion of the token is still relativised in the key."""
    cache = tmp_path / "cache"
    keys = []
    for name in ("wt-a", "wt-b"):
        checkout = tmp_path / name
        _write_checkout(checkout)
        (checkout / "rtl" / "waive.vlt").write_text("`verilator_config\n")
        sim = _cache_sim(
            checkout,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=_ROOT_WAIVER_OPTS,
        )
        keys.append(sim._build_compile_plan().fingerprint["cmd"])
    assert "rtl/waive.vlt" in keys[0]
    assert keys[0] == keys[1]


def test_the_cache_namespace_is_the_suite_relative_to_the_project_root(tmp_path):
    """The cache namespace is the suite's place in the project, so two checkouts of one project share it."""
    suite = tmp_path / "verif" / "demo_tiny_alu"
    suite.mkdir(parents=True)
    assert (
        vlog_sim_module.shared_build_namespace(suite, tmp_path)
        == "verif__demo_tiny_alu"
    )
    # A suite that is the project root has no relative components.
    assert vlog_sim_module.shared_build_namespace(tmp_path, tmp_path) == "_root"
    # Outside the root, a digest of the absolute path keeps the namespace unique.
    outside = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    outside.mkdir()
    namespace = vlog_sim_module.shared_build_namespace(outside, tmp_path)
    assert len(namespace) == 12 and all(ch in "0123456789abcdef" for ch in namespace)


def test_shared_build_dir_helper_cache_layout(tmp_path):
    """The cache path is ``<root>/<suite-namespace>/obj_dir_<key>``, and the in-tree default is unchanged."""
    suite = tmp_path / "verif" / "blk"
    suite.mkdir(parents=True)
    assert shared_build_dir(
        suite, "cafe0123", cache_root="/nfs/cache", project_root=tmp_path
    ) == Path("/nfs/cache/verif__blk/obj_dir_cafe0123")
    assert shared_build_dir(suite, "cafe0123", project_root=tmp_path) == (
        suite / "artefacts" / ".shared-builds" / "obj_dir_cafe0123"
    )


def test_two_checkouts_of_identical_content_share_one_cache_dir(tmp_path, monkeypatch):
    """Two checkouts with identical content and one cache root share a build; in-tree keys differ."""
    cache = tmp_path / "cache"
    first = _write_checkout(tmp_path / "wt-a")
    second = _write_checkout(tmp_path / "wt-b")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _cache_sim(
        first.parent.parent, monkeypatch, cache_root=cache, test_name="t"
    )
    assert sim_a.compile() == 0
    assert len(calls) == 1
    shared = Path(sim_a._get_simv_path()).parent
    assert shared.parent == cache / "verif__blk"

    sim_b = _cache_sim(
        second.parent.parent, monkeypatch, cache_root=cache, test_name="t"
    )
    assert sim_b._compile_plan().shared_dir == shared, (
        "the key is still a function of the checkout path"
    )
    assert sim_b.compile() == 0
    assert len(calls) == 1, "the second checkout recompiled instead of reusing"
    assert sim_b.last_compile["reused"] is True

    # Without a root, the two checkouts get two keys.
    plain_a = _make_sim(
        first.parent.parent,
        monkeypatch,
        test_name="t",
        suite_dir=first,
        project_root=first.parent.parent,
        model_path=first / "models.yaml",
        filelist=["../../rtl/a.sv"],
    )
    plain_b = _make_sim(
        second.parent.parent,
        monkeypatch,
        test_name="t",
        suite_dir=second,
        project_root=second.parent.parent,
        model_path=second / "models.yaml",
        filelist=["../../rtl/a.sv"],
    )
    assert (
        plain_a._compile_plan().shared_dir.name
        != plain_b._compile_plan().shared_dir.name
    )


def test_a_cache_mode_key_separates_two_checkouts_on_different_content(
    tmp_path, monkeypatch
):
    """The cache is content-addressed: different content gets a different directory and does not clobber the first build."""
    cache = tmp_path / "cache"
    first = _write_checkout(tmp_path / "wt-a")
    _write_checkout(tmp_path / "wt-b", rtl="module top; /* patched */ endmodule\n")
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim_a = _cache_sim(
        first.parent.parent, monkeypatch, cache_root=cache, test_name="t"
    )
    assert sim_a.compile() == 0
    dir_a = Path(sim_a._get_simv_path()).parent

    sim_b = _cache_sim(tmp_path / "wt-b", monkeypatch, cache_root=cache, test_name="t")
    dir_b = Path(sim_b._compile_plan().shared_dir)
    assert dir_b != dir_a
    assert sim_b.compile() == 0
    assert len(calls) == 2
    assert (dir_a / "simv").is_file(), "the edit clobbered the other checkout's build"
    assert sorted(path.name for path in (cache / "verif__blk").iterdir()) == sorted(
        [dir_a.name, dir_b.name]
    )


def test_a_stamp_written_by_one_checkout_validates_from_another(tmp_path, monkeypatch):
    """In cache mode the stamp's tracked inputs are relative to the project root and re-anchored against the reader's root."""
    cache = tmp_path / "cache"
    first = _write_checkout(tmp_path / "wt-a")
    _write_checkout(tmp_path / "wt-b")
    calls = []
    # A dependency file exercises the deps half of the stamp, which is re-stat'ed.
    _install_fake_builder(monkeypatch, calls, depends=["../../../../rtl/a.sv"])

    sim_a = _cache_sim(
        first.parent.parent, monkeypatch, cache_root=cache, test_name="t"
    )
    assert sim_a.compile() == 0
    stored = json.loads(_stamp_of(sim_a).read_text())
    assert stored["root"] == os.path.realpath(tmp_path / "wt-a")
    assert [entry[0] for entry in stored["sources"]] == ["rtl/a.sv"]
    assert [entry[0] for entry in stored["deps"]] == ["rtl/a.sv"], (
        "a dependency recorded absolute pins the stamp to one checkout"
    )
    # The executable lives in the cache outside either project root, so its absolute spelling is shared.
    assert stored["simv"][0] == str(Path(sim_a._get_simv_path()))

    sim_b = _cache_sim(tmp_path / "wt-b", monkeypatch, cache_root=cache, test_name="t")
    plan = sim_b._compile_plan()
    assert sim_b._build_stamp_is_valid(
        plan.shared_dir, sim_b._get_simv_path(), plan.fingerprint
    ), sim_b.stamp_mismatch_reason
    assert sim_b.compile() == 0
    assert len(calls) == 1

    # An edit in the second checkout is seen through its own root.
    _touch(tmp_path / "wt-b" / "rtl" / "a.sv", "module top; /* edited */ endmodule\n")
    sim_c = _cache_sim(tmp_path / "wt-b", monkeypatch, cache_root=cache, test_name="u")
    assert sim_c.compile() == 0
    assert len(calls) == 2


def test_an_absolute_in_root_compile_flag_is_relativised_for_the_key(
    tmp_path, monkeypatch
):
    """Absolute ``+incdir+``, ``--Mdir`` and bare source arguments inside the project root are relativised in the command."""
    cache = tmp_path / "cache"
    first = _write_checkout(tmp_path / "wt-a")
    _write_checkout(tmp_path / "wt-b")
    (first.parent.parent / "inc").mkdir()
    (tmp_path / "wt-b" / "inc").mkdir()

    def _sim(checkout):
        return _cache_sim(
            checkout,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[f"+incdir+{checkout / 'inc'}"],
        )

    sim_a, sim_b = _sim(tmp_path / "wt-a"), _sim(tmp_path / "wt-b")
    assert "+incdir+inc" in sim_a._compile_plan().fingerprint["cmd"]
    assert not any(
        str(tmp_path / "wt-a") in token
        for token in sim_a._compile_plan().fingerprint["cmd"]
    )
    assert sim_a._compile_plan().shared_dir == sim_b._compile_plan().shared_dir


def test_the_default_mode_keeps_absolute_spellings_everywhere(tmp_path, monkeypatch):
    """With no cache root, the stamp uses absolute paths and has no ``root``."""
    suite = _write_checkout(tmp_path / "wt-a")
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../../../rtl/a.sv"])
    sim = _make_sim(
        tmp_path / "wt-a",
        monkeypatch,
        test_name="t",
        suite_dir=suite,
        project_root=tmp_path / "wt-a",
        model_path=suite / "models.yaml",
        filelist=["../../rtl/a.sv"],
    )
    assert sim.compile() == 0
    stored = json.loads(_stamp_of(sim).read_text())
    assert "root" not in stored
    assert all(os.path.isabs(entry[0]) for entry in stored["sources"])
    assert all(os.path.isabs(entry[0]) for entry in stored["deps"])
    assert os.path.isabs(stored["simv"][0])


def test_the_cache_root_is_created_on_demand(tmp_path, monkeypatch):
    """``mkdir -p``: the first run against a fresh NFS path needs no existing directory."""
    cache = tmp_path / "does" / "not" / "exist" / "yet"
    suite = _write_checkout(tmp_path / "wt-a")
    calls = []
    _install_fake_builder(monkeypatch, calls)
    sim = _cache_sim(tmp_path / "wt-a", monkeypatch, cache_root=cache, test_name="t")
    assert sim.compile() == 0
    assert (cache / "verif__blk").is_dir()
    assert suite.is_dir()


def test_a_relative_cache_root_anchors_to_the_project_root(tmp_path, monkeypatch):
    """A relative cache root is resolved against the project root, not the cwd."""
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    sim = _cache_sim(checkout, monkeypatch, cache_root=".rb-cache", test_name="t")
    assert sim.shared_build_root == str(Path(os.path.realpath(checkout)) / ".rb-cache")


def test_resolve_shared_build_root_precedence_and_expansion(tmp_path, monkeypatch):
    """The cache root comes from the CLI, then the environment, then config, and a blank value turns it off."""
    resolve = vlog_sim_module.resolve_shared_build_root
    assert resolve(None, tmp_path) is None
    assert resolve("  ", tmp_path) is None
    assert resolve("/abs/cache", tmp_path) == "/abs/cache"
    monkeypatch.setenv("RB_CACHE_HOME", str(tmp_path / "env"))
    assert resolve("$RB_CACHE_HOME/c", tmp_path) == str(tmp_path / "env" / "c")
    assert resolve("~", tmp_path) == os.path.expanduser("~")


def test_the_cli_resolves_the_cache_root_cli_over_env_over_config(monkeypatch):
    """The three cache-root sources apply in the documented order."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    class _Root:
        def get_project_rootdir(self):
            return "/proj"

        def get_shared_build_root(self):
            return "from-config"

    app = RtlBuddy(name="test_shared_build_root")
    app.root_cfg = _Root()
    monkeypatch.delenv("RTL_BUDDY_SHARED_BUILD_ROOT", raising=False)
    assert app.shared_build_root == "/proj/from-config"
    monkeypatch.setenv("RTL_BUDDY_SHARED_BUILD_ROOT", "/from/env")
    assert app.shared_build_root == "/from/env"
    app._shared_build_root_flag = "/from/cli"
    assert app.shared_build_root == "/from/cli"
    # An explicit empty value is an override: the cache goes off for this run.
    app._shared_build_root_flag = ""
    assert app.shared_build_root is None
    # No root config: nothing to anchor to and nothing configured.
    app.root_cfg = None
    assert app.shared_build_root is None


def test_share_build_off_ignores_a_configured_cache_root(tmp_path, monkeypatch):
    """Cache mode applies to the shared build only; per-test stamps are unchanged without sharing."""
    _write_checkout(tmp_path / "wt-a")
    suite = tmp_path / "wt-a" / "verif" / "blk"
    sim = _make_sim(
        tmp_path / "wt-a",
        monkeypatch,
        test_name="t",
        share_build=False,
        suite_dir=suite,
        project_root=tmp_path / "wt-a",
        model_path=suite / "models.yaml",
        filelist=["../../rtl/a.sv"],
        shared_build_root=tmp_path / "cache",
    )
    assert sim.shared_build_root is None


def test_switching_the_cache_root_on_or_off_rebuilds_once_and_says_why(
    tmp_path, monkeypatch
):
    """A stamp written in the other mode is read as unknown and rebuilt once, with a reason that names the mode."""
    _write_source(tmp_path)
    calls = []
    pinned = tmp_path / "pinned" / "simv"
    _install_fake_builder(monkeypatch, calls, simv=str(pinned))

    def _sim(cache_root):
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name="test_a",
            exe="vcs",
            family="vcs",
            simv=str(pinned),
            shared_build_root=cache_root,
        )

    cached = _sim(tmp_path / "cache")
    assert cached.compile() == 0
    assert len(calls) == 1
    plain = _sim(None)
    plan = plain._compile_plan()
    assert not plain._build_stamp_is_valid(
        plan.compile_work_dir, plain._get_simv_path(), plan.fingerprint
    )
    assert plain.stamp_mismatch_reason == (
        "the stamp was written in the other shared-build mode"
    )
    assert plain.compile() == 0
    assert len(calls) == 2
    # The rebuild's own stamp validates from then on.
    assert _sim(None).compile() == 0
    assert len(calls) == 2


def _write_cmd_incdir(checkout, content="`define CMD_W 8\n"):
    """A header reachable only through a compile-line `+incdir+` from `builder-opts.compile-time`."""
    header = checkout / "hdr" / "cmd.svh"
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text(content)
    return header


def test_a_compile_line_incdir_is_content_addressed_in_cache_mode(
    tmp_path, monkeypatch
):
    """The cache-mode key moves when the content of a compile-line input moves, so two checkouts with differing headers under a `builder-opts` `+incdir+` get different `obj_dir`s."""
    cache = tmp_path / "cache"
    for name in ("wt-a", "wt-b"):
        _write_checkout(tmp_path / name)
    _write_cmd_incdir(tmp_path / "wt-a")
    _write_cmd_incdir(tmp_path / "wt-b", content="`define CMD_W 16\n")

    def _sim(checkout):
        return _cache_sim(
            tmp_path / checkout,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[f"+incdir+{tmp_path / checkout / 'hdr'}"],
        )

    differing = _sim("wt-a")._compile_plan().shared_dir
    assert differing != _sim("wt-b")._compile_plan().shared_dir, (
        "two checkouts with different header content share one obj_dir"
    )

    # Identical content still shares.
    _write_cmd_incdir(tmp_path / "wt-b")
    _as_a_fresh_process()
    assert _sim("wt-a")._compile_plan().shared_dir == (
        _sim("wt-b")._compile_plan().shared_dir
    )


def test_a_compile_line_source_and_library_dir_are_content_addressed(
    tmp_path, monkeypatch
):
    """The same holds for a bare source argument and a `-y` library directory."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    extra = checkout / "extra.sv"
    extra.write_text("module extra; endmodule\n")
    library = checkout / "lib"
    library.mkdir()
    (library / "cell.sv").write_text("module cell; endmodule\n")

    def _key():
        _as_a_fresh_process()
        return (
            _cache_sim(
                checkout,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=[str(extra), "-y", str(library)],
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    _touch(extra, "module extra; /* edited */ endmodule\n")
    after_source = _key()
    assert after_source != before
    _touch(library / "cell.sv", "module cell; /* edited */ endmodule\n")
    assert _key() != after_source
    # A file appearing in a `-y` directory can change module resolution, so the listing decides it.
    (library / "late.sv").write_text("module late; endmodule\n")
    assert _key() != after_source


def test_a_compile_line_path_outside_the_project_root_stays_text_only(
    tmp_path, monkeypatch
):
    """A path outside the root keeps its absolute text in the key."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    outside = tmp_path / "vendor-ip"
    outside.mkdir()
    (outside / "vendor.svh").write_text("`define V 1\n")

    def _key():
        _as_a_fresh_process()
        return (
            _cache_sim(
                checkout,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=[f"+incdir+{outside}"],
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    _touch(outside / "vendor.svh", "`define V 2\n")
    assert _key() == before


def test_an_output_path_on_the_compile_line_never_reaches_the_key(
    tmp_path, monkeypatch
):
    """`-o <abs path under the root>` is an unrecognised flag's argument and is skipped, so the build's own output is not hashed into the key."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    output = checkout / "out" / "simv"
    output.parent.mkdir()

    def _key():
        _as_a_fresh_process()
        return (
            _cache_sim(
                checkout,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=["-o", str(output)],
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    output.write_text("a binary\n")
    assert _key() == before
    _touch(output, "a different binary\n")
    assert _key() == before


def test_a_bare_source_after_a_boolean_flag_still_reaches_the_key(
    tmp_path, monkeypatch
):
    """`--binary /proj/tb.sv` names a source; only a known output option's argument is refused."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    extra = checkout / "extra.sv"
    extra.write_text("module extra; endmodule\n")

    def _key():
        _as_a_fresh_process()
        return (
            _cache_sim(
                checkout,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=["--binary", str(extra)],
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    _touch(extra, "module extra; /* edited */ endmodule\n")
    assert _key() != before


def test_a_path_inside_an_artefact_tree_never_reaches_the_key(tmp_path, monkeypatch):
    """Anything under `artefacts/`, `.shared-builds/` or `obj_dir*` is build output and is never keyed, even after an unrecognised option."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    produced = checkout / "verif" / "blk" / "artefacts" / "t" / "obj_dir_x" / "out.sv"
    produced.parent.mkdir(parents=True)
    produced.write_text("module out; endmodule\n")

    def _key():
        _as_a_fresh_process()
        return (
            _cache_sim(
                checkout,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=["--binary", str(produced)],
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    _touch(produced, "module out; /* rebuilt */ endmodule\n")
    assert _key() == before


def test_a_run_f_incdir_is_not_walked_twice_for_the_key(tmp_path, monkeypatch):
    """A directory that `run.f` already names under the same spelling is not walked again."""
    checkout = tmp_path / "wt-a"
    suite = _write_checkout(checkout)
    (checkout / "inc").mkdir()
    (checkout / "inc" / "w.svh").write_text("`define W 8\n")
    sim = _make_sim(
        checkout,
        monkeypatch,
        test_name="t",
        suite_dir=suite,
        project_root=checkout,
        model_path=suite / "models.yaml",
        filelist=["+incdir+../../inc", "../../rtl/a.sv"],
        shared_build_root=tmp_path / "cache",
        compile_opts=[f"+incdir+{checkout / 'inc'}"],
    )
    plan = sim._compile_plan()
    assert "+incdir+inc" in [entry[0] for entry in plan.fingerprint["sources"]]
    assert sim._fingerprint_cmd_inputs(plan.key_cmd, plan.fingerprint["sources"]) == []


def test_the_default_mode_key_ignores_compile_line_content(tmp_path, monkeypatch):
    """Without a cache root the in-tree key is unchanged, and an edit under a compile-line `+incdir+` rebuilds in place."""
    checkout = tmp_path / "wt-a"
    suite = _write_checkout(checkout)
    header = _write_cmd_incdir(checkout)

    def _key():
        _as_a_fresh_process()
        return (
            _make_sim(
                checkout,
                monkeypatch,
                test_name="t",
                suite_dir=suite,
                project_root=checkout,
                model_path=suite / "models.yaml",
                filelist=["../../rtl/a.sv"],
                compile_opts=[f"+incdir+{checkout / 'hdr'}"],
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    _touch(header, "`define CMD_W 16\n")
    assert _key() == before
    # The key function never reads the new field without a root.
    fingerprint = {
        "cmd": ["verilator"],
        "env": {},
        "sources": [["src/top.sv", 31, 17, "0123456789abcdef"]],
        "toolchain": {"exe": "/opt/verilator/bin/verilator"},
    }
    assert vlog_sim_module.VlogSim._compile_config_key(fingerprint) == (
        vlog_sim_module.VlogSim._compile_config_key(
            fingerprint, cmd_inputs=[["+incdir+hdr", [["cmd.svh", "beef"]]]]
        )
    )


def _write_nested_filelists(checkout, *, source="module nested; endmodule\n"):
    """A compile-line `-F` list that names a further list, which names RTL. The list bytes are identical in every checkout.
    Every relative entry anchors to the list that declared it; the `-f` rule is tested below.
    """
    (checkout / "rtl" / "nested.sv").write_text(source)
    lists = checkout / "lists"
    lists.mkdir(exist_ok=True)
    (lists / "inner.f").write_text("// inner\n../rtl/nested.sv\n")
    (lists / "top.f").write_text("// top\n-F inner.f\n")
    return lists / "top.f"


def test_a_nested_compile_line_filelist_is_expanded_into_the_key(tmp_path, monkeypatch):
    """A filelist is keyed by what it names, not by its own bytes; identical nested lists over different RTL get different keys."""
    cache = tmp_path / "cache"
    for name in ("wt-a", "wt-b"):
        _write_checkout(tmp_path / name)
    top_a = _write_nested_filelists(tmp_path / "wt-a")
    _write_nested_filelists(
        tmp_path / "wt-b", source="module nested; /* patched */ endmodule\n"
    )

    def _key(name):
        _as_a_fresh_process()
        return (
            _cache_sim(
                tmp_path / name,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=["-F", str(tmp_path / name / "lists" / "top.f")],
            )
            ._compile_plan()
            .shared_dir.name
        )

    assert _key("wt-a") != _key("wt-b"), (
        "two checkouts whose nested filelists reach different RTL share one dir"
    )
    # Identical RTL behind identical lists still shares.
    _write_nested_filelists(tmp_path / "wt-b")
    assert _key("wt-a") == _key("wt-b")
    # The whole chain is keyed: both lists and the source they reach.
    sim = _cache_sim(
        tmp_path / "wt-a",
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=["-F", str(top_a)],
    )
    plan = sim._compile_plan()
    spellings = [
        entry[0]
        for entry in sim._fingerprint_cmd_inputs(
            plan.key_cmd, plan.fingerprint["sources"]
        )
    ]
    assert spellings == [
        "-F lists/top.f",
        "-F lists/inner.f",
        "rtl/nested.sv",
    ], spellings


def test_a_nested_filelist_incdir_and_a_cycle_are_both_handled(tmp_path, monkeypatch):
    """A nested list may name its own `+incdir+`, and a list that includes itself is visited once."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    (checkout / "hdr").mkdir()
    (checkout / "hdr" / "w.svh").write_text("`define W 8\n")
    lists = checkout / "lists"
    lists.mkdir()
    (lists / "top.f").write_text("+incdir+../hdr\n-F top.f\n")
    # `-F`, so `+incdir+../hdr` anchors to `lists/`.

    def _sim():
        _as_a_fresh_process()
        return _cache_sim(
            checkout,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=["-F", str(lists / "top.f")],
        )

    sim = _sim()
    plan = sim._compile_plan()
    entries = sim._fingerprint_cmd_inputs(plan.key_cmd, plan.fingerprint["sources"])
    # The self-include contributes once, under the spelling it was first reached by.
    assert [entry[0] for entry in entries] == ["-F lists/top.f", "+incdir+hdr"]
    # The include directory is keyed by its listing, so a header inside it moves the key.
    before = plan.shared_dir.name
    _touch(checkout / "hdr" / "w.svh", "`define W 16\n")
    assert _sim()._compile_plan().shared_dir.name != before


def test_an_input_too_large_to_hash_keeps_two_checkouts_apart(tmp_path, monkeypatch):
    """Above the hashing cap the input falls back to its stats, so two branches with different ROM images get different keys."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(vlog_sim_module, "_CONTENT_HASH_MAX_BYTES", 8)
    for name, rom in (("wt-a", "0" * 64), ("wt-b", "1" * 64)):
        checkout = tmp_path / name
        _write_checkout(checkout)
        (checkout / "rtl" / "rom.hex").write_text(rom)

    def _key(name):
        _as_a_fresh_process()
        return (
            _cache_sim(
                tmp_path / name,
                monkeypatch,
                cache_root=cache,
                test_name="t",
                compile_opts=[str(tmp_path / name / "rtl" / "rom.hex")],
            )
            ._compile_plan()
            .shared_dir.name
        )

    assert _key("wt-a") != _key("wt-b"), (
        "two checkouts with different unhashable images share one obj_dir"
    )


def test_an_unhashable_run_f_source_keeps_two_checkouts_apart(tmp_path, monkeypatch):
    """The same holds for a `run.f` entry, which is a `sources` stamp."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(vlog_sim_module, "_CONTENT_HASH_MAX_BYTES", 8)

    def _key(name, rom):
        checkout = tmp_path / name
        suite = _write_checkout(checkout)
        (checkout / "rtl" / "rom.hex").write_text(rom)
        _as_a_fresh_process()
        return (
            _make_sim(
                checkout,
                monkeypatch,
                test_name="t",
                suite_dir=suite,
                project_root=checkout,
                model_path=suite / "models.yaml",
                filelist=["../../rtl/a.sv", "../../rtl/rom.hex"],
                shared_build_root=cache,
            )
            ._compile_plan()
            .shared_dir.name
        )

    assert _key("wt-a", "0" * 64) != _key("wt-b", "1" * 64)


def test_an_out_of_root_input_that_cannot_be_hashed_stays_stat_free(
    tmp_path, monkeypatch
):
    """The stats fallback applies to relocated paths only; a path left absolute is outside every project root and stays out of the key."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(vlog_sim_module, "_CONTENT_HASH_MAX_BYTES", 8)
    checkout = tmp_path / "wt-a"
    suite = _write_checkout(checkout)
    outside = tmp_path / "vendor"
    outside.mkdir()
    (outside / "big.sv").write_text("module big; endmodule\n" + "//" * 64)

    def _key():
        _as_a_fresh_process()
        return (
            _make_sim(
                checkout,
                monkeypatch,
                test_name="t",
                suite_dir=suite,
                project_root=checkout,
                model_path=suite / "models.yaml",
                filelist=["../../rtl/a.sv", str(outside / "big.sv")],
                shared_build_root=cache,
            )
            ._compile_plan()
            .shared_dir.name
        )

    before = _key()
    _touch(outside / "big.sv", "module big; endmodule\n" + "//" * 65)
    assert _key() == before


def test_a_path_valued_define_is_never_relativised(tmp_path, monkeypatch):
    """A define's value is compiled into the model, so `+define+DATA="/wt-a/data.hex"` is not relocated and two checkouts do not share."""
    cache = tmp_path / "cache"
    for name in ("wt-a", "wt-b"):
        _write_checkout(tmp_path / name)

    def _plan(name, opts):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=opts,
        )._compile_plan()

    def _define(name):
        return f'+define+DATA="{tmp_path / name / "data.hex"}"'

    plan_a = _plan("wt-a", [_define("wt-a")])
    plan_b = _plan("wt-b", [_define("wt-b")])
    assert plan_a.shared_dir.name != plan_b.shared_dir.name
    assert _define("wt-a") in plan_a.fingerprint["cmd"], plan_a.fingerprint["cmd"]

    # The same for other value-bearing spellings and for a bare `NAME=value`.
    for opt in (
        f"-DDATA={tmp_path / 'wt-a' / 'data.hex'}",
        f"-GROM={tmp_path / 'wt-a' / 'data.hex'}",
        f"-pvalue+top.ROM={tmp_path / 'wt-a' / 'data.hex'}",
        f"ROM={tmp_path / 'wt-a' / 'data.hex'}",
    ):
        assert opt in _plan("wt-a", [opt]).fingerprint["cmd"], opt

    # A genuine path option still relativises.
    for name in ("wt-a", "wt-b"):
        (tmp_path / name / "hdr").mkdir()
        (tmp_path / name / "hdr" / "w.svh").write_text("`define W 8\n")
    incdir_a = _plan("wt-a", [f"+incdir+{tmp_path / 'wt-a' / 'hdr'}"])
    incdir_b = _plan("wt-b", [f"+incdir+{tmp_path / 'wt-b' / 'hdr'}"])
    assert "+incdir+hdr" in incdir_a.fingerprint["cmd"]
    assert incdir_a.shared_dir.name == incdir_b.shared_dir.name


def test_an_output_option_argument_still_relativises_but_is_never_read(
    tmp_path, monkeypatch
):
    """`-o <in-root path>` carries no checkout prefix into the key and is not hashed."""
    cache = tmp_path / "cache"
    for name in ("wt-a", "wt-b"):
        _write_checkout(tmp_path / name)
        (tmp_path / name / "out").mkdir()

    def _plan(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=["-o", str(tmp_path / name / "out" / "simv")],
        )._compile_plan()

    plan_a = _plan("wt-a")
    assert "out/simv" in plan_a.fingerprint["cmd"]
    assert plan_a.shared_dir.name == _plan("wt-b").shared_dir.name
    sim = _cache_sim(
        tmp_path / "wt-a",
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=["-o", str(tmp_path / "wt-a" / "out" / "simv")],
    )
    assert (
        sim._fingerprint_cmd_inputs(plan_a.key_cmd, plan_a.fingerprint["sources"]) == []
    )


def test_a_checkout_under_a_dot_directory_is_still_content_keyed(tmp_path, monkeypatch):
    """The output test asks only what is below the project root, so a workspace under a dot-directory (such as `/home/ci/.worktrees/pr`) is still keyed."""
    cache = tmp_path / "cache"
    dotted = tmp_path / ".worktrees"
    dotted.mkdir()
    for name in ("wt-a", "wt-b"):
        _write_checkout(dotted / name)
        (dotted / name / "hdr").mkdir()
    (dotted / "wt-a" / "hdr" / "cmd.svh").write_text("`define CMD_W 8\n")
    (dotted / "wt-b" / "hdr" / "cmd.svh").write_text("`define CMD_W 16\n")

    def _sim(name):
        _as_a_fresh_process()
        return _cache_sim(
            dotted / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[f"+incdir+{dotted / name / 'hdr'}"],
        )

    sim_a = _sim("wt-a")
    plan_a = sim_a._compile_plan()
    assert sim_a._fingerprint_cmd_inputs(
        plan_a.key_cmd, plan_a.fingerprint["sources"]
    ), "a dot component in the checkout path disabled the content keying"
    assert plan_a.shared_dir.name != _sim("wt-b")._compile_plan().shared_dir.name
    # A real builder tree below the root is refused, as is an rtl_buddy output named directly, judged by name.
    artefacts = dotted / "wt-a" / "verif" / "blk" / "artefacts"
    artefacts.mkdir(parents=True, exist_ok=True)
    for refused in (
        artefacts / ".shared-builds" / "obj_dir_abc" / "simv",
        artefacts / "obj_dir_t" / "Vtop.cpp",
        artefacts / "t" / "run.f",
        artefacts / "t" / "compile.log",
    ):
        refused.parent.mkdir(parents=True, exist_ok=True)
        refused.write_text("x\n")
        assert sim_a._key_input_path(str(refused)) is None, refused
    generated = artefacts / "t" / "gen" / "gen.svh"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("`define G 1\n")
    assert sim_a._key_input_path(str(generated)) == str(generated)


def test_a_path_valued_plusdefine_in_run_f_is_never_relativised(tmp_path, monkeypatch):
    """`tests.yaml` plusdefines are treated like compile-line defines and are not relocated."""
    cache = tmp_path / "cache"
    for name in ("wt-a", "wt-b"):
        _write_checkout(tmp_path / name)

    def _plan(name):
        _as_a_fresh_process()
        suite = tmp_path / name / "verif" / "blk"
        return _make_sim(
            tmp_path / name,
            monkeypatch,
            test_name="t",
            suite_dir=suite,
            project_root=tmp_path / name,
            model_path=suite / "models.yaml",
            filelist=[
                "../../rtl/a.sv",
                f"+define+DATA={tmp_path / name / 'data.hex'}",
            ],
            shared_build_root=cache,
        )._compile_plan()

    plan_a, plan_b = _plan("wt-a"), _plan("wt-b")
    assert plan_a.shared_dir.name != plan_b.shared_dir.name, (
        "two checkouts baking in different data paths share one key"
    )
    define = [
        entry[0]
        for entry in plan_a.fingerprint["sources"]
        if entry[0].startswith("+define+")
    ]
    assert define == [f"+define+DATA={tmp_path / 'wt-a' / 'data.hex'}"], define
    # A `tests.yaml` plusdefine, which reaches the builder on the command line, is equally verbatim.
    _as_a_fresh_process()
    plusdefine = _cache_sim(
        tmp_path / "wt-a",
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=[],
    )
    plusdefine.test_cfg.pd = {"DATA": str(tmp_path / "wt-a" / "data.hex")}
    assert (
        f"+define+DATA={tmp_path / 'wt-a' / 'data.hex'}"
        in plusdefine._compile_plan().fingerprint["cmd"]
    )


def test_an_in_root_path_embedded_in_an_option_is_content_keyed(tmp_path, monkeypatch):
    """`-CFLAGS=-I<root>/inc` names a directory the build reads, so its content is keyed."""
    cache = tmp_path / "cache"
    for name, content in (("wt-a", "#define W 8\n"), ("wt-b", "#define W 16\n")):
        checkout = tmp_path / name
        _write_checkout(checkout)
        (checkout / "inc").mkdir()
        (checkout / "inc" / "dut.h").write_text(content)

    def _plan(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[f"-CFLAGS=-I{tmp_path / name / 'inc'}"],
        )._compile_plan()

    plan_a, plan_b = _plan("wt-a"), _plan("wt-b")
    assert plan_a.shared_dir.name != plan_b.shared_dir.name, (
        "an embedded include directory's content is not in the key"
    )
    # The token text is relativised too, so identical content at two paths still shares.
    assert "-CFLAGS=-Iinc" in plan_a.fingerprint["cmd"], plan_a.fingerprint["cmd"]
    (tmp_path / "wt-b" / "inc" / "dut.h").write_text("#define W 8\n")
    assert _plan("wt-a").shared_dir.name == _plan("wt-b").shared_dir.name


def test_an_embedded_path_is_keyed_by_what_it_turns_out_to_be(tmp_path, monkeypatch):
    """A file embedded in an option is keyed by its hash, a directory by its listing, and a define's value by neither."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    (checkout / "inc").mkdir()
    (checkout / "inc" / "dut.h").write_text("#define W 8\n")
    (checkout / "cfg.vlt").write_text("`verilator_config\n")

    def _entries(opts):
        _as_a_fresh_process()
        sim = _cache_sim(
            checkout, monkeypatch, cache_root=cache, test_name="t", compile_opts=opts
        )
        plan = sim._compile_plan()
        return [
            entry[0]
            for entry in sim._fingerprint_cmd_inputs(
                plan.key_cmd, plan.fingerprint["sources"]
            )
        ]

    assert _entries([f"-CFLAGS=-I{checkout / 'inc'}"]) == ["+incdir+inc"]
    assert _entries([f"--config={checkout / 'cfg.vlt'}"]) == ["cfg.vlt"]
    assert _entries([f"+define+DATA={checkout / 'cfg.vlt'}"]) == []
    assert _entries(
        [f"-CFLAGS=-I{checkout / 'inc'} -include {checkout / 'cfg.vlt'}"]
    ) == ["+incdir+inc", "cfg.vlt"]


def _compile_cwd_of(sim):
    """Where the builder runs, which is what a `-f` list's entries anchor to."""
    return Path(sim._compile_plan().compile_work_dir)


def test_a_relative_entry_in_a_dash_f_list_resolves_against_the_compile_cwd(
    tmp_path, monkeypatch
):
    """`-f` entries are relative to the builder's cwd for verilator, VCS and Icarus alike.
    The entry climbs out of `artefacts/<test>/`, since a path inside it would name rtl_buddy's own output tree.
    """
    cache = tmp_path / "cache"
    # From `<checkout>/verif/blk/artefacts/<test>`, up four levels to the checkout.
    entry = "../../../../rtl/cwd_rtl.sv"
    for name, body in (
        ("wt-a", "module cwd_rtl; endmodule\n"),
        ("wt-b", "module cwd_rtl; /* patched */ endmodule\n"),
    ):
        checkout = tmp_path / name
        _write_checkout(checkout)
        (checkout / "rtl" / "cwd_rtl.sv").write_text(body)
        lists = checkout / "lists"
        lists.mkdir()
        (lists / "cwd.f").write_text(f"{entry}\n")

    def _sim(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=["-f", str(tmp_path / name / "lists" / "cwd.f")],
        )

    sim_a = _sim("wt-a")
    plan_a = sim_a._compile_plan()
    keyed = [
        entry_row[0]
        for entry_row in sim_a._fingerprint_cmd_inputs(
            plan_a.key_cmd, plan_a.fingerprint["sources"]
        )
    ]
    assert keyed == ["-f lists/cwd.f", "rtl/cwd_rtl.sv"], keyed
    assert plan_a.shared_dir.name != _sim("wt-b")._compile_plan().shared_dir.name


def test_the_same_list_reached_by_dash_F_resolves_against_the_list_dir(
    tmp_path, monkeypatch
):
    """`-F` anchors to the list; one list given with both options gets two different answers."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    lists = checkout / "lists"
    lists.mkdir()
    (lists / "both.f").write_text("beside.sv\n")
    (lists / "beside.sv").write_text("module beside; endmodule\n")

    def _entries(option):
        _as_a_fresh_process()
        sim = _cache_sim(
            checkout,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[option, str(lists / "both.f")],
        )
        plan = sim._compile_plan()
        return [
            row[0]
            for row in sim._fingerprint_cmd_inputs(
                plan.key_cmd, plan.fingerprint["sources"]
            )
        ]

    assert _entries("-F") == ["-F lists/both.f", "lists/beside.sv"]
    # Read as `-f`, the text names `beside.sv` under the compile dir, which does not exist, so only the list itself is keyed.
    assert _entries("-f") == ["-f lists/both.f"]


def test_a_nested_list_switches_the_base_its_entries_anchor_to(tmp_path, monkeypatch):
    """A nested option resets the anchor: a `-f` inside a `-F` list anchors its entries to the builder's cwd."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    (checkout / "rtl" / "from_cwd.sv").write_text("module from_cwd; endmodule\n")
    lists = checkout / "lists"
    lists.mkdir()
    # Outer list reached by -F: its entries are list-relative.
    (lists / "outer.f").write_text("-f inner.f\nbeside.sv\n")
    (lists / "beside.sv").write_text("module beside; endmodule\n")
    # Inner list reached by -f: its entry is cwd-relative, climbing out of the artefact dir to the checkout's rtl/.
    (lists / "inner.f").write_text("../../../../rtl/from_cwd.sv\n")
    # A decoy at the spelling the list-relative reading would produce.
    (lists / "from_cwd.sv").write_text("module decoy; endmodule\n")

    _as_a_fresh_process()
    sim = _cache_sim(
        checkout,
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=["-F", str(lists / "outer.f")],
    )
    plan = sim._compile_plan()
    keyed = [
        row[0]
        for row in sim._fingerprint_cmd_inputs(
            plan.key_cmd, plan.fingerprint["sources"]
        )
    ]
    assert keyed == [
        "-F lists/outer.f",
        "-f lists/inner.f",
        # the inner list's entry took the compile cwd...
        "rtl/from_cwd.sv",
        # ...while the outer list's own entry stayed list-relative.
        "lists/beside.sv",
    ], keyed
    assert "lists/from_cwd.sv" not in keyed


def test_a_relative_dash_f_entry_is_text_only_with_no_compile_cwd(
    tmp_path, monkeypatch
):
    """With no plan yet there is no builder cwd, so a relative `-f` entry is left as text."""
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    lists = checkout / "lists"
    lists.mkdir()
    (lists / "cwd.f").write_text("somewhere.sv\n")
    (lists / "somewhere.sv").write_text("module somewhere; endmodule\n")
    sim = _cache_sim(
        checkout, monkeypatch, cache_root=tmp_path / "cache", test_name="t"
    )
    assert sim._compile_cwd is None
    assert (
        list(
            sim._nested_filelist_tokens(
                str(lists / "cwd.f"), seen=set(), depth=1, base=None
            )
        )
        == []
    )
    # An absolute entry needs no base and is keyed either way.
    (lists / "abs.f").write_text(f"{lists / 'somewhere.sv'}\n")
    assert [
        entry[0]
        for entry in sim._nested_filelist_tokens(
            str(lists / "abs.f"), seen=set(), depth=1, base=None
        )
    ] == ["lists/somewhere.sv"]


def test_a_relative_compile_line_incdir_is_content_keyed(tmp_path, monkeypatch):
    """A relative `+incdir+inc` is resolved against the builder's cwd and its content is keyed, so two checkouts with different headers there do not share."""
    cache = tmp_path / "cache"
    for name, content in (("wt-a", "`define W 8\n"), ("wt-b", "`define W 16\n")):
        checkout = tmp_path / name
        _write_checkout(checkout)
        # Relative to the compile dir (`<suite>/artefacts/<test>`), climbing out to a directory a testbench shares.
        header_dir = checkout / "inc"
        header_dir.mkdir()
        (header_dir / "w.svh").write_text(content)

    rel = os.path.join("..", "..", "..", "..", "inc")

    def _plan(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[f"+incdir+{rel}"],
        )._compile_plan()

    plan_a = _plan("wt-a")
    assert plan_a.shared_dir.name != _plan("wt-b").shared_dir.name, (
        "a relative +incdir+ contributed no content to the key"
    )
    # Recorded under what it resolves to, so it is not confused with a run.f entry of the same raw spelling.
    sim = _cache_sim(
        tmp_path / "wt-a",
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=[f"+incdir+{rel}"],
    )
    plan = sim._compile_plan()
    assert [
        entry[0]
        for entry in sim._fingerprint_cmd_inputs(
            plan.key_cmd, plan.fingerprint["sources"]
        )
    ] == ["+incdir+inc"]


def test_relative_compile_line_inputs_of_every_shape_are_keyed(tmp_path, monkeypatch):
    """`-y`, `-v` and a bare relative source are keyed as well as `+incdir+`."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    (checkout / "lib").mkdir()
    (checkout / "lib" / "cell.sv").write_text("module cell; endmodule\n")
    (checkout / "rtl" / "extra.sv").write_text("module extra; endmodule\n")
    (checkout / "rtl" / "bare.sv").write_text("module bare; endmodule\n")
    up = os.path.join("..", "..", "..", "..")

    _as_a_fresh_process()
    sim = _cache_sim(
        checkout,
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=[
            "-y",
            os.path.join(up, "lib"),
            "-v",
            os.path.join(up, "rtl", "extra.sv"),
            os.path.join(up, "rtl", "bare.sv"),
            # A directory the filelist already names, which the `covered` check drops rather than keying twice.
            os.path.join(up, "rtl", "a.sv"),
        ],
    )
    plan = sim._compile_plan()
    assert [
        entry[0]
        for entry in sim._fingerprint_cmd_inputs(
            plan.key_cmd, plan.fingerprint["sources"]
        )
    ] == ["-y lib", "-v rtl/extra.sv", "rtl/bare.sv"]


def test_a_generated_header_under_an_artefact_incdir_is_keyed(tmp_path, monkeypatch):
    """A compile-line `+incdir+` pointing at a `preproc` `artifact_dir` is tracked like a `run.f` incdir there."""
    cache = tmp_path / "cache"
    for name, content in (("wt-a", "`define G 1\n"), ("wt-b", "`define G 2\n")):
        checkout = tmp_path / name
        _write_checkout(checkout)
        generated = checkout / "verif" / "blk" / "artefacts" / "t" / "gen"
        generated.mkdir(parents=True)
        (generated / "gen.svh").write_text(content)
        # rtl_buddy's own outputs sit beside it and are not keyed.
        (generated / "compile.log").write_text("noise\n")
        (generated / "simv").write_text("a binary\n")

    def _sim(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[
                f"+incdir+{tmp_path / name / 'verif' / 'blk' / 'artefacts' / 't' / 'gen'}"
            ],
        )

    sim_a = _sim("wt-a")
    plan_a = sim_a._compile_plan()
    entries = sim_a._fingerprint_cmd_inputs(
        plan_a.key_cmd, plan_a.fingerprint["sources"]
    )
    assert [entry[0] for entry in entries] == ["+incdir+verif/blk/artefacts/t/gen"]
    listed = [inner[0] for inner in entries[0][1]]
    assert listed == ["gen.svh"], listed
    assert plan_a.shared_dir.name != _sim("wt-b")._compile_plan().shared_dir.name


def test_a_filelist_chain_past_the_depth_bound_fails_closed(tmp_path, monkeypatch):
    """Inputs the key could not read (nesting beyond the `-F` depth limit) make the key checkout-specific."""
    cache = tmp_path / "cache"
    for name, deep in (
        ("wt-a", "module deep; endmodule\n"),
        ("wt-b", "module deep; /* patched */ endmodule\n"),
    ):
        checkout = tmp_path / name
        _write_checkout(checkout)
        lists = checkout / "lists"
        lists.mkdir()
        for level in range(10):
            (lists / f"l{level}.f").write_text(f"-F l{level + 1}.f\n")
        (lists / "l10.f").write_text("../rtl/deep.sv\n")
        (checkout / "rtl" / "deep.sv").write_text(deep)

    def _sim(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=["-F", str(tmp_path / name / "lists" / "l0.f")],
        )

    sim_a = _sim("wt-a")
    plan_a = sim_a._compile_plan()
    keyed = [
        entry[0]
        for entry in sim_a._fingerprint_cmd_inputs(
            plan_a.key_cmd, plan_a.fingerprint["sources"]
        )
    ]
    marker = [k for k in keyed if k.startswith(vlog_sim_module._DEPTH_BOUND_MARKER)]
    assert marker, keyed
    # The marker carries the absolute path, so the key is not shared with a checkout whose unread tail differs.
    assert str(tmp_path / "wt-a") in marker[0]
    assert plan_a.shared_dir.name != _sim("wt-b")._compile_plan().shared_dir.name
    # It is reported once, not once per level.
    assert sim_a._depth_bound_logged is True


def test_an_explicit_disable_reaches_the_dispatched_jobs(monkeypatch):
    """`--shared-build-root ''` disables the cache for the whole run, including the jobs."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    class _Root:
        def get_project_rootdir(self):
            return "/proj"

        def get_shared_build_root(self):
            return "/from/config"

    app = RtlBuddy(name="test_shared_build_disable")
    app.root_cfg = _Root()
    monkeypatch.delenv("RTL_BUDDY_SHARED_BUILD_ROOT", raising=False)
    # Configured and not overridden: forwarded as the resolved path.
    assert app.shared_build_root == "/from/config"
    assert app.shared_build_root_for_jobs == "/from/config"
    # Explicitly disabled: the jobs are told so rather than re-resolving the config.
    app._shared_build_root_flag = ""
    assert app.shared_build_root is None
    assert app.shared_build_root_for_jobs == ""
    # Disabling through the environment counts the same.
    app._shared_build_root_flag = None
    monkeypatch.setenv("RTL_BUDDY_SHARED_BUILD_ROOT", "")
    assert app.shared_build_root_for_jobs == ""
    # Nothing configured: nothing is forwarded, so the job argv is unchanged.
    monkeypatch.delenv("RTL_BUDDY_SHARED_BUILD_ROOT", raising=False)

    class _Bare(_Root):
        def get_shared_build_root(self):
            return None

    app.root_cfg = _Bare()
    assert app.shared_build_root_for_jobs is None


def test_a_job_given_an_empty_shared_build_root_keeps_the_cache_off(monkeypatch):
    """An empty `--shared-build-root` in a job overrides the environment and the config."""
    from rtl_buddy.rtl_buddy import RtlBuddy

    class _Root:
        def get_project_rootdir(self):
            return "/proj"

        def get_shared_build_root(self):
            return "/from/config"

    job = RtlBuddy(name="test_shared_build_job")
    job.root_cfg = _Root()
    monkeypatch.setenv("RTL_BUDDY_SHARED_BUILD_ROOT", "/from/env")
    job._shared_build_root_flag = ""
    assert job.shared_build_root is None


def test_a_relative_include_inside_a_compiler_flag_is_content_keyed(
    tmp_path, monkeypatch
):
    """`-CFLAGS=-I../../inc` is identical text in every checkout, so the option that introduces the path is what identifies it and its content is keyed."""
    cache = tmp_path / "cache"
    # From `<checkout>/verif/blk/artefacts/<test>`, up four to the checkout.
    rel = os.path.join("..", "..", "..", "..", "inc")
    for name, content in (("wt-a", "#define W 8\n"), ("wt-b", "#define W 16\n")):
        checkout = tmp_path / name
        _write_checkout(checkout)
        (checkout / "inc").mkdir()
        (checkout / "inc" / "dut.h").write_text(content)

    def _sim(name):
        _as_a_fresh_process()
        return _cache_sim(
            tmp_path / name,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=[f"-CFLAGS=-I{rel}"],
        )

    sim_a = _sim("wt-a")
    plan_a = sim_a._compile_plan()
    keyed = [
        entry[0]
        for entry in sim_a._fingerprint_cmd_inputs(
            plan_a.key_cmd, plan_a.fingerprint["sources"]
        )
    ]
    assert keyed == ["+incdir+inc"], keyed
    assert plan_a.shared_dir.name != _sim("wt-b")._compile_plan().shared_dir.name, (
        "a relative -I contributed no directory contents to the key"
    )
    # Identical contents still share.
    (tmp_path / "wt-b" / "inc" / "dut.h").write_text("#define W 8\n")
    assert _sim("wt-a")._compile_plan().shared_dir.name == (
        _sim("wt-b")._compile_plan().shared_dir.name
    )


def test_the_embedded_option_scan_reads_paths_and_ignores_the_rest(
    tmp_path, monkeypatch
):
    """Which embedded shapes count as a path and which deliberately do not."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    for name in ("inc", "lib", "other"):
        (checkout / name).mkdir()
        (checkout / name / "x.svh").write_text(f"`define {name.upper()} 1\n")
    up = os.path.join("..", "..", "..", "..")

    def _entries(opts):
        _as_a_fresh_process()
        sim = _cache_sim(
            checkout, monkeypatch, cache_root=cache, test_name="t", compile_opts=opts
        )
        plan = sim._compile_plan()
        return [
            entry[0]
            for entry in sim._fingerprint_cmd_inputs(
                plan.key_cmd, plan.fingerprint["sources"]
            )
        ]

    inc, lib = os.path.join(up, "inc"), os.path.join(up, "lib")
    # Attached and separated spellings, and a `-y` inside a bigger token.
    assert _entries([f"-CFLAGS=-I{inc}"]) == ["+incdir+inc"]
    assert _entries([f"-CFLAGS=-I {inc}"]) == ["+incdir+inc"]
    assert _entries([f"-XTRA=-y {lib}"]) == ["+incdir+lib"]
    # Several paths in one token, with the absolute/relative pair de-duplicated.
    assert _entries([f"-CFLAGS=-I{inc} -I{lib}"]) == ["+incdir+inc", "+incdir+lib"]
    assert _entries([f"-CFLAGS=-I{checkout / 'inc'} -I{inc}"]) == ["+incdir+inc"]
    # `+libext+` is a suffix list, an output option is an output, a define's value is a value, and `--Include`/`-Wno-INCDIR` are not `-I`.
    assert _entries(["-CFLAGS=+libext+.svh"]) == []
    assert _entries(["-o", os.path.join(up, "inc")]) == []
    assert _entries([f"+define+DIR={inc}"]) == []
    assert _entries([f"--Include={inc}"]) == []
    assert _entries([f"-Wno-INCDIR{inc}"]) == []
    assert _entries([f"-CFLAGS=-I{os.path.join(up, 'nope')}"]) == []


def test_one_filelist_read_under_two_bases_contributes_both_readings(
    tmp_path, monkeypatch
):
    """A list reached through both `-f` and `-F` is two sets of inputs, since its relative entries anchor differently; both are keyed."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    lists = checkout / "lists"
    lists.mkdir()
    child = lists / "child.f"
    # One child list named twice by one parent, by absolute path, differing only in the base its relative entry anchors to.
    (lists / "outer.f").write_text(f"-F {child}\n-f {child}\n")
    child.write_text("shared.sv\n")
    (lists / "shared.sv").write_text("module beside_the_list; endmodule\n")

    def _walk():
        _as_a_fresh_process()
        sim = _cache_sim(
            checkout,
            monkeypatch,
            cache_root=cache,
            test_name="t",
            compile_opts=["-F", str(lists / "outer.f")],
        )
        plan = sim._compile_plan()
        return (
            sim,
            plan,
            [
                entry[0]
                for entry in sim._fingerprint_cmd_inputs(
                    plan.key_cmd, plan.fingerprint["sources"]
                )
            ],
        )

    sim, plan, _ = _walk()
    compile_cwd = Path(plan.compile_work_dir)
    compile_cwd.mkdir(parents=True, exist_ok=True)
    (compile_cwd / "shared.sv").write_text("module under_the_cwd; endmodule\n")

    _, _, keyed = _walk()
    # The child list under each option, and both files its one entry names.
    assert "lists/shared.sv" in keyed, keyed
    cwd_reading = [
        spelling
        for spelling in keyed
        if spelling.endswith("shared.sv") and spelling != "lists/shared.sv"
    ]
    assert cwd_reading, keyed
    # The same list under the same base is entered once, so the cycle guard still holds.
    assert keyed.count("lists/shared.sv") == 1, keyed


def test_a_filelist_cycle_under_one_base_is_still_entered_once(tmp_path, monkeypatch):
    """The visitation identity still protects against cycles."""
    cache = tmp_path / "cache"
    checkout = tmp_path / "wt-a"
    _write_checkout(checkout)
    lists = checkout / "lists"
    lists.mkdir()
    (lists / "loop.f").write_text("-F loop.f\nbeside.sv\n")
    (lists / "beside.sv").write_text("module beside; endmodule\n")

    _as_a_fresh_process()
    sim = _cache_sim(
        checkout,
        monkeypatch,
        cache_root=cache,
        test_name="t",
        compile_opts=["-F", str(lists / "loop.f")],
    )
    plan = sim._compile_plan()
    keyed = [
        entry[0]
        for entry in sim._fingerprint_cmd_inputs(
            plan.key_cmd, plan.fingerprint["sources"]
        )
    ]
    assert keyed == ["-F lists/loop.f", "lists/beside.sv"], keyed


# --- the split compile: verilate job, then build job ----------------

_MARKER = vlog_sim_module.VERILATE_MARKER_NAME


@pytest.fixture(autouse=True)
def _forget_no_verilate_support():
    """The `--help` probe is answered once per executable per process."""
    vlog_sim_module._NO_VERILATE_SUPPORT.clear()
    yield
    vlog_sim_module._NO_VERILATE_SUPPORT.clear()


def _install_phase_aware_builder(monkeypatch, calls, *, returncode=0, stderr=""):
    """A fake Verilator that produces a binary only when asked to build.
    The shared `_install_fake_builder` writes a simv wherever it sees `--Mdir`, which would let the build half validate a directory the verilate half never built.
    """

    def _fake_run(cmd, capture_output, text, cwd, env=None):
        calls.append({"cmd": list(cmd), "cwd": cwd})
        mdir = Path(cmd[cmd.index("--Mdir") + 1])
        mdir = mdir if mdir.is_absolute() else Path(cwd) / mdir
        mdir.mkdir(parents=True, exist_ok=True)
        if "--no-verilate" not in cmd:
            (mdir / "Vtop.mk").write_text("default:\n\t@true\n")
        if returncode == 0 and (
            "--binary" in cmd or "--build" in cmd or "--no-verilate" in cmd
        ):
            (mdir / "simv").write_text("binary\n")
        return ManagedProcessResult(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)


def _split_sim(tmp_path, monkeypatch, *, phase, name="alpha", **kwargs):
    return _make_sim(
        tmp_path,
        monkeypatch,
        test_name=name,
        compile_opts=["--binary"],
        build_phase=phase,
        **kwargs,
    )


def _shared_dir_of(sim):
    return Path(sim._compile_plan().shared_dir)


def test_the_compile_key_is_identical_in_every_phase(tmp_path, monkeypatch):
    """The phase does not reach the compile key, so both halves use one build directory; the rewrite happens at argv emission."""
    _write_source(tmp_path)
    keys = []
    for phase in ("full", "verilate", "build"):
        plan = _split_sim(tmp_path, monkeypatch, phase=phase)._compile_plan()
        keys.append(
            (
                tuple(plan.key_cmd),
                vlog_sim_module._fingerprint_sha(plan.fingerprint),
                str(plan.shared_dir),
            )
        )
    assert keys[0] == keys[1] == keys[2]


def test_the_verilate_phase_emits_the_front_end_and_no_binary(tmp_path, monkeypatch):
    """The verilate half is `--binary` without the build; it writes the marker, not the stamp."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    sim = _split_sim(tmp_path, monkeypatch, phase="verilate")
    shared = _shared_dir_of(sim)

    assert sim.compile() == 0

    cmd = calls[0]["cmd"]
    assert "--binary" not in cmd and "--build" not in cmd
    assert {"--exe", "--main", "--timing"} <= set(cmd)
    assert not (shared / "simv").exists()
    assert not (shared / vlog_sim_module.SHARED_BUILD_STAMP_NAME).exists()
    marker = json.loads((shared / _MARKER).read_text())
    assert marker["status"] == "ok"
    assert marker["fingerprint_sha"] is not None
    assert marker["duration_sec"] is not None
    assert marker["transcript"].endswith("compile.log")


def test_the_build_phase_runs_make_alone_and_consumes_the_marker(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    verilate = _split_sim(tmp_path, monkeypatch, phase="verilate")
    shared = _shared_dir_of(verilate)
    assert verilate.compile() == 0

    vlog_sim_module._NO_VERILATE_SUPPORT[verilate.rtl_builder_cfg.get_exe()] = True
    build = _split_sim(tmp_path, monkeypatch, phase="build")
    assert build.compile() == 0

    assert "--no-verilate" in calls[1]["cmd"]
    # The build exists and is stamped, so a gated simulation can reuse it.
    assert (shared / "simv").exists()
    assert (shared / vlog_sim_module.SHARED_BUILD_STAMP_NAME).exists()
    # The marker is gone.
    assert not (shared / _MARKER).exists()


def test_the_build_phase_reports_the_whole_compiles_duration(tmp_path, monkeypatch):
    """The build half adds the verilation time it was handed, so the envelope sees one compile."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    verilate = _split_sim(tmp_path, monkeypatch, phase="verilate")
    shared = _shared_dir_of(verilate)
    assert verilate.compile() == 0
    # Pin the recorded verilation so the sum is a fixed number.
    marker = json.loads((shared / _MARKER).read_text())
    marker["duration_sec"] = 120.0
    (shared / _MARKER).write_text(json.dumps(marker))

    vlog_sim_module._NO_VERILATE_SUPPORT[verilate.rtl_builder_cfg.get_exe()] = True
    build = _split_sim(tmp_path, monkeypatch, phase="build")
    assert build.compile() == 0

    record = build.last_compile
    assert record["verilate_sec"] == 120.0
    assert record["build_sec"] == pytest.approx(record["duration_sec"] - 120.0)
    assert record["duration_sec"] >= 120.0
    assert record["reused"] is False


def test_an_unsplit_compile_records_only_the_three_keys_it_always_did(
    tmp_path, monkeypatch
):
    _write_source(tmp_path)
    _install_phase_aware_builder(monkeypatch, [])
    sim = _split_sim(tmp_path, monkeypatch, phase="full")
    assert sim.compile() == 0
    assert set(sim.last_compile) == {"duration_sec", "builder", "reused"}


@pytest.mark.parametrize(
    "break_marker, reason",
    [
        (lambda path: path.unlink(), "marker-missing"),
        (
            lambda path: path.write_text(
                json.dumps({"status": "ok", "fingerprint_sha": "deadbeef"})
            ),
            "marker-stale",
        ),
    ],
    ids=["missing", "stale"],
)
def test_the_build_phase_falls_back_to_a_full_compile(
    tmp_path, monkeypatch, caplog, break_marker, reason
):
    """With no marker for these inputs, the build job verilates too and says so."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    verilate = _split_sim(tmp_path, monkeypatch, phase="verilate")
    shared = _shared_dir_of(verilate)
    assert verilate.compile() == 0
    break_marker(shared / _MARKER)

    vlog_sim_module._NO_VERILATE_SUPPORT[verilate.rtl_builder_cfg.get_exe()] = True
    build = _split_sim(tmp_path, monkeypatch, phase="build")
    with caplog.at_level(logging.WARNING):
        assert build.compile() == 0

    assert "--no-verilate" not in calls[1]["cmd"]
    assert "--binary" in calls[1]["cmd"]
    fallbacks = [
        record.__dict__["rtl_fields"]
        for record in caplog.records
        if record.__dict__.get("rtl_event") == "compile.build_phase_fallback"
    ]
    assert [entry["reason"] for entry in fallbacks] == [reason]
    assert fallbacks[0]["test"] == "alpha"
    assert (shared / "simv").exists()


def test_a_verilator_without_no_verilate_falls_back_and_says_so(
    tmp_path, monkeypatch, caplog
):
    """The flag is probed rather than version-gated."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    verilate = _split_sim(tmp_path, monkeypatch, phase="verilate")
    assert verilate.compile() == 0

    vlog_sim_module._NO_VERILATE_SUPPORT[verilate.rtl_builder_cfg.get_exe()] = False
    build = _split_sim(tmp_path, monkeypatch, phase="build")
    with caplog.at_level(logging.WARNING):
        assert build.compile() == 0

    assert "--no-verilate" not in calls[1]["cmd"]
    reasons = [
        record.__dict__["rtl_fields"]["reason"]
        for record in caplog.records
        if record.__dict__.get("rtl_event") == "compile.build_phase_fallback"
    ]
    assert reasons == ["no-verilate-unsupported"]


def test_the_no_verilate_probe_is_answered_once_per_process(tmp_path, monkeypatch):
    _write_source(tmp_path)
    probes = []

    def _fake_help(argv, capture_output=True, text=True, timeout=None):
        probes.append(list(argv))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="  --no-verilate\n", stderr="")

    monkeypatch.setattr(vlog_sim_module.subprocess, "run", _fake_help)
    sim = _split_sim(tmp_path, monkeypatch, phase="build")
    assert sim._verilator_supports_no_verilate() is True
    assert sim._verilator_supports_no_verilate() is True
    assert [argv[1] for argv in probes] == ["--help"]


def test_a_failed_verilation_is_not_re_run_under_the_build_reservation(
    tmp_path, monkeypatch, caplog
):
    """A deterministic failure is not retried in the build half."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(
        monkeypatch, calls, returncode=1, stderr="%Error: syntax\n"
    )
    verilate = _split_sim(tmp_path, monkeypatch, phase="verilate")
    shared = _shared_dir_of(verilate)
    assert verilate.compile() != 0
    marker = json.loads((shared / _MARKER).read_text())
    assert marker["status"] == "failed"
    transcript = marker["transcript"]

    vlog_sim_module._NO_VERILATE_SUPPORT[verilate.rtl_builder_cfg.get_exe()] = True
    build = _split_sim(tmp_path, monkeypatch, phase="build")
    with caplog.at_level(logging.ERROR):
        assert build.compile() == 1

    assert len(calls) == 1
    assert build.compile_fail_desc.endswith(f"(see {transcript})")
    assert build.last_compile_failure["transcript"] == transcript
    assert "compile.verilate_failed" in [
        record.__dict__.get("rtl_event") for record in caplog.records
    ]


def test_a_sibling_on_one_key_does_not_verilate_it_twice(tmp_path, monkeypatch):
    """Configs sharing a compile key share a build directory; the marker short-circuits the second."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    first = _split_sim(tmp_path, monkeypatch, phase="verilate", name="alpha")
    assert first.compile() == 0
    second = _split_sim(tmp_path, monkeypatch, phase="verilate", name="beta")
    assert second.compile() == 0

    assert len(calls) == 1
    assert second.last_compile["reused"] is True


def test_a_compile_line_with_no_build_step_is_run_whole_by_the_verilate_phase(
    tmp_path, monkeypatch
):
    """`--cc` alone stops after the front end: the verilate job runs it and stamps, and the build job reuses."""
    _write_source(tmp_path)
    calls = []
    _install_phase_aware_builder(monkeypatch, calls)
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="alpha",
        compile_opts=["--cc"],
        build_phase="verilate",
    )
    shared = Path(sim._compile_plan().shared_dir)
    (shared).mkdir(parents=True, exist_ok=True)
    (shared / "simv").write_text("binary\n")

    assert sim.compile() == 0

    assert "--cc" in calls[0]["cmd"]
    # Stamped like any whole compile, with no marker.
    assert (shared / vlog_sim_module.SHARED_BUILD_STAMP_NAME).exists()
    assert not (shared / _MARKER).exists()


# --- a Verilator build dir must not outlive its toolchain --------
# Verilator's make includes the `*.d` files already in the directory, which name the previous toolchain's headers by absolute path; a directory shared across machines or a reinstalled Verilator then fails to make.


def _install_listing_builder(monkeypatch, calls):
    """The fake builder, plus what the `--Mdir` held when the builder ran."""
    _install_fake_builder(monkeypatch, calls)
    inner = vlog_sim_module.run_managed_process

    def _listing_run(cmd, capture_output, text, cwd, env=None):
        mdir = Path(cwd) / cmd[cmd.index("--Mdir") + 1]
        seen = sorted(p.name for p in mdir.iterdir()) if mdir.is_dir() else []
        result = inner(cmd, capture_output=capture_output, text=text, cwd=cwd, env=env)
        calls[-1]["mdir_before"] = seen
        return result

    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _listing_run)


def _plant_stale_objects(sim):
    mdir = Path(sim._get_simv_path()).parent
    mdir.mkdir(parents=True, exist_ok=True)
    for name in ("verilated.d", "verilated.o", "Vtop__ALL.a"):
        (mdir / name).write_text("/old/include/verilated.cpp\n")
    return mdir


def _toolchain_marker(sim):
    return json.loads(
        (
            Path(sim._get_simv_path()).parent
            / vlog_sim_module.BUILD_TOOLCHAIN_MARKER_NAME
        ).read_text()
    )


_STALE = {"verilated.d", "verilated.o", "Vtop__ALL.a"}


@pytest.mark.parametrize("share_build", [False, True])
def test_an_upgraded_verilator_starts_its_make_clean(
    tmp_path, monkeypatch, share_build
):
    """With another Verilator at the same path, the build dir is reused in both modes but its objects are scrubbed."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.038 2025-07-08")

    def _sim():
        return _make_sim(
            tmp_path,
            monkeypatch,
            test_name="test_a",
            exe=str(exe),
            share_build=share_build,
        )

    sim = _sim()
    assert sim.compile() == 0
    marker = _toolchain_marker(sim)
    assert marker["exe"] == os.path.realpath(exe)
    assert marker["version"] == "Verilator 5.038 2025-07-08"
    assert marker["platform"]
    _plant_stale_objects(sim)

    _touch(exe, '#!/bin/sh\necho "Verilator 5.050 2026-09-01"\n')
    again = _sim()
    assert again.compile() == 0
    assert len(calls) == 2
    assert again._get_simv_path() == sim._get_simv_path()
    assert not _STALE & set(calls[-1]["mdir_before"])
    assert _toolchain_marker(again)["version"] == "Verilator 5.050 2026-09-01"


def test_another_install_path_also_starts_clean(tmp_path, monkeypatch):
    """The unshared laptop/cluster case where the executable differs."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    mac = _fake_toolchain(tmp_path, "mac", "Verilator 5.050 2026-09-01")
    node = _fake_toolchain(tmp_path, "node", "Verilator 5.050 2026-09-01")

    sim = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(mac), share_build=False
    )
    assert sim.compile() == 0
    _plant_stale_objects(sim)
    other = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(node), share_build=False
    )
    assert other.compile() == 0
    assert not _STALE & set(calls[-1]["mdir_before"])
    assert _toolchain_marker(other)["exe"] == os.path.realpath(node)


def test_one_name_for_two_installs_still_starts_clean(tmp_path, monkeypatch):
    """Two hosts can share a `verilator` path at one version while the install differs; the install is what counts."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    laptop = _fake_toolchain(tmp_path, "laptop", "Verilator 5.050 2026-09-01")
    node = _fake_toolchain(tmp_path, "node", "Verilator 5.050 2026-09-01")
    link = tmp_path / "bin" / "verilator"
    link.parent.mkdir()
    link.symlink_to(laptop)

    sim = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(link), share_build=False
    )
    assert sim.compile() == 0
    _plant_stale_objects(sim)
    link.unlink()
    link.symlink_to(node)
    again = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(link), share_build=False
    )
    assert again.compile() == 0
    assert not _STALE & set(calls[-1]["mdir_before"])


def test_another_platform_starts_clean(tmp_path, monkeypatch):
    """Objects are reusable only on the machine type that compiled them."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.050 2026-09-01")

    def _sim():
        return _make_sim(
            tmp_path, monkeypatch, test_name="test_a", exe=str(exe), share_build=False
        )

    sim = _sim()
    assert sim.compile() == 0
    _plant_stale_objects(sim)
    monkeypatch.setattr(vlog_sim_module.platform, "machine", lambda: "riscv64")
    assert _sim().compile() == 0
    assert not _STALE & set(calls[-1]["mdir_before"])


def test_an_unchanged_verilator_keeps_its_objects(tmp_path, monkeypatch):
    """A source edit leaves the objects in place."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.050 2026-09-01")

    sim = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(exe), share_build=False
    )
    assert sim.compile() == 0
    _plant_stale_objects(sim)
    _write_source(tmp_path, "module top; wire w; endmodule\n")
    again = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(exe), share_build=False
    )
    assert again.compile() == 0
    assert _STALE <= set(calls[-1]["mdir_before"])


@pytest.mark.parametrize("share_build", [False, True])
def test_rebuild_starts_the_make_clean(tmp_path, monkeypatch, share_build):
    """`--rebuild` rebuilds for real without --share-build too."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.050 2026-09-01")

    sim = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe=str(exe),
        share_build=share_build,
    )
    assert sim.compile() == 0
    _plant_stale_objects(sim)
    vlog_sim_module._reset_rebuilt_dirs()
    forced = _make_sim(
        tmp_path,
        monkeypatch,
        test_name="test_a",
        exe=str(exe),
        share_build=share_build,
        rebuild=True,
    )
    assert forced.compile() == 0
    assert len(calls) == 2
    assert not _STALE & set(calls[-1]["mdir_before"])


def test_a_build_dir_with_no_recorded_toolchain_starts_clean_once(
    tmp_path, monkeypatch
):
    """An obj_dir without the marker (or left by a killed compile) gets one clean make, then incremental builds."""
    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.050 2026-09-01")

    sim = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(exe), share_build=False
    )
    _plant_stale_objects(sim)
    assert sim.compile() == 0
    assert not _STALE & set(calls[-1]["mdir_before"])

    _plant_stale_objects(sim)
    again = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(exe), share_build=False
    )
    assert again.compile() == 0
    assert _STALE <= set(calls[-1]["mdir_before"])


def test_the_build_half_of_a_split_compile_never_scrubs(tmp_path, monkeypatch):
    """The build half makes what its verilate job emitted; the verilate job decides whether the directory starts clean."""
    _write_source(tmp_path)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.050 2026-09-01")
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    plan = sim._compile_plan()
    mdir = _plant_stale_objects(sim)
    sim._skip_verilate = True
    sim._prepare_build_dir_toolchain(plan, forced=True)
    assert _STALE <= {p.name for p in mdir.iterdir()}
    assert not (mdir / vlog_sim_module.BUILD_TOOLCHAIN_MARKER_NAME).exists()


def test_a_scrub_explains_itself(tmp_path, monkeypatch, caplog):
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_listing_builder(monkeypatch, calls)
    mac = _fake_toolchain(tmp_path, "mac", "Verilator 5.038 2025-07-08")
    node = _fake_toolchain(tmp_path, "node", "Verilator 5.050 2026-09-01")
    sim = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe=str(mac), share_build=False
    )
    assert sim.compile() == 0
    _plant_stale_objects(sim)
    with caplog.at_level(_logging.INFO):
        assert (
            _make_sim(
                tmp_path,
                monkeypatch,
                test_name="test_a",
                exe=str(node),
                share_build=False,
            ).compile()
            == 0
        )
    assert "dropped 3 stale object/dependency files" in caplog.text
    assert f"built by Verilator 5.038 2025-07-08 ({os.path.realpath(mac)}, " in (
        caplog.text
    )
    assert f"now Verilator 5.050 2026-09-01 ({os.path.realpath(node)}, " in (
        caplog.text
    )
