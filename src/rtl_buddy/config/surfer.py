# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import logging
import os
import shutil
import pprint
from dataclasses import dataclass
from serde import serde, field
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


@dataclass
class SurferConfig:
  """
  Configuration for launching Surfer and its WCP client.

  Attributes:
    name (str): Unique identifier.
    path (str): Surfer executable path or bare name (resolved via PATH).
    wcp_port (int): TCP port rtl-buddy listens on; Surfer connects with --wcp-initiate.
    editor_cmd (str): Editor command template; %f = file path, %l = line number.
    editor_terminal (str): Terminal emulator for terminal editors ("iterm2", "terminal", or "").
    root_cfg_path (str): Path of root_config.yaml, used for relative path resolution.
    available (bool): True when the Surfer executable was found at initialise time.
  """
  name: str
  path: str
  wcp_port: int
  editor_cmd: str
  editor_terminal: str
  root_cfg_path: str
  available: bool

  def get_surfer_exe(self) -> str:
    """Return absolute path to the Surfer executable."""
    if os.sep in self.path or self.path.startswith('.'):
      return os.path.join(os.path.dirname(self.root_cfg_path), self.path)
    return shutil.which(self.path) or self.path

  def format_editor_cmd(self, filepath: str, lineno: int) -> str:
    """Substitute %f and %l placeholders in editor_cmd."""
    return self.editor_cmd.replace('%f', filepath).replace('%l', str(lineno))

  def __str__(self):
    return pprint.pformat(self)


@serde
class SurferConfigFile:
  name: str
  path: str = 'surfer'
  wcp_port: int = field(rename='wcp-port', default=0)  # 0 = OS auto-assigns a free port
  editor_cmd: str = field(rename='editor-cmd', default='vim +%l %f')
  editor_terminal: str = field(rename='editor-terminal', default='')

  def initialise(self, root_cfg_path: str) -> SurferConfig:
    cfg = SurferConfig(
      name=self.name,
      path=self.path,
      wcp_port=self.wcp_port,
      editor_cmd=self.editor_cmd,
      editor_terminal=self.editor_terminal,
      root_cfg_path=root_cfg_path,
      available=False,
    )
    if os.sep in self.path or self.path.startswith('.'):
      exe = os.path.join(os.path.dirname(root_cfg_path), self.path)
      cfg.available = os.path.isfile(exe) and os.access(exe, os.X_OK)
    else:
      cfg.available = shutil.which(self.path) is not None
    if not cfg.available:
      log_event(logger, logging.DEBUG, "surfer.path_missing", name=cfg.name, path=self.path)
    return cfg
