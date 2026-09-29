import logging

logger = logging.getLogger(__name__)
import pprint
import os
import shutil
from pathlib import Path

from dataclasses import dataclass, field
from serde import serde
from ..logging_utils import log_event
from .toolpath import resolve_tool_path

#: Binary whose presence marks a directory as a verible install.
_PROBE_BINARY = "verible-verilog-syntax"

#: ``(name, dir, exe)`` triples already warned about; ``get_exe_path`` runs per invocation.
_EXE_FALLBACK_WARNED: set[tuple[str, str, str]] = set()


def reset_exe_fallback_warnings() -> None:
    """Clear the warned-once set (tests)."""
    _EXE_FALLBACK_WARNED.clear()


@dataclass
class VeribleConfig:
    """Verible settings: `path` is the directory of executables; `extra_args` maps a command to its extra arguments."""

    name: str
    path: str
    extra_args: dict[str, list[str]]
    available: bool
    #: fnmatch globs on the project-root-relative path (``*`` crosses ``/``) dropped from ``--model`` expansion; explicitly listed files are never filtered.
    exclude: list[str] = field(default_factory=list)

    def get_name(self):
        return self.name

    def get_extra_args(self, cmd: str) -> list[str]:
        """Return the extra arguments for `cmd`, or an empty list."""
        return self.extra_args[cmd] if cmd in self.extra_args else []

    def get_exe_path(self, exe_name):
        """Return the path to a Verible executable.

        Prefers the configured directory, then PATH (warning once per binary), then the configured join so a "not found" error names the expected location.
        """
        candidate = os.path.join(self.path, exe_name)
        if os.path.exists(candidate):
            return candidate
        on_path = shutil.which(exe_name)
        if on_path:
            key = (self.name, self.path, exe_name)
            if key not in _EXE_FALLBACK_WARNED:
                _EXE_FALLBACK_WARNED.add(key)
                log_event(
                    logger,
                    logging.WARNING,
                    "verible.exe_fallback",
                    name=self.name,
                    exe=exe_name,
                    configured_path=candidate,
                    resolved_path=on_path,
                )
            return on_path
        return candidate

    def __str__(self):
        return pprint.pformat(self)


@serde
class VeribleConfigFile:
    name: str
    path: str | list[str]
    extra_args: dict[str, list[str]]
    exclude: list[str] = field(default_factory=list)

    def initialise(
        self, root_cfg_path: str, *, diagnostics: bool = True
    ) -> VeribleConfig:
        """Resolve this entry against the root-config directory.

        `diagnostics` is true only for the entry the active platform routes to; other entries log an unhonoured pin at DEBUG instead of WARNING.
        """
        pin_level = logging.WARNING if diagnostics else logging.DEBUG
        base_dir = str(Path(root_cfg_path).parent)
        chosen = resolve_tool_path(
            self.path,
            base_dir=base_dir,
            block="cfg-verible",
            name=self.name,
            field="path",
            # A separator-free candidate is a directory next to root_config.yaml, not a PATH lookup.
            directory=True,
        )
        resolved = str(Path(base_dir) / chosen)
        res = VeribleConfig(
            self.name, resolved, self.extra_args, False, list(self.exclude)
        )
        if os.path.exists(resolved):
            res.available = True
            if not os.path.exists(os.path.join(resolved, _PROBE_BINARY)):
                # Directory exists but lacks the binaries.
                log_event(
                    logger,
                    pin_level,
                    "verible.path_incomplete",
                    name=res.get_name(),
                    configured_path=resolved,
                    exe=_PROBE_BINARY,
                    resolved_path=shutil.which(_PROBE_BINARY) or "",
                )
        else:
            on_path = shutil.which(_PROBE_BINARY)
            if on_path:
                # Directory absent but verible is on PATH: usable, but the pin was replaced, so warn.
                res.available = True
                log_event(
                    logger,
                    pin_level,
                    "verible.path_fallback",
                    name=res.get_name(),
                    configured_path=resolved,
                    resolved_path=on_path,
                )
                return res

            log_event(
                logger,
                logging.DEBUG,
                "verible.path_missing",
                name=res.get_name(),
                path=resolved,
            )

        return res
