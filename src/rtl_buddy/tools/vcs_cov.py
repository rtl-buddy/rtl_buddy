"""
vcs_cov module handles VCS coverage merge/report generation for rtl-buddy
"""
import logging
logger = logging.getLogger(__name__)

import os
import shutil
import subprocess
from dataclasses import dataclass

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event


@dataclass
class VcsCoverageMergeResult:
  coverage_file: str
  merged_vdb: str
  report_dir: str
  input_count: int


class VcsCov:
  """
  Merge VCS coverage databases and generate an URG report.
  """

  def merge(self, vdb_paths, outdir, coverage_filename="coverage.f", dbname="merged.vdb", report_dirname="urgReport"):
    os.makedirs(outdir, exist_ok=True)

    normalized_paths = []
    seen = set()
    for vdb_path in vdb_paths:
      if not vdb_path:
        continue
      resolved = os.path.abspath(vdb_path)
      if resolved in seen or not os.path.isdir(resolved):
        continue
      seen.add(resolved)
      normalized_paths.append(resolved)

    if len(normalized_paths) == 0:
      log_event(logger, logging.ERROR, "coverage.vcs_merge.no_inputs", outdir=outdir)
      raise FatalRtlBuddyError("No eligible VCS coverage databases found for merge")

    urg_exe = shutil.which("urg")
    if urg_exe is None:
      log_event(logger, logging.ERROR, "coverage.vcs_merge.tool_missing", executable="urg")
      raise FatalRtlBuddyError("VCS coverage merge requires `urg` on PATH")

    coverage_file = os.path.join(outdir, coverage_filename)
    with open(coverage_file, "w", encoding="utf-8") as fp:
      for vdb_path in normalized_paths:
        fp.write(vdb_path + "\n")

    if os.path.getsize(coverage_file) == 0:
      log_event(logger, logging.ERROR, "coverage.vcs_merge.empty_file", coverage_file=coverage_file)
      raise FatalRtlBuddyError(f"Generated empty VCS coverage file {coverage_file}")

    merged_vdb = os.path.join(outdir, dbname)
    report_dir = os.path.join(outdir, report_dirname)
    run_cmd = [
      urg_exe,
      "-f", coverage_file,
      "-dbname", merged_vdb,
      "-report", report_dir,
      "-lca",
      "-format", "both",
      "-show", "tests",
    ]
    log_event(
      logger,
      logging.INFO,
      "coverage.vcs_merge.start",
      input_count=len(normalized_paths),
      coverage_file=coverage_file,
      merged_vdb=merged_vdb,
      report_dir=report_dir,
    )
    result = subprocess.run(run_cmd, capture_output=True, text=True, cwd=outdir)
    if result.returncode != 0:
      log_event(
        logger,
        logging.ERROR,
        "coverage.vcs_merge.failed",
        returncode=result.returncode,
        coverage_file=coverage_file,
        merged_vdb=merged_vdb,
        report_dir=report_dir,
      )
      stderr = (result.stderr or "").strip()
      stdout = (result.stdout or "").strip()
      details = stderr or stdout
      if details:
        raise FatalRtlBuddyError(f"urg coverage merge failed: {details}")
      raise FatalRtlBuddyError(f"urg coverage merge failed with returncode {result.returncode}")

    missing_outputs = []
    if not os.path.exists(merged_vdb):
      missing_outputs.append(merged_vdb)
    if not os.path.isdir(report_dir):
      missing_outputs.append(report_dir)
    if missing_outputs:
      log_event(
        logger,
        logging.ERROR,
        "coverage.vcs_merge.output_missing",
        merged_vdb=merged_vdb,
        report_dir=report_dir,
        missing=", ".join(missing_outputs),
      )
      raise FatalRtlBuddyError(f"urg coverage merge completed but expected outputs are missing: {', '.join(missing_outputs)}")

    log_event(
      logger,
      logging.INFO,
      "coverage.vcs_merge.completed",
      input_count=len(normalized_paths),
      coverage_file=coverage_file,
      merged_vdb=merged_vdb,
      report_dir=report_dir,
    )
    return VcsCoverageMergeResult(
      coverage_file=coverage_file,
      merged_vdb=merged_vdb,
      report_dir=report_dir,
      input_count=len(normalized_paths),
    )
