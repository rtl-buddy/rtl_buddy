# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Collects, merges and reports Verilator coverage."""

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

from .. import tool_manifest as tm
from ..config.root import RootConfig
from ..cov.raw import COVER, EXPRESSION, parse_raw_records
from ..cov.source_paths import SourcePathResolver
from ..logging_utils import log_event
from .artifact_paths import sanitize_artifact_component


#: Display token for a metric that was never measured or cannot be represented.
COV_UNSUPPORTED_TOKEN = "UNSP"

#: Display token for a metric whose measurement was attempted and lost. Same width as `UNSP`.
COV_FAILED_TOKEN = "FAIL"

#: Scalar metrics `CoverageMetrics` carries, in report order.
METRIC_NAMES = ("line", "branch", "toggle", "expression", "functional")


def _fmt_cov(value, failed=False):
    """Format a coverage ratio as ``0.00``-``1.00``, or ``FAIL`` (when ``failed``) or ``UNSP`` for None."""
    if value is None:
        return COV_FAILED_TOKEN if failed else COV_UNSUPPORTED_TOKEN
    return f"{max(0.0, min(1.0, value)):.2f}"


# Distinguishes an omitted argument from an explicit None.
_UNSET = object()


def aggregate_cover_records(records):
    """Sum ``hits`` of user-coverage records per ``(file, line, name, module)``.

    Module stays in the key so a cover property compiled into several modules
    is not collapsed into a combined count. Returns a list sorted by that key,
    or None when there are no records.
    """
    if not records:
        return None

    aggregated = {}
    for record in records:
        key = (
            record.get("file"),
            record.get("line"),
            record.get("name"),
            record.get("module"),
        )
        entry = aggregated.get(key)
        if entry is None:
            aggregated[key] = {
                "name": record.get("name"),
                "file": record.get("file"),
                "line": record.get("line"),
                "module": record.get("module"),
                "hits": record.get("hits", 0),
            }
        else:
            entry["hits"] += record.get("hits", 0)

    return sorted(
        aggregated.values(),
        key=lambda e: (
            e["file"] or "",
            e["line"] if e["line"] is not None else -1,
            e["name"] or "",
            e["module"] or "",
        ),
    )


@dataclass
class CoverageMetrics:
    line: float | None = None
    branch: float | None = None
    toggle: float | None = None
    expression: float | None = None
    functional: float | None = None
    covers: list[dict] | None = None
    raw_paths: list[str] | None = None
    merged_path: str | None = None
    lcov_path: str | None = None
    html_dir: str | None = None
    #: True when the merge tool failed; metrics with no other source are then lost.
    merge_failed: bool = False
    #: Names from :data:`METRIC_NAMES` that the failed merge lost.
    failed_metrics: list[str] | None = None

    def is_failed(self, metric):
        """Return whether ``metric`` is missing because its measurement failed."""
        return self.failed_metrics is not None and metric in self.failed_metrics

    def summary_str(self):
        """Return the one-line `L/B/T/F` summary; expression is omitted, and failed metrics print `FAIL`."""
        return (
            f"L:{_fmt_cov(self.line, self.is_failed('line'))} "
            f"B:{_fmt_cov(self.branch, self.is_failed('branch'))} "
            f"T:{_fmt_cov(self.toggle, self.is_failed('toggle'))} "
            f"F:{_fmt_cov(self.functional, self.is_failed('functional'))}"
        )

    def to_dict(self):
        """Return the metrics as a result dict.

        ``merge_failed`` and ``failed_metrics`` are always present: a null
        metric listed in ``failed_metrics`` failed, any other was not instrumented.
        """
        return {
            "line": self.line,
            "branch": self.branch,
            "toggle": self.toggle,
            "expression": self.expression,
            "functional": self.functional,
            "covers": None if self.covers is None else list(self.covers),
            "summary": self.summary_str(),
            "raw_paths": [] if self.raw_paths is None else list(self.raw_paths),
            "merged_path": self.merged_path,
            "lcov_path": self.lcov_path,
            "html_dir": self.html_dir,
            "merge_failed": self.merge_failed,
            "failed_metrics": (
                [] if self.failed_metrics is None else list(self.failed_metrics)
            ),
        }


