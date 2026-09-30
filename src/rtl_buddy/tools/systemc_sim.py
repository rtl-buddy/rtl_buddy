# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""SystemC cosim runner: a VlogSim that builds with Verilator's --sc flow and runs the user's sc_main()."""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from .vlog_sim import VlogSim
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


_PIN_STYLE_FLAGS = {
    "uint": "--pins-sc-uint",
    "biguint": "--pins-sc-biguint",
    # "bv" has no flag: it is Verilator's default for >32-bit ports.
    "bv": None,
}


class SystemCSim(VlogSim):
    """Verilator --sc build with a user-supplied sc_main()."""

    def _systemc_cfg(self):
        """Return the cfg-systemc block; raise if root_config.yaml has none."""
        cfg = self.root_cfg.get_systemc_cfg()
        if cfg is None:
            log_event(
                logger,
                logging.ERROR,
                "systemc.cfg_missing",
                test=self.test_name,
            )
            raise FatalRtlBuddyError(
                f"testbench '{self.testbench.name}' requires SystemC, "
                "but root_config.yaml has no cfg-systemc block"
            )
        return cfg

    def _systemc_home(self) -> str:
        cfg = self._systemc_cfg()
        home = cfg.get_home()
        if home is None:
            log_event(
                logger,
                logging.ERROR,
                "systemc.home_unresolved",
                test=self.test_name,
            )
            raise FatalRtlBuddyError(
                f"testbench '{self.testbench.name}' requires SystemC, but neither "
                "cfg-systemc.home nor $SYSTEMC_HOME is set"
            )
        return home

    def _resolve_suite_path(self, rel_path: str) -> str:
        """Resolve a path relative to the suite directory."""
        if os.path.isabs(rel_path):
            return rel_path
        return str(Path(self.suite_work_dir) / rel_path)

    def _pin_style_flag(self) -> str | None:
        style = self.testbench.systemc.pin_style
        if style is None:
            return None
        if style not in _PIN_STYLE_FLAGS:
            raise FatalRtlBuddyError(
                f"testbench '{self.testbench.name}': systemc.pin_style "
                f"'{style}' invalid; expected one of {sorted(_PIN_STYLE_FLAGS)}"
            )
        return _PIN_STYLE_FLAGS[style]

    def _filter_builder_opts(self, opts: list) -> list:
        # --binary conflicts with --exe/--build.
        return [o for o in opts if o != "--binary"]

    def _get_extra_compile_flags(self) -> list:
        sc_cfg = self.testbench.systemc
        root_sc_cfg = self._systemc_cfg()
        home = self._systemc_home()
        include_dir = str(Path(home) / "include")
        lib_dir = str(Path(home) / "lib")

        # Order is root cfg, then testbench, so testbench flags come last.
        cflag_tokens = [
            f"-I{include_dir}",
            *root_sc_cfg.get_cflags(),
            *sc_cfg.cflags,
        ]
        ldflag_tokens = [
            f"-L{lib_dir}",
            "-lsystemc",
            *root_sc_cfg.get_ldflags(),
            *sc_cfg.ldflags,
        ]

        flags = [
            "--sc",
            "--exe",
            "--build",
            "-j",
            "0",
        ]
        # Skip our --top-module when the user configured one: Verilator takes
        # the last, and the base class only warns about conflicts it can see.
        configured_top = self._user_configured_top()
        if configured_top is None:
            flags += ["--top-module", self.testbench.toplevel]

        pin_flag = self._pin_style_flag()
        if pin_flag is not None:
            flags.append(pin_flag)

        flags.append(self._resolve_suite_path(sc_cfg.sc_main))
        for extra in sc_cfg.sc_extra:
            flags.append(self._resolve_suite_path(extra))

        flags += ["-CFLAGS", " ".join(cflag_tokens)]
        flags += ["-LDFLAGS", " ".join(ldflag_tokens)]

        log_event(
            logger,
            logging.DEBUG,
            "systemc.compile_flags",
            test=self.test_name,
            toplevel=self.testbench.toplevel,
            configured_top=configured_top[1] if configured_top else None,
            sc_main=sc_cfg.sc_main,
            sc_extra=sc_cfg.sc_extra,
            pin_style=sc_cfg.pin_style,
            home=home,
        )
        return flags

    def _get_extra_compile_env(self) -> dict:
        cfg = self._systemc_cfg()
        home = self._systemc_home()
        env: dict[str, str] = {
            "SYSTEMC_HOME": home,
            "SYSTEMC_INCLUDE": str(Path(home) / "include"),
            "SYSTEMC_LIBDIR": str(Path(home) / "lib"),
        }
        cxx = cfg.get_cxx()
        if cxx is not None:
            env["CXX"] = cxx
        return env

    def _get_extra_sim_env(self, run_id=None) -> dict:
        """Prepend the SystemC libdir to the dynamic-linker search path.

        Always added, since a shared libsystemc needs it and a static one ignores it.
        """
        home = self._systemc_home()
        lib_dir = str(Path(home) / "lib")

        env: dict[str, str] = {}
        if sys.platform == "darwin":
            existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
            parts = [lib_dir] + ([existing] if existing else [])
            env["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(parts)
        else:
            existing = os.environ.get("LD_LIBRARY_PATH", "")
            parts = [lib_dir] + ([existing] if existing else [])
            env["LD_LIBRARY_PATH"] = ":".join(parts)
        log_event(
            logger,
            logging.DEBUG,
            "systemc.sim_env",
            test=self.test_name,
            run_id=run_id,
            lib_dir=lib_dir,
        )
        return env
