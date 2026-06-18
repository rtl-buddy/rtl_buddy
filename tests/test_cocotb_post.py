import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.config.test import CocotbTestbenchConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.cocotb_sim import CocotbSim


# ---------------------------------------------------------------------------
# Minimal XML fixtures representing cocotb JUnit output
# ---------------------------------------------------------------------------

XML_ALL_PASS = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="tb">
        <testcase name="test_a" />
        <testcase name="test_b" />
      </testsuite>
    </testsuites>
""")

XML_ONE_FAILURE = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="tb">
        <testcase name="test_a" />
        <testcase name="test_b">
          <failure message="assertion failed at line 42" />
        </testcase>
      </testsuite>
    </testsuites>
""")

XML_ONE_ERROR = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="tb">
        <testcase name="test_c">
          <error message="SimTimeoutError after 100ns" />
        </testcase>
      </testsuite>
    </testsuites>
""")

XML_MIXED = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="tb">
        <testcase name="test_pass" />
        <testcase name="test_fail">
          <failure message="mismatch" />
        </testcase>
        <testcase name="test_err">
          <error message="timeout" />
        </testcase>
      </testsuite>
    </testsuites>
""")

# Nested suites — root.iter('testcase') must recurse into both
XML_NESTED_SUITES = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="suite_a">
        <testcase name="test_a1" />
      </testsuite>
      <testsuite name="suite_b">
        <testcase name="test_b1">
          <failure message="fail in suite_b" />
        </testcase>
      </testsuite>
    </testsuites>
""")

# failure/error as grandchild — findall is direct-child only, so this must
# NOT be counted; if cocotb ever wraps them, the test will catch it.
XML_DEEP_FAILURE = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="tb">
        <testcase name="test_a">
          <system-out>
            <failure message="should not be counted" />
          </system-out>
        </testcase>
      </testsuite>
    </testsuites>
""")

XML_MALFORMED = "<?xml this is not valid"


# ---------------------------------------------------------------------------
# Fixture: a CocotbSim instance with all collaborators stubbed out
# ---------------------------------------------------------------------------


class _DummyCocotbCfg:
    def get_modules(self):
        return ["test_mod"]


class _DummyTestbench:
    toplevel = "my_dut"
    cocotb = _DummyCocotbCfg()

    def get_filelist(self):
        return []

    def is_cocotb(self):
        return True


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
        return []

    def get_run_time_opts(self, _m, seed=None):
        return []


class _DummyRootCfg:
    def get_rtl_builder_cfg(self):
        return _DummyBuilderCfg()

    def resolve_rtl_builder_cfg(self, _test_builder_name=None):
        return _DummyBuilderCfg()

    def get_use_lcov(self, _):
        return False


class _DummyTestCfg:
    pd = None
    uvm = None

    def get_name(self):
        return "test_cocotb"

    def get_builder_name(self):
        return None

    def get_model(self):
        return SimpleNamespace(
            get_model_path=lambda: "/dev/null", get_filelist=lambda: []
        )

    def get_testbench(self):
        return _DummyTestbench()

    def get_plusargs(self):
        return None

    def get_plusdefines(self):
        return {}

    def get_timeout(self):
        return 60, False

    def get_preproc_path(self):
        return None


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return CocotbSim(
        name="rtl_buddy/cocotb_sim",
        root_cfg=_DummyRootCfg(),
        test_cfg=_DummyTestCfg(),
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
    )


