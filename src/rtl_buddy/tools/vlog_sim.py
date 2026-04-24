# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
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
from pathlib import Path

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
    self.suite_work_dir = os.path.abspath(os.getcwd())

    output_dir = Path(self.suite_work_dir) / "artefacts"
    output_dir.mkdir(parents=True, exist_ok=True)

    self.output_dir = str(output_dir)

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

  def _get_compile_work_dir(self):
    return self._get_artifact_dir()

  def _get_simv_path(self):
    """
    Return the simulator executable path for this test/build.
    """
    rtl_builder_exe = self.rtl_builder_cfg.get_exe()
    if os.path.basename(rtl_builder_exe).startswith("verilator"):
      return str(Path(self._get_compile_work_dir()) / self._get_build_dir() / "simv")
    simv_path = self.rtl_builder_cfg.get_simv()
    if os.path.isabs(simv_path):
      return simv_path
    return str(Path(self._get_compile_work_dir()) / simv_path)

  def _get_artifact_dir(self, run_id=None):
    artifact_dir = Path(self.output_dir) / self._get_build_tag()
    if run_id is not None:
      artifact_dir /= f"run-{run_id:04d}"
    return str(artifact_dir)

  def _ensure_artifact_dir(self, run_id=None):
    artifact_dir = Path(self._get_artifact_dir(run_id=run_id))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return str(artifact_dir)

  def _get_compile_transcript_path(self):
    return str(Path(self._get_compile_work_dir()) / "compile.log")

  def _get_filelist_path(self):
    return str(Path(self._get_compile_work_dir()) / "run.f")

  def _get_log_path(self, run_id=None):
    return str(Path(self._get_artifact_dir(run_id=run_id)) / "test.log")

  def _get_err_path(self, run_id=None):
    return str(Path(self._get_artifact_dir(run_id=run_id)) / "test.err")

  def _get_randseed_path(self, run_id=None):
    return str(Path(self._get_artifact_dir(run_id=run_id)) / "test.randseed")

  def _coverage_enabled(self):
    compile_opts = self.rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
    return any(opt.startswith("--coverage") for opt in compile_opts)

  def _get_simulator_family(self):
    """
    Return the canonical simulator family for backend-specific handling.
    """
    return self.rtl_builder_cfg.get_simulator_family()

  def _get_extra_compile_flags(self) -> list:
    return []

  def _get_extra_sim_env(self, run_id=None) -> dict:
    return {}

  def _get_cov_path(self, run_id=None):
    return str(Path(self._get_artifact_dir(run_id=run_id)) / "coverage.dat")

  def _get_cov_abspath(self, run_id=None):
    return str(Path(self._get_cov_path(run_id=run_id)).resolve())

  def _get_suite_symlink_path(self, name):
    return str(Path(self.suite_work_dir) / name)

  def _append_hier_instance_seed(self, randseed_fp, *, artifact_dir, run_cmd, test, run_id):
    if 'hier_inst_seed' not in run_cmd:
      return

    hier_seed_path = Path(artifact_dir) / "HierInstanceSeed.txt"
    if not hier_seed_path.exists():
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
    script_path = self.test_cfg.get_preproc_path()
    if script_path is None:
      log_event(logger, logging.DEBUG, "preproc.skipped", test=self.test_name)
      return None

    with open(script_path, 'r') as file:
      code = file.read()

    # Pass self.test_cfg to the preproc script as root_cfg
    # preproc script can mutate self.test_cfg, which is used for compile and sim
    ns = {
      "logger"   : logger, 
      "test_cfg" : self.test_cfg,
      "root_cfg" : self.root_cfg,
      "__file__" : os.path.abspath(script_path),
    }
    try:
      exec(code, ns)
    except Exception as e:
      log_event(logger, logging.ERROR, "preproc.failed", test=self.test_name, script=script_path, error=e)
      logger.debug("preproc traceback", exc_info=True)
      return f"Setup failed in preproc: {e}"

    log_event(logger, logging.INFO, "preproc.completed", test=self.test_name, script=script_path)
    return None

  def compile(self):
    rtl_builder_cfg = self.rtl_builder_cfg
    log_event(logger, logging.DEBUG, "compile.config", test=self.test_name, config=pprint.pformat(rtl_builder_cfg))
    compile_work_dir = self._ensure_artifact_dir()
    
    run_cmd = [ rtl_builder_cfg.get_exe() ]

    builder_opts = rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
    run_cmd += builder_opts

    if os.path.basename(rtl_builder_cfg.get_exe()).startswith("verilator"):
      run_cmd += ["--Mdir", self._get_build_dir()]

    run_cmd += self._get_extra_compile_flags()

    # add test plus-defines
    run_cmd += self._get_plusdefines()

    # Keep compile outputs in the suite work dir, but pass explicit paths so sim cwd can vary later.
    filelist_path = self._get_filelist_path()
    self._write_filelist(filelist_path)  # raises FilelistError on bad path; caught by TestRunner
    run_cmd += ["-f", filelist_path]
    run_str = " ".join(run_cmd)
    log_event(logger, logging.INFO, "compile.start", test=self.test_name, command=run_str, builder=rtl_builder_cfg.get_name())
    s_time = time.time()
    with task_status(f"Compiling {self.test_name}", spinner="dots12"):
      try:
        result = subprocess.run(run_cmd, capture_output=True, text=True, cwd=compile_work_dir)
      except FileNotFoundError:
        log_event(logger, logging.ERROR, "compile.builder_missing", test=self.test_name, executable=run_cmd[0])
        raise FatalRtlBuddyError(f'Builder not found. Run exe: {run_cmd[0]}')

    e_time = time.time()
    if result.returncode!=0:
      transcript_path = self._get_compile_transcript_path()
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
    artifact_dir = self._ensure_artifact_dir(run_id=run_id)
    log_path = self._get_log_path(run_id=run_id)
    err_path = self._get_err_path(run_id=run_id)
    randseed_path = self._get_randseed_path(run_id=run_id)

    run_cmd = [ self._get_simv_path() ]

    if seed_mode == SeedMode.REPLAY:
      seed_source_run_id = replay_run_id if replay_run_id is not None else run_id
      seed_source_path = self._get_randseed_path(run_id=seed_source_run_id)
      try:
        seed = int(open(seed_source_path).readline().strip())
      except (FileNotFoundError, ValueError):
        err_msg = f"Replay seed missing or invalid at {seed_source_path}"
        log_event(logger, logging.ERROR, "sim.replay_seed_missing", test=self.test_name, seed_path=seed_source_path)
        with open(log_path, "w+") as test_out_fp:
          test_out_fp.write("FAIL replay seed missing\n")
          test_out_fp.write(f"ERR: {err_msg}\n")
        with open(err_path, "w+") as test_err_fp:
          test_err_fp.write(err_msg + "\n")
        force_symlink(err_path, self._get_suite_symlink_path("test.err"))
        force_symlink(log_path, self._get_suite_symlink_path("test.log"))
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
      "log": log_path,
      "err": err_path,
      "randseed": randseed_path,
    }
    log_event(logger, logging.DEBUG, "sim.output_paths", test=self.test_name, run_id=run_id, **artifact_paths)
    s_time = time.time()
    t_time = 0

    # subprocess pipe stderr to test.err, stdout to test.log
    with task_status(f"Running simulation {self.test_name}{'' if run_id is None else f' #{run_id:04d}'}", spinner="dots12"):
      extra_env = self._get_extra_sim_env(run_id=run_id)
      sim_env = {**os.environ, **extra_env} if extra_env else None
      with open(err_path, "w+") as test_err_fp:
        with open(log_path, "w+") as test_out_fp:
          with subprocess.Popen(run_cmd, \
            preexec_fn=os.setpgrp,
            cwd=artifact_dir,
            env=sim_env,
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

    with open(randseed_path, "w") as f:
      f.write(str(seed) + '\n')
      self._append_hier_instance_seed(
        f,
        artifact_dir=artifact_dir,
        run_cmd=run_cmd,
        test=self.test_name,
        run_id=run_id,
      )

    force_symlink(err_path, self._get_suite_symlink_path("test.err"))
    force_symlink(log_path, self._get_suite_symlink_path("test.log"))
    force_symlink(randseed_path, self._get_suite_symlink_path("test.randseed"))

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
      self.vlog_post = UvmVlogPost(name=self.test_name, path=log_path, max_warns=self.test_cfg.uvm.max_warns, max_errors=self.test_cfg.uvm.max_errors)
    
    # default post-processing (VlogPost)
    else:  
      self.vlog_post = VlogPost(name=self.test_name, path=log_path)
    results = self.vlog_post.get_results()
    if self._coverage_enabled():
      cov = VlogCov(
        simulator_name=self._get_simulator_family(),
        use_lcov=self.root_cfg.get_use_lcov(self._get_simulator_family()),
        root_cfg=self.root_cfg,
      )
      cov_results = cov.collect(
        self._get_cov_abspath(run_id=run_id),
        source_roots=[self.suite_work_dir],
      )
      if cov_results is not None:
        results.results["coverage"] = cov_results.to_dict()
    log_event(logger, logging.INFO, "postproc.completed", test=self.test_name, run_id=run_id, result=results.results["result"], desc=results.results["desc"])
    return results
