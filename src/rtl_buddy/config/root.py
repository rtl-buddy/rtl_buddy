import logging

logger = logging.getLogger(__name__)
import os
import pprint
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from serde import serde, field, from_dict
from serde.yaml import from_yaml

from .platform import PLATFORM_TOOL_BLOCKS, PlatformConfigFile
from .reg import RegConfig
from .rtl import RtlBuilderConfig
from .verible import VeribleConfigFile
from .coverage import CoverageConfigFile
from .coverview import CoverviewConfigFile
from .surfer import SurferConfig, SurferConfigFile
from .synth import (
    SynthToolConfig,
    SynthToolConfigFile,
    SynthPlatformConfig,
    SynthPlatformConfigFile,
    SynthEffortConfig,
    SynthEffortConfigFile,
    default_effort_config,
)
from .pdk import PdkConfig, PdkConfigFile
from .pnr import PnrToolConfig, PnrToolConfigFile
from .pnr_platform import PnrPlatformConfig, PnrPlatformConfigFile
from .power import PowerToolConfig, PowerToolConfigFile
from .cdc import CdcToolConfig, CdcToolConfigFile
from .fpga import FpgaToolConfig, FpgaToolConfigFile
from .fpga_platform import FpgaPlatformConfig, FpgaPlatformConfigFile
from .fpv import FpvToolConfig, FpvToolConfigFile
from .systemc import SystemCConfig, SystemCConfigFile
from .tools import ToolVersionConfig, ToolVersionConfigFile
from .xplr import XplrConfig, XplrConfigFile
from .dispatch import DispatchConfig, DispatchConfigFile
from .env_file import apply_env_file
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


def _discover_root_cfg(max_levels=8, start_dir: str | Path | None = None) -> str:
    """Find ``root_config.yaml`` by walking up from ``start_dir`` (default: cwd); None if not found."""
    start = os.path.abspath(str(start_dir)) if start_dir is not None else os.getcwd()
    path = start

    level = 0
    while level < max_levels and not os.path.isfile(path + "/root_config.yaml"):
        path = os.path.dirname(path)
        level += 1

    filepath = path + "/root_config.yaml"
    if os.path.isfile(filepath):
        log_event(logger, logging.DEBUG, "root_config.discovered", path=filepath)
        return filepath
    else:
        log_event(
            logger,
            logging.ERROR,
            "root_config.not_found",
            cwd=start,
            max_levels=max_levels,
        )
        return None


def discover_project_root(
    *, fallback_cwd: bool = False, start_dir: str | Path | None = None
) -> Path:
    """Return the project root, walking up from ``start_dir`` (default: cwd).

    Order: the directory with root_config.yaml, then the one with .git, then ``start_dir`` itself if ``fallback_cwd=True``; otherwise raises :class:`FatalRtlBuddyError`.
    """
    start = Path(start_dir).resolve() if start_dir is not None else Path.cwd()
    cfg_path = _discover_root_cfg(start_dir=start)
    if cfg_path is not None:
        return Path(cfg_path).parent
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    if fallback_cwd:
        return start
    raise FatalRtlBuddyError(
        "cannot locate project root "
        "(no root_config.yaml or .git found above "
        f"{start}). Run from inside a project or pass an explicit path."
    )


@serde
class RootRtlField:
    """The ``cfg-rtl-reg`` block: where each flow's regression manifest lives.

    ``reg-cfg-path`` is the simulation manifest; the per-flow keys are optional. Relative paths anchor to the ``root_config.yaml`` directory. ``shared-build-root`` is the shared-build cache root, not a manifest.
    """

    path: str = field(rename="reg-cfg-path")
    synth_path: str | None = field(rename="synth-reg-cfg-path", default=None)
    power_path: str | None = field(rename="power-reg-cfg-path", default=None)
    fpga_path: str | None = field(rename="fpga-reg-cfg-path", default=None)
    cdc_path: str | None = field(rename="cdc-reg-cfg-path", default=None)
    fpv_path: str | None = field(rename="fpv-reg-cfg-path", default=None)
    lint_path: str | None = field(rename="lint-reg-cfg-path", default=None)
    elab_path: str | None = field(rename="elab-reg-cfg-path", default=None)
    shared_build_root: str | None = field(rename="shared-build-root", default=None)


