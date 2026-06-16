import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal

from .model import ModelConfig, ModelConfigLoader
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


@serde
class SynthPlatformConfigFile:
    name: str
    pdk: str
    corner: str = ""


class SynthPlatformConfig:
    """A synthesis-side view of a PDK + corner selection.

    Backends consume `get_path()` (Liberty for STA / tech mapping) and
    `get_lef_paths()` (tech + macro LEF from the PDK). Block-specific
    LEFs live on the per-run synth.yaml (`SynthConfig.get_lef_paths()`).
    """

    def __init__(self, cfg: SynthPlatformConfigFile, pdk_lookup):
        self._name = cfg.name
        self._pdk_name = cfg.pdk

        pdk = pdk_lookup(cfg.pdk)
        self._corner = cfg.corner or pdk.get_default_corner()
        self._lib_path = pdk.get_corner_path(self._corner)
        self._lef_paths = [p for p in (pdk.get_tech_lef(), pdk.get_macro_lef()) if p]

    def get_name(self) -> str:
        return self._name

    def get_pdk_name(self) -> str:
        return self._pdk_name

    def get_corner(self) -> str:
        return self._corner

    def get_path(self) -> str:
        return self._lib_path

    def get_lef_paths(self) -> list[str]:
        return list(self._lef_paths)


@dataclass
class SynthToolOpts:
    synth_args: str = ""
    abc_args: str = ""
    strategy: str = ""
    frontend: str = "verilog"
    plugin_path: str = ""


@serde
class SynthToolOptsFile:
    synth_args: str = field(rename="synth-args", default="")
    abc_args: str = field(rename="abc-args", default="")
    strategy: str = field(default="")
    frontend: str = field(default="verilog")
    plugin_path: str = field(rename="plugin-path", default="")


@serde
class SynthEffortYosysFile:
    synth_args: str = field(rename="synth-args", default="")
    abc_args: str = field(rename="abc-args", default="")


@serde
class SynthEffortOpenroadFile:
    run: bool = True
    pre_sta_tcl: str = field(rename="pre-sta-tcl", default="")


@serde
class SynthEffortConfigFile:
    name: str
    yosys: SynthEffortYosysFile = field(default_factory=SynthEffortYosysFile)
    openroad: SynthEffortOpenroadFile = field(default_factory=SynthEffortOpenroadFile)


class SynthEffortConfig:
    def __init__(self, cfg: SynthEffortConfigFile):
        self._cfg = cfg

    def get_name(self) -> str:
        return self._cfg.name

    def get_yosys_synth_args(self) -> str:
        return self._cfg.yosys.synth_args

    def get_yosys_abc_args(self) -> str:
        return self._cfg.yosys.abc_args

    def get_openroad_run(self) -> bool:
        return self._cfg.openroad.run

    def get_openroad_pre_sta_tcl(self) -> str:
        return self._cfg.openroad.pre_sta_tcl


_DEFAULT_EFFORT_NAME = "standard"


def default_effort_config() -> SynthEffortConfig:
    """Built-in fallback when root-config defines no cfg-synth-efforts."""
    return SynthEffortConfig(SynthEffortConfigFile(name=_DEFAULT_EFFORT_NAME))


@serde
class SynthToolConfigFile:
    name: str
    tool: str
    opts: SynthToolOptsFile = field(default_factory=SynthToolOptsFile)


class SynthToolConfig:
    def __init__(self, cfg: SynthToolConfigFile):
        self._cfg = cfg

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        return self._cfg.tool

    def get_opts(self, overrides: dict | None = None) -> SynthToolOpts:
        synth_args = self._cfg.opts.synth_args
        abc_args = self._cfg.opts.abc_args
        strategy = self._cfg.opts.strategy
        frontend = self._cfg.opts.frontend
        plugin_path = self._cfg.opts.plugin_path
        if overrides:
            synth_args = overrides.get("synth_args", synth_args)
            abc_args = overrides.get("abc_args", abc_args)
            strategy = overrides.get("strategy", strategy)
            frontend = overrides.get("frontend", frontend)
            plugin_path = overrides.get("plugin_path", plugin_path)
        return SynthToolOpts(
            synth_args=synth_args,
            abc_args=abc_args,
            strategy=strategy,
            frontend=frontend,
            plugin_path=plugin_path,
        )


