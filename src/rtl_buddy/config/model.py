import logging
logger = logging.getLogger(__name__)
import pprint

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

@serde
class ModelConfig:
  """
  Representation of a single model entry in a 'model_config' file

  Attributes
    name (str): Unique model identifier.
    filelist (list[str]): List of paths to files associated with the model.
    path (str|None): Path to the model config file. Will usually be set by the loader.
  """
  name: str
  filelist: list[str]
  path: str|None
  
  def get_model_name(self):
    """
    Retrieve the value of model_name.

    Returns:
    model_name (str): The value of model_name in the model.
    """
    return self.model_name

  def get_model_path(self):
    """
    Retrieve the value of path.

    Returns:
    path (str): The value of path in the model. The path to the model config file.
    """
    return self.path

  def get_filelist(self):
    """
    Retrieve the value of filelist.

    Returns:
    filelist (list[str]): The value of filelist in the model.
    """
    return self.filelist

  def __str__(self):
    return pprint.pformat(self)

@serde
class ModelConfigFile:
  """
  Representation of a 'model_config' file.

  Attributes
    rtl_buddy_filetype (Literal['model_config']): Config file type. Must be 'model_config'.
    models (list[RawModelConfig]): List of model configurations.
  """
  rtl_buddy_filetype: Literal['model_config'] = field(rename='rtl-buddy-filetype')
  models: list[ModelConfig] = field(default_factory=list)

# TODO: Raise errors instead of killing things here
class ModelConfigLoader:
  """
  Helper class to load model configurations from a file. Reads the file once.

  Attributes:
    models(list[RawModelConfig]): List of raw model configs.
  """
  def __init__(self, path:str) -> None:
    self.path = path
    self.models = []
    
    try:
      with open(self.path, 'r') as file:
        data = from_yaml(ModelConfigFile, file.read())
        self.models = data.models
    except Exception as e:
      log_event(logger, logging.ERROR, "model_config.load_failed", path=path, error=e)
      raise FatalRtlBuddyError(f'failed to load "{path}"') from e
 
  def get_model(self, model_name:str) -> ModelConfig:
    """
    Get a ModelConfig according to model_name.

    Args:
      name (str): Unique system identifier for the model.
      model_name (str): Unique identifier for the model in file.
    Returns:
      model (ModelConfig): The model configuration.
    Raises:
      Panics if no model corresponding to model_name can be found.
    """
    for model in self.models:
      if model.name == model_name:
        model.path = self.path
        return model

    log_event(logger, logging.ERROR, "model_config.model_not_found", model=model_name, path=self.path)
    raise FatalRtlBuddyError(f"model '{model_name}' not found")