#: ``cfg-rtl-reg`` keys that are not a manifest path; exempt from the unknown-key warning.
_REG_CFG_NON_PATH_KEYS = frozenset({"shared-build-root"})

#: ``cfg-rtl-reg`` YAML key and :class:`RootRtlField` attribute per flow.
REG_CFG_PATH_KEYS: dict[str, tuple[str, str]] = {
    "sim": ("reg-cfg-path", "path"),
    "synth": ("synth-reg-cfg-path", "synth_path"),
    "power": ("power-reg-cfg-path", "power_path"),
    "fpga": ("fpga-reg-cfg-path", "fpga_path"),
    "cdc": ("cdc-reg-cfg-path", "cdc_path"),
    "fpv": ("fpv-reg-cfg-path", "fpv_path"),
    "lint": ("lint-reg-cfg-path", "lint_path"),
    "elab": ("elab-reg-cfg-path", "elab_path"),
}


def load_reg_cfg_paths(root_cfg_path: str | Path) -> RootRtlField | None:
    """Read only the ``cfg-rtl-reg`` block of ``root_config.yaml``, without loading a full :class:`RootConfig`.

    A missing file or block, or a block that fails to parse (logged), yields None. Unknown keys are warned about by name; the known keys are still honoured.
    """
    path = Path(root_cfg_path)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        block = (data or {}).get("cfg-rtl-reg")
        if not isinstance(block, dict):
            return None
        unknown = sorted(
            set(block)
            - {key for key, _ in REG_CFG_PATH_KEYS.values()}
            - _REG_CFG_NON_PATH_KEYS
        )
        if unknown:
            log_event(
                logger,
                logging.WARNING,
                "root_config.reg_cfg_unknown_keys",
                path=str(path),
                keys=", ".join(unknown),
                known=", ".join(key for key, _ in REG_CFG_PATH_KEYS.values()),
            )
        return from_dict(RootRtlField, block)
    except Exception as e:
        log_event(
            logger,
            logging.WARNING,
            "root_config.reg_cfg_block_unreadable",
            path=str(path),
            error=str(e),
        )
        return None


def resolve_reg_cfg_path(
    reg_paths: "RootRtlField | None", root_cfg_path: str | Path, flow: str
) -> str | None:
    """Absolute path of ``flow``'s configured regression manifest, or None.

    A relative entry anchors to the root-config directory.
    """
    _, attr = REG_CFG_PATH_KEYS[flow]
    raw = getattr(reg_paths, attr, None) if reg_paths is not None else None
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(str(root_cfg_path))), raw)
    )


#: ``cfg-platforms`` keys naming a non-routable ``cfg-*-tools`` block (see :data:`~rtl_buddy.config.platform.PLATFORM_TOOL_BLOCKS`). pyserde would ignore them silently.
_UNROUTABLE_PLATFORM_KEYS: dict[str, str] = {
    "synth-tools": "cfg-synth-tools",
    "pnr-tools": "cfg-pnr-tools",
    "power-tools": "cfg-power-tools",
    "cdc-tools": "cfg-cdc-tools",
    "fpv-tools": "cfg-fpv-tools",
    "fpga-tools": "cfg-fpga-tools",
}


def _reject_unroutable_platform_keys(raw: dict) -> None:
    """Fail on a ``cfg-platforms`` key that would parse but never bind."""
    for entry in raw.get("cfg-platforms") or []:
        if not isinstance(entry, dict):
            continue
        for key, block in _UNROUTABLE_PLATFORM_KEYS.items():
            if key not in entry:
                continue
            os_name = entry.get("os", "?")
            log_event(
                logger,
                logging.ERROR,
                "platform.tool_not_routable",
                block=key,
                os=os_name,
            )
            raise FatalRtlBuddyError(
                f"cfg-platforms[{os_name}].{key}: {block} cannot be routed per "
                "platform — its entry name is chosen by the flow yaml's "
                "'tool:' and doubles as the backend selector. Pin the binary "
                "in the entry itself instead, with a candidate list: "
                'tool: ["${RB_TOOLS}/bin/yosys", "/opt/rb-tools/bin/yosys", "yosys"]'
            )


