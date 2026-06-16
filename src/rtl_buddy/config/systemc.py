"""SystemC root-config support.

A single optional block on root_config.yaml that pins where the project's
SystemC install lives (headers + libsystemc.{a,dylib,so}) and, optionally,
the C++ compiler to use so the cosim binary's ABI matches libsystemc.

Schema (in root_config.yaml):

    cfg-systemc:
      home: "${WORKSPACE}/systemc-install"   # optional; $SYSTEMC_HOME fallback
      cxx:  "/opt/homebrew/bin/g++-15"       # optional
      cflags: ["-std=c++17"]                  # optional; project-wide -CFLAGS
      ldflags: []                             # optional; project-wide -LDFLAGS

`cflags` / `ldflags` here are project-wide defaults. Per-testbench
`systemc.cflags` / `systemc.ldflags` in tests.yaml are appended (not
replaced) so testbench-specific tokens layer on top of the project
default. The SystemC include and library paths derived from `home`
are always auto-emitted; users never need to repeat those.

Resolution order for `home`: config value (with ~ and $VAR expansion) →
$SYSTEMC_HOME env var → None. SystemCSim is responsible for failing fast
when a SystemC testbench is requested but home cannot be resolved.

An unresolved `${VAR}` (env var not set) is treated as if `home` was
unset, so the env-var fallback still runs and the existing
`systemc.home_unresolved` error fires instead of Verilator failing later
with a confusing "include not found at ${SYSTEMC_HOME}/include".
"""

import os
import pprint
import re
from dataclasses import dataclass, field

from serde import serde


# Matches `${VAR}` and `$VAR` (identifier form). If `os.path.expandvars`
# leaves either of these in the output, the env var was unset — POSIX
# expandvars semantics return the literal rather than raising.
_UNRESOLVED_VAR_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class SystemCConfig:
    """Resolved SystemC config consumed by SystemCSim."""

    home: str | None
    cxx: str | None
    cflags: list[str] = field(default_factory=list)
    ldflags: list[str] = field(default_factory=list)

    def get_home(self) -> str | None:
        """Configured SystemC install root, with ~ and $VAR expanded.

        Falls back to $SYSTEMC_HOME when not set in config, or when the
        config value contained a $VAR that did not resolve (env var unset).
        Returns None if neither is available; SystemCSim raises a fatal
        error in that case.
        """
        if self.home is not None:
            expanded = os.path.expanduser(os.path.expandvars(self.home))
            if not _UNRESOLVED_VAR_RE.search(expanded):
                return expanded
            # The configured value referenced an unset env var. Treat as
            # if `home` was unset and let the env-var fallback / None path
            # handle it, so SystemCSim's home_unresolved error fires
            # instead of Verilator failing later on a literal "${...}" path.
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
        """Project-wide -CFLAGS tokens (testbench-level cflags append on top)."""
        return list(self.cflags)

    def get_ldflags(self) -> list[str]:
        """Project-wide -LDFLAGS tokens (testbench-level ldflags append on top)."""
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
