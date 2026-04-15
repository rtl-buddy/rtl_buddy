# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 Loh Zhi-Hern, Xie Jiacheng, Emil Goh, Damien Lim, Chai Wei Lynthia, Zubin Jain
#
"""
vlog_sim module handles verilog simulations for rtl-buddy

"""
import os
import random
import signal
import logging
logger = logging.getLogger(__name__)
import re
from ..seed_mode import SeedMode

from .vlog_filelist import VlogFilelist
from .vlog_post import VlogPost
from .vlog_post import UvmVlogPost
from .vlog_cov import VlogCov

import time
import pprint
import subprocess

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status

def force_symlink(target, link_name):
  if os.path.lexists(link_name):
    os.remove(link_name)

  os.symlink(target, link_name)

class VlogSim:
  """
  Verilog Sim Compile and Execution
  """

  # TODO: Replace suite_cfg, test_name with test_info and testbench
  def __init__(self, name, root_cfg, test_cfg, rtl_builder_mode, sim_mode, run_id=None, replay_run_id=None):
    """
    compile and execute sim for given test
    """
    self.name = name
    self.root_cfg = root_cfg
    self.rtl_builder_cfg = root_cfg.get_rtl_builder_cfg()
    self.rtl_builder_mode = rtl_builder_mode
    self.sim_mode = sim_mode
    # assert 'sim_to_stdout' in self.sim_mode NOTE: not used anywhere, may or may not become important in the future
    self.test_cfg = test_cfg
    self.test_name = self.test_cfg.get_name()
    self.run_id = run_id
    self.replay_run_id = replay_run_id
    self.testbench = self.test_cfg.get_testbench()
    self.vlog_post = None

    output_dir = "logs"
    if not os.path.exists(output_dir):
      os.makedirs(output_dir)

    self.output_dir = output_dir

  def _get_build_tag(self):
    """
    Return a filesystem-safe tag derived from the test name.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", self.test_name)

  def _get_build_dir(self):
    """
    Return the simulator build directory for this test.
    """
    return f"obj_dir_{self._get_build_tag()}"

  def _get_simv_path(self):
    """
    Return the simulator executable path for this test/build.
    """
    rtl_builder_exe = self.rtl_builder_cfg.get_exe()
    if os.path.basename(rtl_builder_exe).startswith("verilator"):
      return f"{self._get_build_dir()}/simv"
    return self.rtl_builder_cfg.get_simv()

  def _get_log_path(self, run_id=None):
    log_path = f"{self.output_dir}/{self.test_name}"
    if run_id is not None:
      log_path += f"_{run_id:04d}"     # Append run_id if test rand
    return log_path

  def _coverage_enabled(self):
    compile_opts = self.rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
    return any(opt.startswith("--coverage") for opt in compile_opts)

  def _get_simulator_family(self):
    """
    Return the canonical simulator family for backend-specific handling.
    """
    return self.rtl_builder_cfg.get_simulator_family()

  def _get_cov_path(self, run_id=None):
    cov_path = f"{self.output_dir}/{self.test_name}"
    if run_id is not None:
      cov_path += f"_{run_id:04d}"
    cov_path += ".coverage.dat"
    return cov_path

  def _get_cov_abspath(self, run_id=None):
    return os.path.abspath(self._get_cov_path(run_id=run_id))

  def _append_hier_instance_seed(self, randseed_fp, *, run_cmd, test, run_id):
    if 'hier_inst_seed' not in run_cmd:
      return

    hier_seed_path = "HierInstanceSeed.txt"
    if not os.path.exists(hier_seed_path):
      log_event(
        logger,
        logging.WARNING,
        "sim.hier_seed_missing",
        test=test,
        run_id=run_id,
        seed_path=hier_seed_path,
      )
      return

    with open(hier_seed_path, 'r') as instance_seeds:
      for line in instance_seeds:
        randseed_fp.write(line)

  def _write_filelist(self, output_path):
    """
    generate run.f for sim
    """
    self.vlog_fl = VlogFilelist(name = self.name + '/vlog_filelist', model_cfg = self.test_cfg.get_model(), output_path = output_path)
    self.vlog_fl.write_output(unroll=True, flatten=False, strip=False, deduplicate=True, test_filelist=self.testbench.get_filelist())

  def _get_plusargs(self):
    pa_list = []
    if self.test_cfg.get_plusargs() is not None:
      plusargs = self.test_cfg.get_plusargs()
      log_event(logger, logging.DEBUG, "sim.plusargs", test=self.test_name, plusargs=plusargs)
      for plusarg in plusargs:
        if plusargs[plusarg]!=None:
          pa_list += [ f"+{plusarg}={plusargs[plusarg]}" ]
        else:
          pa_list += [ f"+{plusarg}" ]
    return pa_list

  def _get_plusdefines(self):
    pd_list = []
    if self.test_cfg.pd is not None:
      plusdefines = self.test_cfg.get_plusdefines()
      log_event(logger, logging.DEBUG, "compile.plusdefines", test=self.test_name, plusdefines=plusdefines)
      for plusdefine in plusdefines:
        if plusdefines[plusdefine]!=None:
          pd_list += [ f"+define+{plusdefine}={plusdefines[plusdefine]}" ]
        else:
          pd_list += [ f"+define+{plusdefine}" ]
    return pd_list
    
  def pre(self):
    if self.test_cfg.get_preproc_path() is None:
      log_event(logger, logging.DEBUG, "preproc.skipped", test=self.test_name)
      return None

    with open(self.test_cfg.get_preproc_path(), 'r') as file:
      code = file.read()

    # Pass self.test_cfg to the preproc script as root_cfg
    # preproc script can mutate self.test_cfg, which is used for compile and sim
    ns = {
      "logger"   : logger, 
      "test_cfg" : self.test_cfg,
      "root_cfg" : self.root_cfg
    }
    try:
      exec(code, ns)
    except Exception as e:
      log_event(logger, logging.ERROR, "preproc.failed", test=self.test_name, script=self.test_cfg.get_preproc_path(), error=e)
      logger.debug("preproc traceback", exc_info=True)
      return f"Setup failed in preproc: {e}"

    log_event(logger, logging.INFO, "preproc.completed", test=self.test_name, script=self.test_cfg.get_preproc_path())
    return None

  def compile(self):
    rtl_builder_cfg = self.rtl_builder_cfg
    log_event(logger, logging.DEBUG, "compile.config", test=self.test_name, config=pprint.pformat(rtl_builder_cfg))
    
    run_cmd = [ rtl_builder_cfg.get_exe() ]

    builder_opts = rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
    run_cmd += builder_opts

    if os.path.basename(rtl_builder_cfg.get_exe()).startswith("verilator"):
      run_cmd += ["--Mdir", self._get_build_dir()]

    # add test plus-defines
    run_cmd += self._get_plusdefines()

    # generate run.f for sim
    self._write_filelist("run.f")  # raises FilelistError on bad path; caught by TestRunner
    run_cmd += ["-f", "run.f"]
    run_str = " ".join(run_cmd)
    log_event(logger, logging.INFO, "compile.start", test=self.test_name, command=run_str, builder=rtl_builder_cfg.get_name())
    s_time = time.time()
    with task_status(f"Compiling {self.test_name}", spinner="dots12"):
      try:
        result = subprocess.run(run_cmd, capture_output=True, text=True)
      except FileNotFoundError:
        log_event(logger, logging.ERROR, "compile.builder_missing", test=self.test_name, executable=run_cmd[0])
        raise FatalRtlBuddyError(f'Builder not found. Run exe: {run_cmd[0]}')

    e_time = time.time()
    if result.returncode!=0:
      transcript_path = f"{self.output_dir}/{self.test_name}.compile.log"
      with open(transcript_path, "w") as transcript_fp:
        transcript_fp.write(f"Command: {run_str}\n\n")
        transcript_fp.write("=== stderr ===\n")
        transcript_fp.write(result.stderr or "")
        transcript_fp.write("\n=== stdout ===\n")
        transcript_fp.write(result.stdout or "")
      log_event(
        logger,
        logging.ERROR,
        "compile.failed",
        test=self.test_name,
        returncode=result.returncode,
        duration_sec=round(e_time - s_time, 2),
        transcript=transcript_path,
      )
    else:
      log_event(
        logger,
        logging.INFO,
        "compile.completed",
        test=self.test_name,
        duration_sec=round(e_time - s_time, 2),
      )
      if result.stdout:
        logger.debug("compile stdout\n%s", result.stdout)
    return result.returncode

  def execute(self, run_id=None, seed_mode:SeedMode=SeedMode.DEFAULT, replay_run_id=None):
    """
    Run vlog simulation executable.

    run_id controls run-indexed output naming. seed_mode controls how the seed is
    selected:
      - "default": use builder-config seed
      - "new": generate a fresh random seed
      - "replay": read seed from a previous run's .randseed file
    """
    run_id = self.run_id if run_id is None else run_id
    replay_run_id = self.replay_run_id if replay_run_id is None else replay_run_id
    log_path = self._get_log_path(run_id=run_id)

    run_cmd = [ self._get_simv_path() ]

    if seed_mode == SeedMode.REPLAY:
      seed_source_run_id = replay_run_id if replay_run_id is not None else run_id
      seed_source_path = self._get_log_path(run_id=seed_source_run_id)
      try:
        seed = int(open(f"{seed_source_path}.randseed").readline().strip())
      except (FileNotFoundError, ValueError):
        err_msg = f"Replay seed missing or invalid at {seed_source_path}.randseed"
        log_event(logger, logging.ERROR, "sim.replay_seed_missing", test=self.test_name, seed_path=f"{seed_source_path}.randseed")
        with open(f"{log_path}.log", "w+") as test_out_fp:
          test_out_fp.write("FAIL replay seed missing\n")
          test_out_fp.write(f"ERR: {err_msg}\n")
        with open(f"{log_path}.err", "w+") as test_err_fp:
          test_err_fp.write(err_msg + "\n")
        force_symlink(f"{log_path}.err", "test.err")
        force_symlink(f"{log_path}.log", "test.log")
        return 1

    elif seed_mode == SeedMode.NEW:
      seed = random.randrange(1000000) 
      log_event(logger, logging.INFO, "sim.seed_generated", test=self.test_name, run_id=run_id, seed=seed)

    else:
      seed = self.rtl_builder_cfg.get_seed()

    # add test plus-defines
    run_cmd += self.rtl_builder_cfg.get_run_time_opts(self.rtl_builder_mode, seed=seed)

    run_cmd += self._get_plusdefines()

    # add test runtime args
    run_cmd += self._get_plusargs()

    if self._coverage_enabled() and self._get_simulator_family() == "verilator":
      run_cmd += [f"+verilator+coverage+file+{self._get_cov_abspath(run_id=run_id)}"]

    run_str = " ".join(run_cmd)
    log_event(logger, logging.INFO, "sim.start", test=self.test_name, run_id=run_id, seed=seed, command=run_str)

    timeout, is_custom = self.test_cfg.get_timeout()
    if is_custom:
      log_event(logger, logging.INFO, "sim.timeout_override", test=self.test_name, run_id=run_id, timeout_sec=timeout)
    artifact_paths = {
      "log": f"{log_path}.log",
      "err": f"{log_path}.err",
      "randseed": f"{log_path}.randseed",
    }
    log_event(logger, logging.DEBUG, "sim.output_paths", test=self.test_name, run_id=run_id, **artifact_paths)
    s_time = time.time()
    t_time = 0

    # subprocess pipe stderr to test.err, stdout to test.log
    with task_status(f"Running simulation {self.test_name}{'' if run_id is None else f' #{run_id:04d}'}", spinner="dots12"):
      with open(f"{log_path}.err", "w+") as test_err_fp:
        with open(f"{log_path}.log", "w+") as test_out_fp:
          with subprocess.Popen(run_cmd, \
            preexec_fn=os.setpgrp,
            stdout=test_out_fp,
            stderr=test_err_fp) as process:
              def signal_handler(_no, _frame):
                process.send_signal(signal.SIGQUIT)
                raise KeyboardInterrupt
              signal.signal(signal.SIGINT, signal_handler)

              returncode = None
              try:
                returncode = process.wait(timeout)
              except subprocess.TimeoutExpired:
                pass

              t_time = time.time() - s_time
              if returncode == None:
                process.send_signal(signal.SIGQUIT)
                returncode = 4444
                log_event(
                  logger,
                  logging.ERROR,
                  "sim.timeout",
                  test=self.test_name,
                  run_id=run_id,
                  timeout_sec=timeout,
                  **artifact_paths,
                )

    with open(f"{log_path}.randseed", "w") as f:
      f.write(str(seed) + '\n')
      self._append_hier_instance_seed(f, run_cmd=run_cmd, test=self.test_name, run_id=run_id)

    force_symlink(f"{log_path}.err", "test.err")
    force_symlink(f"{log_path}.log", "test.log")
    force_symlink(f"{log_path}.randseed", "test.randseed")

    if returncode!=0:
      log_event(
        logger,
        logging.ERROR,
        "sim.failed",
        test=self.test_name,
        run_id=run_id,
        returncode=returncode,
        duration_sec=round(t_time, 2),
        **artifact_paths,
      )
    else:
      log_event(logger, logging.INFO, "sim.completed", test=self.test_name, run_id=run_id, duration_sec=round(t_time, 2))

    return returncode

  def post(self, run_id=None):
    """
    post-process vlog test output to determine test results
    return TestResult
    """

    run_id = self.run_id if run_id is None else run_id
    log_path = self._get_log_path(run_id=run_id)

    if self.test_cfg.uvm:
      self.vlog_post = UvmVlogPost(name=self.test_name, path=f"{log_path}.log", max_warns=self.test_cfg.uvm.max_warns, max_errors=self.test_cfg.uvm.max_errors)
    
    # default post-processing (VlogPost)
    else:  
      self.vlog_post = VlogPost(name=self.test_name, path=f"{log_path}.log")
    results = self.vlog_post.get_results()
    if self._coverage_enabled():
      cov = VlogCov(
        simulator_name=self._get_simulator_family(),
        use_lcov=self.root_cfg.get_use_lcov(self._get_simulator_family()),
        root_cfg=self.root_cfg,
      )
      cov_results = cov.collect(
        self._get_cov_abspath(run_id=run_id),
        source_roots=[os.getcwd()],
      )
      if cov_results is not None:
        results.results["coverage"] = cov_results.to_dict()
    log_event(logger, logging.INFO, "postproc.completed", test=self.test_name, run_id=run_id, result=results.results["result"], desc=results.results["desc"])
    return results
