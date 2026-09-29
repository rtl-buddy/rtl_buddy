"""Configuration schema for CDC (clock-domain-crossing) lint runs.

Each ``cdc.yaml`` lists analyses (model, constraints, optional waivers); ``root_config.yaml`` declares the tools under ``cfg-cdc-tools``.
"""

import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field

from serde import field, serde
from serde.yaml import from_yaml
from typing import Literal

from .model import ModelConfig, ModelConfigLoader
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .toolpath import resolve_tool_path

logger = logging.getLogger(__name__)


# ---- tool config -----------------------------------------------------------


@dataclass
class CdcToolOpts:
    sync_depth: int | None = None
    extra_args: str = ""
    # Vivado backend only; ignored by rtl-buddy-cdc.
    part: str | None = None


@serde
class CdcToolOptsFile:
    sync_depth: int | None = field(rename="sync-depth", default=None)
    extra_args: str = field(rename="extra-args", default="")
    part: str | None = None


@serde
class CdcToolConfigFile:
    name: str
    tool: str | list[str]
    opts: CdcToolOptsFile = field(default_factory=CdcToolOptsFile)


class CdcToolConfig:
    """One entry from ``cfg-cdc-tools`` in ``root_config.yaml``."""

    def __init__(self, cfg: CdcToolConfigFile, base_dir: str | None = None):
        self._cfg = cfg
        # Never the process cwd: rb is often run from a suite directory.
        self._base_dir = base_dir

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        """Tool executable with ``~`` and ``$VAR`` expanded; see :mod:`rtl_buddy.config.toolpath`."""
        return resolve_tool_path(
            self._cfg.tool,
            base_dir=self._base_dir,
            block="cfg-cdc-tools",
            name=self._cfg.name,
            field="tool",
        )

    def get_opts(self, overrides: dict | None = None) -> CdcToolOpts:
        sync_depth = self._cfg.opts.sync_depth
        extra_args = self._cfg.opts.extra_args
        part = self._cfg.opts.part
        if overrides:
            sync_depth = overrides.get("sync_depth", sync_depth)
            extra_args = overrides.get("extra_args", extra_args)
            part = overrides.get("part", part)
        return CdcToolOpts(sync_depth=sync_depth, extra_args=extra_args, part=part)


# ---- per-analysis config ---------------------------------------------------


@serde
class CdcConfigFile:
    name: str
    desc: str
    model: str
    model_path: str = field(rename="model_path")
    tool: str
    constraints: str
    waivers: str | None = None
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    # Passed through as --frontend; the analyzer validates the value.
    frontend: str | None = None
    # Passed as --single-unit: parse all sources as one compilation unit.
    single_unit: bool = False
    # Each module is passed as --blackbox <module>; the analyzer validates names.
    blackbox: list[str] = field(default_factory=list)
    # Instance regexes for `--check-xdc` treated as real synchronizers the analyzer did not recognize. A matching crossing must still be constrained, but a correct XDC waiver of it is not reported as an over-waive.
    recognized_syncs: list[str] = field(rename="recognized-syncs", default_factory=list)
    # Either flag turns a FAIL into XFAIL. An unexpected pass (XPASS) passes for `xfail` and fails for `xfail_strict`. `xfail_strict` wins if both are set.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "CdcConfig":
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        constraints = os.path.join(config_dir, self.constraints)
        waivers = (
            os.path.join(config_dir, self.waivers) if self.waivers is not None else None
        )
        return CdcConfig(
            name=self.name,
            desc=self.desc,
            model=model,
            tool=self.tool,
            constraints=constraints,
            waivers=waivers,
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            frontend=self.frontend,
            single_unit=self.single_unit,
            blackbox=self.blackbox,
            recognized_syncs=list(self.recognized_syncs),
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
        )


@dataclass
class CdcConfig:
    name: str
    desc: str
    model: ModelConfig
    tool: str
    constraints: str
    waivers: str | None
    _reglvl: int | dict | None
    tool_overrides: dict | None
    frontend: str | None = None
    single_unit: bool = False
    blackbox: list[str] = dc_field(default_factory=list)
    recognized_syncs: list[str] = dc_field(default_factory=list)
    xfail: bool = False
    xfail_strict: bool = False

    def get_recognized_syncs(self) -> list[str]:
        """Instance-path regexes treated as recognized synchronizers by `--check-xdc`."""
        return list(self.recognized_syncs)

    def is_xfail(self) -> bool:
        """Whether this analysis is expected to fail (either flag set)."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_name(self) -> str:
        return self.name

    def get_desc(self) -> str:
        return self.desc

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        """The module this run elaborates (see :meth:`ModelConfig.get_top`)."""
        return self.model.get_top()

    def get_constraints(self) -> str:
        return self.constraints

    def get_waivers(self) -> str | None:
        return self.waivers

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
                    "cdc_config.reglvl_malformed",
                    cdc=self.name,
                    tool=tool_name,
                )
                raise FatalRtlBuddyError(
                    f"Malformed cdc.yaml, specify reglvl for {self.name} with {tool_name} or default"
                )

    def __str__(self):
        return pprint.pformat(self)


# ---- suite (a single cdc.yaml) --------------------------------------------


@serde
class CdcSuiteConfigFile:
    filetype: Literal["cdc_config"] = field(rename="rtl-buddy-filetype")
    analyses: list[CdcConfigFile]


class CdcSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.analyses: dict[str, CdcConfig] = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(CdcSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "cdc_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        # The dict comprehension below would silently drop the first of two same-named analyses.
        seen: dict[str, int] = {}
        for idx, analysis in enumerate(data.analyses):
            if analysis.name in seen:
                log_event(
                    logger,
                    logging.ERROR,
                    "cdc_suite_config.duplicate_analysis",
                    path=path,
                    name=analysis.name,
                    first_index=seen[analysis.name],
                    second_index=idx,
                )
                raise FatalRtlBuddyError(
                    f"{path}: duplicate analysis name {analysis.name!r}"
                )
            seen[analysis.name] = idx

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.analyses = {a.name: a.initialise(config_dir) for a in data.analyses}
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "cdc_suite_config.analyses_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: analyses section malformed") from e

    def get_analyses(self, name: str | None = None) -> list[CdcConfig]:
        if name is not None:
            if name not in self.analyses:
                log_event(
                    logger,
                    logging.ERROR,
                    "cdc_suite_config.analysis_missing",
                    path=self.path,
                    analysis=name,
                )
                raise FatalRtlBuddyError(
                    f"CDC analysis '{name}' not found in suite {self.path}"
                )
            return [self.analyses[name]]
        return list(self.analyses.values())

    def get_analysis_names(self) -> list[str]:
        return list(self.analyses.keys())

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self)


# ---- regression (a list of cdc.yaml suites) -------------------------------


@serde
class CdcRegConfigFile:
    filetype: Literal["cdc_reg_config"] = field(rename="rtl-buddy-filetype")
    cdc_configs: list[str] = field(rename="cdc-configs", default_factory=list)


class CdcRegConfig:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.suite_configs: list[CdcSuiteConfig] = []
        try:
            with open(path, "r") as f:
                data = from_yaml(CdcRegConfigFile, f.read())
            self.suite_configs = [
                CdcSuiteConfig(os.path.join(os.path.dirname(path), p))
                for p in data.cdc_configs
            ]
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "cdc_reg_config.load_failed",
                name=name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from e

    def get_name(self) -> str:
        return self.name

    def get_path(self) -> str:
        return self.path

    def get_suite_configs(self) -> list[CdcSuiteConfig]:
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
