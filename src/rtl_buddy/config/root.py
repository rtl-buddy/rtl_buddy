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
    """Discover the project root config file by walking up from ``start_dir``.

    ``start_dir`` defaults to the current working directory. Command code
    should pass the command's resolved root (``dirname`` of its primary
    config file) so discovery does not depend on the directory the user
    happened to invoke ``rb`` from.
    """
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
    """Return the project root directory.

    Resolution order, walking up from ``start_dir`` (defaults to cwd):
      1. Directory containing root_config.yaml.
      2. Directory containing .git.
      3. ``start_dir`` itself — only when ``fallback_cwd=True``; otherwise
         raises :class:`FatalRtlBuddyError`.
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

    ``reg-cfg-path`` is the long-standing simulation entry. The per-flow
    keys are optional and exist for projects that keep a flow's manifest
    away from the project root (e.g. ``cdc_regression.yaml`` under
    ``lint/cdc/``), where the ``./<flow>_regression.yaml`` filename
    convention cannot find it (#389). Relative paths anchor to the
    directory containing ``root_config.yaml``.
    """

    path: str = field(rename="reg-cfg-path")
    synth_path: str | None = field(rename="synth-reg-cfg-path", default=None)
    power_path: str | None = field(rename="power-reg-cfg-path", default=None)
    fpga_path: str | None = field(rename="fpga-reg-cfg-path", default=None)
    cdc_path: str | None = field(rename="cdc-reg-cfg-path", default=None)
    fpv_path: str | None = field(rename="fpv-reg-cfg-path", default=None)


#: ``cfg-rtl-reg`` YAML key and :class:`RootRtlField` attribute per flow.
#: One table, consulted by the ``rb <flow>-regression`` commands and the
#: graph's config tier alike, so the two can never disagree about which
#: key names a flow's manifest.
REG_CFG_PATH_KEYS: dict[str, tuple[str, str]] = {
    "sim": ("reg-cfg-path", "path"),
    "synth": ("synth-reg-cfg-path", "synth_path"),
    "power": ("power-reg-cfg-path", "power_path"),
    "fpga": ("fpga-reg-cfg-path", "fpga_path"),
    "cdc": ("cdc-reg-cfg-path", "cdc_path"),
    "fpv": ("fpv-reg-cfg-path", "fpv_path"),
}