def _detect_uname() -> str:
    """This host's ``uname``, matched against ``cfg-platforms[].unames``."""
    result = subprocess.run(["uname"], capture_output=True, check=True, text=True)
    uname = result.stdout.strip()
    log_event(logger, logging.DEBUG, "platform.detected_uname", uname=uname)
    return uname


def _match_platform(
    platforms: list[PlatformConfigFile], uname: str
) -> PlatformConfigFile | None:
    """The ``cfg-platforms`` entry this host selects, or None; the last match wins."""
    matched: PlatformConfigFile | None = None
    for entry in platforms:
        if uname in entry.get_unames():
            matched = entry
    return matched


@serde
class RootConfigFile:
    filetype: Literal["project_root_config"] = field(rename="rtl-buddy-filetype")
    cfg_rtl_reg: RootRtlField = field(rename="cfg-rtl-reg")
    builders: list[RtlBuilderConfig] = field(rename="cfg-rtl-builder")
    platforms: list[PlatformConfigFile] = field(rename="cfg-platforms")
    veribles: list[VeribleConfigFile] = field(
        rename="cfg-verible", default_factory=list
    )
    coverages: list[CoverageConfigFile] = field(
        rename="cfg-coverage", default_factory=list
    )
    coverviews: list[CoverviewConfigFile] = field(
        rename="cfg-coverview", default_factory=list
    )
    surfers: list[SurferConfigFile] = field(rename="cfg-surfer", default_factory=list)
    synth_tools: list[SynthToolConfigFile] = field(
        rename="cfg-synth-tools", default_factory=list
    )
    pdks: list[PdkConfigFile] = field(rename="cfg-pdks", default_factory=list)
    synth_platforms: list[SynthPlatformConfigFile] = field(
        rename="cfg-synth-platforms", default_factory=list
    )
    pnr_platforms: list[PnrPlatformConfigFile] = field(
        rename="cfg-pnr-platforms", default_factory=list
    )
    pnr_tools: list[PnrToolConfigFile] = field(
        rename="cfg-pnr-tools", default_factory=list
    )
    power_tools: list[PowerToolConfigFile] = field(
        rename="cfg-power-tools", default_factory=list
    )
    fpga_tools: list[FpgaToolConfigFile] = field(
        rename="cfg-fpga-tools", default_factory=list
    )
    fpga_platforms: list[FpgaPlatformConfigFile] = field(
        rename="cfg-fpga-platforms", default_factory=list
    )
    cdc_tools: list[CdcToolConfigFile] = field(
        rename="cfg-cdc-tools", default_factory=list
    )
    fpv_tools: list[FpvToolConfigFile] = field(
        rename="cfg-fpv-tools", default_factory=list
    )
    synth_efforts: list[SynthEffortConfigFile] = field(
        rename="cfg-synth-efforts", default_factory=list
    )
    systemc: SystemCConfigFile | None = field(rename="cfg-systemc", default=None)
    tools: list[ToolVersionConfigFile] = field(rename="cfg-tools", default_factory=list)
    xplr: XplrConfigFile | None = field(rename="cfg-xplr", default=None)
    dispatch: DispatchConfigFile | None = field(rename="cfg-dispatch", default=None)


