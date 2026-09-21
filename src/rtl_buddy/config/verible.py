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

#: The binary whose presence stands for "this directory is a verible
#: install". Every rtl-buddy verible flow needs it.
_PROBE_BINARY = "verible-verilog-syntax"

#: ``(name, dir, exe)`` triples already reported through
#: ``verible.exe_fallback``. ``get_exe_path`` is called per lint/syntax
#: invocation, so the warning is deduplicated the same way
#: ``tool_path.unresolved_var`` is: the condition is static for the run.
_EXE_FALLBACK_WARNED: set[tuple[str, str, str]] = set()


def reset_exe_fallback_warnings() -> None:
    """Forget which ``verible.exe_fallback`` warnings were emitted (tests)."""
    _EXE_FALLBACK_WARNED.clear()


@dataclass
class VeribleConfig:
    """
    Configuration for running Verible within the test suite

    Attributes:
      name (str): Unique verible identifier.
      path (str): Path to the directory containing Verible executables.
      extra_args (dict[str, list[str]]): List of arguments to be supplied to verible, grouped by command.
      exclude (list[str]): Glob patterns of files dropped from ``--model``
        expansion (generated sources, third-party IP, ...).
    """

    name: str
    path: str
    extra_args: dict[str, list[str]]
    available: bool
    #: Glob patterns (fnmatch, matched against the project-root-relative
    #: path with ``/`` separators; ``*`` crosses directory boundaries) of
    #: files dropped from ``--model`` file expansion. Explicitly listed
    #: files are never filtered.
    exclude: list[str] = field(default_factory=list)

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

        The PATH fallback **warns** (once per binary): a configured
        directory that exists but does not hold this binary is the same
        silently-broken pin as a directory that does not exist at all, and
        it is the more common half of the case — the directory-level check
        in :meth:`VeribleConfigFile.initialise` never sees it (#439).

        Returns:
          path (str): The path.
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

        Args:
          root_cfg_path: Path to ``root_config.yaml``.
          diagnostics: Whether an unhonoured pin is worth a WARNING. Only
            true for the entry the *active* platform routes to. Every
            ``cfg-verible`` entry is initialised at load, so a project
            with one entry per platform would otherwise warn about the
            other platform's directory on every single invocation — a
            warning nobody on that host can act on, about a pin that is
            not being used (#439). Unrouted entries still record the
            same facts at DEBUG.
        """
        pin_level = logging.WARNING if diagnostics else logging.DEBUG
        base_dir = str(Path(root_cfg_path).parent)
        chosen = resolve_tool_path(
            self.path,
            base_dir=base_dir,
            block="cfg-verible",
            name=self.name,
            field="path",
            # `path` is a directory of binaries, not a binary: a
            # separator-free candidate is a relative directory next to
            # root_config.yaml, never a PATH lookup (#439).
            directory=True,
        )
        resolved = str(Path(base_dir) / chosen)
        res = VeribleConfig(
            self.name, resolved, self.extra_args, False, list(self.exclude)
        )
        if os.path.exists(resolved):
            res.available = True
            if not os.path.exists(os.path.join(resolved, _PROBE_BINARY)):
                # The directory is there but the binaries are not. Same
                # broken pin as a missing directory — and the one the
                # existence check above cannot see, so say it here rather
                # than leaving `get_exe_path` to discover it per command.
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
                # The configured directory is absent but verible is on PATH
                # (e.g. a site module load) — usable without editing the
                # committed path. WARNING, not DEBUG: a project that pinned
                # a directory deliberately has just had that pin silently
                # replaced by whatever PATH resolves, and the whole point of
                # pinning is that PATH cannot do that unannounced (#439).
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
