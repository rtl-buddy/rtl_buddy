import logging
logger = logging.getLogger(__name__)
import pprint

from serde import serde, field
import re

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

def process_opts(opts):
  return re.sub(r'\s+', ' ', opts).split(' ')

@serde
class RtlBuilderConfigOpts:
  """
  Lists of command-line options for a single builder.

  Attributes:
    compile_time (list[str] | None): Compile-time options
    run_time (list[str] | None): Run-time options
  """
  compile_time: list[str] | None = field(rename='compile-time', deserializer=process_opts)
  run_time: list[str] | None = field(rename='run-time', deserializer=process_opts)

@serde
class RtlBuilderConfig:
  """
  Configuration for a RTL Builder.

  Attributes:
    name (str): Unique builder identifier.
    simulator_family (str | None): Simulator family identifier used for
      backend-specific behavior such as coverage processing.
    exe (str): Name of the compiler executable (without location).
    simv (str): Name of the executable file for simulation (on disc).
    sim_rand_seed (int): Random seed for the simulation.
    sim_rand_prefix (str): Simulator-specific prefix for the random seed.
    opts (dict[str, RtlBuilderConfigOpts]): Command-line options for the builder, keyed by mode.
  """
  name: str
  exe: str = field(rename='builder')
  simv: str = field(rename='builder-simv')
  sim_rand_seed: int = field(rename='sim-rand-seed')
  sim_rand_prefix: str = field(rename='sim-rand-seed-prefix')
  opts: dict[str, RtlBuilderConfigOpts] = field(rename='builder-opts')
  simulator_family: str | None = field(rename='simulator-family', default=None)

  def get_name(self) -> str:
    """
    Retrieves the value of name.

    Returns:
      name (str): The value of name.
    """
    return self.name

  def get_simulator_family(self) -> str:
    """
    Retrieve the simulator family for backend-specific handling.

    Returns:
      family (str): Canonical simulator family, e.g. "verilator" or "vcs".
    """
    if self.simulator_family is not None:
      return self.simulator_family

    exe_base = self.exe.split()[0].split("/")[-1].lower()
    if exe_base.startswith("verilator"):
      return "verilator"
    if exe_base.startswith("vcs"):
      return "vcs"
    return exe_base

  def get_exe(self) -> str:
    """
    Retrieves the value of exe.

    Returns:
      exe (str): The value of exe.
    """
    return self.exe

  def get_simv(self) -> str:
    """
    Retrieves the value of simv.

    Returns:
      simv (str): The value of simv.
    """
    return self.simv

  def get_seed(self) -> int:
    """
    Retrieves the value of sim_rand_seed.

    Returns:
      seed (int): The value of sim_rand_seed.
    """
    return self.sim_rand_seed

  def get_modes(self) -> list[str]:
    """
    Retrieves a list of available builder modes.

    Returns:
      modes (list[str]): The list of available modes.
    """
    return self.opts.keys()

  def get_compile_time_opts(self, mode:str) -> list[str]:
    """
    Retrieves the compile time options for a given mode.

    Args:
      mode (str): The requested mode.
    Returns:
      opts (list[str]): The list of options.
    """
    if mode not in self.opts:
      log_event(logger, logging.ERROR, "builder.mode_missing", builder=self.name, mode=mode, stage="compile")
      raise FatalRtlBuddyError(f'Requested mode "{mode}" not in config')

    if self.opts[mode].compile_time is None:
      log_event(logger, logging.ERROR, "builder.stage_missing", builder=self.name, mode=mode, stage="compile-time")
      raise FatalRtlBuddyError(f'Requested stage "compile-time" not in config "{mode}"')

    return list(self.opts[mode].compile_time)
  
  def get_run_time_opts(self, mode:str, seed:int|None=None) -> list[str]:
    """
    Retrieves the run time options for a given mode.

    Args:
      mode (str): The requested mode.
      seed (int|None) [None]: An optional seed to append to the list of options.
    Returns:
      opts (list[str]): The list of options.
    """
    if mode not in self.opts:
      log_event(logger, logging.ERROR, "builder.mode_missing", builder=self.name, mode=mode, stage="run")
      raise FatalRtlBuddyError(f'Requested mode "{mode}" not in config')

    if self.opts[mode].run_time is None:
      log_event(logger, logging.ERROR, "builder.stage_missing", builder=self.name, mode=mode, stage="run-time")
      raise FatalRtlBuddyError(f'Requested stage "run-time" not in config "{mode}"')

    opts = list(self.opts[mode].run_time)
    if seed is not None:
      opts.append(self.sim_rand_prefix + str(seed))
    return opts

  def __str__(self):
    return pprint.pformat(self)