@serde
class SynthConfigFile:
    name: str
    desc: str
    model: str
    model_path: str = field(rename="model_path")
    tool: str
    constraints: str | None = None
    params: dict | None = None
    defines: dict | None = None
    platform: str | None = None
    lef_paths: list[str] = field(rename="lef-paths", default_factory=list)
    lib_paths: list[str] = field(rename="lib-paths", default_factory=list)
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    effort: str | None = None
    # Expected-fail markers (pytest-style). Either marks this run
    # expected-to-fail; `xfail` is non-strict (an unexpected pass still
    # passes), `xfail_strict` is strict (an unexpected pass is a failure).
    # See docs/concepts/expected-failures.md.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "SynthConfig":
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        constraints = (
            os.path.join(config_dir, self.constraints)
            if self.constraints is not None
            else None
        )
        lef_paths = [
            os.path.normpath(os.path.join(config_dir, p)) for p in self.lef_paths
        ]
        lib_paths = [
            os.path.normpath(os.path.join(config_dir, p)) for p in self.lib_paths
        ]
        return SynthConfig(
            name=self.name,
            desc=self.desc,
            model=model,
            tool=self.tool,
            constraints=constraints,
            params=self.params,
            defines=self.defines,
            platform=self.platform,
            lef_paths=lef_paths,
            lib_paths=lib_paths,
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            effort=self.effort,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
        )


@dataclass
class SynthConfig:
    name: str
    desc: str
    model: ModelConfig
    tool: str
    constraints: str | None
    params: dict | None
    defines: dict | None
    platform: str | None
    _reglvl: int | dict | None
    tool_overrides: dict | None
    effort: str | None = None
    lef_paths: list[str] = dc_field(default_factory=list)
    lib_paths: list[str] = dc_field(default_factory=list)
    xfail: bool = False
    xfail_strict: bool = False

    def is_xfail(self) -> bool:
        """Whether this run is expected to fail (either flag set)."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_effort_name(self) -> str | None:
        return self.effort

    def get_name(self) -> str:
        return self.name

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        return self.model.name

    def get_constraints(self) -> str | None:
        return self.constraints

    def get_params(self) -> dict | None:
        return self.params

    def get_defines(self) -> dict | None:
        return self.defines

    def get_platform(self) -> str | None:
        return self.platform

    def get_lef_paths(self) -> list[str]:
        return list(self.lef_paths)

    def get_lib_paths(self) -> list[str]:
        return list(self.lib_paths)

    def get_tool_name(self) -> str:
        return self.tool

    def get_tool_overrides_for(self, tool_name: str) -> dict | None:
        if self.tool_overrides is None:
            return None
        return self.tool_overrides.get(tool_name)

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
                    "synth_config.reglvl_malformed",
                    synth=self.name,
                    tool=tool_name,
                )
                raise FatalRtlBuddyError(
                    f"Malformed synth.yaml, specify reglvl for {self.name} with {tool_name} or default"
                )

    def __str__(self):
        return pprint.pformat(self)


@serde
class SynthSuiteConfigFile:
    filetype: Literal["synth_config"] = field(rename="rtl-buddy-filetype")
    syntheses: list[SynthConfigFile]


class SynthSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.syntheses = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(SynthSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "synth_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        # Fail loud on duplicate ``name:`` — same rationale as the
        # cdc.yaml side: the dict-comprehension below would silently
        # overwrite the first synthesis with the second.
        seen: dict[str, int] = {}
        for idx, synthesis in enumerate(data.syntheses):
            if synthesis.name in seen:
                log_event(
                    logger,
                    logging.ERROR,
                    "synth_suite_config.duplicate_synthesis",
                    path=path,
                    name=synthesis.name,
                    first_index=seen[synthesis.name],
                    second_index=idx,
                )
                raise FatalRtlBuddyError(
                    f"{path}: duplicate synthesis name {synthesis.name!r}"
                )
            seen[synthesis.name] = idx

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.syntheses = {s.name: s.initialise(config_dir) for s in data.syntheses}
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "synth_suite_config.syntheses_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: syntheses section malformed") from e

    def get_syntheses(self, name: str | None = None) -> list[SynthConfig]:
        if name is not None:
            if name not in self.syntheses:
                log_event(
                    logger,
                    logging.ERROR,
                    "synth_suite_config.synth_missing",
                    path=self.path,
                    synth=name,
                )
                raise FatalRtlBuddyError(
                    f"synthesis '{name}' not found in suite {self.path}"
                )
            return [self.syntheses[name]]
        return list(self.syntheses.values())

    def get_synth_names(self) -> list[str]:
        return list(self.syntheses.keys())

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self)


@serde
class SynthRegConfigFile:
    filetype: Literal["synth_reg_config"] = field(rename="rtl-buddy-filetype")
    synth_configs: list[str] = field(rename="synth-configs", default_factory=list)


class SynthRegConfig:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.suite_configs = []
        try:
            with open(path, "r") as f:
                data = from_yaml(SynthRegConfigFile, f.read())
            self.suite_configs = [
                SynthSuiteConfig(os.path.join(os.path.dirname(path), p))
                for p in data.synth_configs
            ]
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "synth_reg_config.load_failed",
                name=name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from e

    def get_name(self) -> str:
        return self.name

    def get_path(self) -> str:
        return self.path

    def get_suite_configs(self) -> list[SynthSuiteConfig]:
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
