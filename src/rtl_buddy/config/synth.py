import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field

from serde import serde, field
from serde.yaml import from_yaml
from typing import Literal

from .blocks import BlockRef, BlockRefFile, load_block_refs
from .model import ModelConfig, ModelConfigLoader
from .openroad_threads import validate_threads
from .pdk import _validate_dont_use_cells, merge_dont_use_cells
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .toolpath import resolve_tool_path

logger = logging.getLogger(__name__)


@serde
class SynthPlatformConfigFile:
    name: str
    pdk: str
    corner: str = ""
    # Added to the PDK's `dont-use-cells`, not replacing them.
    dont_use_cells: list[str] = field(rename="dont-use-cells", default_factory=list)


class SynthPlatformConfig:
    """A PDK plus corner selection for synthesis.

    `get_paths()` is the corner's standard-cell Liberty, one or more files, for STA and tech mapping; `get_lef_paths()` is the PDK's tech and macro LEF. Block-specific LEFs come from `SynthConfig.get_lef_paths()`.
    """

    def __init__(self, cfg: SynthPlatformConfigFile, pdk_lookup):
        self._name = cfg.name
        self._pdk_name = cfg.pdk

        pdk = pdk_lookup(cfg.pdk)
        self._corner = cfg.corner or pdk.get_default_corner()
        self._lib_paths = pdk.get_corner_paths(self._corner)
        self._lef_paths = [p for p in (pdk.get_tech_lef(), pdk.get_macro_lef()) if p]
        self._dont_use_cells = merge_dont_use_cells(
            pdk.get_dont_use_cells(),
            _validate_dont_use_cells(
                cfg.dont_use_cells, f"synth platform '{self._name}'"
            ),
        )

    def get_name(self) -> str:
        return self._name

    def get_pdk_name(self) -> str:
        return self._pdk_name

    def get_corner(self) -> str:
        return self._corner

    def get_paths(self) -> list[str]:
        return list(self._lib_paths)

    def get_lef_paths(self) -> list[str]:
        return list(self._lef_paths)

    def get_dont_use_cells(self) -> list[str]:
        """The PDK's excluded cells plus this platform's, PDK first."""
        return list(self._dont_use_cells)


@dataclass
class SynthToolOpts:
    synth_args: str = ""
    abc_args: str = ""
    # Mapped-run ABC script; empty means the built-in default.
    abc_script: str = ""
    strategy: str = ""
    frontend: str = "verilog"
    plugin_path: str = ""
    # One compilation unit for all sources (``read_slang --single-unit``); slang frontend only.
    single_unit: bool = False
    # Keep hierarchy (``read_slang --best-effort-hierarchy``); slang frontend only.
    best_effort_hierarchy: bool = False
    # Gate on `function`/`task` without `automatic`: "error", "warn" or "allow". See :func:`resolve_static_functions_mode`.
    static_functions: str = ""
    # Gate on Yosys "multiple conflicting drivers" warnings: "error" (default) or "allow".
    conflicting_drivers: str = ""
    # Gate on Yosys "Could not find interface instance" warnings: "error", "warn" (default) or "allow".
    unresolved_interfaces: str = ""


@serde
class SynthToolOptsFile:
    synth_args: str = field(rename="synth-args", default="")
    abc_args: str = field(rename="abc-args", default="")
    abc_script: str = field(rename="abc-script", default="")
    strategy: str = field(default="")
    frontend: str = field(default="verilog")
    plugin_path: str = field(rename="plugin-path", default="")
    single_unit: bool = field(rename="single-unit", default=False)
    best_effort_hierarchy: bool = field(rename="best-effort-hierarchy", default=False)
    static_functions: str = field(rename="static-functions", default="")
    conflicting_drivers: str = field(rename="conflicting-drivers", default="")
    unresolved_interfaces: str = field(rename="unresolved-interfaces", default="")


# Accepted values for the correctness gates.
STATIC_FUNCTIONS_MODES: tuple[str, ...] = ("error", "warn", "allow")
CONFLICTING_DRIVERS_MODES: tuple[str, ...] = ("error", "allow")
UNRESOLVED_INTERFACES_MODES: tuple[str, ...] = ("error", "warn", "allow")


def resolve_static_functions_mode(opts: SynthToolOpts) -> str:
    """Effective ``static-functions`` mode.

    The default is ``error`` for the slang frontend, which shares one net per formal across call sites, and ``warn`` for the verilog frontend. An explicit setting wins.
    """
    mode = (opts.static_functions or "").strip()
    if not mode:
        return "error" if opts.frontend == "slang" else "warn"
    if mode not in STATIC_FUNCTIONS_MODES:
        raise FatalRtlBuddyError(
            f"synth option static-functions must be one of "
            f"{', '.join(STATIC_FUNCTIONS_MODES)}, got {mode!r}"
        )
    return mode


