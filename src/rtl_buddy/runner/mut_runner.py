"""Mutation-campaign runner for ``rb mut``.

Generates mutants with the external ``rtl-buddy-xeno`` engine, splices each into an isolated copy of the model source tree,
and scores it against the configured FPV and sim oracles. xeno is optional and imported lazily.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import importlib.metadata
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from ..config.mut import MutConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..seeding import resolve_test_seed
from .fpv_runner import FpvRunner
from .mut_results import ERRORED, KILLED, SURVIVED, MutantOutcome, MutResults

logger = logging.getLogger(__name__)


# Keep in sync with the floor of the `mut` extra in pyproject.toml.
_XENO_MIN_VERSION = "0.1.0"


_XENO_INSTALL_HINT = (
    "rb mut requires the rtl-buddy-xeno mutation engine, which is not "
    "installed. Install it with the mut extra:\n"
    '    pip install "rtl_buddy[mut]"\n'
    "or install the engine directly:\n"
    f'    pip install "rtl-buddy-xeno[verible,slang] >= {_XENO_MIN_VERSION}"\n'
    "(the [verible] and [slang] extras pull the Verible CST + pyslang "
    "toolchain the structural operators need)."
)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Return the leading (major, minor, patch) ints of a PEP 440 version; suffixes such as rc/dev/+local are dropped."""
    parts = []
    for segment in version.split(".")[:3]:
        match = re.match(r"\d+", segment)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


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
        # Builder mode for the sim oracle's TestRunner; the FPV oracle ignores it.
        self.rtl_builder_mode = rtl_builder_mode

    @staticmethod
    def _load_xeno():
        try:
            import rtl_buddy_xeno
        except ImportError as e:
            raise FatalRtlBuddyError(_XENO_INSTALL_HINT) from e
        MutRunner._check_xeno_version()
        return rtl_buddy_xeno

    @staticmethod
    def _check_xeno_version() -> None:
        """Raise when the installed rtl-buddy-xeno is older than ``_XENO_MIN_VERSION``.

        Git and editable installs bypass the pyproject floor. The check is skipped when no distribution metadata is available.
        """
        try:
            installed = importlib.metadata.version("rtl-buddy-xeno")
        except importlib.metadata.PackageNotFoundError:
            return
        if _version_tuple(installed) < _version_tuple(_XENO_MIN_VERSION):
            raise FatalRtlBuddyError(
                f"rb mut requires rtl-buddy-xeno >= {_XENO_MIN_VERSION}, "
                f"but {installed} is installed. Upgrade it with:\n"
                f'    pip install -U "rtl-buddy-xeno[verible,slang] '
                f'>= {_XENO_MIN_VERSION}"'
            )

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
            count = min(count, budget.per_file_cap)
        return count

    def _schedule(self, xeno):
        if self.mut_cfg.budget.schedule == "round_robin":
            return xeno.Schedule.ROUND_ROBIN
        return xeno.Schedule.SEQUENTIAL

    def _scope_graph_json(self) -> dict:
        """Run rtl-buddy-view into ``hier.json`` under the work dir and load it.

        Only called when the campaign has a scope. Raises ``FatalRtlBuddyError`` if the binary is missing or the schema major version is not 1.
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
        """Resolve scope include/exclude globs against the hier graph.

        Each glob is matched against a node's dotted instance path, its absolute source file and that file relative to the model dir.
        A node is in scope when include is empty or matches, and exclude does not match.

        Returns the sorted, de-duplicated absolute source files. Raises ``FatalRtlBuddyError`` when none are selected or one lies outside the model dir.
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
            # fnmatchcase, not fnmatch: fnmatch case-folds on some platforms.
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

        Each candidate has a model-relative ``file`` key; ``per_file_cap`` limits the sites reported per file.
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

    def run(self) -> MutResults:
        xeno = self._load_xeno()
        if self.mut_cfg.has_scope():
            return self._run_scoped(xeno)
        mutator = self._mutator(xeno)
        kinds = self._kinds(xeno)

        self._validate_design_in_model()
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        # A non-PASS baseline only warns; every mutant then reads as killed.
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
        """Run a multi-file campaign, mutating each scoped file in sorted order.

        ``per_file_cap`` limits mutants per file. ``max_mutants`` is a global ceiling and the campaign stops when it is
        reached, even mid-file; the time budget also covers the whole campaign, so later files may be truncated. The
        schedule applies per file.
        """
        kinds = self._kinds(xeno)
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

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
        """Copy the model tree, splice in the mutant, and return a per-mutant ModelConfig for the copy plus its work root.

        ``target_file`` is the file to splice into; None means the configured ``design_file``.
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

    def _load_fpv_cfg(self):
        from ..config.fpv import FpvSuiteConfig

        suite = FpvSuiteConfig(path=self.mut_cfg.fpv_config)
        fpv_cfg = suite.get_verifications(self.mut_cfg.verification)[0]
        override = self.mut_cfg.get_top_override()
        if override is None or override == fpv_cfg.get_top():
            return fpv_cfg
        # The campaign's top overrides the verification's so baseline and mutants share a root module.
        log_event(
            logger,
            logging.INFO,
            "mut_runner.fpv_top_override",
            campaign=self.mut_cfg.get_name(),
            verification=fpv_cfg.get_name(),
            fpv_top=fpv_cfg.get_top(),
            top=override,
        )
        return dataclasses.replace(fpv_cfg, top=override)

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

        resolve_test_seed(
            test_cfg,
            self.root_cfg,
            suite_config_path=self.mut_cfg.test_config,
        )
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
        # These failures occur before the design runs, so the mutant is errored, not killed.
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

        Killed when a selected test fails or an assertion fires; build failures are errored.
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

    def _score_mutant(
        self, idx: int, mutant, fpv_cfg, fpv_baseline, target_file: str | None = None
    ) -> MutantOutcome:
        operator = mutant.kind.value
        mutant_id = f"m{idx:04d}_{operator}"
        predicted = sorted(getattr(mutant.prediction, "perturbs_signals", []) or [])
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

        # Killed if any oracle killed it, else survived if any scored it, else errored.
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
