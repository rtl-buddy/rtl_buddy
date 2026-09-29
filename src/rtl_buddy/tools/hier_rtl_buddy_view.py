"""Wrappers that run the standalone `rtl-buddy-view` CLI on a model's generated filelist.

`rb hier` streams the viewer's stdout unless `-o` is given; stderr goes to `artefacts/hier/<model>/hier.log`.
"""

from __future__ import annotations

import logging
import os
import sys
import re
import shutil
import subprocess
from pathlib import Path

from .vlog_filelist import VlogFilelist
from ..config.model import ModelConfig
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
from ..process_utils import run_managed_process

logger = logging.getLogger(__name__)

#: Keeps the whole version token, dev suffix included, so a dev build can be told from a release.
_VIEW_VERSION_RE = re.compile(r"rtl-buddy-view\s+(\S+)")

#: Seconds to wait for the version probe; a timeout counts as unprobeable.
_VERSION_PROBE_TIMEOUT = 30

#: First `rtl-buddy-sch` release that accepts `--block-diagram`.
VIEW_BLOCK_DIAGRAM_MIN_VERSION = "0.8.0"

#: Unknown-option messages, matched case-insensitively against whitespace-collapsed stderr.
_UNKNOWN_OPTION_MARKERS = (
    "no such option",
    "unrecognized argument",
    "unknown option",
)


def resolve_view_executable(executable: str = "rtl-buddy-view") -> str:
    """Return the path the viewer is invoked as.

    A bare name prefers the binary next to `sys.executable`, then falls back to the name for PATH lookup. Paths pass through. Every caller must resolve through here so the fingerprinted version matches the binary that runs.
    """
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return executable
    sibling = Path(sys.executable).parent / executable
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return executable


