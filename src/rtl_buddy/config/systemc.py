"""Optional ``cfg-systemc`` block in ``root_config.yaml``.

Pins the SystemC install (``home``), an optional C++ compiler (``cxx``) and project-wide ``cflags`` / ``ldflags``, which per-testbench ``systemc.cflags`` / ``systemc.ldflags`` in tests.yaml extend. The include and library paths derived from ``home`` are added automatically, so ``cflags`` and ``ldflags`` need not repeat them.

``home`` resolves from the config value (``~`` and ``$VAR`` expanded), then ``$SYSTEMC_HOME``, then None. When it is None, SystemCSim logs ``systemc.home_unresolved`` and fails a SystemC testbench.
"""

import os
import pprint
import re
from dataclasses import dataclass, field

from serde import serde


# expandvars leaves an unset variable in the output instead of raising.
_UNRESOLVED_VAR_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class SystemCConfig:
    """Resolved SystemC config consumed by SystemCSim."""

    home: str | None
    cxx: str | None
    cflags: list[str] = field(default_factory=list)
    ldflags: list[str] = field(default_factory=list)

    def get_home(self) -> str | None:
        """SystemC install root, or None.

        Falls back to $SYSTEMC_HOME when `home` is unset or references an unset variable. An unset variable is never left in the returned path, so a missing home is caught as ``systemc.home_unresolved`` before Verilator runs.
        """
        if self.home is not None:
            expanded = os.path.expanduser(os.path.expandvars(self.home))
            if not _UNRESOLVED_VAR_RE.search(expanded):
                return expanded
        env_home = os.environ.get("SYSTEMC_HOME")
        return env_home if env_home else None

    def get_include_dir(self) -> str | None:
        home = self.get_home()
        return os.path.join(home, "include") if home else None

    def get_lib_dir(self) -> str | None:
        home = self.get_home()
        return os.path.join(home, "lib") if home else None

    def get_cxx(self) -> str | None:
        return self.cxx

    def get_cflags(self) -> list[str]:
        """Project-wide -CFLAGS tokens."""
        return list(self.cflags)

    def get_ldflags(self) -> list[str]:
        """Project-wide -LDFLAGS tokens."""
        return list(self.ldflags)

    def __str__(self):
        return pprint.pformat(self)


@serde
class SystemCConfigFile:
    """YAML-backed cfg-systemc block."""

    home: str | None = None
    cxx: str | None = None
    cflags: list[str] | None = None
    ldflags: list[str] | None = None

    def initialise(self) -> SystemCConfig:
        return SystemCConfig(
            home=self.home,
            cxx=self.cxx,
            cflags=list(self.cflags) if self.cflags else [],
            ldflags=list(self.ldflags) if self.ldflags else [],
        )
