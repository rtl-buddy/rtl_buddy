"""Configuration schema for FPV (formal property verification) runs.

Each ``fpv.yaml`` lists verifications: a model, property files, mode, depth and engines. ``root_config.yaml`` declares the tools under ``cfg-fpv-tools``.
"""

import logging
import os
import pprint
import re
from dataclasses import dataclass, field as dc_field

from serde import field, serde
from serde.yaml import from_yaml
from typing import Literal

from .model import ModelConfig, ModelConfigLoader, validate_top
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .toolpath import resolve_tool_path

logger = logging.getLogger(__name__)


# ---- tool config -----------------------------------------------------------


@dataclass
class FpvToolOpts:
    timeout: int | None = None
    extra_args: str = ""
    solver_versions: dict[str, str] = dc_field(default_factory=dict)
    # Absolute or project-relative path to the yosys-slang shared library; required when any verification uses `frontend: slang`.
    plugin_path: str | None = None


@serde
class FpvToolOptsFile:
    timeout: int | None = field(rename="timeout", default=None)
    extra_args: str = field(rename="extra-args", default="")
    # Solver name (yices, z3, boolector, btormc, abc) to exact version; SbyFpv fails on a mismatch.
    solver_versions: dict[str, str] = field(
        rename="solver-versions", default_factory=dict
    )
    plugin_path: str | None = field(rename="plugin-path", default=None)


@serde
class FpvToolConfigFile:
    name: str
    tool: str | list[str]
    opts: FpvToolOptsFile = field(default_factory=FpvToolOptsFile)


class FpvToolConfig:
    """One entry from ``cfg-fpv-tools`` in ``root_config.yaml``."""

    def __init__(self, cfg: FpvToolConfigFile, base_dir: str | None = None):
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
            block="cfg-fpv-tools",
            name=self._cfg.name,
            field="tool",
        )

    def get_opts(self, overrides: dict | None = None) -> FpvToolOpts:
        timeout = self._cfg.opts.timeout
        extra_args = self._cfg.opts.extra_args
        solver_versions = dict(self._cfg.opts.solver_versions)
        plugin_path = self._cfg.opts.plugin_path
        if overrides:
            timeout = overrides.get("timeout", timeout)
            extra_args = overrides.get("extra_args", extra_args)
            if "solver_versions" in overrides:
                solver_versions = dict(overrides["solver_versions"])
            plugin_path = overrides.get("plugin_path", plugin_path)
        return FpvToolOpts(
            timeout=timeout,
            extra_args=extra_args,
            solver_versions=solver_versions,
            plugin_path=plugin_path,
        )


# ---- per-verification config ----------------------------------------------


_VALID_MODES = ("bmc", "prove", "cover", "live")
# sby cover mode needs a BMC engine; prove-only engines such as `abc pdr` reject it.
_VACUITY_DEFAULT_ENGINE = "smtbmc yices"
_VALID_FRONTENDS = ("verilog", "slang")

# A `params:` name is emitted straight into a yosys script line.
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Characters a value may not carry because it is spliced verbatim into a yosys script line:
# whitespace splits the token, `#` comments out the rest of the line (including the source files),
# and `;` does not separate commands but reaches the frontend as a syntax error.
_SCRIPT_UNSAFE_RE = re.compile(r"[\s;#]")


def render_param_value(value: int | bool | str) -> str:
    """Render one `params:` value as a yosys script token.

    - `bool` becomes `1` or `0`.
    - `int` is written in decimal.
    - `str` is passed through as SystemVerilog expression text (`"8'h20"`). A string-typed parameter needs inner quotes (`MODE: '"small"'`).
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def validate_params(name: str, params: dict | None) -> dict[str, int | bool | str]:
    """Validate a verification's `params:` map, raising ``FatalRtlBuddyError`` at config load."""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise FatalRtlBuddyError(
            f"{name}: fpv `params:` must be a map of parameter name -> value"
        )
    out: dict[str, int | bool | str] = {}
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_NAME_RE.match(key):
            raise FatalRtlBuddyError(
                f"{name}: fpv `params:` name {key!r} is not a valid "
                f"SystemVerilog identifier"
            )
        # `bool` is an `int` subclass, so it passes here.
        if not isinstance(value, (int, str)):
            raise FatalRtlBuddyError(
                f"{name}: fpv `params:` value for '{key}' must be an integer, "
                f"a boolean, or a string holding a SystemVerilog literal "
                f"(got {type(value).__name__})"
            )
        rendered = render_param_value(value)
        if not rendered:
            raise FatalRtlBuddyError(
                f"{name}: fpv `params:` value for '{key}' may not be empty"
            )
        bad = _SCRIPT_UNSAFE_RE.search(rendered)
        if bad:
            raise FatalRtlBuddyError(
                f"{name}: fpv `params:` value for '{key}' may not contain "
                f"{bad.group(0)!r} — the token is spliced verbatim into a "
                f"yosys script line: {value!r}"
            )
        out[key] = value
    return out


