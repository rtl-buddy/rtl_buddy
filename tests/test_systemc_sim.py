"""Unit tests for SystemCSim — verilator --sc cosim runner."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rtl_buddy.config.systemc import SystemCConfig
from rtl_buddy.config.test import SystemCTestbenchConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.systemc_sim import SystemCSim


class _DummyBuilderCfg:
    def get_exe(self):
        return "verilator"

    def get_simv(self):
        return "simv"

    def get_seed(self):
        return 1

    def get_name(self):
        return "verilator"

    def get_simulator_family(self):
        return "verilator"

    def get_compile_time_opts(self, _m):
        return ["--binary", "-sv", "-o", "simv"]

    def get_run_time_opts(self, _m, seed=None):
        return []


class _DummyRootCfg:
    def __init__(self, systemc_cfg: SystemCConfig | None):
        self._systemc_cfg = systemc_cfg

    def get_rtl_builder_cfg(self):
        return _DummyBuilderCfg()

    def get_use_lcov(self, _):
        return False

    def get_systemc_cfg(self):
        return self._systemc_cfg


class _DummyTestbench:
    def __init__(self, sc_cfg: SystemCTestbenchConfig, toplevel="my_dut"):
        self.name = "tb_sc"
        self.toplevel = toplevel
        self.systemc = sc_cfg
        self.cocotb = None

    def get_filelist(self):
        return []

    def is_cocotb(self):
        return False

    def is_systemc(self):
        return True


class _DummyTestCfg:
    pd = None
    uvm = None

    def __init__(self, sc_cfg, toplevel="my_dut"):
        self._tb = _DummyTestbench(sc_cfg, toplevel=toplevel)

    def get_name(self):
        return "test_sc"

    def get_model(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            get_model_path=lambda: "/dev/null", get_filelist=lambda: []
        )

    def get_testbench(self):
        return self._tb

    def get_plusargs(self):
        return None

    def get_plusdefines(self):
        return {}

    def get_timeout(self):
        return 60, False

    def get_preproc_path(self):
        return None


def _make_sim(tmp_path, sc_cfg, *, systemc_cfg=None):
    return SystemCSim(
        name="rtl_buddy/systemc_sim",
        root_cfg=_DummyRootCfg(systemc_cfg),
        test_cfg=_DummyTestCfg(sc_cfg),
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
        suite_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Compile-flag emission
# ---------------------------------------------------------------------------


def test_filter_builder_opts_drops_binary(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    assert sim._filter_builder_opts(["--binary", "-sv", "-o", "simv"]) == [
        "-sv",
        "-o",
        "simv",
    ]


def test_compile_flags_include_sc_exe_build_and_top(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    flags = sim._get_extra_compile_flags()
    assert "--sc" in flags
    assert "--exe" in flags
    assert "--build" in flags
    assert flags[flags.index("--top-module") + 1] == "my_dut"


def test_compile_flags_resolve_sc_main_against_suite(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="harness/sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    flags = sim._get_extra_compile_flags()
    expected = str(Path(tmp_path) / "harness" / "sc_main.cpp")
    assert expected in flags


def test_compile_flags_include_sc_extra_resolved(tmp_path):
    sc = SystemCTestbenchConfig(
        sc_main="sc_main.cpp", sc_extra=["models/bus.cpp", "models/scoreboard.cpp"]
    )
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    flags = sim._get_extra_compile_flags()
    assert str(Path(tmp_path) / "models" / "bus.cpp") in flags
    assert str(Path(tmp_path) / "models" / "scoreboard.cpp") in flags


def test_compile_flags_embed_cflags_and_ldflags(tmp_path):
    sc = SystemCTestbenchConfig(
        sc_main="sc_main.cpp",
        cflags=["-std=c++17", "-DFOO=1"],
        ldflags=["-lpthread"],
    )
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    flags = sim._get_extra_compile_flags()

    cflags_value = flags[flags.index("-CFLAGS") + 1]
    assert "-I/opt/sc/include" in cflags_value
    assert "-std=c++17" in cflags_value
    assert "-DFOO=1" in cflags_value

    ldflags_value = flags[flags.index("-LDFLAGS") + 1]
    assert "-L/opt/sc/lib" in ldflags_value
    assert "-lsystemc" in ldflags_value
    assert "-lpthread" in ldflags_value


def test_root_cflags_apply_as_project_default(tmp_path):
    """Project-wide cflags from cfg-systemc apply even when the testbench
    omits its own cflags."""
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(
        tmp_path,
        sc,
        systemc_cfg=SystemCConfig(
            home="/opt/sc",
            cxx=None,
            cflags=["-std=c++17"],
            ldflags=["-lz"],
        ),
    )
    flags = sim._get_extra_compile_flags()
    assert "-std=c++17" in flags[flags.index("-CFLAGS") + 1]
    assert "-lz" in flags[flags.index("-LDFLAGS") + 1]


def test_testbench_cflags_append_to_root_cflags(tmp_path):
    """Per-testbench cflags layer above the root-level default, not replace it."""
    sc = SystemCTestbenchConfig(
        sc_main="sc_main.cpp",
        cflags=["-DTB_LOCAL=1"],
        ldflags=["-lpthread"],
    )
    sim = _make_sim(
        tmp_path,
        sc,
        systemc_cfg=SystemCConfig(
            home="/opt/sc",
            cxx=None,
            cflags=["-std=c++17"],
            ldflags=["-lz"],
        ),
    )
    flags = sim._get_extra_compile_flags()
    cflags_value = flags[flags.index("-CFLAGS") + 1]
    ldflags_value = flags[flags.index("-LDFLAGS") + 1]

    # Both layers present.
    assert "-std=c++17" in cflags_value
    assert "-DTB_LOCAL=1" in cflags_value
    assert "-lz" in ldflags_value
    assert "-lpthread" in ldflags_value

    # Root tokens appear before testbench tokens, so testbench can override
    # via "last wins" semantics on flags like -O2 / -DFOO.
    assert cflags_value.index("-std=c++17") < cflags_value.index("-DTB_LOCAL=1")
    assert ldflags_value.index("-lz") < ldflags_value.index("-lpthread")


def test_pin_style_uint_adds_pins_sc_uint(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp", pin_style="uint")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    assert "--pins-sc-uint" in sim._get_extra_compile_flags()


def test_pin_style_biguint_adds_pins_sc_biguint(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp", pin_style="biguint")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    assert "--pins-sc-biguint" in sim._get_extra_compile_flags()


def test_pin_style_bv_adds_no_pin_flag(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp", pin_style="bv")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    flags = sim._get_extra_compile_flags()
    assert not any(f.startswith("--pins-sc-") for f in flags)


# ---------------------------------------------------------------------------
# Env handling
# ---------------------------------------------------------------------------


def test_compile_env_exports_systemc_paths(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    env = sim._get_extra_compile_env()
    assert env["SYSTEMC_HOME"] == "/opt/sc"
    assert env["SYSTEMC_INCLUDE"] == "/opt/sc/include"
    assert env["SYSTEMC_LIBDIR"] == "/opt/sc/lib"
    assert "CXX" not in env


def test_compile_env_pins_cxx_when_configured(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(
        tmp_path,
        sc,
        systemc_cfg=SystemCConfig(home="/opt/sc", cxx="/usr/local/bin/g++-15"),
    )
    env = sim._get_extra_compile_env()
    assert env["CXX"] == "/usr/local/bin/g++-15"


def test_sim_env_adds_libdir_to_dynamic_loader_path(tmp_path, monkeypatch):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home="/opt/sc", cxx=None))
    monkeypatch.delenv("DYLD_FALLBACK_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)

    env = sim._get_extra_sim_env()
    key = (
        "DYLD_FALLBACK_LIBRARY_PATH"
        if os.uname().sysname == "Darwin"
        else "LD_LIBRARY_PATH"
    )
    assert env[key].startswith("/opt/sc/lib")


# ---------------------------------------------------------------------------
# Fail-fast paths
# ---------------------------------------------------------------------------


def test_missing_cfg_systemc_raises(tmp_path):
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=None)
    with pytest.raises(FatalRtlBuddyError, match="cfg-systemc"):
        sim._get_extra_compile_flags()


def test_missing_home_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEMC_HOME", raising=False)
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home=None, cxx=None))
    with pytest.raises(FatalRtlBuddyError, match="SYSTEMC_HOME"):
        sim._get_extra_compile_flags()


def test_home_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMC_HOME", "/from/env/systemc")
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(tmp_path, sc, systemc_cfg=SystemCConfig(home=None, cxx=None))
    env = sim._get_extra_compile_env()
    assert env["SYSTEMC_HOME"] == "/from/env/systemc"


def test_home_expands_variables_and_user(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SC_ROOT", "/expanded/systemc")
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(
        tmp_path, sc, systemc_cfg=SystemCConfig(home="${MY_SC_ROOT}", cxx=None)
    )
    env = sim._get_extra_compile_env()
    assert env["SYSTEMC_HOME"] == "/expanded/systemc"


def test_home_with_unresolved_var_falls_through_to_env(tmp_path, monkeypatch):
    """If the configured ${VAR} doesn't resolve, the env-var fallback still runs.

    Without this, os.path.expandvars would leave the literal "${VAR}" and
    SystemCSim would pass that to Verilator as a path, producing a confusing
    "include not found at ${SYSTEMC_HOME}/include" instead of the clean
    home_unresolved error.
    """
    monkeypatch.delenv("MY_CUSTOM_ROOT", raising=False)
    monkeypatch.setenv("SYSTEMC_HOME", "/fallback/sc")
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(
        tmp_path, sc, systemc_cfg=SystemCConfig(home="${MY_CUSTOM_ROOT}", cxx=None)
    )
    env = sim._get_extra_compile_env()
    assert env["SYSTEMC_HOME"] == "/fallback/sc"


def test_home_with_unresolved_var_and_no_fallback_raises(tmp_path, monkeypatch):
    """Unresolved ${VAR} + unset SYSTEMC_HOME → clean home_unresolved error,
    not a literal "${VAR}" propagated as a path."""
    monkeypatch.delenv("MY_CUSTOM_ROOT", raising=False)
    monkeypatch.delenv("SYSTEMC_HOME", raising=False)
    sc = SystemCTestbenchConfig(sc_main="sc_main.cpp")
    sim = _make_sim(
        tmp_path, sc, systemc_cfg=SystemCConfig(home="${MY_CUSTOM_ROOT}", cxx=None)
    )
    with pytest.raises(FatalRtlBuddyError, match="SYSTEMC_HOME"):
        sim._get_extra_compile_flags()
