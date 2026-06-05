import logging
import os
import pprint
from dataclasses import dataclass
from typing import Literal

from serde import field, serde
from serde.yaml import from_yaml

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .pnr import PnrSuiteConfig
from .synth import SynthSuiteConfig

logger = logging.getLogger(__name__)


@serde
class PowerToolConfigFile:
    name: str
    tool: str


class PowerToolConfig:
    def __init__(self, cfg: PowerToolConfigFile):
        self._cfg = cfg

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        return self._cfg.tool


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
    constraints: str | None = None
    platform: str = ""
    activity: PowerActivityFile = field(default_factory=PowerActivityFile)
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
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

        if not self.platform:
            raise FatalRtlBuddyError(
                f"power run '{self.name}': missing 'platform' "
                "(name of a cfg-pnr-platforms entry)"
            )

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
            activity=activity,
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
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

    def get_constraints(self) -> str | None:
        return self.constraints

    def get_platform(self) -> str:
        return self.platform

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