def _write_results(sim, xml: str, run_id=None):
    path = Path(sim._get_cocotb_results_path(run_id=run_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_post_all_pass(sim):
    _write_results(sim, XML_ALL_PASS)
    r = sim.post()
    assert r.results["result"] == "PASS"
    assert "2" in r.results["desc"]


def test_post_one_failure(sim):
    _write_results(sim, XML_ONE_FAILURE)
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "test_b" in r.results["desc"]
    assert "assertion failed at line 42" in r.results["desc"]


def test_post_one_error(sim):
    _write_results(sim, XML_ONE_ERROR)
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "test_c" in r.results["desc"]
    assert "SimTimeoutError" in r.results["desc"]


def test_post_mixed_failure_and_error(sim):
    _write_results(sim, XML_MIXED)
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "test_fail" in r.results["desc"]
    assert "test_err" in r.results["desc"]


def test_post_recurses_into_nested_suites(sim):
    _write_results(sim, XML_NESTED_SUITES)
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "fail in suite_b" in r.results["desc"]


def test_post_does_not_count_deep_failure_grandchildren(sim):
    # failure wrapped in system-out is not a direct child of testcase
    _write_results(sim, XML_DEEP_FAILURE)
    r = sim.post()
    assert r.results["result"] == "PASS"


def test_post_missing_results_file(sim):
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "not found" in r.results["desc"]


def test_post_malformed_xml(sim):
    _write_results(sim, XML_MALFORMED)
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "parse error" in r.results["desc"]


def test_post_truncates_desc_beyond_three_failures(sim):
    cases = "".join(
        f'<testcase name="t{i}"><failure message="msg{i}" /></testcase>'
        for i in range(5)
    )
    xml = f'<testsuites><testsuite name="tb">{cases}</testsuite></testsuites>'
    _write_results(sim, xml)
    r = sim.post()
    assert r.results["result"] == "FAIL"
    assert "+2 more" in r.results["desc"]


# ---------------------------------------------------------------------------
# CocotbTestbenchConfig.get_modules
# ---------------------------------------------------------------------------


def test_get_modules_str_returns_single_element_list():
    cfg = CocotbTestbenchConfig(module="test_foo")
    assert cfg.get_modules() == ["test_foo"]


def test_get_modules_list_returns_list_unchanged():
    cfg = CocotbTestbenchConfig(module=["test_foo", "test_bar"])
    assert cfg.get_modules() == ["test_foo", "test_bar"]


# ---------------------------------------------------------------------------
# Simulator-family dispatch for compile flags / opt filtering
# ---------------------------------------------------------------------------


def _make_sim(tmp_path, monkeypatch, family, compile_opts):
    """A CocotbSim whose builder reports the given family + compile opts.

    cocotb-config is stubbed so these tests run without cocotb installed.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "rtl_buddy.tools.cocotb_sim._cocotb_config",
        lambda *args: f"<{'-'.join(args)}>",
    )

    builder = _DummyBuilderCfg()
    builder.get_simulator_family = lambda: family
    builder.get_compile_time_opts = lambda _m: list(compile_opts)

    root = _DummyRootCfg()
    root.get_rtl_builder_cfg = lambda: builder
    root.resolve_rtl_builder_cfg = lambda _n=None: builder

    return CocotbSim(
        name="rtl_buddy/cocotb_sim",
        root_cfg=root,
        test_cfg=_DummyTestCfg(),
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
    )


@pytest.fixture()
def icarus_sim(tmp_path, monkeypatch):
    """A cocotb CocotbSim on the Icarus builder, with cocotb-config stubbed
    to a known lib-dir so the VPI-load assertions are deterministic."""
    monkeypatch.chdir(tmp_path)
    from rtl_buddy.tools import cocotb_sim as cocotb_sim_module

    def _fake_cocotb_config(*args):
        if args == ("--lib-dir",):
            return "/fake/cocotb/libs"
        return "/fake/cocotb"

    monkeypatch.setattr(cocotb_sim_module, "_cocotb_config", _fake_cocotb_config)

    builder = _DummyBuilderCfg()
    builder.get_exe = lambda: "iverilog"
    builder.get_name = lambda: "icarus"
    builder.get_simulator_family = lambda: "icarus"
    builder.get_compile_time_opts = lambda _m: ["-g2012"]

    root = _DummyRootCfg()
    root.get_rtl_builder_cfg = lambda: builder
    root.resolve_rtl_builder_cfg = lambda _n=None: builder

    return CocotbSim(
        name="rtl_buddy/cocotb_sim",
        root_cfg=root,
        test_cfg=_DummyTestCfg(),
        rtl_builder_mode="sim",
        sim_mode={"sim_to_stdout": True},
    )


def test_verilator_compile_flags_and_binary_filter(tmp_path, monkeypatch):
    sim = _make_sim(tmp_path, monkeypatch, "verilator", ["--binary", "-sv"])
    flags = sim._get_extra_compile_flags()
    assert "--cc" in flags and "--exe" in flags and "--vpi" in flags
    # --binary is dropped (cocotb uses --exe + verilator.cpp main)
    assert sim._filter_builder_opts(["--binary", "-sv"]) == ["-sv"]


def test_vcs_compile_flags_added(tmp_path, monkeypatch):
    sim = _make_sim(tmp_path, monkeypatch, "vcs", ["-sverilog", "-full64", "+vpi"])
    flags = sim._get_extra_compile_flags()
    assert "-load" in flags
    assert flags[flags.index("-load") + 1] == "<--lib-name-path-vpi-vcs>"
    assert "+acc+3" in flags
    assert "-debug_access+all" in flags
    assert "-LDFLAGS" in flags and "-Wl,--no-as-needed" in flags
    # toplevel is elaborated as top
    assert flags[flags.index("-top") + 1] == "my_dut"
    # VCS keeps builder opts intact (no --binary filtering)
    assert sim._filter_builder_opts(["-sverilog", "+vpi"]) == ["-sverilog", "+vpi"]


def test_vcs_does_not_duplicate_existing_flags(tmp_path, monkeypatch):
    sim = _make_sim(
        tmp_path,
        monkeypatch,
        "vcs",
        ["-sverilog", "-debug_access+all+class", "+acc+rw", "-top", "my_dut"],
    )
    flags = sim._get_extra_compile_flags()
    assert "-debug_access+all" not in flags  # already covered by +class variant
    assert "+acc+3" not in flags  # builder already enables +acc
    assert "-top" not in flags  # builder already pins the top
    assert "-load" in flags  # the VPI shim is always injected


def test_vcs_dedup_is_token_level_not_substring(tmp_path, monkeypatch):
    # An opt that merely *contains* "-top" as a substring must NOT suppress
    # toplevel elaboration (token-level membership, per review feedback).
    sim = _make_sim(tmp_path, monkeypatch, "vcs", ["-sverilog", "+define+X_top_Y"])
    flags = sim._get_extra_compile_flags()
    assert flags[flags.index("-top") + 1] == "my_dut"


def test_unsupported_family_raises(tmp_path, monkeypatch):
    # questa is not among the families cocotb can drive via a VPI shim here.
    sim = _make_sim(tmp_path, monkeypatch, "questa", [])
    with pytest.raises(FatalRtlBuddyError, match="cocotb is not supported"):
        sim._get_extra_compile_flags()


# ---------------------------------------------------------------------------
# cocotb on Icarus backend dispatch
# ---------------------------------------------------------------------------


def test_icarus_compile_flags_are_empty(icarus_sim):
    # iverilog needs no cocotb-specific compile flags; the VPI module is
    # loaded at run time, not linked at compile time like Verilator.
    assert icarus_sim._get_extra_compile_flags() == []


def test_icarus_vvp_extra_args_load_cocotb_vpi(icarus_sim):
    args = icarus_sim._icarus_vvp_extra_args()
    assert args == ["-M", "/fake/cocotb/libs", "-m", "libcocotbvpi_icarus"]


def test_icarus_simv_wrapper_embeds_cocotb_vpi_flags(icarus_sim):
    # The vvp flags must precede the snapshot in the generated wrapper.
    icarus_sim._ensure_artifact_dir()
    icarus_sim._write_icarus_simv_wrapper()
    wrapper_text = Path(icarus_sim._get_simv_path()).read_text()
    assert "-M /fake/cocotb/libs" in wrapper_text
    assert "-m libcocotbvpi_icarus" in wrapper_text
    snapshot = icarus_sim._get_icarus_snapshot_path()
    # -M/-m appear before the snapshot path (vvp option ordering requirement).
    assert wrapper_text.index("libcocotbvpi_icarus") < wrapper_text.index(snapshot)
