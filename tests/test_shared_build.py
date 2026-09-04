import errno
import fcntl
import json
import logging
import os
import shutil
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest

from rtl_buddy import artifact_lock as artifact_lock_module
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.runner.test_runner import TestRunner as RtlBuddyTestRunner
from rtl_buddy.tools.artifact_paths import shared_build_dir
from rtl_buddy.tools import vlog_sim as vlog_sim_module


@pytest.fixture(autouse=True)
def _forget_rebuild_claims():
    """``--rebuild`` is honoured once per build dir per PROCESS (#494).

    pytest is one process for the whole file, so a claim left standing by
    one test would silently turn the next test's forced rebuild into a
    reuse — and the tmp_path spellings are close enough to collide once
    somebody parametrises them.
    """
    vlog_sim_module._reset_rebuilt_dirs()
    yield
    vlog_sim_module._reset_rebuilt_dirs()


@pytest.fixture(autouse=True)
def _forget_content_hashes():
    """The hash memo is keyed on (path, size, mtime_ns) per PROCESS (#494).

    tmp_path spellings and restored mtimes recur across tests in this file
    by design — the stale-stat tests fabricate exactly the collisions the
    memo is keyed on — so a stale memo entry would validate content one
    test rewrote for another.
    """
    with vlog_sim_module._CONTENT_HASH_LOCK:
        vlog_sim_module._CONTENT_HASH_CACHE.clear()
    yield
    with vlog_sim_module._CONTENT_HASH_LOCK:
        vlog_sim_module._CONTENT_HASH_CACHE.clear()


@pytest.fixture(autouse=True)
def _forget_reuse_announcements():
    """``compile.build_reused`` hits the console once per build dir per
    PROCESS (#494 review); console-assertion tests need a fresh slate."""
    vlog_sim_module._reset_reuse_announcements()
    yield
    vlog_sim_module._reset_reuse_announcements()


