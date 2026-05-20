"""Configuration schema for FPV (formal property verification) runs.

Mirrors the CDC schema (``config/cdc.py``): each ``fpv.yaml`` lists one
or more verification runs; each run names a model, the top module, a
list of SystemVerilog property files, the formal mode (bmc / prove /
cover), depth, and engines. The project's ``root_config.yaml``
declares the available FPV tools under ``cfg-fpv-tools``.
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

logger = logging.getLogger(__name__)


# ---- tool config -----------------------------------------------------------


@dataclass
class FpvToolOpts:
    timeout: int | None = None
    extra_args: str = ""
    solver_versions: dict[str, str] = dc_field(default_factory=dict)


@serde
class FpvToolOptsFile:
    timeout: int | None = field(rename="timeout", default=None)
    extra_args: str = field(rename="extra-args", default="")
    # Optional pins so CI proofs reproduce across machines. Map solver
    # name (yices / z3 / boolector / btormc / abc) -> exact version
    # string. SbyFpv probes each before running and hard-fails on
    # mismatch.
    solver_versions: dict[str, str] = field(
        rename="solver-versions", default_factory=dict
    )


@serde
class FpvToolConfigFile:
    name: str
    tool: str
    opts: FpvToolOptsFile = field(default_factory=FpvToolOptsFile)


class FpvToolConfig:
    """One entry from ``cfg-fpv-tools`` in ``root_config.yaml``."""

    def __init__(self, cfg: FpvToolConfigFile):
        self._cfg = cfg

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        return self._cfg.tool

    def get_opts(self, overrides: dict | None = None) -> FpvToolOpts:
        timeout = self._cfg.opts.timeout
        extra_args = self._cfg.opts.extra_args
        solver_versions = dict(self._cfg.opts.solver_versions)
        if overrides:
            timeout = overrides.get("timeout", timeout)
            extra_args = overrides.get("extra_args", extra_args)
            if "solver_versions" in overrides:
                solver_versions = dict(overrides["solver_versions"])
        return FpvToolOpts(
            timeout=timeout,
            extra_args=extra_args,
            solver_versions=solver_versions,
        )


# ---- per-verification config ----------------------------------------------


_VALID_MODES = ("bmc", "prove", "cover", "live")


@serde
class FpvConfigFile:
    name: str
    desc: str
    model: str
    model_path: str = field(rename="model_path")
    tool: str
    top: str | None = None
    properties: list[str] = field(default_factory=list)
    mode: str = "bmc"
    depth: int = 20
    engines: list[str] = field(default_factory=lambda: ["smtbmc yices"])
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None

    def initialise(self, config_dir: str) -> "FpvConfig":
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        properties = [os.path.join(config_dir, p) for p in self.properties]
        if self.mode not in _VALID_MODES:
            raise FatalRtlBuddyError(
                f"{self.name}: fpv mode '{self.mode}' is not one of "
                f"{', '.join(_VALID_MODES)}"
            )
        return FpvConfig(
            name=self.name,
            desc=self.desc,
            model=model,
            tool=self.tool,
            top=self.top or self.model,
            properties=properties,
            mode=self.mode,
            depth=self.depth,
            engines=list(self.engines),
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
        )


@dataclass
class FpvConfig:
    name: str
    desc: str
    model: ModelConfig
    tool: str
    top: str
    properties: list[str]
    mode: str
    depth: int
    engines: list[str]
    _reglvl: int | dict | None
    tool_overrides: dict | None = dc_field(default=None)

    def get_name(self) -> str:
        return self.name

    def get_desc(self) -> str:
        return self.desc

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        return self.top

    def get_properties(self) -> list[str]:
        return self.properties

    def get_mode(self) -> str:
        return self.mode

    def get_depth(self) -> int:
        return self.depth

    def get_engines(self) -> list[str]:
        return self.engines

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
                    "fpv_config.reglvl_malformed",
                    fpv=self.name,
                    tool=tool_name,
                )
                raise FatalRtlBuddyError(
                    f"Malformed fpv.yaml, specify reglvl for {self.name} with {tool_name} or default"
                )

    def __str__(self):
        return pprint.pformat(self)


# ---- suite (a single fpv.yaml) --------------------------------------------


@serde
class FpvSuiteConfigFile:
    filetype: Literal["fpv_config"] = field(rename="rtl-buddy-filetype")
    verifications: list[FpvConfigFile]


class FpvSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.verifications: dict[str, FpvConfig] = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(FpvSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "fpv_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.verifications = {
                v.name: v.initialise(config_dir) for v in data.verifications
            }
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "fpv_suite_config.verifications_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: verifications section malformed") from e

    def get_verifications(self, name: str | None = None) -> list[FpvConfig]:
        if name is not None:
            if name not in self.verifications:
                log_event(
                    logger,
                    logging.ERROR,
                    "fpv_suite_config.verification_missing",
                    path=self.path,
                    verification=name,
                )
                raise FatalRtlBuddyError(
                    f"FPV verification '{name}' not found in suite {self.path}"
                )
            return [self.verifications[name]]
        return list(self.verifications.values())

    def get_verification_names(self) -> list[str]:
        return list(self.verifications.keys())

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self)


# ---- regression (a list of fpv.yaml suites) -------------------------------


@serde
class FpvRegConfigFile:
    filetype: Literal["fpv_reg_config"] = field(rename="rtl-buddy-filetype")
    fpv_configs: list[str] = field(rename="fpv-configs", default_factory=list)


class FpvRegConfig:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.suite_configs: list[FpvSuiteConfig] = []
        try:
            with open(path, "r") as f:
                data = from_yaml(FpvRegConfigFile, f.read())
            self.suite_configs = [
                FpvSuiteConfig(os.path.join(os.path.dirname(path), p))
                for p in data.fpv_configs
            ]
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "fpv_reg_config.load_failed",
                name=name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from e

    def get_name(self) -> str:
        return self.name

    def get_path(self) -> str:
        return self.path

    def get_suite_configs(self) -> list[FpvSuiteConfig]:
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
