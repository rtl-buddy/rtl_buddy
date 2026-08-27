import json
import os
from contextlib import nullcontext
from pathlib import Path

from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.runner.test_runner import TestRunner as RtlBuddyTestRunner
from rtl_buddy.tools.artifact_paths import shared_build_dir
from rtl_buddy.tools import vlog_sim as vlog_sim_module


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
    def __init__(self, builder_cfg):
        self.builder_cfg = builder_cfg

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
):
    monkeypatch.chdir(tmp_path)
    builder_cfg = DummyBuilderCfg(
        exe=exe, simulator_family=family, simv=simv, compile_opts=compile_opts
    )
    model_cfg = DummyModelCfg(tmp_path / "models.yaml", filelist=["src/top.sv"])
    test_cfg = DummyTestCfg(test_name, model_cfg, pd=pd)
    return vlog_sim_module.VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=DummyRootCfg(builder_cfg),
        test_cfg=test_cfg,
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        share_build=share_build,
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


def test_verilator_compile_never_reports_license_queue(tmp_path, monkeypatch):
    _write_source(tmp_path)
    calls = []
    _install_fake_builder(monkeypatch, calls, stdout="Queuing for License...\n")

    sim = _make_sim(tmp_path, monkeypatch, test_name="test_a")
    assert sim.compile() == 0
    assert not Path(sim._get_compile_transcript_path()).exists()


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
    assert "built by Verilator 5.049 devel rev vBBBB" in caplog.text


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
