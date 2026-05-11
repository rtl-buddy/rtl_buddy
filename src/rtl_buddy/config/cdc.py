"""Configuration schema for CDC (clock-domain-crossing) lint runs.

Mirrors the synthesis schema (``config/synth.py``) at a smaller surface:
each ``cdc.yaml`` lists one or more analyses; each analysis names a
model + an SDC + optionally a waiver file; the project's
``root_config.yaml`` declares the available CDC tools under
``cfg-cdc-tools``.
"""

import logging
import os
import pprint
from dataclasses import dataclass

from serde import field, serde
from serde.yaml import from_yaml
from typing import Literal

from .model import ModelConfig, ModelConfigLoader
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# ---- tool config -----------------------------------------------------------


@dataclass
class CdcToolOpts:
    sync_depth: int | None = None
    extra_args: str = ""


@serde
class CdcToolOptsFile:
    sync_depth: int | None = field(rename="sync-depth", default=None)
    extra_args: str = field(rename="extra-args", default="")


@serde
class CdcToolConfigFile:
    name: str
    tool: str
    opts: CdcToolOptsFile = field(default_factory=CdcToolOptsFile)


class CdcToolConfig:
    """One entry from ``cfg-cdc-tools`` in ``root_config.yaml``."""

    def __init__(self, cfg: CdcToolConfigFile):
        self._cfg = cfg

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        return self._cfg.tool

    def get_opts(self, overrides: dict | None = None) -> CdcToolOpts:
        sync_depth = self._cfg.opts.sync_depth
        extra_args = self._cfg.opts.extra_args
        if overrides:
            sync_depth = overrides.get("sync_depth", sync_depth)
            extra_args = overrides.get("extra_args", extra_args)
        return CdcToolOpts(sync_depth=sync_depth, extra_args=extra_args)


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

    def get_name(self) -> str:
        return self.name

    def get_desc(self) -> str:
        return self.desc

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        return self.model.name

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
