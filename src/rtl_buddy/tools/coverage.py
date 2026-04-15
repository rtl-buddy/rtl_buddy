# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 Loh Zhi-Hern, Xie Jiacheng, Emil Goh, Damien Lim, Chai Wei Lynthia, Zubin Jain
#
"""
coverage module handles rtl-buddy coverage result orchestration
"""

import os

from .coverview import CoverviewPacker
from .vlog_cov import CoverageMetrics, VlogCov


class CoverageReporter:
  """
  Orchestrate per-test and merged coverage reporting for rtl-buddy summaries.
  """

  def __init__(self, root_cfg):
    """
    Build a coverage reporter for the currently selected builder.
    """
    self.root_cfg = root_cfg

  def _get_cov_tool(self):
    """
    Create a `VlogCov` helper for the active simulator family.
    """
    simulator_family = self.root_cfg.get_rtl_builder_cfg().get_simulator_family()
    return VlogCov(
      simulator_name=simulator_family,
      use_lcov=self.root_cfg.get_use_lcov(simulator_family),
      root_cfg=self.root_cfg,
    )

  def _get_coverview_tool(self):
    """
    Create a `CoverviewPacker` helper for the active simulator family.
    """
    simulator_family = self.root_cfg.get_rtl_builder_cfg().get_simulator_family()
    return CoverviewPacker(
      cfg=self.root_cfg.get_coverview_cfg(simulator_family),
      project_root=self.root_cfg.get_project_rootdir(),
    )

  def _coverview_dataset_name(self, suite_name: str) -> str:
    """
    Derive a stable merged Coverview dataset name from a suite/regression name.
    """
    dataset = os.path.splitext(os.path.basename(suite_name))[0]
    if dataset.endswith("_regression"):
      dataset = dataset[:-len("_regression")]
    return dataset

  def format_summary(self, test_results):
    """
    Return the one-line coverage summary string for a single test result.
    """
    coverage = test_results.results.get("coverage")
    if coverage is None:
      return None
    return coverage.get("summary")

  def collect_paths(self, suite_results):
    """
    Collect raw coverage database paths from a list of suite results.
    """
    raw_paths = []
    for suite_result in suite_results:
      coverage = suite_result['results'].results.get("coverage")
      if coverage is None:
        continue
      raw_paths.extend(coverage.get("raw_paths", []))
    return raw_paths

  def _normalize_source_roots(self, outdir, source_roots=None, suite_name=None):
    """
    Return resolved source roots, adding the suite directory when available.
    """
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
    """
    Return the intermediate coverage artifact directory under the command output directory.
    """
    cov_dir = os.path.join(outdir, "cov_dir")
    os.makedirs(cov_dir, exist_ok=True)
    return cov_dir

  def resolve_dir_summary_paths(self, dir_summary_paths=None, dir_summary_file=None):
    """
    Resolve a deduplicated list of repo-relative directory prefixes from
    repeated CLI args and/or a file containing one path per line.
    """
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

  def _dir_summary_metadata(self, lcov_path, dir_summary_paths):
    """
    Build summary lines for repo-relative directory prefixes from an LCOV file.
    """
    if lcov_path is None or not os.path.exists(lcov_path) or not dir_summary_paths:
      return []

    cov = self._get_cov_tool()
    lines = []
    for prefix in dir_summary_paths:
      metrics = CoverageMetrics()
      metrics.line, metrics.branch = cov.parse_lcov_summary_for_prefix(lcov_path, prefix)
      lines.append(f"Coverage {prefix}: {metrics.summary_str()}")
    return lines

  def _dir_summary_metadata_from_dataset_files(self, dataset_files, dir_summary_paths):
    """
    Build summary lines for repo-relative directory prefixes from typed coverage
    dataset files, including toggle when available.
    """
    if not dataset_files or not dir_summary_paths:
      return []

    cov = self._get_cov_tool()
    lines = []
    for prefix in dir_summary_paths:
      metrics = CoverageMetrics()

      line_info = dataset_files.get("line")
      if line_info is not None:
        metrics.line, _ = cov.parse_lcov_summary_for_prefix(line_info, prefix)

      branch_info = dataset_files.get("branch")
      if branch_info is not None:
        _, metrics.branch = cov.parse_lcov_summary_for_prefix(branch_info, prefix)

      toggle_info = dataset_files.get("toggle")
      if toggle_info is not None:
        _, metrics.toggle = cov.parse_lcov_summary_for_prefix(toggle_info, prefix)

      lines.append(f"Coverage {prefix}: {metrics.summary_str()}")
    return lines

  def merge(self, suite_results, outdir, basename="coverage_merged", html_output=False, source_roots=None):
    """
    Merge raw coverage files across multiple tests and return aggregate metrics.
    """
    raw_paths = self.collect_paths(suite_results)
    if len(raw_paths) == 0:
      return None
    cov_dir = self._cov_dir(outdir)
    return self._get_cov_tool().merge(
      raw_paths=raw_paths,
      outdir=cov_dir,
      merge_basename=basename,
      html_output=html_output,
      source_roots=self._normalize_source_roots(outdir, source_roots=source_roots),
      html_outdir=outdir,
    )

  def generate_unmerged_artifacts(self, suite_results, outdir, suite_name, coverview_output=False, source_roots=None):
    """
    Generate per-test LCOV and HTML artifacts for the provided suite results.
    """
    return self.generate_per_test_artifacts(
      suite_results,
      outdir=outdir,
      suite_name=suite_name,
      html_output=True,
      coverview_output=coverview_output,
      source_roots=source_roots,
    )

  def generate_per_test_artifacts(self, suite_results, *, outdir, suite_name, html_output=False, coverview_output=False, source_roots=None):
    """
    Generate per-test LCOV and optional HTML/Coverview artifacts for a suite.
    """
    cov = self._get_cov_tool()
    coverview = self._get_coverview_tool()
    cov_dir = self._cov_dir(outdir)
    source_roots = self._normalize_source_roots(outdir, source_roots=source_roots, suite_name=suite_name)
    try:
      suite_label = os.path.relpath(suite_name, outdir)
    except ValueError:
      suite_label = suite_name
    generated = []
    for suite_result in suite_results:
      coverage = suite_result['results'].results.get("coverage")
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
          safe_dataset = cov._sanitize_artifact_name(f"{suite_label}__{suite_result['test_name']}")
          cv = coverview.package_info(
            info_path=metrics.lcov_path,
            outdir=cov_dir,
            dataset_name=safe_dataset,
            zip_name=f"coverview_{safe_dataset}.zip",
            raw_path=raw_paths[0],
            zip_outdir=outdir,
            metadata={
              "suite": os.path.relpath(suite_name, self.root_cfg.get_project_rootdir()),
              "test": suite_result["test_name"],
              "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
              "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
            },
          )
          if cv is not None:
            coverview_zip = cv.zip_path
            updated["coverview_zip"] = cv.zip_path
        coverage.update(updated)
        generated.append((f"{suite_label}::{suite_result['test_name']}", metrics, coverview_zip))
    return generated

  def merge_info_process(self, suite_results, *, outdir, suite_name, html_output=False,
                         coverview_output=False, source_roots=None):
    """
    Merge per-test `.info` files with `info-process merge` and optionally emit HTML/Coverview.
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
    info_inputs = [metrics.lcov_path for _, metrics, _ in generated if metrics.lcov_path is not None]
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
    metrics.line, metrics.branch = cov._parse_lcov_summary(merged_lcov)

    safe_dataset = cov._sanitize_artifact_name(self._coverview_dataset_name(suite_name))
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
    if coverview._extract_typed_info(coverview._get_info_process(), merged_lcov, line_info_path, "line") is not None:
      merged_dataset_files["line"] = line_info_path

    toggle_inputs = []
    expression_inputs = []
    branch_inputs = []
    for test_name_i, metrics_i, _ in generated:
      if metrics_i.lcov_path is not None:
        artifact_stem = cov._sanitize_artifact_name(test_name_i.replace("::", "__"))
        branch_info = os.path.join(cov_dir, f"coverage_branch_{artifact_stem}.info")
        if coverview._extract_typed_info(coverview._get_info_process(), metrics_i.lcov_path, branch_info, "branch") is not None:
          branch_inputs.append(branch_info)
      raw_paths = metrics_i.raw_paths or []
      if len(raw_paths) == 0:
        continue
      artifact_stem = cov._sanitize_artifact_name(test_name_i.replace("::", "__"))
      toggle_info = coverview.write_toggle_info(raw_paths[0], cov_dir, artifact_stem)
      if toggle_info is not None:
        toggle_inputs.append(toggle_info)
      expression_info = coverview.write_expression_info(raw_paths[0], cov_dir, artifact_stem)
      if expression_info is not None:
        expression_inputs.append(expression_info)

    if len(branch_inputs) > 0:
      merged_branch_path = os.path.join(cov_dir, f"coverage_branch_{safe_dataset}.info")
      merged_branch_desc = os.path.join(cov_dir, f"covrby_branch_{safe_dataset}.desc")
      merged_branch = coverview.merge_infos(branch_inputs, output_path=merged_branch_path, test_list_path=merged_branch_desc)
      if merged_branch is not None:
        merged_dataset_files["branch"] = merged_branch
        metrics.branch = cov._parse_lcov_summary(merged_branch)[1]
        if os.path.exists(merged_branch_desc):
          rby_description_files["branch"] = merged_branch_desc
    elif coverview._extract_typed_info(coverview._get_info_process(), merged_lcov, branch_info_path, "branch") is not None:
      merged_dataset_files["branch"] = branch_info_path

    if len(toggle_inputs) > 0:
      merged_toggle_path = os.path.join(cov_dir, f"coverage_toggle_{safe_dataset}.info")
      merged_toggle_desc = os.path.join(cov_dir, f"covrby_toggle_{safe_dataset}.desc")
      merged_toggle = coverview.merge_infos(toggle_inputs, output_path=merged_toggle_path, test_list_path=merged_toggle_desc)
      if merged_toggle is not None:
        merged_dataset_files["toggle"] = merged_toggle
        metrics.toggle, _ = cov._parse_lcov_summary(merged_toggle)
        if os.path.exists(merged_toggle_desc):
          rby_description_files["toggle"] = merged_toggle_desc

    if len(expression_inputs) > 0:
      merged_expression_path = os.path.join(cov_dir, f"coverage_expression_{safe_dataset}.info")
      merged_expression_desc = os.path.join(cov_dir, f"covrby_expression_{safe_dataset}.desc")
      merged_expression = coverview.merge_infos(expression_inputs, output_path=merged_expression_path, test_list_path=merged_expression_desc)
      if merged_expression is not None:
        merged_dataset_files["expression"] = merged_expression
        if os.path.exists(merged_expression_desc):
          rby_description_files["expression"] = merged_expression_desc

    if html_output:
      metrics.html_dir = cov.generate_html(merged_lcov, outdir=cov_dir, html_dirname="coverage_merge.html", html_outdir=outdir)

    coverview_zip = None
    if coverview_output:
      cv = coverview.package_dataset_files(
        dataset_name=safe_dataset,
        dataset_files=merged_dataset_files,
        outdir=cov_dir,
        zip_name=f"coverview_{safe_dataset}.zip",
        description_files={
          "line": merged_test_list if os.path.exists(merged_test_list) else None,
        },
        rby_description_files=rby_description_files,
        zip_outdir=outdir,
        metadata={
          "suite": os.path.relpath(suite_name, self.root_cfg.get_project_rootdir()),
          "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
          "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
          "merged": True,
          "merge_mode": "info_process",
        },
      )
      if cv is not None:
        coverview_zip = cv.zip_path

    return metrics, coverview_zip, merged_dataset_files

  def generate_per_test_coverview(self, reg_results, *, outdir, suite_name, source_roots=None):
    """
    Generate one Coverview archive containing one dataset per test result.
    """
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
        info_inputs.append({
          "info_path": metrics.lcov_path,
          "dataset_name": cov._sanitize_artifact_name(artifact_stem),
          "raw_path": raw_paths[0],
          "test_name": suite_result["test_name"],
        })

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
        "suite": os.path.relpath(suite_name, self.root_cfg.get_project_rootdir()),
        "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
        "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
        "per_test": True,
      },
    )

  def build_metadata(self, suite_results, *, outdir, suite_name, coverage_merge=False, coverage_merge_raw=False,
                     coverage_html=False, coverage_coverview=False, coverage_per_test=False, reg_results=None,
                     coverage_merge_info_process=False, source_roots=None, dir_summary_paths=None):
    """
    Build summary metadata lines for merged or unmerged coverage artifacts.
    """
    metadata = []
    if coverage_merge_raw:
      merged_cov = self.merge(
        suite_results,
        outdir=outdir,
        html_output=coverage_html,
        source_roots=source_roots,
      )
      if merged_cov is not None:
        metadata.append(f"Merged Coverage: {merged_cov.summary_str()}")
        if merged_cov.lcov_path is not None:
          metadata.append(f"Merged LCOV: {merged_cov.lcov_path}")
          metadata.extend(self._dir_summary_metadata(merged_cov.lcov_path, dir_summary_paths))
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
                "suite": os.path.relpath(suite_name, self.root_cfg.get_project_rootdir()),
                "builder": self.root_cfg.get_rtl_builder_cfg().get_name(),
                "simulator_family": self.root_cfg.get_rtl_builder_cfg().get_simulator_family(),
                "merged": True,
                "merge_mode": "raw",
              },
            )
            if cv is not None and cv.zip_path is not None:
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
        metadata.append(f"Merged Coverage: {merged_cov.summary_str()}")
        if merged_cov.lcov_path is not None:
          metadata.append(f"Merged LCOV: {merged_cov.lcov_path}")
          if not coverage_coverview:
            metadata.extend(self._dir_summary_metadata(merged_cov.lcov_path, dir_summary_paths))
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
          _, coverview_zip, merged_dataset_files = merged_info
          metadata.extend(self._dir_summary_metadata_from_dataset_files(merged_dataset_files, dir_summary_paths))
          if coverview_zip is not None:
            metadata.append(f"Merged Coverview: {coverview_zip}")
      if coverage_coverview and coverage_per_test and reg_results is not None:
        cv = self.generate_per_test_coverview(
          reg_results,
          outdir=outdir,
          suite_name=suite_name,
          source_roots=source_roots,
        )
        if cv is not None and cv.zip_path is not None:
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
        merged_cov, coverview_zip, merged_dataset_files = merged_info
        metadata.append(f"Merged Coverage: {merged_cov.summary_str()}")
        if merged_cov.lcov_path is not None:
          metadata.append(f"Merged LCOV: {merged_cov.lcov_path}")
          if coverage_coverview:
            metadata.extend(self._dir_summary_metadata_from_dataset_files(merged_dataset_files, dir_summary_paths))
          else:
            metadata.extend(self._dir_summary_metadata(merged_cov.lcov_path, dir_summary_paths))
        if merged_cov.html_dir is not None:
          metadata.append(f"Merged HTML: {merged_cov.html_dir}")
        if coverview_zip is not None:
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
          for line in self._dir_summary_metadata(metrics.lcov_path, dir_summary_paths):
            metadata.append(f"{test_name_i} {line}")
        if metrics.html_dir is not None:
          metadata.append(f"Coverage HTML {test_name_i}: {metrics.html_dir}")
        if coverview_zip is not None:
          metadata.append(f"Coverage Coverview {test_name_i}: {coverview_zip}")
    return metadata
