"""Per-check style-lint runner — drives verible-verilog-lint directly.

Unlike CDC there is no backend registry: the linter is always the
project's routed ``cfg-verible`` entry, so the runner owns the whole
subprocess. If a second style linter ever appears, split this the way
``cdc_runner`` / ``tools/cdc_*`` did (registry keyed on a per-check
``tool:`` field).

A check's file set is the same expansion ``rb verible lint --model``
applies: the model's bare source entries (``-v``/``-y`` library files
and ``+`` directives dropped), filtered by the cfg-verible ``exclude``
globs plus the check's own. The expanded list is written to
``artefacts/<name>/lint.f`` and the linter's output to
``artefacts/<name>/lint.log``, so a FAIL row in the summary always has
the finding lines on disk next to the exact file set they came from.
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

#: A verible finding line: ``path:line:col[-col]: message [rule]``. Also
#: matches syntax-error lines (``path:line:col: syntax error at ...``),
#: which is deliberate — an unparseable file fails a style gate too.
_FINDING_RE = re.compile(r"^[^\s:][^:]*:\d+:")


class LintRunner:
    def __init__(self, name: str, root_cfg, lint_cfg: LintConfig, suite_dir: str):
        self.name = name
        self.root_cfg = root_cfg
        self.lint_cfg = lint_cfg
        # The lint.yaml's directory: artefacts live under it, and the
        # subprocess runs from it so the log's paths are suite-relative.
        self.suite_dir = suite_dir

        artefact_root = Path(suite_dir) / "artefacts" / lint_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    # --- artefact paths -----------------------------------------------------

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "lint.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "lint.log")

    # --- helpers ------------------------------------------------------------

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
            # Config/environment error, not a skippable condition: a lint
            # regression silently green because the linter is missing is
            # the false pass the flow exists to prevent.
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
        # Plain subprocess.run, not run_managed_process: this matches the
        # existing Verible.do_exe convention, and verible-verilog-lint is
        # a per-file parser with no elaboration — runtime scales with file
        # count and stays in seconds, unlike a CDC analysis. Revisit if a
        # check ever needs a timeout.
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

        # verible-verilog-lint writes findings to stderr (stdout stays
        # empty on a plain lint run) — scan both streams so a build that
        # changes the convention keeps counting.
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
        # Non-zero exit with no finding lines: the tool itself failed
        # (bad flag, unreadable file, ...) — surface that instead of a
        # bogus "0 violations" FAIL.
        return LintFailResults(
            name=self.lint_cfg.get_name(),
            violations=0,
            files=len(files),
            excluded=excluded,
            desc=(
                f"verible-verilog-lint exited with code {proc.returncode} "
                f"(see {self._log_path()})"
            ),
        )
