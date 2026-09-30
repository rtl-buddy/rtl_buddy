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
from .toolpath import resolve_tool_path

logger = logging.getLogger(__name__)


@dataclass
class SurferConfig:
    """Settings for launching Surfer and its WCP client.

    `path` is already resolved by `resolve_tool_path`. `wcp_port` is the TCP port rtl-buddy listens on (Surfer connects with `--wcp-initiate`). `editor_cmd` substitutes `%f` (file) and `%l` (line). `editor_terminal` is "iterm2", "terminal" or "". `editor_sock` and `ctrl_sock` are Unix socket paths; empty disables. Relative paths resolve against `root_cfg_path`. `available` is set at initialise time.
    """

    name: str
    path: str
    wcp_port: int
    editor_cmd: str
    editor_terminal: str
    editor_sock: str
    ctrl_sock: str
    root_cfg_path: str
    available: bool

    def get_surfer_exe(self) -> str:
        """Return absolute path to the Surfer executable."""
        if os.sep in self.path or self.path.startswith("."):
            return os.path.join(os.path.dirname(self.root_cfg_path), self.path)
        return shutil.which(self.path) or self.path

    def _resolve_sock(self, sock: str) -> str:
        """Expand `~` and resolve a relative socket path against root_config.yaml."""
        if not sock:
            return sock
        sock = os.path.expanduser(sock)
        if not os.path.isabs(sock):
            sock = os.path.join(os.path.dirname(self.root_cfg_path), sock)
        return sock

    @property
    def resolved_editor_sock(self) -> str:
        return self._resolve_sock(self.editor_sock)

    @property
    def resolved_ctrl_sock(self) -> str:
        return self._resolve_sock(self.ctrl_sock)

    def format_editor_cmd(self, filepath: str, lineno: int) -> str:
        """Substitute %f and %l placeholders in editor_cmd."""
        return self.editor_cmd.replace("%f", filepath).replace("%l", str(lineno))

    def __str__(self):
        return pprint.pformat(self)


@serde
class SurferConfigFile:
    name: str
    path: str | list[str] = "surfer"
    wcp_port: int = field(
        rename="wcp-port", default=0
    )  # 0 = OS auto-assigns a free port
    editor_cmd: str = field(rename="editor-cmd", default="vim +%l %f")
    editor_terminal: str = field(rename="editor-terminal", default="")
    editor_sock: str = field(rename="editor-sock", default="")
    ctrl_sock: str = field(rename="ctrl-sock", default="")

    def initialise(self, root_cfg_path: str) -> SurferConfig:
        path = resolve_tool_path(
            self.path,
            base_dir=os.path.dirname(root_cfg_path),
            block="cfg-surfer",
            name=self.name,
            field="path",
        )
        cfg = SurferConfig(
            name=self.name,
            path=path,
            wcp_port=self.wcp_port,
            editor_cmd=self.editor_cmd,
            editor_terminal=self.editor_terminal,
            editor_sock=self.editor_sock,
            ctrl_sock=self.ctrl_sock,
            root_cfg_path=root_cfg_path,
            available=False,
        )
        if os.sep in path or path.startswith("."):
            exe = os.path.join(os.path.dirname(root_cfg_path), path)
            cfg.available = os.path.isfile(exe) and os.access(exe, os.X_OK)
        else:
            cfg.available = shutil.which(path) is not None
        if not cfg.available:
            log_event(
                logger,
                logging.DEBUG,
                "surfer.path_missing",
                name=cfg.name,
                path=path,
            )
        return cfg
