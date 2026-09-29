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
        return 0  # these tests do not exercise the timeout

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
        self.pa = None
        self.resolved_seed = None
        self.seed_source = None
        self.seed_identity = None
        self.sim_rand_seed_plusarg = None

    def get_name(self):
        return self.name

    def get_builder_name(self):
        return self.builder_name

    def get_model(self):
        return self.model

    def get_testbench(self):
        return self.tb

    def get_plusargs(self):
        return self.pa

    def get_resolved_seed(self):
        return self.resolved_seed

    def ensure_resolved_seed_plusarg(self):
        if self.resolved_seed is None or self.sim_rand_seed_plusarg is None:
            return
        if self.pa is None:
            self.pa = {}
        self.pa[self.sim_rand_seed_plusarg] = self.resolved_seed

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
    test_cfg=None,
    test_builder=None,
    builders=None,
    builder_override=None,
):
    monkeypatch.chdir(tmp_path)
    builder_cfg = builder_cfg or DummyBuilderCfg()
    root_cfg = DummyRootCfg(
        builder_cfg, builders=builders, builder_override=builder_override
    )
    test_cfg = test_cfg or DummyTestCfg(
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
    """A malformed quoted line is stamped with nulls instead of aborting."""
    sim = _make_sim(tmp_path, monkeypatch)
    sim._ensure_artifact_dir()
    run_f = Path(sim._get_filelist_path())
    raw_line = '"a"b"'
    run_f.write_text(f"{raw_line}\n")

    stamps = sim._fingerprint_filelist_sources(str(run_f))

    assert stamps == [[raw_line, None, None, None]]


def _ver_files(obj_dir: Path) -> str:
    """Return the contents of Verilator's record of the files it consumed."""
    ver_files_path = next(iter(sorted(obj_dir.glob("*__verFiles.dat"))), None)
    assert ver_files_path is not None, f"no *__verFiles.dat under {obj_dir}"
    return ver_files_path.read_text()


def _nested_worktree_repro(tmp_path: Path):
    """Build a worktree nested in a project whose path contains spaces."""
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
    """Explicit sources and search directories are both pinned to absolute paths."""
    primary_source, worktree_source, testbench, run_f = _nested_worktree_repro(tmp_path)
    output_dir = run_f.parent
    worktree = output_dir.parents[3]
    lines = run_f.read_text().splitlines()

    assert f'-v "{worktree_source}"' in lines
    assert f'"{testbench}"' in lines
    # The path contains a space, so pinned search directories are quoted like sources.
    assert f'+incdir+"{worktree / "common"}"' in lines
    assert f'-y "{worktree / "lib"}"' in lines
    assert "+define+WIDTH=8" in lines
    assert "+libext+.sv" in lines

    # A relative entry would compose to the primary checkout's source, which Verilator tries before its cwd fallback.
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
    """Verilator consumes the worktree source through a path with spaces."""
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
    """Build a project whose design filelist, pulled in with ``-F``, carries ``+incdir+.`` and ``-y .`` for its own directory.

    The consuming suite is in an unrelated subtree, so search directories are four ``..`` hops from ``run.f``. The default root contains a space.

    ``library_dir=False`` drops ``-y .`` so that only ``+incdir+`` can reach the header, since Verilator also searches ``-y`` directories for includes. ``scratch_artefacts`` puts the suite's ``artefacts/`` on another path and symlinks it in, so ``relpath`` (textual) and the builder (physical) disagree about ``..``.
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
    """``+incdir+.`` in a nested ``-F`` names that filelist's directory and is emitted as an absolute path."""
    _root, design, suite, run_f = _nested_incdir_repro(tmp_path)
    lines = run_f.read_text().splitlines()

    assert f'+incdir+"{design}"' in lines
    assert f'-y "{design}"' in lines
    # The suite-level entry stays anchored on tests.yaml.
    assert f'+incdir+"{suite / "tb_inc"}"' in lines
    # Every search directory is absolute.
    search_dirs = [
        line.removeprefix("+incdir+").removeprefix("-y ").strip('"')
        for line in lines
        if line.startswith(("+incdir+", "-y "))
    ]
    assert search_dirs and all(os.path.isabs(entry) for entry in search_dirs)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_resolves_nested_incdir_from_a_foreign_cwd(tmp_path: Path):
    """Verilator finds the header and library module through absolute entries when run from an unrelated cwd."""
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
        # Not run.f's own directory.
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    ver_files = _ver_files(obj_dir)
    # The header came through +incdir+, the library module through -y.
    assert str(design / "blk_helper.svh") in ver_files
    assert str(design / "blk_lib.sv") in ver_files


def test_write_output_keeps_search_dirs_relative_without_absolute_sources(
    tmp_path: Path,
):
    """Without absolute sources, search directories stay relative to the filelist."""
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
    """Search directories are absolute even when a symlink lies between ``run.f`` and the design."""
    _root, design, _suite, run_f = _nested_incdir_repro(
        tmp_path, scratch_artefacts=True
    )
    lines = run_f.read_text().splitlines()

    assert f'+incdir+"{design}"' in lines
    assert f'-y "{design}"' in lines

    # A relative spelling, resolved physically from run.f's directory, misses the design.
    stale = os.path.relpath(design, run_f.parent)
    physically = os.path.join(os.path.realpath(run_f.parent), stale)
    assert not os.path.isdir(physically)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_resolves_nested_incdir_through_a_symlinked_artefact_dir(
    tmp_path: Path,
):
    """Verilator run from ``run.f``'s directory resolves the nested include through a symlinked artefact directory."""
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
    """An ``+incdir+`` path containing ``+`` stays relative and logs a warning; ``-y`` is still pinned."""
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
    """Verilator compiles with a ``+`` in the checkout path."""
    # No `-y`: Verilator would find the include through it and hide the `+incdir+` behaviour.
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


@pytest.mark.parametrize(
    "seed,source,mode",
    [(410729, "master", SeedMode.MASTER), (0, "default", SeedMode.DEFAULT)],
)
def test_vlog_sim_uses_pre_resolved_seed_for_simulator_plusarg_and_artifact(
    tmp_path, monkeypatch, seed, source, mode
):
    captured = {}
    test_cfg = DummyTestCfg("basic", tmp_path / "models.yaml")
    test_cfg.resolved_seed = seed
    test_cfg.seed_source = source
    test_cfg.seed_identity = "verif/vxp/tests.yaml::deepseek_v4::single"
    test_cfg.sim_rand_seed_plusarg = "stimulus_seed"
    test_cfg.ensure_resolved_seed_plusarg()
    sim = _make_sim(tmp_path, monkeypatch, test_cfg=test_cfg)
    hook_seed = tmp_path / "hook-seed.txt"
    preproc = tmp_path / "preproc.py"
    preproc.write_text(
        "from pathlib import Path\n"
        f"Path({str(hook_seed)!r}).write_text("
        "str(test_cfg.get_resolved_seed()) + ':' + "
        "str(test_cfg.get_plusargs()['stimulus_seed']))\n"
        "test_cfg.resolved_seed = 999\n"
        "test_cfg.pa['stimulus_seed'] = 999\n"
    )
    sim.test_cfg.get_preproc_path = lambda: str(preproc)

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.pre() is None
    assert hook_seed.read_text() == f"{seed}:{seed}"
    assert sim.execute(seed_mode=mode) == 0
    assert f"+seed={seed}" in captured["cmd"]
    assert f"+stimulus_seed={seed}" in captured["cmd"]
    assert Path(sim._get_randseed_path()).read_text().splitlines()[0] == str(seed)


def test_run_multiple_style_preproc_and_simulations_share_fixed_seed(
    tmp_path, monkeypatch
):
    captured = []
    test_cfg = DummyTestCfg("basic", tmp_path / "models.yaml")
    test_cfg.resolved_seed = 41
    test_cfg.seed_source = "fixed"
    test_cfg.seed_identity = "verif/timing/tests.yaml::command_timing::1"
    test_cfg.sim_rand_seed_plusarg = "stimulus_seed"
    test_cfg.ensure_resolved_seed_plusarg()
    sim = _make_sim(tmp_path, monkeypatch, test_cfg=test_cfg)
    hook_seed = tmp_path / "hook-seed.txt"
    preproc = tmp_path / "preproc.py"
    preproc.write_text(
        "from pathlib import Path\n"
        f"Path({str(hook_seed)!r}).write_text("
        "str(test_cfg.get_plusargs()['stimulus_seed']))\n"
    )
    sim.test_cfg.get_preproc_path = lambda: str(preproc)

    def _fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.pre(run_id=None) is None
    assert hook_seed.read_text() == "41"
    for run_id in (1, 2):
        assert sim.execute(run_id=run_id, seed_mode=SeedMode.NEW) == 0

    assert all("+seed=41" in cmd for cmd in captured)
    assert all("+stimulus_seed=41" in cmd for cmd in captured)
    for run_id in (1, 2):
        assert Path(sim._get_randseed_path(run_id=run_id)).read_text() == "41\n"


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
    # A successful compile writes the wrapper script.
    wrapper = Path(tmp_path / "artefacts" / "basic" / "simv")
    assert wrapper.is_file()
    assert "exec vvp" in wrapper.read_text()
    assert snapshot in wrapper.read_text()
    # The wrapper must be executable.
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
    """The live monitor and the post-compile check use the same license markers."""
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
# clear_stale_artefacts
# ---------------------------------------------------------------------------


def test_clear_stale_artefacts_removes_only_what_exists(tmp_path):
    present = tmp_path / "report.json"
    present.write_text("{}")
    absent = tmp_path / "never_written.json"

    removed = clear_stale_artefacts([present, absent, None], owner="demo")

    assert removed == [str(present)]
    assert not present.exists()


def test_clear_stale_artefacts_fails_loudly_when_removal_fails(tmp_path):
    """An artefact that cannot be removed is fatal."""
    from rtl_buddy.errors import FatalRtlBuddyError

    # A directory at the path makes unlink() raise, standing in for any undeletable file.
    blocked = tmp_path / "report.json"
    blocked.mkdir()

    with pytest.raises(FatalRtlBuddyError, match="could not remove"):
        clear_stale_artefacts([blocked], owner="demo")


def test_clear_managed_outputs_matches_by_suffix_only(tmp_path):
    """Only files with a managed suffix are cleared; everything else survives."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    (tmp_path / "old_top.bit").write_bytes(b"\x00")
    (tmp_path / "new_top.bit").write_bytes(b"\x00")
    (tmp_path / "fpga.f").write_text("-v a.sv\n")
    (tmp_path / "yosys.log").write_text("log\n")
    nested = tmp_path / "sby_workdir"
    nested.mkdir()
    (nested / "inner.bit").write_bytes(b"\x00")

    removed = clear_managed_outputs(tmp_path, (".bit",), owner="demo")

    assert sorted(os.path.basename(p) for p in removed) == [
        "new_top.bit",
        "old_top.bit",
    ]
    assert (tmp_path / "fpga.f").exists()
    assert (tmp_path / "yosys.log").exists()
    # Not recursive.
    assert (nested / "inner.bit").exists()


def test_clear_managed_outputs_honours_keep(tmp_path):
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    (tmp_path / "top.json").write_text("{}")
    (tmp_path / "results.json").write_text("{}")

    removed = clear_managed_outputs(
        tmp_path, (".json",), owner="demo", keep=("results.json",)
    )

    assert [os.path.basename(p) for p in removed] == ["top.json"]
    assert (tmp_path / "results.json").exists()


def test_clear_managed_outputs_missing_dir_is_not_an_error(tmp_path):
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    assert clear_managed_outputs(tmp_path / "never-made", (".bit",), owner="d") == []


def test_clear_managed_outputs_unlistable_dir_is_fatal(tmp_path):
    """An unlistable directory raises FatalRtlBuddyError rather than reading as empty."""
    if os.geteuid() == 0:  # pragma: no cover - depends on the runner
        pytest.skip("root ignores directory permissions")
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    locked = tmp_path / "artefacts"
    locked.mkdir()
    (locked / "top.bit").write_text("stale\n")
    locked.chmod(0o000)
    try:
        with pytest.raises(FatalRtlBuddyError) as excinfo:
            clear_managed_outputs(locked, (".bit",), owner="demo")
    finally:
        locked.chmod(0o755)

    message = str(excinfo.value)
    assert "demo" in message
    assert str(locked) in message
    # The stale output remains.
    assert (locked / "top.bit").exists()


def test_clear_managed_outputs_directory_at_an_output_path_is_fatal(tmp_path):
    """A directory where an output belongs is fatal and is not removed."""
    from rtl_buddy.errors import FatalRtlBuddyError
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    blocked = tmp_path / "top.bit"
    blocked.mkdir()
    (blocked / "inside.txt").write_text("not ours to delete\n")

    with pytest.raises(FatalRtlBuddyError, match="could not remove"):
        clear_managed_outputs(tmp_path, (".bit",), owner="demo")

    assert blocked.is_dir()
    assert (blocked / "inside.txt").exists()


def test_clear_managed_outputs_removes_a_dangling_symlink(tmp_path):
    """A dangling symlink is cleared."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    link = tmp_path / "top.bit"
    link.symlink_to(tmp_path / "never-existed.bit")
    assert link.is_symlink() and not link.is_file()

    removed = clear_managed_outputs(tmp_path, (".bit",), owner="demo")

    assert [os.path.basename(p) for p in removed] == ["top.bit"]
    assert not link.is_symlink()


def test_protected_names_are_outputs_a_flow_really_writes():
    """Every protected sibling name is written by some flow."""
    from rtl_buddy.tools import vlog_sim as vlog_sim_mod
    from rtl_buddy.tools.artifact_paths import (
        SHARED_BUILD_STAMP_NAME,
        SIBLING_OUTPUT_NAMES,
    )

    # Several names are shared constants imported from `artifact_paths`; check their bindings rather than grepping for the string.
    from rtl_buddy.cov import manifest as cov_manifest, model as cov_model
    from rtl_buddy.graph import config_tier, results as graph_results
    from rtl_buddy.phys import (
        manifest as phys_manifest,
        model as phys_model,
        publish as phys_publish,
    )
    from rtl_buddy.tools import artifact_paths as ap
    from rtl_buddy.xplr import gitprov, ledger

    rebound = {
        vlog_sim_mod.SHARED_BUILD_STAMP_NAME: SHARED_BUILD_STAMP_NAME,
        config_tier.GRAPH_JSON_NAME: ap.GRAPH_JSON_NAME,
        config_tier.GRAPH_META_NAME: ap.GRAPH_META_NAME,
        graph_results.RESULTS_OVERLAY_NAME: ap.RESULTS_OVERLAY_NAME,
        cov_manifest.MANIFEST_FILENAME: ap.COV_MANIFEST_NAME,
        cov_model.MODEL_FILENAME: ap.COV_MODEL_NAME,
        phys_manifest.MANIFEST_FILENAME: ap.PHYS_MANIFEST_NAME,
        phys_model.MODEL_FILENAME: ap.PHYS_MODEL_NAME,
        phys_publish.PUBLISH_LOCK_FILENAME: ap.PHYS_PUBLISH_LOCK_NAME,
        ledger.RECORD_FILENAME: ap.XPLR_RECORD_NAME,
        gitprov.WORKTREE_SIDECAR: ap.XPLR_WORKTREE_SIDECAR_NAME,
    }
    for writer_name, protected_name in rebound.items():
        assert writer_name == protected_name

    tools = Path(__file__).parent.parent / "src" / "rtl_buddy" / "tools"
    sources = "\n".join(
        (tools / name).read_text()
        for name in (
            "cdc_rtl_buddy.py",
            "cdc_vivado.py",
            "power_openroad.py",
            "pnr_openroad.py",
            "synth_yosys.py",
            "synth_openroad.py",
            "axi_profile_rtl_buddy.py",
        )
    )
    # `cdc.json` and `cdc.txt` are built as f"cdc.{fmt}"; check the stem.
    unwritten = [
        name
        for name in SIBLING_OUTPUT_NAMES
        if name not in sources and not name.startswith("cdc.") and name not in rebound
    ]
    assert not unwritten, f"protected but written by nobody: {unwritten}"


def test_a_suffix_clear_spares_every_protected_name_but_takes_its_own():
    """A suffix clear spares every protected name and removes the flow's own top-named outputs."""
    import tempfile
    from rtl_buddy.tools import fpga_openxc7, pnr_openroad
    from rtl_buddy.tools.artifact_paths import (
        PROTECTED_OUTPUT_PATTERNS,
        clear_managed_outputs,
    )

    for suffixes in (
        fpga_openxc7._MANAGED_OUTPUT_SUFFIXES,
        pnr_openroad._MANAGED_OUTPUT_SUFFIXES,
        (".bit",),
    ):
        d = Path(tempfile.mkdtemp())
        protected = [n for n in PROTECTED_OUTPUT_PATTERNS if "*" not in n]
        for name in protected:
            (d / name).write_text("sibling output\n")
        # The flow's own outputs, named after the top.
        mine = [f"demo_top{suffix}" for suffix in suffixes]
        for name in mine:
            (d / name).write_text("mine\n")

        clear_managed_outputs(d, suffixes, owner="demo")

        for name in protected:
            assert (d / name).exists(), f"{name} was eaten by a {suffixes} clear"
        for name in mine:
            assert not (d / name).exists(), f"{name} should have been cleared"


def test_clear_managed_outputs_own_wins_over_the_protected_patterns(tmp_path):
    """`own` overrides the protected names, so a design topped `graph` can clear its own `graph.json`."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    mine = tmp_path / "graph.json"  # this run's <top>.json netlist
    mine.write_text('{"mine": true}')
    theirs = tmp_path / "record.json"  # a sibling command's output
    theirs.write_text('{"theirs": true}')

    removed = clear_managed_outputs(
        tmp_path, (".json",), owner="demo", own=["graph.json"]
    )

    assert [os.path.basename(p) for p in removed] == ["graph.json"]
    assert not mine.exists()
    assert theirs.exists()


def test_clear_managed_outputs_own_beats_keep_too(tmp_path):
    """`own` outranks `keep`."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    (tmp_path / "top.bit").write_bytes(b"\x00")

    removed = clear_managed_outputs(
        tmp_path, (".bit",), owner="demo", own=["top.bit"], keep=["top.bit"]
    )

    assert [os.path.basename(p) for p in removed] == ["top.bit"]


def test_clear_managed_outputs_own_clears_even_without_a_matching_suffix(tmp_path):
    """`own` names files outright; it does not have to match the suffix list."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    (tmp_path / "odd_name.xyz").write_text("mine")

    removed = clear_managed_outputs(
        tmp_path, (".bit",), owner="demo", own=["odd_name.xyz"]
    )

    assert [os.path.basename(p) for p in removed] == ["odd_name.xyz"]


def test_owned_ledger_round_trips(tmp_path):
    """The ownership ledger round-trips per flow."""
    from rtl_buddy.tools.artifact_paths import (
        OWNED_LEDGER_NAME,
        read_owned_ledger,
        write_owned_ledger,
    )

    assert read_owned_ledger(tmp_path, "demo-flow") == set()

    write_owned_ledger(tmp_path, "demo-flow", ["b.json", "a.bit"])
    assert read_owned_ledger(tmp_path, "demo-flow") == {"a.bit", "b.json"}
    # Claims are per flow: another flow sees nothing of this one's.
    assert read_owned_ledger(tmp_path, "other-flow") == set()

    # Writing another flow's claim preserves the first.
    write_owned_ledger(tmp_path, "other-flow", ["c.bit"])
    assert read_owned_ledger(tmp_path, "demo-flow") == {"a.bit", "b.json"}
    assert read_owned_ledger(tmp_path, "other-flow") == {"c.bit"}

    # A dotfile, so no suffix clear matches it.
    assert OWNED_LEDGER_NAME.startswith(".")


def test_owned_ledger_missing_or_unreadable_is_an_empty_claim(tmp_path):
    """A missing or unreadable ledger reads as an empty claim."""
    from rtl_buddy.tools.artifact_paths import OWNED_LEDGER_NAME, read_owned_ledger

    assert read_owned_ledger(tmp_path / "never-made", "demo-flow") == set()

    # A directory where the file should be is unreadable, not fatal.
    (tmp_path / OWNED_LEDGER_NAME).mkdir()
    assert read_owned_ledger(tmp_path, "demo-flow") == set()


def test_clear_managed_outputs_clears_a_renamed_tops_protected_output(tmp_path):
    """The ledger keeps a renamed top's protected-name output clearable."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    suffixes = (".json", ".bit")

    # Run 1: top is `graph`, a protected basename.
    clear_managed_outputs(
        tmp_path,
        suffixes,
        owner="demo",
        own=["graph.json", "graph.bit"],
        own_flow="demo-flow",
    )
    (tmp_path / "graph.json").write_text("run 1 netlist")

    # Run 2: the top is renamed. The old netlist must still go.
    clear_managed_outputs(
        tmp_path,
        suffixes,
        owner="demo",
        own=["other.json", "other.bit"],
        own_flow="demo-flow",
    )

    assert not (tmp_path / "graph.json").exists()


def test_clear_managed_outputs_spares_a_sibling_dir_it_never_owned(tmp_path):
    """A protected name in a directory the flow never wrote is spared."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    theirs = tmp_path / "graph.json"
    theirs.write_text("written by rb graph")

    clear_managed_outputs(tmp_path, (".json",), owner="demo", own=["other.json"])

    assert theirs.read_text() == "written by rb graph"


def test_clear_managed_outputs_without_own_does_not_claim_the_directory(tmp_path):
    """A caller that passes no `own` writes no claim."""
    from rtl_buddy.tools.artifact_paths import OWNED_LEDGER_NAME, clear_managed_outputs

    clear_managed_outputs(tmp_path, (".bit",), owner="demo", own_flow="demo-flow")

    assert not (tmp_path / OWNED_LEDGER_NAME).exists()


def test_owned_ledger_claims_do_not_leak_between_flows(tmp_path):
    """Two flows sharing a run name and artefact directory do not clear each other's claimed outputs."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs

    pnr_suffixes = (".def", ".routed.odb")
    fpga_suffixes = (".json", ".bit")

    clear_managed_outputs(
        tmp_path,
        pnr_suffixes,
        owner="shared_name",
        own=[f"top{s}" for s in pnr_suffixes],
        own_flow="pnr-openroad",
    )
    odb = tmp_path / "top.routed.odb"
    odb.write_bytes(b"the P&R run's routed database")

    # The FPGA flow clears next, in the same directory.
    clear_managed_outputs(
        tmp_path,
        fpga_suffixes,
        owner="shared_name",
        own=[f"top{s}" for s in fpga_suffixes],
        own_flow="fpga-openxc7",
    )
    assert odb.exists(), "the FPGA cleanup inherited P&R's claim"

    # And the reverse: the FPGA bitstream survives a P&R clear.
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"the FPGA run's bitstream")
    clear_managed_outputs(
        tmp_path,
        pnr_suffixes,
        owner="shared_name",
        own=[f"top{s}" for s in pnr_suffixes],
        own_flow="pnr-openroad",
    )
    assert bit.exists(), "the P&R cleanup inherited the FPGA claim"