class VlogCov:
    """Collects and merges Verilator coverage and exports LCOV and HTML."""

    _VERILATOR_TYPES = {
        "line": "line",
        "branch": "branch",
        "toggle": "toggle",
        "functional": "user",
    }

    def __init__(self, simulator_name, use_lcov=False, root_cfg=None):
        self.simulator_name = simulator_name
        self.use_lcov = use_lcov
        self.root_cfg = root_cfg

    def _get_repo_root(self):
        """Return the absolute project root directory for path normalization."""
        if self.root_cfg is None:
            self.root_cfg = RootConfig(name="coverage")
        return Path(self.root_cfg.get_project_rootdir()).resolve()

    def _sanitize_artifact_name(self, name):
        """Return a filesystem-safe coverage artifact name."""
        return sanitize_artifact_component(name)

    def _require_lcov(self):
        """Assert that genhtml (lcov manifest entry) is available."""
        tm.require("lcov", self.root_cfg)

    def _extract_raw_source_paths(self, raw_path):
        """Extract candidate source-file paths embedded in a raw coverage database."""
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
                    if (
                        re.fullmatch(r"[A-Za-z0-9_./+-]+\.s?vh?", candidate)
                        and candidate not in seen
                    ):
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

    def _source_resolver(self, base_dir, source_roots=None):
        """Build the source-path resolver; ``source_roots`` are ``[run dir, suite root]`` hints, most specific first."""
        return SourcePathResolver(
            self._get_repo_root(),
            base_dir=base_dir,
            source_roots=source_roots,
        )

    def _resolve_source_path(self, sf_path, base_dir, source_roots=None):
        """Resolve a source-file path from LCOV/raw coverage to a real file in the repo."""
        return self._source_resolver(base_dir, source_roots).resolve_path(sf_path)

    def _build_annotate_cwd(self, raw_path, temp_root, source_roots=None):
        """Build a temp working directory so `verilator_coverage --annotate` resolves the raw database's relative source paths."""
        repo_root = self._get_repo_root()
        raw_dir = Path(os.path.dirname(raw_path)).resolve()
        source_roots = (
            []
            if source_roots is None
            else [Path(root).resolve() for root in source_roots if root is not None]
        )
        relative_paths = [
            p for p in self._extract_raw_source_paths(raw_path) if not os.path.isabs(p)
        ]

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
            # Only paths with a directory get extra roots: a bare `tb_top.sv` is ambiguous across worktrees.
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
        """Rewrite LCOV `SF:` entries to normalized repo-resolved paths."""
        self._source_resolver(Path(lcov_path).parent, source_roots).rewrite_info(
            lcov_path, relative=False
        )

    def _line_has_branch_syntax(self, src_line):
        """Heuristically detect whether a source line should carry LCOV branch records."""
        line = src_line.strip()
        if not line:
            return False
        branch_re = re.compile(
            r"\b(if|else\s+if|case|casex|casez|for|foreach|while|repeat)\b|"
            r"\?\s*[^:]+:|&&|\|\|"
        )
        return branch_re.search(line) is not None

    def _sanitize_lcov_branch_records(self, lcov_path):
        """Drop LCOV branch records on lines with no branch syntax and recompute `BRF/BRH`."""
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
                    line_no_str, block_str, branch_str, taken_str = rec_line[5:].split(
                        ",", 3
                    )
                    line_no = int(line_no_str)
                    if line_no not in allowed_branch_lines:
                        continue
                    branch_lines.append(rec_line)
                    if taken_str != "-" and int(taken_str) > 0:
                        branch_hit += 1

            for rec_line in rec_lines:
                if (
                    rec_line.startswith("BRDA:")
                    or rec_line.startswith("BRF:")
                    or rec_line.startswith("BRH:")
                ):
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
        """Report whether this helper supports the selected simulator backend."""
        return self.simulator_name == "verilator"

    def collect(self, raw_path, source_roots=None):
        """Collect per-test coverage metrics from a raw coverage database."""
        if not self.is_supported():
            return None
        if raw_path is None or not os.path.exists(raw_path):
            log_event(
                logger,
                logging.WARNING,
                "coverage.raw_missing",
                path=raw_path,
                simulator=self.simulator_name,
            )
            return None

        metrics = CoverageMetrics(raw_paths=[raw_path])
        with tempfile.TemporaryDirectory(prefix="rtl_buddy_lcov_") as tmpdir:
            lcov_path = os.path.join(tmpdir, "coverage.info")
            if self._write_lcov(raw_path, lcov_path, source_roots=source_roots):
                metrics.line, metrics.branch = self._parse_lcov_summary(lcov_path)
        metrics.toggle = self._parse_verilator_metric(
            raw_path, "toggle", source_roots=source_roots
        )
        # Read from the raw database: LCOV export folds expression points into `DA:` records.
        metrics.expression = self._ratio_from_raw_metric(raw_path, EXPRESSION)
        user_records = self.parse_user_cover_records(raw_path)
        metrics.functional = self._parse_verilator_metric(
            raw_path,
            "functional",
            source_roots=source_roots,
            user_records=user_records,
        )
        metrics.covers = aggregate_cover_records(user_records)
        return metrics

    def _write_lcov(self, raw_path, lcov_path, source_roots=None):
        """Export raw coverage to LCOV, then normalize and sanitize the result."""
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
        """Parse normalized line and branch coverage fractions from an LCOV file."""
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

    def parse_lcov_summary(self, lcov_path):
        """Return `(line_ratio, branch_ratio)` for a whole LCOV `.info` file."""
        return self._parse_lcov_summary(lcov_path)

    def parse_lcov_summary_for_prefix(self, lcov_path, prefix):
        """Return `(line_ratio, branch_ratio)` for files under a repo-relative prefix such as `design/example_block`."""
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
                            sf_path = (
                                sf_path_obj.resolve().relative_to(repo_root).as_posix()
                            )
                        except Exception:
                            sf_path = sf_path_obj.as_posix()
                    else:
                        sf_path = sf_path_raw.replace("\\", "/")
                    sf_path = sf_path.strip("/")
                    current_matches = (
                        sf_path == normalized_prefix
                        or sf_path.startswith(f"{normalized_prefix}/")
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

    def generate_artifacts(
        self,
        raw_path,
        outdir,
        basename=None,
        html_dirname=None,
        html_output=False,
        source_roots=None,
        artifact_name=None,
        html_outdir=None,
    ):
        """Generate per-test LCOV and optional HTML artifacts from a raw coverage file."""
        metrics = self.collect(raw_path, source_roots=source_roots)
        if metrics is None:
            return None

        if basename is None:
            basename_root = (
                artifact_name if artifact_name is not None else Path(raw_path).stem
            )
            basename = f"{self._sanitize_artifact_name(basename_root)}.coverage"
        if html_dirname is None:
            if artifact_name is not None:
                html_dirname = (
                    f"coverage_{self._sanitize_artifact_name(artifact_name)}__html"
                )
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
                    self._require_lcov()
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
                    html_result = subprocess.run(
                        genhtml_cmd, capture_output=True, text=True, cwd=repo_root
                    )
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

    def generate_html(
        self, lcov_path, outdir, html_dirname="coverage_merge.html", html_outdir=None
    ):
        """Generate LCOV HTML for an existing `.info` file."""
        if lcov_path is None or not os.path.exists(lcov_path):
            return None

        self._require_lcov()
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
        html_result = subprocess.run(
            genhtml_cmd, capture_output=True, text=True, cwd=repo_root
        )
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

    def merge(
        self,
        raw_paths,
        outdir,
        merge_basename="coverage_merged",
        html_output=False,
        source_roots=None,
        html_outdir=None,
    ):
        """Merge raw coverage databases and return the aggregate metrics.

        If ``verilator_coverage --write`` fails, toggle, expression and
        functional are lost: ``merge_failed`` and ``failed_metrics`` say so and
        they print ``FAIL``. Line and branch survive only when ``use-lcov`` or
        HTML output is on, since they then come from the per-test LCOV exports.
        Metrics that are simply absent stay ``UNSP``.

        Returns None only when nothing was measured and nothing failed.
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

        # Per-test exports are needed only for the merged `.info` (`use-lcov` or HTML).
        if self.use_lcov or html_output:
            lcov_inputs = []
            with tempfile.TemporaryDirectory(prefix="rtl_buddy_merge_lcov_") as tmpdir:
                for idx, raw_path in enumerate(raw_paths):
                    lcov_input = os.path.join(tmpdir, f"part_{idx}.info")
                    lcov_source_roots = [os.path.dirname(raw_path)]
                    if source_roots is not None:
                        lcov_source_roots.extend(source_roots)
                    if self._write_lcov(
                        raw_path, lcov_input, source_roots=lcov_source_roots
                    ):
                        lcov_inputs.append(lcov_input)

                if len(lcov_inputs) > 0:
                    merged_lcov_path = os.path.join(outdir, f"{merge_basename}.info")
                    self._merge_lcov_files(lcov_inputs, merged_lcov_path)
                    metrics.lcov_path = merged_lcov_path
                    metrics.line, metrics.branch = self._parse_lcov_summary(
                        merged_lcov_path
                    )
                    if html_output:
                        self._require_lcov()
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
                        html_result = subprocess.run(
                            genhtml_cmd, capture_output=True, text=True, cwd=repo_root
                        )
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
            metrics.toggle = self._parse_verilator_metric(
                merged_path, "toggle", source_roots=source_roots
            )
            metrics.expression = self._ratio_from_raw_metric(merged_path, EXPRESSION)
            metrics.functional = self._parse_verilator_metric(
                merged_path, "functional", source_roots=source_roots
            )
        else:
            # Toggle, expression and functional exist only in the merged `.dat`.
            # Line and branch are lost only if no merged `.info` exists either;
            # a merged `.info` that parses to None means "no such points", not failure.
            metrics.merge_failed = True
            merge_only = {"toggle", "expression", "functional"}
            if metrics.lcov_path is None:
                merge_only |= {"line", "branch"}
            metrics.failed_metrics = [
                name
                for name in METRIC_NAMES
                if name in merge_only and getattr(metrics, name) is None
            ]

        if (
            metrics.line is None
            and metrics.branch is None
            and metrics.toggle is None
            and metrics.functional is None
            and not metrics.merge_failed
        ):
            return None

        return metrics

    def _merge_lcov_files(self, input_paths, output_path):
        """Merge multiple LCOV files by summing line and branch hit counts per source file."""
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
                        line_counts[current_sf][line_no] = (
                            line_counts[current_sf].get(line_no, 0) + count
                        )
                    elif line.startswith("BRDA:") and current_sf is not None:
                        line_no_str, block_str, branch_str, taken_str = line[5:].split(
                            ",", 3
                        )
                        key = (int(line_no_str), block_str, branch_str)
                        if taken_str == "-":
                            count = None
                        else:
                            count = int(taken_str)
                        prev = branch_counts[current_sf].get(key)
                        if prev is None or count is None:
                            branch_counts[current_sf][key] = (
                                count if prev is None else prev
                            )
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
                out.write(
                    f"LH:{sum(1 for count in lines_for_sf.values() if count > 0)}\n"
                )

                branches_for_sf = branch_counts.get(sf_path, {})
                for line_no, block_str, branch_str in sorted(branches_for_sf.keys()):
                    count = branches_for_sf[(line_no, block_str, branch_str)]
                    count_str = "-" if count is None else str(count)
                    out.write(f"BRDA:{line_no},{block_str},{branch_str},{count_str}\n")
                out.write(f"BRF:{len(branches_for_sf)}\n")
                out.write(
                    f"BRH:{sum(1 for count in branches_for_sf.values() if count not in (None, 0))}\n"
                )
                out.write("end_of_record\n")

    def _parse_verilator_metric(
        self, raw_path, metric_name, source_roots=None, user_records=_UNSET
    ):
        """Return the hit ratio of a non-LCOV metric (toggle, functional) from a raw database, or None.

        ``user_records`` are pre-parsed ``t=user`` records. Passing None means
        "parsed, none found"; omitting it means "parse now".
        """
        filter_type = self._VERILATOR_TYPES[metric_name]
        if metric_name == "functional":
            if user_records is _UNSET:
                raw_value = self._parse_raw_user_metric(raw_path)
            else:
                raw_value = self._ratio_from_user_records(user_records)
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
            annotate_cwd = self._build_annotate_cwd(
                raw_path, tmpdir, source_roots=source_roots
            )
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
            result = subprocess.run(
                run_cmd, capture_output=True, text=True, cwd=annotate_cwd
            )
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

            # Summary formats: "Total coverage (hit/total) X.XX%" up to Verilator 5.042,
            # "  toggle    : 63.1% ( 82/130)" from 5.048.
            hit, total = None, None
            legacy = re.search(r"Total coverage \((\d+)/(\d+)\)\s+([0-9.]+)%", output)
            if legacy is not None:
                hit = int(legacy.group(1))
                total = int(legacy.group(2))
            else:
                per_metric = re.search(
                    r"^\s+"
                    + re.escape(filter_type)
                    + r"\s*:\s*[0-9.]+%\s*\(\s*(\d+)/(\d+)\)",
                    output,
                    re.MULTILINE,
                )
                if per_metric is not None:
                    hit = int(per_metric.group(1))
                    total = int(per_metric.group(2))

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
            if hit is None or total is None:
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

    def parse_user_cover_records(self, raw_path):
        """Return `t=user` cover-point records from a raw database, or None if unreadable or empty.

        Each record is ``{name, file, line, module, hier, hits}``. Verilator
        writes one record per cover point per containing module, with
        instances already merged (differing hierarchy components become `*`).
        Labels exist only in the raw database, not in LCOV export.
        """
        parsed = parse_raw_records(raw_path, metrics=[COVER])
        if not parsed:
            return None

        records = []
        for record in parsed:
            if not record["name"]:
                # Still reported and counted, but cannot be mapped to a plan item.
                log_event(
                    logger,
                    logging.DEBUG,
                    "coverage.cover_point.unnamed",
                    simulator=self.simulator_name,
                    raw_path=raw_path,
                    file=record["file"],
                    line=record["line"],
                )
            records.append(
                {
                    "name": record["name"],
                    "file": record["file"],
                    "line": record["line"],
                    "module": record["module"],
                    "hier": record["hier"],
                    "hits": record["hits"],
                }
            )
        return records

    def _parse_raw_user_metric(self, raw_path):
        """Compute functional coverage from raw `t=user` records.

        Some Verilator versions report a wrong 0/N summary for `--filter-type user`.
        """
        return self._ratio_from_user_records(self.parse_user_cover_records(raw_path))

    @staticmethod
    def _ratio_from_raw_metric(raw_path, metric):
        """Return the hit ratio of one metric from the raw database, or None if it has no such points."""
        records = parse_raw_records(raw_path, metrics=[metric])
        if not records:
            return None
        return sum(1 for r in records if r["hits"] > 0) / len(records)

    @staticmethod
    def _ratio_from_user_records(records):
        """Hit/total over already-parsed `t=user` records, or None if there are none."""
        if not records:
            return None
        return sum(1 for r in records if r["hits"] > 0) / len(records)

    def _parse_user_annotated_summary(self, annotate_dir):
        """Compute functional coverage by counting `%NNNNNN` hit markers in annotate output.

        Verilator 5.042 prints correct markers but a wrong `Total coverage (0/N)` line.
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
