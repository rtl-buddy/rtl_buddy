import hashlib
import logging
import os
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.seed_mode import SeedMode
from rtl_buddy.tools.artifact_paths import (
    clear_stale_artefacts,
    sanitize_artifact_component,
    test_artifact_dir,
    test_build_dir_name,
)
from rtl_buddy.tools.vlog_cov import VlogCov
from rtl_buddy.tools.vlog_filelist import VlogFilelist
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

    def resolve_extra_sim_timeout(self, _rtl_builder_cfg):
        return 0  # these tests assert on paths, not on the timeout allowance

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


def test_vlog_sim_run_file_pins_explicit_sources(tmp_path, monkeypatch):
    source = tmp_path / "source.sv"
    source.write_text("module source; endmodule\n")
    sim = _make_sim(tmp_path, monkeypatch)
    sim.test_cfg.model.get_filelist = lambda: ["source.sv"]
    sim._ensure_artifact_dir()

    sim._write_filelist(sim._get_filelist_path())

    lines = Path(sim._get_filelist_path()).read_text().splitlines()
    assert str(source) in lines


def test_compile_fingerprint_stats_quoted_absolute_source(tmp_path, monkeypatch):
    source = tmp_path / "source tree" / "source.sv"
    source.parent.mkdir()
    source.write_text("module source; endmodule\n")
    sim = _make_sim(tmp_path, monkeypatch)
    sim._ensure_artifact_dir()
    run_f = Path(sim._get_filelist_path())
    raw_line = f'"{source}"'
    run_f.write_text(f"// generated\n{raw_line}\n")

    stamps = sim._fingerprint_filelist_sources(str(run_f))

    stat = source.stat()
    sha = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    assert stamps == [[raw_line, stat.st_size, stat.st_mtime_ns, sha]]


def test_compile_fingerprint_degrades_on_unbalanced_quote(tmp_path, monkeypatch):
    """A malformed quoted line stamps nulls instead of aborting."""
    sim = _make_sim(tmp_path, monkeypatch)
    sim._ensure_artifact_dir()
    run_f = Path(sim._get_filelist_path())
    raw_line = '"a"b"'
    run_f.write_text(f"{raw_line}\n")

    stamps = sim._fingerprint_filelist_sources(str(run_f))

    assert stamps == [[raw_line, None, None, None]]


def _ver_files(obj_dir: Path) -> str:
    """Verilator's record of every file the verilation actually consumed."""
    ver_files_path = next(iter(sorted(obj_dir.glob("*__verFiles.dat"))), None)
    assert ver_files_path is not None, f"no *__verFiles.dat under {obj_dir}"
    return ver_files_path.read_text()


def _nested_worktree_repro(tmp_path: Path):
    """Build the path geometry from #457, including an ancestor with spaces."""
    primary = tmp_path / "project with spaces"
    primary_source = primary / "design" / "dut.sv"
    primary_source.parent.mkdir(parents=True)
    primary_source.write_text("module primary_dut; endmodule\n")

    worktree = primary / ".claude" / "worktrees" / "feature"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "root_config.yaml").write_text("{}\n")
    worktree_source = worktree / "design" / "dut.sv"
    worktree_source.parent.mkdir()
    worktree_source.write_text("module dut; endmodule\n")
    (worktree / "common").mkdir()
    (worktree / "lib").mkdir()

    suite = worktree / "verif" / "block"
    suite.mkdir(parents=True)
    testbench = suite / "tb_top.sv"
    testbench.write_text("module tb_top; dut u_dut(); endmodule\n")
    output_dir = suite / "artefacts" / "basic"
    output_dir.mkdir(parents=True)

    model = ModelConfig(
        name="dut",
        filelist=["-v dut.sv", "+libext+.sv"],
        path=str(worktree / "design" / "models.yaml"),
    )
    run_f = output_dir / "run.f"
    filelist = VlogFilelist(name="t", model_cfg=model, output_path=str(run_f))
    filelist.write_output(
        unroll=True,
        absolute_sources=True,
        test_filelist=[
            "+incdir+../../common",
            "-y ../../lib",
            "+define+WIDTH=8",
            "tb_top.sv",
        ],
        suite_dir=str(suite),
    )
    return primary_source, worktree_source, testbench, run_f


