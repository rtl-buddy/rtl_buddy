import logging

logger = logging.getLogger(__name__)
import pprint

from serde import serde, field
import re

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .toolpath import resolve_tool_path


def process_opts(opts):
    return re.sub(r"\s+", " ", opts).split(" ")


@serde
class RtlBuilderConfigOpts:
    """
    Lists of command-line options for a single builder.

    Attributes:
      compile_time (list[str] | None): Compile-time options
      run_time (list[str] | None): Run-time options
    """

    compile_time: list[str] | None = field(
        rename="compile-time", deserializer=process_opts
    )
    run_time: list[str] | None = field(rename="run-time", deserializer=process_opts)


@serde
class RtlBuilderConfig:
    """
    Configuration for a RTL Builder.

    Attributes:
      name (str): Unique builder identifier.
      simulator_family (str | None): Simulator family identifier used for
        backend-specific behavior such as coverage processing.
      exe (str | list[str]): Name of the compiler executable, a path to
        it, or a list of candidates in preference order (see
        :mod:`rtl_buddy.config.toolpath`). ``~`` and ``$VAR`` are expanded.
      simv (str): Name of the executable file for simulation (on disc).
      sim_rand_seed (int): Random seed for the simulation.
      sim_rand_prefix (str): Simulator-specific prefix for the random seed.
      opts (dict[str, RtlBuilderConfigOpts]): Command-line options for the builder, keyed by mode.
      wave_format (str | None): Optional post-sim waveform handling for `rb
        wave`. ``fst-postproc`` converts a VCD dump to FST via ``vcd2fst``.
      extra_sim_timeout (int | None): Seconds added to every test's
        ``sim_timeout`` under this builder.
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
    #: Directory relative ``builder:`` candidates are anchored at, set by
    #: :meth:`set_base_dir`. Declared (``skip=True``: it is not a YAML key
    #: and must never be serialised) rather than attached post hoc, so an
    #: unanchored config reads its real default instead of a ``getattr``
    #: fallback — a construction path that forgot the anchor would
    #: otherwise existence-test relative candidates against the process
    #: cwd and look exactly like "the tool is not installed" (#439 review).
    _base_dir: str | None = field(default=None, skip=True)

    def get_name(self) -> str:
        """
        Retrieves the value of name.

        Returns:
          name (str): The value of name.
        """
        return self.name

    def set_base_dir(self, base_dir: str | None) -> None:
        """Anchor relative ``builder:`` candidates at ``base_dir``.

        Set by :class:`~rtl_buddy.config.root.RootConfig` to the directory
        holding ``root_config.yaml``, so a relative candidate is
        existence-tested there rather than against the process cwd — `rb`
        is routinely invoked from a suite directory (#439). A config built
        outside RootConfig (tests) simply has no anchor.
        """
        self._base_dir = base_dir

    def get_simulator_family(self) -> str:
        """
        Retrieve the simulator family for backend-specific handling.

        Returns:
          family (str): Canonical simulator family, e.g. "verilator" or "vcs".
        """
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
        """
        Retrieve the optional post-sim waveform format for `rb wave`.

        Returns:
          wave_format (str | None): e.g. ``"fst-postproc"``, or None.
        """
        return self.wave_format

    def get_extra_sim_timeout(self) -> int:
        """
        Seconds this builder adds to every test's simulation timeout.

        For builders that queue for a license seat, or are otherwise slower
        than the per-test ``sim_timeout`` assumes, without making that
        allowance apply to builders that do not need it: a tight timeout is
        worth keeping wherever nothing legitimately blocks, so a hung test
        still fails fast there.

        Returns:
          seconds (int): Extra seconds, 0 when unset.
        Raises:
          FatalRtlBuddyError: The configured value is negative.
        """
        if self.extra_sim_timeout is None:
            return 0
        # Rejected rather than clamped: a negative value would *shrink* every
        # test's timeout, and one below -sim_timeout reaches the process wait
        # as a negative timeout, i.e. an instant timeout verdict on a sim that
        # never ran. Silently clamping that to 0 would hide a config typo.
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
        """
        Retrieves the value of exe, with ``~`` / ``$VAR`` expanded.

        ``builder:`` may be a single value or a list of candidates in
        preference order; the first that expands cleanly and exists wins,
        with a trailing bare name left for ``PATH``. See
        :mod:`rtl_buddy.config.toolpath`.

        Returns:
          exe (str): The effective compiler executable.
        """
        return resolve_tool_path(
            self.exe,
            base_dir=self._base_dir,
            block="cfg-rtl-builder",
            name=self.name,
            field="builder",
        )

    def get_simv(self) -> str:
        """
        Retrieves the value of simv.

        Returns:
          simv (str): The value of simv.
        """
        return self.simv

    def get_seed(self) -> int:
        """
        Retrieves the value of sim_rand_seed.

        Returns:
          seed (int): The value of sim_rand_seed.
        """
        return self.sim_rand_seed

    def get_modes(self) -> list[str]:
        """
        Retrieves a list of available builder modes.

        Returns:
          modes (list[str]): The list of available modes.
        """
        return self.opts.keys()

    def get_compile_time_opts(self, mode: str) -> list[str]:
        """
        Retrieves the compile time options for a given mode.

        Args:
          mode (str): The requested mode.
        Returns:
          opts (list[str]): The list of options.
        """
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
        """
        Retrieves the run time options for a given mode.

        Args:
          mode (str): The requested mode.
          seed (int|None) [None]: An optional seed to append to the list of options.
        Returns:
          opts (list[str]): The list of options.
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
