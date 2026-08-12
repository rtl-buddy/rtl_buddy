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