def resolve_conflicting_drivers_mode(opts: SynthToolOpts) -> str:
    """Effective ``conflicting-drivers`` mode; defaults to ``error``."""
    mode = (opts.conflicting_drivers or "").strip()
    if not mode:
        return "error"
    if mode not in CONFLICTING_DRIVERS_MODES:
        raise FatalRtlBuddyError(
            f"synth option conflicting-drivers must be one of "
            f"{', '.join(CONFLICTING_DRIVERS_MODES)}, got {mode!r}"
        )
    return mode


def resolve_unresolved_interfaces_mode(opts: SynthToolOpts) -> str:
    """Effective ``unresolved-interfaces`` mode; defaults to ``warn``.

    With ``frontend: verilog``, an interface instance's own port connections can be left undriven, silently. ``error`` gates that; the slang frontend is unaffected.
    """
    mode = (opts.unresolved_interfaces or "").strip()
    if not mode:
        return "warn"
    if mode not in UNRESOLVED_INTERFACES_MODES:
        raise FatalRtlBuddyError(
            f"synth option unresolved-interfaces must be one of "
            f"{', '.join(UNRESOLVED_INTERFACES_MODES)}, got {mode!r}"
        )
    return mode


# Accepted keys of a ``tool_overrides.<tool>`` block: snake_case, unlike the kebab-case keys under ``cfg-synth-tools.opts``.
SYNTH_TOOL_OVERRIDE_KEYS: tuple[str, ...] = (
    "synth_args",
    "abc_args",
    "abc_script",
    "strategy",
    "frontend",
    "plugin_path",
    "single_unit",
    "best_effort_hierarchy",
    "static_functions",
    "conflicting_drivers",
    "unresolved_interfaces",
)

# Type-checked overrides, as key -> (type, label, hint). A quoted `"true"` would otherwise be truthy.
_SYNTH_OVERRIDE_TYPES: dict[str, tuple[type, str, str]] = {
    "abc_script": (str, "string", "write the ABC commands as one quoted string"),
    "single_unit": (bool, "bool", "write an unquoted YAML true/false"),
    "best_effort_hierarchy": (bool, "bool", "write an unquoted YAML true/false"),
    "static_functions": (
        str,
        "string",
        f"write one of {', '.join(STATIC_FUNCTIONS_MODES)}",
    ),
    "conflicting_drivers": (
        str,
        "string",
        f"write one of {', '.join(CONFLICTING_DRIVERS_MODES)}",
    ),
    "unresolved_interfaces": (
        str,
        "string",
        f"write one of {', '.join(UNRESOLVED_INTERFACES_MODES)}",
    ),
}


@serde
class SynthEffortYosysFile:
    synth_args: str = field(rename="synth-args", default="")
    abc_args: str = field(rename="abc-args", default="")
    abc_script: str = field(rename="abc-script", default="")


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

    def get_yosys_abc_script(self) -> str:
        return self._cfg.yosys.abc_script

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
    tool: str | list[str]
    opts: SynthToolOptsFile = field(default_factory=SynthToolOptsFile)


