import logging

logger = logging.getLogger(__name__)
import pprint

from serde import serde, field
import os
import re

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .toolpath import resolve_tool_path


def process_opts(opts):
    return re.sub(r"\s+", " ", opts).split(" ")


#: The ``compile-opts`` mode-map entry that applies when the compiling mode has none.
COMPILE_OPTS_DEFAULT_MODE = "default"

#: A family's normalized ``compile-opts``: tokens for every mode, or tokens per builder mode.
CompileOptsValue = list[str] | dict[str, list[str]]


def _compile_opts_tokens(opts, *, where, key) -> list[str]:
    """Return one ``compile-opts`` string or list as tokens; raises FatalRtlBuddyError naming ``key``."""
    if opts is None:
        return []
    if isinstance(opts, str):
        return [t for t in process_opts(opts.strip()) if t]
    if isinstance(opts, list) and all(isinstance(t, str) for t in opts):
        return list(opts)
    raise FatalRtlBuddyError(
        f"{where}: compile-opts.{key} must be a string or a list of strings"
    )


def normalize_compile_opts(value, *, where) -> dict[str, CompileOptsValue] | None:
    """Validate a suite's ``compile-opts`` and return it as ``{family: tokens}``.

    ``value`` maps a simulator family (``verilator``, ``vcs``, ``icarus`` or a builder's
    ``simulator-family``) to options appended after the builder mode's ``compile-time``
    for that family only. A string is split on whitespace as ``compile-time`` is; a
    list is taken token by token; both apply in every builder mode. A map keyed by
    builder mode, with an optional ``default`` entry, holds a string or list per mode
    and normalizes to ``{mode: [token, ...]}``; :func:`select_compile_opts` picks the
    entry. Raises FatalRtlBuddyError naming ``where``.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FatalRtlBuddyError(
            f"{where}: compile-opts must map a simulator family to options, "
            f"got {type(value).__name__}"
        )
    normalized: dict[str, CompileOptsValue] = {}
    for family, opts in value.items():
        if not isinstance(family, str) or not family.strip():
            raise FatalRtlBuddyError(
                f"{where}: compile-opts keys must be simulator family names, got {family!r}"
            )
        if isinstance(opts, dict):
            by_mode: dict[str, list[str]] = {}
            for mode, mode_opts in opts.items():
                # Mode names are the builder-opts keys, matched exactly as --builder-mode is.
                if not isinstance(mode, str) or not mode.strip():
                    raise FatalRtlBuddyError(
                        f"{where}: compile-opts.{family} keys must be builder mode "
                        f"names or `{COMPILE_OPTS_DEFAULT_MODE}`, got {mode!r}"
                    )
                by_mode[mode] = _compile_opts_tokens(
                    mode_opts, where=where, key=f"{family}.{mode}"
                )
            normalized[family.strip().lower()] = by_mode
        else:
            normalized[family.strip().lower()] = _compile_opts_tokens(
                opts, where=where, key=family
            )
    return normalized


def select_compile_opts(
    compile_opts: dict[str, CompileOptsValue] | None, family: str, mode: str | None
) -> list[str]:
    """Return the ``compile-opts`` tokens for simulator ``family`` compiling in builder ``mode``.

    A string or list applies in every mode. A mode map selects ``mode``'s entry, then
    ``default``, then no options; an entry present but empty means no options. Empty
    when ``compile_opts`` names no ``family``.
    """
    if not compile_opts:
        return []
    opts = compile_opts.get(family.lower())
    if opts is None:
        return []
    if isinstance(opts, dict):
        if mode is not None and mode in opts:
            return list(opts[mode])
        return list(opts.get(COMPILE_OPTS_DEFAULT_MODE, []))
    return list(opts)


#: Set by rtl_buddy, not read from the caller's environment, when ``compile-time`` tokens are expanded.
PROJECT_ROOT_VAR = "RTL_BUDDY_PROJECT_ROOT"

_PROJECT_ROOT_VAR_RE = re.compile(
    r"\$(?:\{%s\}|%s(?![A-Za-z0-9_]))" % ((PROJECT_ROOT_VAR,) * 2)
)


def expand_compile_opts(opts: list[str], project_root: str | None) -> list[str]:
    """Expand ``${RTL_BUDDY_PROJECT_ROOT}``, ``$VAR`` and ``~`` in ``opts``.

    An unset variable is left as written.
    """
    expanded = []
    for opt in opts:
        if project_root is not None:
            opt = _PROJECT_ROOT_VAR_RE.sub(lambda _: project_root, opt)
        expanded.append(os.path.expanduser(os.path.expandvars(opt)))
    return expanded


@serde
class RtlBuilderConfigOpts:
    """Compile-time and run-time command-line options for one builder mode."""

    compile_time: list[str] | None = field(
        rename="compile-time", deserializer=process_opts
    )
    run_time: list[str] | None = field(rename="run-time", deserializer=process_opts)


@serde
class RtlBuilderConfig:
    """A `cfg-rtl-builder` entry.

    `exe` is an executable name, path or candidate list (see :mod:`rtl_buddy.config.toolpath`). `simv` is the simulation executable's file name. `opts` maps a mode to its options. `simulator_family` selects backend-specific behaviour such as coverage processing. `wave_format` ``fst-postproc`` converts a VCD dump to FST via ``vcd2fst`` for `rb wave`. `extra_sim_timeout` is seconds added to every test's ``sim_timeout``.
    """

    name: str
    exe: str | list[str] = field(rename="builder")
    simv: str = field(rename="builder-simv")
    sim_rand_seed: int = field(rename="sim-rand-seed")
    sim_rand_prefix: str = field(rename="sim-rand-seed-prefix")
    opts: dict[str, RtlBuilderConfigOpts] = field(rename="builder-opts")
    simulator_family: str | None = field(rename="simulator-family", default=None)
    wave_format: str | None = field(rename="wave-format", default=None)
    extra_sim_timeout: int | None = field(rename="extra-sim-timeout", default=None)
    #: Anchor for relative ``builder:`` candidates, set by :meth:`set_base_dir`; not a YAML key.
    _base_dir: str | None = field(default=None, skip=True)

    def get_name(self) -> str:
        return self.name

    def set_base_dir(self, base_dir: str | None) -> None:
        """Anchor relative ``builder:`` candidates at ``base_dir``, normally the ``root_config.yaml`` directory."""
        self._base_dir = base_dir

    def get_simulator_family(self) -> str:
        """Return the simulator family, e.g. "verilator" or "vcs"; inferred from the executable name when unset."""
        if self.simulator_family is not None:
            return self.simulator_family

        exe_base = self.get_exe().split()[0].split("/")[-1].lower()
        if exe_base.startswith("verilator"):
            return "verilator"
        if exe_base.startswith("vcs"):
            return "vcs"
        if exe_base.startswith("iverilog") or exe_base.startswith("icarus"):
            return "icarus"
        return exe_base

    def get_wave_format(self) -> str | None:
        """Return the post-sim waveform format for `rb wave` (e.g. ``"fst-postproc"``), or None."""
        return self.wave_format

    def get_extra_sim_timeout(self) -> int:
        """Return the seconds this builder adds to every test's simulation timeout; 0 when unset.

        Raises FatalRtlBuddyError if the value is negative.
        """
        if self.extra_sim_timeout is None:
            return 0
        # Rejected, not clamped: a negative value shrinks the timeout and can yield an instant timeout verdict.
        if self.extra_sim_timeout < 0:
            log_event(
                logger,
                logging.ERROR,
                "builder.extra_sim_timeout_negative",
                builder=self.name,
                seconds=self.extra_sim_timeout,
            )
            raise FatalRtlBuddyError(
                f'Builder "{self.name}" has a negative extra-sim-timeout '
                f"({self.extra_sim_timeout}); it must be >= 0"
            )
        return self.extra_sim_timeout

    def get_exe(self) -> str:
        """Return the effective compiler executable (see :mod:`rtl_buddy.config.toolpath`)."""
        return resolve_tool_path(
            self.exe,
            base_dir=self._base_dir,
            block="cfg-rtl-builder",
            name=self.name,
            field="builder",
        )

    def get_simv(self) -> str:
        return self.simv

    def get_seed(self) -> int:
        return self.sim_rand_seed

    def get_modes(self) -> list[str]:
        return self.opts.keys()

    def get_compile_time_opts(self, mode: str) -> list[str]:
        """Return the compile-time options for `mode`; raises FatalRtlBuddyError if the mode or stage is missing."""
        if mode not in self.opts:
            log_event(
                logger,
                logging.ERROR,
                "builder.mode_missing",
                builder=self.name,
                mode=mode,
                stage="compile",
            )
            raise FatalRtlBuddyError(f'Requested mode "{mode}" not in config')

        if self.opts[mode].compile_time is None:
            log_event(
                logger,
                logging.ERROR,
                "builder.stage_missing",
                builder=self.name,
                mode=mode,
                stage="compile-time",
            )
            raise FatalRtlBuddyError(
                f'Requested stage "compile-time" not in config "{mode}"'
            )

        return list(self.opts[mode].compile_time)

    def get_run_time_opts(self, mode: str, seed: int | None = None) -> list[str]:
        """Return the run-time options for `mode`, with the seed option appended when `seed` is given.

        Raises FatalRtlBuddyError if the mode or stage is missing.
        """
        if mode not in self.opts:
            log_event(
                logger,
                logging.ERROR,
                "builder.mode_missing",
                builder=self.name,
                mode=mode,
                stage="run",
            )
            raise FatalRtlBuddyError(f'Requested mode "{mode}" not in config')

        if self.opts[mode].run_time is None:
            log_event(
                logger,
                logging.ERROR,
                "builder.stage_missing",
                builder=self.name,
                mode=mode,
                stage="run-time",
            )
            raise FatalRtlBuddyError(
                f'Requested stage "run-time" not in config "{mode}"'
            )

        opts = list(self.opts[mode].run_time)
        if seed is not None:
            opts.append(self.sim_rand_prefix + str(seed))
        return opts

    def __str__(self):
        return pprint.pformat(self)