class RootConfig:
    """The loaded ``root_config.yaml`` of a project.

    `builder_override` (``--builder``) forces one builder for every test. `extra_sim_timeout_override` (``--extra-sim-timeout``) replaces each builder's ``extra-sim-timeout``. `platform_cfg` is the entry selected for this host. `reg_cfg` loads on first use.
    """

    def __init__(
        self,
        name,
        builder_override=None,
        start_dir=None,
        extra_sim_timeout_override=None,
    ):
        """Load the root config found by walking up from `start_dir` (default: cwd).

        Raises FatalRtlBuddyError if it is missing, unparseable or matches no platform.
        """
        self.name = name
        self.root_cfg_path = _discover_root_cfg(start_dir=start_dir)
        if self.root_cfg_path is None:
            raise FatalRtlBuddyError(
                "unable to discover root_config.yaml from current working directory"
            )
        log_event(
            logger, logging.INFO, "root_config.load_start", path=self.root_cfg_path
        )

        # The env file must load before any tool path field is expanded.
        apply_env_file(os.path.dirname(self.root_cfg_path))

        self.builder_override = builder_override
        self.extra_sim_timeout_override = extra_sim_timeout_override

        self.rtl_builder_cfgs = dict()
        self.verible_cfgs = dict()
        self.coverage_cfgs = dict()
        self.coverview_cfgs = dict()
        self.surfer_cfgs: dict = {}
        self.synth_tool_cfgs = dict()
        self.pdk_cfgs: dict = {}
        self.synth_platform_cfgs: dict = {}
        self.pnr_platform_cfgs: dict = {}
        self.pnr_tool_cfgs: dict = {}
        self.power_tool_cfgs: dict = {}
        self.fpga_tool_cfgs: dict = {}
        self.fpga_platform_cfgs: dict = {}
        self.cdc_tool_cfgs: dict = {}
        self.fpv_tool_cfgs: dict = {}
        self.synth_effort_cfgs: dict = {}
        self.systemc_cfg: SystemCConfig | None = None
        self.tool_version_cfgs: dict[str, ToolVersionConfig] = {}
        self._tool_version_files: list[ToolVersionConfigFile] = []
        self.xplr_cfg: XplrConfig = XplrConfigFile().initialise()
        self.dispatch_cfg: DispatchConfig = DispatchConfigFile().initialise()
        self.platform_cfg = None
        self.reg_cfg = None

        data = None
        raw: dict = {}
        try:
            with open(self.root_cfg_path, "r") as file:
                text = file.read()
            data = from_yaml(RootConfigFile, text)
            # Untyped second read: pyserde drops unknown keys that `_reject_unroutable_platform_keys` must see.
            reparsed = yaml.safe_load(text)
            raw = reparsed if isinstance(reparsed, dict) else {}

        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "root_config.load_failed",
                name=self.name,
                path=self.root_cfg_path,
                error=e,
            )
            raise FatalRtlBuddyError(
                f'{self.name}: failed to load "{self.root_cfg_path}"'
            ) from e

        if data is not None:
            # Anchor for relative tool path candidates.
            cfg_dir = os.path.dirname(self.root_cfg_path)

            # Populate builder configs
            self.rtl_builder_cfgs = {cfg.get_name(): cfg for cfg in data.builders}
            for builder_cfg in self.rtl_builder_cfgs.values():
                builder_cfg.set_base_dir(cfg_dir)

            # Matched before the tool blocks so pin diagnostics can be scoped to the active entry.
            uname = _detect_uname()
            active_platform_file = _match_platform(data.platforms, uname)

            # Only the routed verible entry warns about a broken pin.
            active_verible = (
                active_platform_file.verible
                if active_platform_file is not None
                else None
            )
            self.verible_cfgs = {
                cfg.name: cfg.initialise(
                    self.root_cfg_path, diagnostics=cfg.name == active_verible
                )
                for cfg in data.veribles
            }

            # Populate coverage configs
            self.coverage_cfgs = {cfg.name: cfg.initialise() for cfg in data.coverages}
            self.coverview_cfgs = {
                cfg.name: cfg.initialise(self.root_cfg_path) for cfg in data.coverviews
            }
            self.surfer_cfgs = {
                cfg.name: cfg.initialise(self.root_cfg_path) for cfg in data.surfers
            }

            # Populate synth tool configs
            self.synth_tool_cfgs = {
                cfg.name: SynthToolConfig(cfg, cfg_dir) for cfg in data.synth_tools
            }

            # Populate PDK configs (referenced by synth + pnr platforms)
            self.pdk_cfgs = {
                cfg.name: PdkConfig(cfg, self.root_cfg_path) for cfg in data.pdks
            }

            def _pdk_lookup(name: str) -> PdkConfig:
                pdk = self.pdk_cfgs.get(name)
                if pdk is None:
                    raise FatalRtlBuddyError(
                        f"PDK '{name}' not found in cfg-pdks; "
                        f"available: {sorted(self.pdk_cfgs)}"
                    )
                return pdk

            # Populate synth platform configs (referencing PDKs by name)
            self.synth_platform_cfgs = {
                cfg.name: SynthPlatformConfig(cfg, _pdk_lookup)
                for cfg in data.synth_platforms
            }

            # Populate P&R platform configs
            self.pnr_platform_cfgs = {
                cfg.name: PnrPlatformConfig(cfg, _pdk_lookup)
                for cfg in data.pnr_platforms
            }

            # Populate P&R tool configs
            self.pnr_tool_cfgs = {
                cfg.name: PnrToolConfig(cfg, cfg_dir) for cfg in data.pnr_tools
            }

            # Populate power tool configs
            self.power_tool_cfgs = {
                cfg.name: PowerToolConfig(cfg, cfg_dir) for cfg in data.power_tools
            }

            # Populate FPGA tool configs
            self.fpga_tool_cfgs = {
                cfg.name: FpgaToolConfig(cfg, cfg_dir) for cfg in data.fpga_tools
            }

            # Populate FPGA platform configs (device part + default XDC)
            self.fpga_platform_cfgs = {
                cfg.name: FpgaPlatformConfig(cfg, self.root_cfg_path)
                for cfg in data.fpga_platforms
            }

            # Populate CDC tool configs
            self.cdc_tool_cfgs = {
                cfg.name: CdcToolConfig(cfg, cfg_dir) for cfg in data.cdc_tools
            }

            # Populate FPV tool configs
            self.fpv_tool_cfgs = {
                cfg.name: FpvToolConfig(cfg, cfg_dir) for cfg in data.fpv_tools
            }

            # Populate synth effort configs
            self.synth_effort_cfgs = {
                cfg.name: SynthEffortConfig(cfg) for cfg in data.synth_efforts
            }

            # SystemC config (optional, single block)
            if data.systemc is not None:
                self.systemc_cfg = data.systemc.initialise()

            # Filtered by `platform:` once the active platform is known.
            self._tool_version_files = list(data.tools)

            # cfg-xplr experiment-ledger policy (optional, single block)
            if data.xplr is not None:
                self.xplr_cfg = data.xplr.initialise()

            # cfg-dispatch execution backend (optional, single block)
            if data.dispatch is not None:
                self.dispatch_cfg = data.dispatch.initialise()

            # RegConfig loads lazily in get_rtl_reg_cfg() so non-simulation commands never read regression.yaml or suite files.
            self.cfg_rtl_reg = data.cfg_rtl_reg

            # Built here because it references the blocks above.
            tool_blocks = {
                block: getattr(self, attr, {})
                for block, (attr, _) in PLATFORM_TOOL_BLOCKS.items()
            }

            # Validates every entry, not only this host's; `PlatformConfigFile.initialise` does not repeat it.
            for platform_cfg in data.platforms:
                platform_cfg.validate_routing(tool_blocks)
            _reject_unroutable_platform_keys(raw)

            if active_platform_file is not None:
                log_event(
                    logger,
                    logging.DEBUG,
                    "platform.match",
                    os=active_platform_file.get_os(),
                    uname=uname,
                )
                self.platform_cfg = active_platform_file.initialise(
                    self.rtl_builder_cfgs,
                    self.verible_cfgs,
                    self.builder_override,
                )

            if self.platform_cfg is None:
                log_event(
                    logger,
                    logging.ERROR,
                    "platform.match_missing",
                    name=self.name,
                    uname=uname,
                )
                raise FatalRtlBuddyError(
                    f"{self.name}: cannot find cfg-platform for uname {uname}"
                )
            else:
                routed = self.platform_cfg.get_routed_tools()
                log_event(
                    logger,
                    logging.INFO,
                    "platform.selected",
                    os=self.platform_cfg.get_os(),
                    builder=self.platform_cfg.get_builder().get_name(),
                    verible=self.platform_cfg.get_verible().get_name(),
                    routed=", ".join(f"{k}={v}" for k, v in sorted(routed.items()))
                    or "-",
                )

            # Needs the active platform.
            self.tool_version_cfgs = self._select_tool_version_cfgs(
                self._tool_version_files,
                known_platforms={p.get_os() for p in data.platforms},
            )

    def _select_tool_version_cfgs(
        self,
        entries: list[ToolVersionConfigFile],
        known_platforms: set[str],
    ) -> dict[str, ToolVersionConfig]:
        """Resolve ``cfg-tools`` entries against the active platform.

        Entries naming another platform are dropped. An entry matching the active platform beats an unqualified one for the same tool; among equals the last wins. A ``platform:`` outside ``known_platforms`` (every ``cfg-platforms[].os``) raises FatalRtlBuddyError.
        """
        active_os = self.platform_cfg.get_os() if self.platform_cfg else None
        selected: dict[str, ToolVersionConfig] = {}
        pinned_for_platform: set[str] = set()
        for cfg in entries:
            if cfg.platform is not None and cfg.platform not in known_platforms:
                log_event(
                    logger,
                    logging.ERROR,
                    "tool_version.platform_unknown",
                    name=cfg.name,
                    entry_platform=cfg.platform,
                    available=", ".join(sorted(known_platforms)),
                )
                raise FatalRtlBuddyError(
                    f'cfg-tools[{cfg.name}].platform: "{cfg.platform}" is not a '
                    f"configured cfg-platforms os "
                    f"(available: {sorted(known_platforms)})"
                )
            if cfg.platform is not None and cfg.platform != active_os:
                log_event(
                    logger,
                    logging.DEBUG,
                    "tool_version.platform_skipped",
                    name=cfg.name,
                    entry_platform=cfg.platform,
                    active_platform=active_os,
                )
                continue
            if cfg.platform is None and cfg.name in pinned_for_platform:
                continue
            if cfg.platform is not None:
                pinned_for_platform.add(cfg.name)
            selected[cfg.name] = ToolVersionConfig.from_file(cfg)
        return selected

    def get_platform_tool_name(self, block: str) -> str | None:
        """Return the entry name the active platform routes for `block` (a ``PLATFORM_TOOL_BLOCKS`` key such as ``"surfer"``), or None."""
        if self.platform_cfg is None:
            return None
        return self.platform_cfg.get_routed_tool(block)

    @staticmethod
    def discover_rtl_builder_names(max_levels: int = 8) -> list[str]:
        """Return the sorted builder names in root_config.yaml, searching up to `max_levels` directories; does not initialise platform or regression config.

        Raises ValueError if the file cannot be found or parsed, or has no builders.
        """
        root_cfg_path = _discover_root_cfg(max_levels=max_levels)
        if root_cfg_path is None:
            raise ValueError(
                "unable to discover root_config.yaml from current working directory"
            )

        try:
            with open(root_cfg_path, "r") as file:
                data = from_yaml(RootConfigFile, file.read())
        except Exception as e:
            raise ValueError(f'failed to parse "{root_cfg_path}" ({e})') from e

        builder_names = sorted({cfg.get_name() for cfg in data.builders})
        if len(builder_names) == 0:
            raise ValueError(
                f'no builders configured in "{root_cfg_path}" (cfg-rtl-builder is empty)'
            )

        return builder_names

    def get_rtl_builders(self) -> list[RtlBuilderConfig]:
        """Return all configured builders."""
        return list(self.rtl_builder_cfgs.values())

    def get_builder_name(self):
        """Return the name of the platform's builder."""
        return self.platform_cfg.get_builder().get_name()

    def get_rtl_builder_cfg(self):
        """Return the platform's builder configuration."""
        return self.platform_cfg.get_builder()

    def get_rtl_builder_cfg_by_name(self, name):
        """Return the ``cfg-rtl-builder`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.rtl_builder_cfgs.get(name)
        if cfg is None:
            log_event(
                logger,
                logging.ERROR,
                "builder.not_found",
                builder=name,
                available=list(self.rtl_builder_cfgs.keys()),
            )
            raise FatalRtlBuddyError(f'builder "{name}" not found in cfg-rtl-builder')
        return cfg

    def resolve_rtl_builder_cfg(self, test_builder_name=None):
        """Return the effective builder for a test.

        Precedence: the ``--builder`` override, then the test or suite ``builder:`` (`test_builder_name`), then the platform default.
        """
        if self.builder_override is None and test_builder_name is not None:
            return self.get_rtl_builder_cfg_by_name(test_builder_name)
        return self.get_rtl_builder_cfg()

    def resolve_extra_sim_timeout(self, rtl_builder_cfg):
        """Return the seconds to add to a test's simulation timeout: ``--extra-sim-timeout``, else the builder's ``extra-sim-timeout``, else 0."""
        if self.extra_sim_timeout_override is not None:
            return self.extra_sim_timeout_override
        return rtl_builder_cfg.get_extra_sim_timeout()

    def get_rtl_reg_cfg(self):
        """Return the simulation regression config, loading it on first call.

        Raises FatalRtlBuddyError if it or a referenced suite cannot be loaded.
        """
        if self.reg_cfg is None:
            self.reg_cfg = RegConfig(
                name=self.name + "/reg_config",
                path=resolve_reg_cfg_path(self.cfg_rtl_reg, self.root_cfg_path, "sim"),
            )
        return self.reg_cfg

    def get_verible_cfg(self):
        """Return the platform's Verible configuration."""
        return self.platform_cfg.get_verible()

    def get_coverage_cfg(self, simulator_name: str):
        """Return the coverage configuration for a simulator family (e.g. "verilator"), or None."""
        return self.coverage_cfgs.get(simulator_name)

    def get_use_lcov(self, simulator_name: str) -> bool:
        """Whether LCOV output is enabled for a simulator family."""
        cfg = self.get_coverage_cfg(simulator_name)
        return False if cfg is None else cfg.get_use_lcov()

    def get_coverview_cfg(self, simulator_name: str):
        """Return the Coverview packaging configuration for a simulator family, or None."""
        return self.coverview_cfgs.get(simulator_name)

    def get_surfer_cfg(self, name: str | None = None) -> "SurferConfig | None":
        """Return the ``cfg-surfer`` entry `name`, or None.

        When `name` is omitted, the platform's ``surfer`` routing decides, else ``"surfer-default"``.
        """
        if name is None:
            name = self.get_platform_tool_name("surfer") or "surfer-default"
        return self.surfer_cfgs.get(name)

    def get_synth_tool_cfg(self, name: str):
        """Return the ``cfg-synth-tools`` entry `name` (the flow YAML's ``tool:``); raises FatalRtlBuddyError if absent.

        The block is not routable per platform; pin a path with a candidate list in ``tool:``.
        """
        cfg = self.synth_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synthesis tool '{name}' not found in cfg-synth-tools"
            )
        return cfg

    def get_pnr_tool_cfg(self, name: str):
        """Return the ``cfg-pnr-tools`` entry `name`, or None (callers then use the bare name on PATH)."""
        return self.pnr_tool_cfgs.get(name)

    def get_power_tool_cfg(self, name: str):
        """Return the ``cfg-power-tools`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.power_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"power tool '{name}' not found in cfg-power-tools"
            )
        return cfg

    def get_fpga_tool_cfg(self, name: str):
        """Return the ``cfg-fpga-tools`` entry `name`, or None (callers then use the bare name on PATH)."""
        return self.fpga_tool_cfgs.get(name)

    def get_fpga_platform_cfg(self, name: str) -> FpgaPlatformConfig:
        """Return the ``cfg-fpga-platforms`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.fpga_platform_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"fpga platform '{name}' not found in cfg-fpga-platforms; "
                f"available: {sorted(self.fpga_platform_cfgs)}"
            )
        return cfg

    def get_cdc_tool_cfg(self, name: str):
        """Return the ``cfg-cdc-tools`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.cdc_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(f"CDC tool '{name}' not found in cfg-cdc-tools")
        return cfg

    def get_fpv_tool_cfg(self, name: str):
        """Return the ``cfg-fpv-tools`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.fpv_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(f"FPV tool '{name}' not found in cfg-fpv-tools")
        return cfg

    def get_pdk_cfg(self, name: str) -> PdkConfig:
        """Return the ``cfg-pdks`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.pdk_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"PDK '{name}' not found in cfg-pdks; available: {sorted(self.pdk_cfgs)}"
            )
        return cfg

    def get_synth_platform_cfg(self, name: str) -> SynthPlatformConfig:
        """Return the ``cfg-synth-platforms`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.synth_platform_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synth platform '{name}' not found in cfg-synth-platforms; "
                f"available: {sorted(self.synth_platform_cfgs)}"
            )
        return cfg

    def get_pnr_platform_cfg(self, name: str) -> PnrPlatformConfig:
        """Return the ``cfg-pnr-platforms`` entry `name`; raises FatalRtlBuddyError if absent."""
        cfg = self.pnr_platform_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"pnr platform '{name}' not found in cfg-pnr-platforms; "
                f"available: {sorted(self.pnr_platform_cfgs)}"
            )
        return cfg

    def get_synth_effort_cfg(self, name: str | None):
        """Return the ``cfg-synth-efforts`` entry `name`; raises FatalRtlBuddyError if absent.

        With `name` None, returns the built-in "standard" effort.
        """
        if name is None:
            return default_effort_config()
        cfg = self.synth_effort_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synthesis effort '{name}' not found in cfg-synth-efforts"
            )
        return cfg

    def get_tool_version_cfg(self, name: str) -> ToolVersionConfig | None:
        """Return the ``cfg-tools`` min-version pin for a tool, or None."""
        return self.tool_version_cfgs.get(name)

    def get_xplr_cfg(self) -> XplrConfig:
        """Return the ``cfg-xplr`` block, or its defaults when absent."""
        return self.xplr_cfg

    def get_dispatch_cfg(self) -> DispatchConfig:
        """Return the ``cfg-dispatch`` block, or defaults (local in-process execution) when absent."""
        return self.dispatch_cfg

    def get_systemc_cfg(self) -> SystemCConfig | None:
        """Return the ``cfg-systemc`` block, or None when absent."""
        return self.systemc_cfg

    def get_project_rootdir(self):
        """Return the absolute path of the project root directory."""
        path = os.path.dirname(self.root_cfg_path)
        if not os.path.isdir(path):
            path = "."
        return path

    def get_shared_build_root(self) -> str | None:
        """Return ``cfg-rtl-reg.shared-build-root`` unresolved, or None.

        Resolution is done by :func:`~rtl_buddy.tools.vlog_sim.resolve_shared_build_root`.
        """
        reg = getattr(self, "cfg_rtl_reg", None)
        return getattr(reg, "shared_build_root", None) if reg is not None else None

    def get_project_path(self, subpath: str):
        """Return the absolute path of `subpath` under the project root; raises FatalRtlBuddyError if it is not a directory."""
        root_dir = self.get_project_rootdir()
        path = os.path.join(root_dir, subpath)
        if not os.path.isdir(path):
            log_event(
                logger, logging.ERROR, "project_path.missing_directory", path=path
            )
            raise FatalRtlBuddyError(f"{path} is not a directory")
        return path

    def __str__(self):
        return pprint.pformat(self)
