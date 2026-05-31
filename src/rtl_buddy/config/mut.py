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
    # Caps mutants PER SCOPED FILE: in scoped mode each source file is
    # treated as one unit, so this bounds how many mutants any single
    # scoped file contributes; for the single-file default it caps that
    # one file.
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
    # FPV oracle: a verification inside an fpv.yaml. A mutant is killed
    # when the proof flips from PASS to FAIL.
    fpv_config: str | None = field(rename="fpv_config", default=None)
    verification: str | None = None
    # Simulation oracle: a tests.yaml run with SVA assertions compiled in.
    # A mutant is killed when a test FAILs or an assertion fires.
    test_config: str | None = field(rename="test_config", default=None)
    # Optional subset of test names to run; empty = every test in the suite.
    tests: list[str] = field(default_factory=list)
    # Compile SVA in (Verilator --assert). Defaults on — the sim oracle is
    # far weaker without assertions firing.
    assertions: bool = True


# ---- scope (optional; no-op for single-file leaf blocks) -------------------


@serde
class MutScopeFile:
    """Scope selector for a hierarchical mutation campaign.

    Patterns are matched (case-sensitively, shell-glob via ``fnmatch`` —
    so no ``**`` recursion) against BOTH a node's instance path
    (e.g. ``top.u_alu``) AND its source file (matched in both absolute and
    model-relative forms). An empty ``include`` means every in-scope node
    is selected; any ``exclude`` match drops a node. An empty resulting
    selection is a fatal error.
    """

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


# ---- campaign config (one mut.yaml) ----------------------------------------


@serde
class MutConfigFile:
    filetype: Literal["mut_config"] = field(rename="rtl-buddy-filetype")
    model: str
    model_path: str = field(rename="model_path")
    # SystemVerilog file, relative to mut.yaml. Must be one of the model's
    # source files and must live within the model directory (the directory
    # containing models.yaml) so per-mutant isolation can copy the tree and
    # splice the mutant in. It is the baseline-oracle target in BOTH modes;
    # when a scope block is set it is NOT itself the mutation target (the
    # scoped file-set is) — it only anchors the model dir / oracle baseline.
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

        # At least one kill oracle must be configured.
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
            top=self.top or self.model,
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