@serde
class FpvConfigFile:
    name: str
    desc: str
    model: str
    model_path: str = field(rename="model_path")
    tool: str
    top: str | None = None
    properties: list[str] = field(default_factory=list)
    # SVA/Verilog file of environment `assume property` statements (clock, reset); read before `properties:`.
    constraints: str | None = None
    mode: str = "bmc"
    depth: int = 20
    engines: list[str] = field(default_factory=lambda: ["smtbmc yices"])
    # Top-module parameter overrides applied at elaboration; see `validate_params`.
    params: dict | None = None
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    # Spec coverage item ids, as in tests.yaml; read by `rb spec check-coverage` and the graph, no effect on the proof.
    covers: list[str] | None = None
    tool_overrides: dict | None = None
    # Default true for `bmc` / `prove`, false for `cover` / `live`: runs a secondary cover pass on every `a |-> b` antecedent to expose vacuous proofs.
    vacuity: bool | None = None
    # Engines for the vacuity cover pass. Default: the `smtbmc` entries of `engines:`, else `smtbmc yices`; `abc pdr` and other prove-only engines cannot run covers.
    vacuity_engines: list[str] | None = None
    # Default true: after the proof, report the % of design cells reachable from at least one assertion.
    coi: bool | None = None
    # "verilog" is yosys's native frontend with a limited SVA subset (no `|->`, `|=>` or sequences).
    # "slang" needs `cfg-fpv-tools[].opts.plugin-path` and is required for concurrent SVA implications and `bind`.
    frontend: str = "verilog"
    # Either flag turns a FAIL into XFAIL, which counts as a pass. An unexpected pass (XPASS) passes for `xfail` and fails for `xfail_strict`, which wins if both are set.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "FpvConfig":
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        properties = [os.path.join(config_dir, p) for p in self.properties]
        constraints = (
            os.path.join(config_dir, self.constraints) if self.constraints else None
        )
        if self.mode not in _VALID_MODES:
            raise FatalRtlBuddyError(
                f"{self.name}: fpv mode '{self.mode}' is not one of "
                f"{', '.join(_VALID_MODES)}"
            )
        if self.frontend not in _VALID_FRONTENDS:
            raise FatalRtlBuddyError(
                f"{self.name}: fpv frontend '{self.frontend}' is not one of "
                f"{', '.join(_VALID_FRONTENDS)}"
            )
        if self.vacuity_engines is not None and not self.vacuity_engines:
            raise FatalRtlBuddyError(
                f"{self.name}: fpv `vacuity_engines:` may not be empty; omit it "
                f"for the default or set `vacuity: false`"
            )
        params = validate_params(self.name, self.params)
        return FpvConfig(
            name=self.name,
            desc=self.desc,
            model=model,
            tool=self.tool,
            top=self.top or model.get_top(),
            properties=properties,
            constraints=constraints,
            mode=self.mode,
            depth=self.depth,
            engines=list(self.engines),
            _reglvl=self.reglvl,
            covers=self.covers,
            tool_overrides=self.tool_overrides,
            vacuity=self.vacuity,
            vacuity_engines=(
                list(self.vacuity_engines) if self.vacuity_engines else None
            ),
            coi=self.coi,
            frontend=self.frontend,
            params=params,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
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
    constraints: str | None = dc_field(default=None)
    # Same attribute name as `TestConfig.covers`; consumers read both alike.
    covers: list[str] | None = dc_field(default=None)
    tool_overrides: dict | None = dc_field(default=None)
    vacuity: bool | None = dc_field(default=None)
    vacuity_engines: list[str] | None = dc_field(default=None)
    coi: bool | None = dc_field(default=None)
    frontend: str = dc_field(default="verilog")
    # Insertion-ordered so the generated script is stable.
    params: dict[str, int | bool | str] = dc_field(default_factory=dict)
    xfail: bool = dc_field(default=False)
    xfail_strict: bool = dc_field(default=False)

    def get_frontend(self) -> str:
        return self.frontend

    def get_params(self) -> dict[str, int | bool | str]:
        # A copy: callers stamp the result onto graph nodes.
        return dict(self.params)

    def get_param_tokens(self) -> list[tuple[str, str]]:
        """`(name, yosys-ready value token)` pairs, in declaration order."""
        return [(k, render_param_value(v)) for k, v in self.params.items()]

    def is_xfail(self) -> bool:
        """Whether this verification is expected to fail (either flag set)."""
        return self.xfail or self.xfail_strict

    def get_xfail(self) -> bool:
        return self.xfail

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def vacuity_enabled(self) -> bool:
        """Whether to run the vacuity cover pass: `vacuity:` if set, else true for `bmc` and `prove` only."""
        if self.vacuity is not None:
            return bool(self.vacuity)
        return self.mode in ("bmc", "prove")

    def get_vacuity_engines(self) -> list[str]:
        """Engines for the vacuity cover pass: `vacuity_engines:` if set, else the `smtbmc` entries of `engines:`, else `smtbmc yices`."""
        if self.vacuity_engines:
            return list(self.vacuity_engines)
        cover_capable = [e for e in self.engines if e.split()[:1] == ["smtbmc"]]
        return cover_capable or [_VACUITY_DEFAULT_ENGINE]

    def coi_enabled(self) -> bool:
        """Whether to run the cone-of-influence coverage pass: `coi:` if set, else true."""
        if self.coi is not None:
            return bool(self.coi)
        return True

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

    def get_constraints(self) -> str | None:
        return self.constraints

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
        # A verification `top:` reaches yosys script lines, so it gets the model top's validation.
        for v in data.verifications:
            if v.top is not None:
                validate_top(
                    v.top,
                    v.name,
                    path,
                    subject="verification",
                    event="fpv_config.invalid_top",
                )
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