@pytest.fixture(autouse=True)
def _forget_lock_degrade_warnings():
    """``compile.build_lock_unavailable`` is emitted once per build dir per
    PROCESS (#494), which is one claim per pytest session unless reset."""
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
            # Only a root config that really has one gets the accessor: the
            # absent-accessor fallback (VlogSim built straight from tests,
            # older config objects) is a live path too, and the rest of this
            # file exercises it.
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
    def __init__(self, name, model_cfg, pd=None):
        self.name = name
        self.model = model_cfg
        self.tb = DummyTestbenchCfg()
        self.pd = pd
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
        return None

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
    pd=None,
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
):
    monkeypatch.chdir(tmp_path)
    builder_cfg = DummyBuilderCfg(
        exe=exe, simulator_family=family, simv=simv, compile_opts=compile_opts
    )
    model_cfg = DummyModelCfg(
        model_path or (tmp_path / "models.yaml"), filelist=filelist or ["src/top.sv"]
    )
    test_cfg = DummyTestCfg(test_name, model_cfg, pd=pd)
    return vlog_sim_module.VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=DummyRootCfg(builder_cfg, project_root=project_root),
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        suite_dir=str(suite_dir) if suite_dir is not None else None,
        share_build=share_build,
        rebuild=rebuild,
        run_id=run_id,
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
    """run_managed_process stand-in that drops a simv where the flags say.

    Mirrors each supported family's output convention: Verilator's
    ``--Mdir <dir>`` (simv inside it), and the ``-o <path>`` that VCS and
    Icarus take (simv/snapshot at exactly that path).

    ``depends`` (Verilator only) is the prerequisite list to write into a
    ``V<prefix>__ver.d`` beside the build, in the format Verilator really
    emits: absolute targets and the prerequisites relative to the *compile
    cwd* (not to ``--Mdir``), followed by ``--MP``'s tail of phony
    ``<prerequisite>:`` rules. That tail is what a project enabling ``--MP``
    in ``builder-opts`` gets, and it must not be mistaken for input.
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
            # A builder rtl_buddy cannot redirect: it drops its executable
            # where `builder-simv:` says, relative to the compile dir.
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
    # --Mdir was passed as the absolute shared build dir
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--Mdir") + 1] == str(Path(sim_a._get_simv_path()).parent)

    # The compile record the build envelope and the results overlay are
    # built from (#495). The producer is here, on the sim instance: a real
    # compile times itself, a reuse costs 0.0 and says so, and both name
    # the builder that (would have) run.
    assert sim_a.last_compile["reused"] is False
    # A real number, timed around the builder (0.0 here only because the
    # fake builder returns instantly); the reuse below is 0.0 by decision.
    assert isinstance(sim_a.last_compile["duration_sec"], float)
    assert sim_a.last_compile["builder"] == "verilator"
    assert sim_b.last_compile == {
        "duration_sec": 0.0,
        "builder": "verilator",
        "reused": True,
    }


def test_an_unshareable_builder_also_records_its_reuse(tmp_path, monkeypatch):
    """The per-test-stamp reuse path stamps the same record (#495).

    A builder that cannot share still short-circuits on its own stamp, and
    that branch is the one a dispatched re-run of an unchanged suite takes
    for every test — the reuse it reports is what stops right-sizing from
    reading "nothing compiled" as "the compile is fast".
    """
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
    """Probing settles the builder; it does not compile anything (#495).

    So a config that never reaches a builder still names one, with the
    duration and the reuse flag left unknown rather than guessed at 0.
    """
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
    """A failure is an observation too — the record is not gated on success.

    A compile that failed after 14 minutes is exactly the number the build
    job's reservation has to cover, so it counts as work that ran: the
    record is stamped before the pass/fail branch.
    """
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
    # same compile config -> same shared dir, rebuilt in place
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
    """VCS shares one build like Verilator does (#358)."""
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
    # Second test short-circuits on the stamp: one elaboration, not two.
    assert len(calls) == 1
    assert sim_a._get_simv_path() == sim_b._get_simv_path()
    shared = Path(sim_a._get_simv_path()).parent
    assert shared.parent == tmp_path / "artefacts" / ".shared-builds"
    # The executable AND its intermediate C tree land in the shared dir, so
    # the build is self-contained and a later rebuild reuses it.
    assert "-o" in calls[0]["cmd"]
    assert calls[0]["cmd"][calls[0]["cmd"].index("-o") + 1] == str(shared / "simv")
    assert f"-Mdir={shared / 'csrc'}" in calls[0]["cmd"]


def test_share_build_on_vcs_overrides_configured_output_opts(tmp_path, monkeypatch):
    """A configured -o / -Mdir must not fight the shared build's own."""
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
    # Non-output opts survive untouched.
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
    # The wrapper the execute() path invokes is the stamp-validated `simv`.
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
    """A build that cannot be *shared* can still be *reused* by the next
    process to ask for the same test — which is what lets a dispatched
    fan-out compile once in the build job instead of racing N compiles into
    one directory (#369)."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    first = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert first.compile() == 0
    assert len(calls) == 1
    # The stamp lands beside the test's compile outputs: an unshared build
    # has no directory of rtl_buddy's choosing.
    stamp = tmp_path / "artefacts" / "test_a" / vlog_sim_module.SHARED_BUILD_STAMP_NAME
    assert stamp.is_file()

    second = _make_sim(
        tmp_path, monkeypatch, test_name="test_a", exe="qrun", family="questa"
    )
    assert second.compile() == 0
    assert len(calls) == 1  # reused, not recompiled

    # ...and it is still not shared: a different test with identical inputs
    # compiles for itself.
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
    """Reuse stays opt-in: plain `rb test` compiles every time, as before."""
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
    """An absolute builder-simv pins the executable; sharing would ignore it."""
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
    """An absolute `builder-simv:` is one path shared by every test on that
    builder, while the stamp is per test. Without stamping the executable,
    test_a's stamp keeps validating after test_b overwrote the binary they
    both point at, and test_a silently simulates test_b's build (#369)."""
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
    # Nothing else has touched the binary: test_a reuses its own build.
    assert _sim("test_a").compile() == 0
    assert len(calls) == 1

    # test_b compiles a *different* configuration over the same pinned path.
    assert _sim("test_b", pd={"WIDTH": 8}).compile() == 0
    assert len(calls) == 2
    _touch(Path(pinned), "test_b's binary\n")

    # test_a must not inherit it.
    assert _sim("test_a").compile() == 0
    assert len(calls) == 3


def test_configs_pinned_to_one_absolute_simv_land_in_one_group(tmp_path, monkeypatch):
    """One executable, one group — even though the compile dirs differ.

    An absolute `builder-simv:` cannot be shared, so each test keeps its own
    compile work dir and its own stamp; what it cannot keep to itself is the
    binary, which is the one path every test on that builder writes. Group
    on the compile dirs and `compile.parallel > 1` runs two builders onto
    one output (#496 review), so the pinned path is the grouping key. The
    fix is serialization, not sharing: the second member still rebuilds.
    """
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

    # A different pinned path is a different output, so it may compile at
    # the same time — over-serializing a fleet is a real cost too.
    other = _sim("test_c", str(tmp_path / "elsewhere" / "simv"))
    assert other.compile_group_dir() != pinned

    # A relative builder-simv resolves inside the test's own compile dir,
    # which is already one writer per #369: still one group per test.
    rel_a = _sim("test_d", "simv")
    rel_b = _sim("test_e", "simv")
    assert rel_a.compile_group_dir() != rel_b.compile_group_dir()

    # The collision is not about sharing — `--no-share-build` writes the
    # same pinned path — so the grouping does not depend on it either.
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
    """`builder-simv: ../shared/simv` collides exactly like an absolute pin.

    A relative spelling is joined to each test's own compile dir, so with
    enough `..` two tests' paths meet at one suite-level file — the same
    single-output collision the absolute case has (#496 review), it just
    spells the path differently. The group is therefore the NORMALIZED
    resolved output, not the raw config value: syntactic absoluteness is a
    sharing question, never the collision predicate.
    """
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

    # artefacts/<test>/../shared/simv collapses to artefacts/shared/simv
    # for every test in the suite: one file, one group.
    sim_a = _sim("test_a", "../shared/simv")
    sim_b = _sim("test_b", "../shared/simv")
    meeting_point = sim_a.compile_group_dir()
    assert ".." not in meeting_point
    assert sim_b.compile_group_dir() == meeting_point

    # A relative path that stays inside the workspace resolves per test.
    inside_a = _sim("test_c", "sub/simv")
    inside_b = _sim("test_d", "sub/simv")
    assert inside_a.compile_group_dir() != inside_b.compile_group_dir()

    # Two spellings can also meet at one file through a symlinked parent,
    # which textual normalization cannot see: the group is the CANONICAL
    # output (`realpath`), so an aliased pin and the real one serialize.
    (tmp_path / "alias").symlink_to(tmp_path / "artefacts" / "shared")
    via_link = _sim("test_e", str(tmp_path / "alias" / "simv"))
    assert via_link.compile_group_dir() == meeting_point


def test_verilator_ignores_an_absolute_builder_simv_for_grouping(tmp_path, monkeypatch):
    """Verilator's output comes from `--Mdir`, so nothing is pinned.

    The grouping predicate is the *same* one that declines sharing, and it
    excuses verilator/icarus for the same reason: `builder-simv:` cannot
    move their output, so two such configs write two build dirs and are two
    groups. Grouping them together would serialize builds that never
    collide.
    """
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
    """A licqueue wait makes compile elapsed untrustworthy — say so (#329)."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(
        monkeypatch, calls, stdout="Queuing for License...\nParsing design\n"
    )

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe="vcs", family="vcs")
    assert sim.compile() == 0
    # The evidence is kept even though the compile succeeded.
    transcript = Path(sim._get_compile_transcript_path())
    assert transcript.is_file()
    assert "Queuing for License" in transcript.read_text()


def test_verilator_compile_never_reports_license_queue(tmp_path, monkeypatch, caplog):
    """Only VCS queues, so the marker in another family's output is text.

    Asserted on the event rather than on an absent ``compile.log``: since
    #494 every compile that runs leaves a transcript, so the file's absence
    no longer means anything.
    """
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


# --- include-dir headers in the reuse stamp (issue #303) ---------------------


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
    """The reported gap: a header reachable only through +incdir+ is not in
    the filelist, so the stamp used to stay valid across an edit to it and a
    warm run reused a simv built from the old header (#303)."""
    _write_source(tmp_path)
    header = _write_header(tmp_path)
    calls = []
    # Verilator names the header among the inputs it consumed, relative to
    # the compile cwd (the test's artefact dir).
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
    """`run.f` is rewritten on every compile, so tracking its mtime would
    make the test that built the simv rebuild it on its own next run."""
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
    """VCS and Icarus emit nothing comparable; reuse must still work there."""
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
    """A stamp with no `deps` key cannot say whether headers were tracked;
    "we do not know" must not validate a reuse."""
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
    # ...and the rebuild leaves a stamp that does say.
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
    """Gap 1 of #478: VCS and Icarus emit no dependency file, so before the
    directory listing their stamps recorded `deps: null` and a header edit
    reachable only through `+incdir+` reused a simv built from the old
    header — on the builder that usually signs a merge off."""
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
    """A header that did not exist cannot be in any dependency record, so
    only the directory listing can notice it appear."""
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
    """Gap 2 of #478: `-y` resolves by module name on demand, so a file that
    nobody consumed yet can change tomorrow's elaboration. A depfile records
    what was opened and structurally cannot name it — this must invalidate
    even for Verilator, with a `.d` present."""
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
    """`+libext+` can be set on the builder command line
    (`builder-opts.compile-time`) and never reach run.f, so a listing that
    filtered by the suffixes run.f declares would silently miss the library
    file that appears with any other one — Gap 2, still open. Everything in
    the directory is listed instead. `+libext+` itself is a suffix, not a
    path, and keeps the untracked entry shape."""
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

    # The suffix run.f never mentions: only an unfiltered listing sees it.
    _write_lib(tmp_path, "newmod.vp", "module newmod; endmodule\n")
    assert _sim("test_b").compile() == 0
    assert len(calls) == 2


def test_a_library_dir_listing_stays_flat(tmp_path, monkeypatch):
    """`-y` maps a module name to a file in the directory itself, so a
    subdirectory holds nothing the search can reach and walking it would
    charge the stamp for files no compile can see."""
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
    """Any name at all can be `include`d, so an include dir is listed
    unfiltered — and recursively, because `` `include "nested/deep.svh" ``
    resolves *beneath* the directory. A dot-*file* is ordinary input
    (`` `include ".config.svh" `` resolves and compiles); only dot
    *directories* and a denylist of editor/VCS bookkeeping are dropped."""
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
    """The counterpart to the denylist: `.config.svh` is a legal include, so
    dropping every dot name would reopen the gap this stamp closes."""
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
    """`+incdir+.` in a tests.yaml, or `+incdir+..` from a design directory
    holding verif suites, puts the suite's own `artefacts/` inside the walk.
    Everything under it — run.f, compile.log, the obj_dir, the stamp itself
    — is written AFTER the fingerprint that would list it, so a stamp that
    listed them could never validate again and every gated dispatch job
    would recompile."""
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

    # The reuse the artefact tree would otherwise have made impossible.
    assert _sim("test_b").compile() == 0
    assert len(calls) == 1
    assert _sim("test_c").compile() == 0
    assert len(calls) == 1


def test_a_generated_header_under_the_artefact_dir_is_tracked(tmp_path, monkeypatch):
    """A `preproc` hook is documented to generate headers into its
    `artifact_dir`, and the filelist then names `+incdir+artefacts/<test>/gen`.
    The walk STARTS inside the managed tree, so no `artefacts` component is
    ever seen and pruning by directory name cannot help. The generated
    header must be tracked — that is the point of the include — while
    rtl_buddy's own outputs beside it must not be, because every one of them
    is written after the fingerprint that would list it."""
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
    # ...and nothing rtl_buddy wrote into that same directory.
    for output in ("run.f", "compile.log", "result.json", "rb-compile-stamp.json"):
        assert not any(name.endswith(output) for name in listing), listing

    # The reuse those outputs would otherwise have made impossible: the
    # compile writes run.f and the stamp *after* the fingerprint is taken.
    assert _sim("test_b").compile() == 0
    assert len(calls) == 1
    assert _sim("test_c").compile() == 0
    assert len(calls) == 1

    # But the generated header itself is a real input.
    _touch(generated, "`define GW 16\n")
    assert _sim("test_d").compile() == 0
    assert len(calls) == 2


def test_rtl_buddys_own_outputs_are_never_listed(tmp_path, monkeypatch):
    """The exclusion is by name and applies wherever a listing is taken, so
    a run directory's logs and envelopes are out too. Pinned against the
    constants the writers use, so a renamed output cannot silently start
    being stamped."""
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
    """`obj_dir*` is rtl_buddy's build-directory spelling wherever it lands,
    including an unshared build dropped next to the sources."""
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
    """`` `include "nested/deep.svh" `` is an ordinary spelling and resolves
    below the include directory, so a flat listing would leave an edit to it
    invisible on a builder that reports no dependencies."""
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
    """The over-approximation stops at a denylist of names no simulator ever
    reads. A `.DS_Store` dropped by browsing the directory in Finder must
    not cost a rebuild of a whole chip."""
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
    """The listing over-approximates on purpose, but only inside the
    directories the filelist actually names."""
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
    """A directory entry gained a fifth element, so a stamp written before
    #478 is silent where a listing is now expected. Silence is not a reuse:
    one rebuild, and what it writes back is readable."""
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
    """The listing belongs on the stamp side of the line #494 drew: an edit
    inside an include dir rebuilds *in place* instead of stranding one
    obj_dir per edit."""
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

    # ...while the fingerprint sha, which asks "would the stamp match", moves.
    sha = vlog_sim_module._fingerprint_sha(fingerprint)
    assert sha != vlog_sim_module._fingerprint_sha(edited)
    assert sha != vlog_sim_module._fingerprint_sha(added)


def test_an_edit_inside_an_include_dir_rebuilds_in_place(tmp_path, monkeypatch):
    """End to end for the same property: same obj_dir, second compile."""
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
    """`compile.build_source_changed` is the answer to "why did this
    recompile", so a directory entry has to name the file inside it — for an
    edit, and for a file added or removed, which shifts every entry after it
    and used to be answered with a bare entry count."""
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
    """A `+incdir+` whose directory is gone stamps as untracked again, which
    cannot match the listing that was recorded — one rebuild, not a reuse."""
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

    # The filelist writer refuses a missing directory, so the stamp is
    # revalidated directly: this is what a compile from a stamp whose
    # include tree has since been deleted decides.
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
    """A directory that cannot be listed records the pre-#478 untracked
    entry, never an empty listing — "the directory is empty" is a claim, and
    a false one would validate a reuse on the strength of it."""
    _write_source(tmp_path)
    _write_header(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    real_scandir = os.scandir

    def _refuse(path, *args, **kwargs):
        if os.path.basename(str(path)) == "inc":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    # `os.walk` calls `scandir` too, and by default swallows a directory it
    # cannot open — which would make an unreadable include dir look empty.
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
    """Every shape this version cannot read answers "rebuild", never raises."""
    good = ["+incdir+inc", None, None, None, [["w.svh", 9, 17, "aaaa"]]]
    assert vlog_sim_module._entry_matches(good, list(good))
    for corrupt in (
        ["+incdir+inc", None, None, None, "not-a-list"],
        ["+incdir+inc", None, None, None, [["w.svh", 9, 17]]],
        ["+incdir+inc", None, None, None, ["w.svh"]],
    ):
        assert not vlog_sim_module._entry_matches(corrupt, good)
        assert not vlog_sim_module._entry_matches(good, corrupt)


# ------------- what the run itself writes into a stamped directory (#535-537)


def _incdir_dot_sim(tmp_path, monkeypatch, test_name, *, family="vcs"):
    """A sim whose filelist puts the working directory itself on the include
    path — `+incdir+.` in a tests.yaml, the shape all three reports share."""
    return _make_sim(
        tmp_path,
        monkeypatch,
        test_name=test_name,
        exe=family,
        family=family,
        filelist=["src/top.sv", "+incdir+."],
    )


def test_the_suite_log_is_never_listed_in_an_include_directory(tmp_path, monkeypatch):
    """rtl_buddy's own log lands in the SUITE directory, not under
    `artefacts/`, so the directory prune never reaches it. The head appends
    to it once a minute for the whole life of a dispatched run — through
    every gated job's stamp check (#537)."""
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


def test_a_pycache_beside_a_preproc_helper_is_never_listed(tmp_path, monkeypatch):
    """CPython writes bytecode beside a helper module a `preproc` hook
    imports out of the suite directory, during the very phase that computes
    the fingerprint (#537)."""
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


def test_a_regenerated_file_the_build_never_read_does_not_invalidate(
    tmp_path, monkeypatch
):
    """A dependency file names every input the verilation opened, so a file
    it never opened cannot have changed the binary. A per-test `preproc`
    regenerating its program directory under a stamped `+incdir+` is exactly
    that file, and hashing it out of the listing is what made a build job
    compile one key once per test (#535/#536)."""
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
    """The narrowing must not reach a file the build actually consumed: that
    one is in `deps`, where content still decides (#303)."""
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
    """What a dependency file structurally cannot see: a name that was not
    there when the build ran, and could shadow one that was. The listing
    keeps deciding that half (#478 gap 2)."""
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
    """The build job's own shape (#535): PRE and the compile-key probe run
    for every config before the first compile, so a per-test `preproc`
    writing under a stamped `+incdir+` moves the inputs between one member's
    fingerprint and the stamp it is meant to reuse. Members of one group
    compile once."""
    _write_source(tmp_path)
    for name in ("test_a", "test_b"):
        prog = tmp_path / f"prog_{name}"
        prog.mkdir()
        (prog / "data.txt").write_text("first run\n")
    calls = []
    _install_fake_builder(monkeypatch, calls, depends=["../../src/top.sv"])

    sim_a = _incdir_dot_sim(tmp_path, monkeypatch, "test_a", family="verilator")
    sim_b = _incdir_dot_sim(tmp_path, monkeypatch, "test_b", family="verilator")

    # PRE, then the probe, one config at a time — then the compiles.
    _touch(tmp_path / "prog_test_a" / "data.txt", "second run\n")
    group_a = sim_a.compile_group_dir()
    _touch(tmp_path / "prog_test_b" / "data.txt", "second run\n")
    group_b = sim_b.compile_group_dir()
    assert group_a == group_b  # one compile key, so one group

    assert sim_a.compile() == 0
    assert sim_b.compile() == 0
    assert len(calls) == 1
    assert sim_b.last_compile["reused"] is True


def test_a_gated_retry_says_what_drifted(tmp_path, monkeypatch, caplog):
    """A dispatched job logs at INFO and the stamp check's own diagnostics
    are DEBUG, so the one line a reader of an OOM-killed sim job gets has to
    name the file (#536)."""
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
    """`--MP` appends one bare `<prerequisite>:` rule per dependency so make
    does not fail on a deleted include. Those are targets; collecting them
    would stamp a shadow entry per real dep, each ending in a colon and so
    resolving to a path that never exists. Shape copied from a real
    `V<prefix>__ver.d` (Verilator 5.048, `--cc --MP`)."""
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
    """End to end: the tail must not double the stamp."""
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
    """Never mistake a target list for an input list."""
    assert (
        vlog_sim_module.parse_depend_prerequisites("obj/Vtop.cpp obj/Vtop.mk\n") == []
    )


# --- content-hashed stamps (issue #494) --------------------------------------


def _edit_behind_a_stale_stat(path, text):
    """Rewrite ``path``'s content while its size and mtime stay put.

    This is what the reported failure looks like from the validating side.
    The edit really happened, seconds ago, on the submit host; the compute
    node that revalidates the stamp asks NFS for the file's attributes and
    is served the *cached* pre-edit answer, so `stat` reports the size and
    mtime the build recorded. Freezing both here reproduces that
    deterministically, without a cluster: if size and mtime are all the
    stamp compares, the edited design is reused and reports PASS.
    """
    stat = os.stat(path)
    assert len(text.encode()) == stat.st_size, "an equal-size edit is the repro"
    path.write_text(text)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _as_a_fresh_process():
    """Drop the per-process content-hash memo.

    The memo is keyed on (path, size, mtime_ns), so within one process a
    file whose stats never moved is hashed once — which is the whole point
    of it, and which the equal-stat edits above would otherwise defeat.
    The run that reuses a stale build is a *different* process (a later
    `rb test`, or a sim job on another node), and this is that boundary.
    """
    vlog_sim_module._CONTENT_HASH_CACHE.clear()


def test_an_edit_hidden_by_an_unchanged_stat_still_invalidates_the_stamp(
    tmp_path, monkeypatch
):
    """The reported bug: a stale PASS on a design that was never simulated.

    A source whose recorded size and mtime still describe it exactly must
    not validate the build when its bytes have changed (#494).
    """
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
    # In place, as ever: an edit rebuilds the dir it had, it does not strand
    # a new one per edit.
    assert sim_b._get_simv_path() == sim_a._get_simv_path()


def test_an_edited_header_hidden_by_an_unchanged_stat_invalidates_the_stamp(
    tmp_path, monkeypatch
):
    """The same, on the deps side: a header reached only through +incdir+."""
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
    """The other side of hashing: content decides, so a moved mtime alone
    is not a change. A `git checkout` that restores the same bytes, a
    `touch`, or a regenerated file that came out identical used to cost a
    full rebuild each; now the stamp still validates."""
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
    """Verilator names its own std includes among the inputs it consumed.

    Those are not the project's files and hashing an install per validation
    buys nothing, so they keep the old stat comparison — which means a
    moved mtime alone still invalidates there.
    """
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

    # With no hash to decide, stats do — as they always did.
    _touch(outside, outside.read_text())

    sim_b = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    assert sim_b.compile() == 0
    assert len(calls) == 2


def test_a_stamp_written_before_content_hashing_rebuilds_once(tmp_path, monkeypatch):
    """Entries gained a fourth element, so every stamp an older rtl_buddy
    wrote is a shape this version cannot read. "We do not know" is not a
    reuse: it rebuilds, once, and what it writes back is readable."""
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
    """The hash lives in the stamp, never in the key.

    If it leaked into the key an edit would name a *different* obj_dir,
    stranding one build tree per edit instead of rebuilding in place — so
    the key is pinned here against the exact input set, and the entry shape
    change of #494 has to leave it alone.
    """
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
    """A suite validating N stamps over one source set reads each file once.

    Content hashing is only affordable because of the memo: without it a
    fifty-test suite re-reads every source fifty times per run.
    """
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
    """The scope that makes this fix work is the PROJECT root, not the suite.

    The reporter's layout is the ordinary one: the suite lives at
    ``verif/<blk>/`` and owns ``artefacts/.shared-builds``, while the RTL it
    compiles lives *above* it. Scoped to the suite dir, every one of those
    sources falls outside the hashing policy and stays stat-only — which is
    #494, still open, for exactly the projects that reported it. So this
    test builds from a source outside the suite and asserts both halves:
    the stamp carries a hash for it, and an edit a stale ``stat`` would hide
    still invalidates the build.
    """
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
    """ "Under the project root" is the implementation; "the project's files,
    not the toolchain's" is the policy. A vendored install puts the two in
    tension: ``verilator_bin`` and ``verilated.h`` land *inside* the root and
    would be content-hashed, which is tens of megabytes read once per process
    per node — the exact cost the policy exists to avoid.
    """
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
    """The exclusion is only taken when it is a *proper* subdirectory.

    A project that keeps its simulator in ``<root>/bin`` would otherwise
    derive ``<root>`` as the install prefix and exclude the entire design —
    turning the fix off everywhere, silently, for the projects most likely
    to vendor a toolchain in the first place.
    """
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
    """Symlinking a shared IP or RTL tree into the checkout is an ordinary
    hardware-repo layout, and it is served off the same NFS mount #494 is
    about. Deciding containment on where the name *resolves* would leave
    exactly those files stat-only — the fix off for the case it exists
    for — so the name ``run.f`` declares counts too.
    """
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


def test_a_symlinked_in_dependency_is_judged_where_it_resolves(tmp_path, monkeypatch):
    """The boundary of the rule above, stated so it is a decision and not a
    surprise: the dependency list is keyed on realpaths — that is what makes
    both sides of the comparison and the ``run.f`` exclusion agree — so an
    entry that only ever appears there carries no declared path to consult.
    A header reached through ``+incdir+`` from a symlinked-in tree is
    therefore stat-only, while that same tree's sources, which ``run.f``
    names, are hashed by the test above.
    """
    external = tmp_path.parent / f"{tmp_path.name}-inc"
    external.mkdir(exist_ok=True)
    header = external / "w.svh"
    header.write_text("`define W 8\n")
    (tmp_path / "inc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "inc" / "w.svh").symlink_to(header)
    _write_source(tmp_path)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")

    assert sim._tracked_entry(os.path.realpath(header), resolved=True)[3] is None


def test_an_oversized_input_stays_stat_only_and_says_which(
    tmp_path, monkeypatch, caplog
):
    """The hashing policy is locational, so a memory-init ``.hex`` or a
    vendored blob named in ``run.f`` qualifies exactly as a ``.sv`` does —
    and would be read in full on every validation, on every node. Over the
    cap it keeps the old stat comparison, and says so once so that "why was
    this not hashed" has an answer in the log.
    """
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
    """A stamp is data from another machine and possibly another version.

    Under dispatch the stamp is written on whichever node built and read on
    whichever node reuses, and a mixed-version cluster (submit host upgraded,
    compute nodes not) is the ordinary way the two disagree. Every shape
    this version cannot read has to answer "rebuild" — not raise, which
    under the build job's exit-0 contract would be a failed build instead of
    a slow one.
    """
    _write_source(tmp_path)
    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")

    def _refuse(path, **kwargs):
        raise AssertionError(f"an unreadable stamp shape reached os.stat: {path!r}")

    # The guards have to decide *before* anything is stat'd — the point of
    # rejecting an int path is that `os.stat` would take it for a file
    # *descriptor* and answer about whatever unrelated file is open on it,
    # so "returns False anyway" is not the property being asserted here.
    monkeypatch.setattr(vlog_sim_module, "_hashed_stat_entry", _refuse)

    # Entries from before content hashing: three elements, no hash.
    assert sim._deps_unchanged("test_a", [["/x", 1, 2]]) is False
    assert sim._deps_unchanged("test_a", [[5, 1, 2, "abcd"]]) is False
    assert sim._deps_unchanged("test_a", ["not-an-entry"]) is False
    # `deps` itself in a container this version was never taught.
    assert sim._deps_unchanged("test_a", 5) is False
    assert sim._deps_unchanged("test_a", {"/x": [1, 2, "abcd"]}) is False
    assert sim._deps_unchanged("test_a", None) is False


def test_nothing_but_a_regular_file_is_ever_opened_for_hashing(tmp_path, monkeypatch):
    """Stats decide *whether* to read, before anything is opened.

    ``_collect_build_deps`` records whatever the builder's ``.d`` names, and
    it does not gate on ``isfile`` the way the filelist fingerprint does. A
    directory there is ordinary and merely raises on open; a FIFO is the case
    that decides the shape of the guard, because opening one blocks forever —
    a build job that never returns rather than one that fails closed. The
    ``st_mode`` is already in hand, so the question is asked before the open.
    """
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
    """A subclass-injected -o must not outrank the shared build's own.

    _get_extra_compile_flags() is appended AFTER the shared-build output argv,
    so an unfiltered -o there would win on VCS's duplicate-option precedence:
    the simv lands outside the shared dir, the stamp check never finds it, and
    every job recompiles silently and forever.
    """
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
    # The build really did land where the stamp validates it.
    assert (shared / "simv").is_file()
    assert sim._shared_build_is_valid(shared, None) is False  # wrong fingerprint
    assert Path(sim._get_simv_path()).is_file()


def test_icarus_wrapper_args_separate_the_compile_key(tmp_path, monkeypatch):
    """The shared `simv` wrapper bakes in _icarus_vvp_extra_args() (#358).

    CocotbSim adds the VPI module there while contributing no Icarus compile
    flags, so two tests differing only in those args would otherwise share one
    key and one wrapper — whichever compiled first deciding how vvp is invoked
    for both.
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
    # Different wrapper contents -> different build, so both compiled.
    assert len(calls) == 2
    assert plain._get_simv_path() != vpi._get_simv_path()
    assert "libcocotbvpi" in Path(vpi._get_simv_path()).read_text()
    assert "libcocotbvpi" not in Path(plain._get_simv_path()).read_text()


def test_relative_builder_simv_override_is_logged(tmp_path, monkeypatch, caplog):
    """The shared build discards a relative builder-simv; don't do it silently."""
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
    """The build-job gate *orders* the elements; it does not exclude them.

    If the stamp that build left fails to validate, every element compiles
    into the same directory at once — #369 resurrected — and the resulting
    `Compile failed` reads as a design error. This WARNING is the only thing
    that says otherwise (#369 review).
    """
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
    """The normal path must not warn, or the signal is worthless."""
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


def test_clear_retry_transcripts_unlinks_every_named_run(tmp_path, monkeypatch):
    """`run_multiple`'s one compile serves runs 1..N (#498 review).

    The per-run cleanup in pre()/compile() reaches only the sim's own
    run_id, so a local rerun after a dispatched fan-out relies on this to
    stop runs 2..N advertising the dispatch's retry transcripts.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=1)
    stale = []
    for run_id in (1, 2, 3):
        p = Path(sim._get_artifact_dir(run_id=run_id)) / "compile.retry.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("%Error: an old dispatch's retry\n")
        stale.append(p)

    sim.clear_retry_transcripts([1, 2, 3])
    assert not any(p.exists() for p in stale)


def test_a_stale_retry_log_is_cleared_before_a_failing_pre(tmp_path, monkeypatch):
    """A PRE failure must not resurrect the last invocation's retry (#498 review).

    A reused run directory whose next invocation dies in `preproc` never
    reaches compile(), so the cleanup there cannot run — the fresh
    SetupFail envelope would be paired with the previous invocation's
    `compile.retry.log` by the results overlay.
    """
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
    """`compile.retry.log` describes exactly one run's retry (#498 review).

    Left behind, a later run that reused the build (or never retried at
    all) would keep advertising the old transcript through `rb graph
    results`' existence check, implying this run retried compilation.
    """
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


# ------------------------------- a gated job vs. a failed build (#498)

# What the build job left in artefacts/<test>/compile.log. Every test below
# asserts it byte-for-byte afterwards: the whole bug was a sim-side retry
# writing its own `%Error: Verilator threw signal 9` over this text, so the
# only visible failure became an OOM that read as a resource problem.
_BUILD_TRANSCRIPT = (
    "Command: verilator --Mdir obj_dir -f run.f\n\n"
    "=== stderr ===\n"
    "%Error: src/top.sv:3:7: Signal is not driven: 'q'\n"
    "%Error: Exiting due to 1 error(s)\n"
    "\n=== stdout ===\n"
)


def _seed_build_transcript(sim):
    """Put the build job's compile.log where this sim would look for it."""
    path = Path(sim._get_compile_transcript_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BUILD_TRANSCRIPT)
    return path


def _write_build_envelope(tmp_path, *, failed, builds=None):
    from rtl_buddy.runner.result_io import write_build_result_json

    return write_build_result_json(
        tmp_path / "artefacts" / ".dispatch" / "build-result-1.json",
        built=[name for name in ("test_a",) if name not in failed],
        failed=failed,
        builds=builds,
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
    """A deterministic compile error will not pass on retry (#498).

    Retrying it in the sim job runs the same elaboration under the *sim*
    reservation, so a big design is OOM-killed and writes `signal 9` over
    the build job's real error. Refuse the retry, report the build's exit
    status and error lines, and leave that transcript alone.
    """
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
    # The real error reaches the summary row, in one line.
    desc = sim.compile_fail_desc
    assert "Signal is not driven" in desc
    assert "(exit 1)" in desc
    assert str(compile_log) in desc
    assert "\n" not in desc
    # ...and the retry WARNING must not fire: nothing is compiling here, so
    # the "every sibling is compiling into one dir" advice would be wrong.
    assert _events(caplog, "compile.prebuilt_stamp_invalid") == []
    assert _events(caplog, "compile.build_job_failed")[0]["returncode"] == 1


def test_a_gated_job_retries_a_failure_recorded_without_compiler_evidence(
    tmp_path, monkeypatch, caplog
):
    """`failed` alone is not a compile verdict (#498 review).

    The envelope's `failed` list also carries PRE/setup failures, filelist
    errors and worker exceptions, and a sim job re-runs its own preproc —
    so a transient setup failure on the build side can pass here, and
    suppressing the retry would turn that run into a false CompileFail.
    Only a per-build record with a `returncode` — a builder that genuinely
    ran and exited non-zero — is deterministic enough to stop the retry.
    """
    import logging as _logging

    cases = (
        # An older build job: listed as failed, no builds records at all.
        ("no builds record", {"failed": ["test_a"]}),
        # A worker exception / setup failure: a record, but no returncode
        # because no builder ever ran.
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
        # A distinct define per case, so no case short-circuits on the
        # shared build stamp an earlier one left.
        sim = _make_sim(tmp_path, monkeypatch, test_name="test_a", pd={"CASE": case_i})
        compile_log = _seed_build_transcript(sim)
        sim.expect_prebuilt = True
        sim.build_result_json = _write_build_envelope(tmp_path, **shape)

        caplog.clear()
        with caplog.at_level(_logging.DEBUG):
            assert sim.compile() == 0, label  # the retry ran, and passed

        assert len(calls) == 1, label
        assert _events(caplog, "compile.prebuilt_stamp_invalid"), label
        assert _events(caplog, "compile.build_job_failed") == [], label
        # The build job's transcript is untouched by the successful retry.
        assert compile_log.read_text() == _BUILD_TRANSCRIPT, label
        assert sim.compile_fail_desc is None, label


def test_a_no_evidence_retry_that_fails_writes_the_retry_log(
    tmp_path, monkeypatch, caplog
):
    """The evidence-less retry is a gated retry like any other (#498 review).

    Its transcript goes to `compile.retry.log` beside the build job's
    `compile.log`, never over it — and its failure is the sim job's own
    story (the generic desc), not the build job's verdict.
    """
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

    assert len(calls) == 1  # the retry really ran
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    retry_log = compile_log.parent / "compile.retry.log"
    assert retry_log.is_file()
    assert _events(caplog, "compile.prebuilt_stamp_invalid")
    assert sim.compile_fail_desc is None


def test_a_siblings_retry_log_survives_another_runs_compile(tmp_path, monkeypatch):
    """Fan-out siblings share the test dir; retry logs are per run (#498 r6).

    Run 1's gated retry fails and leaves `run-0001/compile.retry.log` — the
    only diagnostic of what ITS recompile hit. Run 2 then enters compile();
    its self-cleanup unlink must target run 2's own retry log, never run 1's
    evidence, and run 2's own retry writes only inside run 2's directory.

    Run 2's retry passes and still leaves a transcript, in its own
    `run-0002/compile.retry.log`: since #494 a compile that RAN always
    records what it ran, so the presence of a transcript cannot come to mean
    "nothing compiled". #498 only redirects WHICH file that is, so the
    build's `compile.log` survives untouched either way.
    """
    _write_source(tmp_path)

    # Run 1: an evidence-less failure record earns the retry, which fails.
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
    # The test-scoped retry name is gone: nothing writes it any more.
    assert not (compile_log.parent / "compile.retry.log").exists()

    # Run 2, same test artefact dir: retries too, and passes.
    calls2 = []
    _install_fake_builder(monkeypatch, calls2, stdout="run 2 recompiled\n")
    run2 = _make_sim(tmp_path, monkeypatch, test_name="test_a", run_id=2)
    run2.expect_prebuilt = True
    run2.build_result_json = run1.build_result_json
    assert run2.compile() == 0
    assert len(calls2) == 1

    # Run 1's diagnostic survives, byte for byte; run 2's own retry recorded
    # itself in run 2's directory and nowhere else.
    assert run1_retry.read_text() == run1_evidence
    run2_retry = Path(run2._get_artifact_dir(run_id=2)) / "compile.retry.log"
    assert run2_retry.is_file()
    assert "run 2 recompiled" in run2_retry.read_text()
    assert "run 2 recompiled" not in run1_evidence
    assert not (compile_log.parent / "compile.retry.log").exists()
    # And the build job's own transcript was never touched either.
    assert compile_log.read_text() == _BUILD_TRANSCRIPT


def test_the_no_retry_verdict_holds_only_for_the_same_inputs(
    tmp_path, monkeypatch, caplog
):
    """A recorded failure of a *different* compile earns the retry (#498 review).

    The sim job's PRE has re-run and its fingerprint is recomputed; if the
    sources, flags or toolchain moved since the build job's compile failed,
    the new inputs might pass, and suppressing the recompile would report a
    CompileFail nobody has run. The record's `fingerprint_sha` is compared
    against the sim's own; only a match (or an older record without one)
    keeps the verdict.
    """
    import logging as _logging

    from rtl_buddy.tools.vlog_sim import _fingerprint_sha

    _write_source(tmp_path)

    # --- the build job's side: a real failing compile records the sha.
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)
    build_sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert build_sim.compile() == 1
    recorded = build_sim.last_compile_failure
    assert recorded["fingerprint_sha"]

    # --- same inputs: the gated job honours the verdict and does not retry.
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
    # The sim's own hash really is the same helper over the same shape.
    assert _events(caplog, "compile.build_failure_inputs_changed") == []
    assert _fingerprint_sha(None) is None

    # --- the inputs moved: the same record with a different sha retries.
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
    """`_fingerprint_sha` means what `_entry_lists_match` means (#494 + #498).

    The no-retry verdict compares two hashes of a fingerprint, and the
    stamp compares the same fingerprint entry-wise — where a content hash
    OUTVOTES a moved `mtime_ns` (#494). Hashing the raw entries would put
    the two into disagreement in the one direction that costs a run: a
    `touch`, or a regenerated file with identical bytes, would move the sha,
    the gated job would call the inputs "changed", and it would recompile a
    deterministic failure under the sim reservation — exactly what #498
    exists to stop. An edited byte must still move both.
    """
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

    # Same bytes, new mtime: the stamp still validates, so the sha must not
    # move either.
    os.utime(src, (0, 0))
    touched = fingerprint()
    assert touched["sources"] != before["sources"]  # the mtimes really moved
    assert _entry_lists_match(before["sources"], touched["sources"])
    assert _fingerprint_sha(touched) == _fingerprint_sha(before)

    # A real edit moves both, in step.
    src.write_text("module top; wire q; endmodule\n")
    edited = fingerprint()
    assert not _entry_lists_match(before["sources"], edited["sources"])
    assert _fingerprint_sha(edited) != _fingerprint_sha(before)


def test_a_gated_retry_writes_beside_the_build_log_never_over_it(
    tmp_path, monkeypatch, caplog
):
    """A stamp invalid for some *other* reason still earns its retry (#498).

    Toolchain drift, a clock skew, a config the build job never reached:
    the recompile is right. What it may not do is truncate the build job's
    compile.log, so its transcript goes to compile.retry.log — and the
    compile.failed event names whichever file was actually written, because
    that is what every reader is pointed at.
    """
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    compile_log = _seed_build_transcript(sim)
    sim.expect_prebuilt = True
    # The build job says this test BUILT; the stamp simply did not validate.
    sim.build_result_json = _write_build_envelope(tmp_path, failed=[])

    with caplog.at_level(_logging.DEBUG):
        assert sim.compile() == 1

    assert len(calls) == 1  # the retry really ran
    assert compile_log.read_text() == _BUILD_TRANSCRIPT
    retry_log = compile_log.parent / "compile.retry.log"
    assert retry_log.is_file()
    assert "Command: " in retry_log.read_text()
    assert _events(caplog, "compile.prebuilt_stamp_invalid")
    assert _events(caplog, "compile.failed")[0]["transcript"] == str(retry_log)
    # No build-side verdict to report, so the generic desc still applies.
    assert sim.compile_fail_desc is None


def test_a_gated_retry_falls_back_to_todays_behaviour_without_an_envelope(
    tmp_path, monkeypatch, caplog
):
    """Missing, corrupt, or simply not passed: retry, exactly as before.

    Declining to compile on a guess would turn an unreadable file into a
    lost run, so only an envelope that positively names this test as failed
    stops the retry.
    """
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
        # A distinct define per case, so no case short-circuits on the
        # shared build stamp an earlier one left.
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
        # The retry's own transcript goes to compile.retry.log — since #494
        # a compile that ran records itself even when it passed — so the
        # build job's compile.log stays exactly as it was.
        assert compile_log.read_text() == _BUILD_TRANSCRIPT, label
        assert (
            Path(sim._get_artifact_dir(run_id=sim.run_id)) / "compile.retry.log"
        ).is_file(), label


def test_an_ungated_compile_still_writes_compile_log(tmp_path, monkeypatch):
    """The `.retry.` name is for gated retries and nothing else (#498).

    Every local run, and every dispatched job with no build job behind it,
    must keep writing the file the docs, `rb graph results` and a decade of
    muscle memory look for.
    """
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, returncode=1)

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 1

    transcript = Path(sim._get_compile_transcript_path())
    assert transcript.name == "compile.log"
    assert transcript.is_file()
    assert not (transcript.parent / "compile.retry.log").exists()
    # And the record a dispatched build job would put in its envelope —
    # including the identity of the inputs this compile failed on, which
    # is what lets a gated sim job tell "same compile" from "inputs moved
    # since" (#498 review).
    failure = sim.last_compile_failure
    assert failure["returncode"] == 1
    assert failure["transcript"] == str(transcript)
    sha = failure["fingerprint_sha"]
    assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")


def test_a_gated_build_failure_becomes_the_compile_fail_desc(tmp_path, monkeypatch):
    """The desc reaches the run summary through TestRunner, not just VlogSim.

    `_compile_outcome` maps a non-zero compile to CompileFailResults, and
    that mapping is where a bare "Compile failed" used to erase everything
    the sim had just learned (#498).
    """
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
    # The runner's own view of the failure, which the build job records.
    assert runner.last_compile_failure["returncode"] == 2
    assert calls == []


def test_share_build_unsupported_reason_is_the_predicate_the_head_uses():
    """The head plans reservations and gating from this, and the job takes
    the unshared path from it. Family alone is not enough: an absolute
    `builder-simv:` declines sharing too, and a head consulting only the
    family would plan such a builder as shareable (#369 review)."""
    reason = vlog_sim_module.share_build_unsupported_reason

    assert reason(DummyBuilderCfg(simulator_family="verilator")) is None
    assert reason(DummyBuilderCfg(simulator_family="vcs")) is None
    assert "no shared-build support" in reason(
        DummyBuilderCfg(simulator_family="questa")
    )
    # The case the two predicates used to disagree on.
    assert "builder-simv is an absolute path" in reason(
        DummyBuilderCfg(simulator_family="vcs", simv="/pinned/simv")
    )
    # Verilator and Icarus are redirected wholesale, so a pinned simv is
    # overridden rather than honoured, and sharing still applies.
    assert (
        reason(DummyBuilderCfg(simulator_family="verilator", simv="/pinned/simv"))
        is None
    )


# --- toolchain identity (INF-22) -------------------------------------------
#
# The stamp used to record the *configured* builder name ("verilator"), which
# is the same string whichever install PATH resolves it to. So pointing the
# project at a different simulator left every stamp validating, the compile
# short-circuited, and the run reported PASS on a binary the new toolchain
# never produced. Dispatch implies --share-build, so a dispatched toolchain
# A/B reported green on both sides while the same regression run locally
# (compiling per test) failed correctly.


def _fake_toolchain(tmp_path, name, version, binary="verilator"):
    """An executable that answers `--version` / `-V` and nothing else."""
    exe = tmp_path / name / binary
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f'#!/bin/sh\necho "{version}"\n')
    exe.chmod(0o755)
    return exe


def test_share_build_keeps_a_separate_build_per_toolchain(tmp_path, monkeypatch):
    """Two installs, two build dirs — which is what an A/B wants: neither
    side overwrites the other's simv, so both stay runnable."""
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

    # And a third test back on the first toolchain reuses that one's build.
    sim_c = _make_sim(tmp_path, monkeypatch, test_name="test_c", exe=str(old))
    assert sim_c.compile() == 0
    assert len(calls) == 2
    assert sim_c._get_simv_path() == sim_a._get_simv_path()


def test_share_build_rebuilds_when_one_install_is_upgraded_in_place(
    tmp_path, monkeypatch, caplog
):
    """Same path, new binary behind it: rebuild in the same dir (no
    directory per version) and say why, because nothing else would."""
    import logging as _logging

    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)
    exe = _fake_toolchain(tmp_path, "tc", "Verilator 5.048 2024-01-01")

    sim_a = _make_sim(tmp_path, monkeypatch, test_name="test_a", exe=str(exe))
    assert sim_a.compile() == 0
    assert len(calls) == 1

    # Upgrade the install the project points at. The mtime bump is explicit
    # so the version probe cannot answer from its (path, mtime) cache.
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
    """`bin/verilator` is a script that execs `verilator_bin`; it can be
    byte-identical and same-mtime across an upgrade of the binary behind it.
    The version banner is the only entry that notices, so hold size and
    mtime fixed and prove it does."""
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

    # Same length, same mtime -- only the banner moves.
    exe.write_text('#!/bin/sh\necho "Verilator 5.049 bbbb"\n')
    exe.chmod(0o755)
    os.utime(exe, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(exe).st_size == before.st_size
    assert os.stat(exe).st_mtime_ns == before.st_mtime_ns
    # The probe memoises on (path, mtime), which is exactly what did not
    # change, so the cache has to be stepped around for the banner to be
    # re-read at all -- as a fresh process would.
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
    """`compile skipped` on its own does not say which compiler's output is
    about to be simulated, which is the whole INF-22 complaint."""
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
    """The unshared stamp (a family rtl_buddy cannot redirect) reuses the
    same fingerprint, so it was fooled the same way and is fixed with it."""
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
    """A simulator whose banner cannot be read still gets built; it only
    costs the stamp the ability to notice an in-place upgrade."""
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
    """`which` miss must not raise here — the compile below reports it far
    better than this fingerprint could."""
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
    """An rtl_buddy upgrade is not a toolchain change. The rebuild is
    unavoidable (the entry is new); crying 'toolchain changed' about it
    would train people to ignore the message that matters."""
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


# --- the compile-key probe (#495) ------------------------------------------
#
# The dispatched build job groups its configs by the directory each compile
# will write, then compiles the groups concurrently. The grouping value has
# to be the one the compile itself uses, so these pin that it is derived in
# exactly one place and asked for without compiling.


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
    """An unshared build's output stays in its per-test workspace: own group.

    #369 already guarantees a single writer there, which is what makes it
    safe for every unshared config to compile at once. The group is the
    resolved OUTPUT path (not the directory), so a `builder-simv:` that
    lands two tests on one file serializes them — see the pinned/escaping
    tests above.
    """
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
    """Probe then compile is ONE derivation, not two.

    Two derivations is how the group key and the build dir drift apart, and
    the symptom of that drift is two builders in one directory. Counting
    ``_write_filelist`` counts derivations: it is the plan's side effect.
    """
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
    """The cached plan serves one compile, not the instance's lifetime.

    A source edited between two compiles has to invalidate the stamp, and
    it can only do that if the second compile re-stats its inputs.
    """
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


# ---------------------------------------------- visible reuse + --rebuild (#494)


def _compile_log_of(sim):
    return Path(sim._get_compile_transcript_path())


def _console_events(monkeypatch):
    """Collect the events that go out through ``log_console_event``.

    The channel is the point, not the record: ``caplog`` captures
    ``log_event`` and ``log_console_event`` identically, so a test that
    only reads ``caplog`` cannot tell that a line survives a console
    handler sitting at WARNING — which is the whole of #494's "a stale
    reuse must be visible at default verbosity". Forwards to the real
    function so the record is emitted as usual.
    """
    seen = []
    real = vlog_sim_module.log_console_event

    def _spy(spy_logger, level, event, **fields):
        seen.append(event)
        return real(spy_logger, level, event, **fields)

    monkeypatch.setattr(vlog_sim_module, "log_console_event", _spy)
    return seen


def _logged_events(monkeypatch):
    """Collect the events that go out through ``log_event``.

    A spy rather than ``caplog`` for the reason above turned inside out:
    the first ``log_console_event`` of a pytest process detaches pytest's
    capture handler, so what a test sees in ``caplog`` depends on which
    tests ran before it. The call is the observable either way.
    """
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
    """A stale reuse used to be deducible only from an *absent*
    ``compile.log``, which reads as "nothing to do" (#494).

    So the reuse names the directory and how old its stamp is, and it goes
    out through ``log_console_event``: the console handler sits at WARNING,
    and a dispatched run's job log is the only artifact a stale PASS can be
    caught in.
    """
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
    # Through the console channel, not merely into the log file: at default
    # verbosity the handler is at WARNING, and a dispatched run's job log is
    # the only artifact a stale PASS can be caught in.
    assert console == ["compile.build_reused"]

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "compile.build_reused"
    )
    shared_dir = Path(writer._get_simv_path()).parent
    # The basename, because that is what a reader compares against
    # `ls artefacts/.shared-builds/`; the absolute path rides alongside.
    assert record.rtl_fields["build_dir"] == shared_dir.name
    assert record.rtl_fields["build_path"] == str(shared_dir)
    assert record.rtl_fields["stamp_age_sec"] >= 0
    assert record.rtl_fields["toolchain"] == "Verilator 5.049 devel rev vBBBB"
    # The rendered line, not just the fields: it is what a human reads.
    assert shared_dir.name in record.getMessage()
    assert "ago" in record.getMessage()


def test_a_reuse_leaves_a_compile_log_naming_what_it_reused(tmp_path, monkeypatch):
    """ "The absence of compile.log reads as nothing to do" (#494).

    So a skipped compile writes the same transcript a real one does, saying
    which directory was reused, when its stamp was written, and the command
    a rebuild would have run.
    """
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
    # The command that WOULD have run, derived from the same assembly the
    # real compile uses — so it names this build's --Mdir, not a guess.
    assert f"--Mdir {shared_dir}" in text
    assert "--rebuild" in text


def test_a_reuse_over_a_non_utf8_transcript_degrades_instead_of_raising(
    tmp_path, monkeypatch
):
    """A carried transcript owes nobody valid UTF-8 (#494 review).

    Real compile output is raw simulator bytes; a breadcrumb helper that
    read it strictly would raise UnicodeDecodeError on the exit-0 path of
    a build job. The reuse must still write its breadcrumb, carrying the
    old transcript with undecodable bytes replaced.
    """
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
    """Since a reuse writes ``compile.log``, a silent success would make the
    file's *presence* mean "nothing compiled" — the inverse of what
    docs/concepts/tests.md says it is (#494)."""
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
    """The compile transcript is written on the SUCCESS path since #494, so
    it has to degrade the way the reuse breadcrumb already does: a builder
    that exited 0 must not become a failed compile — a failed row in the
    build job, a traceback in-process — because its breadcrumb could not be
    written."""
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
    """Under dispatch the build job's compile and then every gated element's
    reuse write this one path in turn, and that first write is the run's only
    file-level record of, say, a VCS ``-licqueue`` wait — so the breadcrumb
    carries it rather than dropping it (#494)."""
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

    # And a reuse over a reuse carries that transcript forward instead of
    # nesting breadcrumbs, so N elements leave one file of bounded size.
    second_reuse = _vcs("test_a")
    assert second_reuse.compile() == 0
    text = _compile_log_of(second_reuse).read_text()
    assert text.count("Compile skipped") == 1
    assert "Queuing for License" in text


def test_an_unshareable_builders_reuse_also_leaves_a_breadcrumb(
    tmp_path, monkeypatch, caplog
):
    """The per-test-stamp reuse is the branch a dispatched fan-out takes for
    every element, so it is the one most likely to hide a stale build."""
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
    # Where the build is, not the test name a second time: an unshared
    # build's directory IS `artefacts/<test>`, so its basename is the word
    # the line already opens with and only the path identifies it.
    build_dir = Path(second._get_compile_work_dir())
    assert record.rtl_fields["build_path"] == str(build_dir)
    assert record.rtl_fields["build_dir"] == build_dir.name
    assert str(build_dir) in record.getMessage()
    assert "Compile skipped" in _compile_log_of(second).read_text()


def test_rebuild_recompiles_over_a_warm_valid_stamp(tmp_path, monkeypatch):
    """The escape hatch the issue asks for: dropping ``--share-build`` does
    not stop the reuse, so there has to be something that does (#494)."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert len(calls) == 1
    # Same key, warm stamp: without --rebuild this is the reuse proven above.
    assert (
        _make_sim(tmp_path, monkeypatch, test_name="test_b", rebuild=True).compile()
        == 0
    )
    assert len(calls) == 2


def test_rebuild_forces_one_rebuild_per_build_dir_per_process(tmp_path, monkeypatch):
    """One user request is one rebuild of the shared directory, not one per
    test: N builders into one directory is #369 with extra steps.

    The first test through claims the directory and rebuilds; the rest
    validate the stamp that rebuild just wrote and reuse it.
    """
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
    """Two spellings of one directory are one build, so the claim is
    ``realpath``'d — the same reason the compile grouping is (#495).

    Exercised with a symlinked suite dir, because that is the spelling
    textual normalization cannot see through: the compile key excludes the
    suite path, so both sims land in one ``obj_dir_<key>`` and a claim keyed
    on the string would force a second rebuild of it.

    The claim is read off ``compile.rebuild_forced``, which fires exactly
    when a claim is granted. (A builder call count would not isolate it:
    the stamp records ``simv`` by the path spelling that wrote it, so the
    second spelling's stamp check fails on its own account — separate
    behaviour, and not what this test is about.)
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
    # One directory under two names — the premise the claim has to see.
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
    """``compile()`` re-derives its plan every call, but the claim the first
    call made still stands, so the second validates the stamp instead."""
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
    """The per-test stamp is a reuse too, so the escape hatch has to reach
    it — otherwise `--rebuild` works for verilator and silently does not for
    the families that cannot share."""
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
    """Byte-parity when nothing is configured: the flag defaults off and the
    reuse it overrides is the one that was there before it existed."""
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls)

    assert _make_sim(tmp_path, monkeypatch, test_name="test_a").compile() == 0
    assert _make_sim(tmp_path, monkeypatch, test_name="test_b").compile() == 0
    assert len(calls) == 1


def test_a_forced_rebuild_says_which_directory_it_is_recompiling(
    tmp_path, monkeypatch, caplog
):
    """With ``--rebuild`` the reader's question flips from "is this stale?"
    to "did it actually recompile?", so the answer has a line of its own."""
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
    # The same field schema its counterpart `compile.build_reused` carries:
    # basename in `build_dir`, absolute in `build_path`. A consumer keying
    # on either across the pair gets one kind of thing.
    assert record.rtl_fields["build_dir"] == shared_dir.name
    assert record.rtl_fields["build_path"] == str(shared_dir)
    assert "--rebuild given" in record.getMessage()
    assert shared_dir.name in record.getMessage()
    # And on the console, like the reuse line: "did --rebuild reach this
    # job?" is unanswerable from a job log that never printed it.
    assert console == ["compile.rebuild_forced"]


# ------------------------------------------- cross-process build lock (#494)
#
# After a manual `rm -rf .shared-builds`, the issue's suite started eight
# tests together, three of which died with `collect2: error: ld returned 1
# exit status`: every process found no stamp and compiled into the one
# freshly created directory. The #495 in-job grouping serialises the
# compiles of ONE process, so the fix is a flock the stamp check sits
# inside. These drive VlogSim directly, which is where that lock lives —
# the cross-PROCESS half is in tests/test_artifact_lock.py.


def _lock_events(monkeypatch):
    """Collect (and forward) the build lock's console events."""
    seen = []
    real = artifact_lock_module.log_console_event

    def _spy(spy_logger, level, event, **fields):
        seen.append((event, fields))
        return real(spy_logger, level, event, **fields)

    monkeypatch.setattr(artifact_lock_module, "log_console_event", _spy)
    return seen


def test_a_compile_blocked_on_the_build_lock_reuses_what_it_waited_for(
    tmp_path, monkeypatch
):
    """Double-checked locking: the waiter re-decides after acquiring.

    Two compiles of one shared directory, the first held inside its
    builder until the second is provably blocked on the lock. Waiting and
    then compiling anyway would be the same two writers with extra
    latency, so what the second must do is validate the stamp the first
    just wrote and reuse it — one builder invocation for both.

    Threads rather than processes only because the fake builder has to
    live in this process; flock treats descriptors from separate
    ``open()`` calls as separate holders, so the lock is genuinely
    contended. The cross-PROCESS half is in tests/test_artifact_lock.py.

    Read off the console spies rather than ``caplog``: the first
    ``log_console_event`` of a pytest process initialises logging, which
    detaches pytest's capture handler — and here that first event is the
    waiting line itself.
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

    first = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    second = _make_sim(tmp_path, monkeypatch, test_name="test_b")
    results = {}

    def _compile(key, sim):
        results[key] = sim.compile()

    builder = threading.Thread(target=_compile, args=("first", first))
    builder.start()
    assert compiling.wait(60)
    waiter = threading.Thread(target=_compile, args=("second", second))
    waiter.start()
    # The wait line is emitted immediately before the blocking flock, so
    # this is "the second compile is now queued behind the first" with no
    # sleep to be flaky about.
    assert waiting.wait(60), "the second compile did not queue on the lock"
    finish.set()
    builder.join(60)
    waiter.join(60)

    assert results == {"first": 0, "second": 0}
    assert len(calls) == 1, "the waiter recompiled instead of reusing"
    assert [event for event, _ in lock_events] == ["compile.build_lock_wait"]
    # It waited, then reused — the double check paying for itself. (Only
    # the waiter reports a reuse; the compile it waited for reports none.)
    assert compile_events == ["compile.build_reused"]

    _, fields = lock_events[0]
    shared_dir = Path(first._get_simv_path()).parent
    # The same directory-field schema every other compile.* build event
    # carries, so one consumer reads the whole family.
    assert {key: fields[key] for key in ("build_dir", "build_path")} == (
        vlog_sim_module._build_dir_fields(shared_dir, shared=True)
    )
    assert fields["test"] == "test_b"
    assert fields["holder_pid"] == os.getpid()
    assert fields["holder_test"] == "test_a"


def test_a_warm_shared_build_is_reused_without_taking_the_lock(tmp_path, monkeypatch):
    """The reuse fast path never queues.

    A dispatched suite's gated sim elements all call ``compile()`` against
    one already-valid shared build. Serialising those on the lock would
    put N stamp validations on the critical path back to back and leave
    every reuser hostage to whatever compile happened to hold it — for a
    guarantee the reuse path does not get anyway, since the lock is
    released before the simulation runs.
    """
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
    """The same claim, made against a lock somebody really holds.

    Held from a separate file description, which flock counts as another
    holder even in this process, so a reuse that took the lock would
    block here forever — the timeout is what the assertion is made of.
    """
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
    """``--rebuild`` skips the fast path, not the serialisation.

    The claim ``_rebuild_forced`` makes is the decision to compile, so it
    belongs inside the lock next to the compile it forces — a rebuild
    racing another process into one directory is the very thing the lock
    is for.
    """
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
    """The pre-check is advisory; the in-lock check owns the diagnostics.

    Asking twice must not say everything twice.
    ``compile.build_toolchain_changed`` is a WARNING that names an
    upgrade a reader is meant to act on, and hearing it twice for one
    rebuild reads as two upgrades.
    """
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
    """flock support varies (ENOLCK on some NFS mounts, EROFS on a
    read-only tree). A lock that cannot be taken costs the cross-process
    serialisation and nothing else — never a red build, because nothing on
    this path may change a build job's exit code."""
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
    """Per-test build directories already have one writer within a
    dispatched run (#369), and the lock file would land in the test's
    artefact directory for nothing."""
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
    """The holder metadata is advisory: a lock file a dead process left
    behind, or one nobody had written yet, must still leave a sentence."""
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "compile.build_lock_wait",
        {"build_dir": "obj_dir_abc", "build_path": "/w/obj_dir_abc"},
    )
    assert "another rtl-buddy process to finish compiling obj_dir_abc" in message
    # The first line of a wait says nothing about elapsed time, because
    # none has elapsed.
    assert "so far" not in message


def test_a_repeated_wait_line_says_how_long_it_has_been():
    """A wait re-announced every few minutes has to distinguish itself
    from the line that started it, or a job log reads as a stutter."""
    from rtl_buddy.logging_utils import _human_message

    message = _human_message(
        "compile.build_lock_wait",
        {"build_dir": "obj_dir_abc", "build_path": "/w/obj_dir_abc", "waited_sec": 600},
    )
    assert "(600s so far)" in message


def test_a_reuse_line_reports_the_stamp_age_before_the_toolchain():
    """The question a stale reuse raises is "was this built before my
    edit?", so the age leads and is written the way a reader reads a
    wall clock rather than as a raw second count (#494)."""
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
    """The stamp can vanish between validating and being stat-ed, and a
    reuse must not fail over telemetry. "Age unknown" is still a fact a
    reader wants, so the line states it instead of dropping the clause."""
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
