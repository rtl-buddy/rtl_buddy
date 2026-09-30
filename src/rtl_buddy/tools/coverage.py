# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Coverage result orchestration for rtl-buddy summaries."""

import contextlib
import os

from ..cov import manifest as manifest_mod
from ..cov import model as model_mod
from ..cov.raw import METRICS as cov_metrics
from .coverview import CoverviewPacker
from .vlog_cov import CoverageMetrics, VlogCov, aggregate_cover_records


class CoverageReporter:
    """Per-test and merged coverage reporting for rtl-buddy summaries."""

    def __init__(self, root_cfg):
        """Build a reporter for the currently selected builder."""
        self.root_cfg = root_cfg

    def _get_cov_tool(self):
        """Create a `VlogCov` for the platform-selected builder's simulator family.

        A per-test or per-suite ``builder:`` override is not consulted, so the family can
        differ from the one a test simulated on unless ``--builder`` is given.
        """
        simulator_family = self.root_cfg.get_rtl_builder_cfg().get_simulator_family()
        return VlogCov(
            simulator_name=simulator_family,
            use_lcov=self.root_cfg.get_use_lcov(simulator_family),
            root_cfg=self.root_cfg,
        )

    def _get_coverview_tool(self):
        """Create a `CoverviewPacker` for the active simulator family."""
        simulator_family = self.root_cfg.get_rtl_builder_cfg().get_simulator_family()
        return CoverviewPacker(
            cfg=self.root_cfg.get_coverview_cfg(simulator_family),
            project_root=self.root_cfg.get_project_rootdir(),
        )

    def _coverview_dataset_name(self, suite_name: str) -> str:
        """Derive the merged Coverview dataset name from a suite or regression name."""
        dataset = os.path.splitext(os.path.basename(suite_name))[0]
        if dataset.endswith("_regression"):
            dataset = dataset[: -len("_regression")]
        return dataset

    def format_summary(self, test_results):
        """Return the one-line coverage summary for a test result, or None."""
        coverage = test_results.results.get("coverage")
        if coverage is None:
            return None
        return coverage.get("summary")

    def collect_paths(self, suite_results):
        """Collect the raw coverage database paths from suite results."""
        raw_paths = []
        for suite_result in suite_results:
            coverage = suite_result["results"].results.get("coverage")
            if coverage is None:
                continue
            raw_paths.extend(coverage.get("raw_paths", []))
        return raw_paths

    def collect_cover_records(self, suite_results):
        """Fold per-test user cover points into run-wide ``{name, file, line, module, hits}`` records.

        Keyed on ``(file, line, name, module)`` and independent of the merge mode.
        Returns None when no user cover points were recorded.
        """
        records = []
        for suite_result in suite_results:
            coverage = suite_result["results"].results.get("coverage")
            if coverage is None:
                continue
            records.extend(coverage.get("covers") or [])
        return aggregate_cover_records(records)

    def _normalize_source_roots(self, outdir, source_roots=None, suite_name=None):
        """Return absolute source roots, defaulting to the suite directory when none are given."""
        roots = []
        seen = set()

        def add_root(root):
            if root is None:
                return
            root = os.path.abspath(root)
            if root not in seen:
                seen.add(root)
                roots.append(root)

        if source_roots is not None:
            for root in source_roots:
                add_root(root)

        if suite_name is not None and len(roots) == 0:
            add_root(os.path.dirname(os.path.join(outdir, suite_name)))

        return roots

    def _cov_dir(self, outdir):
        """Return (creating it) the `cov_dir` intermediate artifact directory under ``outdir``."""
        cov_dir = os.path.join(outdir, "cov_dir")
        os.makedirs(cov_dir, exist_ok=True)
        return cov_dir

    def resolve_dir_summary_paths(self, dir_summary_paths=None, dir_summary_file=None):
        """Return deduplicated repo-relative directory prefixes from CLI args and/or a file of one path per line."""
        resolved = []
        seen = set()

        def add_path(path):
            if path is None:
                return
            normalized = path.replace("\\", "/").strip().strip("/")
            if not normalized:
                return
            if normalized.startswith("./"):
                normalized = normalized[2:]
            if normalized not in seen:
                seen.add(normalized)
                resolved.append(normalized)

        if dir_summary_paths is not None:
            for path in dir_summary_paths:
                add_path(path)

        if dir_summary_file is not None:
            with open(dir_summary_file, "r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.split("#", 1)[0].strip()
                    if line:
                        add_path(line)

        return resolved

    @staticmethod
    def _metrics_payload(metrics):
        """Structured per-metric dict for a CoverageMetrics, including ``expression``, which the display summary omits."""
        return {
            "line": metrics.line,
            "branch": metrics.branch,
            "toggle": metrics.toggle,
            "expression": metrics.expression,
            "functional": metrics.functional,
        }

    @staticmethod
    def _dir_summary_lines(records):
        """Format structured per-prefix records into `Coverage <prefix>: L/B/T/F` lines."""
        lines = []
        for r in records:
            metrics = CoverageMetrics(
                line=r["line"],
                branch=r["branch"],
                toggle=r["toggle"],
                functional=r["functional"],
            )
            lines.append(f"Coverage {r['prefix']}: {metrics.summary_str()}")
        return lines

    def _dir_summary_records(self, lcov_path, dir_summary_paths):
        """Per-prefix ``{prefix, line, branch, toggle, functional}`` records from an LCOV file.

        ``toggle`` and ``functional`` are ``None``; an LCOV file has only line and branch.
        """
        if lcov_path is None or not os.path.exists(lcov_path) or not dir_summary_paths:
            return []

        cov = self._get_cov_tool()
        records = []
        for prefix in dir_summary_paths:
            line, branch = cov.parse_lcov_summary_for_prefix(lcov_path, prefix)
            records.append(
                {
                    "prefix": prefix,
                    "line": line,
                    "branch": branch,
                    "toggle": None,
                    "functional": None,
                }
            )
        return records

    def _dir_summary_records_from_dataset_files(self, dataset_files, dir_summary_paths):
        """Per-prefix coverage records from typed dataset files, including toggle when available."""
        if not dataset_files or not dir_summary_paths:
            return []

        cov = self._get_cov_tool()
        records = []
        for prefix in dir_summary_paths:
            line = branch = toggle = None

            line_info = dataset_files.get("line")
            if line_info is not None:
                line, _ = cov.parse_lcov_summary_for_prefix(line_info, prefix)

            branch_info = dataset_files.get("branch")
            if branch_info is not None:
                _, branch = cov.parse_lcov_summary_for_prefix(branch_info, prefix)

            toggle_info = dataset_files.get("toggle")
            if toggle_info is not None:
                _, toggle = cov.parse_lcov_summary_for_prefix(toggle_info, prefix)

            records.append(
                {
                    "prefix": prefix,
                    "line": line,
                    "branch": branch,
                    "toggle": toggle,
                    "functional": None,
                }
            )
        return records

    @staticmethod
    def _source_summary_record(model):
        """Return ``{"source_totals": {...}, "totals": {...}}``, or None when the model has neither.

        ``source_totals`` scores each source point once (covered when any elaboration hit
        it); ``totals`` scores each elaborated point. Both are ``{found, hit, ratio}`` per
        metric. They come from the model built from raw ``.dat`` databases, since LCOV
        has already merged elaborations. With no raw database the two are equal.
        """
        collapsed = model_mod.source_totals(model or {})
        if collapsed is None:
            return None
        return {"source_totals": collapsed, "totals": model.get("totals") or {}}

    @staticmethod
    def _source_summary_lines(record):
        """Format a source-summary record as one line per metric.

        ``Coverage source points <metric>: <hit>/<found> (NN.N%) [per elaboration <hit>/<found> (NN.N%)]``.
        Metrics with no recorded points are skipped.
        """
        if not record:
            return []

        def fmt(entry):
            found = (entry or {}).get("found") or 0
            hit = (entry or {}).get("hit") or 0
            if found == 0:
                return None
            return f"{hit}/{found} ({hit / found * 100:.1f}%)"

        lines = []
        collapsed = record.get("source_totals") or {}
        per_elab = record.get("totals") or {}
        for metric in cov_metrics:
            source_cell = fmt(collapsed.get(metric))
            if source_cell is None:
                continue
            elab_cell = fmt(per_elab.get(metric))
            suffix = "" if elab_cell is None else f" [per elaboration {elab_cell}]"
            lines.append(f"Coverage source points {metric}: {source_cell}{suffix}")
        return lines

    def _dir_summary_metadata(self, lcov_path, dir_summary_paths):
        """Summary lines for repo-relative directory prefixes from an LCOV file."""
        return self._dir_summary_lines(
            self._dir_summary_records(lcov_path, dir_summary_paths)
        )

    def _dir_summary_metadata_from_dataset_files(
        self, dataset_files, dir_summary_paths
    ):
        """Summary lines for repo-relative directory prefixes from typed dataset files."""
        return self._dir_summary_lines(
            self._dir_summary_records_from_dataset_files(
                dataset_files, dir_summary_paths
            )
        )

    def _test_artefacts(self, suite_results, *, outdir, suite_name, source_roots):
        """Per-test coverage artefacts for the model builder, taken from the per-test coverage dicts rather than any merge product."""
        suite_roots = self._normalize_source_roots(
            outdir, source_roots=source_roots, suite_name=suite_name
        )
        entries = []
        for suite_result in suite_results:
            coverage = suite_result["results"].results.get("coverage")
            if coverage is None:
                continue
            raw_paths = coverage.get("raw_paths") or []
            raw = raw_paths[0] if len(raw_paths) > 0 else None
            info = coverage.get("lcov_path")
            if raw is None and info is None:
                continue
            hints = [] if raw is None else [os.path.dirname(raw)]
            hints.extend(suite_roots)
            entries.append(
                model_mod.TestArtefacts(
                    name=suite_result["test_name"],
                    raw=raw,
                    info=info,
                    suite=suite_name,
                    source_roots=tuple(hints),
                )
            )
        return entries

    def _test_manifest_rows(self, suite_results, suite_name):
        """Per-test artefact rows for the manifest."""
        rows = []
        for suite_result in suite_results:
            coverage = suite_result["results"].results.get("coverage")
            if coverage is None:
                continue
            raw_paths = coverage.get("raw_paths") or []
            rows.append(
                {
                    "name": suite_result["test_name"],
                    "suite": suite_name,
                    "raw": raw_paths[0] if len(raw_paths) > 0 else None,
                    "info": coverage.get("lcov_path"),
                    "html_dir": coverage.get("html_dir"),
                    "coverview_zip": coverage.get("coverview_zip"),
                }
            )
        return rows

    def build_run_model(
        self,
        suite_results,
        *,
        outdir,
        suite_name,
        source_roots=None,
        merged_info=None,
        model_mode=model_mod.MODEL_MODE_FULL,
    ):
        """Build the structured coverage model for a run.

        Returns None when the run produced no coverage, or when no artefact parsed into a
        coverage point (no simulator coverage support, or databases since cleaned).
        A ``model_mode`` other than ``full`` omits per-point test attribution; totals,
        per-test rows and source-point figures are unchanged.
        """
        tests = self._test_artefacts(
            suite_results,
            outdir=outdir,
            suite_name=suite_name,
            source_roots=source_roots,
        )
        if len(tests) == 0:
            return None
        model = model_mod.build_model(
            tests,
            project_root=self.root_cfg.get_project_rootdir(),
            simulator=self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
            merged_info=merged_info,
            attribution=model_mode == model_mod.MODEL_MODE_FULL,
        )
        return model if model["files"] else None

    def write_artefacts(
        self,
        suite_results,
        *,
        outdir,
        suite_name,
        command,
        source_roots=None,
        merge_mode=None,
        merged=None,
        datasets=None,
        descriptions=None,
        coverview=None,
        merge_failed=False,
        failed_metrics=None,
        model=None,
        model_mode=model_mod.MODEL_MODE_FULL,
    ):
        """Write the coverage model and manifest and return the artefacts block, or None if there is no coverage.

        ``model`` reuses a model from :meth:`build_run_model`. ``model_mode="none"`` writes
        the manifest with totals but no model, and removes any earlier model file.
        """
        project_root = self.root_cfg.get_project_rootdir()
        builder_cfg = self.root_cfg.get_rtl_builder_cfg()
        simulator_family = builder_cfg.get_simulator_family()
        merged = merged or {}

        if model is None:
            model = self.build_run_model(
                suite_results,
                outdir=outdir,
                suite_name=suite_name,
                source_roots=source_roots,
                merged_info=merged.get("info"),
                model_mode=model_mode,
            )
        if model is None:
            return None

        cov_dir = self._cov_dir(outdir)
        if model_mode == model_mod.MODEL_MODE_NONE:
            model_path = None
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(cov_dir, model_mod.MODEL_FILENAME))
        else:
            model_path = model_mod.write_model(model, cov_dir)
        manifest = manifest_mod.build_manifest(
            project_root=project_root,
            cov_dir=cov_dir,
            command=command,
            suite=suite_name,
            builder=builder_cfg.get_name(),
            simulator_family=simulator_family,
            merge_mode=merge_mode,
            merge_failed=merge_failed,
            failed_metrics=failed_metrics,
            model_path=model_path,
            coverage_model=model_mode,
            totals=model["totals"],
            source_totals=model_mod.source_totals(model),
            merged=merged,
            datasets=datasets,
            descriptions=descriptions,
            coverview=coverview,
            tests=self._test_manifest_rows(suite_results, suite_name),
        )
        manifest_path = manifest_mod.write_manifest(manifest, cov_dir)
        return {
            "manifest": manifest_mod.project_relative(manifest_path, project_root),
            "merge_failed": manifest["merge_failed"],
            "failed_metrics": list(manifest["failed_metrics"]),
            "cov_dir": manifest["cov_dir"],
            "model": manifest["model"],
            "merged_info": manifest["merged"]["info"],
            "merged_raw": manifest["merged"]["raw"],
            "merged_desc": manifest["merged"]["desc"],
            "html_dir": manifest["merged"]["html_dir"],
            "datasets": manifest["datasets"],
            "descriptions": manifest["descriptions"],
            "coverview_zip": manifest["coverview"]["zip"],
            "coverview_per_test_zip": manifest["coverview"]["per_test_zip"],
        }

    def merge(
        self,
        suite_results,
        outdir,
        basename="coverage_merged",
        html_output=False,
        source_roots=None,
    ):
        """Merge the raw coverage files of all tests and return aggregate metrics."""
        raw_paths = self.collect_paths(suite_results)
        if len(raw_paths) == 0:
            return None
        cov_dir = self._cov_dir(outdir)
        return self._get_cov_tool().merge(
            raw_paths=raw_paths,
            outdir=cov_dir,
            merge_basename=basename,
            html_output=html_output,
            source_roots=self._normalize_source_roots(
                outdir, source_roots=source_roots
            ),
            html_outdir=outdir,
        )

    def generate_unmerged_artifacts(
        self,
        suite_results,
        outdir,
        suite_name,
        coverview_output=False,
        source_roots=None,
    ):
        """Generate per-test LCOV and HTML artifacts."""
        return self.generate_per_test_artifacts(
            suite_results,
            outdir=outdir,
            suite_name=suite_name,
            html_output=True,
            coverview_output=coverview_output,
            source_roots=source_roots,
        )

    def generate_per_test_artifacts(
        self,
        suite_results,
        *,
        outdir,
        suite_name,
        html_output=False,
        coverview_output=False,
        source_roots=None,
    ):
        """Generate per-test LCOV and optional HTML and Coverview artifacts."""
        cov = self._get_cov_tool()
        coverview = self._get_coverview_tool()
        cov_dir = self._cov_dir(outdir)
        source_roots = self._normalize_source_roots(
            outdir, source_roots=source_roots, suite_name=suite_name
        )
        try:
            suite_label = os.path.relpath(suite_name, outdir)
        except ValueError:
            suite_label = suite_name
        generated = []
        for suite_result in suite_results:
            coverage = suite_result["results"].results.get("coverage")
            if coverage is None:
                continue
            raw_paths = coverage.get("raw_paths", [])
            if len(raw_paths) == 0:
                continue
            metrics = cov.generate_artifacts(
                raw_paths[0],
                outdir=cov_dir,
                html_output=html_output,
                artifact_name=f"{suite_label}__{suite_result['test_name']}",
                source_roots=source_roots,
                html_outdir=outdir,
            )
            if metrics is not None:
                updated = metrics.to_dict()
                updated["raw_paths"] = list(raw_paths)
                coverview_zip = None
                if coverview_output and metrics.lcov_path is not None:
                    safe_dataset = cov._sanitize_artifact_name(
                        f"{suite_label}__{suite_result['test_name']}"
                    )
                    cv = coverview.package_info(
                        info_path=metrics.lcov_path,
                        outdir=cov_dir,
                        dataset_name=safe_dataset,
                        zip_name=f"coverview_{safe_dataset}.zip",
                        raw_path=raw_paths[0],
                        zip_outdir=outdir,
                        metadata={
                            "suite": os.path.relpath(
                                suite_name, self.root_cfg.get_project_rootdir()
                            ),
                            "test": suite_result["test_name"],
                            "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
                            "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
                        },
                    )
                    if cv is not None:
                        coverview_zip = cv.zip_path
                        updated["coverview_zip"] = cv.zip_path
                coverage.update(updated)
                generated.append(
                    (
                        f"{suite_label}::{suite_result['test_name']}",
                        metrics,
                        coverview_zip,
                    )
                )
        return generated

    def merge_info_process(
        self,
        suite_results,
        *,
        outdir,
        suite_name,
        html_output=False,
        coverview_output=False,
        source_roots=None,
    ):
        """Merge per-test `.info` files with `info-process merge`, optionally emitting HTML and Coverview.

        Returns ``(metrics, coverview_zip, dataset_files, description_files)``, or None if
        nothing merged. The ``.desc`` files are reported whether or not Coverview was packaged.
        """
        cov = self._get_cov_tool()
        coverview = self._get_coverview_tool()
        cov_dir = self._cov_dir(outdir)
        generated = self.generate_per_test_artifacts(
            suite_results,
            outdir=outdir,
            suite_name=suite_name,
            html_output=False,
            coverview_output=False,
            source_roots=self._normalize_source_roots(
                outdir,
                source_roots=source_roots,
                suite_name=suite_name,
            ),
        )
        info_inputs = [
            metrics.lcov_path
            for _, metrics, _ in generated
            if metrics.lcov_path is not None
        ]
        if len(info_inputs) == 0:
            return None

        merged_lcov_path = os.path.join(cov_dir, "coverage_merged.info")
        merged_test_list = os.path.join(cov_dir, "coverage_merged.desc")
        merged_lcov = coverview.merge_infos(
            info_inputs,
            output_path=merged_lcov_path,
            test_list_path=merged_test_list,
        )
        if merged_lcov is None:
            return None

        metrics = CoverageMetrics()
        metrics.lcov_path = merged_lcov
        metrics.line, metrics.branch = cov.parse_lcov_summary(merged_lcov)

        safe_dataset = cov._sanitize_artifact_name(
            self._coverview_dataset_name(suite_name)
        )
        merged_dataset_files = {
            "line": None,
            "branch": None,
            "expression": None,
            "toggle": None,
        }
        rby_description_files = {
            "branch": None,
            "expression": None,
            "toggle": None,
        }
        line_info_path = os.path.join(cov_dir, f"coverage_line_{safe_dataset}.info")
        branch_info_path = os.path.join(cov_dir, f"coverage_branch_{safe_dataset}.info")
        if (
            coverview._extract_typed_info(
                coverview._get_info_process(), merged_lcov, line_info_path, "line"
            )
            is not None
        ):
            merged_dataset_files["line"] = line_info_path

        toggle_inputs = []
        expression_inputs = []
        branch_inputs = []
        for test_name_i, metrics_i, _ in generated:
            if metrics_i.lcov_path is not None:
                artifact_stem = cov._sanitize_artifact_name(
                    test_name_i.replace("::", "__")
                )
                branch_info = os.path.join(
                    cov_dir, f"coverage_branch_{artifact_stem}.info"
                )
                if (
                    coverview._extract_typed_info(
                        coverview._get_info_process(),
                        metrics_i.lcov_path,
                        branch_info,
                        "branch",
                    )
                    is not None
                ):
                    branch_inputs.append(branch_info)
            raw_paths = metrics_i.raw_paths or []
            if len(raw_paths) == 0:
                continue
            artifact_stem = cov._sanitize_artifact_name(test_name_i.replace("::", "__"))
            toggle_info = coverview.write_toggle_info(
                raw_paths[0], cov_dir, artifact_stem
            )
            if toggle_info is not None:
                toggle_inputs.append(toggle_info)
            expression_info = coverview.write_expression_info(
                raw_paths[0], cov_dir, artifact_stem
            )
            if expression_info is not None:
                expression_inputs.append(expression_info)

        if len(branch_inputs) > 0:
            merged_branch_path = os.path.join(
                cov_dir, f"coverage_branch_{safe_dataset}.info"
            )
            merged_branch_desc = os.path.join(
                cov_dir, f"covrby_branch_{safe_dataset}.desc"
            )
            merged_branch = coverview.merge_infos(
                branch_inputs,
                output_path=merged_branch_path,
                test_list_path=merged_branch_desc,
            )
            if merged_branch is not None:
                merged_dataset_files["branch"] = merged_branch
                metrics.branch = cov.parse_lcov_summary(merged_branch)[1]
                if os.path.exists(merged_branch_desc):
                    rby_description_files["branch"] = merged_branch_desc
        elif (
            coverview._extract_typed_info(
                coverview._get_info_process(), merged_lcov, branch_info_path, "branch"
            )
            is not None
        ):
            merged_dataset_files["branch"] = branch_info_path

        if len(toggle_inputs) > 0:
            merged_toggle_path = os.path.join(
                cov_dir, f"coverage_toggle_{safe_dataset}.info"
            )
            merged_toggle_desc = os.path.join(
                cov_dir, f"covrby_toggle_{safe_dataset}.desc"
            )
            merged_toggle = coverview.merge_infos(
                toggle_inputs,
                output_path=merged_toggle_path,
                test_list_path=merged_toggle_desc,
            )
            if merged_toggle is not None:
                merged_dataset_files["toggle"] = merged_toggle
                metrics.toggle, _ = cov.parse_lcov_summary(merged_toggle)
                if os.path.exists(merged_toggle_desc):
                    rby_description_files["toggle"] = merged_toggle_desc

        if len(expression_inputs) > 0:
            merged_expression_path = os.path.join(
                cov_dir, f"coverage_expression_{safe_dataset}.info"
            )
            merged_expression_desc = os.path.join(
                cov_dir, f"covrby_expression_{safe_dataset}.desc"
            )
            merged_expression = coverview.merge_infos(
                expression_inputs,
                output_path=merged_expression_path,
                test_list_path=merged_expression_desc,
            )
            if merged_expression is not None:
                merged_dataset_files["expression"] = merged_expression
                # This per-type file records one `DA:` per term, so its line ratio is the
                # expression ratio (as for toggle); the merged LCOV has no expression detail.
                metrics.expression, _ = cov.parse_lcov_summary(merged_expression)
                if os.path.exists(merged_expression_desc):
                    rby_description_files["expression"] = merged_expression_desc

        if html_output:
            metrics.html_dir = cov.generate_html(
                merged_lcov,
                outdir=cov_dir,
                html_dirname="coverage_merge.html",
                html_outdir=outdir,
            )

        description_files = dict(rby_description_files)
        description_files["line"] = (
            merged_test_list if os.path.exists(merged_test_list) else None
        )

        coverview_zip = None
        if coverview_output:
            cv = coverview.package_dataset_files(
                dataset_name=safe_dataset,
                dataset_files=merged_dataset_files,
                outdir=cov_dir,
                zip_name=f"coverview_{safe_dataset}.zip",
                description_files={"line": description_files["line"]},
                rby_description_files=rby_description_files,
                zip_outdir=outdir,
                metadata={
                    "suite": os.path.relpath(
                        suite_name, self.root_cfg.get_project_rootdir()
                    ),
                    "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
                    "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
                    "merged": True,
                    "merge_mode": "info_process",
                },
            )
            if cv is not None:
                coverview_zip = cv.zip_path

        return metrics, coverview_zip, merged_dataset_files, description_files

    def generate_per_test_coverview(
        self, reg_results, *, outdir, suite_name, source_roots=None
    ):
        """Generate one Coverview archive with one dataset per test."""
        cov = self._get_cov_tool()
        coverview = self._get_coverview_tool()
        cov_dir = self._cov_dir(outdir)
        info_inputs = []

        for reg_result in reg_results:
            suite_path = reg_result["test_suite"]
            for suite_result in reg_result["results"]:
                coverage = suite_result["results"].results.get("coverage")
                if coverage is None:
                    continue
                raw_paths = coverage.get("raw_paths", [])
                if len(raw_paths) == 0:
                    continue
                artifact_stem = f"{suite_path}__{suite_result['test_name']}"
                metrics = cov.generate_artifacts(
                    raw_paths[0],
                    outdir=cov_dir,
                    html_output=False,
                    artifact_name=artifact_stem,
                    source_roots=self._normalize_source_roots(
                        outdir,
                        source_roots=source_roots,
                        suite_name=suite_path,
                    ),
                )
                if metrics is None or metrics.lcov_path is None:
                    continue
                info_inputs.append(
                    {
                        "info_path": metrics.lcov_path,
                        "dataset_name": cov._sanitize_artifact_name(artifact_stem),
                        "raw_path": raw_paths[0],
                        "test_name": suite_result["test_name"],
                    }
                )

        if len(info_inputs) == 0:
            return None

        safe_dataset = self._get_cov_tool()._sanitize_artifact_name(
            self._coverview_dataset_name(suite_name)
        )
        return coverview.package_infos(
            info_inputs=info_inputs,
            outdir=cov_dir,
            dataset_name=safe_dataset,
            zip_name=f"coverview_{safe_dataset}_per_test.zip",
            zip_outdir=outdir,
            metadata={
                "suite": os.path.relpath(
                    suite_name, self.root_cfg.get_project_rootdir()
                ),
                "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
                "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
                "per_test": True,
            },
        )

    def build_metadata(
        self,
        suite_results,
        *,
        outdir,
        suite_name,
        coverage_merge=False,
        coverage_merge_raw=False,
        coverage_html=False,
        coverage_coverview=False,
        coverage_per_test=False,
        reg_results=None,
        coverage_merge_info_process=False,
        source_roots=None,
        dir_summary_paths=None,
        source_summary=False,
        command="regression",
        model_mode=model_mod.MODEL_MODE_FULL,
    ):
        """Build the coverage summaries for merged or unmerged runs.

        Returns ``(metadata, coverage)``: ``metadata`` is the list of display lines and
        ``coverage`` the machine payload. Its keys:

        - ``merged``: ``{line, branch, toggle, functional}``, or None when no merge happened.
        - ``dir_summary``: per-prefix records.
        - ``covers``: user cover points; omitted when none were recorded.
        - ``artefacts``: project-relative paths of everything written, including
          ``cov_dir/manifest.json``; present whenever the run produced coverage.
        - ``merge_failed`` and ``failed_metrics``: always present. A requested merge that
          died sets them, and the named metrics read ``FAIL`` instead of ``UNSP``.
        - ``source_summary``: only with ``source_summary=True``; see
          :meth:`_source_summary_record`.

        ``model_mode`` is ``--coverage-model``: ``full``, ``totals`` (no per-point test
        attribution) or ``none`` (manifest only).
        """
        metadata = []
        coverage = {
            "merged": None,
            "dir_summary": [],
            "merge_failed": False,
            "failed_metrics": [],
        }
        covers = self.collect_cover_records(suite_results)
        if covers:
            coverage["covers"] = covers
        # Paths accumulated by the branches below for the single manifest.
        merge_mode = None
        merged_paths = {"info": None, "raw": None, "desc": None, "html_dir": None}
        dataset_files = None
        description_files = None
        coverview_paths = {"zip": None, "per_test_zip": None}

        def record_merged(merged_cov):
            merged_paths["info"] = merged_cov.lcov_path
            merged_paths["raw"] = merged_cov.merged_path
            merged_paths["html_dir"] = merged_cov.html_dir

        def record_merge_failure(merged_cov):
            """Record a failed raw merge in the payload and add a console line explaining it."""
            if not merged_cov.merge_failed:
                return
            failed = list(merged_cov.failed_metrics or [])
            coverage["merge_failed"] = True
            coverage["failed_metrics"] = failed
            lost = ", ".join(failed) if failed else "no metric"
            metadata.append(
                "Coverage merge FAILED: verilator_coverage --write wrote no merged "
                f"database, so {lost} read FAIL (measurement lost), not UNSP "
                "(not instrumented) — see the coverage.merge.failed event"
            )

        if coverage_merge_raw:
            merged_cov = self.merge(
                suite_results,
                outdir=outdir,
                html_output=coverage_html,
                source_roots=source_roots,
            )
            if merged_cov is not None:
                merge_mode = "raw"
                record_merged(merged_cov)
                metadata.append(f"Merged Coverage: {merged_cov.summary_str()}")
                coverage["merged"] = self._metrics_payload(merged_cov)
                record_merge_failure(merged_cov)
                if merged_cov.lcov_path is not None:
                    metadata.append(f"Merged LCOV: {merged_cov.lcov_path}")
                    records = self._dir_summary_records(
                        merged_cov.lcov_path, dir_summary_paths
                    )
                    metadata.extend(self._dir_summary_lines(records))
                    coverage["dir_summary"] = records
                    if coverage_coverview:
                        safe_dataset = self._get_cov_tool()._sanitize_artifact_name(
                            self._coverview_dataset_name(suite_name)
                        )
                        cv = self._get_coverview_tool().package_info(
                            info_path=merged_cov.lcov_path,
                            outdir=outdir,
                            dataset_name=safe_dataset,
                            zip_name=f"coverview_{safe_dataset}.zip",
                            raw_path=merged_cov.merged_path,
                            metadata={
                                "suite": os.path.relpath(
                                    suite_name, self.root_cfg.get_project_rootdir()
                                ),
                                "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
                                "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
                                "merged": True,
                                "merge_mode": "raw",
                            },
                        )
                        if cv is not None and cv.zip_path is not None:
                            coverview_paths["zip"] = cv.zip_path
                            metadata.append(f"Merged Coverview: {cv.zip_path}")
                if merged_cov.html_dir is not None:
                    metadata.append(f"Merged HTML: {merged_cov.html_dir}")
            if coverage_coverview and coverage_per_test and reg_results is not None:
                cv = self.generate_per_test_coverview(
                    reg_results,
                    outdir=outdir,
                    suite_name=suite_name,
                    source_roots=source_roots,
                )
                if cv is not None and cv.zip_path is not None:
                    coverview_paths["per_test_zip"] = cv.zip_path
                    metadata.append(f"Per-Test Coverview: {cv.zip_path}")
        elif coverage_merge:
            merged_cov = self.merge(
                suite_results,
                outdir=outdir,
                html_output=coverage_html,
                source_roots=source_roots,
            )
            merged_dataset_files = None
            if merged_cov is not None:
                merge_mode = "raw"
                record_merged(merged_cov)
                metadata.append(f"Merged Coverage: {merged_cov.summary_str()}")
                coverage["merged"] = self._metrics_payload(merged_cov)
                record_merge_failure(merged_cov)
                if merged_cov.lcov_path is not None:
                    metadata.append(f"Merged LCOV: {merged_cov.lcov_path}")
                    if not coverage_coverview:
                        records = self._dir_summary_records(
                            merged_cov.lcov_path, dir_summary_paths
                        )
                        metadata.extend(self._dir_summary_lines(records))
                        coverage["dir_summary"] = records
                if merged_cov.html_dir is not None:
                    metadata.append(f"Merged HTML: {merged_cov.html_dir}")
            if coverage_coverview:
                merged_info = self.merge_info_process(
                    suite_results,
                    outdir=outdir,
                    suite_name=suite_name,
                    html_output=False,
                    coverview_output=True,
                    source_roots=source_roots,
                )
                if merged_info is not None:
                    (
                        info_metrics,
                        coverview_zip,
                        merged_dataset_files,
                        description_files,
                    ) = merged_info
                    dataset_files = merged_dataset_files
                    merged_paths["desc"] = description_files.get("line")
                    # This merge overwrites the raw merge's `coverage_merged.info`.
                    if info_metrics.lcov_path is not None:
                        merged_paths["info"] = info_metrics.lcov_path
                    records = self._dir_summary_records_from_dataset_files(
                        merged_dataset_files, dir_summary_paths
                    )
                    metadata.extend(self._dir_summary_lines(records))
                    coverage["dir_summary"] = records
                    if coverview_zip is not None:
                        coverview_paths["zip"] = coverview_zip
                        metadata.append(f"Merged Coverview: {coverview_zip}")
                elif merged_cov is not None and merged_cov.lcov_path is not None:
                    # Coverview unavailable; fall back to the LCOV-based summary.
                    records = self._dir_summary_records(
                        merged_cov.lcov_path, dir_summary_paths
                    )
                    metadata.extend(self._dir_summary_lines(records))
                    coverage["dir_summary"] = records
            if coverage_coverview and coverage_per_test and reg_results is not None:
                cv = self.generate_per_test_coverview(
                    reg_results,
                    outdir=outdir,
                    suite_name=suite_name,
                    source_roots=source_roots,
                )
                if cv is not None and cv.zip_path is not None:
                    coverview_paths["per_test_zip"] = cv.zip_path
                    metadata.append(f"Per-Test Coverview: {cv.zip_path}")
        elif coverage_merge_info_process:
            merged_info = self.merge_info_process(
                suite_results,
                outdir=outdir,
                suite_name=suite_name,
                html_output=coverage_html,
                coverview_output=coverage_coverview,
                source_roots=source_roots,
            )
            if merged_info is not None:
                (
                    merged_cov,
                    coverview_zip,
                    merged_dataset_files,
                    description_files,
                ) = merged_info
                merge_mode = "info_process"
                dataset_files = merged_dataset_files
                record_merged(merged_cov)
                merged_paths["desc"] = description_files.get("line")
                metadata.append(f"Merged Coverage: {merged_cov.summary_str()}")
                coverage["merged"] = self._metrics_payload(merged_cov)
                if merged_cov.lcov_path is not None:
                    metadata.append(f"Merged LCOV: {merged_cov.lcov_path}")
                    if coverage_coverview:
                        records = self._dir_summary_records_from_dataset_files(
                            merged_dataset_files, dir_summary_paths
                        )
                    else:
                        records = self._dir_summary_records(
                            merged_cov.lcov_path, dir_summary_paths
                        )
                    metadata.extend(self._dir_summary_lines(records))
                    coverage["dir_summary"] = records
                if merged_cov.html_dir is not None:
                    metadata.append(f"Merged HTML: {merged_cov.html_dir}")
                if coverview_zip is not None:
                    coverview_paths["zip"] = coverview_zip
                    metadata.append(f"Merged Coverview: {coverview_zip}")
        elif coverage_html or coverage_coverview or dir_summary_paths:
            if coverage_coverview and coverage_per_test and reg_results is not None:
                cv = self.generate_per_test_coverview(
                    reg_results,
                    outdir=outdir,
                    suite_name=suite_name,
                    source_roots=source_roots,
                )
                if cv is not None and cv.zip_path is not None:
                    coverview_paths["per_test_zip"] = cv.zip_path
                    metadata.append(f"Per-Test Coverview: {cv.zip_path}")
            html_reports = self.generate_per_test_artifacts(
                suite_results,
                outdir=outdir,
                suite_name=suite_name,
                html_output=coverage_html,
                coverview_output=(coverage_coverview and not coverage_per_test),
                source_roots=source_roots,
            )
            for test_name_i, metrics, coverview_zip in html_reports:
                if metrics.lcov_path is not None:
                    metadata.append(f"Coverage LCOV {test_name_i}: {metrics.lcov_path}")
                    for line in self._dir_summary_metadata(
                        metrics.lcov_path, dir_summary_paths
                    ):
                        metadata.append(f"{test_name_i} {line}")
                if metrics.html_dir is not None:
                    metadata.append(f"Coverage HTML {test_name_i}: {metrics.html_dir}")
                if coverview_zip is not None:
                    metadata.append(
                        f"Coverage Coverview {test_name_i}: {coverview_zip}"
                    )

        # One model serves both the artefacts and the source-point summary.
        model = self.build_run_model(
            suite_results,
            outdir=outdir,
            suite_name=suite_name,
            source_roots=source_roots,
            merged_info=merged_paths["info"],
            model_mode=model_mode,
        )
        artefacts = self.write_artefacts(
            suite_results,
            outdir=outdir,
            suite_name=suite_name,
            command=command,
            source_roots=source_roots,
            merge_mode=merge_mode,
            merged=merged_paths,
            datasets=dataset_files,
            descriptions=description_files,
            coverview=coverview_paths,
            merge_failed=coverage["merge_failed"],
            failed_metrics=coverage["failed_metrics"],
            model=model,
            model_mode=model_mode,
        )
        if artefacts is not None:
            coverage["artefacts"] = artefacts
            metadata.append(f"Coverage manifest: {artefacts['manifest']}")
        if source_summary:
            record = self._source_summary_record(model)
            if record is None:
                # No model to collapse; say so rather than report zero coverage.
                metadata.append(
                    "Coverage source points: unavailable (no coverage model)"
                )
            else:
                coverage["source_summary"] = record
                metadata.extend(self._source_summary_lines(record))
        return metadata, coverage
