# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
coverview module handles packaging LCOV outputs into Coverview archives
"""
import json
import logging
logger = logging.getLogger(__name__)

import getpass
import os
import shutil
import subprocess
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from ..logging_utils import log_event


@dataclass
class CoverviewArtifacts:
  line_info: str | None = None
  branch_info: str | None = None
  toggle_info: str | None = None
  expression_info: str | None = None
  zip_path: str | None = None
  dataset_files: dict | None = None


class CoverviewPacker:
  """
  Package LCOV `.info` coverage into a Coverview-compatible zip archive.
  """

  def __init__(self, cfg, project_root: str):
    self.cfg = cfg
    self.project_root = project_root

  def is_supported(self) -> bool:
    """
    Return whether Coverview packaging is configured for this simulator family.
    """
    return self.cfg is not None

  def _sanitize_dataset_name(self, dataset_name: str) -> str:
    """
    Return a Coverview-compatible dataset identifier matching `\\w+`.
    """
    sanitized = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in dataset_name)
    return sanitized

  def _write_config_json(self, outdir: str, config_name: str, config: dict) -> str:
    """
    Materialize the configured inline Coverview values into a JSON file for info-process.
    """
    config_path = os.path.join(outdir, f"{config_name}.config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
      json.dump(config, fh, indent=2)
      fh.write("\n")
    return config_path

  def _get_git_metadata(self) -> tuple[str | None, str | None]:
    """
    Return the current git branch and commit hash for the project root, if available.
    """
    try:
      branch = subprocess.run(
        ["git", "-C", self.project_root, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
      ).stdout.strip()
      commit = subprocess.run(
        ["git", "-C", self.project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
      ).stdout.strip()
      return branch, commit
    except Exception:
      return None, None

  def _build_config(self, dataset_name: str, metadata: dict | None) -> dict:
    """
    Merge configured Coverview values with runtime metadata fields supported by Coverview.
    """
    config = dict(self.cfg.get_config())
    branch, commit = self._get_git_metadata()
    config.setdefault("repo", os.path.basename(self.project_root))
    if branch is not None:
      config.setdefault("branch", branch)
    if commit is not None:
      config.setdefault("commit", commit)
    config.setdefault("timestamp", datetime.now().astimezone().isoformat())

    additional = dict(config.get("additional", {}))
    additional.setdefault("dataset", dataset_name)
    additional.setdefault("user", getpass.getuser())
    if metadata is not None:
      for key, value in metadata.items():
        if value is not None:
          additional[key] = value
    config["additional"] = additional

    suite_path = additional.get("suite")
    if suite_path:
      config["title"] = f"rtl-buddy {os.path.basename(self.project_root)}/{suite_path}"

    return config

  def _run_info_process(self, run_cmd, event_prefix: str, **event_fields):
    """
    Run an info-process command and emit structured logs.
    """
    log_event(
      logger,
      logging.INFO,
      f"{event_prefix}.start",
      command=" ".join(run_cmd),
      **event_fields,
    )
    result = subprocess.run(run_cmd, capture_output=True, text=True)
    if result.returncode != 0:
      log_event(
        logger,
        logging.ERROR,
        f"{event_prefix}.failed",
        returncode=result.returncode,
        stderr=result.stderr.strip(),
        stdout=result.stdout.strip(),
        **event_fields,
      )
      return None
    return result

  def _get_info_process(self):
    """
    Resolve the info-process executable from PATH or the active virtual environment.
    """
    info_process = shutil.which("info-process")
    if info_process is None:
      candidate = os.path.join(os.path.dirname(sys.executable), "info-process")
      if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        info_process = candidate
    if info_process is None:
      log_event(logger, logging.WARNING, "coverview.tool_missing", executable="info-process")
      return None
    return info_process

  def merge_infos(self, input_paths: list[str], output_path: str, test_list_path: str | None = None) -> str | None:
    """
    Merge multiple `.info` files with `info-process merge`.
    """
    if len(input_paths) == 0:
      return None

    info_process = self._get_info_process()
    if info_process is None:
      return None

    run_cmd = [
      info_process,
      "merge",
      "--output",
      output_path,
    ]
    if test_list_path is not None:
      run_cmd.extend(["--test-list", test_list_path])
    run_cmd.extend(input_paths)

    result = self._run_info_process(
      run_cmd,
      "coverview.merge_info",
      output=output_path,
      inputs=input_paths,
      test_list=test_list_path,
    )
    if result is None or not os.path.exists(output_path):
      return None

    self._rewrite_sf_relative_to_project_root(output_path)
    if test_list_path is not None and os.path.exists(test_list_path):
      self._rewrite_desc_relative_to_project_root(test_list_path)
    return output_path

  def _rewrite_desc_relative_to_project_root(self, desc_path: str) -> None:
    """
    Rewrite `.desc` `SN:` entries from absolute project-root paths to project-relative paths.
    """
    project_root = Path(self.project_root).resolve()
    rewritten = []
    with open(desc_path, "r", encoding="utf-8") as fh:
      for line in fh:
        if line.startswith("SN:"):
          source_path = Path(line[3:].strip())
          try:
            if source_path.is_absolute():
              source_path = source_path.resolve()
            else:
              source_path = (project_root / source_path).resolve()
            rel_path = source_path.relative_to(project_root)
            rewritten.append(f"SN:{rel_path.as_posix()}\n")
            continue
          except Exception:
            pass
        rewritten.append(line)
    with open(desc_path, "w", encoding="utf-8") as fh:
      fh.writelines(rewritten)

  def _write_single_test_desc(self, info_path: str, outdir: str, desc_name: str, test_name: str) -> str | None:
    """
    Synthesize a single-test `.desc` file from line coverage data so Coverview can
    show test-origin hover details for per-test archives.
    """
    desc_path = os.path.join(outdir, desc_name)
    current_source = None
    hit_lines = []
    desc_lines = []

    try:
      with open(info_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
          line = raw_line.strip()
          if line.startswith("SF:"):
            if current_source is not None and hit_lines:
              desc_lines.append(f"SN:{current_source}\n")
              for line_no in hit_lines:
                desc_lines.append(f"TEST:{line_no},{test_name}\n")
              desc_lines.append("end_of_record\n")
            current_source = line[3:]
            hit_lines = []
          elif line.startswith("DA:"):
            payload = line[3:].split(",")
            if len(payload) >= 2:
              try:
                line_no = int(payload[0])
                hit_count = int(payload[1])
              except ValueError:
                continue
              if hit_count > 0:
                hit_lines.append(line_no)

      if current_source is not None and hit_lines:
        desc_lines.append(f"SN:{current_source}\n")
        for line_no in hit_lines:
          desc_lines.append(f"TEST:{line_no},{test_name}\n")
        desc_lines.append("end_of_record\n")

      if len(desc_lines) == 0:
        return None

      with open(desc_path, "w", encoding="utf-8") as fh:
        fh.writelines(desc_lines)
      self._rewrite_desc_relative_to_project_root(desc_path)
      return desc_path
    except OSError:
      return None

  def _build_covrby_coverview_metadata(self, rby_description_files: dict[str, str | None] | None) -> dict | None:
    """
    Build local Coverview extension metadata for extra per-type provenance files.
    """
    if not rby_description_files:
      return None

    payload = {}
    for coverage_type, path in rby_description_files.items():
      if path is None:
        continue
      payload[f"{coverage_type}_desc"] = os.path.basename(path)
    return payload or None

  def _extract_typed_info(self, info_process: str, info_path: str, output_path: str, coverage_type: str):
    """
    Extract a single typed LCOV file from a combined LCOV input.
    """
    run_cmd = [
      info_process,
      "extract",
      "--coverage-type",
      coverage_type,
      "--output",
      output_path,
      info_path,
    ]
    result = self._run_info_process(
      run_cmd,
      "coverview.extract",
      coverage_type=coverage_type,
      input=info_path,
      output=output_path,
    )
    if result is None:
      return None
    self._rewrite_sf_relative_to_project_root(output_path)
    return output_path

  def _write_filtered_dat(self, raw_path: str, output_path: str, *, record_type: str, output_name: str) -> str | None:
    """
    Filter a raw Verilator coverage database down to the header plus records of one type.
    """
    record_markers = [f"\x01t\x02{record_type}"]
    if record_type == "expression":
      record_markers.append("\x01t\x02expr")
    try:
      with open(raw_path, "r", encoding="utf-8", errors="ignore") as src:
        lines = src.readlines()
      with open(output_path, "w", encoding="utf-8") as out:
        for idx, line in enumerate(lines):
          if idx == 0 or any(marker in line for marker in record_markers):
            out.write(line)
    except OSError as e:
      log_event(
        logger,
        logging.ERROR,
        f"coverview.{output_name}_filter.failed",
        raw_path=raw_path,
        output_path=output_path,
        error=str(e),
      )
      return None
    return output_path

  def _write_raw_metric_info(self, raw_path: str, outdir: str, dataset_name: str, *,
                             metric_name: str, record_type: str) -> str | None:
    """
    Convert one raw Verilator coverage metric into an LCOV-like `.info` file by filtering the
    raw database to one record type and running `verilator_coverage --write-info`.
    """
    metric_dat = os.path.join(outdir, f"coverage_{metric_name}_{dataset_name}.dat")
    metric_info = os.path.join(outdir, f"coverage_{metric_name}_{dataset_name}.info")
    if self._write_filtered_dat(
      raw_path,
      metric_dat,
      record_type=record_type,
      output_name=metric_name,
    ) is None:
      return None

    run_cmd = ["verilator_coverage", "--write-info", metric_info, metric_dat]
    result = self._run_info_process(
      run_cmd,
      f"coverview.{metric_name}_info",
      raw_path=raw_path,
      metric_dat=metric_dat,
      metric_info=metric_info,
    )
    if result is None or not os.path.exists(metric_info):
      return None
    raw_dir = Path(os.path.dirname(raw_path)).resolve()
    metric_source_roots = [raw_dir]
    if raw_dir.name == "logs":
      metric_source_roots.append(raw_dir.parent)
    self._rewrite_sf_relative_to_project_root(
      metric_info,
      base_dir=os.path.dirname(raw_path),
      source_roots=[str(root) for root in metric_source_roots],
    )
    return metric_info

  def _write_toggle_info(self, raw_path: str, outdir: str, dataset_name: str) -> str | None:
    """
    Convert raw Verilator toggle coverage into an LCOV-like `.info` file.
    """
    return self._write_raw_metric_info(
      raw_path,
      outdir,
      dataset_name,
      metric_name="toggle",
      record_type="toggle",
    )

  def write_toggle_info(self, raw_path: str, outdir: str, dataset_name: str) -> str | None:
    """
    Public wrapper for generating toggle `.info` from a raw coverage database.
    """
    dataset_name = self._sanitize_dataset_name(dataset_name)
    return self._write_toggle_info(raw_path, outdir, dataset_name)

  def _write_expression_info(self, raw_path: str, outdir: str, dataset_name: str) -> str | None:
    """
    Convert raw Verilator expression coverage into an LCOV-like `.info` file.
    """
    return self._write_raw_metric_info(
      raw_path,
      outdir,
      dataset_name,
      metric_name="expression",
      record_type="expression",
    )

  def write_expression_info(self, raw_path: str, outdir: str, dataset_name: str) -> str | None:
    """
    Public wrapper for generating expression `.info` from a raw coverage database.
    """
    dataset_name = self._sanitize_dataset_name(dataset_name)
    return self._write_expression_info(raw_path, outdir, dataset_name)

  def _rewrite_sf_relative_to_project_root(self, info_path: str, base_dir: str | None = None,
                                           source_roots: list[str] | None = None) -> None:
    """
    Rewrite LCOV `SF:` entries from absolute project-root paths to project-relative paths.
    """
    project_root = Path(self.project_root).resolve()
    resolved_base = None if base_dir is None else Path(base_dir).resolve()
    resolved_source_roots = [] if source_roots is None else [Path(root).resolve() for root in source_roots if root is not None]
    rewritten = []
    with open(info_path, "r", encoding="utf-8") as fh:
      for line in fh:
        if line.startswith("SF:"):
          sf_path = Path(line[3:].strip())
          try:
            if not sf_path.is_absolute() and resolved_base is not None:
              candidate = (resolved_base / sf_path).resolve()
              if candidate.exists():
                try:
                  candidate.relative_to(project_root)
                  sf_path = candidate
                except Exception:
                  candidate = None
              else:
                candidate = None
              if candidate is None:
                parts = [part for part in sf_path.as_posix().split("/") if part not in ("", ".", "..")]
                repo_candidate = (project_root / Path(*parts)).resolve()
                basename = parts[-1] if parts else sf_path.name

                search_roots = list(resolved_source_roots)
                if project_root not in search_roots:
                  search_roots.append(project_root)
                source_root_matches = []
                matches = []
                for search_root in search_roots:
                  for match in search_root.rglob(basename):
                    if not match.is_file():
                      continue
                    match = match.resolve()
                    matches.append(match)
                    if search_root in resolved_source_roots:
                      source_root_matches.append(match)

                deduped_source_root_matches = []
                seen_source_matches = set()
                for match in source_root_matches:
                  key = str(match)
                  if key not in seen_source_matches:
                    seen_source_matches.add(key)
                    deduped_source_root_matches.append(match)

                if len(parts) > 0:
                  suffix = "/" + "/".join(parts)
                  suffix_source_matches = [
                    match for match in deduped_source_root_matches
                    if str(match).replace("\\", "/").endswith(suffix)
                  ]
                  if len(suffix_source_matches) == 1:
                    sf_path = suffix_source_matches[0]
                  elif len(deduped_source_root_matches) == 1:
                    sf_path = deduped_source_root_matches[0]
                  elif repo_candidate.exists():
                    sf_path = repo_candidate
                  else:
                    suffix_matches = [m for m in matches if str(m).replace("\\", "/").endswith(suffix)]
                    if len(suffix_matches) == 1:
                      sf_path = suffix_matches[0]
                    elif len(matches) == 1:
                      sf_path = matches[0]
                    else:
                      sf_path = repo_candidate
                elif len(deduped_source_root_matches) == 1:
                  sf_path = deduped_source_root_matches[0]
                elif len(matches) == 1:
                  sf_path = matches[0]
                else:
                  sf_path = repo_candidate
            elif not sf_path.is_absolute():
              repo_candidate = (project_root / sf_path).resolve()
              if repo_candidate.exists():
                sf_path = repo_candidate
              else:
                sf_path = repo_candidate
            else:
              sf_path = sf_path.resolve()
            rel_path = sf_path.relative_to(project_root)
            rewritten.append(f"SF:{rel_path.as_posix()}\n")
            continue
          except Exception:
            pass
        rewritten.append(line)
    with open(info_path, "w", encoding="utf-8") as fh:
      fh.writelines(rewritten)

  def package_info(self, info_path: str, outdir: str, dataset_name: str, zip_name: str, metadata: dict | None = None,
                   raw_path: str | None = None, zip_outdir: str | None = None):
    """
    Split a combined LCOV file into typed inputs and pack them into a Coverview archive.
    """
    if not self.is_supported() or not os.path.exists(info_path):
      return None

    info_process = self._get_info_process()
    if info_process is None:
      return None

    dataset_name = self._sanitize_dataset_name(dataset_name)
    line_info = os.path.join(outdir, f"coverage_line_{dataset_name}.info")
    branch_info = os.path.join(outdir, f"coverage_branch_{dataset_name}.info")
    toggle_info = None
    expression_info = None
    line_desc = None
    rby_descs = {}

    for coverage_type, output_path in [("line", line_info), ("branch", branch_info)]:
      if self._extract_typed_info(info_process, info_path, output_path, coverage_type) is None:
        return None
    if raw_path is not None and os.path.exists(raw_path):
      toggle_info = self._write_toggle_info(raw_path, outdir, dataset_name)
      expression_info = self._write_expression_info(raw_path, outdir, dataset_name)

    test_name = None if metadata is None else metadata.get("test")
    if test_name is not None:
      line_desc = self._write_single_test_desc(line_info, outdir, f"coverage_{dataset_name}.desc", test_name)
      rby_descs["branch"] = self._write_single_test_desc(branch_info, outdir, f"covrby_branch_{dataset_name}.desc", test_name)
      if expression_info is not None:
        rby_descs["expression"] = self._write_single_test_desc(expression_info, outdir, f"covrby_expression_{dataset_name}.desc", test_name)
      if toggle_info is not None:
        rby_descs["toggle"] = self._write_single_test_desc(toggle_info, outdir, f"covrby_toggle_{dataset_name}.desc", test_name)

    packaged = self.package_dataset_files(
      dataset_name=dataset_name,
      dataset_files={
        "line": line_info,
        "branch": branch_info,
        "expression": expression_info,
        "toggle": toggle_info,
      },
      outdir=outdir,
      zip_name=zip_name,
      metadata=metadata,
      description_files={
        "line": line_desc,
      },
      rby_description_files=rby_descs,
      zip_outdir=zip_outdir,
    )
    if packaged is None:
      return None
    packaged.line_info = line_info
    packaged.branch_info = branch_info
    packaged.expression_info = expression_info
    packaged.toggle_info = toggle_info
    return packaged

  def package_dataset_files(self, dataset_name: str, dataset_files: dict[str, str | None], outdir: str, zip_name: str,
                            metadata: dict | None = None, description_files: dict[str, str | None] | None = None,
                            rby_description_files: dict[str, str | None] | None = None, zip_outdir: str | None = None):
    """
    Package an explicit set of typed coverage files into one Coverview archive.
    """
    if not self.is_supported():
      return None

    info_process = self._get_info_process()
    if info_process is None:
      return None

    dataset_name = self._sanitize_dataset_name(dataset_name)
    datasets_cfg = {
      dataset_name: {}
    }
    coverage_files = []
    desc_files = description_files or {}
    description_paths = []
    for coverage_type in ("line", "branch", "expression", "toggle"):
      path = dataset_files.get(coverage_type)
      if path is None:
        continue
      desc_path = desc_files.get(coverage_type)
      if desc_path is not None:
        datasets_cfg[dataset_name][coverage_type] = [os.path.basename(path), os.path.basename(desc_path)]
        description_paths.append(desc_path)
      else:
        datasets_cfg[dataset_name][coverage_type] = os.path.basename(path)
      coverage_files.append(path)

    if len(coverage_files) == 0:
      return None

    config = self._build_config(dataset_name, metadata)
    rby_metadata = self._build_covrby_coverview_metadata(rby_description_files)
    if rby_metadata is not None:
      config["additional"]["covrby_coverview"] = rby_metadata
    config["datasets"] = datasets_cfg
    config_name = os.path.splitext(os.path.basename(zip_name))[0]
    config_path = self._write_config_json(outdir, config_name, config)
    zip_base_dir = outdir if zip_outdir is None else zip_outdir
    zip_path = os.path.join(zip_base_dir, zip_name)
    extra_files = [path for path in (rby_description_files or {}).values() if path is not None]

    run_cmd = [
      info_process,
      "pack",
      "--output",
      zip_path,
      "--config",
      config_path,
      "--coverage-files",
      *coverage_files,
      "--sources-root",
      self.project_root,
    ]
    if len(description_paths) > 0:
      run_cmd.extend(["--description-files", *description_paths])
    if len(extra_files) > 0:
      run_cmd.extend(["--extra-files", *extra_files])
    if self.cfg.get_generate_tables() is not None:
      run_cmd.extend(["--generate-tables", self.cfg.get_generate_tables()])

    result = self._run_info_process(
      run_cmd,
      "coverview.pack",
      input="explicit",
      zip_path=zip_path,
      config=config_path,
    )
    if result is None:
      return None

    return CoverviewArtifacts(
      line_info=dataset_files.get("line"),
      branch_info=dataset_files.get("branch"),
      expression_info=dataset_files.get("expression"),
      toggle_info=dataset_files.get("toggle"),
      zip_path=zip_path,
      dataset_files={dataset_name: dataset_files},
    )

  def package_infos(self, info_inputs: list[dict], outdir: str, dataset_name: str, zip_name: str, metadata: dict | None = None,
                    zip_outdir: str | None = None):
    """
    Package multiple combined LCOV inputs into one Coverview archive with one dataset per input.
    """
    if not self.is_supported() or len(info_inputs) == 0:
      return None

    info_process = self._get_info_process()
    if info_process is None:
      return None

    archive_dataset = self._sanitize_dataset_name(dataset_name)
    dataset_files = {}
    datasets_cfg = {}
    description_paths = []
    extra_files = []
    rby_dataset_metadata = {}
    for item in info_inputs:
      input_path = item["info_path"]
      raw_path = item.get("raw_path")
      test_name = item.get("test_name")
      test_dataset = self._sanitize_dataset_name(item["dataset_name"])
      line_info = os.path.join(outdir, f"coverage_line_{test_dataset}.info")
      branch_info = os.path.join(outdir, f"coverage_branch_{test_dataset}.info")
      toggle_info = None
      expression_info = None
      line_desc = None
      rby_descs = {}
      if self._extract_typed_info(info_process, input_path, line_info, "line") is None:
        return None
      if self._extract_typed_info(info_process, input_path, branch_info, "branch") is None:
        return None
      if raw_path is not None and os.path.exists(raw_path):
        toggle_info = self._write_toggle_info(raw_path, outdir, test_dataset)
        expression_info = self._write_expression_info(raw_path, outdir, test_dataset)
      if test_name is not None:
        line_desc = self._write_single_test_desc(line_info, outdir, f"coverage_{test_dataset}.desc", test_name)
        rby_descs["branch"] = self._write_single_test_desc(branch_info, outdir, f"covrby_branch_{test_dataset}.desc", test_name)
        if expression_info is not None:
          rby_descs["expression"] = self._write_single_test_desc(expression_info, outdir, f"covrby_expression_{test_dataset}.desc", test_name)
        if toggle_info is not None:
          rby_descs["toggle"] = self._write_single_test_desc(toggle_info, outdir, f"covrby_toggle_{test_dataset}.desc", test_name)
      dataset_files[test_dataset] = {
        "line": line_info,
        "branch": branch_info,
        "toggle": toggle_info,
        "expression": expression_info,
      }
      datasets_cfg[test_dataset] = {
        "line": [os.path.basename(line_info), os.path.basename(line_desc)] if line_desc is not None else os.path.basename(line_info),
        "branch": os.path.basename(branch_info),
      }
      if expression_info is not None:
        datasets_cfg[test_dataset]["expression"] = os.path.basename(expression_info)
      if toggle_info is not None:
        datasets_cfg[test_dataset]["toggle"] = os.path.basename(toggle_info)
      if line_desc is not None:
        description_paths.append(line_desc)
      extra_files.extend(path for path in rby_descs.values() if path is not None)
      rby_metadata = self._build_covrby_coverview_metadata(rby_descs)
      if rby_metadata is not None:
        rby_dataset_metadata[test_dataset] = rby_metadata

    config = self._build_config(archive_dataset, metadata)
    if len(rby_dataset_metadata) > 0:
      config["additional"]["covrby_coverview"] = rby_dataset_metadata
    config["datasets"] = datasets_cfg
    config_name = os.path.splitext(os.path.basename(zip_name))[0]
    config_path = self._write_config_json(outdir, config_name, config)
    zip_base_dir = outdir if zip_outdir is None else zip_outdir
    zip_path = os.path.join(zip_base_dir, zip_name)

    coverage_files = []
    for files in dataset_files.values():
      coverage_files.extend([files["line"], files["branch"]])
      if files.get("toggle") is not None:
        coverage_files.append(files["toggle"])
      if files.get("expression") is not None:
        coverage_files.append(files["expression"])

    run_cmd = [
      info_process,
      "pack",
      "--output",
      zip_path,
      "--config",
      config_path,
      "--coverage-files",
      *coverage_files,
      "--sources-root",
      self.project_root,
    ]
    if len(description_paths) > 0:
      run_cmd.extend(["--description-files", *description_paths])
    if len(extra_files) > 0:
      run_cmd.extend(["--extra-files", *extra_files])
    if self.cfg.get_generate_tables() is not None:
      run_cmd.extend(["--generate-tables", self.cfg.get_generate_tables()])

    result = self._run_info_process(
      run_cmd,
      "coverview.pack",
      input="multiple",
      zip_path=zip_path,
      config=config_path,
    )
    if result is None:
      return None

    log_event(
      logger,
      logging.INFO,
      "coverview.pack.completed",
      input="multiple",
      zip_path=zip_path,
      dataset_count=len(dataset_files),
    )
    return CoverviewArtifacts(
      zip_path=zip_path,
      dataset_files=dataset_files,
    )
