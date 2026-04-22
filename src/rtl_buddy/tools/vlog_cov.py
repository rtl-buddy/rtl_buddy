# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
vlog_cov module handles coverage post-processing for rtl-buddy
"""
import logging
logger = logging.getLogger(__name__)

import os
import re
import subprocess
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
from collections import defaultdict

from ..config.root import RootConfig
from ..logging_utils import log_event


def _fmt_cov(value):
  """
  Format a normalized coverage value for summary output.
  """
  if value is None:
    return "UNSP"
  return f"{max(0.0, min(1.0, value)):.2f}"


@dataclass
class CoverageMetrics:
  line: float | None = None
  branch: float | None = None
  toggle: float | None = None
  functional: float | None = None
  raw_paths: list[str] | None = None
  merged_path: str | None = None
  lcov_path: str | None = None
  html_dir: str | None = None

  def summary_str(self):
    """
    Return the one-line `L/B/T/F` coverage summary string.
    """
    return (
      f"L:{_fmt_cov(self.line)} "
      f"B:{_fmt_cov(self.branch)} "
      f"T:{_fmt_cov(self.toggle)} "
      f"F:{_fmt_cov(self.functional)}"
    )

  def to_dict(self):
    """
    Serialize the metrics into the result-dict shape used by rtl-buddy.
    """
    return {
      "line": self.line,
      "branch": self.branch,
      "toggle": self.toggle,
      "functional": self.functional,
      "summary": self.summary_str(),
      "raw_paths": [] if self.raw_paths is None else list(self.raw_paths),
      "merged_path": self.merged_path,
      "lcov_path": self.lcov_path,
      "html_dir": self.html_dir,
    }


class VlogCov:
  """
  Coverage collection and merge helper.
  """

  _VERILATOR_TYPES = {
    "line": "line",
    "branch": "branch",
    "toggle": "toggle",
    "functional": "user",
  }

  def __init__(self, simulator_name, use_lcov=False, root_cfg=None):
    """
    Build a coverage helper for a simulator family.
    """
    self.simulator_name = simulator_name
    self.use_lcov = use_lcov
    self.root_cfg = root_cfg

  def _get_repo_root(self):
    """
    Return the absolute project root directory for path normalization.
    """
    if self.root_cfg is None:
      self.root_cfg = RootConfig(name="coverage")
    return Path(self.root_cfg.get_project_rootdir()).resolve()

  def _sanitize_artifact_name(self, name):
    """
    Return a filesystem-safe coverage artifact name.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)

  def _extract_raw_source_paths(self, raw_path):
    """
    Extract candidate source-file paths embedded in a raw coverage database.
    """
    decoded = []
    seen = set()

    try:
      strings_result = subprocess.run(
        ["strings", "-a", raw_path],
        capture_output=True,
        text=True,
      )
      if strings_result.returncode == 0:
        for candidate in strings_result.stdout.splitlines():
          candidate = candidate.strip()
          if re.fullmatch(r"[A-Za-z0-9_./+-]+\.s?vh?", candidate) and candidate not in seen:
            seen.add(candidate)
            decoded.append(candidate)
        if len(decoded) > 0:
          return decoded
    except OSError:
      pass

    try:
      raw_bytes = Path(raw_path).read_bytes()
    except OSError:
      return []

    path_re = re.compile(
      rb"((?:(?:(?:\.\.?/)+|/)?(?:[A-Za-z0-9_+-]+/)+[A-Za-z0-9_.+-]+\.s?vh?))"
    )
    for match in path_re.finditer(raw_bytes):
      candidate = match.group(1).decode("utf-8", errors="ignore")
      if candidate not in seen:
        seen.add(candidate)
        decoded.append(candidate)
    return decoded

  def _resolve_source_path(self, sf_path, base_dir, source_roots=None):
    """
    Resolve a source-file path from LCOV/raw coverage to a real file in the repo.
    """
    base_dir = Path(base_dir).resolve()
    source_roots = [] if source_roots is None else [
      Path(root).resolve() for root in source_roots if root is not None
    ]

    repo_root = self._get_repo_root()
    repo_roots = [repo_root]

    if os.path.isabs(sf_path):
      resolved = Path(sf_path)
      return resolved if resolved.exists() else resolved

    normalized = sf_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    candidates = []
    seen = set()

    def add_candidate(candidate):
      candidate = candidate.resolve()
      key = str(candidate)
      if key not in seen:
        seen.add(key)
        candidates.append(candidate)

    basename_only = "/" not in normalized

    if not basename_only:
      for root in [base_dir] + source_roots + repo_roots:
        add_candidate(root / normalized)

    if not basename_only:
      for idx in range(len(parts)):
        suffix_parts = parts[idx:]
        if len(suffix_parts) == 0:
          continue
        suffix = Path(*suffix_parts)
        for repo_root in repo_roots:
          add_candidate(repo_root / suffix)

    for candidate in candidates:
      if candidate.exists():
        return candidate

    if len(parts) > 0:
      basename = parts[-1]
      source_root_matches = []
      suffix_matches = []
      basename_matches = []
      normalized_suffix = "/" + "/".join(parts)
      search_roots = source_roots + [root for root in repo_roots if root not in source_roots]
      ignored_dirs = {"coverage_annotated", "cov_annot", "coverage_merge.html", "logs", "artefacts"}
      for search_root in search_roots:
        for match in search_root.rglob(basename):
          match = match.resolve()
          if any(part in ignored_dirs for part in match.parts):
            continue
          if any(part.startswith("obj_dir") for part in match.parts):
            continue
          match_str = str(match).replace("\\", "/")
          if search_root in source_roots:
            source_root_matches.append(match)
          if match_str.endswith(normalized_suffix):
            suffix_matches.append(match)
          basename_matches.append(match)

      deduped_source_root_matches = []
      seen_source_matches = set()
      for match in source_root_matches:
        key = str(match)
        if key not in seen_source_matches:
          seen_source_matches.add(key)
          deduped_source_root_matches.append(match)
      if len(deduped_source_root_matches) == 1:
        return deduped_source_root_matches[0]

      if len(suffix_matches) == 1:
        return suffix_matches[0]

      deduped_basename_matches = []
      seen_matches = set()
      for match in basename_matches:
        key = str(match)
        if key not in seen_matches:
          seen_matches.add(key)
          deduped_basename_matches.append(match)
      if len(deduped_basename_matches) == 1:
        return deduped_basename_matches[0]

    if len(candidates) > 0:
      return candidates[0]
    return base_dir / normalized

  def _build_annotate_cwd(self, raw_path, temp_root, source_roots=None):
    """
    Build a synthetic working directory so `verilator_coverage --annotate` can
    resolve relative source paths stored in the raw coverage database.
    """
    repo_root = self._get_repo_root()
    raw_dir = Path(os.path.dirname(raw_path)).resolve()
    source_roots = [] if source_roots is None else [Path(root).resolve() for root in source_roots if root is not None]
    relative_paths = [p for p in self._extract_raw_source_paths(raw_path) if not os.path.isabs(p)]

    max_up = 0
    for rel_path in relative_paths:
      up = 0
      norm = rel_path.replace("\\", "/")
      while norm.startswith("../"):
        up += 1
        norm = norm[3:]
      max_up = max(max_up, up)

    levels = max(max_up, 1)
    anchor = Path(temp_root) / "annotate_root"
    current = anchor
    current.mkdir(parents=True, exist_ok=True)
    level_dirs = [anchor]
    for idx in range(levels):
      current = current / f"lvl_{idx}"
      current.mkdir(exist_ok=True)
      level_dirs.append(current)

    deep_cwd = level_dirs[-1]
    for rel_path in relative_paths:
      extra_roots = []
      # Keep basename-only files anchored to the suite roots. Broad repo-parent
      # scans make names like `tb_top.sv` ambiguous across sibling worktrees.
      if "/" in rel_path.replace("\\", "/"):
        for root in [raw_dir.parent, raw_dir]:
          if root != repo_root:
            extra_roots.append(root)
      target_path = self._resolve_source_path(
        rel_path,
        base_dir=raw_dir,
        source_roots=source_roots + extra_roots,
      )
      synthetic_path = Path(os.path.normpath(os.path.join(deep_cwd, rel_path)))
      synthetic_path.parent.mkdir(parents=True, exist_ok=True)
      if synthetic_path.exists():
        continue
      try:
        shutil.copyfile(target_path, synthetic_path)
      except OSError:
        pass

    return str(deep_cwd)

  def _normalize_lcov_paths(self, lcov_path, source_roots=None):
    """
    Rewrite LCOV `SF:` entries to normalized repo-resolved paths.
    """
    lcov_file = Path(lcov_path)
    base_dir = lcov_file.parent.resolve()
    source_roots = [] if source_roots is None else [
      Path(root).resolve() for root in source_roots if root is not None
    ]

    repo_root = self._get_repo_root()
    repo_roots = [repo_root]

    def resolve_sf_path(sf_path):
      if os.path.isabs(sf_path):
        resolved = Path(sf_path)
        return str(resolved if resolved.exists() else resolved)

      normalized = sf_path.replace("\\", "/")
      parts = [part for part in normalized.split("/") if part not in ("", ".")]
      candidates = []
      seen = set()

      def add_candidate(candidate):
        candidate = candidate.resolve()
        key = str(candidate)
        if key not in seen:
          seen.add(key)
          candidates.append(candidate)

      for root in [base_dir] + source_roots + repo_roots:
        add_candidate(root / normalized)

      # Try trimming leading relative path segments and matching the remaining
      # repo-relative suffix from any discovered repo root.
      for idx in range(len(parts)):
        suffix_parts = parts[idx:]
        if len(suffix_parts) == 0:
          continue
        suffix = Path(*suffix_parts)
        for repo_root in repo_roots:
          add_candidate(repo_root / suffix)

      for candidate in candidates:
        if candidate.exists():
          return str(candidate)

      if len(parts) > 0:
        basename = parts[-1]
        source_root_matches = []
        suffix_matches = []
        basename_matches = []
        normalized_suffix = "/" + "/".join(parts)
        search_roots = source_roots + [root for root in repo_roots if root not in source_roots]
        for search_root in search_roots:
          for match in search_root.rglob(basename):
            match = match.resolve()
            match_str = str(match).replace("\\", "/")
            if search_root in source_roots:
              source_root_matches.append(match)
            if match_str.endswith(normalized_suffix):
              suffix_matches.append(match)
            basename_matches.append(match)

        deduped_source_root_matches = []
        seen_source_matches = set()
        for match in source_root_matches:
          key = str(match)
          if key not in seen_source_matches:
            seen_source_matches.add(key)
            deduped_source_root_matches.append(match)
        if len(deduped_source_root_matches) == 1:
          return str(deduped_source_root_matches[0])

        if len(suffix_matches) == 1:
          return str(suffix_matches[0])

        deduped_basename_matches = []
        seen_matches = set()
        for match in basename_matches:
          key = str(match)
          if key not in seen_matches:
            seen_matches.add(key)
            deduped_basename_matches.append(match)
        if len(deduped_basename_matches) == 1:
          return str(deduped_basename_matches[0])

      return str(candidates[0])

    out_lines = []
    with lcov_file.open("r", encoding="utf-8") as f:
      for line in f:
        if line.startswith("SF:"):
          sf_path = line[3:].strip()
          sf_path = resolve_sf_path(sf_path)
          out_lines.append(f"SF:{sf_path}\n")
        else:
          out_lines.append(line)
    with lcov_file.open("w", encoding="utf-8") as f:
      f.writelines(out_lines)

  def _line_has_branch_syntax(self, src_line):
    """
    Heuristically detect whether a source line should carry LCOV branch records.
    """
    line = src_line.strip()
    if not line:
      return False
    branch_re = re.compile(
      r"\b(if|else\s+if|case|casex|casez|for|foreach|while|repeat)\b|"
      r"\?\s*[^:]+:|&&|\|\|"
    )
    return branch_re.search(line) is not None

  def _sanitize_lcov_branch_records(self, lcov_path):
    """
    Drop noisy LCOV branch records on non-branch source lines and recompute
    `BRF/BRH` totals.
    """
    current_sf = None
    current_lines = []
    records = []

    with open(lcov_path, "r", encoding="utf-8") as f:
      for raw_line in f:
        line = raw_line.rstrip("\n")
        if line.startswith("SF:"):
          current_sf = line[3:]
          current_lines = [line]
        elif line == "end_of_record":
          current_lines.append(line)
          records.append((current_sf, list(current_lines)))
          current_sf = None
          current_lines = []
        else:
          current_lines.append(line)

    sanitized = []
    for sf_path, rec_lines in records:
      allowed_branch_lines = set()
      try:
        with open(sf_path, "r", encoding="utf-8") as srcf:
          for lineno, src_line in enumerate(srcf, start=1):
            if self._line_has_branch_syntax(src_line):
              allowed_branch_lines.add(lineno)
      except OSError:
        allowed_branch_lines = set()

      branch_lines = []
      branch_hit = 0
      for rec_line in rec_lines:
        if rec_line.startswith("BRDA:"):
          line_no_str, block_str, branch_str, taken_str = rec_line[5:].split(",", 3)
          line_no = int(line_no_str)
          if line_no not in allowed_branch_lines:
            continue
          branch_lines.append(rec_line)
          if taken_str != "-" and int(taken_str) > 0:
            branch_hit += 1

      for rec_line in rec_lines:
        if rec_line.startswith("BRDA:") or rec_line.startswith("BRF:") or rec_line.startswith("BRH:"):
          continue
        if rec_line == "end_of_record":
          sanitized.extend(branch_lines)
          sanitized.append(f"BRF:{len(branch_lines)}")
          sanitized.append(f"BRH:{branch_hit}")
        sanitized.append(rec_line)

    with open(lcov_path, "w", encoding="utf-8") as f:
      for line in sanitized:
        f.write(line + "\n")

  def is_supported(self):
    """
    Report whether this helper supports the selected simulator backend.
    """
    return self.simulator_name == "verilator"

  def collect(self, raw_path, source_roots=None):
    """
    Collect per-test coverage metrics from a raw coverage database.
    """
    if not self.is_supported():
      return None
    if raw_path is None or not os.path.exists(raw_path):
      log_event(logger, logging.WARNING, "coverage.raw_missing", path=raw_path, simulator=self.simulator_name)
      return None

    metrics = CoverageMetrics(raw_paths=[raw_path])
    with tempfile.TemporaryDirectory(prefix="rtl_buddy_lcov_") as tmpdir:
      lcov_path = os.path.join(tmpdir, "coverage.info")
      if self._write_lcov(raw_path, lcov_path, source_roots=source_roots):
        metrics.line, metrics.branch = self._parse_lcov_summary(lcov_path)
    metrics.toggle = self._parse_verilator_metric(raw_path, "toggle", source_roots=source_roots)
    metrics.functional = self._parse_verilator_metric(raw_path, "functional", source_roots=source_roots)
    return metrics

  def _write_lcov(self, raw_path, lcov_path, source_roots=None):
    """
    Export raw coverage to LCOV, then normalize and sanitize the result.
    """
    lcov_cmd = ["verilator_coverage", "--write-info", lcov_path, raw_path]
    log_event(
      logger,
      logging.INFO,
      "coverage.lcov_export.start",
      simulator=self.simulator_name,
      raw_path=raw_path,
      lcov_path=lcov_path,
      command=" ".join(lcov_cmd),
    )
    lcov_result = subprocess.run(lcov_cmd, capture_output=True, text=True)
    if lcov_result.returncode != 0:
      log_event(
        logger,
        logging.ERROR,
        "coverage.lcov_export.failed",
        simulator=self.simulator_name,
        raw_path=raw_path,
        lcov_path=lcov_path,
        returncode=lcov_result.returncode,
        stderr=lcov_result.stderr.strip(),
        stdout=lcov_result.stdout.strip(),
      )
      return False
    self._normalize_lcov_paths(lcov_path, source_roots=source_roots)
    self._sanitize_lcov_branch_records(lcov_path)
    log_event(
      logger,
      logging.INFO,
      "coverage.lcov_export.completed",
      simulator=self.simulator_name,
      raw_path=raw_path,
      lcov_path=lcov_path,
    )
    return True

  def _parse_lcov_summary(self, lcov_path):
    """
    Parse normalized line and branch coverage fractions from an LCOV file.
    """
    line_found = 0
    line_hit = 0
    branch_found = 0
    branch_hit = 0
    saw_lf_lh = False
    saw_brf_brh = False

    with open(lcov_path, "r", encoding="utf-8") as f:
      for line in f:
        if line.startswith("LF:"):
          saw_lf_lh = True
          line_found += int(line[3:].strip())
        elif line.startswith("LH:"):
          saw_lf_lh = True
          line_hit += int(line[3:].strip())
        elif line.startswith("DA:") and not saw_lf_lh:
          line_found += 1
          count = int(line.split(",", 1)[1].strip())
          if count > 0:
            line_hit += 1
        elif line.startswith("BRF:"):
          saw_brf_brh = True
          branch_found += int(line[4:].strip())
        elif line.startswith("BRH:"):
          saw_brf_brh = True
          branch_hit += int(line[4:].strip())
        elif line.startswith("BRDA:") and not saw_brf_brh:
          branch_found += 1
          count_field = line.rsplit(",", 1)[1].strip()
          if count_field != "-" and int(count_field) > 0:
            branch_hit += 1

    line_cov = None if line_found == 0 else (line_hit / line_found)
    branch_cov = None if branch_found == 0 else (branch_hit / branch_found)
    return line_cov, branch_cov

  def parse_lcov_summary_for_prefix(self, lcov_path, prefix):
    """
    Parse normalized line and branch coverage fractions for files rooted under a
    repo-relative path prefix such as `design/example_block`.
    """
    if lcov_path is None or not os.path.exists(lcov_path):
      return None, None

    normalized_prefix = prefix.replace("\\", "/").strip("/")
    if not normalized_prefix:
      return self._parse_lcov_summary(lcov_path)

    repo_root = self._get_repo_root()
    current_matches = False
    line_found = 0
    line_hit = 0
    branch_found = 0
    branch_hit = 0

    with open(lcov_path, "r", encoding="utf-8") as f:
      for raw_line in f:
        line = raw_line.strip()
        if line.startswith("SF:"):
          sf_path_raw = line[3:].strip()
          sf_path_obj = Path(sf_path_raw)
          if sf_path_obj.is_absolute():
            try:
              sf_path = sf_path_obj.resolve().relative_to(repo_root).as_posix()
            except Exception:
              sf_path = sf_path_obj.as_posix()
          else:
            sf_path = sf_path_raw.replace("\\", "/")
          sf_path = sf_path.strip("/")
          current_matches = (
            sf_path == normalized_prefix or
            sf_path.startswith(f"{normalized_prefix}/")
          )
        elif not current_matches:
          continue
        elif line.startswith("DA:"):
          payload = line[3:].split(",")
          if len(payload) < 2:
            continue
          try:
            hit_count = int(payload[1])
          except ValueError:
            continue
          line_found += 1
          if hit_count > 0:
            line_hit += 1
        elif line.startswith("BRDA:"):
          payload = line[5:].split(",")
          if len(payload) < 4:
            continue
          branch_found += 1
          hit_count = payload[3]
          if hit_count not in ("-", "0"):
            branch_hit += 1

    line_cov = None if line_found == 0 else (line_hit / line_found)
    branch_cov = None if branch_found == 0 else (branch_hit / branch_found)
    return line_cov, branch_cov

  def generate_artifacts(self, raw_path, outdir, basename=None, html_dirname=None, html_output=False,
                         source_roots=None, artifact_name=None, html_outdir=None):
    """
    Generate per-test LCOV and optional HTML artifacts from a raw coverage file.
    """
    metrics = self.collect(raw_path, source_roots=source_roots)
    if metrics is None:
      return None

    if basename is None:
      basename_root = artifact_name if artifact_name is not None else Path(raw_path).stem
      basename = f"{self._sanitize_artifact_name(basename_root)}.coverage"
    if html_dirname is None:
      if artifact_name is not None:
        html_dirname = f"coverage_{self._sanitize_artifact_name(artifact_name)}__html"
      else:
        html_dirname = f"{basename}_html"

    if self.use_lcov or html_output:
      lcov_path = os.path.join(outdir, f"{basename}.info")
      lcov_source_roots = [os.path.dirname(raw_path)]
      if source_roots is not None:
        lcov_source_roots.extend(source_roots)
      if self._write_lcov(raw_path, lcov_path, source_roots=lcov_source_roots):
        metrics.lcov_path = lcov_path
        if html_output:
          html_base_dir = outdir if html_outdir is None else html_outdir
          html_dir = os.path.join(html_base_dir, html_dirname)
          repo_root = str(self._get_repo_root())
          genhtml_cmd = [
            "genhtml",
            "--branch-coverage",
            lcov_path,
            "--prefix",
            repo_root,
            "-o",
            html_dir,
          ]
          log_event(
            logger,
            logging.INFO,
            "coverage.html_export.start",
            simulator=self.simulator_name,
            lcov_path=lcov_path,
            html_dir=html_dir,
            command=" ".join(genhtml_cmd),
          )
          html_result = subprocess.run(genhtml_cmd, capture_output=True, text=True, cwd=repo_root)
          if html_result.returncode != 0:
            log_event(
              logger,
              logging.ERROR,
              "coverage.html_export.failed",
              simulator=self.simulator_name,
              lcov_path=lcov_path,
              html_dir=html_dir,
              returncode=html_result.returncode,
              stderr=html_result.stderr.strip(),
              stdout=html_result.stdout.strip(),
            )
          else:
            metrics.html_dir = html_dir
            log_event(
              logger,
              logging.INFO,
              "coverage.html_export.completed",
              simulator=self.simulator_name,
              lcov_path=lcov_path,
              html_dir=html_dir,
            )

    return metrics

  def generate_html(self, lcov_path, outdir, html_dirname="coverage_merge.html", html_outdir=None):
    """
    Generate LCOV HTML for an existing `.info` file.
    """
    if lcov_path is None or not os.path.exists(lcov_path):
      return None

    html_base_dir = outdir if html_outdir is None else html_outdir
    html_dir = os.path.join(html_base_dir, html_dirname)
    repo_root = str(self._get_repo_root())
    genhtml_cmd = [
      "genhtml",
      "--branch-coverage",
      lcov_path,
      "--prefix",
      repo_root,
      "-o",
      html_dir,
    ]
    log_event(
      logger,
      logging.INFO,
      "coverage.html_export.start",
      simulator=self.simulator_name,
      lcov_path=lcov_path,
      html_dir=html_dir,
      command=" ".join(genhtml_cmd),
    )
    html_result = subprocess.run(genhtml_cmd, capture_output=True, text=True, cwd=repo_root)
    if html_result.returncode != 0:
      log_event(
        logger,
        logging.ERROR,
        "coverage.html_export.failed",
        simulator=self.simulator_name,
        lcov_path=lcov_path,
        html_dir=html_dir,
        returncode=html_result.returncode,
        stderr=html_result.stderr.strip(),
        stdout=html_result.stdout.strip(),
      )
      return None

    log_event(
      logger,
      logging.INFO,
      "coverage.html_export.completed",
      simulator=self.simulator_name,
      lcov_path=lcov_path,
      html_dir=html_dir,
    )
    return html_dir

  def merge(self, raw_paths, outdir, merge_basename="coverage_merged", html_output=False, source_roots=None, html_outdir=None):
    """
    Merge multiple raw coverage databases and return aggregate coverage metrics.
    """
    if not self.is_supported():
      return None

    raw_paths = [p for p in raw_paths if p is not None and os.path.exists(p)]
    if len(raw_paths) == 0:
      return None

    merged_path = os.path.join(outdir, f"{merge_basename}.dat")
    run_cmd = ["verilator_coverage", "--write", merged_path] + raw_paths
    log_event(
      logger,
      logging.INFO,
      "coverage.merge.start",
      simulator=self.simulator_name,
      merged_path=merged_path,
      inputs=raw_paths,
      command=" ".join(run_cmd),
    )
    result = subprocess.run(run_cmd, capture_output=True, text=True)
    if result.returncode != 0:
      log_event(
        logger,
        logging.ERROR,
        "coverage.merge.failed",
        simulator=self.simulator_name,
        merged_path=merged_path,
        inputs=raw_paths,
        returncode=result.returncode,
        stderr=result.stderr.strip(),
        stdout=result.stdout.strip(),
      )
      merged_path = None
    else:
      log_event(
        logger,
        logging.INFO,
        "coverage.merge.completed",
        simulator=self.simulator_name,
        merged_path=merged_path,
        inputs=raw_paths,
      )

    metrics = CoverageMetrics(raw_paths=list(raw_paths), merged_path=merged_path)

    lcov_inputs = []
    with tempfile.TemporaryDirectory(prefix="rtl_buddy_merge_lcov_") as tmpdir:
      for idx, raw_path in enumerate(raw_paths):
        lcov_input = os.path.join(tmpdir, f"part_{idx}.info")
        lcov_source_roots = [os.path.dirname(raw_path)]
        if source_roots is not None:
          lcov_source_roots.extend(source_roots)
        if self._write_lcov(raw_path, lcov_input, source_roots=lcov_source_roots):
          lcov_inputs.append(lcov_input)

      if len(lcov_inputs) > 0 and (self.use_lcov or html_output):
        merged_lcov_path = os.path.join(outdir, f"{merge_basename}.info")
        self._merge_lcov_files(lcov_inputs, merged_lcov_path)
        metrics.lcov_path = merged_lcov_path
        metrics.line, metrics.branch = self._parse_lcov_summary(merged_lcov_path)
        if html_output:
          html_base_dir = outdir if html_outdir is None else html_outdir
          html_dir = os.path.join(html_base_dir, "coverage_merge.html")
          repo_root = str(self._get_repo_root())
          genhtml_cmd = [
            "genhtml",
            "--branch-coverage",
            merged_lcov_path,
            "--prefix",
            repo_root,
            "-o",
            html_dir,
          ]
          log_event(
            logger,
            logging.INFO,
            "coverage.html_export.start",
            simulator=self.simulator_name,
            lcov_path=merged_lcov_path,
            html_dir=html_dir,
            command=" ".join(genhtml_cmd),
          )
          html_result = subprocess.run(genhtml_cmd, capture_output=True, text=True, cwd=repo_root)
          if html_result.returncode != 0:
            log_event(
              logger,
              logging.ERROR,
              "coverage.html_export.failed",
              simulator=self.simulator_name,
              lcov_path=merged_lcov_path,
              html_dir=html_dir,
              returncode=html_result.returncode,
              stderr=html_result.stderr.strip(),
              stdout=html_result.stdout.strip(),
            )
          else:
            metrics.html_dir = html_dir
            log_event(
              logger,
              logging.INFO,
              "coverage.html_export.completed",
              simulator=self.simulator_name,
              lcov_path=merged_lcov_path,
              html_dir=html_dir,
            )

    if metrics.line is None and merged_path is not None:
      with tempfile.TemporaryDirectory(prefix="rtl_buddy_lcov_") as tmpdir:
        lcov_path = os.path.join(tmpdir, "coverage.info")
        if self._write_lcov(merged_path, lcov_path):
          metrics.line, metrics.branch = self._parse_lcov_summary(lcov_path)

    if merged_path is not None:
      metrics.toggle = self._parse_verilator_metric(merged_path, "toggle", source_roots=source_roots)
      metrics.functional = self._parse_verilator_metric(merged_path, "functional", source_roots=source_roots)

    if metrics.line is None and metrics.branch is None and metrics.toggle is None and metrics.functional is None:
      return None

    return metrics

  def _merge_lcov_files(self, input_paths, output_path):
    """
    Merge multiple LCOV files by summing line and branch hit counts per source file.
    """
    line_counts = defaultdict(dict)
    branch_counts = defaultdict(dict)

    current_sf = None
    with open(output_path, "w", encoding="utf-8") as _:
      pass

    for input_path in input_paths:
      with open(input_path, "r", encoding="utf-8") as f:
        for raw_line in f:
          line = raw_line.strip()
          if line.startswith("SF:"):
            current_sf = line[3:]
          elif line.startswith("DA:") and current_sf is not None:
            line_no_str, count_str = line[3:].split(",", 1)
            line_no = int(line_no_str)
            count = int(count_str)
            line_counts[current_sf][line_no] = line_counts[current_sf].get(line_no, 0) + count
          elif line.startswith("BRDA:") and current_sf is not None:
            line_no_str, block_str, branch_str, taken_str = line[5:].split(",", 3)
            key = (int(line_no_str), block_str, branch_str)
            if taken_str == "-":
              count = None
            else:
              count = int(taken_str)
            prev = branch_counts[current_sf].get(key)
            if prev is None or count is None:
              branch_counts[current_sf][key] = count if prev is None else prev
            else:
              branch_counts[current_sf][key] = prev + count
          elif line == "end_of_record":
            current_sf = None

    with open(output_path, "w", encoding="utf-8") as out:
      for sf_path in sorted(set(line_counts.keys()) | set(branch_counts.keys())):
        out.write(f"SF:{sf_path}\n")

        lines_for_sf = line_counts.get(sf_path, {})
        for line_no in sorted(lines_for_sf.keys()):
          out.write(f"DA:{line_no},{lines_for_sf[line_no]}\n")
        out.write(f"LF:{len(lines_for_sf)}\n")
        out.write(f"LH:{sum(1 for count in lines_for_sf.values() if count > 0)}\n")

        branches_for_sf = branch_counts.get(sf_path, {})
        for (line_no, block_str, branch_str) in sorted(branches_for_sf.keys()):
          count = branches_for_sf[(line_no, block_str, branch_str)]
          count_str = "-" if count is None else str(count)
          out.write(f"BRDA:{line_no},{block_str},{branch_str},{count_str}\n")
        out.write(f"BRF:{len(branches_for_sf)}\n")
        out.write(f"BRH:{sum(1 for count in branches_for_sf.values() if count not in (None, 0))}\n")
        out.write("end_of_record\n")

  def _parse_verilator_metric(self, raw_path, metric_name, source_roots=None):
    """
    Parse a non-LCOV Verilator metric such as toggle or user coverage from a raw
    coverage database.
    """
    filter_type = self._VERILATOR_TYPES[metric_name]
    if metric_name == "functional":
      raw_value = self._parse_raw_user_metric(raw_path)
      if raw_value is not None:
        log_event(
          logger,
          logging.DEBUG,
          "coverage.metric.completed",
          simulator=self.simulator_name,
          metric=metric_name,
          raw_path=raw_path,
          value=raw_value,
          method="raw_user_entries",
        )
        return raw_value
    with tempfile.TemporaryDirectory(prefix="rtl_buddy_cov_") as tmpdir:
      annotate_cwd = self._build_annotate_cwd(raw_path, tmpdir, source_roots=source_roots)
      run_cmd = [
        "verilator_coverage",
        "--annotate",
        annotate_cwd,
        "--filter-type",
        filter_type,
        raw_path,
      ]
      log_event(
        logger,
        logging.DEBUG,
        "coverage.metric.start",
        simulator=self.simulator_name,
        metric=metric_name,
        raw_path=raw_path,
        command=" ".join(run_cmd),
      )
      result = subprocess.run(run_cmd, capture_output=True, text=True, cwd=annotate_cwd)
      output = f"{result.stdout}\n{result.stderr}"
      if result.returncode != 0:
        if metric_name == "functional":
          log_event(
            logger,
            logging.DEBUG,
            "coverage.metric.unsupported",
            simulator=self.simulator_name,
            metric=metric_name,
            raw_path=raw_path,
            returncode=result.returncode,
            output=output.strip(),
          )
          return None
        log_event(
          logger,
          logging.WARNING,
          "coverage.metric.failed",
          simulator=self.simulator_name,
          metric=metric_name,
          raw_path=raw_path,
          returncode=result.returncode,
          output=output.strip(),
        )
        return None

      match = re.search(r"Total coverage \((\d+)/(\d+)\)\s+([0-9.]+)%", output)
      if metric_name == "functional":
        manual_value = self._parse_user_annotated_summary(annotate_cwd)
        if manual_value is not None:
          log_event(
            logger,
            logging.DEBUG,
            "coverage.metric.completed",
            simulator=self.simulator_name,
            metric=metric_name,
            raw_path=raw_path,
            value=manual_value,
            method="annotated_user_lines",
          )
          return manual_value
      if match is None:
        if metric_name == "functional":
          log_event(
            logger,
            logging.DEBUG,
            "coverage.metric.unsupported",
            simulator=self.simulator_name,
            metric=metric_name,
            raw_path=raw_path,
            output=output.strip(),
          )
          return None
        log_event(
          logger,
          logging.WARNING,
          "coverage.metric.summary_missing",
          simulator=self.simulator_name,
          metric=metric_name,
          raw_path=raw_path,
          output=output.strip(),
        )
        return None

      total = int(match.group(2))
      hit = int(match.group(1))
      if total == 0:
        return None
      value = hit / total
      log_event(
        logger,
        logging.DEBUG,
        "coverage.metric.completed",
        simulator=self.simulator_name,
        metric=metric_name,
        raw_path=raw_path,
        hit=hit,
        total=total,
        value=value,
      )
      return value

  def _parse_raw_user_metric(self, raw_path):
    """
    Derive functional/user coverage directly from raw Verilator coverage entries.

    Some Verilator versions can report an incorrect 0/N summary for
    `--filter-type user` despite non-zero user counters in the raw database.
    Parse `t=user` counter records from `coverage.dat` to compute hit/total.
    """
    try:
      raw_bytes = Path(raw_path).read_bytes()
    except OSError:
      return None

    # Example raw record shape:
    # C '<... \x01t\x02user ...>' <count>
    user_re = re.compile(rb"C '([^']*?\x01t\x02user[^']*)' ([0-9]+)")
    matches = user_re.findall(raw_bytes)
    if len(matches) == 0:
      return None

    total = len(matches)
    hit = sum(1 for _, count in matches if int(count) > 0)
    if total == 0:
      return None
    return hit / total

  def _parse_user_annotated_summary(self, annotate_dir):
    """
    Derive functional/user coverage directly from Verilator annotate output.

    Verilator 5.042 can emit correct per-line `%000001` style hit markers for
    `--filter-type user` while still printing `Total coverage (0/N) 0.00%`.
    Count those annotated markers instead of trusting the broken summary line.
    """
    annotate_root = Path(annotate_dir)
    if not annotate_root.exists():
      return None

    total = 0
    hit = 0
    line_re = re.compile(r"\s*%(\d+)\b")

    for path in annotate_root.rglob("*"):
      if not path.is_file():
        continue
      try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
          for raw_line in f:
            match = line_re.match(raw_line)
            if match is None:
              continue
            total += 1
            if int(match.group(1)) > 0:
              hit += 1
      except OSError:
        continue

    if total == 0:
      return None
    return hit / total