def probe_view_version(executable: str = "rtl-buddy-view") -> str | None:
    """Return the version string from `<executable> --version`, or None.

    None means the binary is missing, has no `--version`, or prints an unrecognised format. Treat it as unknown, not too old.
    """
    try:
        proc = subprocess.run(
            [resolve_view_executable(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = _VIEW_VERSION_RE.search((proc.stdout or "") + (proc.stderr or ""))
    return match.group(1) if match else None


def _is_non_source_filelist_line(line: str) -> bool:
    """Return whether a filelist line names no HDL source (`+incdir+`, `-y`, `-v`, `+libext+`, `+define+`, `.vlt`).

    Such lines must be dropped because `strip=True` would emit their argument as a bare source path.
    """
    s = line.strip()
    if s.startswith(("+incdir+", "+libext+", "+define+")):
        return True
    for prefix in ("-y", "-v"):
        if s.startswith(prefix) and len(s) > len(prefix) and s[len(prefix)].isspace():
            return True
    # Verilator config/waiver files are not HDL; the parser exits non-zero on them.
    if s.endswith(".vlt"):
        return True
    return False


class RtlBuddyView:
    """Generate a filelist and run `rtl-buddy-view`; one instance per `rb hier` invocation."""

    # Overridden by subclasses.
    _event_name = "hier"
    _status_label = "hier"
    _stream_stderr = False

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        format: str = "tree",
        output: str | None = None,
        frontend: str | None = None,
        cdc_annotations: str | None = None,
        rdc_annotations: str | None = None,
        axi_perf_annotations: str | None = None,
        clock_legend: bool = False,
        block_diagram: bool = False,
        executable: str = "rtl-buddy-view",
        test_cfg: TestConfig | None = None,
        test_suite_dir: str | None = None,
    ):
        self.name = name
        self.model_cfg = model_cfg
        self.format = format
        self.output = output
        self.frontend = frontend
        self.cdc_annotations = cdc_annotations
        self.rdc_annotations = rdc_annotations
        self.axi_perf_annotations = axi_perf_annotations
        self.clock_legend = clock_legend
        # Forwarded only when set so viewers that predate the flag see an unchanged command.
        self.block_diagram = block_diagram
        self.executable = executable
        self.test_cfg = test_cfg
        # Base for TB filelist entries; `suite_dir` is the artefact root, not this.
        self.test_suite_dir = test_suite_dir

        self.capture = False
        self.stdout: str | None = None
        self.stderr: str | None = None

        artefact_root = Path(suite_dir) / "artefacts" / "hier" / model_cfg.name
        if test_cfg is not None:
            # Keyed on (model, tb name): tests sharing a TB share the artefact.
            artefact_root = artefact_root / "tb" / test_cfg.tb.name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _event_fields(self) -> dict[str, object]:
        """Return the command-specific fields for the `.start` log event."""
        return {"format": self.format}

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "hier.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "hier.log")

    def _extra_filelist(self) -> list[str] | None:
        """Return filelist lines to merge on top of the model's, or None.

        The base class returns the test's testbench filelist. Subclasses override this for other extra HDL.
        """
        if self.test_cfg is not None:
            return self.test_cfg.tb.get_filelist()
        return None

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.model_cfg,
            output_path=fl_path,
        )
        # Filter extra lines first: `strip=True` would emit `+incdir+<dir>` as a bare directory path.
        extra = self._extra_filelist()
        test_filelist = None
        if extra is not None:
            test_filelist = [
                line for line in extra if not _is_non_source_filelist_line(line)
            ]
        vlog_fl.write_output(
            output_filepath=fl_path,
            unroll=True,
            strip=True,
            deduplicate=True,
            test_filelist=test_filelist,
            suite_dir=self.test_suite_dir,
        )
        return fl_path

    def _build_cmd(self, fl_path: str) -> list[str]:
        cmd = [
            self.executable,
            "--top",
            self.model_cfg.get_top(),
            "--filelist",
            fl_path,
            "--format",
            self.format,
        ]
        if self.test_cfg is not None:
            # A plain SV testbench leaves `toplevel` unset; its config name is the top module by convention.
            tb_top = self.test_cfg.tb.toplevel or self.test_cfg.tb.name
            cmd += ["--tb-top", tb_top]
        if self.output is not None:
            cmd += ["--output", self.output]
        if self.frontend is not None:
            cmd += ["--frontend", self.frontend]
        if self.cdc_annotations is not None:
            cmd += ["--cdc-annotations", self.cdc_annotations]
        if self.rdc_annotations is not None:
            cmd += ["--rdc-annotations", self.rdc_annotations]
        if self.axi_perf_annotations is not None:
            cmd += ["--overlay", f"axi-perf={self.axi_perf_annotations}"]
        if self.clock_legend:
            cmd += ["--clock-legend"]
        if self.block_diagram:
            cmd += ["--block-diagram"]
        return cmd

    def _captured_stderr(self, log_path: str, cmd_echo: str) -> str:
        """Return the viewer's stderr for this run, without the `cmd_echo` line.

        The text comes from memory in capture mode, otherwise from the log file. It is empty when stderr was streamed to the terminal or the log is unreadable. `cmd_echo` is stripped because it repeats every flag passed and would match any flag search.
        """
        if self.capture:
            return self.stderr or ""
        if self._stream_stderr:
            return ""
        try:
            text = Path(log_path).read_text()
        except OSError:  # pragma: no cover
            return ""
        return text.removeprefix(cmd_echo)

    def _check_block_diagram_supported(self, log_path: str, cmd_echo: str) -> None:
        """Raise a clear error when the viewer's stderr shows it rejected `--block-diagram`.

        The version is probed only on this failure path, so a dev build that supports the flag is never refused up front.
        """
        text = " ".join(self._captured_stderr(log_path, cmd_echo).split()).lower()
        if "--block-diagram" not in text:
            return
        if not any(marker in text for marker in _UNKNOWN_OPTION_MARKERS):
            return
        version = probe_view_version(self.executable)
        installed = (
            f"the installed viewer is {version}"
            if version
            else "the installed viewer predates it"
        )
        log_event(
            logger,
            logging.ERROR,
            f"{self._event_name}.tool_too_old",
            model=self.model_cfg.name,
            option="--block-diagram",
            required=VIEW_BLOCK_DIAGRAM_MIN_VERSION,
            installed=version,
        )
        raise FatalRtlBuddyError(
            f"hier: --block-diagram needs rtl-buddy-sch >= "
            f"{VIEW_BLOCK_DIAGRAM_MIN_VERSION} (rtl-buddy-sch#160), but "
            f"{installed}. Upgrade the renderer, or drop --block-diagram "
            f"to render the hierarchy instead:\n"
            f"    pip uninstall -y rtl-buddy-view && "
            f'pip install -U "rtl-buddy-sch >= '
            f'{VIEW_BLOCK_DIAGRAM_MIN_VERSION}"\n'
            f"The renderer's own message is in {log_path}."
        )

    def run(self) -> int:
        if os.sep in self.executable or (os.altsep and os.altsep in self.executable):
            if not (
                os.path.isfile(self.executable) and os.access(self.executable, os.X_OK)
            ):
                raise FatalRtlBuddyError(
                    f"hier: rtl-buddy-view not found or not executable: "
                    f"{self.executable}"
                )
        else:
            self.executable = resolve_view_executable(self.executable)
            if os.sep not in self.executable and shutil.which(self.executable) is None:
                raise FatalRtlBuddyError(
                    f"hier: '{self.executable}' not found on PATH or next to "
                    f"{sys.executable}; install rtl-buddy-sch into the active "
                    f"venv (the dist that ships this executable) or pass "
                    f"--tool to point at the binary"
                )

        if self.cdc_annotations is not None and not os.path.isfile(
            self.cdc_annotations
        ):
            raise FatalRtlBuddyError(
                f"hier: cdc-annotations file not found: {self.cdc_annotations}"
            )
        if self.axi_perf_annotations is not None and not os.path.isfile(
            self.axi_perf_annotations
        ):
            raise FatalRtlBuddyError(
                f"hier: axi-perf annotations file not found: "
                f"{self.axi_perf_annotations}"
            )

        if self.rdc_annotations is not None and not os.path.isfile(
            self.rdc_annotations
        ):
            raise FatalRtlBuddyError(
                f"hier: rdc-annotations file not found: {self.rdc_annotations}"
            )

        fl_path = self._write_filelist()
        cmd = self._build_cmd(fl_path)
        log_path = self._log_path()
        # First line of the log; kept so readers can strip it back off.
        cmd_echo = "$ " + " ".join(cmd) + "\n"

        with task_status(f"Running {self._status_label} {self.model_cfg.name}"):
            log_event(
                logger,
                logging.INFO,
                f"{self._event_name}.start",
                model=self.model_cfg.name,
                tool=self.executable,
                **self._event_fields(),
            )
            with open(log_path, "w") as logf:
                logf.write(cmd_echo)
                logf.flush()
                # Capture mode must not pass stdout through: it would corrupt an MCP stdio stream.
                if self.capture:
                    proc = run_managed_process(
                        cmd,
                        capture_output=True,
                        text=True,
                        cwd=self.artefact_dir,
                    )
                    self.stdout = proc.stdout or ""
                    self.stderr = proc.stderr or ""
                    logf.write(self.stderr)
                else:
                    stdout = subprocess.DEVNULL if self.output is not None else None
                    proc = run_managed_process(
                        cmd,
                        stdout=stdout,
                        stderr=None if self._stream_stderr else logf,
                        cwd=self.artefact_dir,
                    )

        if self.block_diagram and proc.returncode != 0:
            # Failure path only: a healthy render must not pay for a version probe.
            self._check_block_diagram_supported(log_path, cmd_echo)

        log_event(
            logger,
            logging.INFO,
            f"{self._event_name}.done",
            model=self.model_cfg.name,
            returncode=proc.returncode,
        )
        return proc.returncode


class RtlBuddyViewGraph(RtlBuddyView):
    """Generate a filelist and run `rtl-buddy-view graph`, writing the design-tier graph as node-link JSON.

    The viewer also writes a `graph-meta.json` sidecar. The filelist is shared with `rb hier`; stderr goes to `graph.log`.

    With `test_cfg` set the export is testbench-rooted: the DUT and TB filelists are merged, the artefact directory is keyed on (model, tb), and `--tb-top` is passed alongside `--top`.

    With `run_top` set the export is run-rooted, for a formal, synth or cdc run whose `top:` only elaborates inside the flow's own filelist. `run_filelist` is merged like a testbench filelist, `--tb-top` carries `run_top`, and the artefact directory is keyed on `run_key`. It is mutually exclusive with `test_cfg`.
    """

    _event_name = "graph_design"
    _status_label = "graph export"
    _stream_stderr = False

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        output: str,
        project_root: str,
        frontend: str | None = None,
        executable: str = "rtl-buddy-view",
        test_cfg: TestConfig | None = None,
        test_suite_dir: str | None = None,
        run_top: str | None = None,
        run_filelist: list[str] | None = None,
        run_key: str | None = None,
    ):
        super().__init__(
            name,
            model_cfg,
            suite_dir=suite_dir,
            output=output,
            frontend=frontend,
            executable=executable,
            test_cfg=test_cfg,
            test_suite_dir=test_suite_dir,
        )
        self.project_root = project_root
        self.run_top = run_top
        self.run_filelist = run_filelist
        if run_key is not None:
            # Own cache: the flow's extra sources make the filelist differ from the plain one.
            artefact_root = (
                Path(suite_dir) / "artefacts" / "hier" / model_cfg.name / run_key
            )
            artefact_root.mkdir(parents=True, exist_ok=True)
            self.artefact_dir = str(artefact_root)

    def tb_top(self) -> str | None:
        """Return the `--tb-top` this export elaborates from, or None.

        It is `run_top` if set, else the testbench's `toplevel:`, else its config name. The viewer corrects a name that is not a module, so read the elaborated top from the export.
        """
        if self.run_top is not None:
            return self.run_top
        if self.test_cfg is None:
            return None
        return self.test_cfg.tb.toplevel or self.test_cfg.tb.name

    def _extra_filelist(self) -> list[str] | None:
        if self.run_filelist is not None:
            return self.run_filelist
        return super()._extra_filelist()

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "graph.log")

    def log_path(self) -> str:
        """Return the path of the viewer's stderr log."""
        return self._log_path()

    def _event_fields(self) -> dict[str, object]:
        return {"output": self.output}

    def write_filelist(self) -> str:
        """Write the filelist without running the viewer, so callers can hash the sources first."""
        return self._write_filelist()

    def source_files(self) -> list[str]:
        """Return the absolute source paths in the generated filelist, resolved against its directory."""
        fl_path = self._filelist_path()
        files: list[str] = []
        try:
            lines = Path(fl_path).read_text().splitlines()
        except OSError:
            return files
        base = os.path.dirname(fl_path)
        for line in lines:
            entry = line.strip()
            if not entry or entry.startswith("//") or entry.startswith("#"):
                continue
            files.append(os.path.abspath(os.path.join(base, entry)))
        return files

    def meta_path(self) -> str:
        """Return the path of the provenance sidecar the viewer writes beside `--output`."""
        out = Path(self.output)
        return str(out.with_name(f"{out.stem}-meta.json"))

    def _build_cmd(self, fl_path: str) -> list[str]:
        cmd = [
            self.executable,
            "graph",
            "--filelist",
            fl_path,
            "--top",
            self.model_cfg.get_top(),
            "--output",
            str(self.output),
            "--project-root",
            self.project_root,
        ]
        tb_top = self.tb_top()
        if tb_top is not None:
            # `--top` stays the DUT so the viewer can mark the design under test.
            cmd += ["--tb-top", tb_top]
        if self.frontend is not None:
            cmd += ["--frontend", self.frontend]
        return cmd


