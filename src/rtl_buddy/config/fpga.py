import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field
from typing import Literal

from serde import field, serde
from serde.yaml import from_yaml

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .model import ModelConfig, ModelConfigLoader

logger = logging.getLogger(__name__)


@serde
class FpgaToolConfigFile:
    name: str
    tool: str


class FpgaToolConfig:
    def __init__(self, cfg: FpgaToolConfigFile):
        self._cfg = cfg

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        return self._cfg.tool


@serde
class FpgaConfigFile:
    name: str
    desc: str
    model: str
    model_path: str
    part: str = ""
    platform: str = ""
    tool: str = "vivado"
    xdc: list[str] = field(default_factory=list)
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    # Gate the run on timing closure. By default a routed run with
    # negative slack still PASSes (metrics carry the truth so a
    # timing-closure loop can iterate, matching `rb pnr`); set this true
    # to make an unmet-timing run FAIL — useful for regression gating.
    # No effect when the backend cannot report timing (timing_met None).
    # See docs/known-issues.md.
    require_timing_met: bool = field(rename="require-timing-met", default=False)
    # Expected-fail markers (pytest-style). Either marks this run
    # expected-to-fail; `xfail` is non-strict (an unexpected pass still
    # passes), `xfail_strict` is strict (an unexpected pass is a failure).
    # See docs/concepts/expected-failures.md.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "FpgaConfig":
        # `part:` (inline device, P1) and `platform:` (cfg-fpga-platforms
        # ref, #286) are mutually exclusive — a run naming both is
        # ambiguous, so it is a config error rather than a precedence
        # rule.
        if self.part and self.platform:
            raise FatalRtlBuddyError(
                f"fpga run '{self.name}': 'part' and 'platform' are "
                "mutually exclusive — name the device inline OR "
                "reference a cfg-fpga-platforms entry, not both"
            )
        if not self.part and not self.platform:
            raise FatalRtlBuddyError(
                f"fpga run '{self.name}': missing 'part' "
                "(full device part name, e.g. xczu7ev-ffvc1156-2-e) "
                "or 'platform' (name of a cfg-fpga-platforms entry)"
            )
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        xdc_files = [os.path.normpath(os.path.join(config_dir, p)) for p in self.xdc]
        return FpgaConfig(
            name=self.name,
            desc=self.desc,
            model=model,
            tool=self.tool,
            part=self.part,
            platform=self.platform,
            xdc_files=xdc_files,
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            require_timing_met=self.require_timing_met,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
        )


@dataclass
class FpgaConfig:
    name: str
    desc: str
    model: ModelConfig
    tool: str
    part: str
    _reglvl: int | dict | None
    tool_overrides: dict | None
    xdc_files: list[str] = dc_field(default_factory=list)
    platform: str = ""
    require_timing_met: bool = False
    xfail: bool = False
    xfail_strict: bool = False

    def get_require_timing_met(self) -> bool:
        """Whether an unmet-timing routed run should FAIL (vs PASS)."""
        return self.require_timing_met

    def is_xfail(self) -> bool:
        """Whether this run is expected to fail (either flag set)."""
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
        return self.model.name

    def get_tool_name(self) -> str:
        return self.tool

    def get_part(self) -> str:
        return self.part

    def get_platform(self) -> str:
        """Name of the cfg-fpga-platforms entry, or "" for an inline part."""
        return self.platform

    def get_xdc_files(self) -> list[str]:
        return list(self.xdc_files)

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
                    "fpga_config.reglvl_malformed",
                    fpga=self.name,
                    tool=tool_name,
                )
                raise FatalRtlBuddyError(
                    f"Malformed fpga.yaml, specify reglvl for {self.name} with {tool_name} or default"
                )

    def __str__(self):
        return pprint.pformat(self)


@serde
class FpgaSuiteConfigFile:
    filetype: Literal["fpga_config"] = field(rename="rtl-buddy-filetype")
    runs: list[FpgaConfigFile]


class FpgaSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.runs: dict[str, FpgaConfig] = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(FpgaSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "fpga_suite_config.load_failed",
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
                "fpga_suite_config.runs_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: runs section malformed") from e

    def get_runs(self, name: str | None = None) -> list[FpgaConfig]:
        if name is not None:
            if name not in self.runs:
                log_event(
                    logger,
                    logging.ERROR,
                    "fpga_suite_config.run_missing",
                    path=self.path,
                    run=name,
                )
                raise FatalRtlBuddyError(
                    f"fpga run '{name}' not found in suite {self.path}"
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
class FpgaRegConfigFile:
    filetype: Literal["fpga_reg_config"] = field(rename="rtl-buddy-filetype")
    fpga_configs: list[str] = field(rename="fpga-configs", default_factory=list)


class FpgaRegConfig:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.suite_configs: list[FpgaSuiteConfig] = []
        try:
            with open(path, "r") as f:
                data = from_yaml(FpgaRegConfigFile, f.read())
            self.suite_configs = [
                FpgaSuiteConfig(os.path.join(os.path.dirname(path), p))
                for p in data.fpga_configs
            ]
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "fpga_reg_config.load_failed",
                name=name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from e

    def get_name(self) -> str:
        return self.name

    def get_path(self) -> str:
        return self.path

    def get_suite_configs(self) -> list[FpgaSuiteConfig]:
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
