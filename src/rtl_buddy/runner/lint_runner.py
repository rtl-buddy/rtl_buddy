"""Runs one style-lint check with the project's ``cfg-verible`` verible-verilog-lint.

The check's file set is the model's bare source entries (``-v``/``-y`` library files and ``+`` directives dropped),
minus the ``cfg-verible`` ``exclude`` globs and the check's own, as in ``rb verible lint --model``.
The file set is written to ``artefacts/<name>/lint.f`` and the linter output to ``artefacts/<name>/lint.log``.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

from ..config.lint import LintConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..tools.vlog_filelist import VlogFilelist, apply_exclude_globs
from .lint_results import LintFailResults, LintPassResults, LintResults

logger = logging.getLogger(__name__)

#: A verible finding line, ``path:line:col[-col]: message [rule]``. Syntax-error lines match on purpose.
_FINDING_RE = re.compile(r"^[^\s:][^:]*:\d+:")


class LintRunner:
    def __init__(self, name: str, root_cfg, lint_cfg: LintConfig, suite_dir: str):
        self.name = name
        self.root_cfg = root_cfg
        self.lint_cfg = lint_cfg
        # The lint.yaml's directory; the subprocess runs here so log paths are suite-relative.
        self.suite_dir = suite_dir

        artefact_root = Path(suite_dir) / "artefacts" / lint_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "lint.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "lint.log")

    def _expand_files(self) -> tuple[list[str], int]:
        """The check's file set: model expansion minus exclude globs.

        Returns ``(files, excluded_count)`` with files absolute.
        """
        verible_cfg = self.root_cfg.platform_cfg.get_verible()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist", model_cfg=None, output_path=None
        )
        files = vlog_fl.extract_source_files(self.lint_cfg.get_model())
        patterns = list(verible_cfg.exclude) + self.lint_cfg.get_exclude()
        return apply_exclude_globs(files, patterns, self.root_cfg.get_project_rootdir())

    def run(self) -> LintResults:
        verible_cfg = self.root_cfg.platform_cfg.get_verible()
        if not verible_cfg.available:
            # A missing linter is an error, not a skip: a skip would read as a green lint.
            raise FatalRtlBuddyError(
                f"lint check '{self.lint_cfg.get_name()}': verible binaries "
                "unavailable (see cfg-verible in root_config.yaml)"
            )

        files, excluded = self._expand_files()
        if not files:
            raise FatalRtlBuddyError(
                f"lint check '{self.lint_cfg.get_name()}': model expansion "
                "left no source files (every entry was a -v/-y library "
                "file, a +directive, or matched an exclude glob)"
            )

        # Suite-relative paths: short in the log, and stable across hosts
        # because the artefact tree travels with the suite.
        rel_files = [os.path.relpath(f, self.suite_dir) for f in files]
        Path(self._filelist_path()).write_text("".join(f + "\n" for f in rel_files))

        exe = verible_cfg.get_exe_path("verible-verilog-lint")
        cmd = (
            [exe]
            + verible_cfg.get_extra_args("lint")
            + self.lint_cfg.get_extra_args()
            + rel_files
        )
        log_event(
            logger,
            logging.INFO,
            "lint.start",
            check=self.lint_cfg.get_name(),
            tool=exe,
            files=len(files),
            excluded=excluded,
        )
        with task_status(f"Linting {self.lint_cfg.get_name()}"):
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.suite_dir,
            )
        Path(self._log_path()).write_text(
            "$ " + " ".join(cmd) + "\n" + proc.stdout + proc.stderr
        )

        # Findings normally go to stderr; scan both streams.
        findings = [
            line
            for line in (proc.stdout + proc.stderr).splitlines()
            if _FINDING_RE.match(line)
        ]
        log_event(
            logger,
            logging.INFO,
            "lint.done",
            check=self.lint_cfg.get_name(),
            returncode=proc.returncode,
            violations=len(findings),
            log=self._log_path(),
        )

        if proc.returncode == 0:
            return LintPassResults(
                name=self.lint_cfg.get_name(), files=len(files), excluded=excluded
            )
        if findings:
            return LintFailResults(
                name=self.lint_cfg.get_name(),
                violations=len(findings),
                files=len(files),
                excluded=excluded,
            )
        # Non-zero exit with no findings means the tool itself failed.
        return LintFailResults(
            name=self.lint_cfg.get_name(),
            violations=0,
            files=len(files),
            excluded=excluded,
            desc=(
                f"verible-verilog-lint exited with code {proc.returncode} "
                f"(see {self._log_path()})"
            ),
            # A tool failure is not a violation count, so xfail cannot excuse it.
            fail_stage="tool",
        )
