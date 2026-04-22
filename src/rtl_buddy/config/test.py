import logging
from dataclasses import dataclass
from serde import serde, field
from .model import ModelConfig, ModelConfigLoader
from .uvm import UVMConfig

import pprint
import os

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

@serde
class TestbenchConfig:
  """
  Configuration for a single testbench within a test suite.

  Attributes:
    name (str): Unique testbench identifier.
    filelist (list[str]): List of paths to files involved in running the testbench.
  """
  name: str
  filelist: list[str]

  def get_name(self):
    """
    Retrieve the value of name

    Returns:
    name (str): The name of the testbench
    """
    return self.name

  def get_filelist(self):
    """
    Retrieve the value of filelist

    Returns:
    filelist(list[str]): The value of filelist in the testbench
    """
    return self.filelist

  def __str__(self):
    return pprint.pformat(self)

@dataclass
class TestConfig:
  """
  Configuration for a single test within a test suite.
  
  Encapsulates all test-specific settings loaded from tests.yaml, including
  required metadata, optional runtime parameters, and sweep/pre/post processing
  scripts. Supports mutable properties for dynamic test expansion during execution.
  
  Attributes:
    name (str): Unique test identifier.
    desc (str): Human-readable test description.
    model (str): RTL model name from models.yaml.
    model_path (str): Path to models.yaml.
    tb_name (str): Name of the testbench to use for this test.
    tb (TestbenchConfig): Associated testbench configuration object.
    _reglvl (int | dict | None): Regression level(s) controlling test execution.
      Can be a uniform int, a dict with builder-specific levels, or None (defaults to 0).
    pa (dict | None): Plusargs dict passed to simulator at runtime.
    pd (dict | None): Plusdefines dict passed to simulator at runtime.
    uvm (UVMConfig | None): If defined, enables UVM report parsing in post.
    sweep_path (str | None): Path to sweep expansion Python script (expands one test into many).
    preproc_path (str | None): Path to pre-processing Python script (runs before compile).
    postproc_path (str | None): Path to post-processing Python script (runs after simulation).
  """

  name: str
  desc: str
  model: ModelConfig
  _reglvl: int | dict | None
  pa: dict | None
  pd: dict | None
  uvm: UVMConfig | None
  preproc_path: str | None
  postproc_path: str | None
  sweep_path: str | None
  tb: TestbenchConfig
  timeout: int | None
  default_timeout: int = 60 #NOTE: potential for config through root config

  def get_name(self):
    """
    Retrieve the value of name

    Returns:
    name (str): The name of the test
    """
    return self.name

  def get_model(self):
    """
    Retrieve the value of model

    Returns:
    model (ModelConfig): The value of model in the test
    """
    return self.model

  def get_testbench(self):
    """
    Retrieve the testbench configuration associated with this test.
    
    Returns:
      TestbenchConfig: Testbench configuration containing HDL filelist.
    """
    return self.tb

  def get_plusarg(self, key):
    """
    Return the value of the plusarg from the plusargs dictionary
    
    Args:
      key (str): Plusarg name (e.g., 'NUM_ITERATIONS').
    Returns:
      value (str | int | float): Plusarg value.
    """
    return self.pa.get(key)

  def get_plusargs(self):
    """
    Retrieve the current plusargs dictionary for simulator runtime.
    
    Returns:
      dict | None: Dict of plusargs, or None if not specified.
    """
    return self.pa

  def set_plusarg(self, key, value):
    """
    Set or update a single plusarg for simulator runtime.
    
    Lazily initializes the plusargs dict if not already present.
    Used by pre-processing scripts to inject vlog runtime plusargs dynamically.
    
    Args:
      key (str): Plusarg name (e.g., 'NUM_ITERATIONS').
      value (str | int | float): Plusarg value.
    """
    if self.pa is None:
      self.pa = {}
    self.pa[key] = value

  def set_plusargs(self, new_args):
    """
    Set or update multiple plusargs for simulator runtime.
    
    Merges the provided dictionary with the existing plusargs dict.
    Lazily initializes the plusargs dict if not already present.
    Used by pre-processing scripts to inject multiple vlog runtime plusargs dynamically.
    
    Args:
      new_args (dict): Dictionary of plusarg key-value pairs to merge.
    """
    if self.pa is None:
      self.pa = {}
    self.pa.update(new_args)

  def get_plusdefine(self, key):
    """
    Return the value of the plusdefine from the plusdefine dictionary
    
    Args:
      key (str): Plusdefine name (e.g., 'NUM_ITERATIONS').
    Returns:
      value (str | int | float): Plusdefine value.
    """
    return self.pd.get(key)

  def get_plusdefines(self):
    """
    Retrieve the current plusdefines dictionary for simulator runtime.
    
    Returns:
      dict | None: Dict of plusdefines, or None if not specified.
    """
    return self.pd

  def set_plusdefine(self, key, value):
    """
    Set or update a single plusdefine for simulator runtime.
    
    Lazily initializes the plusdefines dict if not already present.
    Used by pre-processing scripts to inject vlog plusdefines dynamically.
    
    Args:
      key (str): Define name (e.g., 'WIDTH').
      value (str | int): Define value.
    """
    if self.pd is None:
      self.pd = {}
    self.pd[key] = value

  def set_plusdefines(self, new_defines):
    """
    Set or update multiple plusdefines for simulator runtime.
    
    Merges the provided dictionary with the existing plusdefines dict.
    Lazily initializes the plusdefines dict if not already present.
    Used by pre-processing scripts to inject multiple vlog plusdefines dynamically.
    
    Args:
      new_defines (dict): Dictionary of plusdefine key-value pairs to merge.
    """
    if self.pd is None:
      self.pd = {}
    self.pd.update(new_defines)
  
  def get_timeout(self):
    """
    Retrieve the simulation timeout for this test in seconds.

    Returns:
      timeout (int): Simulation timeout in seconds.
      is_custom (bool): Whether this value is custom set.
    """
    is_custom = self.timeout is not None
    return self.timeout if is_custom else self.default_timeout, is_custom

  def set_timeout(self, timeout):
    """
    Set the simulation timeout for this test in seconds

    Args:
      timeout (int): Simulation timeout in seconds
    """
    self.timeout = timeout

  def get_sweep_path(self):
    """
    Retrieve the path to the sweep expansion script.
    
    Sweep scripts expand a single test configuration into multiple test variants,
    enabling parametric testing across design configurations.
    
    Returns:
      str | None: Absolute path to sweep script, or None if not specified.
    """
    return self.sweep_path

  def get_preproc_path(self):
    """
    Retrieve the path to the pre-processing Python script.
    
    Pre-processing runs before compilation to allow dynamic modification of
    TestConfig object.
    
    Returns:
      str | None: Absolute path to pre-proc script, or None if not specified.
    """
    return self.preproc_path

  def get_postproc_path(self):
    """
    Retrieve the path to the post-processing Python script.
    
    Post-processing runs after simulation for an external script to do any
    checking needed.
    
    Returns:
      str | None: Absolute path to post-proc script, or None if not specified.
    """
    return self.postproc_path

  def get_reglvl(self, builder):
    """
    Get the regression level for this test on a specific builder.
    
    Regression level controls test execution filtering:
    - Tests are skipped if reglvl falls outside the --reg-level range
    - Enables tiered testing: e.g. critical tests (reglvl=0) → longer tests (reglvl=5000)
    
    Args:
      builder (str): Builder name defined in root_config.yaml (e.g., 'verilator', 'vcs').
    
    Returns:
      int: Regression level for this builder. Resolves in order:
        1. Builder-specific level from reglvl dict
        2. Default level from reglvl dict if present
        3. Uniform reglvl if specified as int
        4. 0 if reglvl not specified
    
    Raises:
      SystemExit: If reglvl is malformed, e.g. missing default.
    """
    match self._reglvl:
      case int() as lvl:
        reglvl = lvl
      case dict() if builder in self._reglvl:
        reglvl = self._reglvl[builder]
      case dict() if 'default' in self._reglvl:
        reglvl = self._reglvl['default']
      case None:
        reglvl = 0
      case _:
        log_event(logger, logging.ERROR, "test_config.reglvl_malformed", test=self.name, builder=builder)
        raise FatalRtlBuddyError(f'Malformed tests.yaml, specify reglvl for {self.name} with {builder} or default')

    return reglvl

  def __str__(self):
    return pprint.pformat(self)

@serde
class TestConfigFile:
  name: str
  desc: str
  model: str
  model_path: str
  _reglvl: int | dict | None = field(rename='reglvl')
  pa: dict | None = field(rename='plusargs')
  pd: dict | None = field(rename='plusdefines')
  uvm: UVMConfig | None
  preproc_path: str | None = field(rename='preproc', deserializer=lambda data: data.get('path') if data is not None else None)
  postproc_path: str | None = field(rename='postproc', deserializer=lambda data: data.get('path') if data is not None else None)
  sweep_path: str | None = field(rename='sweep', deserializer=lambda data: data.get('path') if data is not None else None)
  tb: str = field(rename='testbench')
  timeout: int | None = field(rename='sim_timeout')

  def initialise(self, config_dir, tbs):
    tb = tbs[self.tb]
    model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(self.model)
    return TestConfig(self.name, self.desc, model, self._reglvl, self.pa, self.pd, self.uvm, self.preproc_path, self.postproc_path, self.sweep_path, tb, self.timeout)
