"""Configuration schema for ``rb mut`` (mutation testing) runs.

One ``mut.yaml`` describes a single mutation campaign: a design file, the operators to apply, a budget, and the kill oracle (FPV, simulation, or both).
The mutation engine is the external ``rtl-buddy-xeno`` library.
"""

import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field
from typing import Literal

from serde import field, serde
from .yaml_loader import config_from_yaml

from .model import ModelConfig, ModelConfigLoader, validate_top
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# Validated here, not by importing xeno, to keep config loading light.
# Keep in sync with the rb-mut variants of rtl_buddy_xeno.MutationKind.
_VALID_OPERATORS = (
    "arith_flip",
    "bit_op_flip",
    "cond_negate",
    "cond_const",
    "assign_drop",
    "port_binding_swap",
)

_VALID_SCHEDULES = ("sequential", "round_robin")


# ---- budget ----------------------------------------------------------------


@serde
class MutBudgetFile:
    max_mutants: int = field(rename="max_mutants", default=100)
    # Caps mutants per file: each scoped file counts separately, or the one design file when unscoped.
    per_file_cap: int | None = field(rename="per_file_cap", default=None)
    time_budget_minutes: float | None = field(
        rename="time_budget_minutes", default=None
    )
    schedule: str = "sequential"


@dataclass
class MutBudget:
    max_mutants: int
    per_file_cap: int | None
    time_budget_minutes: float | None
    schedule: str


# ---- verify (the kill oracle) ----------------------------------------------


@serde
class MutVerifyFile:
    # FPV oracle: a verification in an fpv.yaml. A mutant is killed when the proof flips from PASS to FAIL.
    fpv_config: str | None = field(rename="fpv_config", default=None)
    verification: str | None = None
    # Simulation oracle: a tests.yaml run. A mutant is killed when a test FAILs or an assertion fires.
    test_config: str | None = field(rename="test_config", default=None)
    # Empty means every test in the suite.
    tests: list[str] = field(default_factory=list)
    # Compiles SVA in (Verilator --assert).
    assertions: bool = True


# ---- scope (optional) ------------------------------------------------------


@serde
class MutScopeFile:
    """Scope selector for a hierarchical mutation campaign.

    Patterns are case-sensitive ``fnmatch`` globs matched against a node's instance path (e.g. ``top.u_alu``) and its source file, absolute or model-relative.
    An empty ``include`` selects every node; any ``exclude`` match drops one. An empty selection is fatal.
    """

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


# ---- campaign config (one mut.yaml) ----------------------------------------


