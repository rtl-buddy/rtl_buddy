import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field
from typing import Literal

from serde import field, serde
from serde.yaml import from_yaml

from ..errors import FatalRtlBuddyError
from .openroad_threads import validate_threads
from ..logging_utils import log_event
from .pnr import PnrSuiteConfig
from .synth import SynthSuiteConfig
from .toolpath import resolve_tool_path

logger = logging.getLogger(__name__)


@serde
class PowerToolConfigFile:
    name: str
    tool: str | list[str]


class PowerToolConfig:
    def __init__(self, cfg: PowerToolConfigFile, base_dir: str | None = None):
        self._cfg = cfg
        # Directory relative `tool:` candidates are existence-tested
        # against: the one holding root_config.yaml, never the process
        # cwd (rb is routinely invoked from a suite directory).
        self._base_dir = base_dir

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        """Effective tool executable, with ``~`` / ``$VAR`` expanded.

        ``tool:`` may be a single value or a list of candidates in
        preference order; see :mod:`rtl_buddy.config.toolpath`.
        """
        return resolve_tool_path(
            self._cfg.tool,
            base_dir=self._base_dir,
            block="cfg-power-tools",
            name=self._cfg.name,
            field="tool",
        )


@serde
class PowerActivityFile:
    saif: str | None = None
    vcd: str | None = None
    scope: str | None = None
    default_toggle_rate: float = field(rename="default-toggle-rate", default=0.1)
    default_static_prob: float = field(rename="default-static-prob", default=0.5)


@dataclass
class PowerActivity:
    saif: str | None
    vcd: str | None
    scope: str | None
    default_toggle_rate: float
    default_static_prob: float

    def has_trace(self) -> bool:
        return bool(self.saif) or bool(self.vcd)


PowerMode = Literal["static", "dynamic"]
NetlistSource = Literal["synth", "pnr"]


@serde
class PowerConfigFile:
    name: str
    desc: str
    tool: str = "openroad"
    mode: PowerMode = "static"
    netlist_source: NetlistSource = field(rename="netlist-source", default="synth")
    synth: str = ""
    synth_path: str = field(rename="synth-path", default="")
    pnr: str = ""
    pnr_path: str = field(rename="pnr-path", default="")
    # The synthesis run this analysis publishes its half of the physical
    # model beside, named in the `synth-path` suite (#589). Empty means
    # the run publishes into its own artefact directory, which is the
    # convention that came first: a merged model then happens only where
    # the power run is named after the synthesis and configured in the
    # same directory. See `PowerConfig.get_phys_run`.
    phys_run: str = field(rename="phys-run", default="")
    constraints: str | None = None
    platform: str = ""
    # Liberty for hard macros, on top of whatever the referenced synth or
    # pnr run already declares (#627). The platform corner characterises
    # the standard cells and nothing else, so a macro reaching P&R through
    # that run's `lib-paths` has no library here and reports exactly zero.
    # Inheritance covers the ordinary case; this key is for a macro whose
    # Liberty only the power analysis needs — a corner the upstream run
    # was not routed against, say. Appended after the inherited list.
    lib_paths: list[str] = field(rename="lib-paths", default_factory=list)
    activity: PowerActivityFile = field(default_factory=PowerActivityFile)
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    # OpenROAD worker threads: a positive integer or `auto`; unset keeps
    # OpenROAD's single-thread default (#654). See config/openroad_threads.
    threads: int | str | None = None
    # Expected-fail markers (pytest-style). Either marks this run
    # expected-to-fail; `xfail` is non-strict (an unexpected pass still
    # passes), `xfail_strict` is strict (an unexpected pass is a failure).
    # See docs/concepts/expected-failures.md.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "PowerConfig":
        if self.netlist_source == "synth":
            if not self.synth:
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': missing 'synth' "
                    "(name of upstream rb synth entry)"
                )
            if not self.synth_path:
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': missing 'synth-path' "
                    "(path to the synth.yaml that defines the synth entry)"
                )
        elif self.netlist_source == "pnr":
            if not self.pnr:
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': netlist-source 'pnr' requires "
                    "'pnr' (name of upstream rb pnr entry)"
                )
            if not self.pnr_path:
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': netlist-source 'pnr' requires "
                    "'pnr-path' (path to the pnr.yaml that defines the entry)"
                )

        if self.phys_run:
            if self.netlist_source != "synth":
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': 'phys-run' publishes this "
                    "run's half of the physical model beside a synthesis "
                    "run's, and only a 'netlist-source: synth' run measures "
                    "the netlist that pairing is gated on — this one reads "
                    f"'{self.netlist_source}', whose half can never merge "
                    "with a synthesis' and would replace it instead"
                )
            if any(sep and sep in self.phys_run for sep in (os.sep, os.altsep, "/")):
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': 'phys-run' names a run in "
                    f"'{self.synth_path}', not a path — got '{self.phys_run}'"
                )
            if self.phys_run in (os.curdir, os.pardir):
                raise FatalRtlBuddyError(
                    f"power run '{self.name}': 'phys-run' names a run in "
                    f"'{self.synth_path}', not a directory — got "
                    f"'{self.phys_run}'"
                )

        if not self.platform:
            raise FatalRtlBuddyError(
                f"power run '{self.name}': missing 'platform' "
                "(name of a cfg-pnr-platforms entry)"
            )
        threads = validate_threads(self.threads, where=f"power run '{self.name}'")

        if self.activity.saif and self.activity.vcd:
            raise FatalRtlBuddyError(
                f"power run '{self.name}': activity.saif and activity.vcd "
                "are mutually exclusive; pick one"
            )
        if self.activity.scope and not (self.activity.saif or self.activity.vcd):
            raise FatalRtlBuddyError(
                f"power run '{self.name}': activity.scope is set but no "
                "activity.saif or activity.vcd was provided; scope only "
                "applies when reading a trace file"
            )

        def _resolve(p: str | None) -> str | None:
            return os.path.normpath(os.path.join(config_dir, p)) if p else None

        constraints = _resolve(self.constraints)
        activity = PowerActivity(
            saif=_resolve(self.activity.saif),
            vcd=_resolve(self.activity.vcd),
            scope=self.activity.scope,
            default_toggle_rate=self.activity.default_toggle_rate,
            default_static_prob=self.activity.default_static_prob,
        )

        return PowerConfig(
            name=self.name,
            desc=self.desc,
            tool=self.tool,
            mode=self.mode,
            netlist_source=self.netlist_source,
            synth_name=self.synth or None,
            synth_suite_path=_resolve(self.synth_path) if self.synth_path else None,
            pnr_name=self.pnr or None,
            pnr_suite_path=_resolve(self.pnr_path) if self.pnr_path else None,
            constraints=constraints,
            platform=self.platform,
            lib_paths=[
                os.path.normpath(os.path.join(config_dir, p)) for p in self.lib_paths
            ],
            activity=activity,
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            phys_run=self.phys_run or None,
            threads=threads,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
        )


