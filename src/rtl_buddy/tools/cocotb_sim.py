# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import logging
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_sim import VlogSim
from ..runner.test_results import TestResults
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


def _cocotb_config(*args) -> str:
  try:
    result = subprocess.run(['cocotb-config', *args], capture_output=True, text=True)
  except FileNotFoundError:
    raise FatalRtlBuddyError('cocotb-config not found; is cocotb installed in this environment?')
  if result.returncode != 0:
    raise FatalRtlBuddyError('cocotb-config not found; is cocotb installed in this environment?')
  return result.stdout.strip()


class CocotbSim(VlogSim):
  """
  cocotb simulation — Verilator + Python testbench via VPI.

  Extends VlogSim with cocotb VPI compile flags, runtime env vars,
  and JUnit XML result parsing.
  """

  def _get_cocotb_results_path(self, run_id=None) -> str:
    return str(Path(self._get_artifact_dir(run_id=run_id)) / 'cocotb_results.xml')

  def _filter_builder_opts(self, opts: list) -> list:
    return [o for o in opts if o != '--binary']

  def _get_extra_compile_flags(self) -> list:
    share = _cocotb_config('--share')
    lib_dir = _cocotb_config('--lib-dir')
    vpi_lib = _cocotb_config('--lib-name-path', 'vpi', 'verilator')
    libpython = _cocotb_config('--libpython')
    verilator_cpp = str(Path(share) / 'lib' / 'verilator' / 'verilator.cpp')
    ldflags = f'-Wl,-rpath,{lib_dir} {vpi_lib} {libpython}'
    flags = ['--cc', '--exe', verilator_cpp, '--build', '--timing', '--vpi', '--public-flat-rw', '--prefix', 'Vtop', '-LDFLAGS', ldflags]
    log_event(logger, logging.DEBUG, 'cocotb.compile_flags', test=self.test_name, flags=flags)
    return flags

  def _get_extra_sim_env(self, run_id=None) -> dict:
    import os
    cocotb_cfg = self.testbench.cocotb
    modules = ','.join(cocotb_cfg.get_modules())
    results_path = self._get_cocotb_results_path(run_id=run_id)

    lib_dir = _cocotb_config('--lib-dir')
    libpython = _cocotb_config('--libpython')
    libpython_dir = str(Path(libpython).parent)

    # suite_work_dir so cocotb can import the test module
    existing_pythonpath = os.environ.get('PYTHONPATH', '')
    pythonpath_parts = [self.suite_work_dir] + ([existing_pythonpath] if existing_pythonpath else [])

    # macOS: help dynamic linker find Homebrew libpython and cocotb libs
    existing_dyld = os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '')
    dyld_parts = [libpython_dir, lib_dir] + ([existing_dyld] if existing_dyld else [])

    env = {
      'COCOTB_TEST_MODULES': modules,
      'COCOTB_TOPLEVEL': self.testbench.toplevel,
      'COCOTB_TOPLEVEL_LANG': 'verilog',
      'COCOTB_RESULTS_FILE': results_path,
      'PYTHONPATH': ':'.join(pythonpath_parts),
      'DYLD_FALLBACK_LIBRARY_PATH': ':'.join(dyld_parts),
      'LIBPYTHON_LOC': libpython,
      'PYGPI_PYTHON_BIN': _cocotb_config('--python-bin'),
    }
    log_event(logger, logging.DEBUG, 'cocotb.sim_env', test=self.test_name, run_id=run_id,
              module=modules, toplevel=self.testbench.toplevel, results_file=results_path)
    return env

  def post(self, run_id=None):
    run_id = self.run_id if run_id is None else run_id
    results_path = self._get_cocotb_results_path(run_id=run_id)

    if not Path(results_path).exists():
      log_event(logger, logging.WARNING, 'cocotb.results_missing',
                test=self.test_name, run_id=run_id, path=results_path)
      return TestResults(name=self.test_name,
                         results={'result': 'FAIL', 'desc': f'cocotb results file not found: {results_path}'})

    try:
      tree = ET.parse(results_path)
      root = tree.getroot()
    except ET.ParseError as e:
      return TestResults(name=self.test_name,
                         results={'result': 'FAIL', 'desc': f'cocotb results XML parse error: {e}'})

    failures = []
    total = 0
    for testcase in root.iter('testcase'):
      total += 1
      name = testcase.get('name', 'unknown')
      for bad in testcase.findall('failure') + testcase.findall('error'):
        failures.append(f"{name}: {bad.get('message', '').strip()}")

    log_event(logger, logging.INFO, 'cocotb.results_parsed',
              test=self.test_name, run_id=run_id, total=total, failures=len(failures))

    if not failures:
      return TestResults(name=self.test_name,
                         results={'result': 'PASS', 'desc': f'{total} cocotb test(s) passed'})

    desc = '; '.join(failures[:3])
    if len(failures) > 3:
      desc += f' (+{len(failures) - 3} more)'
    return TestResults(name=self.test_name, results={'result': 'FAIL', 'desc': desc})