_QUERY_VERBS = (
    "find-module",
    "subtree",
    "instances-of",
    "port-connections",
    "source-snippet",
)


class RtlBuddyViewQuery(RtlBuddyView):
    """Generate a filelist and run `rtl-buddy-view query <verb>`, which answers on stdout.

    Stderr streams to the terminal because a lookup miss is the answer. `query.log` records the invocation.
    """

    _event_name = "hier_query"
    _status_label = "hier-query"
    _stream_stderr = True

    def __init__(
        self,
        name: str,
        model_cfg: ModelConfig,
        *,
        suite_dir: str,
        verb: str,
        arg: str,
        frontend: str | None = None,
        subtree_format: str | None = None,
        context: int | None = None,
        line_numbers: bool = True,
        executable: str = "rtl-buddy-view",
        capture: bool = False,
    ):
        super().__init__(
            name,
            model_cfg,
            suite_dir=suite_dir,
            frontend=frontend,
            executable=executable,
        )
        self.capture = capture
        if verb not in _QUERY_VERBS:
            raise FatalRtlBuddyError(
                f"hier-query: unknown verb {verb!r}; "
                f"expected one of: {', '.join(_QUERY_VERBS)}"
            )
        self.verb = verb
        self.arg = arg
        # Forwarded only for the verbs that accept them; the viewer validates the rest.
        self.subtree_format = subtree_format
        self.context = context
        self.line_numbers = line_numbers

    def _event_fields(self) -> dict[str, object]:
        return {"verb": self.verb, "arg": self.arg}

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "query.log")

    def _build_cmd(self, fl_path: str) -> list[str]:
        cmd = [
            self.executable,
            "query",
            self.verb,
            self.arg,
            "--top",
            self.model_cfg.get_top(),
            "--filelist",
            fl_path,
        ]
        if self.frontend is not None:
            cmd += ["--frontend", self.frontend]
        if self.verb == "subtree" and self.subtree_format is not None:
            cmd += ["--format", self.subtree_format]
        if self.verb == "source-snippet":
            if self.context is not None:
                cmd += ["--context", str(self.context)]
            if not self.line_numbers:
                cmd += ["--no-line-numbers"]
        return cmd
