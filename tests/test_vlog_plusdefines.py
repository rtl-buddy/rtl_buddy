"""Tests for how a test's `plusdefines:` reach each simulator family's compile line.

`iverilog` reads `+define+` only inside a `-f`/`-c` command file and takes a bare `+define+NAME` argument as a source file name, so Icarus gets `-DNAME[=VALUE]`; every other family keeps `+define+`. The simulation line is unchanged.
"""

import shutil
import subprocess
from contextlib import nullcontext

import pytest

from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.tools import vlog_sim as vlog_sim_module


class _Builder:
    def __init__(self, *, family, exe):
        self.family = family
        self.exe = exe

    def get_simulator_family(self):
        return self.family

    def get_exe(self):
        return self.exe

    def get_simv(self):
        return "simv"

    def get_seed(self):
        return 0

    def get_name(self):
        return self.family

    def get_compile_time_opts(self, _mode):
        return []

    def get_run_time_opts(self, _mode, seed=None):
        return []


class _Root:
    def __init__(self, builder):
        self._builder = builder

    def get_rtl_builder_cfg(self):
        return self._builder

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
        return self._builder

    def resolve_extra_sim_timeout(self, _rtl_builder_cfg):
        return 0

    def get_use_lcov(self, _simulator_name):
        return False


class _Model:
    def __init__(self, path):
        self._path = str(path)

    def get_model_path(self):
        return self._path

    def get_filelist(self):
        return []


class _Testbench:
    def get_filelist(self):
        return []

    def is_cocotb(self):
        return False


class _TestCfg:
    def __init__(self, model_path, plusdefines):
        self.name = "basic"
        self.model = _Model(model_path)
        self.tb = _Testbench()
        self.pd = plusdefines
        self.pa = None
        self.uvm = None
        self.resolved_seed = None

    def get_resolved_seed(self):
        return self.resolved_seed

    def ensure_resolved_seed_plusarg(self):
        return None

    def get_name(self):
        return self.name

    def get_builder_name(self):
        return None

    def get_model(self):
        return self.model

    def get_testbench(self):
        return self.tb

    def get_plusargs(self):
        return self.pa

    def get_plusdefines(self):
        return self.pd

    def get_timeout(self):
        return 60, False

    def get_preproc_path(self):
        return None


# One valued and one valueless define, in declaration order.
_PLUSDEFINES = {"FOO": 10, "BAR": None}


def _make_sim(
    tmp_path, monkeypatch, *, family, exe, plusdefines=_PLUSDEFINES, share_build=False
):
    monkeypatch.chdir(tmp_path)
    return vlog_sim_module.VlogSim(
        name="rtl_buddy/vlog_sim",
        root_cfg=_Root(_Builder(family=family, exe=exe)),
        test_cfg=_TestCfg(tmp_path / "models.yaml", plusdefines),
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        share_build=share_build,
    )


def _compile_cmd(sim, monkeypatch):
    """Run ``sim.compile()`` against a fake builder; return the argv it launched."""
    launched = []

    def _fake_run(cmd, capture_output, text, cwd, **kwargs):
        launched.append(list(cmd))
        return ManagedProcessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)
    assert sim.compile() == 0
    assert len(launched) == 1
    return launched[0]


@pytest.mark.parametrize(
    "family,exe,expected",
    [
        ("icarus", "iverilog", ["-DFOO=10", "-DBAR"]),
        ("verilator", "verilator", ["+define+FOO=10", "+define+BAR"]),
        ("vcs", "vcs", ["+define+FOO=10", "+define+BAR"]),
        ("xezim", "xezim", ["+define+FOO=10", "+define+BAR"]),
    ],
    ids=["icarus", "verilator", "vcs", "xezim"],
)
def test_compile_line_spells_plusdefines_per_family(
    tmp_path, monkeypatch, family, exe, expected
):
    sim = _make_sim(tmp_path, monkeypatch, family=family, exe=exe)
    cmd = _compile_cmd(sim, monkeypatch)

    assert cmd[0] == exe
    # The defines sit together, just before the trailing `-f run.f`.
    assert cmd[-4:-2] == expected
    assert cmd[-2] == "-f"
    if family == "icarus":
        assert not [arg for arg in cmd if arg.startswith("+define+")]