def test_owned_ledger_ignores_the_pre_namespace_flat_format(tmp_path):
    """A ledger in the flat, non-namespaced format is ignored."""
    from rtl_buddy.tools.artifact_paths import OWNED_LEDGER_NAME, read_owned_ledger

    (tmp_path / OWNED_LEDGER_NAME).write_text("# old format\ngraph.json\ngraph.bit\n")

    assert read_owned_ledger(tmp_path, "fpga-openxc7") == set()
    assert read_owned_ledger(tmp_path, "pnr-openroad") == set()


def test_owned_ledger_retires_a_claim_once_the_leftover_is_cleared(tmp_path):
    """A claim is retired once its leftover is cleared, so a later `rb graph` output of the same name is spared."""
    from rtl_buddy.tools.artifact_paths import clear_managed_outputs, read_owned_ledger

    suffixes = (".json", ".bit")

    # Run 1: topped `graph`, which is also `rb graph`'s protected basename.
    clear_managed_outputs(
        tmp_path,
        suffixes,
        owner="demo",
        own=["graph.json", "graph.bit"],
        own_flow="fpga-openxc7",
    )
    (tmp_path / "graph.json").write_text("the FPGA run's netlist")

    # Run 2: renamed. The leftover is this flow's, and goes.
    clear_managed_outputs(
        tmp_path,
        suffixes,
        owner="demo",
        own=["other.json", "other.bit"],
        own_flow="fpga-openxc7",
    )
    assert not (tmp_path / "graph.json").exists()
    # ...and the claim on it is retired, not carried forward.
    assert read_owned_ledger(tmp_path, "fpga-openxc7") == {"other.json", "other.bit"}

    # `rb graph` now writes its own protected file at that path.
    theirs = tmp_path / "graph.json"
    theirs.write_text("written by rb graph")

    # Run 3: the FPGA flow must not take it.
    clear_managed_outputs(
        tmp_path,
        suffixes,
        owner="demo",
        own=["other.json", "other.bit"],
        own_flow="fpga-openxc7",
    )
    assert theirs.read_text() == "written by rb graph"
