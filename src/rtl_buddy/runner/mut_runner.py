"""Mutation-campaign runner for ``rb mut``.

Orchestrates the external ``rtl-buddy-xeno`` mutation engine against the
existing ``rb fpv`` proof harness:

1. enumerate / generate mutants of a single design file (xeno),
2. materialise each mutant into an isolated copy of the model source
   tree (the original design file is never touched),
3. re-run the named FPV verification against the mutated tree,
4. score ``killed`` (verdict flipped vs the unmutated baseline) vs
   ``survived`` vs ``errored`` (mutant broke elaboration).

xeno is an optional dependency — it pulls in the Verible / pyslang
toolchain via its ``[verible]`` / ``[slang]`` extras — so it is
imported lazily here. ``rb mut`` is the only entry point that needs it;
the rest of rtl_buddy (and its test suite) runs without it installed.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import time
from pathlib import Path

from ..config.fpv import FpvSuiteConfig
from ..config.mut import MutConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .fpv_runner import FpvRunner
from .mut_results import ERRORED, KILLED, SURVIVED, MutantOutcome, MutResults

logger = logging.getLogger(__name__)


_XENO_INSTALL_HINT = (
    "rb mut requires the rtl-buddy-xeno mutation engine, which is not "
    "installed. Install it with:\n"
    '    pip install "rtl-buddy-xeno[verible,slang]"\n'
    "(the [verible] and [slang] extras pull the Verible CST + pyslang "
    "toolchain the structural operators need)."
)


class MutRunner:
    def __init__(self, name: str, root_cfg, mut_cfg: MutConfig, work_dir: str):
        self.name = name
        self.root_cfg = root_cfg
        self.mut_cfg = mut_cfg
        self.work_dir = work_dir

    # --- xeno bridge --------------------------------------------------------

    @staticmethod
    def _load_xeno():
        try:
            import rtl_buddy_xeno
        except ImportError as e:
            raise FatalRtlBuddyError(_XENO_INSTALL_HINT) from e
        return rtl_buddy_xeno

    def _kinds(self, xeno):
        """Map the config's operator strings onto xeno MutationKinds."""
        try:
            return [xeno.MutationKind(op) for op in self.mut_cfg.get_operators()]
        except ValueError as e:
            # Config already validates against _VALID_OPERATORS, so this
            # only fires if xeno's enum drifts from our local list.
            raise FatalRtlBuddyError(
                f"rb mut: operator not recognised by installed rtl-buddy-xeno: {e}"
            ) from e

    def _mutator(self, xeno):
        design_file = self.mut_cfg.get_design_file()
        if not os.path.isfile(design_file):
            raise FatalRtlBuddyError(f"rb mut: design_file not found: {design_file}")
        return xeno.Mutator.from_sv(Path(design_file))

    def _effective_count(self) -> int:
        budget = self.mut_cfg.budget
        count = budget.max_mutants
        if budget.per_module_cap is not None:
            # Single design file == single module for this slice, so the
            # per-module cap is just a tighter ceiling on the total.
            count = min(count, budget.per_module_cap)
        return count

    def _schedule(self, xeno):
        if self.mut_cfg.budget.schedule == "round_robin":
            return xeno.Schedule.ROUND_ROBIN
        return xeno.Schedule.SEQUENTIAL

    # --- list ---------------------------------------------------------------

    def list_candidates(self) -> list[dict]:
        """Enumerate candidate sites without mutating (``rb mut list``)."""
        xeno = self._load_xeno()
        mutator = self._mutator(xeno)
        sites = []
        for site in mutator.candidates(kinds=self._kinds(xeno)):
            sites.append(
                {
                    "operator": site.kind.value,
                    "line": site.line,
                    "column": site.column,
                    "snippet": site.snippet,
                }
            )
        return sites

    # --- run ----------------------------------------------------------------

    def run(self) -> MutResults:
        xeno = self._load_xeno()
        mutator = self._mutator(xeno)
        kinds = self._kinds(xeno)

        baseline_fpv_cfg = self._load_oracle_cfg()
        self._validate_design_in_model()

        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        baseline_verdict = self._run_baseline(baseline_fpv_cfg)
        if baseline_verdict != "PASS":
            log_event(
                logger,
                logging.WARNING,
                "mut_runner.baseline_not_pass",
                campaign=self.mut_cfg.get_name(),
                verdict=baseline_verdict,
            )

        outcomes: list[MutantOutcome] = []
        deadline = self._deadline()
        for idx, mutant in enumerate(
            mutator.generate(
                kinds=kinds,
                count=self._effective_count(),
                seed=0,
                schedule=self._schedule(xeno),
            )
        ):
            if deadline is not None and time.monotonic() > deadline:
                log_event(
                    logger,
                    logging.INFO,
                    "mut_runner.time_budget_reached",
                    campaign=self.mut_cfg.get_name(),
                    generated=idx,
                )
                break
            outcomes.append(
                self._score_mutant(idx, mutant, baseline_fpv_cfg, baseline_verdict)
            )

        return MutResults(
            name=self.mut_cfg.get_name(),
            outcomes=outcomes,
            baseline_verdict=baseline_verdict,
        )

    # --- internals ----------------------------------------------------------

    def _load_oracle_cfg(self):
        suite = FpvSuiteConfig(path=self.mut_cfg.fpv_config)
        # Raises FatalRtlBuddyError if the named verification is absent.
        return suite.get_verifications(self.mut_cfg.verification)[0]

    def _model_dir(self) -> str:
        model = self.mut_cfg.get_model()
        if not model.path:
            raise FatalRtlBuddyError(
                "rb mut: model has no resolved path; cannot isolate mutants"
            )
        return os.path.dirname(os.path.abspath(model.path))

    def _design_relpath(self) -> str:
        return os.path.relpath(self.mut_cfg.get_design_file(), self._model_dir())

    def _validate_design_in_model(self) -> None:
        rel = self._design_relpath()
        if rel.startswith(".."):
            raise FatalRtlBuddyError(
                f"rb mut: design_file ({self.mut_cfg.get_design_file()}) must live "
                f"within the model directory ({self._model_dir()}) so per-mutant "
                "isolation can copy the source tree."
            )

    def _run_baseline(self, baseline_fpv_cfg) -> str:
        suite_dir = os.path.join(self.work_dir, "baseline")
        Path(suite_dir).mkdir(parents=True, exist_ok=True)
        results = FpvRunner(
            name=self.name + "/baseline",
            root_cfg=self.root_cfg,
            fpv_cfg=baseline_fpv_cfg,
            suite_dir=suite_dir,
        ).run()
        return results.results.get("result", "NA")

    def _materialise_mutant(self, mutant_id: str, mutant_sv: str):
        """Copy the model tree, splice in the mutant, return a per-mutant
        ModelConfig pointing at the copy."""
        mutant_root = os.path.join(self.work_dir, mutant_id)
        model_src = os.path.join(mutant_root, "model_src")
        if os.path.exists(model_src):
            shutil.rmtree(model_src)
        shutil.copytree(self._model_dir(), model_src)

        spliced = os.path.join(model_src, self._design_relpath())
        with open(spliced, "w") as f:
            f.write(mutant_sv)

        orig_model = self.mut_cfg.get_model()
        copied_models_yaml = os.path.join(
            model_src, os.path.basename(os.path.abspath(orig_model.path))
        )
        return dataclasses.replace(orig_model, path=copied_models_yaml), mutant_root

    def _score_mutant(
        self, idx: int, mutant, baseline_fpv_cfg, baseline_verdict: str
    ) -> MutantOutcome:
        operator = mutant.kind.value
        mutant_id = f"m{idx:04d}_{operator}"
        predicted = sorted(getattr(mutant.prediction, "perturbs_signals", []) or [])

        try:
            mutant_model, mutant_root = self._materialise_mutant(mutant_id, mutant.sv)
            mutant_fpv_cfg = dataclasses.replace(
                baseline_fpv_cfg,
                model=mutant_model,
                name=f"{baseline_fpv_cfg.get_name()}__{mutant_id}",
            )
            results = FpvRunner(
                name=self.name + "/" + mutant_id,
                root_cfg=self.root_cfg,
                fpv_cfg=mutant_fpv_cfg,
                suite_dir=mutant_root,
            ).run()
            verdict = results.results.get("result", "NA")
        except FatalRtlBuddyError as e:
            # A mutant that breaks elaboration / the build is errored, not
            # scored — dropped from the denominator.
            log_event(
                logger,
                logging.INFO,
                "mut_runner.mutant_errored",
                campaign=self.mut_cfg.get_name(),
                mutant=mutant_id,
                error=str(e),
            )
            return MutantOutcome(
                mutant_id=mutant_id,
                operator=operator,
                outcome=ERRORED,
                diff_summary=mutant.diff_summary,
                verdict="ERROR",
                predicted_signals=predicted,
            )

        if verdict in ("NA", "ERROR"):
            outcome = ERRORED
        elif verdict != baseline_verdict:
            outcome = KILLED
        else:
            outcome = SURVIVED

        log_event(
            logger,
            logging.DEBUG,
            "mut_runner.mutant_scored",
            campaign=self.mut_cfg.get_name(),
            mutant=mutant_id,
            operator=operator,
            verdict=verdict,
            outcome=outcome,
        )
        return MutantOutcome(
            mutant_id=mutant_id,
            operator=operator,
            outcome=outcome,
            diff_summary=mutant.diff_summary,
            verdict=verdict,
            predicted_signals=predicted,
        )

    def _deadline(self) -> float | None:
        mins = self.mut_cfg.budget.time_budget_minutes
        if mins is None:
            return None
        return time.monotonic() + mins * 60.0
