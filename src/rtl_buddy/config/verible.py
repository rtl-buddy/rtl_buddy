import logging

logger = logging.getLogger(__name__)
import pprint
import os
import shutil
from pathlib import Path

from dataclasses import dataclass
from serde import serde
from ..logging_utils import log_event


@dataclass
class VeribleConfig:
    """
    Configuration for running Verible within the test suite

    Attributes:
      name (str): Unique verible identifier.
      path (str): Path to the directory containing Verible executables.
      extra_args (dict[str, list[str]]): List of arguments to be supplied to verible, grouped by command.
    """

    name: str
    path: str
    extra_args: dict[str, list[str]]
    available: bool

    def get_name(self):
        """
        Retrieve the value of name.

        Returns:
          name (str): The value of name.
        """
        return self.name

    def get_extra_args(self, cmd: str) -> list[str]:
        """
        Retrieve the extra_args associated with a command.

        Args:
          cmd (str): The command.
        Returns:
          extra_args (list[str]): The list of extra_args associated with the command. If none are found, returns an empty array.
        """
        return self.extra_args[cmd] if cmd in self.extra_args else []

    def get_exe_path(self, exe_name):
        """
        Retrieves the full path to a Verible executable.

        The configured ``path`` directory wins when it actually contains the
        executable. Otherwise fall back to PATH, so a site that exposes
        verible via ``module load`` / an env script (rather than the
        committed default directory) does not need to edit ``root_config.yaml``.
        The configured join is returned as a last resort so a genuine
        "not found" error still points at the expected location.

        Returns:
          path (str): The path.
        """
        candidate = os.path.join(self.path, exe_name)
        if os.path.exists(candidate):
            return candidate
        return shutil.which(exe_name) or candidate

    def __str__(self):
        return pprint.pformat(self)


@serde
class VeribleConfigFile:
    name: str
    path: str
    extra_args: dict[str, list[str]]

    def initialise(self, root_cfg_path: str) -> VeribleConfig:
        resolved = str(Path(root_cfg_path).parent / self.path)
        res = VeribleConfig(self.name, resolved, self.extra_args, False)
        if os.path.exists(resolved):
            res.available = True
        elif shutil.which("verible-verilog-syntax"):
            # configured dir absent, but verible is on PATH (e.g. a site
            # module load) — usable without editing the committed path.
            res.available = True
            log_event(
                logger,
                logging.DEBUG,
                "verible.path_fallback",
                name=res.get_name(),
                path=resolved,
            )
        else:
            log_event(
                logger,
                logging.DEBUG,
                "verible.path_missing",
                name=res.get_name(),
                path=resolved,
            )

        return res