@dataclass
class PowerConfig:
    name: str
    desc: str
    tool: str
    mode: PowerMode
    netlist_source: NetlistSource
    synth_name: str | None
    synth_suite_path: str | None
    pnr_name: str | None
    pnr_suite_path: str | None
    constraints: str | None
    platform: str
    activity: PowerActivity
    _reglvl: int | dict | None
    tool_overrides: dict | None
    # Macro Liberty this run adds on top of what the referenced synth or
    # pnr run declares, resolved against the `power.yaml` directory (#627).
    lib_paths: list[str] = dc_field(default_factory=list)
    # Optional, so it sits with the defaults rather than beside the
    # `synth`/`synth-path` pair it is resolved against (#589).
    phys_run: str | None = None
    threads: int | str | None = None
    xfail: bool = False
    xfail_strict: bool = False

    def is_xfail(self) -> bool:
        """Whether this run is expected to fail (either flag set)."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_name(self) -> str:
        return self.name

    def get_desc(self) -> str:
        return self.desc

    def get_tool_name(self) -> str:
        return self.tool

    def get_mode(self) -> PowerMode:
        return self.mode

    def get_threads(self) -> int | str | None:
        """Validated `threads:` — a positive int, `auto`, or None (#654)."""
        return self.threads

    def get_netlist_source(self) -> NetlistSource:
        return self.netlist_source

    def get_synth_name(self) -> str | None:
        return self.synth_name

    def get_synth_suite_path(self) -> str | None:
        return self.synth_suite_path

    def get_pnr_name(self) -> str | None:
        return self.pnr_name

    def get_pnr_suite_path(self) -> str | None:
        return self.pnr_suite_path

    def get_phys_run(self) -> str | None:
        """The synthesis run this analysis publishes its model half beside.

        ``None`` for a run that says nothing, and that is the convention
        the flow had before the field existed: the power half is
        published into the run's own ``artefacts/<name>/``, so the two
        halves of a model meet only where a power run is named after the
        synthesis it reads *and* configured in the same directory. Rename
        either side and the halves land in two directories, each
        half-filled, with nothing said about it (#589).

        Naming a run says the pairing out loud. It does not weaken the
        merge: the netlist sha256 both producers record is still what
        decides whether the two halves describe one design, and this only
        decides which directory they are asked to meet in.
        """
        return self.phys_run

    def get_constraints(self) -> str | None:
        return self.constraints

    def get_platform(self) -> str:
        return self.platform

    def get_lib_paths(self) -> list[str]:
        """This run's own macro Liberty, over the inherited list (#627)."""
        return list(self.lib_paths)

    def get_activity(self) -> PowerActivity:
        return self.activity

    def get_activity_source(self) -> str:
        """Resolve the activity strategy for backends to dispatch on.

        Returns one of:
          - "default":   static mode; no activity commands emitted
          - "saif":      dynamic mode, SAIF trace supplied
          - "vcd":       dynamic mode, VCD trace supplied
          - "synthetic": dynamic mode, no trace — use default toggle/duty

        The string also flows into PowerPassResults.activity_source so
        the results table shows what drove the numbers.
        """
        if self.mode == "static":
            return "default"
        if self.activity.saif:
            return "saif"
        if self.activity.vcd:
            return "vcd"
        return "synthetic"

    def get_reglvl(self, tool_name: str) -> int:
        match self._reglvl:
            case int() as lvl:
                return lvl
            case dict() if tool_name in self._reglvl:
                return self._reglvl[tool_name]
            case dict() if "default" in self._reglvl:
                return self._reglvl["default"]
            case None:
                return 0
            case _:
                log_event(
                    logger,
                    logging.ERROR,
                    "power_config.reglvl_malformed",
                    power=self.name,
                    tool=tool_name,
                )
                raise FatalRtlBuddyError(
                    f"Malformed power.yaml, specify reglvl for {self.name} "
                    f"with {tool_name} or default"
                )

    def get_tool_overrides(self) -> dict | None:
        return self.tool_overrides

    def resolve_synth_cfg(self):
        """Load the upstream synth.yaml and return the referenced entry.

        Only valid when `netlist_source == "synth"`. For the `pnr` path,
        use `resolve_pnr_cfg()` and chain to its synth.
        """
        if not self.synth_suite_path or not self.synth_name:
            raise FatalRtlBuddyError(
                f"power run '{self.name}': resolve_synth_cfg() called but "
                "synth/synth-path are not configured"
            )
        suite = SynthSuiteConfig(self.synth_suite_path)
        return suite.get_syntheses(self.synth_name)[0]

    def resolve_phys_run_cfg(self):
        """The synthesis entry ``phys-run`` names, or a fatal config error.

        Read out of the same ``synth-path`` suite :meth:`resolve_synth_cfg`
        reads, because ``phys-run`` names a *sibling* of the synthesis
        this analysis measures: the directory it publishes into is that
        suite's ``artefacts/<run>/``, which is where a synthesis entry of
        that name writes its own half. A name no entry carries is a typo
        or a rename, and publishing into the empty directory it points at
        would be the half-filled model the field exists to prevent —
        harder to find, because the directory would not even be a run's.

        Called for its check rather than its value; the entry is returned
        because the loader has it and a caller may want the top.
        """
        if not self.synth_suite_path or not self.phys_run:
            raise FatalRtlBuddyError(
                f"power run '{self.name}': resolve_phys_run_cfg() called but "
                "phys-run/synth-path are not configured"
            )
        suite = SynthSuiteConfig(self.synth_suite_path)
        return suite.get_syntheses(self.phys_run)[0]

    def resolve_pnr_cfg(self):
        """Load the upstream pnr.yaml and return the referenced entry.

        Only valid when `netlist_source == "pnr"`. The pnr entry itself
        chains to a synth via its own `resolve_synth_cfg()`, which is
        how the top module name is recovered.
        """
        if not self.pnr_suite_path or not self.pnr_name:
            raise FatalRtlBuddyError(
                f"power run '{self.name}': resolve_pnr_cfg() called but "
                "pnr/pnr-path are not configured"
            )
        suite = PnrSuiteConfig(self.pnr_suite_path)
        return suite.get_runs(self.pnr_name)[0]

    def get_top(self) -> str:
        """Return the design top module name regardless of netlist source."""
        if self.netlist_source == "pnr":
            return self.resolve_pnr_cfg().resolve_synth_cfg().get_top()
        return self.resolve_synth_cfg().get_top()

    def __str__(self):
        return pprint.pformat(self)


@serde
class PowerSuiteConfigFile:
    filetype: Literal["power_config"] = field(rename="rtl-buddy-filetype")
    runs: list[PowerConfigFile]


class PowerSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.runs: dict[str, PowerConfig] = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(PowerSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "power_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.runs = {r.name: r.initialise(config_dir) for r in data.runs}
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "power_suite_config.runs_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: runs section malformed") from e

    def get_runs(self, name: str | None = None) -> list[PowerConfig]:
        if name is not None:
            if name not in self.runs:
                log_event(
                    logger,
                    logging.ERROR,
                    "power_suite_config.run_missing",
                    path=self.path,
                    run=name,
                )
                raise FatalRtlBuddyError(
                    f"power run '{name}' not found in suite {self.path}"
                )
            return [self.runs[name]]
        return list(self.runs.values())

    def get_run_names(self) -> list[str]:
        return list(self.runs.keys())

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self)


@serde
class PowerRegConfigFile:
    filetype: Literal["power_reg_config"] = field(rename="rtl-buddy-filetype")
    power_configs: list[str] = field(rename="power-configs", default_factory=list)


class PowerRegConfig:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.suite_configs: list[PowerSuiteConfig] = []
        try:
            with open(path, "r") as f:
                data = from_yaml(PowerRegConfigFile, f.read())
            self.suite_configs = [
                PowerSuiteConfig(os.path.join(os.path.dirname(path), p))
                for p in data.power_configs
            ]
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "power_reg_config.load_failed",
                name=name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from e

    def get_name(self) -> str:
        return self.name

    def get_path(self) -> str:
        return self.path

    def get_suite_configs(self) -> list[PowerSuiteConfig]:
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
