import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtl_buddy.config.test import CocotbTestbenchConfig
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
    return ['test_mod']


class _DummyTestbench:
  toplevel = 'my_dut'
  cocotb = _DummyCocotbCfg()

  def get_filelist(self):
    return []

  def is_cocotb(self):
    return True


class _DummyBuilderCfg:
  def get_exe(self):      return 'verilator'
  def get_simv(self):     return 'simv'
  def get_seed(self):     return 1
  def get_name(self):     return 'verilator'
  def get_simulator_family(self): return 'verilator'
  def get_compile_time_opts(self, _m): return []
  def get_run_time_opts(self, _m, seed=None): return []


class _DummyRootCfg:
  def get_rtl_builder_cfg(self): return _DummyBuilderCfg()
  def get_use_lcov(self, _): return False


class _DummyTestCfg:
  pd = None
  uvm = None

  def get_name(self):        return 'test_cocotb'
  def get_model(self):       return SimpleNamespace(get_model_path=lambda: '/dev/null', get_filelist=lambda: [])
  def get_testbench(self):   return _DummyTestbench()
  def get_plusargs(self):    return None
  def get_plusdefines(self): return {}
  def get_timeout(self):     return 60, False
  def get_preproc_path(self): return None


@pytest.fixture()
def sim(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  return CocotbSim(
    name='rtl_buddy/cocotb_sim',
    root_cfg=_DummyRootCfg(),
    test_cfg=_DummyTestCfg(),
    rtl_builder_mode='sim',
    sim_mode={'sim_to_stdout': True},
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
  assert r.results['result'] == 'PASS'
  assert '2' in r.results['desc']


def test_post_one_failure(sim):
  _write_results(sim, XML_ONE_FAILURE)
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert 'test_b' in r.results['desc']
  assert 'assertion failed at line 42' in r.results['desc']


def test_post_one_error(sim):
  _write_results(sim, XML_ONE_ERROR)
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert 'test_c' in r.results['desc']
  assert 'SimTimeoutError' in r.results['desc']


def test_post_mixed_failure_and_error(sim):
  _write_results(sim, XML_MIXED)
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert 'test_fail' in r.results['desc']
  assert 'test_err' in r.results['desc']


def test_post_recurses_into_nested_suites(sim):
  _write_results(sim, XML_NESTED_SUITES)
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert 'fail in suite_b' in r.results['desc']


def test_post_does_not_count_deep_failure_grandchildren(sim):
  # failure wrapped in system-out is not a direct child of testcase
  _write_results(sim, XML_DEEP_FAILURE)
  r = sim.post()
  assert r.results['result'] == 'PASS'


def test_post_missing_results_file(sim):
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert 'not found' in r.results['desc']


def test_post_malformed_xml(sim):
  _write_results(sim, XML_MALFORMED)
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert 'parse error' in r.results['desc']


def test_post_truncates_desc_beyond_three_failures(sim):
  cases = ''.join(
    f'<testcase name="t{i}"><failure message="msg{i}" /></testcase>'
    for i in range(5)
  )
  xml = f'<testsuites><testsuite name="tb">{cases}</testsuite></testsuites>'
  _write_results(sim, xml)
  r = sim.post()
  assert r.results['result'] == 'FAIL'
  assert '+2 more' in r.results['desc']


# ---------------------------------------------------------------------------
# CocotbTestbenchConfig.get_modules
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# VCS simulator-awareness: compile flags, filter, and sim args
# ---------------------------------------------------------------------------

class _VcsBuilderCfg(_DummyBuilderCfg):
  def get_exe(self):              return 'vcs'
  def get_simv(self):             return 'simv'
  def get_name(self):             return 'vcs'
  def get_simulator_family(self): return 'vcs'
  def get_compile_time_opts(self, _m): return ['--binary', '+v2k']


class _VcsRootCfg:
  def get_rtl_builder_cfg(self): return _VcsBuilderCfg()
  def get_use_lcov(self, _):     return False


@pytest.fixture()
def vcs_sim(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  return CocotbSim(
    name='rtl_buddy/cocotb_sim_vcs',
    root_cfg=_VcsRootCfg(),
    test_cfg=_DummyTestCfg(),
    rtl_builder_mode='sim',
    sim_mode={'sim_to_stdout': True},
  )


def _fake_cocotb_config(*args):
  table = {
    ('--share',):                             '/cocotb/share',
    ('--lib-dir',):                           '/cocotb/libs',
    ('--lib-name-path', 'vpi', 'verilator'):  '/cocotb/libs/libcocotbvpi_verilator.so',
    ('--lib-name-path', 'vpi', 'vcs'):        '/cocotb/libs/libcocotbvpi_vcs.so',
    ('--libpython',):                         '/usr/lib/libpython3.11.so',
    ('--python-bin',):                        '/usr/bin/python3',
  }
  return table[args]


def test_vcs_filter_builder_opts_passes_binary_through(vcs_sim):
  opts = ['--binary', '+v2k', '-sverilog']
  assert vcs_sim._filter_builder_opts(opts) == opts


def test_verilator_filter_builder_opts_strips_binary(sim):
  opts = ['--binary', '-sv', '+define+X']
  result = sim._filter_builder_opts(opts)
  assert '--binary' not in result
  assert '-sv' in result


def test_vcs_compile_flags_enable_vpi_and_pli_table(vcs_sim, monkeypatch):
  import rtl_buddy.tools.cocotb_sim as mod
  monkeypatch.setattr(mod, '_cocotb_config', _fake_cocotb_config)
  flags = vcs_sim._get_extra_compile_flags()
  assert '+vpi' in flags
  assert '-P' in flags
  assert '/cocotb/share/lib/vcs/pli.tab' in flags
  assert '+define+COCOTB_SIM=1' in flags
  # VCS must not include Verilator-specific flags
  assert '--timing' not in flags
  assert '--prefix' not in flags
  assert '-LDFLAGS' not in flags


def test_vcs_compile_flags_have_no_verilator_cpp(vcs_sim, monkeypatch):
  import rtl_buddy.tools.cocotb_sim as mod
  monkeypatch.setattr(mod, '_cocotb_config', _fake_cocotb_config)
  flags = vcs_sim._get_extra_compile_flags()
  assert not any('verilator.cpp' in f for f in flags)


def test_vcs_sim_args_load_vpi_library(vcs_sim, monkeypatch):
  import rtl_buddy.tools.cocotb_sim as mod
  monkeypatch.setattr(mod, '_cocotb_config', _fake_cocotb_config)
  args = vcs_sim._get_extra_sim_args()
  assert len(args) == 1
  assert 'libcocotbvpi_vcs.so' in args[0]
  assert 'vlog_startup_routines_bootstrap' in args[0]


def test_verilator_sim_args_is_empty(sim, monkeypatch):
  import rtl_buddy.tools.cocotb_sim as mod
  monkeypatch.setattr(mod, '_cocotb_config', _fake_cocotb_config)
  assert sim._get_extra_sim_args() == []


# ---------------------------------------------------------------------------
# CocotbTestbenchConfig.get_modules
# ---------------------------------------------------------------------------

def test_get_modules_str_returns_single_element_list():
  cfg = CocotbTestbenchConfig(module='test_foo')
  assert cfg.get_modules() == ['test_foo']


def test_get_modules_list_returns_list_unchanged():
  cfg = CocotbTestbenchConfig(module=['test_foo', 'test_bar'])
  assert cfg.get_modules() == ['test_foo', 'test_bar']