@serde
class MutConfigFile:
    filetype: Literal["mut_config"] = field(rename="rtl-buddy-filetype")
    model: str
    model_path: str = field(rename="model_path")
    # Relative to mut.yaml; must be a model source file inside the models.yaml directory.
    # With a scope block it only anchors the model dir and oracle baseline; the scoped files are mutated.
    design_file: str = field(rename="design_file")
    operators: list[str]
    verify: MutVerifyFile
    name: str | None = None
    top: str | None = None
    budget: MutBudgetFile = field(default_factory=MutBudgetFile)
    scope: MutScopeFile = field(default_factory=MutScopeFile)

    def initialise(self, config_dir: str) -> "MutConfig":
        if not self.operators:
            raise FatalRtlBuddyError("mut.yaml: operators list is empty")
        for op in self.operators:
            if op not in _VALID_OPERATORS:
                raise FatalRtlBuddyError(
                    f"mut.yaml: operator '{op}' is not one of "
                    f"{', '.join(_VALID_OPERATORS)}"
                )
        if self.budget.schedule not in _VALID_SCHEDULES:
            raise FatalRtlBuddyError(
                f"mut.yaml: schedule '{self.budget.schedule}' is not one of "
                f"{', '.join(_VALID_SCHEDULES)}"
            )

        has_fpv = bool(self.verify.fpv_config)
        has_sim = bool(self.verify.test_config)
        if not has_fpv and not has_sim:
            raise FatalRtlBuddyError(
                "mut.yaml: verify must configure at least one kill oracle "
                "(fpv_config + verification, and/or test_config)"
            )
        if has_fpv and not self.verify.verification:
            raise FatalRtlBuddyError(
                "mut.yaml: verify.fpv_config requires verify.verification "
                "(the verification name to use as the oracle)"
            )

        if self.top is not None and not has_fpv:
            # Only the FPV oracle elaborates a top.
            log_event(
                logger,
                logging.WARNING,
                "mut_config.top_override_unused",
                name=self.name or self.model,
                top=self.top,
            )

        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        design_file = os.path.normpath(os.path.join(config_dir, self.design_file))
        fpv_config = (
            os.path.normpath(os.path.join(config_dir, self.verify.fpv_config))
            if self.verify.fpv_config
            else None
        )
        test_config = (
            os.path.normpath(os.path.join(config_dir, self.verify.test_config))
            if self.verify.test_config
            else None
        )

        return MutConfig(
            name=self.name or self.model,
            model=model,
            top=self.top or model.get_top(),
            top_override=self.top,
            design_file=design_file,
            operators=list(self.operators),
            fpv_config=fpv_config,
            verification=self.verify.verification,
            test_config=test_config,
            tests=list(self.verify.tests),
            assertions=self.verify.assertions,
            budget=MutBudget(
                max_mutants=self.budget.max_mutants,
                per_file_cap=self.budget.per_file_cap,
                time_budget_minutes=self.budget.time_budget_minutes,
                schedule=self.budget.schedule,
            ),
            scope_include=list(self.scope.include),
            scope_exclude=list(self.scope.exclude),
        )


@dataclass
class MutConfig:
    name: str
    model: ModelConfig
    top: str
    design_file: str
    operators: list[str]
    budget: MutBudget
    # `top` above is the effective value; this is the explicit `top:` (None if unset), so the runner can tell an override from the model default.
    top_override: str | None = None
    # FPV oracle (optional)
    fpv_config: str | None = None
    verification: str | None = None
    # Sim oracle (optional)
    test_config: str | None = None
    tests: list[str] = dc_field(default_factory=list)
    assertions: bool = True
    scope_include: list[str] = dc_field(default_factory=list)
    scope_exclude: list[str] = dc_field(default_factory=list)

    def get_name(self) -> str:
        return self.name

    def get_model(self) -> ModelConfig:
        return self.model

    def get_top(self) -> str:
        return self.top

    def get_top_override(self) -> str | None:
        return self.top_override

    def get_design_file(self) -> str:
        return self.design_file

    def get_operators(self) -> list[str]:
        return self.operators

    def has_fpv_oracle(self) -> bool:
        return self.fpv_config is not None

    def has_sim_oracle(self) -> bool:
        return self.test_config is not None

    def get_scope_include(self) -> list[str]:
        return self.scope_include

    def get_scope_exclude(self) -> list[str]:
        return self.scope_exclude

    def has_scope(self) -> bool:
        return bool(self.scope_include or self.scope_exclude)

    def __str__(self):
        return pprint.pformat(self)


class MutSuiteConfig:
    """Loads one ``mut.yaml`` into a :class:`MutConfig`."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, "r") as f:
                data = config_from_yaml(MutConfigFile, f.read(), path)
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "mut_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        config_dir = os.path.dirname(os.path.abspath(path))
        # The campaign `top:` reaches generated yosys/sby scripts, so it gets the model top's validation.
        if data.top is not None:
            validate_top(
                data.top,
                data.name or data.model,
                path,
                subject="campaign",
                event="mut_config.invalid_top",
            )
        try:
            self.config = data.initialise(config_dir)
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "mut_suite_config.malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: mut config malformed") from e

    def get_config(self) -> MutConfig:
        return self.config

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self.config)
