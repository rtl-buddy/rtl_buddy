import logging

logger = logging.getLogger(__name__)
import os
import pprint
import subprocess
from pathlib import Path
from typing import Literal

from serde import serde, field
from serde.yaml import from_yaml

from .platform import PlatformConfigFile
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
from .fpv import FpvToolConfig, FpvToolConfigFile
from .systemc import SystemCConfig, SystemCConfigFile
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


def _discover_root_cfg(max_levels=8) -> str:
    """
    Discover the project root config file

    Args:
      max_levels (int) [8]: The maximum directory depth to search for 'root_config.yaml'.
    """
    path = os.getcwd()

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
            cwd=os.getcwd(),
            max_levels=max_levels,
        )
        return None


def discover_project_root(*, fallback_cwd: bool = False) -> Path:
    """Return the project root directory.

    Resolution order:
      1. Directory containing root_config.yaml (walked up from cwd).
      2. Directory containing .git (walked up from cwd).
      3. cwd — only when fallback_cwd=True; otherwise raises FatalRtlBuddyError.
    """
    cfg_path = _discover_root_cfg()
    if cfg_path is not None:
        return Path(cfg_path).parent
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        if (candidate / ".git").exists():
            return candidate
    if fallback_cwd:
        return Path.cwd()
    raise FatalRtlBuddyError(
        "cannot locate project root "
        "(no root_config.yaml or .git found above cwd). "
        "Run from inside a project or pass an explicit path."
    )


@serde
class RootRtlField:
    path: str = field(rename="reg-cfg-path")


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


