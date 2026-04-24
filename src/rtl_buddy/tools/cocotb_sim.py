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


def _cocotb_share() -> str:
  result = subprocess.run(['cocotb-config', '--share'], capture_output=True, text=True)
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

  def _get_extra_compile_flags(self) -> list:
    share = _cocotb_share()
    verilator_cpp = str(Path(share) / 'lib' / 'verilator' / 'verilator.cpp')
    flags = ['--vpi', verilator_cpp]
    log_event(logger, logging.DEBUG, 'cocotb.compile_flags', test=self.test_name, flags=flags)
    return flags

  def _get_extra_sim_env(self, run_id=None) -> dict:
    cocotb_cfg = self.testbench.cocotb
    modules = ','.join(cocotb_cfg.get_modules())
    results_path = self._get_cocotb_results_path(run_id=run_id)
    env = {
      'MODULE': modules,
      'TOPLEVEL': self.testbench.toplevel,
      'TOPLEVEL_LANG': 'verilog',
      'COCOTB_RESULTS_FILE': results_path,
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
