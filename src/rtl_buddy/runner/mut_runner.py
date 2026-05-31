"""Mutation-campaign runner for ``rb mut``.

Orchestrates the external ``rtl-buddy-xeno`` mutation engine against one
or more kill oracles:

1. enumerate / generate mutants of a single design file (xeno),
2. materialise each mutant into an isolated copy of the model source
   tree (the original design file is never touched),
3. re-evaluate each configured oracle against the mutated tree:
   - **FPV** — re-prove a named ``fpv.yaml`` verification (killed when
     the verdict flips vs the unmutated baseline),
   - **sim** — re-run a ``tests.yaml`` suite with SVA assertions
     compiled in (killed when a test FAILs or an assertion fires),
4. score ``killed`` (any oracle caught it) / ``survived`` (every oracle
   passed) / ``errored`` (the mutant broke the build under every oracle,
   so it can't be scored — dropped from the denominator).

xeno is an optional dependency — it pulls in the Verible / pyslang
toolchain via its ``[verible]`` / ``[slang]`` extras — so it is
imported lazily here. ``rb mut`` is the only entry point that needs it;
the rest of rtl_buddy (and its test suite) runs without it installed.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
import os
import shutil
import time
from pathlib import Path

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
    def __init__(
        self,
        name: str,
        root_cfg,
        mut_cfg: MutConfig,
        work_dir: str,
        rtl_builder_mode: str = "debug",
    ):
        self.name = name
        self.root_cfg = root_cfg
        self.mut_cfg = mut_cfg
        self.work_dir = work_dir
        # Builder mode handed to TestRunner for the sim oracle; unused by
        # the FPV oracle. "debug" matches the rb-test default.
        self.rtl_builder_mode = rtl_builder_mode

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
        if budget.per_file_cap is not None:
            # Single design file == single scoped file for this slice, so the
            # per-file cap is just a tighter ceiling on the total.
            count = min(count, budget.per_file_cap)
        return count

    def _schedule(self, xeno):
        if self.mut_cfg.budget.schedule == "round_robin":
            return xeno.Schedule.ROUND_ROBIN
        return xeno.Schedule.SEQUENTIAL

    # --- scope graph ingestion ----------------------------------------------

    def _scope_graph_json(self) -> dict:
        """Run rtl-buddy-view (via the existing RtlBuddyView wrapper) to a
        JSON file under the work dir and load it.

        Only called when ``self.mut_cfg.has_scope()``. ``RtlBuddyView.run()``
        streams JSON to the ``--output`` file and returns a returncode (it
        does NOT return stdout), so we read the file back. The wrapper raises
        ``FatalRtlBuddyError`` if the binary is missing — let that propagate.
        """
        from ..tools.hier_rtl_buddy_view import RtlBuddyView

        out = os.path.join(self.work_dir, "scope", "hier.json")
        Path(os.path.dirname(out)).mkdir(parents=True, exist_ok=True)
        rc = RtlBuddyView(
            name=self.name + "/mut-scope",
            model_cfg=self.mut_cfg.get_model(),
            suite_dir=self.work_dir,
            format="json",
            output=out,
        ).run()
        if rc != 0 or not os.path.isfile(out):
            log_event(
                logger,
                logging.ERROR,
                "mut_runner.scope_graph_failed",
                campaign=self.mut_cfg.get_name(),
                model=self.mut_cfg.get_model().name,
                rc=rc,
                output=out,
            )
            raise FatalRtlBuddyError(
                "rb mut: scope graph-ingestion needs the rtl-buddy-view binary "
                "on PATH; install or build it per its README, then re-run. "
                f"(rtl-buddy-view exited rc={rc}; run `rb hier "
                f"{self.mut_cfg.get_model().name} --format json` to diagnose.) "
                "Removing the scope block from mut.yaml runs rb mut in "
                "single-file mode, which does not require rtl-buddy-view."
            )
        with open(out) as f:
            data = json.load(f)
        major = str(data.get("schema_version", "")).split(".")[0]
        if major != "1":
            raise FatalRtlBuddyError(
                "rb mut: unexpected hier schema_version "
                f"{data.get('schema_version')!r} (expected 1.x)"
            )
        return data

    def _scoped_source_files(self) -> list[str]:
        """Resolve scope.include/exclude against the hier graph.

        Glob-matches each include/exclude pattern (stdlib shell glob via
        ``fnmatch``, case-sensitive on every platform) against THREE targets
        per node: the dotted instance
        path (``node["id"]``), the node's absolute source file, and that
        file relative to the model dir. A node is in scope when include is
        empty OR any include matches, and no exclude matches.

        Returns the sorted, de-duplicated set of in-scope source files
        (absolute paths). Raises ``FatalRtlBuddyError`` when the resolved
        set is empty or any file escapes the model dir.
        """
        inc = self.mut_cfg.get_scope_include()
        exc = self.mut_cfg.get_scope_exclude()
        data = self._scope_graph_json()
        model_dir = self._model_dir()

        kept: set[str] = set()
        for node in data.get("nodes", []):
            src = (node.get("source") or {}).get("file")
            if not src:
                continue
            abspath = os.path.normpath(os.path.abspath(src))
            rel = os.path.relpath(abspath, model_dir)
            targets = (node.get("id", ""), abspath, rel)
            # fnmatchcase: case-sensitive on all platforms (fnmatch would
            # case-fold on macOS), so a scope selects the same files in dev
            # and CI.
            included = (not inc) or any(
                fnmatch.fnmatchcase(t, p) for p in inc for t in targets
            )
            excluded = any(fnmatch.fnmatchcase(t, p) for p in exc for t in targets)
            if included and not excluded:
                kept.add(abspath)

        if not kept:
            raise FatalRtlBuddyError(
                "rb mut: scope.include/exclude selected no source files from "
                f"the hierarchy of model '{self.mut_cfg.get_model().name}' "
                f"(include={inc!r}, exclude={exc!r})"
            )
        for f in kept:
            rel = os.path.relpath(f, model_dir)
            if rel.startswith(".."):
                raise FatalRtlBuddyError(
                    f"rb mut: scoped source file ({f}) must live within the "
                    f"model directory ({model_dir}) so per-mutant isolation "
                    "can copy the source tree."
                )
        files = sorted(kept)
        log_event(
            logger,
            logging.INFO,
            "mut_runner.scope_resolved",
            campaign=self.mut_cfg.get_name(),
            files=len(files),
        )
        return files

    # --- list ---------------------------------------------------------------

    def list_candidates(self) -> list[dict]:
        """Enumerate candidate sites without mutating (``rb mut list``)."""
        xeno = self._load_xeno()
        if self.mut_cfg.has_scope():
            return self._list_candidates_scoped(xeno)
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

    def _list_candidates_scoped(self, xeno) -> list[dict]:
        """Enumerate candidate sites across every scoped source file.

        Each candidate carries a model-relative ``file`` key so multi-file
        output disambiguates which scoped file the site belongs to. The
        per-file ``per_file_cap`` (one scoped file == one unit for this
        slice) caps how many sites are reported per file.
        """
        kinds = self._kinds(xeno)
        model_dir = self._model_dir()
        cap = self.mut_cfg.budget.per_file_cap
        sites: list[dict] = []
        for source_file in self._scoped_source_files():
            mutator = xeno.Mutator.from_sv(Path(source_file))
            rel = os.path.relpath(source_file, model_dir)
            n = 0
            for site in mutator.candidates(kinds=kinds):
                if cap is not None and n >= cap:
                    break
                sites.append(
                    {
                        "operator": site.kind.value,
                        "line": site.line,
                        "column": site.column,
                        "snippet": site.snippet,
                        "file": rel,
                    }
                )
                n += 1
        return sites

    # --- run ----------------------------------------------------------------

    def run(self) -> MutResults:
        xeno = self._load_xeno()
        if self.mut_cfg.has_scope():
            return self._run_scoped(xeno)
        mutator = self._mutator(xeno)
        kinds = self._kinds(xeno)

        self._validate_design_in_model()
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        # Load + baseline each configured oracle. Baselines are expected to
        # PASS on the unmutated design; a non-passing baseline means the
        # oracle is broken (warn, but keep going — every mutant will then
        # look "killed" and the user can see why).
        fpv_cfg = self._load_fpv_cfg() if self.mut_cfg.has_fpv_oracle() else None
        fpv_baseline = self._baseline_fpv(fpv_cfg) if fpv_cfg is not None else None
        sim_baseline = self._baseline_sim() if self.mut_cfg.has_sim_oracle() else None
        for label, verdict in (("fpv", fpv_baseline), ("sim", sim_baseline)):
            if verdict is not None and verdict != "PASS":
                log_event(
                    logger,
                    logging.WARNING,
                    "mut_runner.baseline_not_pass",
                    campaign=self.mut_cfg.get_name(),
                    oracle=label,
                    verdict=verdict,
                )
        baseline_verdict = " ".join(
            f"{label}={v}"
            for label, v in (("fpv", fpv_baseline), ("sim", sim_baseline))
            if v is not None
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
            outcomes.append(self._score_mutant(idx, mutant, fpv_cfg, fpv_baseline))

        return MutResults(
            name=self.mut_cfg.get_name(),
            outcomes=outcomes,
            baseline_verdict=baseline_verdict or "NA",
        )

    def _run_scoped(self, xeno) -> MutResults:
        """Multi-file campaign: resolve the scoped source files from the
        hier graph, then mutate each one in turn, splicing every mutant back
        into ITS origin file.

        Budget semantics for the scoped slice (one scoped file == one unit):
          - ``per_file_cap`` caps mutants generated PER scoped file;
          - ``max_mutants`` is a GLOBAL ceiling across all scoped files —
            once it is reached the campaign stops, even mid-file;
          - the time budget applies across the whole campaign;
          - the schedule is applied independently per file.

        Scoped files are processed in sorted order, so the global
        ``max_mutants`` ceiling may truncate later files; users control
        fairness via scope ordering / ``per_file_cap``.
        """
        kinds = self._kinds(xeno)
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        # Baseline each configured oracle on the unmutated design (same
        # semantics as the single-file path).
        fpv_cfg = self._load_fpv_cfg() if self.mut_cfg.has_fpv_oracle() else None
        fpv_baseline = self._baseline_fpv(fpv_cfg) if fpv_cfg is not None else None
        sim_baseline = self._baseline_sim() if self.mut_cfg.has_sim_oracle() else None
        for label, verdict in (("fpv", fpv_baseline), ("sim", sim_baseline)):
            if verdict is not None and verdict != "PASS":
                log_event(
                    logger,
                    logging.WARNING,
                    "mut_runner.baseline_not_pass",
                    campaign=self.mut_cfg.get_name(),
                    oracle=label,
                    verdict=verdict,
                )
        baseline_verdict = " ".join(
            f"{label}={v}"
            for label, v in (("fpv", fpv_baseline), ("sim", sim_baseline))
            if v is not None
        )

        source_files = self._scoped_source_files()
        model_dir = self._model_dir()
        per_file_cap = self.mut_cfg.budget.per_file_cap
        global_cap = self.mut_cfg.budget.max_mutants

        outcomes: list[MutantOutcome] = []
        per_file: dict[str, dict[str, int]] = {}
        deadline = self._deadline()
        idx = 0
        stop = False
        for source_file in source_files:
            if stop:
                break
            rel = os.path.relpath(source_file, model_dir)
            count = global_cap - len(outcomes)
            if per_file_cap is not None:
                count = min(count, per_file_cap)
            if count <= 0:
                break
            mutator = xeno.Mutator.from_sv(Path(source_file))
            for mutant in mutator.generate(
                kinds=kinds,
                count=count,
                seed=0,
                schedule=self._schedule(xeno),
            ):
                if deadline is not None and time.monotonic() > deadline:
                    log_event(
                        logger,
                        logging.INFO,
                        "mut_runner.time_budget_reached",
                        campaign=self.mut_cfg.get_name(),
                        generated=idx,
                    )
                    stop = True
                    break
                outcome = self._score_mutant(
                    idx, mutant, fpv_cfg, fpv_baseline, target_file=source_file
                )
                outcomes.append(outcome)
                bucket = per_file.setdefault(rel, {KILLED: 0, SURVIVED: 0, ERRORED: 0})
                bucket[outcome.outcome] += 1
                idx += 1
                if len(outcomes) >= global_cap:
                    stop = True
                    break

        return MutResults(
            name=self.mut_cfg.get_name(),
            outcomes=outcomes,
            baseline_verdict=baseline_verdict or "NA",
            per_file=per_file,
        )

    # --- mutant materialisation ---------------------------------------------

    def _model_dir(self) -> str:
        model = self.mut_cfg.get_model()
        if not model.path:
            raise FatalRtlBuddyError(
                "rb mut: model has no resolved path; cannot isolate mutants"
            )
        return os.path.dirname(os.path.abspath(model.path))

    def _design_relpath(self, target_file: str | None = None) -> str:
        target = target_file or self.mut_cfg.get_design_file()
        return os.path.relpath(target, self._model_dir())

    def _validate_design_in_model(self) -> None:
        rel = self._design_relpath()
        if rel.startswith(".."):
            raise FatalRtlBuddyError(
                f"rb mut: design_file ({self.mut_cfg.get_design_file()}) must live "
                f"within the model directory ({self._model_dir()}) so per-mutant "
                "isolation can copy the source tree."
            )

    def _materialise_mutant(
        self, mutant_id: str, mutant_sv: str, target_file: str | None = None
    ):
        """Copy the model tree, splice in the mutant, return a per-mutant
        ModelConfig pointing at the copy plus the mutant's work root.

        ``target_file`` names the source file the mutant should be spliced
        into (its origin file in a multi-file scoped campaign). When None
        (the single-file / empty-scope path), it is the configured
        ``design_file`` — keeping the no-scope behaviour byte-for-byte.
        """
        mutant_root = os.path.join(self.work_dir, mutant_id)
        model_src = os.path.join(mutant_root, "model_src")
        if os.path.exists(model_src):
            shutil.rmtree(model_src)
        shutil.copytree(self._model_dir(), model_src)

        spliced = os.path.join(model_src, self._design_relpath(target_file))
        with open(spliced, "w") as f:
            f.write(mutant_sv)

        orig_model = self.mut_cfg.get_model()
        copied_models_yaml = os.path.join(
            model_src, os.path.basename(os.path.abspath(orig_model.path))
        )
        return dataclasses.replace(orig_model, path=copied_models_yaml), mutant_root

    # --- FPV oracle ---------------------------------------------------------

    def _load_fpv_cfg(self):
        from ..config.fpv import FpvSuiteConfig

        suite = FpvSuiteConfig(path=self.mut_cfg.fpv_config)
        # Raises FatalRtlBuddyError if the named verification is absent.
        return suite.get_verifications(self.mut_cfg.verification)[0]

    def _baseline_fpv(self, fpv_cfg) -> str:
        suite_dir = os.path.join(self.work_dir, "baseline_fpv")
        Path(suite_dir).mkdir(parents=True, exist_ok=True)
        results = FpvRunner(
            name=self.name + "/baseline_fpv",
            root_cfg=self.root_cfg,
            fpv_cfg=fpv_cfg,
            suite_dir=suite_dir,
        ).run()
        return results.results.get("result", "NA")

    def _eval_fpv(self, fpv_cfg, mutant_model, mutant_root, mutant_id, baseline):
        """Return (outcome, "fpv=<verdict>") for the FPV oracle."""
        try:
            mutant_fpv_cfg = dataclasses.replace(
                fpv_cfg,
                model=mutant_model,
                name=f"{fpv_cfg.get_name()}__{mutant_id}",
            )
            results = FpvRunner(
                name=self.name + "/" + mutant_id + "/fpv",
                root_cfg=self.root_cfg,
                fpv_cfg=mutant_fpv_cfg,
                suite_dir=os.path.join(mutant_root, "fpv"),
            ).run()
            verdict = results.results.get("result", "NA")
        except FatalRtlBuddyError:
            return ERRORED, "fpv=ERROR"
        if verdict in ("NA", "ERROR"):
            return ERRORED, "fpv=ERROR"
        outcome = KILLED if verdict != baseline else SURVIVED
        return outcome, f"fpv={verdict}"

    # --- sim oracle ---------------------------------------------------------

    def _sim_suite_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.mut_cfg.test_config))

    def _sim_tests(self, suite):
        names = self.mut_cfg.tests or [None]
        tests = []
        for name in names:
            tests.extend(suite.get_tests(name))
        return tests

    def _run_one_test(self, test_cfg, suite_dir, name_suffix):
        from .test_runner import TestRunner

        return TestRunner(
            name=self.name + "/" + name_suffix,
            root_cfg=self.root_cfg,
            test_cfg=test_cfg,
            rtl_builder_mode=self.rtl_builder_mode,
            test_runner_mode={"sim_to_stdout": False},
            suite_dir=suite_dir,
        ).run()

    @staticmethod
    def _is_build_error(results) -> bool:
        # A mutant that won't compile is "errored", not killed. These
        # result classes all signal a failure *before* the design's
        # behaviour was actually exercised.
        return type(results).__name__ in (
            "CompileFailResults",
            "FilelistFailResults",
            "SetupFailResults",
        )

    @staticmethod
    def _assertion_fired(results) -> bool:
        return (results.results.get("assertions") or {}).get("fired", 0) > 0

    def _baseline_sim(self) -> str:
        from ..config.suite import SuiteConfig

        suite = SuiteConfig(path=self.mut_cfg.test_config)
        suite_dir = self._sim_suite_dir()
        all_pass = True
        scored = False
        for tcfg in self._sim_tests(suite):
            mt = dataclasses.replace(tcfg, assertions=self.mut_cfg.assertions)
            res = self._run_one_test(mt, suite_dir, "baseline_sim")
            scored = True
            if (not res.is_pass()) or self._assertion_fired(res):
                all_pass = False
        if not scored:
            return "NA"
        return "PASS" if all_pass else "FAIL"

    def _eval_sim(self, mutant_model, mutant_id):
        """Return (outcome, "sim=<verdict>") for the sim oracle.

        Killed when any selected test FAILs or fires an assertion;
        build failures are errored (dropped), not killed.
        """
        from ..config.suite import SuiteConfig

        suite = SuiteConfig(path=self.mut_cfg.test_config)
        suite_dir = self._sim_suite_dir()
        killed = False
        scored = False
        for tcfg in self._sim_tests(suite):
            mt = dataclasses.replace(
                tcfg, model=mutant_model, assertions=self.mut_cfg.assertions
            )
            res = self._run_one_test(mt, suite_dir, mutant_id + "/sim")
            if self._is_build_error(res):
                continue
            scored = True
            if (not res.is_pass()) or self._assertion_fired(res):
                killed = True
        if not scored:
            return ERRORED, "sim=ERROR"
        return (KILLED if killed else SURVIVED), ("sim=FAIL" if killed else "sim=PASS")

    # --- scoring ------------------------------------------------------------

    def _score_mutant(
        self, idx: int, mutant, fpv_cfg, fpv_baseline, target_file: str | None = None
    ) -> MutantOutcome:
        operator = mutant.kind.value
        mutant_id = f"m{idx:04d}_{operator}"
        predicted = sorted(getattr(mutant.prediction, "perturbs_signals", []) or [])
        # Model-relative origin file, recorded only for scoped (multi-file)
        # campaigns; empty for the single-file path (back-compat).
        file_rel = self._design_relpath(target_file) if target_file else ""

        try:
            mutant_model, mutant_root = self._materialise_mutant(
                mutant_id, mutant.sv, target_file=target_file
            )
        except OSError as e:
            log_event(
                logger,
                logging.WARNING,
                "mut_runner.materialise_failed",
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
                file=file_rel,
            )

        per_outcomes: list[str] = []
        verdicts: list[str] = []
        if fpv_cfg is not None:
            o, v = self._eval_fpv(
                fpv_cfg, mutant_model, mutant_root, mutant_id, fpv_baseline
            )
            per_outcomes.append(o)
            verdicts.append(v)
        if self.mut_cfg.has_sim_oracle():
            o, v = self._eval_sim(mutant_model, mutant_id)
            per_outcomes.append(o)
            verdicts.append(v)

        # Union semantics: killed if any oracle caught it; else survived if
        # any oracle actually scored it; else errored (every oracle failed
        # to build/evaluate the mutant).
        if KILLED in per_outcomes:
            overall = KILLED
        elif SURVIVED in per_outcomes:
            overall = SURVIVED
        else:
            overall = ERRORED

        log_event(
            logger,
            logging.DEBUG,
            "mut_runner.mutant_scored",
            campaign=self.mut_cfg.get_name(),
            mutant=mutant_id,
            operator=operator,
            verdict=" ".join(verdicts),
            outcome=overall,
        )
        return MutantOutcome(
            mutant_id=mutant_id,
            operator=operator,
            outcome=overall,
            diff_summary=mutant.diff_summary,
            verdict=" ".join(verdicts) or "NA",
            predicted_signals=predicted,
            file=file_rel,
        )

    def _deadline(self) -> float | None:
        mins = self.mut_cfg.budget.time_budget_minutes
        if mins is None:
            return None
        return time.monotonic() + mins * 60.0