def load_reg_cfg_paths(root_cfg_path: str | Path) -> RootRtlField | None:
    """Read just the ``cfg-rtl-reg`` block of ``root_config.yaml``.

    The graph's config tier (and the flow-regression commands' fallback)
    anchor on a project root without loading the full :class:`RootConfig`
    — no builders or platforms are needed there, the same reasoning as
    :func:`~rtl_buddy.config.xplr.load_xplr_config`. A missing file or a
    missing block yields None; a block that does not parse is logged and
    also yields None, because callers of this lenient path treat the
    configured locations as best-effort hints and fall back to the
    filename convention (the full RootConfig load is where a malformed
    root config fails loudly).

    Lenient-on-missing is not lenient-on-*misspelled*: ``from_dict``
    ignores keys it does not know, so a ``cdc-reg-cfg-paths:`` typo would
    otherwise reproduce the exact silence #389 exists to remove — zero
    cdc-flow nodes and no diagnostic naming the key. Unknown keys are
    reported by name (and the known ones still honoured, since one typo
    should not cost a project the flows it spelled correctly).
    """
    path = Path(root_cfg_path)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        block = (data or {}).get("cfg-rtl-reg")
        if not isinstance(block, dict):
            return None
        unknown = sorted(set(block) - {key for key, _ in REG_CFG_PATH_KEYS.values()})
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

    Relative entries anchor to the root-config directory — the anchoring
    :meth:`RootConfig.get_rtl_reg_cfg` has always applied to
    ``reg-cfg-path``, kept identical for the per-flow keys so the graph
    and every ``rb <flow>-regression`` command resolve one file.
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
    """
    Root configuration for an entire project.

    Attributes:
      name (str): Unique root identifier.
      root_cfg_path (str): Path of the root config.
      builder_override (str): Name of builder configuration to override all others.
      extra_sim_timeout_override (int | None): ``--extra-sim-timeout`` value,
        overriding each builder's ``extra-sim-timeout``.
      rtl_builder_cfgs (dict[str, BuilderConfig]): Dictionary of available builder configurations, keyed by name.
      verible_cfgs (dict[str, VeribleConfig]): Dictionary of available verible configurations, keyed by name.
      platform_cfg (PlatformConfig): PlatformConfig selected based on current system.
      reg_cfg (RegConfig | None): RegConfig.
    """

    def __init__(
        self,
        name,
        builder_override=None,
        start_dir=None,
        extra_sim_timeout_override=None,
    ):
        """
        Constructor.

        Args:
          name (str): Unique root identifier.
          builder_override (str | None): Optional name of the builder to override test-specific builders.
          start_dir (str | Path | None): Directory to start the upward walk
            for ``root_config.yaml``. Defaults to the current working
            directory; command code should pass the command root so
            discovery doesn't depend on invocation cwd.
          extra_sim_timeout_override (int | None): Optional
            ``--extra-sim-timeout`` value, overriding every builder's
            ``extra-sim-timeout``.
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

        # Project-local env defaults must be in the environment *before*
        # any tool path field is expanded, and cfg-verible / cfg-surfer
        # resolve theirs inside this constructor. Idempotent and
        # fallback-only (a variable already set is never overridden), so
        # the later call in the CLI's context setup is a no-op.
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

            # Populate FPGA tool configs
            self.fpga_tool_cfgs = {
                cfg.name: FpgaToolConfig(cfg) for cfg in data.fpga_tools
            }

            # Populate FPGA platform configs (device part + default XDC)
            self.fpga_platform_cfgs = {
                cfg.name: FpgaPlatformConfig(cfg, self.root_cfg_path)
                for cfg in data.fpga_platforms
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

            # cfg-tools min-version overrides (optional). Entries carrying
            # a `platform:` selector are filtered against the active
            # platform below, once it has been selected.
            self._tool_version_files = list(data.tools)

            # cfg-xplr experiment-ledger policy (optional, single block)
            if data.xplr is not None:
                self.xplr_cfg = data.xplr.initialise()

            # cfg-dispatch execution backend (optional, single block)
            if data.dispatch is not None:
                self.dispatch_cfg = data.dispatch.initialise()

            # Record the regression config path; the RegConfig itself is
            # loaded lazily in get_rtl_reg_cfg() so non-simulation commands
            # (fpv, cdc, synth, ...) never touch regression.yaml or the
            # suite tests.yaml files it references (issue #248).
            self.cfg_rtl_reg = data.cfg_rtl_reg

            # Select platform config
            result = subprocess.run(
                ["uname"], capture_output=True, check=True, text=True
            )
            uname = result.stdout.strip()
            log_event(logger, logging.DEBUG, "platform.detected_uname", uname=uname)

            tool_blocks = {
                block: getattr(self, attr, {})
                for block, (attr, _) in PLATFORM_TOOL_BLOCKS.items()
            }

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
                            tool_blocks,
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

            # cfg-tools pins, now that the active platform is known: an
            # entry with a matching ``platform:`` wins over an unqualified
            # one for the same tool, and entries naming another platform
            # are dropped.
            self.tool_version_cfgs = self._select_tool_version_cfgs(
                self._tool_version_files
            )

    def _select_tool_version_cfgs(
        self, entries: list[ToolVersionConfigFile]
    ) -> dict[str, ToolVersionConfig]:
        """Resolve ``cfg-tools`` entries against the active platform.

        An entry's optional ``platform:`` names a ``cfg-platforms[].os``.
        Entries naming a *different* platform are dropped; a matching
        entry beats an unqualified one for the same tool regardless of
        declaration order, so a project can state the portable floor once
        and raise it where a platform pins a newer tool tree. Ordering
        among equally-qualified entries is last-wins, as before.
        """
        active_os = self.platform_cfg.get_os() if self.platform_cfg else None
        selected: dict[str, ToolVersionConfig] = {}
        pinned_for_platform: set[str] = set()
        for cfg in entries:
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
                # A platform-specific pin already won this tool.
                continue
            if cfg.platform is not None:
                pinned_for_platform.add(cfg.name)
            selected[cfg.name] = ToolVersionConfig.from_file(cfg)
        return selected

    def get_platform_tool_name(self, block: str) -> str | None:
        """
        Entry name the active platform routes for a tool block.

        Args:
          block (str): A :data:`~rtl_buddy.config.platform.PLATFORM_TOOL_BLOCKS`
            key, e.g. ``"surfer"`` or ``"fpv-tools"``.
        Returns:
          name (str | None): Routed ``cfg-<block>`` entry name, or None
            when this platform does not route the block — in which case
            the block keeps whatever global default it had.
        """
        if self.platform_cfg is None:
            return None
        return self.platform_cfg.get_routed_tool(block)

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

    def get_rtl_builder_cfg_by_name(self, name):
        """
        Get a builder configuration by its cfg-rtl-builder name.

        Args:
          name (str): Builder name as defined in cfg-rtl-builder.
        Returns:
          cfg (RtlBuilderConfig): The matching builder configuration.
        Raises:
          FatalRtlBuddyError: If no builder with that name is configured.
        """
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
        """
        Resolve the effective builder for a test.

        Precedence: a ``--builder`` CLI override forces the builder for every
        test (it "overrides all others"); otherwise a per-test/suite
        ``builder:`` selection wins; otherwise the platform default applies.

        Args:
          test_builder_name (str | None): Builder name from the test/suite
            ``builder:`` field, or None when unset.
        Returns:
          cfg (RtlBuilderConfig): The builder configuration to use.
        """
        if self.builder_override is None and test_builder_name is not None:
            return self.get_rtl_builder_cfg_by_name(test_builder_name)
        return self.get_rtl_builder_cfg()

    def resolve_extra_sim_timeout(self, rtl_builder_cfg):
        """
        Seconds to add to a test's simulation timeout under this builder.

        Precedence: ``--extra-sim-timeout`` overrides every builder;
        otherwise the builder's own ``extra-sim-timeout`` applies; otherwise
        nothing is added.

        Args:
          rtl_builder_cfg (RtlBuilderConfig): The builder in effect for the test.
        Returns:
          seconds (int): Extra seconds, 0 when neither is set.
        """
        if self.extra_sim_timeout_override is not None:
            return self.extra_sim_timeout_override
        return rtl_builder_cfg.get_extra_sim_timeout()

    def get_rtl_reg_cfg(self):
        """
        Get rtl regression configuration, reading one if it does not exist.

        Loading is deferred to this first call so commands that never
        consume the simulation regression config do not fail when
        regression.yaml or a referenced suite tests.yaml is absent
        (e.g. design-only sandboxed checkouts).

        Returns:
          cfg (RegConfig): The RTL Regression configuration.
        Raises:
          FatalRtlBuddyError: The regression config or a referenced
            suite config cannot be loaded.
        """
        if self.reg_cfg is None:
            self.reg_cfg = RegConfig(
                name=self.name + "/reg_config",
                path=resolve_reg_cfg_path(self.cfg_rtl_reg, self.root_cfg_path, "sim"),
            )
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

    def get_surfer_cfg(self, name: str | None = None) -> "SurferConfig | None":
        """
        Get Surfer configuration by name.

        Args:
          name (str | None): cfg-surfer entry name. When omitted, the
            active platform's ``cfg-platforms[].surfer`` routing decides,
            falling back to ``"surfer-default"`` when the platform routes
            nothing (the pre-#439 behaviour).
        Returns:
          cfg (SurferConfig|None): Matching Surfer configuration, if present.
        """
        if name is None:
            name = self.get_platform_tool_name("surfer") or "surfer-default"
        return self.surfer_cfgs.get(name)

    def _routed_or(self, block: str, name: str | None) -> str | None:
        """``name`` when given, else the active platform's routing for ``block``.

        An explicit selection — a ``tool:`` in synth.yaml/pnr.yaml/…, or a
        CLI flag — always wins; platform routing only supplies the default
        for a caller that did not name an entry. Same precedence the
        builder has had since ``cfg-platforms`` existed.
        """
        return name if name is not None else self.get_platform_tool_name(block)

    def get_synth_tool_cfg(self, name: str | None = None):
        """
        Get synthesis tool configuration by name.

        Args:
          name (str | None): Tool name as defined in cfg-synth-tools.
            When omitted, the active platform's ``cfg-platforms[].synth-tools``
            routing supplies it.
        Returns:
          cfg (SynthToolConfig): Matching synthesis tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured, or
            if no name was given and the platform routes nothing.
        """
        name = self._routed_or("synth-tools", name)
        cfg = self.synth_tool_cfgs.get(name) if name is not None else None
        if cfg is None:
            raise FatalRtlBuddyError(
                f"synthesis tool '{name}' not found in cfg-synth-tools"
            )
        return cfg

    def get_pnr_tool_cfg(self, name: str | None = None):
        """
        Get P&R tool configuration by name.

        Args:
          name (str | None): Tool name as defined in cfg-pnr-tools. When
            omitted, the active platform's ``cfg-platforms[].pnr-tools``
            routing supplies it.
        Returns:
          cfg (PnrToolConfig|None): Matching P&R tool configuration, or
            None if no entry with that name is configured. Callers fall
            back to the bare tool name on PATH when None is returned.
        """
        name = self._routed_or("pnr-tools", name)
        return self.pnr_tool_cfgs.get(name) if name is not None else None

    def get_power_tool_cfg(self, name: str | None = None):
        """
        Get power analysis tool configuration by name.

        Args:
          name (str | None): Tool name as defined in cfg-power-tools. When
            omitted, the active platform's ``cfg-platforms[].power-tools``
            routing supplies it.
        Returns:
          cfg (PowerToolConfig): Matching power tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        name = self._routed_or("power-tools", name)
        cfg = self.power_tool_cfgs.get(name) if name is not None else None
        if cfg is None:
            raise FatalRtlBuddyError(
                f"power tool '{name}' not found in cfg-power-tools"
            )
        return cfg

    def get_fpga_tool_cfg(self, name: str | None = None):
        """
        Get FPGA tool configuration by name.

        Args:
          name (str | None): Tool name as defined in cfg-fpga-tools. When
            omitted, the active platform's ``cfg-platforms[].fpga-tools``
            routing supplies it.
        Returns:
          cfg (FpgaToolConfig|None): Matching FPGA tool configuration, or
            None if no entry with that name is configured. Callers fall
            back to the bare tool name on PATH when None is returned.
        """
        name = self._routed_or("fpga-tools", name)
        return self.fpga_tool_cfgs.get(name) if name is not None else None

    def get_fpga_platform_cfg(self, name: str) -> FpgaPlatformConfig:
        """Get an FPGA platform configuration by name (cfg-fpga-platforms entry)."""
        cfg = self.fpga_platform_cfgs.get(name)
        if cfg is None:
            raise FatalRtlBuddyError(
                f"fpga platform '{name}' not found in cfg-fpga-platforms; "
                f"available: {sorted(self.fpga_platform_cfgs)}"
            )
        return cfg

    def get_cdc_tool_cfg(self, name: str | None = None):
        """
        Get CDC tool configuration by name.

        Args:
          name (str | None): Tool name as defined in cfg-cdc-tools. When
            omitted, the active platform's ``cfg-platforms[].cdc-tools``
            routing supplies it.
        Returns:
          cfg (CdcToolConfig): Matching CDC tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        name = self._routed_or("cdc-tools", name)
        cfg = self.cdc_tool_cfgs.get(name) if name is not None else None
        if cfg is None:
            raise FatalRtlBuddyError(f"CDC tool '{name}' not found in cfg-cdc-tools")
        return cfg

    def get_fpv_tool_cfg(self, name: str | None = None):
        """
        Get FPV tool configuration by name.

        Args:
          name (str | None): Tool name as defined in cfg-fpv-tools. When
            omitted, the active platform's ``cfg-platforms[].fpv-tools``
            routing supplies it.
        Returns:
          cfg (FpvToolConfig): Matching FPV tool configuration.
        Raises:
          FatalRtlBuddyError: If no tool with that name is configured.
        """
        name = self._routed_or("fpv-tools", name)
        cfg = self.fpv_tool_cfgs.get(name) if name is not None else None
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

    def get_tool_version_cfg(self, name: str) -> ToolVersionConfig | None:
        """Get optional ``cfg-tools`` min-version pin for the given tool name."""
        return self.tool_version_cfgs.get(name)

    def get_xplr_cfg(self) -> XplrConfig:
        """
        Get the xplr experiment-ledger configuration.

        Returns:
          cfg (XplrConfig): The cfg-xplr block, or the documented
            defaults when the block is absent. Note xplr commands
            themselves load this block leniently via
            ``config.xplr.load_xplr_config`` (they never construct a
            full RootConfig); this accessor is for code that already
            holds one.
        """
        return self.xplr_cfg

    def get_dispatch_cfg(self) -> DispatchConfig:
        """
        Get the validated dispatch (remote test execution) configuration.

        Returns:
          cfg (DispatchConfig): The initialised cfg-dispatch block, or
            defaults (backend None → local in-process execution) when absent.
        """
        return self.dispatch_cfg

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
