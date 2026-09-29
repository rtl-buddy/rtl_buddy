"""Configuration schema for style-lint (verible) runs.

Each ``lint.yaml`` lists checks: a model whose filelist supplies the files to lint, plus optional exclude globs and extra lint arguments.
The linter is always the project's ``cfg-verible`` entry.
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


# ---- per-check config ------------------------------------------------------


@serde
class LintConfigFile:
    name: str
    desc: str
    model: str
    model_path: str = field(rename="model_path")
    # fnmatch globs against the project-root-relative path, added to the cfg-verible ``exclude`` list; ``*`` crosses ``/``.
    exclude: list[str] = field(default_factory=list)
    # Appended after cfg-verible ``extra_args.lint``; later gflags win.
    extra_args: list[str] = field(default_factory=list)
    reglvl: int | None = None
    # Either flag turns a FAIL into XFAIL; an unexpected pass (XPASS) fails only for `xfail_strict`.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "LintConfig":
        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        return LintConfig(
            name=self.name,
            desc=self.desc,
            model=model,
            exclude=list(self.exclude),
            extra_args=list(self.extra_args),
            _reglvl=self.reglvl,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
        )


@dataclass
class LintConfig:
    name: str
    desc: str
    model: ModelConfig
    exclude: list[str] = dc_field(default_factory=list)
    extra_args: list[str] = dc_field(default_factory=list)
    _reglvl: int | None = None
    xfail: bool = False
    xfail_strict: bool = False

    def get_name(self) -> str:
        return self.name

    def get_desc(self) -> str:
        return self.desc

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        """The module this run elaborates (see :meth:`ModelConfig.get_top`)."""
        return self.model.get_top()

    def get_tool_name(self) -> str:
        return "verible"

    def get_exclude(self) -> list[str]:
        return list(self.exclude)

    def get_extra_args(self) -> list[str]:
        return list(self.extra_args)

    def is_xfail(self) -> bool:
        """Whether this check is expected to fail (either flag set)."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_reglvl(self) -> int:
        return self._reglvl if self._reglvl is not None else 0

    def __str__(self):
        return pprint.pformat(self)


# ---- suite (a single lint.yaml) --------------------------------------------


@serde
class LintSuiteConfigFile:
    filetype: Literal["lint_config"] = field(rename="rtl-buddy-filetype")
    checks: list[LintConfigFile]


class LintSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.checks: dict[str, LintConfig] = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(LintSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "lint_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        # The dict build below would silently drop the first of two same-named checks.
        seen: dict[str, int] = {}
        for idx, check in enumerate(data.checks):
            if check.name in seen:
                log_event(
                    logger,
                    logging.ERROR,
                    "lint_suite_config.duplicate_check",
                    path=path,
                    name=check.name,
                    first_index=seen[check.name],
                    second_index=idx,
                )
                raise FatalRtlBuddyError(f"{path}: duplicate check name {check.name!r}")
            seen[check.name] = idx

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.checks = {c.name: c.initialise(config_dir) for c in data.checks}
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "lint_suite_config.checks_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: checks section malformed") from e

    def get_checks(self, name: str | None = None) -> list[LintConfig]:
        if name is not None:
            if name not in self.checks:
                log_event(
                    logger,
                    logging.ERROR,
                    "lint_suite_config.check_missing",
                    path=self.path,
                    check=name,
                )
                raise FatalRtlBuddyError(
                    f"lint check '{name}' not found in suite {self.path}"
                )
            return [self.checks[name]]
        return list(self.checks.values())

    def get_check_names(self) -> list[str]:
        return list(self.checks.keys())

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self)


# ---- regression (a list of lint.yaml suites) -------------------------------


@serde
class LintRegConfigFile:
    filetype: Literal["lint_reg_config"] = field(rename="rtl-buddy-filetype")
    lint_configs: list[str] = field(rename="lint-configs", default_factory=list)


class LintRegConfig:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self.suite_configs: list[LintSuiteConfig] = []
        try:
            with open(path, "r") as f:
                data = from_yaml(LintRegConfigFile, f.read())
            self.suite_configs = [
                LintSuiteConfig(os.path.join(os.path.dirname(path), p))
                for p in data.lint_configs
            ]
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "lint_reg_config.load_failed",
                name=name,
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'{name}: failed to load "{path}"') from e

    def get_name(self) -> str:
        return self.name

    def get_path(self) -> str:
        return self.path

    def get_suite_configs(self) -> list[LintSuiteConfig]:
        return self.suite_configs

    def __str__(self):
        return pprint.pformat(self)