@pytest.mark.parametrize(
    "family,exe", [("icarus", "iverilog"), ("verilator", "verilator")]
)
def test_simulation_line_defines_are_unchanged(tmp_path, monkeypatch, family, exe):
    # The simulation line still gets the `+define+` spelling, which a simulator
    # (including `vvp`) reads as an ignorable plusarg.
    sim = _make_sim(tmp_path, monkeypatch, family=family, exe=exe)
    launched = []

    def _fake_run(cmd, cwd, stdout, stderr, timeout, terminate_signal, **kwargs):
        launched.append(list(cmd))
        stdout.write("PASS basic\n")
        return ManagedProcessResult(returncode=0)

    monkeypatch.setattr(
        vlog_sim_module, "task_status", lambda *args, **kwargs: nullcontext()
    )
    monkeypatch.setattr(vlog_sim_module, "run_managed_process", _fake_run)

    assert sim.execute(run_id=1) == 0
    [cmd] = launched
    assert cmd[0] == sim._get_simv_path()
    assert [a for a in cmd if a.startswith(("+define+", "-D"))] == [
        "+define+FOO=10",
        "+define+BAR",
    ]


def test_no_plusdefines_adds_nothing(tmp_path, monkeypatch):
    sim = _make_sim(
        tmp_path, monkeypatch, family="icarus", exe="iverilog", plusdefines=None
    )
    assert sim._get_compile_defines() == []
    cmd = _compile_cmd(sim, monkeypatch)
    assert not [arg for arg in cmd if arg.startswith(("-D", "+define+"))]


def test_icarus_compile_key_uses_the_compile_spelling(tmp_path, monkeypatch):
    # With sharing on, the build key and its fingerprint carry the arguments the
    # compile passes, and a test without the defines gets another build dir.
    sim = _make_sim(
        tmp_path, monkeypatch, family="icarus", exe="iverilog", share_build=True
    )
    plan = sim._build_compile_plan()
    assert plan.plusdefines == ["-DFOO=10", "-DBAR"]
    assert plan.key_cmd[-2:] == ["-DFOO=10", "-DBAR"]
    assert [a for a in plan.fingerprint["cmd"] if a.startswith(("-D", "+define+"))] == [
        "-DFOO=10",
        "-DBAR",
    ]
    assert plan.shared_dir is not None

    bare = _make_sim(
        tmp_path,
        monkeypatch,
        family="icarus",
        exe="iverilog",
        plusdefines=None,
        share_build=True,
    )._build_compile_plan()
    assert bare.shared_dir is not None and bare.shared_dir != plan.shared_dir


@pytest.fixture
def iverilog():
    exe = shutil.which("iverilog")
    if exe is None:
        pytest.skip("iverilog not on PATH")
    return exe


def test_iverilog_accepts_the_compile_spelling(tmp_path, monkeypatch, iverilog):
    # The argv rb builds for Icarus defines compiles under a real iverilog, and
    # both the valued and the valueless define reach the source.
    src = tmp_path / "top.sv"
    src.write_text(
        "module top;\n"
        "`ifndef BAR\n"
        "  BAR_not_defined u_missing();\n"
        "`endif\n"
        "  localparam int P = `FOO;\n"
        "endmodule\n"
    )
    sim = _make_sim(tmp_path, monkeypatch, family="icarus", exe=iverilog)

    def _iverilog(defines, out):
        return subprocess.run(
            [iverilog, "-g2012", "-o", str(out), *defines, str(src)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )

    # The `+define+` spelling on the command line is read as a missing source file.
    plus = _iverilog(sim._get_plusdefines(), tmp_path / "plus.vvp")
    assert plus.returncode != 0
    assert "+define+FOO=10" in plus.stderr + plus.stdout

    result = _iverilog(sim._get_compile_defines(), tmp_path / "a.vvp")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "a.vvp").is_file()