class SynthToolConfig:
    def __init__(self, cfg: SynthToolConfigFile, base_dir: str | None = None):
        self._cfg = cfg
        # Anchor for relative `tool:` candidates: the root_config.yaml directory, not the cwd.
        self._base_dir = base_dir

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        """Return the effective tool executable (see :mod:`rtl_buddy.config.toolpath`)."""
        return resolve_tool_path(
            self._cfg.tool,
            base_dir=self._base_dir,
            block="cfg-synth-tools",
            name=self._cfg.name,
            field="tool",
        )

    def _validate_overrides(self, overrides: dict) -> None:
        """Check a ``tool_overrides.<tool>`` block before it is merged.

        An unknown or kebab-case key is warned about and ignored. A wrongly typed value for a checked key is fatal.
        """
        unknown = sorted(
            (str(k) for k in overrides if k not in SYNTH_TOOL_OVERRIDE_KEYS)
        )
        if unknown:
            hints = [
                f"{key!r} -> {key.replace('-', '_')!r}"
                for key in unknown
                if key.replace("-", "_") in SYNTH_TOOL_OVERRIDE_KEYS
            ]
            log_event(
                logger,
                logging.WARNING,
                "synth_tool_config.unknown_override",
                tool=self._cfg.name,
                unknown=unknown,
                accepted=list(SYNTH_TOOL_OVERRIDE_KEYS),
                hints=hints,
            )

        for key, (expected, label, hint) in _SYNTH_OVERRIDE_TYPES.items():
            if key not in overrides:
                continue
            value = overrides[key]
            if not isinstance(value, expected):
                log_event(
                    logger,
                    logging.ERROR,
                    "synth_tool_config.override_type",
                    tool=self._cfg.name,
                    key=key,
                    expected=label,
                    got=type(value).__name__,
                )
                raise FatalRtlBuddyError(
                    f"tool_overrides.{self._cfg.name}.{key} must be a {label}, "
                    f"got {type(value).__name__} ({value!r}); {hint}"
                )

    def get_opts(self, overrides: dict | None = None) -> SynthToolOpts:
        synth_args = self._cfg.opts.synth_args
        abc_args = self._cfg.opts.abc_args
        abc_script = self._cfg.opts.abc_script
        strategy = self._cfg.opts.strategy
        frontend = self._cfg.opts.frontend
        plugin_path = self._cfg.opts.plugin_path
        single_unit = self._cfg.opts.single_unit
        best_effort_hierarchy = self._cfg.opts.best_effort_hierarchy
        static_functions = self._cfg.opts.static_functions
        conflicting_drivers = self._cfg.opts.conflicting_drivers
        unresolved_interfaces = self._cfg.opts.unresolved_interfaces
        if overrides:
            if not isinstance(overrides, dict):
                log_event(
                    logger,
                    logging.ERROR,
                    "synth_tool_config.override_not_mapping",
                    tool=self._cfg.name,
                    got=type(overrides).__name__,
                )
                raise FatalRtlBuddyError(
                    f"tool_overrides.{self._cfg.name} must be a mapping, "
                    f"got {type(overrides).__name__} ({overrides!r})"
                )
            self._validate_overrides(overrides)
            synth_args = overrides.get("synth_args", synth_args)
            abc_args = overrides.get("abc_args", abc_args)
            abc_script = overrides.get("abc_script", abc_script)
            strategy = overrides.get("strategy", strategy)
            frontend = overrides.get("frontend", frontend)
            plugin_path = overrides.get("plugin_path", plugin_path)
            single_unit = overrides.get("single_unit", single_unit)
            best_effort_hierarchy = overrides.get(
                "best_effort_hierarchy", best_effort_hierarchy
            )
            static_functions = overrides.get("static_functions", static_functions)
            conflicting_drivers = overrides.get(
                "conflicting_drivers", conflicting_drivers
            )
            unresolved_interfaces = overrides.get(
                "unresolved_interfaces", unresolved_interfaces
            )
        return SynthToolOpts(
            synth_args=synth_args,
            abc_args=abc_args,
            abc_script=abc_script,
            strategy=strategy,
            frontend=frontend,
            plugin_path=plugin_path,
            single_unit=single_unit,
            best_effort_hierarchy=best_effort_hierarchy,
            static_functions=static_functions,
            conflicting_drivers=conflicting_drivers,
            unresolved_interfaces=unresolved_interfaces,
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
    # Hardened blocks: each adds its abstract's Liberty and LEF like `lib-paths` / `lef-paths`. The model's filelist must leave the module undefined or a blackbox stub.
    blocks: list[BlockRefFile] = field(default_factory=list)
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    effort: str | None = None
    # OpenROAD threads for the `tool: openroad` timing stage: a positive integer or `auto`; unset means single-threaded.
    threads: int | str | None = None
    # Either flag marks the run expected-to-fail; `xfail_strict` fails on an unexpected pass. See docs/concepts/expected-failures.md.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "SynthConfig":
        threads = validate_threads(self.threads, where=f"synthesis '{self.name}'")
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
            blocks=load_block_refs(
                f"synthesis '{self.name}'",
                self.blocks,
                config_dir,
                default_pnr_path=None,
            ),
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            effort=self.effort,
            threads=threads,
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
    blocks: list[BlockRef] = dc_field(default_factory=list)
    threads: int | str | None = None
    xfail: bool = False
    xfail_strict: bool = False

    def is_xfail(self) -> bool:
        """Whether this run is expected to fail."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_effort_name(self) -> str | None:
        return self.effort

    def get_threads(self) -> int | str | None:
        """Validated `threads:`: a positive int, `auto`, or None."""
        return self.threads

    def get_name(self) -> str:
        return self.name

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        """The module this run elaborates: the model's ``top:`` override, else the model name."""
        return self.model.get_top()

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

    def get_blocks(self) -> list[BlockRef]:
        """The hardened blocks this run instances."""
        return list(self.blocks)

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

        # The dict comprehension below would silently keep the last duplicate.
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
        except FatalRtlBuddyError:
            raise
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