class RootConfig:
    """
    Root configuration for an entire project.

    Attributes:
      name (str): Unique root identifier.
      root_cfg_path (str): Path of the root config.
      builder_override (str): Name of builder configuration to override all others.
      rtl_builder_cfgs (dict[str, BuilderConfig]): Dictionary of available builder configurations, keyed by name.
      verible_cfgs (dict[str, VeribleConfig]): Dictionary of available verible configurations, keyed by name.
      platform_cfg (PlatformConfig): PlatformConfig selected based on current system.
      reg_cfg (RegConfig | None): RegConfig.
    """

    def __init__(self, name, builder_override=None):
        """
        Constructor.

        Args:
          name (str): Unique root identifier.
          builder_override (str | None): Optional name of the builder to override test-specific builders.
        """

        self.name = name
        self.root_cfg_path = _discover_root_cfg()
        if self.root_cfg_path is None:
            raise FatalRtlBuddyError(
                "unable to discover root_config.yaml from current working directory"
            )
        log_event(
            logger, logging.INFO, "root_config.load_start", path=self.root_cfg_path
        )

        self.builder_override = builder_override

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
        self.cdc_tool_cfgs: dict = {}
        self.fpv_tool_cfgs: dict = {}
        self.synth_effort_cfgs: dict = {}
        self.systemc_cfg: SystemCConfig | None = None
        self.platform_cfg = None
        self.reg_cfg = None  # initialise later when get_rtl_reg_cfg is called

        data = None
        try:
            with open(self.root_cfg_path, "r") as file:
                data = from_yaml(RootConfigFile, file.read())

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
            # Populate builder configs
            self.rtl_builder_cfgs = {cfg.get_name(): cfg for cfg in data.builders}

            # Populate verible configs
            self.verible_cfgs = {
                cfg.name: cfg.initialise(self.root_cfg_path) for cfg in data.veribles
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
                cfg.name: SynthToolConfig(cfg) for cfg in data.synth_tools
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
                cfg.name: PnrToolConfig(cfg) for cfg in data.pnr_tools
            }

            # Populate power tool configs
            self.power_tool_cfgs = {
                cfg.name: PowerToolConfig(cfg) for cfg in data.power_tools
            }

            # Populate CDC tool configs
            self.cdc_tool_cfgs = {
                cfg.name: CdcToolConfig(cfg) for cfg in data.cdc_tools
            }

            # Populate FPV tool configs
            self.fpv_tool_cfgs = {
                cfg.name: FpvToolConfig(cfg) for cfg in data.fpv_tools
            }

            # Populate synth effort configs
            self.synth_effort_cfgs = {
                cfg.name: SynthEffortConfig(cfg) for cfg in data.synth_efforts
            }

            # SystemC config (optional, single block)
            if data.systemc is not None:
                self.systemc_cfg = data.systemc.initialise()

            # Initialise regression config
            self.cfg_rtl_reg = data.cfg_rtl_reg
            self.reg_cfg = RegConfig(
                name=self.name + "/reg_config",
                path=os.path.join(
                    os.path.dirname(self.root_cfg_path), self.cfg_rtl_reg.path
                ),
            )

            # Select platform config
            result = subprocess.run(
                ["uname"], capture_output=True, check=True, text=True
            )
            uname = result.stdout.strip()
            log_event(logger, logging.DEBUG, "platform.detected_uname", uname=uname)

            for platform_cfg in data.platforms:
                for cfg_uname in platform_cfg.get_unames():
                    if uname == cfg_uname:
                        log_event(
                            logger,
                            logging.DEBUG,
                            "platform.match",
                            os=platform_cfg.get_os(),
                            uname=uname,
                        )
                        self.platform_cfg = platform_cfg.initialise(
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
                log_event(
                    logger,
                    logging.INFO,
                    "platform.selected",
                    os=self.platform_cfg.get_os(),
                    builder=self.platform_cfg.get_builder().get_name(),
                    verible=self.platform_cfg.get_verible().get_name(),
                )

    @staticmethod
    def discover_rtl_builder_names(max_levels: int = 8) -> list[str]:
        """
        Discover configured RTL builder names from root_config.yaml.

        This helper only parses root_config.yaml and does not initialise
        platform/regression config.

        Args:
          max_levels (int) [8]: Maximum directory depth to search for root config.

        Returns:
          names (list[str]): Sorted list of configured builder names.

        Raises:
          ValueError: root_config.yaml cannot be found or parsed.
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
        """
        Retrieve the names of all the builders in rtl_builder_cfgs.

        Returns:
          names (list[RtlBuilderConfig]): A list of the builders.
        """
        return list(self.rtl_builder_cfgs.values())

    def get_builder_name(self):
        """
        Retrieve the name of the builder used by the platform.

        Returns:
          builder_name (str): The builder's name.
        """
        return self.platform_cfg.get_builder().get_name()

    def get_rtl_builder_cfg(self):
        """
        Get rtl builder configuration.

        Returns:
          cfg (RtlBuilderConfiguration): The configuration.
        """
        return self.platform_cfg.get_builder()

    def get_rtl_reg_cfg(self):
        """
        Get rtl regression configuration, reading one if it does not exist.

        Returns:
          cfg (RegConfig): The RTL Regression configuration.
        """
        return self.reg_cfg

    def get_verible_cfg(self):
        """
        Get verible configuration.

        Returns:
          cfg (VeribleConfig): Verible configuration corresponding to the current platform.
        """
        return self.platform_cfg.get_verible()

    def get_coverage_cfg(self, simulator_name: str):
        """
        Get coverage configuration for a simulator family.

        Args:
          simulator_name (str): Simulator family name, e.g. "verilator".
        Returns:
          cfg (CoverageConfig|None): Matching coverage configuration, if present.
        """
        return self.coverage_cfgs.get(simulator_name)

    def get_use_lcov(self, simulator_name: str) -> bool:
        """
        Query whether LCOV output should be emitted for the given simulator family.

        Args:
          simulator_name (str): Simulator family name, e.g. "verilator".
        Returns:
          use_lcov (bool): True when LCOV is enabled for this simulator.
        """
        cfg = self.get_coverage_cfg(simulator_name)
        return False if cfg is None else cfg.get_use_lcov()

    def get_coverview_cfg(self, simulator_name: str):
        """
        Get Coverview packaging configuration for a simulator family.

        Args:
          simulator_name (str): Simulator family name, e.g. "verilator".
        Returns:
          cfg (CoverviewConfig|None): Matching Coverview configuration, if present.
        """
        return self.coverview_cfgs.get(simulator_name)

    def get_surfer_cfg(self, name: str = "surfer-default") -> "SurferConfig | None":
        """
        Get Surfer configuration by name.

        Args:
          name (str): cfg-surfer entry name. Defaults to "surfer-default".
        Returns:
          cfg (SurferConfig|None): Matching Surfer configuration, if present.
        """
        return self.surfer_cfgs.get(name)

    def get_synth_tool_cfg(self, name: str):
        """
        Get synthesis tool configuration by name.

        Args:
          name (str): Tool name as defined in cfg-synth-tools.
        Returns:
          cfg (SynthToolConfig): Matching synthesis tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        cfg = self.synth_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synthesis tool '{name}' not found in cfg-synth-tools"
            )
        return cfg

    def get_pnr_tool_cfg(self, name: str):
        """
        Get P&R tool configuration by name.

        Args:
          name (str): Tool name as defined in cfg-pnr-tools.
        Returns:
          cfg (PnrToolConfig|None): Matching P&R tool configuration, or
            None if no entry with that name is configured. Callers fall
            back to the bare tool name on PATH when None is returned.
        """
        return self.pnr_tool_cfgs.get(name)

    def get_power_tool_cfg(self, name: str):
        """
        Get power analysis tool configuration by name.

        Args:
          name (str): Tool name as defined in cfg-power-tools.
        Returns:
          cfg (PowerToolConfig): Matching power tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        cfg = self.power_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"power tool '{name}' not found in cfg-power-tools"
            )
        return cfg

    def get_cdc_tool_cfg(self, name: str):
        """
        Get CDC tool configuration by name.

        Args:
          name (str): Tool name as defined in cfg-cdc-tools.
        Returns:
          cfg (CdcToolConfig): Matching CDC tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        cfg = self.cdc_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(f"CDC tool '{name}' not found in cfg-cdc-tools")
        return cfg

    def get_fpv_tool_cfg(self, name: str):
        """
        Get FPV tool configuration by name.

        Args:
          name (str): Tool name as defined in cfg-fpv-tools.
        Returns:
          cfg (FpvToolConfig): Matching FPV tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        cfg = self.fpv_tool_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(f"FPV tool '{name}' not found in cfg-fpv-tools")
        return cfg

    def get_pdk_cfg(self, name: str) -> PdkConfig:
        """Get a PDK configuration by name (cfg-pdks entry)."""
        cfg = self.pdk_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"PDK '{name}' not found in cfg-pdks; available: {sorted(self.pdk_cfgs)}"
            )
        return cfg

    def get_synth_platform_cfg(self, name: str) -> SynthPlatformConfig:
        """
        Get a synthesis platform configuration by name.

        Args:
          name (str): Platform name as defined in cfg-synth-platforms.
        Returns:
          cfg (SynthPlatformConfig): Matching synth platform configuration.
        Raises:
          FatalRtlBuddyError: If no platform with that name is configured.
        """
        cfg = self.synth_platform_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synth platform '{name}' not found in cfg-synth-platforms; "
                f"available: {sorted(self.synth_platform_cfgs)}"
            )
        return cfg

    def get_pnr_platform_cfg(self, name: str) -> PnrPlatformConfig:
        """Get a P&R platform configuration by name (cfg-pnr-platforms entry)."""
        cfg = self.pnr_platform_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"pnr platform '{name}' not found in cfg-pnr-platforms; "
                f"available: {sorted(self.pnr_platform_cfgs)}"
            )
        return cfg

    def get_synth_effort_cfg(self, name: str | None):
        """
        Get synthesis effort configuration by name.

        When name is None or no efforts are configured, returns a built-in
        default-standard effort with all knobs at their defaults.

        Args:
          name (str | None): Effort name as defined in cfg-synth-efforts.
        Returns:
          cfg (SynthEffortConfig): Matching effort configuration.
        Raises:
          FatalRtlBuddyError: If name is given but not configured.
        """
        if name is None:
            return default_effort_config()
        cfg = self.synth_effort_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synthesis effort '{name}' not found in cfg-synth-efforts"
            )
        return cfg

    def get_systemc_cfg(self) -> SystemCConfig | None:
        """
        Get the SystemC root configuration, if cfg-systemc is present.

        Returns:
          cfg (SystemCConfig | None): SystemC config, or None when cfg-systemc
            is absent. Callers (e.g. SystemCSim) decide whether absence is
            fatal — a project with no SystemC testbenches does not require it.
        """
        return self.systemc_cfg

    def get_project_rootdir(self):
        """
        Get abs path to project rootdir.

        Returns:
          path (str): The project rootdir.
        Raises:
          AssertionError: No directory can be derived from the path held in root_cfg_path.
        """
        path = os.path.dirname(self.root_cfg_path)
        if not os.path.isdir(path):
            path = "."
        return path

    def get_project_path(self, subpath: str):
        """
        Get abs path to project subdir.

        Args:
          subpath (str): Path of subdir.
        Returns
          path (str): Abs path.
        """
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
