# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 Loh Zhi-Hern, Xie Jiacheng, Emil Goh, Damien Lim, Chai Wei Lynthia, Zubin Jain
#
import logging
from enum import Enum
logger = logging.getLogger(__name__)

from ..tools.vlog_sim import VlogSim
from ..seed_mode import SeedMode
from .test_results import *
from ..errors import FilelistError
from ..logging_utils import log_event

class RunDepth(Enum):
  PRE = "pre"
  COMP = "comp"
  SIM = "sim"
  POST = "post"

class TestRunner:

  def __init__(self, name, root_cfg, test_cfg, rtl_builder_mode, test_runner_mode, run_id=None, seed_mode:SeedMode=SeedMode.DEFAULT, replay_run_id=None, run_depth=None):
    """
    Run tests based on config
    Handles Verilog compilation
    """
    log_event(logger, logging.DEBUG, "test_runner.init", name=name, test=test_cfg.get_name(), run_id=run_id)
    self.name = name
    self.root_cfg = root_cfg
    self.test_cfg = test_cfg
    self.run_id = run_id
    self.seed_mode = seed_mode
    self.replay_run_id = replay_run_id
    self.run_depth = run_depth
    self.rtl_builder_mode = rtl_builder_mode
    self.test_runner_mode = test_runner_mode

  def _create_vlog_sim(self):
    sim_mode = {'sim_to_stdout': True}
    if 'sim_to_stdout' in self.test_runner_mode:
      sim_mode['sim_to_stdout'] = self.test_runner_mode['sim_to_stdout']

    return VlogSim(name=self.name+"/vlog_sim",
      root_cfg=self.root_cfg,
      test_cfg=self.test_cfg,
      rtl_builder_mode=self.rtl_builder_mode,
      sim_mode=sim_mode,
      run_id=self.run_id,
      replay_run_id=self.replay_run_id)

  def run(self):
    # compile simulation exe
    log_event(logger, logging.DEBUG, "test_runner.start", runner=self.name, test=self.test_cfg.get_name(), run_id=self.run_id)
    vlog_sim = self._create_vlog_sim()

    # run pre-proc python
    pre_error = vlog_sim.pre()
    if pre_error is not None:
      return SetupFailResults(name=self.name+"/results", desc=pre_error)

    if self.run_depth == RunDepth.PRE:
      log_event(logger, logging.INFO, "run.early_stop", test=self.test_cfg.get_name(), run_id=self.run_id, stage="preproc")
      return EarlyStopResults(name=self.name+"/results", desc='Stopped early at preproc')
  
    # compile sim executable
    try:
      compile_returncode = vlog_sim.compile()
    except FilelistError as e:
      return FilelistFailResults(name=self.name+"/results", desc=str(e))
    if (compile_returncode!=0):
      return CompileFailResults(name=self.name+"/results")

    if self.run_depth == RunDepth.COMP:
      log_event(logger, logging.INFO, "run.early_stop", test=self.test_cfg.get_name(), run_id=self.run_id, stage="compile")
      return EarlyStopResults(name=self.name+"/results", desc='Stopped early at compile')

    # run simulation
    execute_returncode = vlog_sim.execute(run_id=self.run_id, seed_mode=self.seed_mode, replay_run_id=self.replay_run_id)
    if execute_returncode == 4444:
      return SimTimeoutResults(name=self.name+"/results")

    if self.run_depth == RunDepth.SIM:
      log_event(logger, logging.INFO, "run.early_stop", test=self.test_cfg.get_name(), run_id=self.run_id, stage="sim")
      return EarlyStopResults(name=self.name+"/results", desc='Stopped early at sim')

    # run post-proc
    results = vlog_sim.post(run_id=self.run_id)
    return results

  def run_multiple(self, run_ids):
    """
    Execute one pre/compile flow and run multiple simulations over run_ids.

    run_id controls output naming for each simulation. seed_mode controls whether
    each run uses default seed, fresh random seed, or replayed seed.
    """
    log_event(logger, logging.DEBUG, "test_runner.start_multiple", runner=self.name, test=self.test_cfg.get_name(), run_ids=run_ids)
    vlog_sim = self._create_vlog_sim()

    pre_error = vlog_sim.pre()
    if pre_error is not None:
      return [SetupFailResults(name=self.name+"/results", desc=pre_error) for _ in run_ids]

    if self.run_depth == RunDepth.PRE:
      log_event(logger, logging.INFO, "run.early_stop", test=self.test_cfg.get_name(), stage="preproc", run_ids=run_ids)
      return [EarlyStopResults(name=self.name+"/results", desc='Stopped early at preproc') for _ in run_ids]

    try:
      compile_returncode = vlog_sim.compile()
    except FilelistError as e:
      return [FilelistFailResults(name=self.name+"/results", desc=str(e)) for _ in run_ids]
    if compile_returncode != 0:
      return [CompileFailResults(name=self.name+"/results") for _ in run_ids]

    if self.run_depth == RunDepth.COMP:
      log_event(logger, logging.INFO, "run.early_stop", test=self.test_cfg.get_name(), stage="compile", run_ids=run_ids)
      return [EarlyStopResults(name=self.name+"/results", desc='Stopped early at compile') for _ in run_ids]

    repeated_results = []
    for run_id in run_ids:
      replay_run_id = self.replay_run_id
      if self.seed_mode == SeedMode.REPLAY and replay_run_id is None:
        replay_run_id = run_id
      execute_returncode = vlog_sim.execute(run_id=run_id, seed_mode=self.seed_mode, replay_run_id=replay_run_id)
      if execute_returncode == 4444:
        repeated_results.append(SimTimeoutResults(name=self.name+"/results"))
      elif self.run_depth == RunDepth.SIM:
        log_event(logger, logging.INFO, "run.early_stop", test=self.test_cfg.get_name(), run_id=run_id, stage="sim")
        repeated_results.append(EarlyStopResults(name=self.name+"/results", desc='Stopped early at sim'))
      else:
        repeated_results.append(vlog_sim.post(run_id=run_id))

    return repeated_results
