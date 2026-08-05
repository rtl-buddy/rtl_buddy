"""rtl-buddy-view tool wrapper.

Drives the standalone ``rtl-buddy-view`` CLI: hands it a generated
filelist for a model from ``models.yaml`` and forwards renderer
options. Same subprocess-granularity integration as
:mod:`tools.cdc_rtl_buddy` — rtl_buddy is not tied to the viewer's
Python API, and a viewer release can be picked up via ``uv sync``
without code changes here.

The viewer's stdout is streamed through to the user's stdout when
``-o`` is not supplied, so ``rb hier <model> --format dot | dot ...``
keeps working. Its stderr is captured into ``artefacts/hier/<model>/
hier.log`` alongside the generated filelist.
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

#: ``rtl-buddy-view --version`` prints ``rtl-buddy-view <version>``.
#: Unlike ``tool_manifest``'s probe this keeps the **whole** version
#: token, dev suffix included: the manifest only needs to compare
#: ``X.Y.Z`` against a floor, but a per-feature gate has to tell an
#: editable ``0.3.1.dev1+g<sha>`` build (which may well carry the
#: feature) apart from a released ``0.3.1`` (which cannot).
_VIEW_VERSION_RE = re.compile(r"rtl-buddy-view\s+(\S+)")

#: Seconds to wait for the version probe. A viewer that cannot answer
#: ``--version`` promptly is treated as unprobeable, not as a failure.
_VERSION_PROBE_TIMEOUT = 30


def probe_view_version(executable: str = "rtl-buddy-view") -> str | None:
    """Full version string reported by ``<executable> --version``.

    ``None`` when the binary is missing, too old to know the flag, or
    prints something unrecognizable. Callers must treat ``None`` as
    "unknown", never as "too old" — a pre-0.2.1 viewer has no
    ``--version`` at all, and refusing to run on that basis would be a
    guess where the subsequent invocation's exit code is the real answer.
    """
    try:
        proc = subprocess.run(
            [executable, "--version"],
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
    """Return True when ``line`` is a filelist directive that doesn't
    name a source file (include dirs, lib dirs, lib files) or names a
    file that isn't parseable HDL (Verilator config/waiver files).
    These must not survive into rtl-buddy-view's filelist because the
    renderer expects bare source paths and ``strip=True`` would emit
    the trailing argument as one — turning ``+incdir+../../common``
    into the bare directory ``../../common`` and crashing the
    parser with IsADirectoryError on the conventional testbench
    layout.
    """
    s = line.strip()
    if s.startswith("+incdir+") or s.startswith("+libext+"):
        return True
    # ``-y <dir>`` / ``-v <file>`` use a space (or tab) separator.
    for prefix in ("-y", "-v"):
        if s.startswith(prefix) and len(s) > len(prefix) and s[len(prefix)].isspace():
            return True
    # Verilator config/waiver files (``*.vlt``) are commonly listed
    # alongside sources in a testbench filelist (lint waivers scoped to
    # vendor code). They are not HDL — Verible's ``verible-verilog-syntax``
    # exits non-zero on them — so drop them before the merge.
    if s.endswith(".vlt"):
        return True
    return False


class RtlBuddyView:
    """Generates a filelist + invokes ``rtl-buddy-view``.

    Single-shot. Constructed per ``rb hier`` invocation.
    """

    # Overridden by :class:`RtlBuddyViewQuery`: the log-event prefix /
    # spinner label, and whether the viewer's stderr streams through to
    # the user's terminal instead of being captured into the log file.
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
        # Path to an ``axi-perf.json`` (the ``rb axi-profile run``
        # output for a given test). When set, rtl-buddy-view bakes
        # the per-bundle/interconnect throughput overlay AND emits a
        # top-level ``axi_perf.{source,test,suite_dir}`` block that
        # the SPA's "Open in marimo" button uses to skip its prompt
        # (Phase 2.5 of the marimo umbrella). Passed via the new
        # ``--overlay axi-perf=PATH`` form.
        self.axi_perf_annotations = axi_perf_annotations
        self.clock_legend = clock_legend
        self.executable = executable
        # Optional test that pins the TB top + TB filelist for the
        # TB-rooted view (#99 / 6b). When set, the generated filelist
        # is DUT+TB merged and rtl-buddy-view is invoked with both
        # ``--top <model>`` AND ``--tb-top <tb.toplevel>`` so the
        # rendered tree is rooted at the TB with the DUT recorded for
        # the SPA's dashed-boundary overlay. When None, today's
        # DUT-only invocation is byte-identical (no behavioural change
        # for the unconditional ``rb hier <model>`` callers).
        self.test_cfg = test_cfg
        # Directory the test's ``tests.yaml`` lives in. The TB filelist
        # entries (e.g. ``tb_axi_2x2.sv``, ``+incdir+../../common``) are
        # declared relative to it, so it must anchor their resolution.
        # ``suite_dir`` above is the *artefact* root (the hub passes the
        # project root there) and is the wrong base for the TB filelist
        # — without this, the merge resolves TB sources against the hub
        # process cwd and fails with ``FilelistError: <tb> does not
        # exist``. ``None`` falls back to cwd, the legacy behaviour for
        # callers that run from the suite dir.
        self.test_suite_dir = test_suite_dir

        # Set by callers that need the viewer's answer as a value rather
        # than on the terminal (``RtlBuddyViewQuery(capture=True)``, used
        # by ``rb mcp``). ``run()`` fills :attr:`stdout` / :attr:`stderr`.
        self.capture = False
        self.stdout: str | None = None
        self.stderr: str | None = None

        artefact_root = Path(suite_dir) / "artefacts" / "hier" / model_cfg.name
        if test_cfg is not None:
            # Cache key for TB mode is (model, tb_name). Two tests
            # sharing the same TB share the artefact — the test's
            # other parameters (plusargs, sweep) don't affect the
            # elaborated hierarchy, only its top + filelist do.
            artefact_root = artefact_root / "tb" / test_cfg.tb.name
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)

    def _event_fields(self) -> dict[str, object]:
        """Command-specific structured fields for the ``.start`` event.

        ``format`` is a render-only concept; the query subclass logs
        ``verb``/``arg`` instead so ``hier_query.start`` events don't
        carry a misleading constant ``format=tree``.
        """
        return {"format": self.format}

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "hier.f")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "hier.log")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.model_cfg,
            output_path=fl_path,
        )
        # rtl-buddy-view rejects +incdir+/-y/-f, so strip everything
        # down to plain source paths.
        #
        # TB mode merges the test's TB filelist on top of the model
        # filelist via VlogFilelist's existing test_filelist parameter
        # (the same merge the compile flow uses). Order is DUT first,
        # TB second — the TB modules instantiate the DUT, not the
        # other way around, and Verible elaborates from the
        # ``--tb-top`` regardless of file order.
        #
        # Drop non-source entries (``+incdir+...``, ``-y .../``,
        # ``+libext+...``) from the TB filelist before merging. With
        # ``strip=True`` VlogFilelist would emit them as bare paths
        # which rtl-buddy-view then tries to open as source files,
        # producing ``IsADirectoryError`` on the typical
        # ``+incdir+../../common`` testbench convention. rtl-buddy-view
        # works on absolute source paths and does not need include
        # directories — the TB's compile-time options are not relevant
        # to its CST walk.
        test_filelist = None
        if self.test_cfg is not None:
            test_filelist = [
                line
                for line in self.test_cfg.tb.get_filelist()
                if not _is_non_source_filelist_line(line)
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
            self.model_cfg.name,
            "--filelist",
            fl_path,
            "--format",
            self.format,
        ]
        if self.test_cfg is not None:
            # ``--tb-top`` is independent of ``--top`` (rtl-buddy-view
            # #99 / 6a). When both are supplied, the renderer elaborates
            # from --tb-top and records the DUT name in
            # ``view.json::dut_top`` so the SPA can mark the DUT
            # subtree with a dashed boundary.
            #
            # ``toplevel`` is the explicit top override — set for cocotb
            # / SystemC harnesses, but conventionally unset for a plain
            # SystemVerilog testbench (Verilator auto-detects the top at
            # sim time). The view has no elaboration to auto-detect from,
            # so fall back to the testbench config name, which by
            # convention is the TB's top module name (e.g. ``tb_axi_2x2``).
            # Without this fallback a plain-SV testbench silently rendered
            # DUT-rooted, so clicking "TB" in the SPA showed the DUT view
            # with no AXI overlay. rtl-buddy-view fails loudly with a
            # "top module not found" error if the convention doesn't hold,
            # so a mismatch surfaces as a clear 500 rather than a silent
            # wrong render.
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
        return cmd

    def run(self) -> int:
        # Resolve the viewer up-front. Bare names (no '/') go through
        # PATH lookup; an absolute or relative path is checked for
        # existence + executability. Without this, a missing binary
        # surfaces as an unhandled Python traceback from subprocess.
        if os.sep in self.executable or (os.altsep and os.altsep in self.executable):
            if not (
                os.path.isfile(self.executable) and os.access(self.executable, os.X_OK)
            ):
                raise FatalRtlBuddyError(
                    f"hier: rtl-buddy-view not found or not executable: "
                    f"{self.executable}"
                )
        else:
            # rb is routinely invoked by absolute venv path with no
            # activation (agents, cron, scripts) — PATH then knows
            # nothing about the venv, but the viewer installed next to
            # this interpreter is exactly the one that belongs to it.
            sibling = Path(sys.executable).parent / self.executable
            if sibling.is_file() and os.access(sibling, os.X_OK):
                self.executable = str(sibling)
            elif shutil.which(self.executable) is None:
                raise FatalRtlBuddyError(
                    f"hier: '{self.executable}' not found on PATH or next to "
                    f"{sys.executable}; install rtl-buddy-view into the active "
                    f"venv or pass --tool to point at the binary"
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
                logf.write("$ " + " ".join(cmd) + "\n")
                logf.flush()
                # Let the renderer's stdout pass through to the user's
                # terminal when --output is not used; capture stderr in
                # the log for diagnosis. Queries stream stderr through
                # instead — a lookup miss ("instance path ... not
                # found") is an interactive answer, not a diagnostic to
                # bury in a log file.
                #
                # ``capture`` overrides both: an in-process caller (the
                # MCP server) needs the answer as a string, and under a
                # stdio transport a passed-through stdout would be
                # written straight into the JSON-RPC stream.
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

        log_event(
            logger,
            logging.INFO,
            f"{self._event_name}.done",
            model=self.model_cfg.name,
            returncode=proc.returncode,
        )
        return proc.returncode


class RtlBuddyViewGraph(RtlBuddyView):
    """Generates a filelist + invokes ``rtl-buddy-view graph``.

    The **design tier** of the knowledge graph (rtl_buddy#375 /
    rtl-buddy-view#126): module / instance / port / parameter /
    interface / modport nodes written as node-link JSON, plus the
    viewer's own ``graph-meta.json`` provenance sidecar beside it.

    Shares ``rb hier``'s ``artefacts/hier/<model>/hier.f`` — the input
    to a graph export is exactly the input to a render, so generating a
    second filelist would only create a way for the two to disagree.
    The viewer's stdout is suppressed (we always pass ``--output``) and
    its stderr is captured to ``graph.log`` next to the filelist.

    With ``test_cfg`` set the export is **TB-rooted** (#377 follow-up):
    the parent's DUT+TB filelist merge runs, the artefact dir keys on
    ``(model, tb)`` exactly as ``rb hier --view tb`` does, and
    ``--tb-top`` is passed alongside ``--top`` so the viewer elaborates
    from the testbench and records the DUT name in
    ``graph.design.dut_top``. Module ids are ``module:<name>`` either
    way, which is what welds the TB export's DUT subtree onto the
    DUT-rooted export at merge time.
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

    def tb_top(self) -> str | None:
        """The ``--tb-top`` this export will elaborate from, or None.

        Same convention as :class:`RtlBuddyView`: the testbench's
        explicit ``toplevel:`` when it has one, else its config name
        (which is the TB's top module name by project convention). The
        viewer auto-corrects a hint that names no real module, so the
        elaborated answer is read back off the export rather than
        trusted from here.
        """
        if self.test_cfg is None:
            return None
        return self.test_cfg.tb.toplevel or self.test_cfg.tb.name

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "graph.log")

    def log_path(self) -> str:
        """Where the viewer's stderr lands, for citing in a failure report.

        ``rb graph build`` records this in ``graph-meta.json`` when a
        model fails to export, so the public accessor exists rather than
        having the orchestrator reach for ``_log_path``.
        """
        return self._log_path()

    def _event_fields(self) -> dict[str, object]:
        return {"output": self.output}

    def write_filelist(self) -> str:
        """Generate the filelist without invoking the viewer.

        ``rb graph build`` hashes the model's sources *before* deciding
        whether an export is needed at all, and the filelist is where
        that source list comes from. Writing it is cheap (no parse), and
        :meth:`run` regenerates it identically, so calling this first
        costs nothing and keeps the no-op check honest.
        """
        return self._write_filelist()

    def source_files(self) -> list[str]:
        """Absolute source paths in the generated filelist.

        The filelist is written with ``strip=True``, so every non-empty,
        non-comment line is a bare path — relative ones resolve against
        the filelist's own directory, which is how the viewer reads them.
        """
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
        """Where the viewer writes its provenance sidecar for ``--output``."""
        out = Path(self.output)
        return str(out.with_name(f"{out.stem}-meta.json"))

    def _build_cmd(self, fl_path: str) -> list[str]:
        cmd = [
            self.executable,
            "graph",
            "--filelist",
            fl_path,
            "--top",
            self.model_cfg.name,
            "--output",
            str(self.output),
            "--project-root",
            self.project_root,
        ]
        tb_top = self.tb_top()
        if tb_top is not None:
            # ``--tb-top`` roots the export at the testbench; ``--top``
            # stays the DUT so the viewer can record which subtree is
            # the design under test. Both names land in
            # ``graph.design``.
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
    """Generates a filelist + invokes ``rtl-buddy-view query <verb>``.

    The CLI face of the viewer's query API (rtl_buddy#198): JSON (or
    snippet text) answers on stdout, for shell pipelines and agent
    tool use. Reuses the parent's filelist generation and artefact
    layout (``artefacts/hier/<model>/hier.f`` is identical for both
    commands), but streams stderr through to the terminal — a lookup
    miss is the answer to the user's question, not a diagnostic to
    capture. ``query.log`` records the invocation alongside hier.log.
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
        # Verb-specific knobs; only forwarded for the verbs that
        # accept them so the viewer's own usage validation stays the
        # single source of truth for what combines with what.
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
            self.model_cfg.name,
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