def test_write_output_absolute_sources_blocks_nested_worktree_composition(
    tmp_path: Path,
):
    """Explicit sources and search directories are both pinned (#457, #474)."""
    primary_source, worktree_source, testbench, run_f = _nested_worktree_repro(tmp_path)
    output_dir = run_f.parent
    worktree = output_dir.parents[3]
    lines = run_f.read_text().splitlines()

    assert f'-v "{worktree_source}"' in lines
    assert f'"{testbench}"' in lines
    # The worktree sits under a directory with a space, so the pinned search
    # directories come back quoted exactly like the pinned sources do.
    assert f'+incdir+"{worktree / "common"}"' in lines
    assert f'-y "{worktree / "lib"}"' in lines
    assert "+define+WIDTH=8" in lines
    assert "+libext+.sv" in lines

    # Before #457, Verilator tried this composed candidate before its cwd
    # fallback. It exists in the primary checkout, so the wrong source won.
    old_source_entry = os.path.relpath(worktree_source, output_dir)
    incdir_entry = next(
        line.removeprefix("+incdir+").strip('"')
        for line in lines
        if line.startswith("+incdir+")
    )
    composed = Path(os.path.normpath(output_dir / incdir_entry / old_source_entry))
    assert composed == primary_source


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_compiles_nested_worktree_source_from_absolute_run_f(tmp_path: Path):
    """The real builder consumes the worktree source, including a spaced path."""
    primary_source, worktree_source, _testbench, run_f = _nested_worktree_repro(
        tmp_path
    )
    obj_dir = run_f.parent / "obj_dir"
    result = subprocess.run(
        [
            "verilator",
            "--cc",
            "--top-module",
            "tb_top",
            "--Mdir",
            str(obj_dir),
            "-f",
            str(run_f),
        ],
        cwd=run_f.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    ver_files = _ver_files(obj_dir)
    assert str(worktree_source) in ver_files
    assert str(primary_source) not in ver_files


def _nested_incdir_repro(
    tmp_path: Path,
    *,
    root_name: str = "project with spaces",
    absolute_sources: bool = True,
    scratch_artefacts: bool = False,
    library_dir: bool = True,
):
    """The #474 geometry: a design filelist that owns its own include path.

    ``models.yaml`` at the project root pulls ``design/blk/blk.f`` in with
    ``-F``; that nested filelist carries ``+incdir+.`` and ``-y .`` for its
    own directory. The consuming suite lives in an unrelated subtree, so
    every search directory needs four ``..`` hops from ``run.f``. The
    default root has a space in it to exercise quoting.

    ``library_dir`` off drops the ``-y .`` entry, leaving ``+incdir+`` as
    the only way to reach the header: Verilator searches ``-y`` directories
    for includes too, so a test that means to exercise ``+incdir+`` alone
    has to take the library directory away.

    ``scratch_artefacts`` puts the suite's ``artefacts/`` tree on a
    different path and symlinks it into the suite — the ordinary "artefacts
    live on scratch space" setup, and the reason ``rb test`` was exposed
    even though it compiles with its cwd set to ``run.f``'s directory:
    ``os.path.relpath`` collapses ``..`` textually while the builder walks
    it physically.
    """
    root = tmp_path / root_name
    (root / ".git").mkdir(parents=True)
    (root / "root_config.yaml").write_text("{}\n")
    design = root / "design" / "blk"
    design.mkdir(parents=True)
    (design / "blk_helper.svh").write_text("localparam int BLK_W = 8;\n")
    if library_dir:
        (design / "blk_lib.sv").write_text("module blk_lib; endmodule\n")
        (design / "blk.sv").write_text(
            'module blk;\n`include "blk_helper.svh"\nblk_lib u_lib();\nendmodule\n'
        )
        (design / "blk.f").write_text("+incdir+.\n-y .\n+libext+.sv\nblk.sv\n")
    else:
        (design / "blk.sv").write_text(
            'module blk;\n`include "blk_helper.svh"\nendmodule\n'
        )
        (design / "blk.f").write_text("+incdir+.\nblk.sv\n")

    suite = root / "verif" / "unrelated"
    suite.mkdir(parents=True)
    (suite / "tb_top.sv").write_text("module tb_top;\nblk u_blk();\nendmodule\n")
    (suite / "tb_inc").mkdir()
    if scratch_artefacts:
        physical = tmp_path / "scratch" / "artefacts"
        (physical / "basic").mkdir(parents=True)
        (suite / "artefacts").symlink_to(physical, target_is_directory=True)
    output_dir = suite / "artefacts" / "basic"
    output_dir.mkdir(parents=True, exist_ok=True)

    model = ModelConfig(
        name="blk",
        filelist=["-F design/blk/blk.f"],
        path=str(root / "models.yaml"),
    )
    run_f = output_dir / "run.f"
    VlogFilelist(name="t", model_cfg=model, output_path=str(run_f)).write_output(
        unroll=True,
        deduplicate=True,
        absolute_sources=absolute_sources,
        test_filelist=["+incdir+tb_inc", "tb_top.sv"],
        suite_dir=str(suite),
    )
    return root, design, suite, run_f


def test_write_output_pins_nested_filelist_search_dirs_to_declaring_filelist(
    tmp_path: Path,
):
    """``+incdir+.`` in a nested ``-F`` names the nested filelist's directory,
    and is emitted as an absolute path so no consumer's cwd can reinterpret
    it (#474)."""
    _root, design, suite, run_f = _nested_incdir_repro(tmp_path)
    lines = run_f.read_text().splitlines()

    assert f'+incdir+"{design}"' in lines
    assert f'-y "{design}"' in lines
    # The suite-level entry is still anchored on tests.yaml, not on the model.
    assert f'+incdir+"{suite / "tb_inc"}"' in lines
    # Nothing directory-valued is left for a consumer's cwd to reinterpret.
    search_dirs = [
        line.removeprefix("+incdir+").removeprefix("-y ").strip('"')
        for line in lines
        if line.startswith(("+incdir+", "-y "))
    ]
    assert search_dirs and all(os.path.isabs(entry) for entry in search_dirs)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_resolves_nested_incdir_from_a_foreign_cwd(tmp_path: Path):
    """The reported failure: ``-f`` makes relative filelist entries resolve
    against the *builder's* cwd, so a relative ``+incdir+`` silently landed on
    the consuming directory and the header was not found (#474)."""
    root, design, _suite, run_f = _nested_incdir_repro(tmp_path)
    obj_dir = run_f.parent / "obj_dir"
    result = subprocess.run(
        [
            "verilator",
            "--cc",
            "--top-module",
            "tb_top",
            "--Mdir",
            str(obj_dir),
            "-f",
            str(run_f),
        ],
        # Deliberately not run.f's own directory.
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    ver_files = _ver_files(obj_dir)
    # The header came through +incdir+ and the library module through -y.
    assert str(design / "blk_helper.svh") in ver_files
    assert str(design / "blk_lib.sv") in ver_files


def test_write_output_keeps_search_dirs_relative_without_absolute_sources(
    tmp_path: Path,
):
    """The other half of the contract: only the flow that opts in gets the
    pin. Every other consumer reads its filelist back itself and resolves
    entries against the filelist's own directory, so their spelling is
    unchanged (#474)."""
    _root, design, suite, run_f = _nested_incdir_repro(tmp_path, absolute_sources=False)
    lines = run_f.read_text().splitlines()
    output_dir = run_f.parent

    assert f"+incdir+{os.path.relpath(design, output_dir)}" in lines
    assert f"-y {os.path.relpath(design, output_dir)}" in lines
    assert f"+incdir+{os.path.relpath(suite / 'tb_inc', output_dir)}" in lines
    assert not [line for line in lines if line.startswith(("+incdir+/", "-y /"))]


def test_write_output_pins_search_dirs_through_a_symlinked_artefact_dir(
    tmp_path: Path,
):
    """A symlink anywhere between ``run.f`` and the design is enough to
    reproduce the report under ``rb test`` itself: it compiles with its cwd
    set to ``run.f``'s directory, but ``relpath`` collapses ``..``
    textually while the builder walks it physically, so the two disagree
    (#474)."""
    _root, design, _suite, run_f = _nested_incdir_repro(
        tmp_path, scratch_artefacts=True
    )
    lines = run_f.read_text().splitlines()

    assert f'+incdir+"{design}"' in lines
    assert f'-y "{design}"' in lines

    # The spelling emitted before the fix, resolved the way a process whose
    # cwd is run.f's directory actually resolves it. It misses the design
    # entirely — which is the "directory exists, wrong directory" trap when
    # something else happens to sit there.
    stale = os.path.relpath(design, run_f.parent)
    physically = os.path.join(os.path.realpath(run_f.parent), stale)
    assert not os.path.isdir(physically)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_resolves_nested_incdir_through_a_symlinked_artefact_dir(
    tmp_path: Path,
):
    """Belt and braces for the symlink case, with the builder invoked the
    way ``rb test`` invokes it: cwd is ``run.f``'s own directory (#474)."""
    _root, design, _suite, run_f = _nested_incdir_repro(
        tmp_path, scratch_artefacts=True
    )
    obj_dir = run_f.parent / "obj_dir"
    result = subprocess.run(
        [
            "verilator",
            "--cc",
            "--top-module",
            "tb_top",
            "--Mdir",
            str(obj_dir),
            "-f",
            str(run_f),
        ],
        cwd=run_f.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(design / "blk_helper.svh") in _ver_files(obj_dir)


def test_write_output_keeps_incdir_relative_when_the_path_contains_plus(
    tmp_path: Path, caplog
):
    """``+incdir+`` cannot express a ``+`` in a path — every filelist parser
    reads ``+incdir+a+b`` as two directories, and quoting does not help — so
    such an entry keeps its relative spelling and says so. ``-y`` takes its
    argument as a separate token and is still pinned (#474)."""
    with caplog.at_level(logging.WARNING, logger="rtl_buddy.tools.vlog_filelist"):
        _root, design, _suite, run_f = _nested_incdir_repro(
            tmp_path, root_name="pro+ject"
        )
    lines = run_f.read_text().splitlines()

    assert f"+incdir+{os.path.relpath(design, run_f.parent)}" in lines
    assert f"-y {design}" in lines
    assert not [line for line in lines if line.startswith("+incdir+/")]

    events = [
        record
        for record in caplog.records
        if getattr(record, "rtl_event", None) == "filelist.incdir_unrepresentable"
    ]
    assert len(events) == 1, caplog.text
    assert str(design) in events[0].rtl_fields["paths"]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_compiles_when_the_checkout_path_contains_plus(tmp_path: Path):
    """The fallback is what keeps a ``+`` checkout working at all: pinning
    such an include directory absolute would split it in two (#474)."""
    # No `-y`: Verilator also searches library directories for includes, and
    # `-y` is unaffected by `+`, so it would rescue the include and hide
    # whatever the `+incdir+` entry does.
    _root, design, _suite, run_f = _nested_incdir_repro(
        tmp_path, root_name="pro+ject", library_dir=False
    )
    obj_dir = run_f.parent / "obj_dir"
    result = subprocess.run(
        [
            "verilator",
            "--cc",
            "--top-module",
            "tb_top",
            "--Mdir",
            str(obj_dir),
            "-f",
            str(run_f),
        ],
        cwd=run_f.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Recorded relative, because the entry that found it stayed relative.
    assert "blk_helper.svh" in _ver_files(obj_dir)


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
    cfg.name = "icarus-builder"
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


def test_license_marker_helpers_share_one_implementation():
    """The live monitor and the post-hoc compile check must agree (#358)."""
    from rtl_buddy.tools import vcs_license

    for text in (
        "Queuing for License",
        "Licensed number of users already reached",
        "  Queuing for License...",
    ):
        assert vcs_license.has_license_queue_marker(text)
        assert vcs_license._is_marker_line(text)
    for text in ("Parsing design file", "", "...."):
        assert not vcs_license.has_license_queue_marker(text)
        assert not vcs_license._is_marker_line(text)


# ---------------------------------------------------------------------------
# clear_stale_artefacts (#469)
# ---------------------------------------------------------------------------


def test_clear_stale_artefacts_removes_only_what_exists(tmp_path):
    present = tmp_path / "report.json"
    present.write_text("{}")
    absent = tmp_path / "never_written.json"

    removed = clear_stale_artefacts([present, absent, None], owner="demo")

    assert removed == [str(present)]
    assert not present.exists()


def test_clear_stale_artefacts_fails_loudly_when_removal_fails(tmp_path):
    """An artefact we cannot delete would silently mask the run, so refuse to
    run rather than risk reporting a previous run's numbers."""
    from rtl_buddy.errors import FatalRtlBuddyError

    # A directory at the artefact's path: unlink() raises, and no flow
    # writes a directory there, so this stands in for any undeletable file.
    blocked = tmp_path / "report.json"
    blocked.mkdir()

    with pytest.raises(FatalRtlBuddyError, match="could not remove"):
        clear_stale_artefacts([blocked], owner="demo")
