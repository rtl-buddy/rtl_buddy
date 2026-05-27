"""Configuration schema for ``rb mut`` (mutation testing) runs.

A ``mut.yaml`` names a design file to mutate, the set of mutation
operators to apply (mapped 1:1 onto ``rtl_buddy_xeno.MutationKind``),
a budget, and the FPV verification that acts as the kill oracle. The
mutation engine itself lives in the external ``rtl-buddy-xeno``
library; this schema only describes *what* to mutate and *how* to
score it.

Unlike ``fpv.yaml`` (a list of verifications), one ``mut.yaml``
describes a single mutation campaign — keeping ``rb mut list`` /
``rb mut run`` unambiguous about which design is under test.
"""

import logging
import os
import pprint
from dataclasses import dataclass, field as dc_field
from typing import Literal

from serde import field, serde
from serde.yaml import from_yaml

from .model import ModelConfig, ModelConfigLoader
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)


# The six rb-mut operators implemented in rtl-buddy-xeno. Validated here
# (rather than by importing xeno) so config loading stays light and does
# not pull in the Verible / pyslang toolchain just to parse a YAML file.
# Must stay in sync with rtl_buddy_xeno.MutationKind's rb-mut variants.
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
    per_module_cap: int | None = field(rename="per_module_cap", default=None)
    time_budget_minutes: float | None = field(
        rename="time_budget_minutes", default=None
    )
    schedule: str = "sequential"


@dataclass
class MutBudget:
    max_mutants: int
    per_module_cap: int | None
    time_budget_minutes: float | None
    schedule: str


# ---- verify (the kill oracle) ----------------------------------------------


@serde
class MutVerifyFile:
    # Path (relative to mut.yaml) to the fpv.yaml that owns the proof.
    fpv_config: str = field(rename="fpv_config")
    # Name of the verification inside that fpv.yaml to use as the oracle.
    verification: str


# ---- scope (optional; no-op for single-file leaf blocks) -------------------


@serde
class MutScopeFile:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


# ---- campaign config (one mut.yaml) ----------------------------------------


@serde
class MutConfigFile:
    filetype: Literal["mut_config"] = field(rename="rtl-buddy-filetype")
    model: str
    model_path: str = field(rename="model_path")
    # SystemVerilog file to mutate, relative to mut.yaml. Must be one of
    # the model's source files and must live within the model directory
    # (the directory containing models.yaml) so per-mutant isolation can
    # copy the tree and splice the mutant in.
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

        model = ModelConfigLoader(os.path.join(config_dir, self.model_path)).get_model(
            self.model
        )
        design_file = os.path.normpath(os.path.join(config_dir, self.design_file))
        fpv_config = os.path.normpath(os.path.join(config_dir, self.verify.fpv_config))

        return MutConfig(
            name=self.name or self.model,
            model=model,
            top=self.top or self.model,
            design_file=design_file,
            operators=list(self.operators),
            fpv_config=fpv_config,
            verification=self.verify.verification,
            budget=MutBudget(
                max_mutants=self.budget.max_mutants,
                per_module_cap=self.budget.per_module_cap,
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
    fpv_config: str
    verification: str
    budget: MutBudget
    scope_include: list[str] = dc_field(default_factory=list)
    scope_exclude: list[str] = dc_field(default_factory=list)

    def get_name(self) -> str:
        return self.name

    def get_model(self) -> ModelConfig:
        return self.model

    def get_design_file(self) -> str:
        return self.design_file

    def get_operators(self) -> list[str]:
        return self.operators

    def __str__(self):
        return pprint.pformat(self)


class MutSuiteConfig:
    """Loads a single ``mut.yaml`` into a resolved :class:`MutConfig`."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, "r") as f:
                data = from_yaml(MutConfigFile, f.read())
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
