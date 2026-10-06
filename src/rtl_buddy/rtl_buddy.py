# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import typer
from importlib.metadata import version
from typing_extensions import Annotated
import click

from .config import (
    RegConfig,
    RootConfig,
    SuiteConfig,
    TestConfig,
    parse_plusarg_overrides,
)
from .config.env_file import apply_env_file
from .config.root import (
    REG_CFG_PATH_KEYS,
    _discover_root_cfg,
    discover_project_root,
    load_reg_cfg_paths,
    resolve_reg_cfg_path,
)
from .config.cdc import CdcRegConfig, CdcSuiteConfig
from .config.lint import LintRegConfig, LintSuiteConfig
from .config.elab import ElabConfig, ElabRegConfig
from .config.fpga import FpgaRegConfig, FpgaSuiteConfig
from .config.fpv import FpvRegConfig, FpvSuiteConfig
from .config.mut import MutSuiteConfig
from .config.model import ModelConfig, ModelConfigLoader
from .config.pnr import GdsMode, PnrSuiteConfig
from .config.power import PowerRegConfig, PowerSuiteConfig
from .config.synth import SynthRegConfig, SynthSuiteConfig
from .cov import model as cov_model
from .cov import query as cov_query_mod
from .cov.raw import METRICS as cov_metrics
from .docs_access import get_page, get_section, list_pages
from .graph import build as graph_build_mod
from .graph import coverage as graph_coverage_mod
from .graph import extract as extract_mod
from .graph import query as graph_query_mod
from .graph import results as graph_results_mod
from .mcp import server as mcp_server_mod
from .phys import manifest as phys_manifest_mod
from .phys import query as phys_query_mod
from .mcp import toolset as mcp_toolset_mod
from .artifact_lock import ArtifactLocks
from .errors import FatalRtlBuddyError, FilelistError
from .exec_context import ExecutionContext
from .hooks import exec_hook_script
from .logging_utils import (
    attach_file_log,
    emit_console_text,
    is_machine_mode,
    log_console_event,
    log_event,
    render_summary,
    set_print_failures_only,
    setup_logging,
)
from .process_utils import cancellation_has_started, terminate_live_managed_processes
from .runner.cdc_runner import CdcRunner
from .runner.lint_runner import LintRunner
from .runner.cdc_results import CdcSkipResults
from .runner.lint_results import LintSkipResults
from .runner.elab_runner import ElabRunner
from .runner.elab_results import (
    ElabResults,
    elab_failure,
    load_elab_result_json,
    write_elab_result_json_best_effort,
)
from .runner.fpv_runner import FpvRunner
from .runner.fpv_results import FpvSkipResults
from .runner.mut_runner import MutRunner
from .runner.mut_results import MutResults
from .config.dispatch import (
    ORPHANS_POLICIES,
    JobResources,
    aggregate_verilate_resources,
    combine_for_in_job_compile,
    compile_parallel,
    compile_parallel_origin,
    compile_resource_origins,
    compile_split_verilate,
    aggregate_compile_resources,
    mode_governed_fields,
    resolve_compile_resources,
    resolve_verilate_resources,
    resolve_resources,
    cpu_request_overrides,
)
from .dispatch import (
    LocalProcessBackend,
    create_dispatch_backend,
    validate_backend_name,
)
from .dispatch.argv import job_log_path
from .dispatch.base import (
    BUILD_PHASE_BUILD,
    BUILD_PHASE_FULL,
    BUILD_PHASE_VERILATE,
    BUILD_PHASES,
    BuildJobSpec,
    ElabJobSpec,
    TestJobSpec,
    telemetry_key,
)
from .dispatch.gates import release_batches, wait_for_gates, write_gates
from .dispatch.plan import (
    PLAN_SCHEMA_VERSION,
    read_plan_config,
    read_plan_configs,
    read_plan_master_seed,
    read_plan_token,
    run_scoped_path,
    write_plan,
)
from .dispatch.progress import group_job_ids
from .dispatch.run_manifest import (
    STATUS_CANCELLED,
    STATUS_COLLECTED,
    STATUS_STALE,
    STATUS_SUBMITTING,
    build_from,
    finish_submission,
    discover_run_manifests,
    handles_from,
    pending_from,
    row_identities,
    record_build_handle,
    record_pending_handles,
    record_verilate_handle,
    run_manifest_path,
    set_run_status,
    update_pending_job_ids,
    verilate_from,
    write_run_manifest,
)
from .dispatch.retry import backoff_delay, classify_missing_result
from .dispatch.rightsize import (
    analyze_build_reservation,
    analyze_suite_reservations,
)
from .dispatch.slurm import RELEASE_BUDGET_S, release_dependency
from .runner.result_io import (
    BUILD_COMPILE_FAIL_PREFIX,
    COMPILE_ERROR_TAIL_LINES,
    attach_result_key,
    attach_telemetry_json,
    build_compile_fail_desc,
    compile_error_tail,
    load_build_result_json,
    load_result_json,
    refresh_result_json,
    write_build_result_json,
    write_result_json,
)
from .runner.test_results import (
    COMPILE_FAIL_DESC,
    CompileFailResults,
    DispatchFailResults,
    EarlyStopResults,
    SetupFailResults,
    EARLY_STOP_KEY,
    SkipResults,
    is_run_failure,
)
from .runner.test_runner import RunDepth, TestRunner
from .runner.xfail import apply_xfail, xfail_refusal
from .runner.fpga_runner import FpgaRunner
from .runner.fpga_results import FpgaSkipResults
from .runner.pnr_runner import PnrExportRunner, PnrRunner
from .runner.pnr_plan import OnceMap, PlannedRun, plan_pnr_runs, run_plan
from .runner.pnr_results import PnrFailResults, PnrSkipResults
from .runner.power_runner import PowerRunner
from .runner.power_results import PowerSkipResults
from .runner.synth_runner import SynthRunner
from .runner.synth_results import SynthSkipResults
from .seed_mode import SeedMode
from .seeding import (
    SEED_DERIVATION_VERSION,
    SeedResolution,
    expanded_test_seed_identity,
    suite_seed_identity,
    validate_master_seed,
    validate_resolved_seed,
    resolve_test_seed,
)
from .hub.cli import app as hub_app
from .skill_install import app as skill_app
from .tools.axi_profile_rtl_buddy import (
    RtlBuddyAxiProfileDiscover,
    RtlBuddyAxiProfileGenMonitor,
    RtlBuddyAxiProfileNotebook,
    RtlBuddyAxiProfileRun,
)
from .tools.coverage import CoverageReporter
from .tools.artifact_paths import (
    RESULT_JSON_NAME,
    run_artifact_root,
    test_artifact_dir,
    validate_run_tag,
)
from .tools.hier_rtl_buddy_view import (
    VIEW_BLOCK_DIAGRAM_MIN_VERSION,
    RtlBuddyView,
    RtlBuddyViewQuery,
    probe_view_version,
)
from .tools.pnr_openroad import DEFAULT_PNG_HEIGHT, DEFAULT_PNG_WIDTH
from .tools.spec_trace import (
    all_spec_blocks,
    build_coverage_map,
    build_spec_to_models_map,
    discover_fpv_verifications,
    discover_model_configs,
    discover_spec_configs,
    discover_suite_tests,
)
from .tools.verible import Verible
from .tools.vlog_filelist import VlogFilelist, apply_exclude_globs
from .tools.vlog_sim import (
    SHARED_BUILD_ROOT_ENV,
    resolve_shared_build_root,
    share_build_unsupported_reason,
)
from .config.xplr import load_xplr_config
from .xplr import analysis as xplr_analysis
from .xplr import commands as xplr_commands
from .xplr import dumps_record
from .xplr import gitprov as xplr_gitprov
from .xplr import ledger as xplr_ledger
from .xplr import mockflow as xplr_mockflow

logger = logging.getLogger(__name__)


def _dispatch_suite_identity(config_path: str | Path) -> str:
    """Stable filesystem component identifying one resolved suite config."""
    resolved = Path(config_path).resolve()
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", resolved.stem).strip("._-")
    stem = (stem or "suite")[:48]
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:12]
    return f"{stem}-{digest}"


def _raise_first(findings: list) -> list:
    """Stable sort with every ``raise`` finding ahead of every ``reduce``."""
    return sorted(findings, key=lambda f: f.direction != "raise")


def _log_reservation_advice(findings: list) -> None:
    for finding in findings:
        fields = {k: v for k, v in finding.as_event().items() if k != "event"}
        log_event(logger, logging.INFO, "rightsize.advice", **fields)


def _replace_environ(snapshot: dict) -> None:
    """Make ``os.environ`` equal to ``snapshot`` (keys removed and restored)."""
    for key in list(os.environ):
        if key not in snapshot:
            del os.environ[key]
    os.environ.update(snapshot)


def _graph_where(node: dict) -> str:
    """``file:line`` for a graph node summary, or ``-``."""
    file_path = node.get("file")
    if not file_path:
        return "-"
    line = node.get("line")
    return f"{file_path}:{line}" if line is not None else str(file_path)


def _pnr_worst_corner_cell(res: dict) -> str:
    """The `Worst Corner` column for one multi-corner P&R row.

    Names the corner that set the setup WNS and the one that set the hold WNS. A
    single-corner row in a mixed table reads `-`.
    """
    setup = res.get("worst_setup_corner")
    hold = res.get("worst_hold_corner")
    if setup is None and hold is None:
        return "-"
    return f"setup {setup or '-'} / hold {hold or '-'}"


def _pnr_outputs_cell(res: dict) -> str:
    """The `Outputs` column for one P&R row, qualified when the layout is incomplete.

    Cells with no GDS are a shortfall unless the run's `gds-allow-empty` covers them, in
    which case they are reported as intentionally empty.
    """
    tags = []
    if res.get("gds_path"):
        tags.append("gds")
    if res.get("png_path"):
        tags.append("png")
    if res.get("abstract_dir"):
        tags.append("abstract")
    text = "+".join(tags) if tags else "-"

    def _qualified(note: str) -> str:
        # No tags to qualify, so the note stands alone.
        return f"{text} ({note})" if tags else note

    if res.get("gds_status") == "failed":
        return _qualified("export failed")
    missing = res.get("gds_missing_cell_count")
    if missing:
        return _qualified(f"incomplete: {missing} missing")
    empty = len(res.get("gds_allowed_empty_cells") or [])
    if empty:
        return _qualified(f"{empty} empty by design")
    return text


def _line_ratio_text(coverage: dict | None) -> str:
    """One test's line coverage as a percentage, or ``-``."""
    ratio = ((coverage or {}).get("totals") or {}).get("line", {}).get("ratio")
    return "-" if ratio is None else f"{ratio * 100:.1f}%"


def _explain_coverage_lines(entry: dict | None, run: dict | None) -> list[str]:
    """`rb graph explain`'s coverage lines, or none.

    Machine mode returns the whole entry; the console gets the verdict and the manifest
    behind it.
    """
    if not entry:
        return []
    if entry.get("kind") == "design":
        head = f"  cov:    {_line_ratio_text(entry)} line ({entry.get('module')})"
    else:
        observed = entry.get("observed") or []
        detail = ", ".join(
            f"{record.get('name')} ×{record.get('hits', 0)} [{record.get('match')}]"
            for record in observed
        )
        head = (
            f"  cov:    {entry.get('status')} "
            f"({entry.get('hits', 0)} hit(s)"
            f"{'; ' + detail if detail else '; no cover point in the model'})"
        )
    lines = [head]
    manifest = (run or {}).get("manifest")
    if manifest:
        lines.append(f"  from:   {manifest}")
    return lines


def _summarize_compile_work(build_entries) -> dict:
    """Summarize how much real compiling a build envelope's ``builds`` list describes.

    Returns ``{"records": n, "compiled": n, "compiled_sec": float}``. A record counts as
    compiled only when ``reused is False``: ``True`` is a stamp short-circuit and
    ``None`` is a config that never reached a builder. A non-numeric duration
    contributes nothing to the total.
    """
    records = 0
    compiled = 0
    compiled_sec = 0.0
    for entry in build_entries:
        records += 1
        if entry.get("reused") is not False:
            continue
        compiled += 1
        duration = entry.get("duration_sec")
        # bool is an int in Python; a flag is not a time.
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            compiled_sec += float(duration)
    return {
        "records": records,
        "compiled": compiled,
        "compiled_sec": round(compiled_sec, 2),
    }


def _annotate_build_failure(entry, *, failure, worker_error, suite_dir):
    """Add the failure keys to one ``builds`` record, in place.

    Adds ``returncode``, ``fingerprint_sha``, ``transcript`` and ``error_tail`` for a
    failed build, only for the fields that failure has: a config that never reached a
    builder has no returncode.

    ``transcript`` is suite-relative so the artifact does not pin the compute node's
    mount. ``error_tail`` is read from the absolute path on the node that wrote it.
    """
    failure = failure or {}
    returncode = failure.get("returncode")
    if returncode is not None:
        entry["returncode"] = returncode
    fingerprint_sha = failure.get("fingerprint_sha")
    if fingerprint_sha is not None:
        # Identity of the failed inputs, so a gated sim job can tell an unchanged
        # compile from a changed one.
        entry["fingerprint_sha"] = fingerprint_sha
    transcript = failure.get("transcript")
    if transcript:
        entry["transcript"] = os.path.relpath(transcript, suite_dir)
        tail = compile_error_tail(transcript)
        if tail:
            entry["error_tail"] = tail
    elif failure.get("error_tail"):
        # No builder and no transcript, but the failure carries its own account
        # (group-adopt drift).
        entry["error_tail"] = list(failure["error_tail"])[-COMPILE_ERROR_TAIL_LINES:]
    elif worker_error:
        # One element per non-blank line: consumers treat each `error_tail` element as
        # one summary line.
        # Tail-capped like the transcript path.
        lines = [
            line.strip() for line in str(worker_error).splitlines() if line.strip()
        ]
        if lines:
            entry["error_tail"] = lines[-COMPILE_ERROR_TAIL_LINES:]
    return entry


class RtlBuddy:
    """Command-line entry point for rtl_buddy."""

    _GIT_COMMANDS = {
        "test",
        "randtest",
        "regression",
        "filelist",
        "wave",
        "wave-fpv",
        "synth",
        "synth-regression",
        "power",
        "power-regression",
        "fpga",
        "fpga-regression",
        "cdc",
        "cdc-regression",
        "fpv",
        "fpv-regression",
        "hier",
        "hier-query",
        "elab",
        "elab-regression",
    }

    def cb_builder(value: str | None) -> str | None:
        if value is None:
            return value

        try:
            configured_builders = RootConfig.discover_rtl_builder_names()
        except ValueError as e:
            raise typer.BadParameter(f"Cannot validate builder override: {e}") from e

        if value not in configured_builders:
            raise typer.BadParameter(
                f"Choose from configured builders: [{', '.join(configured_builders)}]"
            )
        return value

    def cb_version(value: bool):
        if value:
            if "--machine" in sys.argv:
                print(
                    json.dumps(
                        {
                            "command": "version",
                            "exit_code": 0,
                            "meta": {
                                "rtl_buddy_version": version("rtl-buddy"),
                                "argv": sys.argv[:],
                                "cwd": os.getcwd(),
                                "git": None,
                            },
                            "payload": {},
                        },
                        ensure_ascii=True,
                    )
                )
            else:
                print(f"rtl_buddy v{version('rtl-buddy')}")
            raise typer.Exit()

    def __init__(self, name):
        self.app = typer.Typer(no_args_is_help=True)
        self.docs_app = typer.Typer(
            help="browse bundled rtl_buddy documentation", no_args_is_help=True
        )
        self.spec_app = typer.Typer(
            help="spec traceability commands", no_args_is_help=True
        )
        self.mut_app = typer.Typer(
            help="mutation testing (rb mut)", no_args_is_help=True
        )
        self.axi_profile_app = typer.Typer(
            help=("profile AXI interconnect performance via rtl-buddy-axi-profiler"),
            no_args_is_help=True,
        )
        self.app.callback()(self.root_options)
        self.app.command("test", help="run a simple test")(self.do_cmd_test)
        self.app.command("randtest", help="repeat a test with multiple random seeds")(
            self.do_rand_test
        )
        self.app.command("regression", help="run rtl regression")(
            self.do_rtl_regression
        )
        self.app.command("elab", help="elaborate a model with pyslang")(
            self.do_cmd_elab
        )
        self.app.command(
            "elab-regression", help="run named model elaboration profiles"
        )(self.do_elab_regression)
        self.app.command(
            "_elab-job",
            hidden=True,
            help="internal: elaborate one model/profile and write its result JSON",
        )(self.do_cmd_elab_job)
        # Remote-dispatch re-entry point: runs one (test, run_id) and serializes the
        # result for the head.
        self.app.command(
            "_test-job",
            hidden=True,
            help="internal: run one (test, run_id) and write its result JSON",
        )(self.do_cmd_test_job)
        # Remote-dispatch build job: compiles the suite's shared simv on a compute node.
        self.app.command(
            "_build-job",
            hidden=True,
            help="internal: compile a suite's runnable tests (share-build)",
        )(self.do_cmd_build_job)
        self.app.command("filelist", help="generate filelists using models.yaml")(
            self.do_gen_model_filelist
        )
        self.app.command("hier", help="render module hierarchy via rtl-buddy-view")(
            self.do_cmd_hier
        )
        self.app.command(
            "hier-query",
            help="query the module hierarchy via rtl-buddy-view "
            "(find-module, subtree, instances-of, port-connections, "
            "source-snippet); JSON on stdout",
        )(self.do_cmd_hier_query)
        self.app.command(
            "mcp",
            help=(
                "serve the knowledge graph, test status, coverage, physical "
                "metrics and hierarchy queries (plus the live session when a "
                "hub runs) over MCP (stdio); needs the 'mcp' extra"
            ),
        )(self.do_cmd_mcp)
        self.graph_app = typer.Typer(
            help=(
                "design knowledge graph: one queryable graph.json stitching "
                "the design tier (rtl-buddy-view), the config tier "
                "(tests/models/specs) and, when installed, the extractor's "
                "binding tier (rtl-buddy-graph-extract)"
            ),
            no_args_is_help=True,
        )
        self.graph_app.command(
            "build",
            help="extract every tier and merge them into artefacts/graph/graph.json",
        )(self.do_graph_build)
        self.graph_app.command(
            "results",
            help=(
                "refresh artefacts/graph/results-overlay.json — last status, "
                "seed and artefact paths per test node; graph.json is not touched"
            ),
        )(self.do_graph_results)
        self.graph_app.command(
            "query",
            help=(
                "keyword search over graph.json with neighbourhood expansion "
                "and the results overlay joined in"
            ),
        )(self.do_graph_query)
        self.graph_app.command(
            "path",
            help="shortest chain of edges between two graph nodes",
        )(self.do_graph_path)
        self.graph_app.command(
            "explain",
            help="one node's attributes, every edge on it, and its last result",
        )(self.do_graph_explain)
        self.app.add_typer(
            self.graph_app,
            name="graph",
            help="build the design knowledge graph",
        )
        self.cov_app = typer.Typer(
            help=(
                "read a finished run's cov_dir/manifest.json: per-file, "
                "per-point line/branch/toggle/expression coverage with "
                "per-test attribution; runs no simulator"
            ),
            no_args_is_help=True,
        )
        self.cov_app.command(
            "summary",
            help="run-level and per-test scalars, coldest files first",
        )(self.do_cov_summary)
        self.cov_app.command(
            "module",
            help="per-file, per-point coverage for one module's sources",
        )(self.do_cov_module)
        self.app.add_typer(
            self.cov_app,
            name="cov",
            help="query coverage artefacts already on disk",
        )
        self.phys_app = typer.Typer(
            help=(
                "read a finished run's phys-manifest.json: per-module cells "
                "and area, per-instance power; runs no tools"
            ),
            no_args_is_help=True,
        )
        self.phys_app.command(
            "runs",
            help="every run with physical artefacts under the project, newest first",
        )(self.do_phys_runs)
        self.phys_app.command(
            "summary",
            help="the run's totals, its heaviest modules and its hottest instances",
        )(self.do_phys_summary)
        self.phys_app.command(
            "module",
            help="one module's cells and area, and the instances of it with power",
        )(self.do_phys_module)
        self.phys_app.command(
            "instance",
            help="one instance's power, or the rolled-up subtree under its path",
        )(self.do_phys_instance)
        self.app.add_typer(
            self.phys_app,
            name="phys",
            help="query physical artefacts already on disk",
        )
        self.axi_profile_app.command(
            "run",
            help="ingest a test's FST and emit per-test axi-perf.json",
        )(self.do_cmd_axi_profile_run)
        self.axi_profile_app.command(
            "discover",
            help="parse RTL to (re)generate the model's axi-bundles.yaml manifest",
        )(self.do_cmd_axi_profile_discover)
        self.axi_profile_app.command(
            "gen-monitor",
            help="emit the SV bind-style AXI monitor for the model's testbench",
        )(self.do_cmd_axi_profile_gen_monitor)
        self.axi_profile_app.command(
            "notebook",
            help="launch the packaged marimo notebook against a test's per-txn parquet",
        )(self.do_cmd_axi_profile_notebook)
        self.app.add_typer(
            self.axi_profile_app,
            name="axi-profile",
            help=("profile AXI interconnect performance via rtl-buddy-axi-profiler"),
        )
        self.verible_app = typer.Typer(
            help="verible tooling and filelist generation", no_args_is_help=True
        )
        # Passthrough subcommands forward unrecognised arguments to verible, so
        # `--rules_config=x` needs no `--`.
        _verible_passthrough_ctx = {
            "allow_extra_args": True,
            "ignore_unknown_options": True,
        }
        self.verible_app.command(
            "lint",
            help="run verible-verilog-lint",
            context_settings=_verible_passthrough_ctx,
        )(self.do_verible_lint)
        self.verible_app.command(
            "syntax",
            help="run verible-verilog-syntax",
            context_settings=_verible_passthrough_ctx,
        )(self.do_verible_syntax)
        self.verible_app.command(
            "format",
            help="run verible-verilog-format",
            context_settings=_verible_passthrough_ctx,
        )(self.do_verible_format)
        self.verible_app.command(
            "preprocessor",
            help="run verible-verilog-preprocessor",
            context_settings=_verible_passthrough_ctx,
        )(self.do_verible_preprocessor)
        self.verible_app.command(
            "filelist",
            help="generate verible.filelist from models.yaml so verible-verilog-ls "
            "can resolve cross-file symbols",
        )(self.do_verible_filelist)
        self.app.add_typer(self.verible_app, name="verible", help="verible commands")
        self.app.command("wave", help="open waveform viewer for a test")(
            self.do_cmd_wave
        )
        self.app.command(
            "wave-fpv",
            help="open SymbiYosys counterexample VCD for a failed FPV verification",
        )(self.do_cmd_wave_fpv)
        self.app.command(
            "nvim-install",
            help="install/update the unified rtl-buddy-nvim editor plugin "
            "(hub + wave annotation)",
        )(self.do_nvim_install)
        self.app.command("wave-install-nvim", help="alias for nvim-install")(
            self.do_nvim_install
        )
        self.app.command("synth", help="run synthesis")(self.do_cmd_synth)
        self.app.command("synth-regression", help="run synthesis regression")(
            self.do_synth_regression
        )
        self.app.command("pnr", help="run place-and-route")(self.do_cmd_pnr)
        self.app.command(
            "pnr-export",
            help="export GDS/PNG from a saved P&R result (no synthesis, no OpenROAD)",
        )(self.do_cmd_pnr_export)
        self.app.command("power", help="run power analysis")(self.do_cmd_power)
        self.app.command("power-regression", help="run power analysis regression")(
            self.do_power_regression
        )
        self.app.command(
            "fpga", help="run FPGA implementation (synth + place + route)"
        )(self.do_cmd_fpga)
        self.app.command("fpga-regression", help="run FPGA implementation regression")(
            self.do_fpga_regression
        )
        self.app.command("saif", help="convert FST/VCD trace to SAIF v2.0")(
            self.do_cmd_saif
        )
        self.app.command("cdc", help="run CDC lint")(self.do_cmd_cdc)
        self.app.command("cdc-regression", help="run CDC lint regression")(
            self.do_cdc_regression
        )
        self.app.command("lint", help="run style lint (verible)")(self.do_cmd_lint)
        self.app.command("lint-regression", help="run style lint regression")(
            self.do_lint_regression
        )
        self.app.command("fpv", help="run formal property verification")(
            self.do_cmd_fpv
        )
        self.app.command("fpv-regression", help="run FPV regression")(
            self.do_fpv_regression
        )
        self.mut_app.command(
            "list", help="enumerate mutation candidate sites without mutating"
        )(self.do_mut_list)
        self.mut_app.command(
            "run", help="generate mutants, score against an FPV proof, report"
        )(self.do_mut_run)
        self.mut_app.command(
            "score", help="recompute mutation score from a saved report"
        )(self.do_mut_score)
        self.app.add_typer(self.mut_app, name="mut", help="mutation testing")
        self.app.add_typer(hub_app, name="hub", help="manage the rtl-buddy-hub daemon")
        self.app.add_typer(
            skill_app, name="skill", help="manage the rtl_buddy agent skill"
        )
        self.docs_app.command("list", help="list bundled documentation pages")(
            self.do_docs_list
        )
        self.docs_app.command("show", help="show a bundled documentation page")(
            self.do_docs_show
        )
        self.app.add_typer(
            self.docs_app, name="docs", help="browse bundled documentation"
        )
        self.spec_app.command(
            "list", help="list all spec blocks discovered in the project"
        )(self.do_spec_list)
        self.spec_app.command(
            "check-design",
            help="show which spec blocks have design models referencing them",
        )(self.do_spec_check_testplan)
        self.spec_app.command(
            "check-coverage",
            help="show which spec coverage items are addressed by tests",
        )(self.do_spec_check_coverage)
        self.app.add_typer(
            self.spec_app, name="spec", help="spec traceability commands"
        )
        self.xplr_app = typer.Typer(
            help=(
                "tool-agnostic experiment ledger for design-space exploration. "
                "A bookkeeper, not an optimizer: you declare the knob deltas "
                "and flow outcomes; it pins the source revision and records "
                "them in artefacts/xplr/<exp-id>/record.json. Agent-facing: "
                "use the global --machine flag for a JSON envelope on stdout "
                "and pass manifests with --json <file|->"
            ),
            no_args_is_help=True,
        )
        self.xplr_app.command(
            "register",
            help="open a new experiment: pin the current git ref, record the "
            "agent-declared knob manifest, return its experiment id",
        )(self.do_xplr_register)
        self.xplr_app.command(
            "attach-outcome",
            help="attach flow-declared outcome metrics to an experiment "
            "(pending/running -> success|failed)",
        )(self.do_xplr_attach_outcome)
        self.xplr_app.command(
            "list",
            help="list experiments in the ledger (one summary row each)",
        )(self.do_xplr_list)
        self.xplr_app.command(
            "show",
            help="show one experiment's full record",
        )(self.do_xplr_show)
        self.xplr_app.command(
            "diff",
            help="pairwise experiment diff: knob delta, direction-aware "
            "outcome delta, and the git diff between the pinned sources",
        )(self.do_xplr_diff)
        self.xplr_app.command(
            "frontier",
            help="curate the Pareto frontier (non-dominated set) over the "
            "declared numeric outcome metrics; dominated, infeasible "
            "(routed=false), and excluded experiments are reported alongside",
        )(self.do_xplr_frontier)
        self.xplr_app.command(
            "knob-effect",
            help="per-knob effect history: every experiment that declared "
            "the knob, with metric deltas vs its parent when available",
        )(self.do_xplr_knob_effect)
        self.xplr_app.command(
            "materialize",
            help="check the experiment's pinned sha out into its own git "
            "worktree (isolated build dir; disposable — the branch is the "
            "durable artifact). Idempotent",
        )(self.do_xplr_materialize)
        self.xplr_app.command(
            "release",
            help="remove the experiment's worktree (worktree remove + "
            "prune); the exp branch and the ledger record are kept",
        )(self.do_xplr_release)
        self.xplr_app.command(
            "gc",
            help="reclaim experiment disk space non-interactively: evict "
            "heavy artifacts and worktrees per policy (default keep-frontier "
            "spares Pareto-frontier members and their lineage); record.json "
            "and the pinned sha survive, so evicted experiments can be "
            "re-materialized",
        )(self.do_xplr_gc)
        self.xplr_mock_app = typer.Typer(
            help=(
                "synthetic DSE backend with known optima (dev/CI harness): "
                "EDA-style knobs and metrics over Rastrigin and ZDT1 "
                "landscapes with feasibility cliffs; instant, deterministic, "
                "license-free, and self-scoring against the analytic optimum "
                "or Pareto front"
            ),
            no_args_is_help=True,
        )
        self.xplr_mock_app.command(
            "info",
            help="list scenarios: knob specs, metric_meta, cost model, and "
            "the analytic ground truth (optimum / Pareto front)",
        )(self.do_xplr_mock_info)
        self.xplr_mock_app.command(
            "run",
            help="evaluate one knob vector; with --register, record it as a "
            "ledger experiment with the outcome attached in one step",
        )(self.do_xplr_mock_run)
        self.xplr_mock_app.command(
            "score",
            help="score the ledger's mockflow experiments against the ground "
            "truth: regret (single-objective) or hypervolume + "
            "distance-to-front (multi-objective)",
        )(self.do_xplr_mock_score)
        self.xplr_mock_app.callback()(self._xplr_mock_group_options)
        self.xplr_app.add_typer(
            self.xplr_mock_app,
            name="mock",
            help="synthetic DSE backend with known optima (dev/CI harness)",
        )
        self.xplr_app.callback()(self._xplr_group_options)
        self.app.add_typer(
            self.xplr_app,
            name="xplr",
            help="design-space exploration experiment ledger (agent-facing)",
        )
        self.app.command(
            "tool-check",
            help="check installed tool dependencies and subcommand readiness",
        )(self.do_cmd_tool_check)

        if "." not in os.environ["PATH"].split(os.pathsep):
            os.environ["PATH"] = "." + os.pathsep + os.environ["PATH"]

        self.name = name
        self.rtl_builder_mode = None
        self.builder = None
        self.root_cfg = None
        self.coverage = None
        self._git_banner_shown = False
        self._git_root = None
        self._git_root_resolved = False
        self.run_depth = RunDepth.POST
        self.share_build = False
        # `--shared-build-root` as typed; the resolved value is the `shared_build_root`
        # property.
        self._shared_build_root_flag = None
        self.expect_prebuilt = False
        # `--rebuild`: ignore build stamps and compile anyway.
        self.rebuild = False
        # `--plusarg KEY=VALUE` overrides merged over each test's `plusargs:`; empty for
        # commands without the flag.
        self._plusarg_overrides: dict = {}
        # `--orphans` as typed; None defers to `cfg-dispatch.orphans` (default `warn`).
        # `_orphans_policy` is the resolved value.
        self._orphans: str | None = None
        self._orphans_policy: str = "warn"
        self.build_result_json = None
        self.machine = False
        self.invocation_cwd: Path = Path.cwd()
        self.exec_ctx: ExecutionContext | None = None
        self._builder_override: str | None = None
        self._artifact_locks = ArtifactLocks()
        self._xplr_root_override: Path | None = None
        # Nonce stamped into every result envelope; under dispatch the head's plan token
        # replaces it.
        self._run_token: str | None = None
        # Validated `--run-tag` namespace, or None for the flat tree.
        # Set before entering the execution context, which derives the artefact root and
        # tree lock from it.
        self._run_tag: str | None = None

    def run(self):
        """The process entry point: run the CLI and always release the artefact-tree lock.

        Every exit path (clean return, ``FatalRtlBuddyError``, click abort,
        ``KeyboardInterrupt`` from the subprocess signal handler) leaves through
        ``finally``, which releases the lock deterministically.
        """
        try:
            rv = self.app(standalone_mode=False)
        except click.exceptions.Exit as exc:
            return exc.exit_code
        except click.exceptions.Abort:
            return 1
        except click.ClickException as exc:
            exc.show(file=sys.stderr)
            return exc.exit_code
        except (FatalRtlBuddyError, FilelistError) as exc:
            emit_console_text(str(exc), style="red", markup=False)
            if self.machine:
                # Emit an envelope so machine consumers get JSON on stdout rather than a
                # silent stdout.
                command = (
                    getattr(self, "_pending_invoked_subcommand", None) or "rtl_buddy"
                )
                self._emit_machine_result(command, 2, error=str(exc))
            return 2
        except KeyboardInterrupt:
            # process_utils already terminated the tool's process group; report and exit
            # 128+SIGINT so `finally` releases as usual.
            emit_console_text("interrupted", style="red", markup=False)
            return 130
        finally:
            self._artifact_locks.release_all()
        # standalone_mode=False makes click return the exit code from `typer.Exit`
        # instead of re-raising.
        return rv if isinstance(rv, int) else 0

    def root_options(
        self,
        ctx: typer.Context,
        debug: Annotated[
            bool,
            typer.Option(
                "--debug", "-D", help="Print rtl_buddy debug details to console"
            ),
        ] = False,
        verbose: Annotated[
            bool,
            typer.Option("--verbose", "-v", help="Print execution details to console"),
        ] = False,
        machine: Annotated[
            bool,
            typer.Option(
                "--machine", help="Emit machine-oriented logs and plain console output"
            ),
        ] = False,
        print_failures_only: Annotated[
            bool,
            typer.Option(
                "--print-failures-only",
                help="Hide PASS, SKIP, and XFAIL rows from console summaries",
            ),
        ] = False,
        color: Annotated[
            bool, typer.Option(help="Logs without ANSI color codes")
        ] = True,
        rtl_builder_mode: Annotated[
            str,
            typer.Option("-M", "--builder-mode", help="Override default builder_mode"),
        ] = None,
        builder_override: Annotated[
            str,
            typer.Option(
                "-B",
                "--builder",
                callback=cb_builder,
                help="Override platform default builder",
            ),
        ] = None,
        extra_sim_timeout: Annotated[
            int | None,
            typer.Option(
                "--extra-sim-timeout",
                min=0,
                help="Seconds to add to every test's sim_timeout, "
                "overriding the builder's extra-sim-timeout",
            ),
        ] = None,
        run_depth: Annotated[
            RunDepth,
            typer.Option(
                "-E",
                "--early-stop",
                case_sensitive=False,
                help="Run step to stop early at",
                show_default=False,
            ),
        ] = RunDepth.POST,
        version_opt: Annotated[
            bool,
            typer.Option(
                "--version", callback=cb_version, is_eager=True, help="Prints version"
            ),
        ] = False,
    ):
        rtl_buddy_argv = sys.argv[1:]
        if "--" in rtl_buddy_argv:
            rtl_buddy_argv = rtl_buddy_argv[: rtl_buddy_argv.index("--")]

        if ctx.resilient_parsing or any(
            arg in {"--help", "-h"} for arg in rtl_buddy_argv
        ):
            return

        self.machine = machine
        set_print_failures_only(print_failures_only)
        self.invocation_cwd = Path.cwd().resolve()

        if ctx.invoked_subcommand in {"skill", "docs", "spec", "hub", "tool-check"}:
            return

        # Phase 1: console logging only. The file handler is attached once the
        # ExecutionContext is known.
        setup_logging(debug=debug, verbose=verbose, color=color, machine=machine)

        log_event(logger, logging.INFO, "cli.start", version=version("rtl-buddy"))

        # RootConfig and CoverageReporter are built in _enter_command_context(), after
        # root_config.yaml is found from the command root.
        self.rtl_builder_mode = rtl_builder_mode
        self._builder_override = builder_override
        self._extra_sim_timeout_override = extra_sim_timeout
        self.run_depth = run_depth
        self._pending_invoked_subcommand = ctx.invoked_subcommand

    def _enter_command_context(
        self,
        *,
        primary_config: str | Path | None = None,
        command_root: str | Path | None = None,
        list_only: bool = False,
        log_path: str | Path | None = None,
    ) -> ExecutionContext:
        """Build the command's ExecutionContext and attach the file log.

        Pass exactly one of:
        - ``primary_config``: the command's ``-c`` argument (e.g. ``tests.yaml``); the
          command root is its parent directory.
        - ``command_root``: an explicit directory anchor for commands without a single
          primary config.

        ``log_path`` overrides where the file log is attached (its parent is created).
        Dispatched ``rb _test-job`` and ``rb _build-job`` use it to log beside their
        result envelope (:func:`~rtl_buddy.dispatch.argv.job_log_path`); otherwise they
        would truncate the head's ``<suite>/rtl_buddy.log``.

        Constructs :attr:`root_cfg` and :attr:`coverage` once the command root is known,
        so ``root_config.yaml`` is found relative to the command. Later calls in the
        same process re-anchor the file log, so ``rb regression`` keeps each suite's log
        under its own root.

        ``list_only=True`` skips ``RootConfig``, builder and ``CoverageReporter`` setup,
        so the metadata-only ``--list`` paths work when the surrounding project config
        is invalid. It also leaves the project's log file alone: opening a file log
        truncates it, which would fail in a read-only checkout and destroy the log of
        the run being queried. These paths (`rb phys`, `rb cov`, the `rb graph` read
        verbs, `rb xplr`, `--list`) keep only the console handler. An explicit
        ``log_path`` still attaches.
        """
        if (primary_config is None) == (command_root is None):
            raise FatalRtlBuddyError(
                "_enter_command_context requires exactly one of "
                "primary_config or command_root"
            )

        if primary_config is not None:
            ctx = ExecutionContext.for_command(
                invocation_cwd=self.invocation_cwd,
                primary_config=primary_config,
                run_tag=self._run_tag,
            )
        else:
            ctx = ExecutionContext.for_dir(
                invocation_cwd=self.invocation_cwd,
                command_root=command_root,
                run_tag=self._run_tag,
            )

        ctx.command_root.mkdir(parents=True, exist_ok=True)
        if log_path is not None:
            log_path = Path(log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            attach_file_log(log_path)
        elif not list_only:
            # Under `--run-tag` the log directory does not exist yet; the tree lock
            # below creates it.
            ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
            attach_file_log(ctx.log_path)
        self.exec_ctx = ctx

        if list_only:
            return ctx

        # Fail loud if another process uses this artefact tree; held until exit.
        # `--list` paths stay lock-free.
        # Dispatched `_test-job` processes share the head's tree and must not take the
        # lock.
        # `ctx.artifact_root` carries the `--run-tag`, so the lock is per tag.
        if getattr(self, "_pending_invoked_subcommand", None) not in (
            "_test-job",
            "_build-job",
            "_elab-job",
        ):
            self._artifact_locks.acquire(
                ctx.artifact_root,
                command=getattr(self, "_pending_invoked_subcommand", None),
            )

        # Rebuild root_cfg only when the command root resolves to a different
        # root_config.yaml, so multi-root regressions get each suite's defaults.
        rebuild = self.root_cfg is None
        if not rebuild:
            try:
                new_root_path = _discover_root_cfg(start_dir=ctx.command_root)
            except FatalRtlBuddyError:
                new_root_path = None
            current_root_path = getattr(self.root_cfg, "root_cfg_path", None)
            rebuild = new_root_path is not None and new_root_path != current_root_path

        if rebuild:
            self.root_cfg = RootConfig(
                name=self.name + "/root_config",
                builder_override=self._builder_override,
                start_dir=ctx.command_root,
                extra_sim_timeout_override=self._extra_sim_timeout_override,
            )
            # Project-local env defaults (.rtl-buddy/.env) apply before tools read the
            # environment and never override set vars, so the first project wins.
            apply_env_file(self.root_cfg.get_project_rootdir())
            self.builder = self.root_cfg.get_builder_name()
            self.coverage = CoverageReporter(self.root_cfg)
            log_event(
                logger,
                logging.DEBUG,
                "cli.context_ready",
                command=getattr(self, "_pending_invoked_subcommand", None),
                command_root=str(ctx.command_root),
                builder=self.builder,
                builder_mode=self.rtl_builder_mode,
                run_depth=self.run_depth.value,
            )

        # After root_cfg, so the banner and machine envelope describe the same repo.
        if (
            not self._git_banner_shown
            and getattr(self, "_pending_invoked_subcommand", None) in self._GIT_COMMANDS
        ):
            self._git_banner_shown = True
            self.show_git_rev()

        return ctx

    @property
    def shared_build_root(self) -> str | None:
        """The persistent shared-build cache root in force, or None.

        Precedence: ``--shared-build-root``, then :data:`SHARED_BUILD_ROOT_ENV`, then
        ``cfg-rtl-reg.shared-build-root``. An empty flag or variable is an override too:
        it disables the cache for this run.

        Derived on each read: a regression whose suites span project roots rebuilds
        ``root_cfg`` per suite, and a relative root anchors to the suite's project.
        """
        return self._resolve_shared_build_root()[0]

    def _resolve_shared_build_root(self) -> tuple[str | None, bool]:
        """``(root or None, was it explicitly disabled)``; see the property.

        The second element lets a dispatched job tell "nobody asked for a cache" from
        "somebody asked for none"; both are ``None`` otherwise, and the job would
        re-enable the cache.
        """
        if self.root_cfg is None:
            return None, False
        # getattr: root_cfg is duck-typed here; test stand-ins provide only some
        # accessors.
        project_root = getattr(self.root_cfg, "get_project_rootdir", None)
        if project_root is None:
            return None, False
        raw, source = self._shared_build_root_flag, "cli"
        if raw is None:
            raw, source = os.environ.get(SHARED_BUILD_ROOT_ENV), "env"
        if raw is None:
            configured = getattr(self.root_cfg, "get_shared_build_root", None)
            raw, source = (configured() if configured is not None else None), "config"
        root = resolve_shared_build_root(raw, project_root())
        if root is not None:
            log_event(
                logger,
                logging.DEBUG,
                "cli.shared_build_root",
                root=root,
                source=source,
            )
        # A value was given but resolved to nothing: a disable, not an absence.
        return root, root is None and raw is not None

    @property
    def shared_build_root_for_jobs(self) -> str | None:
        """The value to forward to a dispatched job, as the tri-state argv wants.

        A path enables the cache there; ``""`` disables it; ``None`` lets the job
        resolve its own, which keeps job scripts unchanged for projects without a cache
        root.
        """
        root, disabled = self._resolve_shared_build_root()
        if root is not None:
            return root
        return "" if disabled else None

    def _exit_code_from_results(self, suite_results):
        # The exit code reflects whether rtl_buddy and the tools ran: a real FAIL or a
        # strict XPASS fails the run; an intentional early stop (NA with `early_stop`)
        # does not.
        # An NA meaning "no verdict produced" is unknown and fails the run.
        exit_code = 0
        for suite_result in suite_results:
            if is_run_failure(suite_result["results"]):
                exit_code |= 1
        return exit_code

    def _guard_coverage_requested(
        self,
        suite_results,
        exit_code,
        *,
        coverage_merge,
        coverage_merge_raw,
        coverage_merge_info_process,
        coverage_html,
        coverage_coverview,
        coverage_dir_summary,
        coverage_dir_summary_file,
        coverage_source_summary=False,
    ):
        """Fail loud when a coverage output flag was requested but no executed (non-skipped) test produced raw coverage data."""
        coverage_requested = (
            coverage_merge
            or coverage_merge_raw
            or coverage_merge_info_process
            or coverage_html
            or coverage_coverview
            or coverage_dir_summary
            or coverage_dir_summary_file
            # A source-point summary needs the model built from the raw databases.
            or coverage_source_summary
        )
        if (
            exit_code == 0
            and coverage_requested
            and not self.coverage.collect_paths(suite_results)
            and any(r["results"].results.get("result") != "SKIP" for r in suite_results)
        ):
            raise FatalRtlBuddyError(
                "Coverage output requested but no coverage data was produced by any "
                "executed test; ensure the selected builder supports coverage instrumentation"
            )

    def _coverage_merge_exit_code(self, coverage_payload) -> int:
        """Return 1 when a requested coverage merge failed, else 0.

        Sibling of ``_guard_coverage_requested``: a merge whose tool was killed leaves
        some metrics unmeasured while line and branch still report, so the summary would
        look almost complete.

        Not a ``FatalRtlBuddyError``: the caller invokes this after results, side-cars
        and manifest are written, and the fatal path would replace the envelope with an
        ``error`` payload and lose them. It folds into the run status as exit 1, "a tool
        flow failed".
        """
        if not coverage_payload or not coverage_payload.get("merge_failed"):
            return 0
        log_event(
            logger,
            logging.ERROR,
            "coverage.merge.degraded",
            failed_metrics=list(coverage_payload.get("failed_metrics") or []),
        )
        return 1

    def _apply_xfail_logged(self, res, cfg, event):
        """Re-interpret one result under cfg's xfail marker, and log it.

        Shared by every command whose per-item config exposes ``is_xfail()`` /
        ``get_xfail_strict()`` (test, fpv, synth, cdc, pnr, power); call only when
        ``cfg.is_xfail()`` is true.

        A FAIL that happened instead of a verdict (setup or compile failure, sim killed
        at the timeout, lost dispatch job) keeps its FAIL; the event reports
        ``excused=false`` with the reason.
        """
        observed = res.results.get("result")
        strict = cfg.get_xfail_strict()
        # Read before apply_xfail, which rewrites the desc either way.
        refusal = xfail_refusal(res.results) if observed == "FAIL" else None
        apply_xfail(res, strict=strict)
        log_event(
            logger,
            logging.INFO,
            event,
            name=cfg.get_name(),
            observed=observed,
            reported=res.results.get("result"),
            strict=strict,
            # log_event drops None fields, so `excused` appears only for an observed
            # failure.
            excused=(refusal is None) if observed == "FAIL" else None,
            reason=refusal,
        )
        return res

    def _render_test_summary(
        self,
        title,
        suite_results,
        *,
        include_run_id: bool = False,
        metadata: list[str] | None = None,
    ):
        rows = []
        has_coverage = False
        has_assertions = False
        builders = set()
        for suite_result in suite_results:
            cov_summary = self._format_coverage_summary(suite_result["results"])
            has_coverage |= cov_summary is not None
            assert_summary = self._format_assertions_summary(suite_result["results"])
            has_assertions |= assert_summary is not None
            builder = suite_result.get("builder")
            if builder:
                builders.add(builder)
            row = {
                "test_name": suite_result["test_name"],
                "result": suite_result["results"].results["result"],
                "desc": suite_result["results"].results["desc"],
                "builder": builder or "",
            }
            if include_run_id:
                row["run_id"] = (
                    ""
                    if suite_result["randmode_i"] is None
                    else suite_result["randmode_i"]
                )
            if cov_summary is not None:
                row["coverage"] = cov_summary
            if assert_summary is not None:
                row["assertions"] = assert_summary
            rows.append(row)

        columns = [("test_name", "Test")]
        if include_run_id:
            columns.append(("run_id", "Run"))
        columns.extend([("result", "Result"), ("desc", "Description")])
        # Per-row Builder column only when more than one builder is in play.
        if len(builders) > 1:
            columns.append(("builder", "Builder"))
        if has_assertions:
            columns.append(("assertions", "Assertions"))
        if has_coverage:
            columns.append(("coverage", "Coverage"))
        render_summary(
            title=title, columns=columns, rows=rows, logger=logger, metadata=metadata
        )

    def _render_regression_summary(
        self, reg_results, *, metadata: list[str] | None = None
    ):
        rows = []
        has_coverage = False
        has_assertions = False
        builders = set()
        for reg_result in reg_results:
            for suite_result in reg_result["results"]:
                cov_summary = self._format_coverage_summary(suite_result["results"])
                has_coverage |= cov_summary is not None
                assert_summary = self._format_assertions_summary(
                    suite_result["results"]
                )
                has_assertions |= assert_summary is not None
                builder = suite_result.get("builder")
                if builder:
                    builders.add(builder)
                rows.append(
                    {
                        "suite_name": reg_result["test_suite"],
                        "test_name": suite_result["test_name"],
                        "result": suite_result["results"].results["result"],
                        "desc": suite_result["results"].results["desc"],
                        "builder": builder or "",
                        "assertions": assert_summary or "",
                        "coverage": cov_summary or "",
                    }
                )

        columns = [
            ("suite_name", "Suite"),
            ("test_name", "Test"),
            ("result", "Result"),
            ("desc", "Description"),
        ]
        if len(builders) > 1:
            columns.append(("builder", "Builder"))
        if has_assertions:
            columns.append(("assertions", "Assertions"))
        if has_coverage:
            columns.append(("coverage", "Coverage"))
        render_summary(
            title="Regression Results Summary",
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata
            if metadata is not None
            else [f"Builder: {self.builder}", f"Builder Mode: {self.rtl_builder_mode}"],
        )

    def _builder_metadata_line(self, suite_cfgs, test_name=None):
        """Footer line naming the distinct builder(s) the run uses.

        Resolves each test's effective builder (per-test/suite `builder:`, `--builder`,
        or platform default). `suite_cfgs` may be one SuiteConfig or an iterable.
        Renders `Builder: x` for one builder and `Builders: x, y` for several.
        """
        if not isinstance(suite_cfgs, (list, tuple)):
            suite_cfgs = [suite_cfgs]
        names = sorted(
            {
                self.root_cfg.resolve_rtl_builder_cfg(t.get_builder_name()).get_name()
                for suite_cfg in suite_cfgs
                for t in suite_cfg.get_tests(test_name)
            }
        )
        if not names:
            names = [self.builder]
        label = "Builder" if len(names) == 1 else "Builders"
        return f"{label}: {', '.join(names)}"

    def _display_path(self, path: str, *, base_dir: str | None = None) -> str:
        if base_dir is None:
            return path

        try:
            relpath = os.path.relpath(path, base_dir)
        except ValueError:
            return path

        return relpath if len(relpath) < len(path) else path

    @staticmethod
    def _checked_master_seed(master_seed: int | None) -> int | None:
        if master_seed is None:
            return None
        try:
            return validate_master_seed(master_seed)
        except ValueError as e:
            raise FatalRtlBuddyError(str(e)) from e

    def _resolve_test_seed(
        self,
        test_cfg,
        *,
        master_seed: int | None,
        suite_config_path: str,
        run_id: int | None,
        seed_mode: SeedMode,
    ):
        return resolve_test_seed(
            test_cfg,
            self.root_cfg,
            master_seed=master_seed,
            suite_config_path=suite_config_path,
            run_id=run_id,
            seed_mode=seed_mode,
        )

    def _resolve_coverage_dir_summary_paths(
        self, coverage_dir_summary=None, coverage_dir_summary_file=None
    ):
        """Resolve coverage directory-summary prefixes from repeated CLI options and/or a file with one path per line."""
        return self.coverage.resolve_dir_summary_paths(
            dir_summary_paths=coverage_dir_summary,
            dir_summary_file=coverage_dir_summary_file,
        )

    def do_cmd_test(
        self,
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="test_config.yaml to use")
        ] = "tests.yaml",
        test_name: Annotated[
            list[str] | None,
            typer.Argument(help="names of tests", show_default="run all tests"),
        ] = None,
        list_tests: Annotated[
            bool,
            typer.Option(
                "--list", help="list tests in the selected test-config and exit"
            ),
        ] = False,
        test_filter: Annotated[
            str | None,
            typer.Option(
                "--filter",
                help="case-sensitive Python regex matched against configured test names",
            ),
        ] = None,
        coverage_merge: Annotated[
            bool,
            typer.Option(
                "--coverage-merge",
                help="merge coverage across selected tests; uses raw merge for summary/html and info-process for Coverview",
            ),
        ] = False,
        coverage_merge_raw: Annotated[
            bool,
            typer.Option(
                "--coverage-merge-raw",
                help="use raw Verilator merge for merged summary/html/Coverview",
            ),
        ] = False,
        coverage_merge_info_process: Annotated[
            bool,
            typer.Option(
                "--coverage-merge-info-process",
                help="use info-process merge for merged summary/Coverview; HTML merge is not supported",
            ),
        ] = False,
        coverage_html: Annotated[
            bool,
            typer.Option(
                "--coverage-html",
                help="generate merged LCOV HTML output in coverage_merge.html",
            ),
        ] = False,
        coverage_coverview: Annotated[
            bool,
            typer.Option(
                "--coverage-coverview",
                help="generate Coverview zip output from coverage info",
            ),
        ] = False,
        coverage_dir_summary: Annotated[
            list[str] | None,
            typer.Option(
                "--coverage-dir-summary",
                help="append coverage summary lines for repo-relative directory prefixes; may be repeated",
            ),
        ] = None,
        coverage_dir_summary_file: Annotated[
            str | None,
            typer.Option(
                "--coverage-dir-summary-file",
                help="file containing repo-relative directory prefixes, one per line",
            ),
        ] = None,
        coverage_source_summary: Annotated[
            bool,
            typer.Option(
                "--coverage-source-summary",
                help="append run coverage scored per source point (covered when any elaboration hit it), beside the per-elaboration figure",
            ),
        ] = False,
        coverage_model: Annotated[
            str,
            typer.Option(
                "--coverage-model",
                help="coverage-model.json to write: full (per-test attribution per point), totals (points without attribution), or none (manifest and totals only)",
                metavar="[full|totals|none]",
                click_type=click.Choice(list(cov_model.MODEL_MODES)),
            ),
        ] = cov_model.MODEL_MODE_FULL,
        rnd_new: Annotated[
            bool,
            typer.Option(
                "-n",
                "--rnd-new",
                help="use a randomly generated seed instead of root config seed",
                show_default=False,
            ),
        ] = None,
        rnd_last: Annotated[
            bool,
            typer.Option(
                "-l", "--rnd-last", help="reuse last generated seed", show_default=False
            ),
        ] = None,
        master_seed: Annotated[
            int | None,
            typer.Option(
                "--master-seed",
                help="derive an exact, stable runtime seed for each selected test",
            ),
        ] = None,
        share_build: Annotated[
            bool,
            typer.Option(
                "--share-build",
                help="reuse one compiled simv across tests with identical compile inputs (Verilator builders only)",
            ),
        ] = False,
        shared_build_root: Annotated[
            str,
            typer.Option(
                "--shared-build-root",
                help="persistent directory the shared builds are cached under, "
                "so the cache survives a workspace wipe",
                show_default="cfg-rtl-reg shared-build-root, else in-tree",
            ),
        ] = None,
        rebuild: Annotated[
            bool,
            typer.Option(
                "--rebuild",
                help="recompile even when a valid build already exists "
                "(implies nothing about --share-build)",
            ),
        ] = False,
        reg_level: Annotated[
            int | None,
            typer.Option("--reg-level", help="regression level to stop at"),
        ] = None,
        start_level: Annotated[
            int | None,
            typer.Option("--start-level", help="regression level to start at"),
        ] = None,
        dispatch: Annotated[
            str,
            typer.Option(
                "--dispatch",
                help="execution backend for the test run "
                "(local, local-parallel, slurm); opt-in per run — "
                "cfg-dispatch.backend does not redirect rb test",
                show_default="local",
            ),
        ] = None,
        jobs: Annotated[
            int,
            typer.Option(
                "-j",
                "--jobs",
                help="concurrent jobs for --dispatch local-parallel",
                show_default="cfg-dispatch jobs, else min(4, cpu count)",
            ),
        ] = None,
        orphans: Annotated[
            str,
            typer.Option(
                "--orphans",
                help="what to do about an interrupted run's jobs that are "
                "still queued or running (warn, cancel, adopt)",
                show_default="cfg-dispatch orphans, else warn",
            ),
        ] = None,
        plusarg: Annotated[
            list[str] | None,
            typer.Option(
                "--plusarg",
                help="add or override one runtime plusarg (KEY=VALUE, or bare "
                "KEY for +KEY); repeatable, the last repeat wins, and it "
                "beats the test's plusargs:",
                show_default=False,
            ),
        ] = None,
        run_tag: Annotated[
            str | None,
            typer.Option(
                "--run-tag",
                help="namespace this run's artefact tree under "
                "artefacts/.runs/<tag>/ with its own tree lock and log, so "
                "concurrent runs of a suite do not collide; shared builds "
                "stay shared",
            ),
        ] = None,
    ):
        """
        run a simple test
        """
        # Recorded before a backend is resolved; the policy is validated against it
        # there.
        self._orphans = orphans
        # Parsed before anything runs, so a malformed value is a usage error.
        self._plusarg_overrides = parse_plusarg_overrides(plusarg)
        # Validated before the execution context is entered, which derives the artefact
        # root from it.
        self._run_tag = validate_run_tag(run_tag)
        master_seed = self._checked_master_seed(master_seed)
        if master_seed is not None and (rnd_new or rnd_last):
            raise FatalRtlBuddyError(
                "--master-seed cannot be combined with --rnd-new or --rnd-last"
            )

        # Direct callers may pass a scalar; Typer supplies a list.
        test_names = [test_name] if isinstance(test_name, str) else test_name

        # Validated before the `--list` short circuit, so an unusable flag is rejected,
        # not dropped.
        # Name first, so no message quotes an unknown backend.
        validate_backend_name(dispatch)
        # The raw --dispatch, not a resolved backend: `rb test` ignores
        # `cfg-dispatch.backend`.
        self._validate_jobs_flag(dispatch, jobs)
        if list_tests and dispatch not in (None, "local"):
            raise FatalRtlBuddyError(
                f"--list cannot be combined with --dispatch {dispatch}: it "
                "prints the suite's test names and runs nothing, so there is "
                "nothing to dispatch."
            )
        if list_tests and test_filter is not None:
            raise FatalRtlBuddyError(
                "--list cannot be combined with --filter: --list prints every "
                "configured test name and runs nothing."
            )
        if list_tests and self._plusarg_overrides:
            raise FatalRtlBuddyError(
                "--list cannot be combined with --plusarg: --list prints the "
                "suite's test names and runs no simulation, so the override "
                "would have nothing to apply to."
            )
        if test_names and test_filter is not None:
            raise FatalRtlBuddyError("test names and --filter are mutually exclusive")
        merge_mode_count = sum(
            1
            for enabled in [
                coverage_merge,
                coverage_merge_raw,
                coverage_merge_info_process,
            ]
            if enabled
        )
        if merge_mode_count > 1:
            raise FatalRtlBuddyError(
                "--coverage-merge, --coverage-merge-raw, and --coverage-merge-info-process are mutually exclusive"
            )
        if coverage_merge_info_process and coverage_html:
            raise FatalRtlBuddyError(
                "--coverage-html is not supported with --coverage-merge-info-process"
            )

        self.rtl_builder_mode = (
            "debug" if self.rtl_builder_mode is None else self.rtl_builder_mode
        )
        ctx = self._enter_command_context(
            primary_config=test_config, list_only=list_tests
        )
        self.suite_cfg = SuiteConfig(path=str(ctx.primary_config))

        if list_tests:
            log_event(
                logger,
                logging.INFO,
                "command.test",
                command="test",
                test="all" if not test_names else ", ".join(test_names),
                test_config=test_config,
            )
            if self.machine:
                self._emit_machine_result(
                    "test --list", 0, names=list(self.suite_cfg.get_test_names())
                )
            else:
                emit_console_text(
                    "  ".join(self.suite_cfg.get_test_names()), stream="stdout"
                )
            raise typer.Exit(0)

        test_selection = test_names or None
        if test_filter is not None:
            try:
                pattern = re.compile(test_filter)
            except re.error as e:
                raise FatalRtlBuddyError(
                    f"invalid --filter regex {test_filter!r}: {e}"
                ) from e
            test_selection = [
                name for name in self.suite_cfg.get_test_names() if pattern.search(name)
            ]
            if not test_selection:
                raise FatalRtlBuddyError(
                    f"--filter regex {test_filter!r} matched no tests in suite "
                    f"{self.suite_cfg.get_path()}"
                )

        log_event(
            logger,
            logging.INFO,
            "command.test",
            command="test",
            test="all" if test_selection is None else ", ".join(test_selection),
            test_config=test_config,
            master_seed=master_seed,
            # Runtime overrides not in tests.yaml; None rather than {}, like
            # `master_seed`.
            plusarg_overrides=self._plusarg_overrides or None,
            # Artefact tree written to; omitted by log_event when unset.
            run_tag=self._run_tag,
        )

        seed_mode: SeedMode = SeedMode.DEFAULT
        replay_run_id = None
        if rnd_new:
            seed_mode = SeedMode.NEW
        elif rnd_last:
            seed_mode = SeedMode.REPLAY
        elif master_seed is not None:
            seed_mode = SeedMode.MASTER
        self.share_build = share_build
        self.rebuild = rebuild
        self._shared_build_root_flag = shared_build_root

        # `rb test` uses the `rb regression --dispatch` planning path: one plan, one
        # build job, one gated sim job per selected test.
        # Dispatch is opt-in per invocation: `rb test` does not read
        # `cfg-dispatch.backend`, so a project with a cluster backend does not queue
        # single-test runs.
        dispatch_backend = (
            self._resolve_dispatch_backend(dispatch, jobs=jobs)
            if dispatch is not None
            else None
        )
        reservation_findings = []
        if dispatch_backend is None:
            suite_results = self._do_test_suite(
                self.suite_cfg,
                test_name=test_selection,
                run_ids=[None],
                seed_mode=seed_mode,
                replay_run_id=replay_run_id,
                reg_level=reg_level,
                start_level=start_level,
                master_seed=master_seed,
            )
        else:
            # As for dispatched regressions: no stop point before POST per job, and the
            # build job lets the sim job skip compilation, so share_build is implied.
            self._reject_early_stop_under_dispatch(dispatch, dispatch_backend)
            if not share_build:
                self.share_build = True
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.share_build_implied",
                    backend=dispatch_backend.name,
                )
            suite_display = self._display_path(
                str(ctx.primary_config), base_dir=str(self.invocation_cwd)
            )
            state = self._dispatch_suite_submit(
                self.suite_cfg,
                dispatch_backend,
                run_token=uuid.uuid4().hex,
                test_name=test_selection,
                run_ids=[None],
                seed_mode=seed_mode,
                replay_run_id=replay_run_id,
                reg_level=reg_level,
                start_level=start_level,
                master_seed=master_seed,
            )
            self._announce_dispatched_suite(
                state,
                backend=dispatch_backend,
                suite=suite_display,
            )
            self._wait_or_cancel(dispatch_backend, state)
            suite_results = self._dispatch_collect(dispatch_backend, state)
            reservation_findings = self._analyze_reservations(
                suite_results,
                suite_display=suite_display,
                suite_config_path=str(Path(ctx.primary_config).resolve()),
                reg_level=reg_level,
                backend=dispatch_backend,
                state=state,
            )
            _log_reservation_advice(reservation_findings)
        dir_summary_paths = self._resolve_coverage_dir_summary_paths(
            coverage_dir_summary=coverage_dir_summary,
            coverage_dir_summary_file=coverage_dir_summary_file,
        )
        exit_code = self._exit_code_from_results(suite_results)
        self._guard_coverage_requested(
            suite_results,
            exit_code,
            coverage_merge=coverage_merge,
            coverage_merge_raw=coverage_merge_raw,
            coverage_merge_info_process=coverage_merge_info_process,
            coverage_html=coverage_html,
            coverage_coverview=coverage_coverview,
            coverage_dir_summary=coverage_dir_summary,
            coverage_dir_summary_file=coverage_dir_summary_file,
            coverage_source_summary=coverage_source_summary,
        )
        metadata = [self._builder_metadata_line(self.suite_cfg, test_selection)]
        if master_seed is not None:
            metadata.append(f"Master Seed: {master_seed}")
        if self._plusarg_overrides:
            # Named in the footer like the master seed, spelled as the simulator
            # receives it.
            metadata.append(
                "Plusarg Overrides: "
                + " ".join(
                    f"+{key}" if value is None else f"+{key}={value}"
                    for key, value in self._plusarg_overrides.items()
                )
            )
        cov_metadata, coverage_payload = self.coverage.build_metadata(
            suite_results,
            outdir=str(ctx.command_root),
            suite_name=self.suite_cfg.get_path(),
            coverage_merge=coverage_merge,
            coverage_merge_raw=coverage_merge_raw,
            coverage_html=coverage_html,
            coverage_coverview=coverage_coverview,
            coverage_merge_info_process=coverage_merge_info_process,
            source_roots=[str(ctx.command_root)],
            dir_summary_paths=dir_summary_paths,
            source_summary=coverage_source_summary,
            command="test",
            model_mode=coverage_model,
        )
        metadata.extend(cov_metadata)
        # After build_metadata: the manifest and model are on disk, so a failed merge
        # costs only the exit code.
        exit_code |= self._coverage_merge_exit_code(coverage_payload)
        self._refresh_result_side_cars(suite_results)
        # Rendered in both modes; in machine mode it emits the "summary" log event and
        # leaves stdout for the envelope.
        self._render_test_summary(
            "Test Results Summary", suite_results, metadata=metadata
        )
        if reservation_findings and not self.machine:
            self._render_reservation_advice(reservation_findings)
        if self.machine:
            payload = {
                "results": [
                    self._machine_test_row(r["test_name"], r["results"])
                    for r in suite_results
                ]
            }
            coverage = self._machine_coverage_payload(coverage_payload)
            if coverage is not None:
                payload["coverage"] = coverage
            if dispatch_backend is not None:
                payload["reservation_advice"] = [
                    finding.as_event() for finding in reservation_findings
                ]
            if master_seed is not None:
                payload["master_seed"] = master_seed
            self._emit_machine_result("test", exit_code, **payload)
        raise typer.Exit(exit_code)

    def do_rand_test(
        self,
        test_name: Annotated[
            str, typer.Argument(help="name of test", show_default="run all tests")
        ],
        rnd_cnt: Annotated[
            int,
            typer.Argument(
                metavar="RND_CNT", help="number of random iterations to test"
            ),
        ] = 2,
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="test_config.yaml to use")
        ] = "tests.yaml",
        rpt_i: Annotated[
            int,
            typer.Option(
                "-r",
                "--rnd-rpt",
                help="repeat iteration number from previous run",
                show_default=False,
            ),
        ] = None,
        rebuild: Annotated[
            bool,
            typer.Option(
                "--rebuild",
                help="recompile even when a valid build already exists "
                "(implies nothing about --share-build)",
            ),
        ] = False,
        shared_build_root: Annotated[
            str,
            typer.Option(
                "--shared-build-root",
                help="persistent directory the shared builds are cached under, "
                "so the cache survives a workspace wipe",
                show_default="cfg-rtl-reg shared-build-root, else in-tree",
            ),
        ] = None,
        dispatch: Annotated[
            str,
            typer.Option(
                "--dispatch",
                help="execution backend for the seed fan-out "
                "(local, local-parallel, slurm)",
                show_default="cfg-dispatch backend, else local",
            ),
        ] = None,
        jobs: Annotated[
            int,
            typer.Option(
                "-j",
                "--jobs",
                help="concurrent jobs for --dispatch local-parallel",
                show_default="cfg-dispatch jobs, else min(4, cpu count)",
            ),
        ] = None,
        orphans: Annotated[
            str,
            typer.Option(
                "--orphans",
                help="what to do about an interrupted run's jobs that are "
                "still queued or running (warn, cancel, adopt)",
                show_default="cfg-dispatch orphans, else warn",
            ),
        ] = None,
        run_tag: Annotated[
            str | None,
            typer.Option(
                "--run-tag",
                help="namespace this run's artefact tree under "
                "artefacts/.runs/<tag>/ with its own tree lock and log, so "
                "concurrent runs of a suite do not collide; shared builds "
                "stay shared",
            ),
        ] = None,
    ):
        """
        repeat a test with multiple random seeds
        """
        self._orphans = orphans
        self._run_tag = validate_run_tag(run_tag)
        self.rebuild = rebuild
        self._shared_build_root_flag = shared_build_root
        self.rtl_builder_mode = (
            "debug" if self.rtl_builder_mode is None else self.rtl_builder_mode
        )
        ctx = self._enter_command_context(primary_config=test_config)
        self.suite_cfg = SuiteConfig(path=str(ctx.primary_config))

        log_event(
            logger,
            logging.INFO,
            "command.randtest",
            command="randtest",
            test=test_name,
            iterations=rnd_cnt,
            replay_run_id=rpt_i,
            run_tag=self._run_tag,
        )

        # Seed fan-out suits dispatch (one shared build, N sims); a single replay (-r)
        # stays local.
        backend_name = self._dispatch_backend_name(dispatch)
        # Validate --jobs on the replay path too, which never builds a backend.
        self._validate_jobs_flag(backend_name, jobs)
        if rpt_i is not None and (
            jobs is not None or (dispatch is not None and dispatch != "local")
        ):
            # A single-seed replay stays local; say so when a dispatch flag was
            # explicit.
            log_event(
                logger,
                logging.WARNING,
                "randtest.dispatch_ignored_for_replay",
                backend=backend_name or "local",
                replay_run_id=rpt_i,
                jobs=jobs,
            )
        dispatch_backend = (
            self._resolve_dispatch_backend(dispatch, jobs=jobs)
            if rpt_i is None
            else None
        )
        reservation_findings = []
        if dispatch_backend is not None:
            self.share_build = True
            state = self._dispatch_suite_submit(
                self.suite_cfg,
                dispatch_backend,
                run_token=uuid.uuid4().hex,
                test_name=test_name,
                run_ids=list(range(1, rnd_cnt + 1)),
                seed_mode=SeedMode.NEW,
            )
            self._announce_dispatched_suite(
                state,
                backend=dispatch_backend,
                suite=self._display_path(
                    str(ctx.primary_config), base_dir=str(self.invocation_cwd)
                ),
            )
            self._wait_or_cancel(dispatch_backend, state)
            suite_results = self._dispatch_collect(dispatch_backend, state)
            reservation_findings = self._analyze_reservations(
                suite_results,
                suite_display=self._display_path(
                    str(ctx.primary_config), base_dir=str(self.invocation_cwd)
                ),
                suite_config_path=str(Path(ctx.primary_config).resolve()),
                backend=dispatch_backend,
                state=state,
            )
            _log_reservation_advice(reservation_findings)
            if not self.machine:
                self._render_test_summary(
                    "RandTest Results Summary",
                    suite_results,
                    include_run_id=True,
                    metadata=[self._builder_metadata_line(self.suite_cfg, test_name)],
                )
                if reservation_findings:
                    self._render_reservation_advice(reservation_findings)
        elif rpt_i is not None:
            suite_results = self._do_test_suite(
                self.suite_cfg,
                test_name=test_name,
                run_ids=[rpt_i],
                seed_mode=SeedMode.REPLAY,
                replay_run_id=rpt_i,
            )
            if not self.machine:
                self._render_test_summary(
                    "RandTest Replay Summary",
                    suite_results,
                    include_run_id=True,
                    metadata=[self._builder_metadata_line(self.suite_cfg, test_name)],
                )
        else:
            suite_results = self._do_test_suite(
                self.suite_cfg,
                test_name=test_name,
                run_ids=list(range(1, rnd_cnt + 1)),
                seed_mode=SeedMode.NEW,
                replay_run_id=None,
            )
            if not self.machine:
                self._render_test_summary(
                    "RandTest Results Summary",
                    suite_results,
                    include_run_id=True,
                    metadata=[self._builder_metadata_line(self.suite_cfg, test_name)],
                )

        exit_code = self._exit_code_from_results(suite_results)
        if self.machine:
            payload = {
                "results": [
                    self._machine_test_row(
                        r["test_name"], r["results"], run_id=r["randmode_i"]
                    )
                    for r in suite_results
                ]
            }
            if dispatch_backend is not None:
                payload["reservation_advice"] = [
                    finding.as_event() for finding in reservation_findings
                ]
            self._emit_machine_result("randtest", exit_code, **payload)
        raise typer.Exit(exit_code)

    def _abs_invocation_path(self, path: str) -> Path:
        """Resolve ``path`` against the invocation cwd if it is relative.

        A dispatched job's ``--result-json`` and ``--plan`` are relative to where the
        head submitted from, not the re-anchored suite dir.
        """
        p = Path(path)
        return p if p.is_absolute() else self.invocation_cwd / p

    def _resolve_job_test_cfg(self, suite_cfg, test_name, suite_dir, plan_path=None):
        """Resolve a job's test config by name, honoring sweep expansion.

        Accepts a base test name or a sweep-expanded config name. Returns ``(test_cfg,
        None)``, or ``(None, setup_error)`` when a sweep hook failed, which the caller
        writes as a ``SetupFailResults``. An unknown name raises ``FatalRtlBuddyError``.

        With ``plan_path``, the config is read from the head's dispatch plan and the
        sweep hook does not run; a name absent from the plan falls through to hook
        expansion.
        """
        if plan_path is not None:
            cfg = read_plan_config(self._abs_invocation_path(plan_path), test_name)
            if cfg is not None:
                return cfg, None
        sweep_error_seen = None
        if test_name in suite_cfg.get_test_names():
            base = suite_cfg.get_tests(test_name)[0]
            expanded, sweep_error = self._expand_tests_with_sweep(
                base, suite_dir=suite_dir
            )
            if sweep_error is not None:
                return None, sweep_error
            for cfg in expanded:
                if cfg.name == test_name:
                    return cfg, None
            if len(expanded) == 1:
                return expanded[0], None
            raise FatalRtlBuddyError(
                f"test {test_name} sweep-expands to multiple configs "
                f"[{', '.join(c.name for c in expanded)}]; "
                "address one by its expanded name"
            )

        for base in suite_cfg.get_tests():
            expanded, sweep_error = self._expand_tests_with_sweep(
                base, suite_dir=suite_dir
            )
            if sweep_error is not None:
                # A broken sweep must not mask other names, but is remembered: the
                # requested name may come from it.
                if sweep_error_seen is None:
                    sweep_error_seen = sweep_error
                continue
            for cfg in expanded:
                if cfg.name == test_name:
                    return cfg, None

        if sweep_error_seen is not None:
            return None, sweep_error_seen
        raise FatalRtlBuddyError(
            f"test_name {test_name} not found in suite {suite_cfg.get_path()} "
            "(after sweep expansion)"
        )

    def do_cmd_test_job(
        self,
        test_name: Annotated[
            str,
            typer.Argument(help="test to run (sweep-expanded names accepted)"),
        ],
        result_json: Annotated[
            str,
            typer.Option(
                "--result-json",
                help="path to write the run's result JSON envelope "
                "(relative to the invocation cwd)",
            ),
        ],
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="test_config.yaml to use")
        ] = "tests.yaml",
        run_id: Annotated[
            int,
            typer.Option(
                "--run-id",
                help="run id for output naming and seed replay",
                show_default="single unnumbered run",
            ),
        ] = None,
        seed_mode: Annotated[
            SeedMode,
            typer.Option("--seed-mode", case_sensitive=False),
        ] = SeedMode.DEFAULT,
        replay_run_id: Annotated[
            int,
            typer.Option(
                "--replay-run-id",
                help="seed run id to replay",
                show_default="--run-id when --seed-mode replay",
            ),
        ] = None,
        master_seed: Annotated[
            int,
            typer.Option(
                "--master-seed",
                help="exact master seed selected by the dispatching command",
            ),
        ] = None,
        resolved_seed: Annotated[
            int,
            typer.Option(
                "--resolved-seed",
                help="resolved simulator seed selected by the dispatching command",
            ),
        ] = None,
        share_build: Annotated[
            bool,
            typer.Option(
                "--share-build",
                help="reuse one compiled simv across tests with identical compile inputs (Verilator builders only)",
            ),
        ] = False,
        shared_build_root: Annotated[
            str,
            typer.Option(
                "--shared-build-root",
                help="persistent directory the shared builds are cached under, "
                "so the cache survives a workspace wipe",
                show_default="cfg-rtl-reg shared-build-root, else in-tree",
            ),
        ] = None,
        rebuild: Annotated[
            bool,
            typer.Option(
                "--rebuild",
                help="recompile even when a valid build already exists "
                "(implies nothing about --share-build)",
            ),
        ] = False,
        plan: Annotated[
            str,
            typer.Option(
                "--plan",
                help="dispatch plan manifest; resolve this test's config from "
                "it instead of re-running the suite's sweep hook",
            ),
        ] = None,
        expect_prebuilt: Annotated[
            bool,
            typer.Option(
                "--expect-prebuilt",
                help="this job was gated on a build job, so compiling here "
                "means that build's stamp did not validate (warns)",
            ),
        ] = False,
        build_result_json: Annotated[
            str,
            typer.Option(
                "--build-result-json",
                help="the gating build job's result envelope; a test it "
                "records as failed is reported without recompiling here",
            ),
        ] = None,
        plusarg: Annotated[
            list[str] | None,
            typer.Option(
                "--plusarg",
                help="one-off runtime plusarg override the dispatching "
                "command applied (KEY=VALUE or bare KEY); repeatable",
            ),
        ] = None,
        run_tag: Annotated[
            str | None,
            typer.Option(
                "--run-tag",
                help="the head's artefact namespace; write into "
                "artefacts/.runs/<tag>/ so this job's outputs land in the "
                "tree the head planned",
            ),
        ] = None,
    ):
        """internal: run one (test, run_id) and write its result JSON"""
        master_seed = self._checked_master_seed(master_seed)
        self._plusarg_overrides = parse_plusarg_overrides(plusarg)
        # Re-validated at this CLI boundary: a mangled tag must fail loud, not write a
        # second tree.
        self._run_tag = validate_run_tag(run_tag)
        self.rtl_builder_mode = (
            "reg" if self.rtl_builder_mode is None else self.rtl_builder_mode
        )
        self.share_build = share_build
        self.expect_prebuilt = expect_prebuilt
        self.rebuild = rebuild
        self._shared_build_root_flag = shared_build_root
        # Resolved against the invocation cwd like --result-json; absent for an ungated
        # job or an old head.
        self.build_result_json = (
            self._abs_invocation_path(build_result_json)
            if build_result_json is not None
            else None
        )
        # Resolved before entering the command context so a relative --result-json lands
        # where the head expects it.
        result_json_path = self._abs_invocation_path(result_json)
        # Mirror run_multiple: a replayed job replays its own run_id unless told
        # otherwise.
        if seed_mode == SeedMode.REPLAY and replay_run_id is None:
            replay_run_id = run_id

        # Log beside the envelope, never into the head's <suite>/rtl_buddy.log, which
        # the first open would truncate.
        ctx = self._enter_command_context(
            primary_config=test_config,
            log_path=job_log_path(result_json_path),
        )
        suite_cfg = SuiteConfig(path=str(ctx.primary_config))
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        log_event(
            logger,
            logging.INFO,
            "command.test_job",
            command="_test-job",
            test=test_name,
            run_id=run_id,
            seed_mode=seed_mode.value,
            master_seed=master_seed,
            resolved_seed=resolved_seed,
            result_json=str(result_json_path),
            plan=plan,
            run_tag=self._run_tag,
        )

        plan_config = (
            read_plan_config(self._abs_invocation_path(plan), test_name)
            if plan is not None
            else None
        )
        test_cfg, setup_error = self._resolve_job_test_cfg(
            suite_cfg, test_name, suite_dir, plan_path=plan
        )
        if test_cfg is not None:
            # Re-applies the head's `--plusarg` overrides: a no-op for configs from the
            # plan, the whole override for a config absent from it.
            # Also puts them in this job's result envelope.
            test_cfg = test_cfg.with_plusarg_overrides(self._plusarg_overrides)
        if plan is not None:
            plan_master_seed = self._checked_master_seed(
                read_plan_master_seed(self._abs_invocation_path(plan))
            )
            if master_seed is None:
                master_seed = plan_master_seed
            elif plan_master_seed is not None and master_seed != plan_master_seed:
                raise FatalRtlBuddyError(
                    f"--master-seed {master_seed} does not match dispatch plan "
                    f"master seed {plan_master_seed}"
                )
        if test_cfg is not None:
            planned_seed = test_cfg.get_resolved_seed()
            if plan_config is not None:
                if planned_seed is not None:
                    try:
                        validate_resolved_seed(planned_seed, test_cfg.seed_source)
                    except ValueError as e:
                        raise FatalRtlBuddyError(
                            f"dispatch plan seed for {test_name!r} is invalid: {e}"
                        ) from e
                    test_cfg.set_resolved_seed(
                        SeedResolution(
                            seed=planned_seed,
                            source=test_cfg.seed_source,
                            identity=test_cfg.seed_identity,
                        )
                    )
                elif (
                    master_seed is not None
                    or test_cfg.sim_rand_seed is not None
                    or test_cfg.sim_rand_seed_plusarg is not None
                ):
                    raise FatalRtlBuddyError(
                        f"dispatch plan has no resolved seed for seeded test "
                        f"{test_name!r}"
                    )
                if resolved_seed != planned_seed:
                    raise FatalRtlBuddyError(
                        f"--resolved-seed {resolved_seed!r} does not match dispatch "
                        f"plan seed {planned_seed!r} for {test_name!r}"
                    )
            else:
                resolution = self._resolve_test_seed(
                    test_cfg,
                    master_seed=master_seed,
                    suite_config_path=suite_cfg.get_path(),
                    run_id=run_id,
                    seed_mode=seed_mode,
                )
                if resolved_seed is not None:
                    try:
                        validate_resolved_seed(
                            resolved_seed, resolution.source if resolution else None
                        )
                    except ValueError as e:
                        raise FatalRtlBuddyError(str(e)) from e
                    if resolution is not None and resolution.seed != resolved_seed:
                        raise FatalRtlBuddyError(
                            f"--resolved-seed {resolved_seed} does not match derived "
                            f"seed {resolution.seed} for {test_name!r}"
                        )
                    if resolution is None:
                        suite_identity = suite_seed_identity(
                            suite_cfg.get_path(), self.root_cfg.get_project_rootdir()
                        )
                        resolution = SeedResolution(
                            seed=resolved_seed,
                            source=test_cfg.seed_source or "dispatch",
                            identity=test_cfg.seed_identity
                            or expanded_test_seed_identity(
                                suite_identity, test_cfg.get_name(), run_id
                            ),
                        )
                        test_cfg.set_resolved_seed(resolution)
            current_seed = test_cfg.get_resolved_seed()
            if plan_config is not None and current_seed != planned_seed:
                raise FatalRtlBuddyError(
                    f"dispatch plan seed {planned_seed!r} changed to "
                    f"{current_seed!r} for {test_name!r}"
                )
        # Resolve the head's run token before the sim and never abort on failure: a None
        # token only makes the head reject the envelope as stale.
        run_token = None
        if plan is not None:
            try:
                run_token = read_plan_token(self._abs_invocation_path(plan))
            except FatalRtlBuddyError:
                run_token = None
        # The artifact-dir envelope carries the head's token too, so both records agree.
        if run_token is not None:
            self._run_token = run_token

        if setup_error is not None:
            res = SetupFailResults(name=test_name + "/results", desc=setup_error)
            reported_name = test_name
        else:
            run_results = self._run_test_cfg_for_run_ids(
                test_cfg=test_cfg,
                run_ids=[run_id],
                seed_mode=seed_mode,
                replay_run_id=replay_run_id,
                test_runner_mode={"sim_to_stdout": False},
                suite_dir=suite_dir,
                master_seed=master_seed,
            )
            res = run_results[0]
            reported_name = test_cfg.get_name()

        # Stamp the head's run token so collection can reject a stale envelope by
        # identity.
        write_result_json(
            result_json_path,
            test_name=reported_name,
            run_id=run_id,
            results=res,
            run_token=run_token,
            run_tag=self._run_tag,
        )
        # The head's grading rule, so job and collector score a run alike: unknown NA
        # fails, intentional early stop does not.
        exit_code = 1 if is_run_failure(res) else 0
        if self.machine:
            self._emit_machine_result(
                "_test-job",
                exit_code,
                result={
                    "name": reported_name,
                    "run_id": run_id,
                    "result": res.results["result"],
                    "desc": res.results["desc"],
                    **({"seed": res.results["seed"]} if "seed" in res.results else {}),
                },
                result_json=str(result_json_path),
            )
        else:
            self._render_test_summary(
                "Test Job Result",
                [
                    {
                        "test_name": reported_name,
                        "randmode_i": run_id,
                        "results": res,
                    }
                ],
                include_run_id=run_id is not None,
                metadata=[f"Builder: {self.builder}"],
            )
        raise typer.Exit(exit_code)

    def do_cmd_build_job(
        self,
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="test_config.yaml to use")
        ] = "tests.yaml",
        reg_level: Annotated[
            int, typer.Option("-l", "--reg-level", help="regression level to stop at")
        ] = None,
        start_level: Annotated[
            int,
            typer.Option("-s", "--start-level", help="regression level to start at"),
        ] = None,
        share_build: Annotated[
            bool,
            typer.Option(
                "--share-build",
                help="reuse one compiled simv across tests with identical compile inputs",
            ),
        ] = True,
        shared_build_root: Annotated[
            str,
            typer.Option(
                "--shared-build-root",
                help="persistent directory the shared builds are cached under, "
                "so the cache survives a workspace wipe",
                show_default="cfg-rtl-reg shared-build-root, else in-tree",
            ),
        ] = None,
        rebuild: Annotated[
            bool,
            typer.Option(
                "--rebuild",
                help="recompile even when a valid build already exists "
                "(implies nothing about --share-build)",
            ),
        ] = False,
        plan: Annotated[
            str,
            typer.Option(
                "--plan",
                help="dispatch plan manifest; compile its configs instead of "
                "re-running the suite's sweep hook",
            ),
        ] = None,
        result_json: Annotated[
            str,
            typer.Option(
                "--result-json",
                help="path to write the build outcome (built/failed test names)",
            ),
        ] = None,
        parallel: Annotated[
            int,
            typer.Option(
                "--parallel",
                help="compile up to N distinct builds concurrently "
                "(grouped by compile key)",
            ),
        ] = 1,
        parallel_configured: Annotated[
            int,
            typer.Option(
                "--parallel-configured",
                help="what compile.parallel says, when --parallel is the "
                "head's plan-capped value (diagnostics only)",
            ),
        ] = None,
        gates: Annotated[
            str,
            typer.Option(
                "--gates",
                help="dispatch gates manifest; release each compile key's "
                "simulation jobs as soon as that key is built",
            ),
        ] = None,
        phase: Annotated[
            str,
            typer.Option(
                "--phase",
                hidden=True,
                help="which half of the compile to run: full (verilate and "
                "build), verilate (front end only), or build (make only)",
            ),
        ] = BUILD_PHASE_FULL,
        run_tag: Annotated[
            str | None,
            typer.Option(
                "--run-tag",
                help="the head's artefact namespace; write into "
                "artefacts/.runs/<tag>/ so this job's outputs land in the "
                "tree the head planned",
            ),
        ] = None,
    ):
        """internal: compile a suite's runnable tests on a compute node

        Runs PRE+COMPILE (share-build) for every runnable test so each compile key
        Verilates once. With ``--plan`` the configs come from the head's single sweep
        expansion. A failed compile is reported but does not fail the job, so dependent
        sim jobs still run; the exit code is 0 unless setup is fatal.

        With ``--parallel 1`` or a one-config plan, PRE then COMPILE runs per config.
        Above that, every PRE runs first and distinct builds compile concurrently, so
        one config's preproc hook must not mutate another config's inputs.

        With ``--result-json`` (always set under dispatch) the job logs beside that
        envelope; without it, it logs to the suite log.

        With ``--gates`` (Slurm only) it releases each compile key's sim jobs as the key
        finishes; see ``dispatch.gates``. Every sim job keeps its ``afterok`` on this
        job.
        """
        if phase not in BUILD_PHASES:
            # Rejected before anything is written: an unimplemented phase would compile
            # the wrong half of the suite.
            raise FatalRtlBuddyError(
                f"--phase must be one of {', '.join(BUILD_PHASES)} (got {phase!r})."
            )
        # Re-validated before anything is written: a mangled tag must fail loud.
        self._run_tag = validate_run_tag(run_tag)
        if parallel < 1:
            # Rejected before anything is written: zero concurrent builds compile
            # nothing.
            raise FatalRtlBuddyError(
                f"--parallel must be >= 1 (got {parallel}); a build job "
                "allowed zero concurrent builds would compile nothing."
            )
        # Diagnostics only: a nonsensical value is dropped, not raised, since failing
        # would cancel the afterok fan-out.
        # Absent means the config value is --parallel.
        if parallel_configured is None or parallel_configured < parallel:
            parallel_configured = parallel
        self.rtl_builder_mode = (
            "reg" if self.rtl_builder_mode is None else self.rtl_builder_mode
        )
        self.share_build = share_build
        self.rebuild = rebuild
        self._shared_build_root_flag = shared_build_root
        # Resolved before entering the context: relative paths are the head's, and the
        # log derives from the envelope path.
        result_json_path = (
            self._abs_invocation_path(result_json) if result_json is not None else None
        )
        # Likewise the gates manifest: a head path, not the compute node's cwd.
        gates_path = self._abs_invocation_path(gates) if gates is not None else None
        ctx = self._enter_command_context(
            primary_config=test_config,
            log_path=(
                job_log_path(result_json_path) if result_json_path is not None else None
            ),
        )
        suite_cfg = SuiteConfig(path=str(ctx.primary_config))
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        # Config layer owning `compile.parallel` for this suite (the key to edit), read
        # from the suite block the head resolved against.
        # Reported separately from `parallel_requested`.
        parallel_origin = compile_parallel_origin(
            getattr(suite_cfg.get_compile(), "parallel", None) is not None,
            suite_cfg.get_path(),
        )
        log_event(
            logger,
            logging.INFO,
            "command.build_job",
            command="_build-job",
            test_config=test_config,
            reg_level=reg_level,
            start_level=start_level,
            plan=plan,
            parallel=parallel,
            parallel_configured=parallel_configured,
            parallel_origin=parallel_origin,
            gates=gates,
            phase=phase,
            run_tag=self._run_tag,
        )

        if plan is not None:
            # Head-expanded plan: the sweep hook ran on the head, so rebuild each config
            # with no skip/level logic.
            configs = read_plan_configs(self._abs_invocation_path(plan))
        else:
            # Standalone: expand here. _iter_suite_runnables applies level filtering and
            # sweep expansion; its skip/setup rows are irrelevant to a build job.
            discard = []
            configs = list(
                self._iter_suite_runnables(
                    suite_cfg,
                    test_name=None,
                    reg_level=reg_level,
                    start_level=start_level,
                    run_ids=[None],
                    suite_results=discard,
                )
            )

        def _prepare_config(index, cfg):
            """Construct, PRE and probe one config.

            Returns ``(outcome row, group dir, member)``: a terminal outcome row with no
            member, or a member for the group its compile writes into. Shared by both
            loop shapes.
            """
            runner = TestRunner(
                name=self.name + "/build-job",
                root_cfg=self.root_cfg,
                test_cfg=cfg,
                test_runner_mode={"sim_to_stdout": False},
                run_id=None,
                rtl_builder_mode=self.rtl_builder_mode,
                run_depth=RunDepth.COMP,
                suite_dir=suite_dir,
                share_build=share_build,
                shared_build_root=self.shared_build_root,
                # Under dispatch `--rebuild` belongs to the build job: it is the single
                # writer of the shared directory, and its per-process memo rebuilds it
                # once.
                rebuild=rebuild,
                build_phase=phase,
                run_tag=self._run_tag,
            )
            try:
                res = runner.prepare()
                group_dir = None
                if res is None:
                    group_dir, res = runner.compile_group_dir()
            except Exception as exc:  # noqa: BLE001 - see exit-0 contract
                # The exit-0 contract covers this phase too: one config's broken setup
                # (e.g. SystemCSim's missing `cfg-systemc`) is reported failed and must
                # not cancel the afterok fan-out.
                return (
                    (index, cfg.get_name(), False, str(exc), runner, None),
                    None,
                    None,
                )
            if res is not None:
                # A setup or filelist failure never reaches a builder: reported as
                # failed, and the job still exits 0.
                return (
                    (index, cfg.get_name(), False, None, runner, group_dir),
                    None,
                    None,
                )
            return None, group_dir, (index, cfg.get_name(), runner)

        # Config that compiled each group's build, keyed by group dir. A group is one
        # worker's unit, so no two threads touch one key.
        group_leaders = {}
        # The group's first failed compile, as (test name, failure record), keyed the
        # same way. Siblings with the same inputs adopt it rather than fail it again.
        group_failures = {}
        # Distinct from a runner reporting no stamp: a runner class that reports none
        # keeps the leader rule, as with `adopt_group_build`.
        unreported = object()

        # ---- per-key release.
        # Sim jobs are gated `afterok` on this job. The head cannot gate per key, since
        # keys exist only once `run.f` is written here, so this job releases a key's
        # sims once its build and stamp are on disk. A failed key keeps its gate.
        # `afterok` is never cleared: it stays as the orphan safety net
        # (`--kill-on-invalid-dep=yes` reaps the fan-out if this job dies).
        # The scontrol lookup and the manifest are resolved once, lazily, at the first
        # release.
        release_lock = threading.Lock()
        release_state = {"resolved": False, "gates": None, "disabled": None}

        def _resolve_gates_locked():
            """The manifest, or ``None`` with the reason logged. Once."""
            if release_state["resolved"]:
                return release_state["gates"]
            release_state["resolved"] = True
            if shutil.which("scontrol") is None:
                # `scontrol` is an optional binary in the tool manifest: say so once and
                # keep every job on `afterok`.
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.release_unavailable",
                    reason=(
                        "no `scontrol` on PATH; simulation jobs stay gated on "
                        "this build job"
                    ),
                )
                return None
            try:
                token = (
                    read_plan_token(self._abs_invocation_path(plan))
                    if plan is not None
                    else None
                )
            except FatalRtlBuddyError:
                # Unreachable unless the plan file changed since it was parsed; an
                # unreadable token only costs the staleness check.
                token = None
            payload, reason = wait_for_gates(gates_path, run_token=token)
            if payload is None:
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.gates_unavailable",
                    path=str(gates_path),
                    reason=reason,
                )
                return None
            release_state["gates"] = payload
            return payload

        def _release_group(group_dir, members, *, verdict_error=None):
            """Clear the `afterok` of this key's sims. Never raises.

            ``members`` is ``[(plan index, test name), …]`` for the rows this group
            built. Called from the worker right after the group's last member returns.

            ``verdict_error`` is why the build record did not reach disk; the key then
            keeps its gate, since released jobs could not read the file that says the
            build exists.
            """
            if gates_path is None or not members or cancellation_has_started():
                return
            if phase == BUILD_PHASE_VERILATE:
                # No build to release onto: this half emitted sources and a Makefile.
                # The head passes no `--gates` to a verilate job, so this is the second
                # guard.
                return
            if verdict_error is not None:
                # The envelope these jobs would consult is not on disk, so releasing
                # them would start jobs that cannot learn the build exists. They keep
                # `afterok`.
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.release_skipped",
                    group=group_dir,
                    tests=[name for _, name in members],
                    error=verdict_error,
                )
                return
            with release_lock:
                # Held across the wait on purpose: the manifest is resolved once, and a
                # second worker joins that wait rather than starting its own.
                payload = _resolve_gates_locked()
                # A systemic failure (wedged controller, scontrol that will not run)
                # belongs to the node, not to the ids asked about; do not pay its
                # timeout once per remaining key.
                disabled = release_state["disabled"]
            if payload is None or disabled is not None:
                return
            batches = release_batches(payload, [index for index, _ in members])
            if not batches:
                # The manifest does not know these configs (hand-run build job, or plan
                # indices moved): nothing to release.
                return
            # One call per cluster: an array id is unique only on the controller that
            # accepted it.
            released, failures, skipped, systemic = [], [], [], None
            # One deadline for the whole key, shared by its clusters, not one per batch.
            deadline = time.monotonic() + RELEASE_BUDGET_S
            for position, (cluster, job_ids) in enumerate(batches):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    systemic = "release budget exhausted"
                    skipped.extend(
                        job_id for _, ids in batches[position:] for job_id in ids
                    )
                    break
                outcome = release_dependency(
                    job_ids, cluster=cluster, cwd=suite_dir, budget_s=remaining
                )
                released.extend(outcome.released)
                failures.extend(outcome.failures)
                skipped.extend(outcome.skipped)
                if outcome.systemic is not None:
                    systemic = outcome.systemic
                    # Whatever stopped this cluster's batch stops the rest and every
                    # later key in this job.
                    skipped.extend(
                        job_id for _, ids in batches[position + 1 :] for job_id in ids
                    )
                    break
            if systemic is not None:
                with release_lock:
                    release_state["disabled"] = systemic
            if released:
                # On the console, not just the job log: this line says a key stopped
                # holding its tests, and INFO is invisible on a CI console.
                # It goes to stderr via Rich `print`, so the machine JSON stream on
                # stdout is untouched.
                log_console_event(
                    logger,
                    logging.INFO,
                    "dispatch.key_released",
                    group=group_dir,
                    tests=[name for _, name in members],
                    job_ids=released,
                )
            for position, (job_id, error) in enumerate(failures):
                # A failed release only means the job starts when this one ends, so it
                # is a warning and the build continues.
                # The warning that ended the batch names the ids never attempted; it is
                # the last one recorded because the loop breaks straight after appending
                # it.
                is_systemic = systemic is not None and position == len(failures) - 1
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.release_failed",
                    group=group_dir,
                    job_id=job_id,
                    error=error,
                    skipped=len(skipped) if is_systemic else None,
                )
            if systemic is not None and not failures:
                # The budget ran out between calls, so no single id failed: say it
                # against the key.
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.release_failed",
                    group=group_dir,
                    error=systemic,
                    skipped=len(skipped),
                )

        def _build_entry(name, ok, worker_error, runner, group_dir):
            """One config's envelope record. Pure but for the stamp refresh.

            Called once per config: from ``_record_group`` as each group finishes, and
            from the tail for a config no group ran (PRE failure, cancelled worker). The
            tail reuses what is recorded, so ``refresh_build_stamp`` runs once per
            config.
            """
            record = runner.last_compile or {}
            build_entry = {
                "test": name,
                # The runner's own builder when no compile plan was derived (a config
                # whose PRE failed): distinguishes "never compiled" from "unknown
                # builder".
                "builder": record.get("builder")
                or getattr(runner, "builder_name", None),
                "duration_sec": record.get("duration_sec"),
                "reused": record.get("reused"),
                # Suite-relative, not absolute and not a basename: an absolute path pins
                # the compute node's mount, and a basename collides (every unshared
                # output is `simv`).
                # Equal values mean one single-writer output; distinct values mean two.
                "group": (os.path.relpath(group_dir, suite_dir) if group_dir else None),
            }
            for half in ("verilate_sec", "build_sec"):
                # For a split compile, `duration_sec` above is the whole and these say
                # where the time went. Absent for an unsplit compile.
                if record.get(half) is not None:
                    build_entry[half] = record[half]
            # The stamp after every member is done: a sibling's adoption rewrote its
            # listing after the leader recorded, and gated jobs validate against the
            # final one.
            refresh_stamp = getattr(runner, "refresh_build_stamp", None)
            if callable(refresh_stamp):
                refresh_stamp()
            stamp = getattr(runner, "last_build_stamp", None) or {}
            if ok and stamp.get("fingerprint_sha") is not None:
                # Which inputs this build was made from: a gated sim job that cannot
                # validate the stamp compares its fingerprint against this, and declines
                # to recompile either way.
                build_entry["fingerprint_sha"] = stamp["fingerprint_sha"]
            if ok and getattr(runner, "stamp_write_failed", False):
                # Built, but with nothing on disk to say so. Gated sim jobs read this as
                # "built" and get a reason naming the write. Absent means "stamped".
                build_entry["stamp_written"] = False
            if not ok:
                # Why it failed, carried in the envelope rather than only the job log.
                # Additive and best-effort under the exit-0 contract.
                try:
                    _annotate_build_failure(
                        build_entry,
                        failure=getattr(runner, "last_compile_failure", None),
                        worker_error=worker_error,
                        suite_dir=suite_dir,
                    )
                except Exception as exc:  # noqa: BLE001 - never fatal here
                    log_event(
                        logger,
                        logging.WARNING,
                        "build_job.failure_detail_failed",
                        test=name,
                        error=str(exc),
                    )
            return build_entry

        # ---- the envelope, written as the job goes.
        # A released sim whose stamp fails to validate asks this envelope whether the
        # build exists; "no envelope" is `inconclusive`, which recompiles.
        # So the verdict is persisted before the dependency is cleared: each group
        # rewrites the envelope marked `partial`, and the final write drops the mark.
        recorded_lock = threading.Lock()
        recorded_entries = {}  # plan index -> envelope record
        recorded_ok = {}  # plan index -> did it build?

        def _record_group(rows):
            """Persist this group's outcomes; return ``None`` or why it failed.

            Called before the group's release, and its answer decides whether the
            release happens: a failed write costs the key its early start, never the
            job's exit status.

            ``result_json_path is None`` is a hand-run build job with no head and no
            gated simulation, not a failure; the head passes ``--result-json`` on every
            submission that passes ``--gates``.
            """
            entries = {
                index: (
                    _build_entry(name, ok, worker_error, runner, group_dir),
                    ok,
                )
                for index, name, ok, worker_error, runner, group_dir in rows
            }
            with recorded_lock:
                for index, (entry, ok) in entries.items():
                    recorded_entries[index] = entry
                    recorded_ok[index] = ok
                if result_json_path is None:
                    return None
                ordered = sorted(recorded_entries)
                snapshot = [recorded_entries[index] for index in ordered]
                names = {
                    index: recorded_entries[index].get("test") for index in ordered
                }
                try:
                    # Under the lock, so two workers finishing together cannot
                    # interleave a stale snapshot over a fresher one. tmp + os.replace
                    # gives readers a whole file.
                    write_build_result_json(
                        result_json_path,
                        built=[names[index] for index in ordered if recorded_ok[index]],
                        failed=[
                            names[index] for index in ordered if not recorded_ok[index]
                        ],
                        builds=snapshot,
                        partial=True,
                    )
                except Exception as exc:  # noqa: BLE001 - never fatal here
                    log_event(
                        logger,
                        logging.WARNING,
                        "build_job.partial_result_failed",
                        path=str(result_json_path),
                        error=str(exc),
                    )
                    return str(exc)
                return None

        def _compile_group(group):
            """Compile one group's configs serially; return rows for the caller.

            The first member compiles; the rest adopt what it built. Members share one
            compile key, so a sibling's fingerprint can differ from the leader's stamp
            only through a file that moved during this job (e.g. this member's preproc
            output under an ``+incdir+``), and re-deriving the stamp would Verilate an
            identical design twice. The sibling checks the leader's ``deps``: a
            differing input the build consumed means two tests compile different bytes
            under one key, which is reported, not recompiled, because a recompile would
            make the last writer decide what both simulate.

            A failed compile is adopted the same way: a sibling whose compile inputs
            are identical takes the failure (return code and transcript) instead of
            failing the same compile again.

            Re-checks the cancellation latch before every member and when a worker takes
            the next group. The pool cancels only pending futures, and
            ``ThreadPoolExecutor.__exit__`` waits for a worker that already took the
            next group, which could start a compiler the signal sweep cannot see. The
            latch makes that worker a no-op; the handler cannot cancel directly because
            it runs between bytecodes with the pool's internals mid-flight.
            """
            group_dir, members = group
            rows = []
            for index, name, runner in members:
                if cancellation_has_started():
                    # Reported failed like anything that never reached a builder:
                    # `failed` is all the envelope can say about a compile that did not
                    # happen.
                    # In practice the envelope is not written; the handler re-raises out
                    # of the pool.
                    rows.append((index, name, False, None, runner, group_dir))
                    continue
                leader = group_leaders.get(group_dir)
                # getattr, like every optional runner capability: a runner class with no
                # adopt keeps the default path.
                adopt = getattr(runner, "adopt_group_build", None)
                if leader is not None and adopt is not None:
                    try:
                        verdict, detail = adopt()
                    except Exception as exc:  # noqa: BLE001 - exit-0 contract
                        rows.append((index, name, False, str(exc), runner, group_dir))
                        continue
                    if verdict == "adopted":
                        rows.append((index, name, True, None, runner, group_dir))
                        continue
                    if verdict == "drift":
                        log_event(
                            logger,
                            logging.WARNING,
                            "build_job.group_input_drift",
                            test=name,
                            leader=leader,
                            dependency=detail,
                        )
                        rows.append((index, name, False, None, runner, group_dir))
                        continue
                    # Undecidable (no dependency file, no stamp, or a moved compile
                    # line): the leader's full comparison decides.
                    # Logged at INFO so a decline is visible: it is the difference
                    # between compiling a key once and N times.
                    log_event(
                        logger,
                        logging.INFO,
                        "build_job.group_adoption_declined",
                        test=name,
                        leader=leader,
                        reason=detail,
                    )
                failed_leader = group_failures.get(group_dir)
                adopt_failure = getattr(runner, "adopt_group_failure", None)
                if failed_leader is not None and adopt_failure is not None:
                    # A deterministic failure (a fatal lint warning) would fail every
                    # sibling the same way, one full compile each, serially.
                    failed_name, failure = failed_leader
                    try:
                        verdict, detail = adopt_failure(failure, leader=failed_name)
                    except Exception as exc:  # noqa: BLE001 - exit-0 contract
                        rows.append((index, name, False, str(exc), runner, group_dir))
                        continue
                    if verdict == "adopted":
                        rows.append((index, name, False, None, runner, group_dir))
                        continue
                    log_event(
                        logger,
                        logging.INFO,
                        "build_job.group_failure_adoption_declined",
                        test=name,
                        leader=failed_name,
                        reason=detail,
                    )
                try:
                    res = runner.compile_prepared()
                except Exception as exc:  # noqa: BLE001 - see exit-0 contract
                    # A worker exception must never escape: a non-zero build job exit
                    # makes Slurm cancel every afterok sim job. The group's remaining
                    # members still get their attempt.
                    rows.append((index, name, False, str(exc), runner, group_dir))
                    continue
                built = isinstance(res, EarlyStopResults)
                if built:
                    # This member's build is what the rest of the group adopts, but only
                    # if it left a stamp: without one every sibling would adopt(), find
                    # none and compile anyway, so warn once.
                    # `last_build_stamp` names the stamp's directory, which for an
                    # unshared build is not `group_dir`. The verilate half writes no
                    # stamp by design, so its absence is not a fault.
                    if getattr(runner, "last_build_stamp", unreported) is None:
                        if phase != BUILD_PHASE_VERILATE:
                            log_event(
                                logger,
                                logging.WARNING,
                                "build_job.group_leader_unstamped",
                                test=name,
                                group=group_dir,
                            )
                    else:
                        group_leaders.setdefault(group_dir, name)
                elif isinstance(res, CompileFailResults):
                    # The builder ran and failed; a filelist or setup failure never
                    # reached it and has nothing to adopt.
                    failure = getattr(runner, "last_compile_failure", None)
                    if failure is not None:
                        group_failures.setdefault(group_dir, (name, failure))
                rows.append((index, name, built, None, runner, group_dir))
            # Outside the loop, so outside every build-directory lock `compile_prepared`
            # took: this key is built and stamped.
            # Envelope first, release second: a job released before its verdict is on
            # disk would recompile into the directory it was gated on.
            verdict_error = _record_group(rows)
            # Per row, not per group: a failed member, or one whose build left no stamp,
            # keeps its gate.
            _release_group(
                group_dir,
                [
                    (index, name)
                    for index, name, built, _error, runner, _dir in rows
                    if built
                    and getattr(runner, "last_build_stamp", unreported) is not None
                ],
                verdict_error=verdict_error,
            )
            return rows

        # ---- serial phase: construct, PRE, and probe the compile key.
        # PRE stays on the main thread whatever --parallel says: hook execution is
        # process-global-serial (see hooks.py), preproc code may write suite-level files
        # configs would race on, and a preproc may mutate test_cfg, so the compile key
        # exists only after pre().
        # Outcome rows are (plan index, test name, built?, worker error, runner, group
        # dir), replayed in plan order so identical runs log identically.
        # `streaming` (default) runs PRE then COMPILE per config; batching every PRE
        # first would let a preproc hook that rewrites a suite-level input clobber an
        # earlier config's before it compiled. `parallel > 1` opts into batching; see
        # docs/known-issues.md.
        streaming = min(parallel, len(configs)) <= 1
        outcomes = []
        groups = {}
        for index, cfg in enumerate(configs):
            row, group_dir, member = _prepare_config(index, cfg)
            if row is not None:
                outcomes.append(row)
                continue
            # Group by the directory the compile will write, not by config: configs with
            # one compile key share a build dir, and later ones short-circuit on the
            # first's stamp.
            # Test names in a plan are assumed unique (two configs with one name share a
            # per-test `run.f`).
            groups.setdefault(group_dir, []).append(member)
            if streaming:
                # Compile now, before the next config's hook runs. The group still
                # records membership so telemetry reads the same in both shapes.
                outcomes.extend(_compile_group((group_dir, [member])))

        # ---- parallel phase: one worker per distinct build.
        pool_size = max(1, min(parallel, len(groups)))
        prepared = sum(len(members) for members in groups.values())
        if pool_size > 1 or parallel > pool_size:
            # Liveness on a CI console (INFO shows only under -v). The second condition
            # flags over-reservation (the plan collapsed to fewer compile keys than
            # `parallel`); it is not an error, so it stays INFO.
            log_console_event(
                logger,
                logging.INFO,
                "build_job.pool_configured",
                groups=len(groups),
                # Why the group count can be below `parallel`: configs sharing a compile
                # key are one group and siblings adopt the leader's build.
                # Counts cover configs that reached the pool only; one whose PRE or
                # filelist probe failed never joined a group.
                configs=prepared,
                # ...and the ones that did not. Derived from the plan, because
                # `outcomes` also holds the streaming shape's already-compiled rows.
                unprepared=len(configs) - prepared,
                parallel=pool_size,
                parallel_requested=parallel,
                # The config value, which is `parallel_requested` unless the head's plan
                # cap lowered it: quote the number the named key holds.
                parallel_configured=parallel_configured,
                # Name the key a reader would edit: a suite that set `compile.parallel`
                # is not moved by cfg-dispatch.
                parallel_origin=parallel_origin,
            )

        # Streaming shape: every group was compiled as it was prepared.
        if not streaming:
            if pool_size > 1:
                # Threads, not processes: the work is a subprocess wait, and prepared
                # TestRunners would not survive a fork/spawn.
                # Worker threads install no signal handlers and each compiler runs in
                # its own session, so SIGTERM to this job would orphan in-flight
                # Verilations. While the pool runs, the main thread owns SIGINT/SIGTERM
                # and sweeps live compilers by hand. Streaming needs none of it:
                # run_managed_process is on the main thread.
                # The handler follows run_managed_process (chain, then KeyboardInterrupt
                # / SystemExit(128+signum)) and does no logging: a worker may hold the
                # logging lock.
                previous_handlers = {}

                def _sweep_compilers_and_reraise(signum, frame):
                    terminate_live_managed_processes()
                    previous = previous_handlers.get(signum)
                    if callable(previous):
                        previous(signum, frame)
                    if signum == signal.SIGINT:
                        raise KeyboardInterrupt
                    raise SystemExit(128 + signum)

                for signum in (signal.SIGINT, signal.SIGTERM):
                    try:
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, _sweep_compilers_and_reraise)
                    except ValueError:
                        # Only reachable off the main thread; the exit-0 contract
                        # outranks the cleanup, so an escaping ValueError must not fail
                        # the build job.
                        previous_handlers.pop(signum, None)
                try:
                    with ThreadPoolExecutor(max_workers=pool_size) as pool:
                        for rows in pool.map(_compile_group, list(groups.items())):
                            outcomes.extend(rows)
                finally:
                    # Restored on every exit, including the re-raise above: the tail
                    # below runs on the main thread with no pool to protect.
                    for signum, handler in previous_handlers.items():
                        signal.signal(signum, handler)
            else:
                # `parallel` exceeded the group count: one group, but the plan held
                # several configs, so the batched shape was already chosen.
                for group in groups.items():
                    outcomes.extend(_compile_group(group))

        built, failed, builds = [], [], []
        for index, name, ok, worker_error, runner, group_dir in sorted(
            outcomes, key=lambda row: row[0]
        ):
            # Plan order, one row per planned config: a config that never reached a
            # builder still names its builder, so a gap means "never seen", not
            # "compiled instantly".
            build_entry = recorded_entries.get(index)
            if build_entry is None:
                build_entry = _build_entry(name, ok, worker_error, runner, group_dir)
            builds.append(build_entry)
            if ok:
                built.append(name)
                continue
            failed.append(name)
            if worker_error is not None:
                log_event(
                    logger,
                    logging.WARNING,
                    "build_job.compile_worker_error",
                    test=name,
                    error=worker_error,
                )
            log_event(
                logger,
                logging.WARNING,
                "build_job.compile_failed",
                test=name,
            )
        log_event(
            logger,
            logging.INFO,
            "build_job.done",
            built=len(built),
            failed=len(failed),
            # Both numbers: `parallel_requested` is the budget the head reserved CPUs
            # for; `parallel` is what the job could use once the plan collapsed to
            # distinct builds.
            parallel=pool_size,
            parallel_requested=parallel,
            parallel_configured=parallel_configured,
            parallel_origin=parallel_origin,
            groups=len(groups),
            phase=phase,
        )
        if result_json_path is not None:
            # Persist the outcome so the head can map a compile failure to a CompileFail
            # row (parity with the in-process path).
            try:
                write_build_result_json(
                    result_json_path, built=built, failed=failed, builds=builds
                )
            except Exception as exc:  # noqa: BLE001 - telemetry is never fatal
                # Compile records are additive telemetry; built/failed is what maps a
                # failure to a CompileFail row. On an unserialisable record, drop the
                # telemetry and write the bare envelope.
                log_event(
                    logger,
                    logging.WARNING,
                    "build_job.build_records_failed",
                    error=str(exc),
                )
                try:
                    write_build_result_json(
                        result_json_path, built=built, failed=failed
                    )
                except Exception as exc2:  # noqa: BLE001 - never fatal here
                    # The retry can fail for the same reason as the first write, and
                    # run() catches only click exits, FatalRtlBuddyError and
                    # FilelistError: an escape would exit the build job non-zero and
                    # cancel the sim fan-out.
                    # Losing the envelope only costs the compile-failure mapping.
                    log_event(
                        logger,
                        logging.WARNING,
                        "build_job.result_json_failed",
                        path=str(result_json_path),
                        error=str(exc2),
                    )
        if self.machine:
            # Reporting only, so it must not change the exit status: a non-zero build
            # job makes the scheduler cancel every afterok dependent.
            try:
                self._emit_machine_result("_build-job", 0, built=built, failed=failed)
            except Exception as exc:  # noqa: BLE001 - telemetry is never fatal
                log_event(
                    logger,
                    logging.WARNING,
                    "build_job.machine_result_failed",
                    error=str(exc),
                )
        # Always exit 0: a per-test compile failure is not a build-job failure. Only a
        # fatal setup error (raised above) fails the job.
        raise typer.Exit(0)

    def _append_skip_results(
        self, test_name, desc, run_ids, suite_results, builder=None
    ):
        test_results = SkipResults(name=test_name + "/results", desc=desc)
        for run_id in run_ids:
            suite_results.append(
                {
                    "test_name": test_name,
                    "randmode_i": run_id,
                    "results": test_results,
                    "builder": builder,
                }
            )

    def _append_setup_results(
        self, test_name, desc, run_ids, suite_results, builder=None
    ):
        test_results = SetupFailResults(name=test_name + "/results", desc=desc)
        for run_id in run_ids:
            suite_results.append(
                {
                    "test_name": test_name,
                    "randmode_i": run_id,
                    "results": test_results,
                    "builder": builder,
                }
            )

    def _expand_tests_with_sweep(self, test_cfg, suite_dir):
        script_path = test_cfg.get_sweep_path()
        if script_path is None:
            return [test_cfg], None

        with open(script_path, "r") as file:
            code = file.read()

        try:
            ns = exec_hook_script(
                script_path,
                code,
                stage="sweep",
                logger=logger,
                TestConfig=TestConfig,
                test_cfg=test_cfg,
                root_cfg=self.root_cfg,
                suite_dir=suite_dir,
                artifact_dir=str(
                    test_artifact_dir(
                        suite_dir, test_cfg.get_name(), run_tag=self._run_tag
                    )
                ),
                out_test_cfgs=[],
            )
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "sweep.failed",
                test=test_cfg.name,
                script=script_path,
                error=e,
            )
            logger.debug("sweep traceback", exc_info=True)
            return [], f"Setup failed in sweep: {e}"

        log_event(
            logger,
            logging.INFO,
            "sweep.completed",
            test=test_cfg.name,
            script=script_path,
            expanded=len(ns["out_test_cfgs"]),
        )
        return ns["out_test_cfgs"], None

    def _run_test_cfg_for_run_ids(
        self,
        test_cfg,
        run_ids,
        seed_mode: SeedMode,
        replay_run_id,
        test_runner_mode,
        suite_dir,
        master_seed: int | None = None,
    ):
        test_runner = TestRunner(
            name=self.name + "/testrunner",
            root_cfg=self.root_cfg,
            test_cfg=test_cfg,
            test_runner_mode=test_runner_mode,
            run_id=run_ids[0],
            seed_mode=seed_mode,
            replay_run_id=replay_run_id,
            rtl_builder_mode=self.rtl_builder_mode,
            run_depth=self.run_depth,
            suite_dir=suite_dir,
            share_build=self.share_build,
            shared_build_root=self.shared_build_root,
            expect_prebuilt=self.expect_prebuilt,
            rebuild=self.rebuild,
            build_result_json=self.build_result_json,
            run_tag=self._run_tag,
        )

        if len(run_ids) == 1:
            results = [test_runner.run()]
        else:
            results = test_runner.run_multiple(run_ids)
        if test_cfg.is_xfail():
            # FAIL->XFAIL (pass) and PASS->XPASS (failure only when strict) let a
            # known-failing test live in a suite. A failure instead of a verdict (setup,
            # compile, timeout) keeps FAIL; see _apply_xfail_logged.
            for res in results:
                self._apply_xfail_logged(res, test_cfg, "suite.xfail")
        get_resolved_seed = getattr(test_cfg, "get_resolved_seed", None)
        resolved_seed = get_resolved_seed() if callable(get_resolved_seed) else None
        if resolved_seed is not None:
            seed_record = {
                "master_seed": master_seed,
                "resolved_seed": resolved_seed,
                "source": getattr(test_cfg, "seed_source", None),
                "identity": getattr(test_cfg, "seed_identity", None),
            }
            for res in results:
                res.results["seed"] = dict(seed_record)
        compile_record = test_runner.last_compile
        if compile_record is not None:
            # Same key the dispatch path folds in from the build envelope, so `rb graph
            # results` reads one shape. Recorded before the envelope is written: the sim
            # instance goes out of scope with the runner.
            for res in results:
                res.results["compile"] = dict(compile_record)
        build_stamp = getattr(test_runner, "last_build_stamp", None)
        if build_stamp is not None:
            # The build this run simulated: the compile key its stamp was written for
            # and the executable it launched.
            # The head cross-checks runs of one key at collect. A multi-run runner's own
            # per-launch stamp is kept.
            for res in results:
                res.results.setdefault("build_stamp", dict(build_stamp))
        if self._plusarg_overrides:
            # What this invocation added on the command line, so a result tells a
            # one-off `--plusarg` run from its tests.yaml entry.
            # Absent without the flag, so existing envelopes keep their keys.
            for res in results:
                res.results["plusarg_overrides"] = dict(self._plusarg_overrides)
        self._record_run_results(test_cfg, suite_dir, run_ids, results)
        return results

    def _invocation_run_token(self) -> str:
        """This process's result-envelope nonce, minted on first use.

        Identifies the run that produced an envelope. `_test-job` overwrites it with the
        head's token so a dispatched run and its collected envelope agree.
        """
        if self._run_token is None:
            self._run_token = uuid.uuid4().hex
        return self._run_token

    def _record_run_results(self, test_cfg, suite_dir, run_ids, results):
        """Write each run's result envelope into its artifact directory.

        The durable record of what a test did, written wherever a test runs; the
        dispatch path's `dispatch/result-*.json` exists only when a head asked for one.
        Best-effort: a passed run is never reported failed because its side-car could
        not be written.
        """
        token = self._invocation_run_token()
        for run_id, res in zip(run_ids, results):
            path = (
                Path(
                    test_artifact_dir(
                        suite_dir,
                        test_cfg.get_name(),
                        run_id=run_id,
                        run_tag=self._run_tag,
                    )
                )
                / RESULT_JSON_NAME
            )
            try:
                write_result_json(
                    path,
                    test_name=test_cfg.get_name(),
                    run_id=run_id,
                    results=res,
                    run_token=token,
                    run_tag=self._run_tag,
                )
                # Remember where the envelope landed so coverage post-processing can
                # re-persist it; see _refresh_result_side_cars.
                res.result_json_path = str(path)
            except Exception as exc:  # noqa: BLE001 - best-effort side-car; a passed run is never reported failed over its envelope
                log_event(
                    logger,
                    logging.WARNING,
                    "test.result_json_write_failed",
                    test=test_cfg.get_name(),
                    run_id=run_id,
                    path=str(path),
                    error=str(exc),
                )

    def _refresh_result_side_cars(self, suite_results):
        """Re-persist result envelopes after coverage post-processing.

        `_record_run_results` writes before the LCOV export, HTML tree and Coverview
        archive exist, and the coverage dict is mutated afterwards. Best-effort, like
        the first write.
        """
        for suite_result in suite_results:
            res = suite_result.get("results")
            path = getattr(res, "result_json_path", None)
            if path is None:
                continue
            try:
                refresh_result_json(path, res)
            except Exception as exc:  # noqa: BLE001 - best-effort side-car
                log_event(
                    logger,
                    logging.WARNING,
                    "test.result_json_refresh_failed",
                    test=suite_result.get("test_name"),
                    path=path,
                    error=str(exc),
                )

    def _append_results(self, test_name, run_ids, results, suite_results, builder=None):
        for run_id, test_results in zip(run_ids, results):
            suite_results.append(
                {
                    "test_name": test_name,
                    "randmode_i": run_id,
                    "results": test_results,
                    "builder": builder,
                }
            )

    def _format_coverage_summary(self, test_results):
        return self.coverage.format_summary(test_results)

    @staticmethod
    def _machine_coverage(test_results):
        """Structured per-test coverage for the machine payload, or None.

        Returns the `{line, branch, toggle, functional}` percentages, plus `covers`
        (per-cover-point names and hit counts) when the test recorded user coverage.
        None when the test produced no coverage data.
        """
        cov = test_results.results.get("coverage")
        if not cov:
            return None
        metrics = {k: cov.get(k) for k in ("line", "branch", "toggle", "functional")}
        covers = cov.get("covers")
        if all(v is None for v in metrics.values()) and not covers:
            return None
        if covers:
            metrics["covers"] = covers
        return metrics

    def _machine_test_row(self, test_name, test_results, *, suite=None, run_id=None):
        """Build one machine-mode result row, attaching structured coverage."""
        res = test_results.results
        row = {"name": test_name, "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        if run_id is not None:
            row["run_id"] = run_id
        if "seed" in res:
            row["seed"] = res["seed"]
        # An NA that stopped on purpose (-E pre|comp|sim) keeps the exit code at 0;
        # `--machine` consumers need this discriminator rather than `desc`.
        if res.get(EARLY_STOP_KEY):
            row["early_stop"] = True
        # The `rb test --plusarg` overrides this run applied; present only when there
        # were any.
        if res.get("plusarg_overrides"):
            row["plusarg_overrides"] = res["plusarg_overrides"]
        cov = self._machine_coverage(test_results)
        if cov is not None:
            row["coverage"] = cov
        return row

    @staticmethod
    def _machine_coverage_payload(coverage):
        """Return the run-level coverage payload if it carries data, else None.

        Any of `covers`, `artefacts`, `merge_failed` or `source_summary` counts as data
        on its own, since each is produced without a `--coverage-merge*` flag or when a
        merge died before writing anything.
        """
        if coverage and (
            coverage.get("merged")
            or coverage.get("dir_summary")
            or coverage.get("covers")
            or coverage.get("artefacts")
            or coverage.get("merge_failed")
            or coverage.get("source_summary")
        ):
            return coverage
        return None

    @staticmethod
    def _format_assertions_summary(test_results):
        """Return a short Assertions cell, or None when the test didn't enable SVA.

        Shape: `"<fired> fired"`; anything above zero is already a FAIL in the Result
        column.
        """
        assertions = test_results.results.get("assertions")
        if not assertions or not assertions.get("enabled"):
            return None
        return f"{assertions.get('fired', 0)} fired"

    def _do_test_suite(
        self,
        suite_cfg,
        test_name=None,
        test_runner_mode={"sim_to_stdout": True},
        reg_level=None,
        start_level=None,
        run_ids=None,
        seed_mode: SeedMode = SeedMode.DEFAULT,
        replay_run_id=None,
        master_seed: int | None = None,
    ):
        if run_ids is None:
            run_ids = [None]
        if master_seed is not None and len(run_ids) != 1:
            raise FatalRtlBuddyError(
                "--master-seed requires one run id per expanded test"
            )
        seed_run_id = run_ids[0] if len(run_ids) == 1 else None

        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        suite_results = []
        for expanded_test_cfg in self._iter_suite_runnables(
            suite_cfg,
            test_name=test_name,
            reg_level=reg_level,
            start_level=start_level,
            run_ids=run_ids,
            suite_results=suite_results,
        ):
            self._resolve_test_seed(
                expanded_test_cfg,
                master_seed=master_seed,
                suite_config_path=suite_cfg.get_path(),
                run_id=seed_run_id,
                seed_mode=seed_mode,
            )
            # A sweep-expanded test may carry its own `builder:`; resolve per expansion.
            exp_builder = self.root_cfg.resolve_rtl_builder_cfg(
                expanded_test_cfg.get_builder_name()
            ).get_name()
            run_results = self._run_test_cfg_for_run_ids(
                test_cfg=expanded_test_cfg,
                run_ids=run_ids,
                seed_mode=seed_mode,
                replay_run_id=replay_run_id,
                test_runner_mode=test_runner_mode,
                suite_dir=suite_dir,
                master_seed=master_seed,
            )
            self._append_results(
                expanded_test_cfg.name,
                run_ids,
                run_results,
                suite_results,
                builder=exp_builder,
            )
        return suite_results

    def _iter_suite_runnables(
        self, suite_cfg, *, test_name, reg_level, start_level, run_ids, suite_results
    ):
        """Yield the suite's sweep-expanded runnable test configs.

        Level-filtered tests and failed sweeps are not yielded; their SKIP / SetupFail
        rows are appended to ``suite_results`` in test order, so callers only decide how
        to execute runnable configs.

        A ``--plusarg`` override is merged in here, after sweep expansion, so it wins
        over a sweep hook that rewrites ``plusargs`` wholesale. Both the in-process
        runner and the dispatch planner go through here.
        """
        tests = suite_cfg.get_tests(test_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        for t in tests:
            # The builder this test runs on (per-test/suite `builder:`, `--builder`, or
            # the platform default); stamped onto each result row and used to resolve
            # per-builder regression levels.
            t_builder = self.root_cfg.resolve_rtl_builder_cfg(
                t.get_builder_name()
            ).get_name()
            if reg_level is not None or start_level is not None:
                t_lvl = t.get_reglvl(t_builder)
            else:
                t_lvl = 0
            if reg_level is not None and t_lvl > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "suite.skip",
                    test=t.name,
                    reason="above_regression_level",
                    test_level=t_lvl,
                    reg_level=reg_level,
                )
                self._append_skip_results(
                    t.name,
                    f"lvl {t_lvl} > cmd end_level {reg_level}",
                    run_ids,
                    suite_results,
                    builder=t_builder,
                )
                continue

            if start_level is not None and t_lvl < start_level:
                log_event(
                    logger,
                    logging.INFO,
                    "suite.skip",
                    test=t.name,
                    reason="below_start_level",
                    test_level=t_lvl,
                    start_level=start_level,
                )
                self._append_skip_results(
                    t.name,
                    f"lvl {t_lvl} < cmd start_level {start_level}",
                    run_ids,
                    suite_results,
                    builder=t_builder,
                )
                continue

            expanded_tests, sweep_error = self._expand_tests_with_sweep(
                t, suite_dir=suite_dir
            )
            if sweep_error is not None:
                self._append_setup_results(
                    t.name, sweep_error, run_ids, suite_results, builder=t_builder
                )
                continue

            for expanded in expanded_tests:
                yield expanded.with_plusarg_overrides(self._plusarg_overrides)

    def _dispatch_backend_name(self, dispatch):
        """The selected backend name: CLI ``--dispatch`` over ``cfg-dispatch``."""
        if dispatch is not None:
            return dispatch
        return self.root_cfg.get_dispatch_cfg().backend

    def _validate_jobs_flag(self, backend_name, jobs):
        """Reject ``--jobs`` where it cannot mean anything.

        Called on every path that accepts the flag, including ones that skip dispatch (a
        randtest replay), so the flag is never silently dropped.
        """
        if jobs is None:
            return
        if backend_name != LocalProcessBackend.name:
            raise FatalRtlBuddyError(
                f"--jobs sizes the --dispatch {LocalProcessBackend.name} pool, "
                f"but the backend is {backend_name or 'local'}: 'local' runs "
                "one job at a time in-process, and Slurm concurrency is "
                "cfg-dispatch.max-jobs-per-array."
            )
        if jobs < 1:
            raise FatalRtlBuddyError(
                f"--jobs must be >= 1 (got {jobs}); a pool of zero would "
                "never start a job."
            )

    def _resolve_dispatch_backend(self, dispatch, *, jobs=None):
        """Instantiate the dispatch backend named by ``--dispatch`` (or config).

        ``--dispatch`` wins over ``cfg-dispatch.backend``; both default to ``local``
        (in-process, returned as ``None``). ``--jobs`` overrides ``cfg-dispatch.jobs``
        for this run.
        """
        backend_name = self._dispatch_backend_name(dispatch)
        self._validate_jobs_flag(backend_name, jobs)
        dispatch_cfg = self.root_cfg.get_dispatch_cfg()
        if jobs is not None:
            dispatch_cfg = replace(dispatch_cfg, jobs=jobs)
        backend = create_dispatch_backend(
            backend_name,
            dispatch_cfg,
            # Root config this `cfg-dispatch` came from. This runs once before the suite
            # loop, so it stays the orchestration config after `root_cfg` is rebuilt for
            # another root; it is the file an `sbatch-args` hint must name.
            config_path=getattr(self.root_cfg, "root_cfg_path", None),
        )
        # Validate `--orphans` / `cfg-dispatch.orphans` against the selected backend
        # before planning.
        self._orphans_policy = self._resolve_orphans_policy(backend)
        return backend

    def _resolve_orphans_policy(self, backend):
        """Return ``warn`` / ``cancel`` / ``adopt`` for this run.

        Precedence: ``--orphans``, then ``cfg-dispatch.orphans``, then ``warn``.
        Validated here rather than by Typer so the flag and config key give one message
        before anything is submitted. Resolved once, beside the backend, so one suite's
        root_config.yaml cannot decide another suite's orphans.

        Only a scheduler-backed backend can leave jobs behind. ``adopt`` given
        explicitly is fatal on any other backend; inherited from config it degrades to
        ``warn`` with a notice, so a project can still run ``rb test`` locally.
        """
        value = self._orphans
        if value is None:
            value = self.root_cfg.get_dispatch_cfg().orphans
        else:
            value = value.strip().lower()
            if value not in ORPHANS_POLICIES:
                raise FatalRtlBuddyError(
                    f"--orphans must be one of {', '.join(ORPHANS_POLICIES)} "
                    f"(got {self._orphans!r})."
                )
        if backend is not None and backend.scheduled:
            return value
        name = backend.name if backend is not None else "local"
        if value == "adopt" and self._orphans is not None:
            raise FatalRtlBuddyError(
                f"--orphans adopt needs a scheduler-backed dispatch backend, "
                f"but this run uses {name}: its jobs are this process's own "
                "children and die with the head, so there is nothing to "
                "adopt. Re-run the tests, or use --dispatch slurm."
            )
        if value != "warn":
            log_event(
                logger,
                logging.WARNING,
                "dispatch.orphans_ignored",
                orphans=value,
                backend=name,
                reason=(
                    "only a scheduler-backed backend can leave jobs running "
                    "after the head exits"
                ),
            )
        return "warn"

    def _reject_early_stop_under_dispatch(self, dispatch, backend):
        """Reject ``--early-stop`` under dispatch, naming what selected the backend.

        No stop point earlier than POST is expressible per job. The message says whether
        ``--dispatch`` or ``cfg-dispatch.backend`` chose the backend, so the remedy it
        offers applies.
        """
        if self.run_depth == RunDepth.POST:
            return
        if dispatch is not None:
            source = f"--dispatch {dispatch}"
            remedy = "run without --dispatch to stop earlier"
        else:
            source = f"cfg-dispatch.backend: {backend.name}"
            remedy = (
                "pass --dispatch local (or clear cfg-dispatch.backend) to stop earlier"
            )
        raise FatalRtlBuddyError(
            f"--early-stop {self.run_depth.value} cannot be combined with "
            f"dispatch ({source}): a build job compiles and the sim jobs run "
            f"sim+post; {remedy}."
        )

    def _open_run_manifest(
        self,
        backend,
        dispatch_root,
        *,
        suite_cfg,
        suite_dir,
        run_token,
        started_at,
        plan_path,
        rows,
    ):
        """Create this suite's empty run record; ``None`` if it cannot be.

        Only for a backend the scheduler keeps alive; local-parallel jobs die with the
        head. Never fatal: a failure only costs the next run the ability to find this
        fleet.
        """
        if not backend.scheduled:
            return None
        path = run_manifest_path(dispatch_root, run_token)
        try:
            return write_run_manifest(
                path,
                run_token=run_token,
                backend=backend.name,
                started_at=started_at,
                suite_config=Path(suite_cfg.get_path()).resolve(),
                plan=plan_path,
                rows=rows,
                status=STATUS_SUBMITTING,
            )
        except (OSError, TypeError, ValueError) as e:
            log_event(
                logger,
                logging.WARNING,
                "dispatch.run_manifest_write_failed",
                suite_dir=suite_dir,
                path=str(path),
                error=str(e),
            )
            return None

    @staticmethod
    def _grow_run_manifest(path, amend, value, *, suite_dir):
        """Apply one incremental update to the run record, best effort.

        The recorded submissions are already accepted, so nothing here may raise into
        the fan-out and cancel a correctly launched fleet.
        """
        if path is None:
            return
        reason = amend(path, value)
        if reason is not None:
            log_event(
                logger,
                logging.WARNING,
                "dispatch.run_manifest_write_failed",
                suite_dir=suite_dir,
                path=str(path),
                error=reason,
            )

    @staticmethod
    def _close_run_manifest(state, status):
        """Record how one suite's fleet ended, for the next run.

        A run that reaches collection or teardown answers whether its jobs are still out
        there, so the next run need not ask the scheduler. Best effort: a failed write
        costs one wasted ``squeue`` and a `dispatch.orphans_found` about an ended fleet,
        which the probe retires as stale.
        """
        path = (state or {}).get("run_manifest")
        if path is None:
            return
        reason = set_run_status(path, status)
        if reason is not None:
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.run_manifest_status_failed",
                path=str(path),
                status=status,
                error=reason,
            )

    @staticmethod
    def _state_handles(state):
        """Every job one collect state holds, compile jobs first.

        One expression so no caller forgets the verilate job. ``None`` entries (a suite
        that submitted no build job) are dropped because they crash ``wait_all`` and
        ``cancel_all``.
        """
        return [
            handle
            for handle in [
                (state or {}).get("verilate_handle"),
                (state or {}).get("build_handle"),
                *(handle for _, handle in (state or {}).get("pending") or []),
            ]
            if handle is not None
        ]

    def _wait_or_cancel(self, backend, state):
        """Await one submitted suite's fleet; cancel it if the head dies.

        An interrupt or fatal error must not leave jobs running after the head releases
        its tree lock. ``None`` handles are dropped as in ``_state_handles``. The
        multi-suite regression does not use this: its cancel scope spans submission of
        every suite.
        """
        handles = self._state_handles(state)
        try:
            backend.wait_all(handles)
        except BaseException:
            backend.cancel_all(handles)
            # The record follows the fleet, not the request: `cancelled` only once the
            # jobs are gone, else `running`.
            self._close_cancelled_run_manifest(backend, state)
            raise

    @staticmethod
    def _dispatch_regression_namespaces(suite_configs):
        """Namespace only suites whose resolved command root is shared."""
        roots = [str(Path(cfg.get_path()).resolve().parent) for cfg in suite_configs]
        counts = {}
        for root in roots:
            counts[root] = counts.get(root, 0) + 1
        return {
            str(Path(cfg.get_path()).resolve()): (
                _dispatch_suite_identity(cfg.get_path())
                if counts[str(Path(cfg.get_path()).resolve().parent)] > 1
                else None
            )
            for cfg in suite_configs
        }

    def _validate_dispatch_test_artifacts(self, prepared_suites):
        """Reject cross-suite test artefact collisions before submission."""
        owners = {}
        for prepared in prepared_suites:
            if prepared["dispatch_namespace"] is None:
                continue
            suite_cfg = prepared["suite_cfg"]
            suite_path = str(Path(suite_cfg.get_path()).resolve())
            suite_dir = str(Path(suite_path).parent)
            for entry in prepared["entries"]:
                test_name = entry["cfg"].get_name()
                artifact_dir = test_artifact_dir(
                    suite_dir, test_name, run_tag=self._run_tag
                )
                key = str(artifact_dir)
                previous = owners.get(key)
                if previous is None:
                    owners[key] = (suite_path, test_name)
                    continue
                previous_path, previous_name = previous
                log_event(
                    logger,
                    logging.ERROR,
                    "dispatch.test_artifact_collision",
                    suite_dir=suite_dir,
                    artifact_dir=artifact_dir,
                    first_suite=previous_path,
                    first_test=previous_name,
                    second_suite=suite_path,
                    second_test=test_name,
                )
                raise FatalRtlBuddyError(
                    "cannot dispatch regression: expanded tests "
                    f"{previous_name!r} from {previous_path} and {test_name!r} "
                    f"from {suite_path} share artifact directory {artifact_dir}; "
                    "rename one test or place the suite configs in separate "
                    "directories"
                )

    # How long `--orphans cancel` waits for a cancelled fleet to leave the queue.
    # `scancel` is asynchronous and COMPLETING counts as live.
    ORPHAN_CANCEL_WAIT_S = 30.0
    ORPHAN_CANCEL_POLL_S = 2.0

    def _discover_orphan_runs(
        self, backend, dispatch_root, *, run_token, suite_config=None
    ):
        """Interrupted runs of this suite whose jobs are still on the cluster.

        Scans run manifests, never scheduler job names: two invocations of one suite
        submit the same build-job name, so a name search could adopt or cancel a
        colleague's fleet.

        Each manifest still marked ``running`` is put to the backend; one with nothing
        live is retired as ``stale`` and not probed again. Returns one record per
        manifest with live jobs.

        ``run_token`` is this invocation's nonce and the only thing that excludes a
        manifest from the scan (see :func:`discover_run_manifests`). ``dispatch_root``
        is the suite's whole ``.dispatch/`` tree, and ``suite_config`` keeps the scan to
        this suite's records.
        """
        if not backend.scheduled:
            # local-parallel jobs are this process's children; an interrupted run leaves
            # nothing to discover.
            return []
        orphans = []
        for path, payload in discover_run_manifests(
            dispatch_root, run_token=run_token, suite_config=suite_config
        ):
            try:
                handles = handles_from(payload)
            except (KeyError, TypeError, ValueError) as e:
                # A manifest from another rtl_buddy version whose job specs this one
                # cannot rebuild: skipped, not fatal, so an upgrade stays runnable.
                log_event(
                    logger,
                    logging.DEBUG,
                    "dispatch.orphan_run_unreadable",
                    path=str(path),
                    error=str(e)[:200],
                )
                continue
            live = sorted(backend.live_job_ids(handles)) if handles else []
            if live:
                orphans.append(
                    {
                        "path": path,
                        "payload": payload,
                        "handles": handles,
                        "live": live,
                    }
                )
                continue
            reason = set_run_status(path, STATUS_STALE)
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.orphan_run_stale",
                path=str(path),
                run_token=payload.get("run_token"),
                jobs=len(handles),
                error=reason,
            )
        return orphans

    @staticmethod
    def _warn_about_orphan_runs(orphans, *, suite_dir):
        """Name an interrupted run's surviving jobs, and change nothing.

        The default, and deliberately inert: this run proceeds with a fresh fleet, since
        acting by default would destroy a nearly finished fleet or bind this run's
        verdict to results it did not submit.

        WARNING and console-visible, because the cost of missing it is a doubled cluster
        footprint.
        """
        for orphan in orphans:
            payload = orphan["payload"]
            log_console_event(
                logger,
                logging.WARNING,
                "dispatch.orphans_found",
                suite_dir=suite_dir,
                manifest=str(orphan["path"]),
                run_token=payload.get("run_token"),
                pid=payload.get("pid"),
                job_ids=group_job_ids(orphan["live"]),
                jobs=len(orphan["live"]),
                remedy=(
                    "re-run with --orphans adopt to collect these jobs "
                    "instead of submitting new ones, or --orphans cancel to "
                    "scancel them first"
                ),
            )

    def _cancel_orphan_runs(self, backend, orphans, *, suite_dir):
        """``scancel`` an interrupted run's fleet, then proceed normally.

        Handles are rebuilt from the manifest and cancelled per cluster like any live
        run's teardown. The manifest is marked ``cancelled`` whether or not the
        scheduler agreed; a job that had already ended is cancelled in the sense that
        matters.
        """
        for orphan in orphans:
            payload = orphan["payload"]
            backend.cancel_all(orphan["handles"])
            # `cancel_all` is best effort and ignores `scancel`'s exit status, so
            # confirm: retiring the manifest and submitting a second fleet beside a
            # survivor is what this policy prevents.
            self._confirm_orphan_cancelled(backend, orphan, suite_dir=suite_dir)
            set_run_status(orphan["path"], STATUS_CANCELLED)
            log_console_event(
                logger,
                logging.WARNING,
                "dispatch.orphans_cancelled",
                backend=backend.name,
                suite_dir=suite_dir,
                manifest=str(orphan["path"]),
                run_token=payload.get("run_token"),
                pid=payload.get("pid"),
                job_ids=group_job_ids(orphan["live"]),
                jobs=len(orphan["live"]),
            )

    def _await_fleet_gone(self, backend, handles):
        """Re-probe until these jobs have left the queue; return the ones still live.

        ``[]`` means the cancellation took. ``scancel`` is asynchronous and
        ``COMPLETING`` counts as live, hence a bounded grace period. Silence never means
        "gone": a failed ``squeue`` reports the recorded ids live
        (:meth:`SlurmDispatchBackend.live_job_ids`).
        """
        deadline = time.monotonic() + self.ORPHAN_CANCEL_WAIT_S
        while True:
            # Each probe is bounded by the remaining grace period, so a wedged
            # controller cannot hold the loop open; a timeout reads as "still live".
            remaining = max(0.0, deadline - time.monotonic())
            live = sorted(
                backend.live_job_ids(
                    handles, timeout_s=max(remaining, self.ORPHAN_CANCEL_POLL_S)
                )
            )
            if not live:
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return live
            time.sleep(min(self.ORPHAN_CANCEL_POLL_S, remaining))

    def _report_cancel_failed(self, backend, *, manifest, payload, suite_dir, live):
        """The WARNING every fleet that survives ``scancel`` produces."""
        log_console_event(
            logger,
            logging.WARNING,
            "dispatch.orphans_cancel_failed",
            backend=backend.name,
            suite_dir=suite_dir,
            manifest=str(manifest),
            run_token=payload.get("run_token"),
            pid=payload.get("pid"),
            job_ids=group_job_ids(live),
            jobs=len(live),
            waited_sec=round(self.ORPHAN_CANCEL_WAIT_S, 1),
        )

    def _confirm_orphan_cancelled(self, backend, orphan, *, suite_dir):
        """Fatal unless the orphan's fleet really has left the queue."""
        live = self._await_fleet_gone(backend, orphan["handles"])
        if not live:
            return
        self._report_cancel_failed(
            backend,
            manifest=orphan["path"],
            payload=orphan["payload"],
            suite_dir=suite_dir,
            live=live,
        )
        raise FatalRtlBuddyError(
            f"--orphans cancel could not take down the interrupted run "
            f"recorded in {orphan['path']}: {' '.join(live)} "
            f"{'is' if len(live) == 1 else 'are'} still queued or running "
            f"after {round(self.ORPHAN_CANCEL_WAIT_S)}s (or the scheduler "
            "could not be asked). Nothing was submitted — a second fleet "
            "beside that one would write the same artefact directories. "
            "Cancel them by hand (scancel " + " ".join(live) + ") and "
            "re-run, or use --orphans adopt to collect them instead."
        )

    def _close_cancelled_run_manifest(self, backend, state):
        """Mark this run's own record `cancelled`, but only if it is true.

        ``cancel_all`` is best effort and never reads ``scancel``'s exit status, so the
        record is retired only once the jobs are gone and otherwise left `running` for
        the next invocation to find.

        Never raises: this runs while the head unwinds from the failure that cancelled
        the fleet, and that exception is the one the user needs to see.
        """
        path = (state or {}).get("run_manifest")
        if path is None:
            return
        handles = self._state_handles(state)
        live = self._await_fleet_gone(backend, handles) if handles else []
        if live:
            self._report_cancel_failed(
                backend,
                manifest=path,
                payload={"run_token": state.get("run_token")},
                suite_dir=self._cwd_of_state(state),
                live=live,
            )
            return
        self._close_run_manifest(state, STATUS_CANCELLED)

    def _cwd_of_state(self, state):
        """The suite directory one collect state's jobs were submitted from."""
        for handle in self._state_handles(state):
            return getattr(handle.spec, "suite_dir", None)
        return None

    def _adopt_orphan_run(
        self,
        orphans,
        *,
        backend,
        suite_cfg,
        suite_dir,
        dispatch_cfg,
        entries,
        suite_results,
        master_seed,
    ):
        """Collect an interrupted run's fleet instead of submitting one.

        Returns the collect state a submission would have, rebuilt from the manifest:
        the orphan's handles, its ``run_token`` (which makes its envelopes acceptable to
        this head) and its submission time (which dates retry classification). Nothing
        is submitted and no plan is written.

        Every way this could collect the wrong thing is a hard error, not a fallback to
        submitting, which would run the suite twice over one set of artefact
        directories.
        """
        if not orphans:
            raise FatalRtlBuddyError(
                f"--orphans adopt found no interrupted run of {suite_dir} with "
                "jobs still queued or running. Nothing was submitted. Drop "
                "--orphans (or pass --orphans warn) to run the suite normally."
            )
        if len(orphans) > 1:
            listed = ", ".join(
                f"{orphan['path']} (run_token "
                f"{orphan['payload'].get('run_token')}, "
                f"{len(orphan['live'])} live jobs)"
                for orphan in orphans
            )
            raise FatalRtlBuddyError(
                f"--orphans adopt found {len(orphans)} interrupted runs of "
                f"{suite_dir} with live jobs and cannot choose between them: "
                f"{listed}. Cancel the ones you do not want (scancel their "
                "ids, or re-run with --orphans cancel to take all of them "
                "down) and try again."
            )
        orphan = orphans[0]
        payload = orphan["payload"]
        manifest_path = orphan["path"]
        if payload.get("status") == STATUS_SUBMITTING:
            # The head died mid-fan-out: this record names only the jobs it submitted.
            # They are live and must be dealt with, but a partial fleet cannot be
            # collected into a complete result.
            raise FatalRtlBuddyError(
                f"--orphans adopt cannot adopt {manifest_path} (run_token "
                f"{payload.get('run_token')}, submitted by pid "
                f"{payload.get('pid')}): that head was killed while it was "
                "still submitting, so the record names only part of its "
                f"fleet ({len(payload.get('pending') or [])} job(s) and no "
                "guarantee there were not more). Re-run with --orphans "
                "cancel to take down what it did launch and start over."
            )
        this_config = str(Path(suite_cfg.get_path()).resolve())
        if payload.get("suite_config") != this_config:
            raise self._adopt_mismatch(
                manifest_path,
                payload,
                "it was submitted for test config "
                f"{payload.get('suite_config')!r}, not {this_config!r}",
            )
        if payload.get("backend") != backend.name:
            raise self._adopt_mismatch(
                manifest_path,
                payload,
                f"it was submitted with the {payload.get('backend')!r} backend, "
                f"not {backend.name!r}",
            )
        planned = [(row["test_name"], row["randmode_i"]) for row in suite_results]
        recorded = row_identities(payload)
        if recorded != planned:
            only_recorded = [row for row in recorded if row not in planned]
            only_planned = [row for row in planned if row not in recorded]
            difference = []
            if only_recorded:
                difference.append(
                    "it ran "
                    + ", ".join(self._row_label(row) for row in only_recorded[:10])
                )
            if only_planned:
                difference.append(
                    "this run plans "
                    + ", ".join(self._row_label(row) for row in only_planned[:10])
                )
            if not difference:
                # Same rows in a different order is a mismatch too: the row index binds
                # a job to a result.
                difference.append("its tests are in a different order")
            raise self._adopt_mismatch(manifest_path, payload, "; ".join(difference))
        # ...then the plan itself: two invocations can agree on names and run ids yet
        # differ in plusdefine, `--master-seed`, `resources:`, builder or tests.yaml.
        # The orphan's plan manifest records what its jobs run, so the fresh expansion
        # is compared to it field by field.
        plan_difference = self._adopt_plan_difference(payload, entries, master_seed)
        if plan_difference is not None:
            raise self._adopt_mismatch(manifest_path, payload, plan_difference)
        (
            suite_compile,
            build_compile_resources,
            build_compile_origins,
            build_parallel,
            verilate_resources,
            verilate_origins,
        ) = self._resolve_build_compile(suite_cfg, dispatch_cfg, entries)
        # ...then what the plan does not carry: `--builder-mode`, `--builder`,
        # `--extra-sim-timeout`, the forwarded shared-build root, `--rebuild` and the
        # resolved reservation reach jobs on their specs, not through the plan.
        spec_difference = self._adopt_spec_difference(
            payload,
            sim_resources=self._planned_sim_resources(
                entries, dispatch_cfg=dispatch_cfg, suite_compile=suite_compile
            ),
            build_resources=self._scaled_build_resources(
                build_compile_resources, build_parallel
            ),
            # ...and the verilate job's, likewise: a raised `compile.verilate.mem` since
            # the orphan went out would otherwise adopt a fleet killed under the old
            # figure.
            verilate_resources=self._scaled_build_resources(
                verilate_resources, build_parallel
            ),
        )
        if spec_difference is not None:
            raise self._adopt_mismatch(manifest_path, payload, spec_difference)
        try:
            build_handle = build_from(payload)
            verilate_handle = verilate_from(payload)
            pending = pending_from(payload)
        except (KeyError, TypeError, ValueError) as e:
            raise self._adopt_mismatch(
                manifest_path, payload, f"its job records cannot be read ({e})"
            ) from e

        # Restore the submit-time reservation metadata on the rows just planned;
        # right-sizing reads it per row and only the submitting head knew it.
        # `results` is not in the manifest: the fresh expansion owns skip/setup verdicts
        # and runnable rows' results come from the envelope at collect.
        for row, recorded_row in zip(suite_results, payload.get("rows") or []):
            for key, value in recorded_row.items():
                if key != "results":
                    row[key] = value

        log_console_event(
            logger,
            logging.INFO,
            "dispatch.orphans_adopted",
            backend=backend.name,
            suite_dir=suite_dir,
            manifest=str(manifest_path),
            run_token=payload.get("run_token"),
            pid=payload.get("pid"),
            job_ids=group_job_ids(orphan["live"]),
            jobs=len(orphan["live"]),
            build_job=build_handle.job_id if build_handle is not None else None,
            verilate_job=(
                verilate_handle.job_id if verilate_handle is not None else None
            ),
        )
        return {
            "suite_results": suite_results,
            "pending": pending,
            "build_handle": build_handle,
            "verilate_handle": verilate_handle,
            # The orphan's token, not this invocation's: its jobs stamp envelopes with
            # the token their own head planned.
            "run_token": payload.get("run_token"),
            "submitted_at": payload.get("submitted_at"),
            "suite_compile": suite_compile,
            "build_compile_resources": build_compile_resources,
            "build_compile_origins": build_compile_origins,
            "verilate_resources": verilate_resources,
            "verilate_origins": verilate_origins,
            # Re-read from this invocation's backend rather than recorded: an
            # `sbatch-args` cpu override is the config an edit hint tells the user to
            # change.
            "cpus_override": cpu_request_overrides(backend.effective_sbatch_args),
            "run_manifest": manifest_path,
            # This suite launched nothing: a failure elsewhere must not cancel these
            # jobs before the fleet-wide wait makes them this run's responsibility.
            "adopted": True,
        }

    @staticmethod
    def _resources_dict(resources):
        """One resolved reservation as the manifest records it."""
        if resources is None:
            return None
        return {
            "cpus": resources.cpus,
            "mem": resources.mem,
            "time": resources.time,
        }

    def _adopt_spec_difference(
        self, payload, *, sim_resources, build_resources, verilate_resources=None
    ):
        """Return the first recorded job option that differs from this run's, else
        ``None``.

        The plan describes the tests; these describe the invocation. Every option the
        head puts on a job spec rather than in the plan is compared (e.g.
        ``--builder-mode``, ``--rebuild``).

        The resolved reservation is compared too: a test inheriting
        ``cfg-dispatch.resources`` has no reservation in the plan, so raising ``time``
        after an orphan hit its limit would otherwise adopt the old TIMEOUT.

        ``expect_prebuilt`` and a simulation job's ``rebuild`` are derived from whether
        the suite submitted a build job, so they are checked against the record's own
        build entry.
        """
        shared = {
            "builder_mode": self.rtl_builder_mode,
            "builder_override": self._builder_override,
            "extra_sim_timeout": self._extra_sim_timeout_override,
            # What the head forwards to a job, not what it resolved: an explicit disable
            # travels as `""` and an absent setting as `None`; comparing resolved roots
            # would conflate them.
            "shared_build_root": self.shared_build_root_for_jobs,
        }

        def compare(spec, what, expected):
            for field, want in expected.items():
                got = spec.get(field)
                if isinstance(want, dict) and isinstance(got, dict):
                    for key in sorted(set(want) | set(got)):
                        if got.get(key) != want.get(key):
                            return (
                                f"its {what} was submitted with "
                                f"{field}.{key}={got.get(key)!r}, this run "
                                f"would submit {want.get(key)!r}"
                            )
                    continue
                if got != want:
                    return (
                        f"its {what} was submitted with {field}="
                        f"{got!r}, this run would submit {want!r}"
                    )
            return None

        build = payload.get("build")
        # Read here as well as compared below: a sim job's derived `expect_prebuilt` and
        # `rebuild` follow from whether the suite submitted a build job.
        build_spec = build.get("spec") if isinstance(build, dict) else None
        for key, what, resources in (
            ("verilate", "verilate job", verilate_resources),
            ("build", "build job", build_resources),
        ):
            entry = payload.get(key)
            spec = entry.get("spec") if isinstance(entry, dict) else None
            if spec is None:
                continue
            difference = compare(
                spec,
                what,
                {
                    **shared,
                    "rebuild": self.rebuild,
                    "resources": self._resources_dict(resources),
                },
            )
            if difference is not None:
                return difference
        for entry in payload.get("pending") or []:
            spec = entry.get("spec")
            if not isinstance(spec, dict):
                return "one of its job records has no spec"
            test_name = spec.get("test_name")
            if test_name not in sim_resources:
                return f"it ran a test this run does not plan: {test_name!r}"
            difference = compare(
                spec,
                f"job for {test_name!r}",
                {
                    **shared,
                    # A gated job never carries --rebuild: the build job already rebuilt
                    # and its stamp stops the array from compiling.
                    "rebuild": self.rebuild and build_spec is None,
                    "expect_prebuilt": build_spec is not None,
                    "resources": self._resources_dict(sim_resources[test_name]),
                },
            )
            if difference is not None:
                return difference
        return None

    @staticmethod
    def _scaled_build_resources(resources, parallel):
        """The build job's reservation once ``compile.parallel`` is applied.

        Scaling happens only here; a fresh :class:`JobResources` keeps it from reaching
        the in-job compile and right-sizing callers, where one compile is one serial
        build. Shared with the adoption check, which needs what this invocation would
        have reserved.

        Unbounded above on purpose: the ceiling is the partition's widest node, which
        the head cannot know. An oversized ``parallel`` is caught when sbatch rejects
        the submission; see docs/concepts/dispatch.md and docs/known-issues.md.
        """
        if parallel <= 1:
            return resources
        return JobResources(
            cpus=resources.cpus * parallel,
            # mem/time are not scaled: N concurrent Verilations need about N times the
            # memory but the wall clock of the longest; leave sizing to
            # cfg-dispatch.compile.
            mem=resources.mem,
            time=resources.time,
        )

    def _planned_sim_resources(self, entries, *, dispatch_cfg, suite_compile):
        """Return ``{test name: JobResources}`` this invocation would submit with.

        Repeats the fan-out's resolution because an adoption never reaches it. Names are
        unique after sweep expansion. The builder mode is included, since a reservation
        can depend on it (an orphan planned under ``-M cov`` must not match a ``-M reg``
        fleet).
        """
        resolved = {}
        for entry in entries:
            cfg = entry["cfg"]
            resources = resolve_resources(
                dispatch_cfg, cfg, builder_mode=self.rtl_builder_mode
            )
            if entry["compile_in_job"]:
                entry_tb_compile = getattr(cfg.get_testbench(), "compile", None)
                resources, _governed_by = combine_for_in_job_compile(
                    resources,
                    resolve_compile_resources(
                        dispatch_cfg,
                        suite_compile,
                        entry_tb_compile,
                        builder_mode=self.rtl_builder_mode,
                    ),
                )
            resolved[cfg.get_name()] = resources
        return resolved

    @staticmethod
    def _adopt_plan_difference(payload, entries, master_seed):
        """Return the first way the orphan's plan differs from this one, else ``None``.

        Compares the orphan's ``plan-<pid>.json`` (the JSON-safe
        ``TestConfig.to_plan_dict()`` its jobs execute) with this invocation's fresh
        expansion, element-wise in plan order, plus the master seed. The dict covers
        plusargs, plusdefines, testbench, hook paths, per-test ``resources:``, builder,
        xfail and the resolved seed, so an unreadable or older-schema plan is a refusal.

        A run with freshly drawn seeds (``rb randtest``) plans different
        ``resolved_seed`` values every time and can never be adopted.
        """

        def short(value):
            text = repr(value)
            return text if len(text) <= 120 else text[:117] + "..."

        plan_path = payload.get("plan")
        try:
            recorded = json.loads(Path(plan_path).read_text())
        except (OSError, TypeError, ValueError) as e:
            return f"its plan {plan_path} cannot be read ({str(e)[:200]})"
        if not isinstance(recorded, dict):
            return f"its plan {plan_path} is not a JSON object"
        if recorded.get("schema_version") != PLAN_SCHEMA_VERSION:
            return (
                f"its plan has schema_version "
                f"{recorded.get('schema_version')!r}, not {PLAN_SCHEMA_VERSION} "
                "(it was written by a different rtl_buddy)"
            )
        if recorded.get("master_seed") != master_seed:
            return (
                f"it was planned with master seed "
                f"{recorded.get('master_seed')!r}, this run with "
                f"{master_seed!r}"
            )
        was_tests = recorded.get("tests")
        if not isinstance(was_tests, list):
            return f"its plan {plan_path} has no `tests` list"
        now_tests = [entry["cfg"].to_plan_dict() for entry in entries]
        if len(was_tests) != len(now_tests):
            return (
                f"it planned {len(was_tests)} test(s), this run plans {len(now_tests)}"
            )
        for index, (was, now) in enumerate(zip(was_tests, now_tests)):
            if not isinstance(was, dict):
                return f"its plan entry {index} is not a JSON object"
            for key in sorted(set(was) | set(now)):
                if was.get(key) == now.get(key):
                    continue
                name = now.get("name") or was.get("name")
                return (
                    f"test {name!r} (plan index {index}) differs in {key!r}: "
                    f"it ran {short(was.get(key))}, this run plans "
                    f"{short(now.get(key))}"
                )
        return None

    @staticmethod
    def _row_label(row):
        """``test`` or ``test:run_id`` for one ``(name, run id)`` row."""
        name, run_id = row
        return name if run_id is None else f"{name}:{run_id}"

    @staticmethod
    def _adopt_mismatch(manifest_path, payload, difference):
        """The one error shape every failed adoption takes."""
        return FatalRtlBuddyError(
            f"--orphans adopt cannot adopt {manifest_path} (run_token "
            f"{payload.get('run_token')}, submitted by pid "
            f"{payload.get('pid')}): {difference}. Adopting it would score "
            "this run against a fleet that is not the one it planned. Re-run "
            "with --orphans cancel to take those jobs down and start over, or "
            "with the same arguments the interrupted run used."
        )

    def _resolve_build_compile(self, suite_cfg, dispatch_cfg, entries):
        """Resolve the compile's reservations as this invocation would submit them.

        Returns ``(suite compile block, build reservation, its origins, parallel,
        verilate reservation, its origins)``.

        The suite's own ``compile:`` block is read once and threaded to the build job,
        the in-job compile combination and the post-run advice; an adopted run rebuilds
        its state through this helper. The reservation is aggregated over the builds
        this plan produces; see the comments below.
        """
        suite_compile = suite_cfg.get_compile()
        # The suite's own `compile:` block: the most specific layer of the compile
        # reservation, read once for the build job, the in-job compile combination and
        # the post-run advice.
        # The builds this plan produces, each with the `compile:` block that sizes it;
        # the build job's reservation aggregates over them, and an unselected testbench
        # adds none.
        # One entry per distinct compile, not per testbench: tests sharing a testbench
        # but differing in plusdefines, builder or model each hold their own peak. The
        # head cannot see the real key without writing filelists, so it keys on
        # (testbench, plusdefines, builder, model, assertions); configs that resolve to
        # one group_dir are counted twice (over-reserving).
        # Keyed per test instead, to avoid under-counting: a `preproc:` hook (may set
        # plusdefines after this snapshot) and a builder that cannot share (per-test
        # output path). The test name goes in the key, not the run id: a run_id fan-out
        # shares `artefacts/<test>/`.
        # `parallel` is resolved here because memory adds across builds in flight and
        # wall clock is their schedule; the same value goes to the build job.
        planned_builds = []
        seen_builds = set()
        for entry in entries:
            cfg = entry["cfg"]
            tb = cfg.get_testbench()
            tb_name = getattr(tb, "name", None)
            tb_compile = getattr(tb, "compile", None)
            model = cfg.get_model()
            key = (
                tb_name,
                getattr(tb_compile, "cpus", None),
                getattr(tb_compile, "mem", None),
                getattr(tb_compile, "time", None),
                # repr, not the value: the key only has to separate configs, and a
                # plusdefine is whatever YAML or a sweep hook put there.
                tuple(
                    sorted(
                        (str(k), repr(v))
                        for k, v in (cfg.get_plusdefines() or {}).items()
                    )
                ),
                cfg.get_builder_name(),
                getattr(model, "name", None),
                getattr(model, "path", None),
                # `assertions: true` adds Verilator's SVA flags to `key_cmd` in
                # `_build_compile_plan`, so tests that disagree on it are two builds.
                getattr(cfg, "assertions", False),
                # Per-test output dir means per-test build; also per test with a
                # `preproc:` hook.
                cfg.get_name()
                if entry["compile_in_job"] or cfg.get_preproc_path()
                else None,
            )
            if key in seen_builds:
                continue
            seen_builds.add(key)
            planned_builds.append((tb_name, tb_compile))
        build_parallel = max(
            1, min(compile_parallel(dispatch_cfg, suite_compile), len(entries))
        )
        build_compile_resources, build_compile_origins = aggregate_compile_resources(
            dispatch_cfg,
            suite_compile,
            planned_builds,
            parallel=build_parallel,
            # The mode this run compiles in: an instrumented build peaks higher than the
            # same sources under `-M reg`, per each layer's `modes:` block.
            builder_mode=self.rtl_builder_mode,
        )
        # ...and the same aggregation over the verilate keys. Resolved even when the
        # compile is not split: an adoption compares what this invocation would have
        # reserved.
        verilate_resources, verilate_origins = aggregate_verilate_resources(
            dispatch_cfg,
            suite_compile,
            planned_builds,
            parallel=build_parallel,
            builder_mode=self.rtl_builder_mode,
        )

        return (
            suite_compile,
            build_compile_resources,
            build_compile_origins,
            build_parallel,
            verilate_resources,
            verilate_origins,
        )

    def _suite_splits_verilate(self, backend, dispatch_cfg, suite_compile, entries):
        """Return whether this suite's compile goes out as two chained jobs.

        All must hold:
        - The backend can chain jobs (Slurm, via ``--dependency=afterok``).
        - ``compile.split-verilate`` resolves true (the default).
        - Every planned build compiles with the plain ``verilator --binary`` line;
          cocotb and SystemC use their own ``--exe --build``, so one such entry makes
          the whole suite unsplit.
        - The builder is resolvable; an unresolvable one is a per-test failure the build
          job reports and must not decide how the suite is submitted.
        """
        if backend.name != "slurm":
            return False
        if not compile_split_verilate(dispatch_cfg, suite_compile):
            return False
        for entry in entries:
            cfg = entry["cfg"]
            tb = cfg.get_testbench()
            if tb.is_cocotb() or tb.is_systemc():
                return False
            try:
                builder_cfg = self.root_cfg.resolve_rtl_builder_cfg(
                    cfg.get_builder_name()
                )
            except FatalRtlBuddyError:
                return False
            if builder_cfg.get_simulator_family() != "verilator":
                return False
        return bool(entries)

    def _dispatch_suite_submit(
        self,
        suite_cfg,
        backend,
        *,
        run_token,
        prepared=None,
        dispatch_namespace=None,
        test_name=None,
        reg_level=None,
        start_level=None,
        run_ids=None,
        seed_mode: SeedMode = SeedMode.DEFAULT,
        replay_run_id=None,
        master_seed: int | None = None,
    ):
        """Plan, build job and array fan-out for one suite; no waiting.

        Nothing heavy runs on the submit host. Phases:
        1. Plan: expand the sweep hooks once on the head and write the configs to a plan
          manifest.
        2. Build job: compile the shared executable on a compute node (``rb _build-job
          --plan``); skipped when no planned test's builder can share a build.
        3. Fan-out: group sim jobs by resolved resources into ``sbatch`` arrays (``rb
          _test-job --plan``), each gated on the build via ``--dependency=afterok``. A
          group whose builder compiles in the job is ungated and reserves for both
          phases.

        Jobs read the plan; none re-runs the sweep hook. Returns collect state for
        :meth:`_dispatch_collect` including the build handle; the caller owns the
        cross-suite wait. A mid-fan-out failure cancels the partial submissions here;
        the caller cancels the whole fleet on a later failure or interrupt.
        """
        if run_ids is None:
            run_ids = [None]
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        suite_config_path = str(Path(suite_cfg.get_path()).resolve())
        dispatch_cfg = self.root_cfg.get_dispatch_cfg()
        # The suite's whole `.dispatch/` tree and the directory this invocation writes
        # to differ when a regression namespaces co-located suite configs: discovery
        # searches the tree, writes go to this invocation's directory.
        dispatch_base = run_artifact_root(suite_dir, self._run_tag) / ".dispatch"
        dispatch_root = dispatch_base
        if dispatch_namespace is not None:
            dispatch_root /= dispatch_namespace
        # When this head started work on the suite, for the run manifest: distinguishes
        # a fleet submitted minutes ago from one from yesterday.
        started_at = time.time()
        # (0) An earlier run of this suite whose head died with its fleet still on the
        # cluster. Probed before planning, so `--orphans cancel` takes the old fleet
        # down first and `--orphans adopt` can decline to submit.
        orphans = self._discover_orphan_runs(
            backend,
            dispatch_base,
            run_token=run_token,
            suite_config=suite_config_path,
        )
        if orphans and self._orphans_policy == "cancel":
            self._cancel_orphan_runs(backend, orphans, suite_dir=suite_dir)
            orphans = []
        elif orphans and self._orphans_policy == "warn":
            self._warn_about_orphan_runs(orphans, suite_dir=suite_dir)
        if prepared is None:
            suite_results = []
            # (1) Plan: one sweep expansion for the whole suite, on the head.
            entries = self._plan_dispatch_suite(
                suite_cfg,
                test_name=test_name,
                reg_level=reg_level,
                start_level=start_level,
                run_ids=run_ids,
                suite_results=suite_results,
                seed_mode=seed_mode,
                master_seed=master_seed,
            )
        else:
            entries = prepared["entries"]
            suite_results = prepared["suite_results"]
        if self._orphans_policy == "adopt":
            # Collect the orphan instead of launching. Checked before the zero-test
            # return so adopting with an empty plan is a diagnosed mismatch, not a
            # silent no-op.
            return self._adopt_orphan_run(
                orphans,
                backend=backend,
                suite_cfg=suite_cfg,
                suite_dir=suite_dir,
                dispatch_cfg=dispatch_cfg,
                entries=entries,
                suite_results=suite_results,
                master_seed=master_seed,
            )
        if not entries:
            # Every test filtered out by -l/-s: skip the build job, which would iterate
            # nothing while wait_all blocks on it.
            return {
                "suite_results": suite_results,
                "pending": [],
                "build_handle": None,
                "verilate_handle": None,
            }

        (
            suite_compile,
            build_compile_resources,
            build_compile_origins,
            build_parallel,
            verilate_resources,
            verilate_origins,
        ) = self._resolve_build_compile(suite_cfg, dispatch_cfg, entries)

        # ``run_token`` is the head's per-invocation nonce, shared across suites and
        # threaded to every sim job through the plan; jobs stamp it into their envelopes
        # so collection tells this run's result from a stale one.
        plan_path = write_plan(
            run_scoped_path(dispatch_root, "plan", run_token),
            str(suite_cfg.get_path()),
            [e["cfg"] for e in entries],
            run_token,
            master_seed=master_seed,
        )

        # The run record is opened here, before anything is submitted, and grown as each
        # handle is accepted: a head killed mid fan-out would otherwise leave nothing on
        # disk to find the jobs by.
        run_manifest = self._open_run_manifest(
            backend,
            dispatch_root,
            suite_cfg=suite_cfg,
            suite_dir=suite_dir,
            run_token=run_token,
            started_at=started_at,
            plan_path=plan_path,
            rows=suite_results,
        )

        # (2) Build job, unless nothing in this suite could use its output.
        # `sbatch-args` follows the generated flags and wins, and `SBATCH_*` reaches
        # sbatch through the environment, so the resolved reservation may not be what
        # jobs are submitted with. Right-sizing must not take it for the request;
        # recording nothing falls back to the scheduler's `ReqCPUS`. Not sanitized: a
        # site that exports these means them.
        # Read once here and carried in the returned state: a later suite's sweep hook
        # (exec()d in this process) can change `SBATCH_*` before analysis.
        # Taken from the backend's arguments, not this suite's `cfg-dispatch`: the
        # backend is built once from the orchestration config while `root_cfg` is
        # rebuilt per root.
        cpus_request_args = cpu_request_overrides(backend.effective_sbatch_args)
        if cpus_request_args:
            # DEBUG, once per suite submit: the override is deliberate, so only say why
            # advice comes from sacct rather than the YAML.
            log_event(
                logger,
                logging.DEBUG,
                "rightsize.request_from_scheduler",
                suite_dir=suite_dir,
                overrides=cpus_request_args,
            )
        # A builder without shared-build support produces no reusable stamp; a build job
        # would only add queue latency (a mixed-builder suite still gets one).
        # Exception, for correctness: a test that compiles in its own job writes
        # `artefacts/<test>/`, keyed on the test, not the run, so a run_id fan-out would
        # overwrite itself. The build job is the single writer; elements short-circuit
        # on its stamp.
        fans_out_in_job = any(
            entry["compile_in_job"] and len(entry["rows"]) > 1 for entry in entries
        )
        if any(not entry["compile_in_job"] for entry in entries) or fans_out_in_job:
            # Two chained jobs when the compile can be split: verilation is
            # single-threaded at peak memory and the C++ build uses `compile.cpus` cores
            # at a fraction of it. Submitted first, because the build job takes an
            # `afterok` on its id.
            splits = self._suite_splits_verilate(
                backend, dispatch_cfg, suite_compile, entries
            )
            verilate_handle = (
                self._submit_dispatch_build(
                    suite_cfg,
                    backend,
                    suite_dir=suite_dir,
                    dispatch_cfg=dispatch_cfg,
                    reg_level=reg_level,
                    start_level=start_level,
                    dispatch_root=dispatch_root,
                    plan_path=plan_path,
                    run_token=run_token,
                    planned=len(entries),
                    suite_compile=suite_compile,
                    compile_resources=verilate_resources,
                    parallel=build_parallel,
                    phase=BUILD_PHASE_VERILATE,
                )
                if splits
                else None
            )
            if verilate_handle is not None:
                self._grow_run_manifest(
                    run_manifest,
                    record_verilate_handle,
                    verilate_handle,
                    suite_dir=suite_dir,
                )
            try:
                build_handle = self._submit_dispatch_build(
                    suite_cfg,
                    backend,
                    suite_dir=suite_dir,
                    dispatch_cfg=dispatch_cfg,
                    reg_level=reg_level,
                    start_level=start_level,
                    dispatch_root=dispatch_root,
                    plan_path=plan_path,
                    run_token=run_token,
                    planned=len(entries),
                    suite_compile=suite_compile,
                    compile_resources=build_compile_resources,
                    parallel=build_parallel,
                    phase=BUILD_PHASE_BUILD if splits else BUILD_PHASE_FULL,
                    dependency=(
                        verilate_handle.job_id if verilate_handle is not None else None
                    ),
                )
            except BaseException:
                # A verilate job with no build job behind it would verilate and never be
                # collected.
                backend.cancel_all([verilate_handle])
                raise
            self._grow_run_manifest(
                run_manifest, record_build_handle, build_handle, suite_dir=suite_dir
            )
        else:
            build_handle = None
            verilate_handle = None
            log_event(
                logger,
                logging.INFO,
                "dispatch.build_job_skipped",
                suite_dir=suite_dir,
                reason=(
                    "no planned test can share a build, and none is fanned out "
                    "over several runs; each sim job compiles into its own "
                    "per-test directory"
                ),
                tests=len(entries),
            )

        # (3) Group by resolved resources: elements of one sbatch array must share a
        # reservation shape. Consumes the single expansion; no hook.
        groups = {}  # (cpus, mem, time) -> list[(row index, plan index, spec)]
        # `plan_index` is the config's position in the plan manifest: the build job's
        # index for it, and the name both processes share for "this compile key's
        # tests".
        for plan_index, entry in enumerate(entries):
            cfg = entry["cfg"]
            # Resolved for the mode this fleet runs in, the same value the jobs carry as
            # `builder_mode`.
            resources = resolve_resources(
                dispatch_cfg, cfg, builder_mode=self.rtl_builder_mode
            )
            # ...and which fields a `modes:` block supplied, for the end-of-run
            # reservation advice.
            mode_governed = mode_governed_fields(
                dispatch_cfg, cfg, builder_mode=self.rtl_builder_mode
            )
            if entry["compile_in_job"]:
                # This test's own compile reservation, resolved per entry: the aggregate
                # the build job takes would give every sim job the sum of every
                # testbench's memory.
                entry_tb = cfg.get_testbench()
                entry_tb_compile = getattr(entry_tb, "compile", None)
                compile_resources = resolve_compile_resources(
                    dispatch_cfg,
                    suite_compile,
                    entry_tb_compile,
                    builder_mode=self.rtl_builder_mode,
                )
                # One allocation covers compile and sim, so it is sized for the larger
                # per field; record which layer won so advice names the governing field.
                resources, governed_by = combine_for_in_job_compile(
                    resources, compile_resources
                )
                # ...and which tests.yaml layer supplied each compile field, per row, so
                # a testbench-block field is hinted at that entry.
                entry_origins = compile_resource_origins(
                    suite_compile,
                    entry_tb_compile,
                    builder_mode=self.rtl_builder_mode,
                )
                for idx, _ in entry["rows"]:
                    suite_results[idx]["governed_by"] = governed_by
                    suite_results[idx]["compile_origins"] = entry_origins
                    suite_results[idx]["compile_testbench"] = getattr(
                        entry_tb, "name", None
                    )
                    # The floor no `reduce` advice can take this allocation below.
                    suite_results[idx]["compile_floor"] = {
                        "cpus": compile_resources.cpus,
                        "mem": compile_resources.mem,
                        "time": compile_resources.time,
                    }
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.compile_in_job",
                    test=cfg.get_name(),
                    cpus=resources.cpus,
                    mem=resources.mem,
                    time=resources.time,
                    governed_by=governed_by,
                )
            for idx, _ in entry["rows"]:
                # The cpus submitted with, for right-sizing: `--cpus-per-task` verbatim,
                # i.e. the request. Judging efficiency against a site's rounded-up
                # allocation would advise a value tests.yaml already holds. Recorded
                # after the in-job compile max.
                self._record_cpu_request_metadata(
                    suite_results[idx],
                    per_task_cpus=resources.cpus,
                    overrides=cpus_request_args,
                )
                # ...and which fields this run's builder mode governed, so advice names
                # `resources.modes.<mode>.<field>`. Recorded only where a mode block is
                # in play.
                if mode_governed:
                    suite_results[idx]["resource_modes"] = mode_governed
            dispatch_dir = (
                Path(
                    test_artifact_dir(suite_dir, cfg.get_name(), run_tag=self._run_tag)
                )
                / "dispatch"
            )
            # Create the log dir on the head before submit: slurmstepd opens `--output`
            # before `rb _test-job` runs.
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            for idx, run_id in entry["rows"]:
                # This job's envelope tag, not the artefact tree's `--run-tag`.
                job_tag = "single" if run_id is None else f"{run_id:04d}"
                result_json = dispatch_dir / f"result-{job_tag}.json"
                # Do not pre-unlink a stale envelope: on NFS the head's negative lookup
                # caches a dentry that hides the job's later write for ~acdirmin.
                # Staleness is rejected by run_token at collection.
                spec = TestJobSpec(
                    test_name=cfg.get_name(),
                    suite_dir=suite_dir,
                    test_config_path=str(suite_cfg.get_path()),
                    result_json=result_json,
                    resources=resources,
                    run_id=run_id,
                    seed_mode=seed_mode,
                    replay_run_id=replay_run_id,
                    master_seed=master_seed,
                    resolved_seed=cfg.get_resolved_seed(),
                    builder_mode=self.rtl_builder_mode,
                    builder_override=self._builder_override,
                    extra_sim_timeout=self._extra_sim_timeout_override,
                    share_build=True,
                    # Gated jobs are told so: reaching their own compile means the build
                    # job's stamp did not validate, which would put every sibling
                    # element into one build directory at once.
                    expect_prebuilt=build_handle is not None,
                    # `--rebuild` reaches a sim job only when the suite submitted no
                    # build job. With one, its fresh stamp stops the array compiling;
                    # passing `--rebuild` would defeat that.
                    rebuild=self.rebuild and build_handle is None,
                    # Resolved once by the head and handed to the build job and every
                    # sim job, which must agree. An explicit disable travels as `""`,
                    # since `None` would let the job re-enable the cache from its
                    # environment.
                    shared_build_root=self.shared_build_root_for_jobs,
                    # ...and where the build job records its verdict, splitting "stamp
                    # did not validate" into "compile failed" (report, do not recompile)
                    # and "stale stamp" (recompile into compile.retry.log).
                    build_result_json=(
                        build_handle.spec.result_json
                        if build_handle is not None
                        else None
                    ),
                    # Named after the backend that writes it: `slurm-*` from sbatch
                    # --output, `local-parallel-*` from the pool's redirected stdout.
                    log_path=dispatch_dir / f"{backend.name}-{job_tag}.log",
                    plan_path=plan_path,
                    # Already merged into the plan; carried so the job records them as
                    # this run's overrides and a plan miss still applies them.
                    plusarg_overrides=dict(self._plusarg_overrides),
                    # Artefact tree this fleet belongs to; the job recomputes its paths
                    # from it.
                    run_tag=self._run_tag,
                )
                # Resources alone: every group takes the same dependency, so a
                # self-compiling test with the same reservation can share the array.
                groups.setdefault(
                    (resources.cpus, resources.mem, resources.time), []
                ).append((idx, plan_index, spec))

        pending = []
        # When this attempt went out. Retry classification accepts only artefacts at
        # least this recent: `artefacts/<test>/test.log` is keyed on the test, and a job
        # that never started has not removed the previous run's, so an old banner would
        # satisfy the rule.
        # Taken before the first submit.
        submitted_at = time.time()
        # (plan index, test name, job id) per submitted row, for the gates manifest.
        # Only the first round belongs: `_resubmit_retryable` runs after the build job
        # exited, and retries are submitted ungated.
        gate_entries = []
        # Read once, and only where a build job exists: a suite whose tests each compile
        # in their own job has none.
        build_cluster = None if build_handle is None else build_handle.cluster
        try:
            # Per-invocation array dir (head pid), so a resubmit or overlapping run
            # never rewrites a manifest under another run's queued elements, which `sed`
            # it at exec time. Sibling of .shared-builds.
            for array_seq, group_entries in enumerate(groups.values(), start=1):
                specs = [spec for _, _, spec in group_entries]
                array_dir = run_scoped_path(
                    dispatch_root, "array", run_token, suffix=f"-{array_seq:03d}"
                )
                handles = backend.submit_array(
                    specs,
                    array_dir=array_dir,
                    max_parallel=dispatch_cfg.max_jobs_per_array,
                    # Every group waits for the build job, including self-compiling
                    # ones: it runs PRE+COMPILE for the whole plan and writes their
                    # `artefacts/<test>/` too, so an ungated element would race it. A
                    # suite with no build job runs unblocked.
                    dependency=(
                        build_handle.job_id if build_handle is not None else None
                    ),
                )
                for (idx, plan_index, spec), handle in zip(group_entries, handles):
                    pending.append((idx, handle))
                    gate_entries.append(
                        (
                            plan_index,
                            spec.test_name,
                            handle.job_id,
                            # Where this job was accepted. `--clusters=a,b` can span
                            # clusters and an id means something only on the cluster
                            # that issued it. A backend recording no cluster gets the
                            # build job's.
                            handle.cluster
                            if handle.cluster is not None
                            else build_cluster,
                        )
                    )
                # This array is accepted, so it is running whether or not the head
                # survives to submit the next: record it now.
                self._grow_run_manifest(
                    run_manifest,
                    record_pending_handles,
                    [
                        (idx, handle)
                        for (idx, _pi, _s), handle in zip(group_entries, handles)
                    ],
                    suite_dir=suite_dir,
                )
        except BaseException:
            # A mid-fan-out submit failure must not leak this suite's build job or
            # already-submitted arrays.
            backend.cancel_all(
                [verilate_handle, build_handle] + [handle for _, handle in pending]
            )
            raise
        # The whole suite is out, so every id the build job could release is known: hand
        # it the map.
        # Written outside the try above: a manifest naming half an array would release
        # half a compile key, and a write failure must not cancel a fleet already gated
        # on `afterok`.
        gates_json = getattr(getattr(build_handle, "spec", None), "gates_json", None)
        if gates_json is not None:
            try:
                write_gates(gates_json, run_token=run_token, entries=gate_entries)
            except OSError as e:
                log_event(
                    logger,
                    logging.WARNING,
                    "dispatch.gates_write_failed",
                    suite_dir=suite_dir,
                    path=str(gates_json),
                    error=str(e),
                )
        # The fan-out is complete, so the record stops saying `submitting`; only from
        # here may it be adopted.
        self._grow_run_manifest(
            run_manifest, finish_submission, submitted_at, suite_dir=suite_dir
        )
        return {
            "suite_results": suite_results,
            "pending": pending,
            "build_handle": build_handle,
            # The verilate half of a split compile, or None. A separate key, not a list:
            # only the build job's envelope decides a compile verdict and gates the
            # fan-out.
            "verilate_handle": verilate_handle,
            "run_token": run_token,
            "submitted_at": submitted_at,
            # Where this suite's fleet is recorded, so collection can retire it
            # (`collected`) and teardown can mark it `cancelled`.
            "run_manifest": run_manifest,
            # For _analyze_reservations, which re-resolves the compile reservation from
            # the root config alone and has no suite_cfg.
            "suite_compile": suite_compile,
            # The build job's reservation and per-field provenance as submit resolved
            # them (maximum over planned testbenches); analysis has neither the plan nor
            # the suite_cfg.
            "build_compile_resources": build_compile_resources,
            "build_compile_origins": build_compile_origins,
            # ...and the verilate job's pair.
            "verilate_resources": verilate_resources,
            "verilate_origins": verilate_origins,
            # What superseded the resolved cpus at submit time. Snapshotted because a
            # later suite's in-process sweep hook can change the environment half.
            "cpus_override": cpus_request_args,
        }

    @staticmethod
    def _record_cpu_request_metadata(row, *, per_task_cpus, overrides):
        """What right-sizing needs to know about one submission's cpus.

        Written at submit and rewritten on every resubmission, since the analysis reads
        the retry's telemetry.
        - ``submitted_cpus_per_task`` is the generated ``--cpus-per-task`` verbatim,
          always recorded; it is what the compile floor bounds.
        - ``requested_cpus`` is the whole-job request, ``None`` under any override (the
          denominator then comes from the scheduler's ``ReqCPUS``).
        - ``cpus_override`` is what did the overriding, for the edit hint.
        """
        row["submitted_cpus_per_task"] = per_task_cpus
        row["requested_cpus"] = None if overrides else per_task_cpus
        row["cpus_override"] = overrides

    def _announce_dispatched_suite(self, state, *, backend, suite):
        """Put a suite's job ids on the console before the wait begins.

        If the head then dies, these ids are the only route to `squeue`/`sacct`; the
        `dispatch.*_submitted` events are INFO and not shown at default verbosity.
        """
        handles = [handle for _, handle in state["pending"]]
        if not handles:
            # Nothing queued (every test filtered out): no ids to report and no wait to
            # explain.
            return
        build_handle = state["build_handle"]
        verilate_handle = state.get("verilate_handle")
        log_console_event(
            logger,
            logging.INFO,
            "dispatch.suite_submitted",
            backend=backend.name,
            suite=suite,
            build_job=build_handle.job_id if build_handle is not None else None,
            # The verilate half of a split compile; absent where the compile went out as
            # one job.
            verilate_job=(
                verilate_handle.job_id if verilate_handle is not None else None
            ),
            job_ids=group_job_ids(handle.job_id for handle in handles),
            # Compile jobs included: the same scale as `dispatch.progress` and
            # `dispatch.suite_drained`, so per-suite counts sum to the fleet's `total`.
            jobs=len(handles)
            + (1 if build_handle is not None else 0)
            + (1 if verilate_handle is not None else 0),
        )

    def _plan_dispatch_suite(
        self,
        suite_cfg,
        *,
        test_name=None,
        reg_level,
        start_level,
        run_ids,
        suite_results,
        seed_mode: SeedMode = SeedMode.DEFAULT,
        master_seed: int | None = None,
    ):
        """Expand the suite once for dispatch; return the runnable entries.

        Appends the same skip/setup rows as the in-process path, and a placeholder row
        per (runnable test, run_id), to ``suite_results`` in test order. Returns one
        ``{cfg, rows}`` entry per runnable config, where ``rows`` are the placeholder
        indices it owns, so build and sim jobs read the plan instead of re-running the
        sweep hook. ``test_name`` narrows the expansion to one base test (randtest
        dispatch).
        """
        if master_seed is not None and len(run_ids) != 1:
            raise FatalRtlBuddyError(
                "--master-seed requires one run id per expanded test"
            )
        seed_run_id = run_ids[0] if len(run_ids) == 1 else None
        entries = []
        for cfg in self._iter_suite_runnables(
            suite_cfg,
            test_name=test_name,
            reg_level=reg_level,
            start_level=start_level,
            run_ids=run_ids,
            suite_results=suite_results,
        ):
            self._resolve_test_seed(
                cfg,
                master_seed=master_seed,
                suite_config_path=suite_cfg.get_path(),
                run_id=seed_run_id,
                seed_mode=seed_mode,
            )
            builder_cfg = self.root_cfg.resolve_rtl_builder_cfg(cfg.get_builder_name())
            exp_builder = builder_cfg.get_name()
            # A builder that cannot share a build recompiles inside every sim job, so
            # that job's reservation must cover the compile.
            # Decided once from the predicate the job itself consults: family and an
            # absolute `builder-simv:`, not family alone, or a pinned VCS builder would
            # be planned as shareable.
            compile_in_job = share_build_unsupported_reason(builder_cfg) is not None
            rows = []
            for run_id in run_ids:
                suite_results.append(
                    {
                        "test_name": cfg.get_name(),
                        "randmode_i": run_id,
                        "results": None,
                        "builder": exp_builder,
                        "compile_in_job": compile_in_job,
                    }
                )
                rows.append((len(suite_results) - 1, run_id))
            entries.append({"cfg": cfg, "rows": rows, "compile_in_job": compile_in_job})
        return entries

    def _submit_dispatch_build(
        self,
        suite_cfg,
        backend,
        *,
        suite_dir,
        dispatch_cfg,
        reg_level,
        start_level,
        dispatch_root,
        plan_path,
        run_token,
        planned,
        suite_compile=None,
        compile_resources=None,
        parallel=None,
        phase=BUILD_PHASE_FULL,
        dependency=None,
    ):
        """Submit the suite's compile as a Slurm build job on a compute node.
        - ``phase`` is which half of the compile this job runs, and ``dependency`` the
          job id it must wait for (the verilate job's, for the ``build`` half). Files
          are named after the phase, so a split suite's two jobs never share an envelope
          or log.
        - ``suite_compile`` is the suite's own ``compile:`` block, layered over
          ``cfg-dispatch.compile`` field by field (``parallel`` included). It is passed
          in so this job and the caller's in-job-compile combination use one resolution.
        - ``compile_resources`` is the reservation this job is sized from: the planned
          testbenches' ``compile:`` blocks aggregated and floored at the suite-level
          whole-job value. ``parallel`` is the concurrency it was computed against; the
          two must match, since memory adds across builds in flight and wall clock
          divides by them. Both ``None`` resolve here, giving the same answer for suites
          whose testbenches declare no block.
        - ``planned`` is the number of configs in the plan. It caps ``compile.parallel``
          so cpus are not reserved for idle slots. It counts configs, not distinct
          compile keys, so the cap is an upper bound on concurrency. The pre-cap value
          travels as ``parallel_configured``, the one a reader can find in a config
          file.
        """
        dispatch_root = Path(dispatch_root)
        dispatch_root.mkdir(parents=True, exist_ok=True)
        # Prefix of every file this job owns. The build half of a split compile keeps
        # the build job's names.
        tag = "verilate" if phase == BUILD_PHASE_VERILATE else "build"
        configured_parallel = compile_parallel(dispatch_cfg, suite_compile)
        # Asked only for the job that could release anything: the verilate half gets no
        # gates manifest, and probing twice would log `dispatch.gates_skipped` twice.
        configured_dependency = (
            self._release_blocking_dependency(backend, suite_dir=suite_dir)
            if phase != BUILD_PHASE_VERILATE
            else None
        )
        if parallel is None:
            parallel = max(1, min(configured_parallel, planned))
        # `parallel` layers like the reservation fields beside it: this job belongs to
        # one suite, so a suite with one compile key reserves `cpus`, not a cluster-wide
        # multiple.
        # Sizing against the partition's widest node stays the writer's obligation; see
        # the scaling note below.
        resources = (
            compile_resources
            if compile_resources is not None
            else resolve_compile_resources(
                dispatch_cfg, suite_compile, builder_mode=self.rtl_builder_mode
            )
        )
        resources = self._scaled_build_resources(resources, parallel)
        spec = BuildJobSpec(
            suite_dir=suite_dir,
            test_config_path=str(suite_cfg.get_path()),
            resources=resources,
            parallel=parallel,
            # ...and what the config asked for before the cap, so the console line names
            # the value found in the file.
            parallel_configured=configured_parallel,
            # Always, when requested: the build job is the single writer of the shared
            # directory, so a forced recompile costs one compile, not one per element.
            rebuild=self.rebuild,
            shared_build_root=self.shared_build_root_for_jobs,
            reg_level=reg_level,
            start_level=start_level,
            builder_mode=self.rtl_builder_mode,
            builder_override=self._builder_override,
            extra_sim_timeout=self._extra_sim_timeout_override,
            log_path=run_scoped_path(dispatch_root, tag, run_token, suffix=".log"),
            plan_path=plan_path,
            phase=phase,
            # Where the build job records which configs compiled; the head reads it at
            # collect.
            result_json=run_scoped_path(dispatch_root, f"{tag}-result", run_token),
            # ...and where the head records which sim job waits on which planned config,
            # so the build job can release a compile key's sims once built.
            # Keyed on the head pid like the envelope beside it, and only for a backend
            # whose jobs can be released: `local-parallel` has no pending queue.
            gates_json=(
                run_scoped_path(dispatch_root, "gates", run_token)
                # ...and never for the verilate half: it leaves sources and a Makefile,
                # so a released simulation would find no build.
                if backend.name == "slurm"
                and configured_dependency is None
                and phase != BUILD_PHASE_VERILATE
                else None
            ),
            # The head's artefact namespace. The shared build it populates stays keyed
            # on the compile fingerprint; the tag only moves this job's per-test outputs
            # and transcripts.
            run_tag=self._run_tag,
        )
        # A stale build-result must not annotate this run's collection.
        Path(spec.result_json).unlink(missing_ok=True)
        # Same for the gates manifest: this pid may have been a head before, and the
        # build job polls for the path before the fan-out writes it. It also carries a
        # run token, but a file never read beats one read and rejected.
        if spec.gates_json is not None:
            Path(spec.gates_json).unlink(missing_ok=True)
        return backend.submit_build(spec, dependency=dependency)

    @staticmethod
    def _release_blocking_dependency(backend, *, suite_dir):
        """Return a user-configured dependency that per-key release must not clear.

        `sbatch-args` can carry a site dependency (e.g. `--dependency=singleton`
        serialising a licensed simulator). It is appended after the generated `afterok`,
        so it is the sim job's effective gate, and `scontrol update Dependency=` clears
        the whole expression. Releasing a key would drop the site's serialisation.

        Slurm takes a dependency expression whole, so such a suite gets no early
        release: no gates manifest, no `--gates`. Logged once per suite at INFO, since
        it is deliberate configuration.

        An exported ``$SBATCH_DEPENDENCY`` is not that case: a command-line
        `--dependency` overrides it, and every gated submission carries a generated one,
        so clearing is safe and early release stays. The INFO line explains why the
        export did not hold.

        ``getattr``, like every optional backend capability.
        """
        probe = getattr(backend, "_sbatch_args_dependency", None)
        configured = probe() if callable(probe) else None
        if configured is not None:
            log_event(
                logger,
                logging.INFO,
                "dispatch.gates_skipped",
                suite_dir=suite_dir,
                dependency=configured,
                reason=(
                    "early release disabled: sbatch-args configures a "
                    f"dependency ({configured}) that a release would clear"
                ),
            )
            return configured
        env_probe = getattr(backend, "_configured_dependency", None)
        exported = env_probe() if callable(env_probe) else None
        if exported is not None:
            log_event(
                logger,
                logging.INFO,
                "dispatch.env_dependency_overridden",
                suite_dir=suite_dir,
                dependency=exported,
            )
        return None

    @staticmethod
    def _audit_shared_binaries(suite_results):
        """Warn when one compile key produced more than one binary.

        Every run gated on a build job validates the same stamp and records the shared
        directory, its inputs' digest and the ``simv`` it vouched for. Runs of one
        directory naming different binaries mean it was rebuilt mid-run, and neighbours
        may have simulated a replaced executable. This is a warning, not a verdict: runs
        are already scored, and the point is to make the substitution visible.

        Grouped by ``build_dir`` (the ``obj_dir_<key>`` directory), not the fingerprint
        digest, which covers input contents and would split the very rebuild being
        looked for. A stamp without ``build_dir`` falls back to its digest.

        Reporting only, never raising: a missing or oddly shaped ``build_stamp`` is a
        run with nothing to say.
        """
        by_key = {}
        for row in suite_results:
            results = getattr(row.get("results"), "results", None)
            stamp = results.get("build_stamp") if isinstance(results, dict) else None
            if not isinstance(stamp, dict):
                continue
            sha, simv = stamp.get("fingerprint_sha"), stamp.get("simv")
            build_dir = stamp.get("build_dir")
            if (
                not isinstance(sha, str)
                or simv is None
                or not isinstance(build_dir, (str, type(None)))
            ):
                continue
            key = build_dir or sha
            group = by_key.setdefault(key, {"binaries": {}, "fingerprints": set()})
            group["binaries"].setdefault(repr(simv), []).append(row.get("test_name"))
            group["fingerprints"].add(sha)
        for key, group in by_key.items():
            binaries = group["binaries"]
            if len(binaries) < 2:
                continue
            log_console_event(
                logger,
                logging.WARNING,
                "dispatch.binary_mismatch",
                build_dir=key,
                fingerprints=len(group["fingerprints"]),
                binaries=len(binaries),
                tests=sorted({name for names in binaries.values() for name in names}),
            )

    def _dispatch_collect(self, backend, state):
        """Collect a submitted suite, retrying what deserves it.

        One pass loads every envelope (see :meth:`_dispatch_collect_pass`). Jobs
        classified retryable (killed by the scheduler while queueing for a license seat,
        and only those) are resubmitted after a jittered backoff, waited on and
        collected again, up to ``cfg-dispatch.retry.attempts`` extra attempts. Without a
        ``retry:`` block nothing is retryable and this is a single pass.

        Every pass writes a result for every row before any retry, so an interrupted or
        exhausted retry leaves the fleet scored and a vanished job never scores green.

        Retries are per suite: a suite's second round is submitted and drained before
        the next suite is collected. That serialises only the retries; later suites'
        envelopes are already on disk.
        """
        suite_results = state["suite_results"]
        pending = state["pending"]
        if not pending:
            self._close_run_manifest(state, STATUS_COLLECTED)
            return suite_results
        retry_cfg = self.root_cfg.get_dispatch_cfg().effective_retry()
        attempt = 0
        # Each round has its own submission time: a retried job rewrites its capture, so
        # the previous attempt's evidence must not be re-read.
        submitted_at = state.get("submitted_at")
        while True:
            retryable = self._dispatch_collect_pass(
                backend,
                state,
                pending,
                attempt=attempt,
                retry_cfg=retry_cfg,
                submitted_at=submitted_at,
            )
            if not retryable:
                self._audit_shared_binaries(suite_results)
                self._close_run_manifest(state, STATUS_COLLECTED)
                return suite_results
            attempt += 1
            resubmitted_at = time.time()
            try:
                pending = self._resubmit_retryable(
                    backend,
                    retryable,
                    attempt=attempt,
                    retry_cfg=retry_cfg,
                    suite_results=suite_results,
                    # A retry is a fresh submission with new job ids. Record them before
                    # waiting on the round: `_resubmit_retryable` blocks until it
                    # drains, and a head killed mid-retry would otherwise point at the
                    # previous attempt's jobs.
                    on_submitted=lambda accepted: self._record_retry_handles(
                        state, accepted
                    ),
                )
                submitted_at = resubmitted_at
            except (FatalRtlBuddyError, OSError, subprocess.SubprocessError) as e:
                # A retry is a best-effort second chance, never a way to lose a scored
                # regression: every row is already written and the retryable ones
                # already say they produced no result, so degrade to that instead of
                # discarding the run.
                # Narrow on purpose: catch the flaky-cluster failures retry exists to
                # survive (a refusing ``sbatch``, ``max-wait`` elapsing, the filesystem
                # giving out); a TypeError/KeyError/AttributeError is a bug and must
                # surface.
                # BaseException (Ctrl-C) is not caught; `_resubmit_retryable` has
                # already taken this attempt's jobs down.
                log_console_event(
                    logger,
                    logging.WARNING,
                    "dispatch.retry_abandoned",
                    backend=backend.name,
                    attempt=attempt,
                    jobs=len(retryable),
                    error=str(e),
                )
                self._audit_shared_binaries(suite_results)
                self._close_run_manifest(state, STATUS_COLLECTED)
                return suite_results

    def _record_retry_handles(self, state, resubmitted):
        """Re-point this suite's run manifest at a retry round's job ids.

        Called inside the resubmission, before the wait. Never raises: the round is
        already accepted, and a manifest that cannot be rewritten must not cancel it.
        """
        path = (state or {}).get("run_manifest")
        if path is None:
            return
        reason = update_pending_job_ids(path, resubmitted)
        if reason is not None:
            log_event(
                logger,
                logging.DEBUG,
                "dispatch.run_manifest_retry_failed",
                path=str(path),
                error=reason,
            )

    def _resubmit_retryable(
        self,
        backend,
        retryable,
        *,
        attempt,
        retry_cfg,
        suite_results=None,
        on_submitted=None,
    ):
        """Re-launch the retryable jobs after their backoff; wait; return them.

        The backend serves the delay (Slurm holds the job on ``--begin``, the local pool
        in its queue); the head never sleeps, so a delayed job holds no allocation the
        license pool needs.

        Each attempt gets its own scheduler log (``…-retry<N>.log``) to keep the first
        attempt's evidence. The result envelope path is unchanged: the job and head must
        agree on it, and this run's token still guards it.

        ``on_submitted`` is called with the accepted ``[(row, handle)]`` once the round
        is out and before the wait.

        The longest delay is passed to ``wait_all`` as ``extra_wait``, so a
        ``cfg-dispatch.max-wait`` shorter than the backoff does not trip on every retry
        round. ``max-wait`` still bounds each wait, not their sum.
        """
        resubmitted = []
        staged = []
        longest_delay = 0.0
        try:
            for idx, handle, classifier in retryable:
                delay = backoff_delay(attempt, retry_cfg)
                longest_delay = max(longest_delay, delay)
                spec = self._retry_spec(handle.spec, attempt=attempt)
                # Console-visible: a green run that needed three attempts must not read
                # like one that needed none, and INFO never reaches a CI console.
                log_console_event(
                    logger,
                    logging.INFO,
                    "dispatch.retry",
                    backend=backend.name,
                    job_id=handle.job_id,
                    test=spec.test_name,
                    run_id=spec.run_id,
                    attempt=attempt,
                    attempts=retry_cfg.attempts,
                    delay_sec=round(delay, 1),
                    classifier=classifier,
                )
                # No `dependency`: safe because a job is retryable only when its suite's
                # build job succeeded (see `build_gate_open`), so the stamp is on disk
                # and the element short-circuits its compile. An afterok on a job the
                # scheduler has forgotten never becomes satisfiable.
                # A retry is a fresh `sbatch` from this moment's environment (a later
                # suite's sweep hook may have changed
                # `SBATCH_NTASKS`/`_NODES`/`_NTASKS_PER_NODE`), so the row must describe
                # this attempt.
                # Read now but applied only once the round has landed: if `sbatch`
                # refuses or the wait fails, the caller keeps the previous attempt's
                # results, which must not pair with this reservation.
                if suite_results is not None:
                    staged.append(
                        self._stage_cpu_request_metadata(
                            suite_results[idx], spec, backend=backend
                        )
                    )
                resubmitted.append((idx, backend.submit(spec, delay_sec=delay)))
            if on_submitted is not None:
                # The whole round is accepted and none waited on yet: the only moment a
                # record can be written before the head blocks.
                on_submitted(resubmitted)
            backend.wait_all([h for _, h in resubmitted], extra_wait=longest_delay)
            # The round landed, so the rows may now describe it. Anything short leaves
            # them describing the attempt whose results the caller keeps.
            self._commit_cpu_request_metadata(staged, backend=backend, attempt=attempt)
        except BaseException:
            # Same contract as the submit fan-out: a failure mid-retry must not leave
            # this attempt's jobs running behind the head.
            backend.cancel_all([h for _, h in resubmitted])
            raise
        return resubmitted

    def _stage_cpu_request_metadata(self, row, spec, *, backend):
        """Read this resubmission's cpu overrides; do not write them yet.

        Returned staged because a row's metadata must describe the attempt whose
        telemetry sits beside it: if this round's `sbatch` is refused or its wait fails,
        the caller keeps the previous attempt's results.
        """
        # The backend's own arguments, as in the first submission: the backend appends
        # them, and `root_cfg` may now belong to another suite.
        return (row, spec, cpu_request_overrides(backend.effective_sbatch_args))

    def _commit_cpu_request_metadata(self, staged, *, backend, attempt):
        """Apply a round's staged metadata, once that round has landed."""
        for row, spec, overrides in staged:
            was = row.get("cpus_override") or []
            self._record_cpu_request_metadata(
                row,
                per_task_cpus=spec.resources.cpus,
                overrides=overrides,
            )
            if overrides != was:
                # Worth a line: this test's advice is now derived from different
                # overrides than its first attempt.
                log_event(
                    logger,
                    logging.DEBUG,
                    "rightsize.request_overrides_changed",
                    backend=backend.name,
                    test=spec.test_name,
                    run_id=spec.run_id,
                    attempt=attempt,
                    was=was,
                    now=overrides,
                )

    @staticmethod
    def _retry_spec(spec, *, attempt):
        """The spec for one more attempt at ``spec``'s job.

        The scheduler log is tagged with the attempt number, replacing the previous tag
        (``…-retry3.log``, not ``…-retry1-retry2-retry3.log``).

        Everything else is carried, ``rebuild`` included, but never added: a gated
        element denied ``--rebuild`` on its first attempt must not acquire it, which
        would put the array back into one build directory. A spec that holds it owns its
        per-test directory and still needs it.
        """
        log_path = spec.log_path
        if log_path is not None:
            log_path = Path(log_path)
            stem = re.sub(r"-retry\d+$", "", log_path.stem)
            log_path = log_path.with_name(f"{stem}-retry{attempt}{log_path.suffix}")
        return replace(spec, log_path=log_path)

    @staticmethod
    def _build_compile_fail_desc(build_handle, build_failure):
        """One summary line for a row the suite's build job failed to compile.

        The head's spelling of the desc the gated sim job writes for itself, plus the
        scheduler job id and the location of that job's logs. The error line comes from
        the build envelope's ``error_tail``.
        """
        # BuildJobSpec.result_json is optional on the type; dispatch always sets it, but
        # an error branch must not raise a TypeError over that.
        build_logs = str(build_handle.spec.log_path)
        if build_handle.spec.result_json is not None:
            build_logs += f" and {job_log_path(build_handle.spec.result_json)}"
        entry = build_failure or {}
        return build_compile_fail_desc(
            job_id=build_handle.job_id,
            returncode=entry.get("returncode"),
            error_tail=entry.get("error_tail"),
            logs=build_logs,
        )

    def _enrich_compile_fail_desc(self, results, build_handle, build_failure):
        """Point a collected compile-fail row at the build job that broke.

        Only rows that are compile failures, recognised by the two descs rtl_buddy
        writes: the generic ``Compile failed`` and the gated sim job's build-failure
        line. Any other desc failed in simulation, and rewriting it would replace a real
        diagnosis with a guess.

        Returns whether the desc changed, so the caller can persist it into the durable
        envelope that ``rb graph results`` re-reads.
        """
        if build_handle is None:
            return False
        desc = results.results.get("desc") or ""
        if desc != COMPILE_FAIL_DESC and not desc.startswith(BUILD_COMPILE_FAIL_PREFIX):
            return False
        # The generic desc needs the compiler evidence the sim job's retry gate demands:
        # bare `failed` membership can record a setup or worker error, and a sim job
        # that saw no evidence retried, so its "Compile failed" may be the retry's own
        # failure.
        # A desc with the build prefix came from a sim job that read a recorded
        # returncode.
        returncode = (build_failure or {}).get("returncode")
        if desc == COMPILE_FAIL_DESC and not (
            isinstance(returncode, int) and returncode
        ):
            return False
        # A record with a fingerprint digest comes from a build job whose sim-side twin
        # suppresses the retry on matching inputs and stamps the build prefix. A
        # still-generic desc means that sim job did retry (inputs drifted), so this
        # failure is the retry's own.
        if desc == COMPILE_FAIL_DESC and (build_failure or {}).get("fingerprint_sha"):
            return False
        results.results["desc"] = self._build_compile_fail_desc(
            build_handle, build_failure
        )
        return results.results["desc"] != desc

    def _dispatch_collect_pass(
        self, backend, state, pending, *, attempt, retry_cfg, submitted_at=None
    ):
        """Load result envelopes for one attempt; return what may be retried.

        Joins per-job scheduler telemetry (``sacct`` reserved-vs-used, when accounting
        exists) into the in-memory results and the on-disk envelopes. A missing or
        unreadable envelope becomes a ``DispatchFailResults`` naming the scheduler state
        when known (TIMEOUT/OOM), or a ``CompileFailResults`` when the build job
        recorded that test's compile as failed.

        Returns ``[(row index, handle, classifier)]`` for missing results a remaining
        retry budget covers. ``submitted_at`` is when this attempt was submitted;
        artefacts older than it are ignored as evidence.
        """
        suite_results = state["suite_results"]
        retryable = []
        build_handle = state.get("build_handle")
        # Build-job compile outcome (advisory): map a compile failure to CompileFail
        # rather than the sim job's downstream DispatchFail.
        build_result = (
            load_build_result_json(build_handle.spec.result_json)
            if build_handle is not None
            else None
        )
        compile_failed = set(build_result["failed"]) if build_result else set()
        # Why each of those failed, keyed by test. The head adds the scheduler's build
        # job id, which leads from a summary row to `build-<id>.log`.
        build_failures = {
            entry["test"]: entry
            for entry in (build_result["builds"] if build_result else [])
            if entry.get("test") in compile_failed
        }
        # What the build job observed each config's compile to cost, keyed by test so a
        # sim row carries its compile to the summary and results overlay. Empty for an
        # envelope from a build job without the records.
        build_entries = build_result["builds"] if build_result else []
        compile_records = {
            entry["test"]: {
                "duration_sec": entry.get("duration_sec"),
                "builder": entry.get("builder"),
                "reused": entry.get("reused"),
            }
            for entry in build_entries
            if entry.get("test")
        }
        # Did this suite's build gate open? A build job that left no result did not
        # finish: the sims it gated were cancelled by `afterok` (or skipped), so nothing
        # in their artefacts is this attempt's evidence, and resubmitting would launch
        # ungated a job the head skipped.
        # A suite with no build job has no gate; one sim job per artefact directory is
        # the only writer.
        # A build job that released keys early rewrites its envelope as it goes, so one
        # can exist for a job that then died; `partial` says so. Listed tests really
        # ran; an unlisted test is the "no result" case, so the flag is per test.
        build_partial = bool(build_result and build_result.get("partial"))
        build_decided = (
            set(build_result["built"]) | set(build_result["failed"])
            if build_result
            else set()
        )

        # ...unless the job finished and only its final write was lost. Decided below
        # once the scheduler says how the build job ended; until then the conservative
        # reading stands.
        build_finished_partial = []

        def _build_gate_open(test_name):
            if build_handle is None:
                return True
            if build_result is None:
                return False
            if not build_partial or build_finished_partial:
                return True
            return test_name in build_decided

        # Keyed, not .get(): a state with pending jobs always sets run_token in
        # _dispatch_suite_submit, so a missing key is a bug; .get() would disable the
        # staleness check and let a stale PASS through.
        # The guard covers this pass's jobs, not the first attempt's fleet in
        # ``state["pending"]``.
        run_token = state["run_token"] if pending else None
        # The build handle joins the query on the first pass only: it is cheap (one
        # `sacct --jobs a,b,c`), finished by then, and never resubmitted, so re-querying
        # would duplicate a row and a write.
        verilate_handle = state.get("verilate_handle")
        query_handles = [h for _, h in pending]
        if attempt == 0:
            # The compile jobs, in run order, on the first pass only: one `sacct` covers
            # them and neither is resubmitted.
            query_handles = [
                handle
                for handle in (verilate_handle, build_handle)
                if handle is not None
            ] + query_handles
        telemetry = backend.collect_telemetry(query_handles)
        if attempt == 0 and verilate_handle is not None:
            # The verilate job's numbers, on its own envelope and in `state` for its own
            # right-sizing row. The build job's block below explains why both are
            # best-effort.
            verilate_tele = telemetry.get(telemetry_key(verilate_handle))
            if verilate_tele:
                if verilate_handle.spec.result_json is not None:
                    attach_telemetry_json(
                        verilate_handle.spec.result_json, verilate_tele
                    )
                state["verilate_telemetry"] = verilate_tele
            verilate_result = (
                load_build_result_json(verilate_handle.spec.result_json)
                if verilate_handle.spec.result_json is not None
                else None
            )
            state["verilate_compile_work"] = (
                _summarize_compile_work(verilate_result["builds"])
                if verilate_result is not None and not verilate_result.get("partial")
                else None
            )
        if attempt == 0 and build_handle is not None:
            build_tele = telemetry.get(telemetry_key(build_handle))
            if build_tele:
                # (a) travels with the artifact like a sim job's (attach_telemetry_json
                # validates no filetype); (b) stays in `state` for the
                # compile-reservation advice, the only consumer of the build job's
                # numbers.
                # Best-effort: a build job that died left no envelope, already reported
                # as a missing build result.
                if build_handle.spec.result_json is not None:
                    attach_telemetry_json(build_handle.spec.result_json, build_tele)
                state["build_telemetry"] = build_tele
            # What the job's wall clock covered. sacct cannot tell "nothing to compile"
            # from "compiled fast": a re-run of an unchanged suite short-circuits every
            # build, so a 2 h reservation shows seconds. Right-sizing needs that before
            # advising a shrink.
            # None (not a zeroed dict) when the envelope is missing, predates the
            # records, or is partial: "unknown", not "nothing".
            state["build_compile_work"] = (
                _summarize_compile_work(build_entries)
                if build_result is not None and not build_partial
                else None
            )
            if build_partial:
                # A partial envelope has two causes only the scheduler can tell apart. A
                # build job that died mid-compile never reached the unnamed tests;
                # closing their gate stops the retry round resubmitting them ungated.
                # One that ran to COMPLETED reached all of them and only lost the final
                # write that drops `partial`; treating its unnamed tests as
                # never-compiled would refuse a retry to a healthy fleet. Anything else
                # keeps the conservative reading. Reservation advice stays suppressed
                # either way.
                # Count distinct test names on both sides, the build job's unit:
                # `suite_results` has a row per (test, run_id) plus skipped ones, so it
                # would report "1 of 100 planned" for one config run a hundred times.
                # Said once per suite, on the first pass.
                planned_names = {
                    handle.spec.test_name for _, handle in state.get("pending") or ()
                }
                # The scheduler's word where there is one, the backend's otherwise:
                # `local-parallel` has no accounting (`collect_telemetry` returns {}),
                # so without asking it a partial envelope there could never be
                # recognised as finished.
                outcome = (build_tele or {}).get("state") or backend.build_outcome(
                    build_handle
                )
                if outcome == "COMPLETED":
                    build_finished_partial.append(True)
                    log_event(
                        logger,
                        logging.WARNING,
                        "dispatch.build_result_final_write_lost",
                        suite_dir=build_handle.spec.suite_dir,
                        job_id=build_handle.job_id,
                        decided=len(build_decided),
                        planned=len(planned_names) or None,
                    )
                else:
                    log_event(
                        logger,
                        logging.WARNING,
                        "dispatch.build_result_partial",
                        suite_dir=build_handle.spec.suite_dir,
                        job_id=build_handle.job_id,
                        decided=len(build_decided),
                        planned=len(planned_names) or None,
                        scheduler_state=outcome,
                    )
        for idx, handle in pending:
            # Keyed by handle, not job id: jobs on different clusters can share an id.
            tele = telemetry.get(telemetry_key(handle))
            try:
                envelope = load_result_json(
                    handle.spec.result_json, expected_run_token=run_token
                )
                results = envelope["result"]
                if handle.spec.test_name in compile_failed:
                    # The complementary case: the sim job left an envelope saying the
                    # compile failed (it declined the retry and reported the build's
                    # verdict, or in older jobs its own retry failed).
                    # Name the build job that broke, so the reader goes to
                    # `build-<id>.log` rather than a `compile.log` the retry may have
                    # overwritten.
                    if self._enrich_compile_fail_desc(
                        results,
                        build_handle,
                        build_failures.get(handle.spec.test_name),
                    ):
                        # The rewrite must outlive this pass: the summary and machine
                        # payload render from memory, but the durable
                        # ``dispatch/result-*.json`` that `rb graph results` re-reads
                        # still has the generic desc. Best-effort, like every collect
                        # annotation.
                        attach_result_key(
                            handle.spec.result_json,
                            "desc",
                            results.results.get("desc"),
                        )
            except FatalRtlBuddyError as e:
                if handle.spec.test_name in compile_failed:
                    # The build job already knows this is a compile failure; don't
                    # mislabel the killed recompile as an infra failure.
                    log_event(
                        logger,
                        logging.WARNING,
                        "dispatch.compile_failed_in_build",
                        job_id=handle.job_id,
                        test=handle.spec.test_name,
                        run_id=handle.spec.run_id,
                        build_job=build_handle.job_id,
                    )
                    results = CompileFailResults(
                        name=handle.spec.test_name + "/results",
                        desc=self._build_compile_fail_desc(
                            build_handle, build_failures.get(handle.spec.test_name)
                        ),
                    )
                else:
                    sched_state = tele.get("state") if tele else None
                    state_note = (
                        f" (scheduler state {sched_state})" if sched_state else ""
                    )
                    attempt_note = f" after {attempt + 1} attempts" if attempt else ""
                    # Both compile jobs, where the compile was split: a fan-out killed
                    # by `kill-on-invalid-dep` never ran a build job, so the log that
                    # says why is the verilate job's.
                    build_note = "".join(
                        f" and {label} log {handle.spec.log_path}"
                        for label, handle in (
                            ("verilate", verilate_handle),
                            ("build", build_handle),
                        )
                        if handle is not None
                    )
                    # The scheduler log holds the job's stdout; its rtl_buddy log is a
                    # separate file per job with the events, so name both.
                    job_note = f" and {job_log_path(handle.spec.result_json)}"
                    # A job the scheduler reports COMPLETED (exit 0) with no envelope is
                    # a contradiction: on a shared filesystem it usually means client
                    # attribute-cache staleness, not a failure. Point at that first so
                    # it is not misread as a scheduler kill.
                    if sched_state == "COMPLETED":
                        cause = (
                            "job COMPLETED (exit 0) but its result is not "
                            "visible on the shared filesystem — likely a "
                            "client attribute-cache delay; check the mount's "
                            "ac* / lookupcache settings"
                        )
                    elif backend.scheduled:
                        cause = (
                            "scheduler kill, crash, or its build job failed so "
                            "afterok cancelled it"
                        )
                    else:
                        # No scheduler (local-parallel): the job crashed, was cancelled
                        # with the fleet, or never started because its build job failed.
                        cause = (
                            "the job crashed or was cancelled, or its build job "
                            "failed so the job never ran"
                        )
                    # Only a resource-condition kill whose fresh output ends inside the
                    # license queue is retryable: a hung test reaches the same TIMEOUT
                    # after real simulator output and must keep failing. The budget is
                    # checked first.
                    # `scheduled` decides whether a scheduler state is required: a
                    # backend that runs jobs itself reports none, and demanding one
                    # would make retry dead code.
                    classifier = (
                        classify_missing_result(
                            handle.spec,
                            sched_state,
                            classifiers=retry_cfg.classifiers,
                            scheduled=backend.scheduled,
                            build_succeeded=_build_gate_open(handle.spec.test_name),
                            submitted_at=submitted_at,
                        )
                        if retry_cfg.enabled and attempt < retry_cfg.attempts
                        else None
                    )
                    log_event(
                        logger,
                        logging.ERROR,
                        "dispatch.result_missing",
                        job_id=handle.job_id,
                        test=handle.spec.test_name,
                        run_id=handle.spec.run_id,
                        scheduler_state=sched_state,
                        attempt=attempt + 1,
                        retry_classifier=classifier,
                        error=str(e),
                    )
                    results = DispatchFailResults(
                        name=handle.spec.test_name + "/results",
                        desc=f"dispatch job {handle.job_id} produced no "
                        f"result{state_note}{attempt_note} ({cause}); see "
                        f"{handle.spec.log_path}{job_note}{build_note}: {e}",
                    )
                    if classifier is not None:
                        retryable.append((idx, handle, classifier))
            if tele is not None:
                # In memory for aggregation (right-sizing) and folded into the envelope
                # so telemetry travels with the artifact.
                results.results["telemetry"] = tele
                attach_telemetry_json(handle.spec.result_json, tele)
            compile_record = compile_records.get(handle.spec.test_name)
            if compile_record is not None:
                # The build job's observation of this test's compile. Where it disagrees
                # with the row's head-side `builder` (a preproc hook overrode it), the
                # envelope wins.
                # Folded into `result.results`, which is what `rb graph results` reads.
                # A copy per row: one record backs every run_id of a test.
                results.results["compile"] = dict(compile_record)
                attach_result_key(handle.spec.result_json, "compile", compile_record)
            suite_results[idx]["results"] = results
        return retryable

    def _simulator_family_of(self, builder_name):
        """Simulator family for a resolved builder name (advice gating).

        Returns ``None`` (unknown) instead of raising: analysis is advisory and runs
        after every job completed, so an unresolvable builder must not abort a finished
        run.
        """
        try:
            return self.root_cfg.resolve_rtl_builder_cfg(
                builder_name
            ).get_simulator_family()
        except FatalRtlBuddyError:
            return None

    def _analyze_reservations(
        self,
        suite_results,
        *,
        suite_display,
        suite_config_path=None,
        reg_level=None,
        backend=None,
        state=None,
    ):
        """Right-size one dispatched suite's rows into advice findings.

        ``state`` is the suite's dispatch state; it carries the build job's own
        telemetry when collect saw any, since the build job has no ``suite_results``
        row.
        """
        rightsize_cfg = self.root_cfg.get_dispatch_cfg().effective_rightsize()
        if not rightsize_cfg.report:
            return []
        # Where the `sbatch-args` the jobs were submitted with live. The overrides come
        # from the backend, so the `edit_hint` file must too: the backend was built from
        # the orchestration root_config.yaml, while `self.root_cfg` is the root this
        # suite walked up to.
        # getattr: analysis is advisory and must not abort a finished run.
        sbatch_args_config_path = getattr(backend, "effective_sbatch_args_path", None)
        # The suite's own `compile:` block as submit resolved it, and which fields it
        # won. .get(), unlike `build_handle` below: an old or hand-built state must
        # degrade to root-config-only attribution. Resolved once so both analyses
        # attribute the same reservation.
        suite_compile = (state or {}).get("suite_compile")
        # Attributed for the mode this run reserved in, so a hint names
        # `compile.modes.cov.mem` where that block governed.
        compile_origins = compile_resource_origins(
            suite_compile, builder_mode=self.rtl_builder_mode
        )
        findings = analyze_suite_reservations(
            suite_results,
            suite_display=suite_display,
            # Absolute tests.yaml path in the machine edit_hint so an agent with a
            # different cwd can apply it; the human table uses the short suite_display.
            suite_config_path=suite_config_path or suite_display,
            rightsize_cfg=rightsize_cfg,
            reg_level=reg_level,
            simulator_family_of=self._simulator_family_of,
            # cfg-dispatch lives in root_config.yaml, so advice about a reservation the
            # compile block governs must point there, not at tests.yaml. getattr:
            # analysis is advisory and must not abort a completed run.
            root_config_path=getattr(self.root_cfg, "root_cfg_path", None),
            # ...except a cpu override in `sbatch-args`, which belongs to the backend
            # and names the backend's config.
            sbatch_args_config_path=sbatch_args_config_path,
            # ...unless the suite's own compile block governs that field: then
            # cfg-dispatch is the layer it overrides and the hint names the suite. This
            # reaches an in-job compile's rows, where no build job exists.
            compile_origins=compile_origins,
            # How often the scheduler sampled usage, so an unmeasured peak cannot become
            # a mem suggestion.
            accounting_interval_s=(
                backend.accounting_interval_s() if backend is not None else None
            ),
        )
        build_telemetry = (state or {}).get("build_telemetry")
        if build_telemetry:
            # Keyed, not .get(): collect stashes build telemetry only when it had a
            # build handle, so the two travel together and a missing handle is a bug
            # that must fail loud.
            build_spec = state["build_handle"].spec
            findings.extend(
                analyze_build_reservation(
                    build_telemetry,
                    # The per-build reservation, not the scaled one the build spec
                    # carries: the advice names cfg-dispatch.compile.cpus, which is per
                    # build.
                    # Use submit's own resolution where there is one (with per-testbench
                    # `compile:` blocks the build job is sized by the maximum over
                    # planned testbenches, which analysis cannot recompute).
                    (state or {}).get("build_compile_resources")
                    or resolve_compile_resources(
                        self.root_cfg.get_dispatch_cfg(),
                        suite_compile,
                        builder_mode=self.rtl_builder_mode,
                    ),
                    build_spec.parallel,
                    rightsize_cfg,
                    suite_display,
                    # cfg-dispatch lives in root_config.yaml; getattr, as in the
                    # per-test analysis, because advice must never abort a completed
                    # run.
                    getattr(self.root_cfg, "root_cfg_path", None),
                    # Whether anything compiled: without it a re-run whose builds all
                    # short-circuited on stamps reads as a fast compile and advises a
                    # limit the next real change times out against.
                    compile_work=state.get("build_compile_work"),
                    # TotalCPU accumulates from usage samples, so a build job shorter
                    # than one interval was measured at most once; same reason memory
                    # advice is gated.
                    accounting_interval_s=(
                        backend.accounting_interval_s() if backend is not None else None
                    ),
                    # Per-field provenance, so a value the suite block won points at the
                    # suite's tests.yaml, not a cfg-dispatch key whose edit would move
                    # nothing.
                    # The build job's own map also names the testbench whose block won
                    # each field; the suite-wide map is the fallback for an older state
                    # dict.
                    compile_origins=(
                        (state or {}).get("build_compile_origins") or compile_origins
                    ),
                    suite_config_hint=suite_config_path or suite_display,
                    # ...and whether the resolved reservation is what the build job was
                    # submitted with: a `sbatch-args` argument or `SBATCH_*` variable
                    # setting the cpu request beats the generated flags, so neither the
                    # ratio nor the decomposition may be stated from it.
                    # Use the submit-time snapshot, not a fresh read: a later suite's
                    # sweep hook can change `os.environ`. The per-test rows carry the
                    # same snapshot.
                    cpus_override=(state or {}).get("cpus_override") or [],
                    # ...and the config those `sbatch-args` came from, so the hint's
                    # `file` is the one the backend reads. Same value as the per-test
                    # rows.
                    sbatch_args_config_path=sbatch_args_config_path,
                )
            )
        verilate_telemetry = (state or {}).get("verilate_telemetry")
        if verilate_telemetry:
            # Its own row, from its own reservation and provenance. The suite inputs are
            # the same as the build job's; the verilate provenance map spells its own
            # edit-hint keys.
            findings.extend(
                analyze_build_reservation(
                    verilate_telemetry,
                    (state or {}).get("verilate_resources")
                    or resolve_verilate_resources(
                        self.root_cfg.get_dispatch_cfg(),
                        suite_compile,
                        builder_mode=self.rtl_builder_mode,
                    ),
                    state["verilate_handle"].spec.parallel,
                    rightsize_cfg,
                    suite_display,
                    getattr(self.root_cfg, "root_cfg_path", None),
                    compile_work=(state or {}).get("verilate_compile_work"),
                    accounting_interval_s=(
                        backend.accounting_interval_s() if backend is not None else None
                    ),
                    compile_origins=(state or {}).get("verilate_origins") or {},
                    suite_config_hint=suite_config_path or suite_display,
                    cpus_override=(state or {}).get("cpus_override") or [],
                    sbatch_args_config_path=sbatch_args_config_path,
                    phase="verilate",
                )
            )
        return _raise_first(findings)

    def _render_reservation_advice(self, findings):
        rows = [
            {
                "suite": f.suite,
                "test": f.test,
                "resource": f.resource,
                "phase": f.phase,
                # The requested reservation, with what the scheduler handed out beside
                # it when they differ; whole-core rounding is not something an edit to
                # the named field can move.
                "reserved": (
                    f"{f.reserved} ({f.allocated} allocated)"
                    if f.allocated
                    else f.reserved
                ),
                "peak": f.peak,
                "utilization": f"{f.utilization:.0%}",
                "advice": f"{f.direction} → {f.suggested}",
                "field": f.edit_hint.get("path", ""),
            }
            for f in findings
        ]
        metadata = [
            "rtl-buddy suggests; apply by editing the named Field",
        ]
        if any(f.phase == "compile+sim" for f in findings):
            # Without this the numbers read as sim-only and a compile-sized reservation
            # looks over-reserved. Gated on the phase it describes.
            metadata.append(
                "compile+sim rows measure a job that also compiled (its "
                "builder cannot share a build), so the peak spans both phases"
            )
        if any(f.allocated for f in findings):
            # Without this the parenthesised number reads as a second reservation to
            # edit, when it is the scheduler's rounding.
            metadata.append(
                "Reserved is what the reservation asked for; the "
                "parenthesised figure is what the scheduler allocated — a "
                "site that hands out whole cores gives more than was "
                "requested, and no edit to Field changes that"
            )
        if any(f.suggested_total for f in findings):
            # Without this the number reads as the reservation to end up with, and
            # writing it into the named field overshoots by what the other builds
            # contribute.
            metadata.append(
                "an aggregated row's suggestion is the named Field's OWN new "
                "value, not the whole-job figure: the build job reserves the "
                "sum of its builds, so writing the total into one of them "
                "would overshoot"
            )
        compile_rows = [f for f in findings if f.phase == "compile"]
        if compile_rows:
            # Name the key that governs these rows: a suite whose own `compile:` block
            # sets `parallel` is not moved by cfg-dispatch. One regression can table
            # suites that disagree; the footnote then states the rule, since each row's
            # file is in the Field column.
            parallel_keys = {
                f.parallel_origin for f in compile_rows if f.parallel_origin
            }
            if len(parallel_keys) == 1:
                parallel_key = parallel_keys.pop()
            elif parallel_keys:
                parallel_key = (
                    "the resolved compile.parallel (suite block or cfg-dispatch)"
                )
            else:
                # Nothing said: findings from a caller without the origin keep their
                # wording.
                parallel_key = "cfg-dispatch.compile.parallel"
            note = (
                "the compile row is the suite's build job: one allocation "
                f"running up to {parallel_key} builds at once"
            )
            # The cpus row is gated independently (efficiency threshold; a reduce needs
            # evidence a compile ran), so a table whose only build-job row is `time`
            # must not carry a footnote about a missing column.
            if any(f.resource == "cpus" for f in compile_rows):
                note += ", so its cpus suggestion is per-build"
            metadata.append(note)
        render_summary(
            title="Reservation Advice (reserved vs used)",
            columns=[
                ("suite", "Suite"),
                ("test", "Test"),
                ("resource", "Resource"),
                ("phase", "Phase"),
                ("reserved", "Reserved"),
                ("peak", "Peak used"),
                ("utilization", "Util"),
                ("advice", "Advice"),
                ("field", "Field"),
            ],
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def do_rtl_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to regressions.yaml",
                show_default="Use ./regression.yaml if present, otherwise root_config.yaml reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int, typer.Option("-l", "--reg-level", help="regression level to stop at")
        ] = 0,
        start_level: Annotated[
            int,
            typer.Option("-s", "--start-level", help="regression level to start at"),
        ] = 0,
        master_seed: Annotated[
            int | None,
            typer.Option(
                "--master-seed",
                help="derive an exact, stable runtime seed for every selected test",
            ),
        ] = None,
        coverage_merge: Annotated[
            bool,
            typer.Option(
                "--coverage-merge",
                help="merge coverage across regression tests; uses raw merge for summary/html and info-process for Coverview",
            ),
        ] = False,
        coverage_merge_raw: Annotated[
            bool,
            typer.Option(
                "--coverage-merge-raw",
                help="use raw Verilator merge for merged summary/html/Coverview",
            ),
        ] = False,
        coverage_merge_info_process: Annotated[
            bool,
            typer.Option(
                "--coverage-merge-info-process",
                help="use info-process merge for merged summary/Coverview; HTML merge is not supported",
            ),
        ] = False,
        coverage_html: Annotated[
            bool,
            typer.Option(
                "--coverage-html",
                help="generate merged LCOV HTML output in coverage_merge.html",
            ),
        ] = False,
        coverage_coverview: Annotated[
            bool,
            typer.Option(
                "--coverage-coverview",
                help="generate Coverview zip output from coverage info",
            ),
        ] = False,
        coverage_per_test: Annotated[
            bool,
            typer.Option(
                "--coverage-per-test",
                help="package one Coverview dataset per test in regression mode",
            ),
        ] = False,
        coverage_dir_summary: Annotated[
            list[str] | None,
            typer.Option(
                "--coverage-dir-summary",
                help="append coverage summary lines for repo-relative directory prefixes; may be repeated",
            ),
        ] = None,
        coverage_dir_summary_file: Annotated[
            str | None,
            typer.Option(
                "--coverage-dir-summary-file",
                help="file containing repo-relative directory prefixes, one per line",
            ),
        ] = None,
        coverage_source_summary: Annotated[
            bool,
            typer.Option(
                "--coverage-source-summary",
                help="append run coverage scored per source point (covered when any elaboration hit it), beside the per-elaboration figure",
            ),
        ] = False,
        coverage_model: Annotated[
            str,
            typer.Option(
                "--coverage-model",
                help="coverage-model.json to write: full (per-test attribution per point), totals (points without attribution), or none (manifest and totals only)",
                metavar="[full|totals|none]",
                click_type=click.Choice(list(cov_model.MODEL_MODES)),
            ),
        ] = cov_model.MODEL_MODE_FULL,
        share_build: Annotated[
            bool,
            typer.Option(
                "--share-build",
                help="reuse one compiled simv across tests with identical compile inputs (Verilator builders only)",
            ),
        ] = False,
        shared_build_root: Annotated[
            str,
            typer.Option(
                "--shared-build-root",
                help="persistent directory the shared builds are cached under, "
                "so the cache survives a workspace wipe",
                show_default="cfg-rtl-reg shared-build-root, else in-tree",
            ),
        ] = None,
        rebuild: Annotated[
            bool,
            typer.Option(
                "--rebuild",
                help="recompile even when a valid build already exists "
                "(implies nothing about --share-build)",
            ),
        ] = False,
        dispatch: Annotated[
            str,
            typer.Option(
                "--dispatch",
                help="execution backend for test runs (local, local-parallel, slurm)",
                show_default="cfg-dispatch backend, else local",
            ),
        ] = None,
        jobs: Annotated[
            int,
            typer.Option(
                "-j",
                "--jobs",
                help="concurrent jobs for --dispatch local-parallel",
                show_default="cfg-dispatch jobs, else min(4, cpu count)",
            ),
        ] = None,
        orphans: Annotated[
            str,
            typer.Option(
                "--orphans",
                help="what to do about an interrupted run's jobs that are "
                "still queued or running (warn, cancel, adopt)",
                show_default="cfg-dispatch orphans, else warn",
            ),
        ] = None,
        run_tag: Annotated[
            str | None,
            typer.Option(
                "--run-tag",
                help="namespace this run's artefact trees under "
                "artefacts/.runs/<tag>/ with their own tree locks and logs, "
                "so concurrent runs of the suites do not collide; shared "
                "builds stay shared",
            ),
        ] = None,
    ):
        """
        run rtl regression
        """
        self._orphans = orphans
        # Before the first `_enter_command_context`: the manifest root's and every
        # suite's artefact root (and tree lock) derive from it.
        self._run_tag = validate_run_tag(run_tag)
        master_seed = self._checked_master_seed(master_seed)
        merge_mode_count = sum(
            1
            for enabled in [
                coverage_merge,
                coverage_merge_raw,
                coverage_merge_info_process,
            ]
            if enabled
        )
        if merge_mode_count > 1:
            raise FatalRtlBuddyError(
                "--coverage-merge, --coverage-merge-raw, and --coverage-merge-info-process are mutually exclusive"
            )
        if coverage_merge_info_process and coverage_html:
            raise FatalRtlBuddyError(
                "--coverage-html is not supported with --coverage-merge-info-process"
            )

        self.rtl_builder_mode = (
            "reg" if self.rtl_builder_mode is None else self.rtl_builder_mode
        )
        self.share_build = share_build
        self.rebuild = rebuild
        self._shared_build_root_flag = shared_build_root
        log_event(
            logger,
            logging.INFO,
            "command.regression",
            reg_config=reg_config,
            reg_level=reg_level,
            start_level=start_level,
            share_build=share_build,
            master_seed=master_seed,
            run_tag=self._run_tag,
        )

        start_dir = str(self.invocation_cwd)
        if reg_config is not None:
            resolved_reg_config = str(
                (self.invocation_cwd / reg_config).resolve()
                if not os.path.isabs(reg_config)
                else Path(reg_config).resolve()
            )
            # Anchor orchestration to dirname(regression.yaml); each suite re-anchors to
            # its own tests.yaml directory.
            ctx = self._enter_command_context(primary_config=resolved_reg_config)
            self.reg_cfg = RegConfig(
                name=self.name + "/reg_config", path=resolved_reg_config
            )
            log_event(
                logger, logging.INFO, "regression.config_override", path=reg_config
            )
        else:
            local_reg_config = str(self.invocation_cwd / "regression.yaml")
            if os.path.isfile(local_reg_config):
                ctx = self._enter_command_context(primary_config=local_reg_config)
                self.reg_cfg = RegConfig(
                    name=self.name + "/reg_config", path=local_reg_config
                )
                log_event(
                    logger,
                    logging.INFO,
                    "regression.config_local_default",
                    path=local_reg_config,
                )
            else:
                # Defer to root_config.yaml: its reg-cfg-path is anchored to the root
                # config directory, which becomes the command root.
                ctx = self._enter_command_context(command_root=self.invocation_cwd)
                self.reg_cfg = self.root_cfg.get_rtl_reg_cfg()
                ctx = self._enter_command_context(
                    primary_config=self.reg_cfg.get_path()
                )
                log_event(
                    logger,
                    logging.INFO,
                    "regression.config_root_default",
                    path=self.reg_cfg.get_path(),
                    # `flow` is on every emission of this event, not only per-flow
                    # commands': one event name, one field set.
                    flow="sim",
                )

        reg_dir = os.path.dirname(self.reg_cfg.get_path())
        emit_console_text(f"Running regression from {reg_dir}", style="cyan")
        seed_mode = SeedMode.MASTER if master_seed is not None else SeedMode.DEFAULT
        if master_seed is not None:
            log_event(
                logger,
                logging.INFO,
                "regression.seed_plan",
                master_seed=master_seed,
                derivation_version=SEED_DERIVATION_VERSION,
            )

        # Dispatch implies share_build: the build job is what lets sim jobs skip
        # compilation.
        dispatch_backend = self._resolve_dispatch_backend(dispatch, jobs=jobs)
        if dispatch_backend is not None:
            # An early stop before POST cannot be honoured per job; the message names
            # whether --dispatch or cfg-dispatch.backend chose the backend, so its
            # remedy exists.
            self._reject_early_stop_under_dispatch(dispatch, dispatch_backend)
            if not share_build:
                self.share_build = True
                log_event(
                    logger,
                    logging.INFO,
                    "dispatch.share_build_implied",
                    backend=dispatch_backend.name,
                )

        exit_code = 0
        reg_results = []
        reservation_findings = []
        # Per-suite ExecutionContext re-anchors the file log under each tests.yaml
        # directory. The process CWD is unchanged; the test runner passes suite_dir
        # explicitly.
        orchestration_ctx = ctx
        if dispatch_backend is not None:
            # Expand every suite before the first submission: keeps sweep hooks
            # head-only and lets the regression reject two co-located configs whose
            # expanded tests would write the same artefact directory.
            suite_configs = list(self.reg_cfg.get_suite_configs())
            namespaces = self._dispatch_regression_namespaces(suite_configs)
            prepared_suites = []
            for suite_cfg in suite_configs:
                log_event(
                    logger,
                    logging.INFO,
                    "regression.suite_start",
                    suite=suite_cfg.get_path(),
                    cwd=os.path.dirname(suite_cfg.get_path()),
                )
                self._enter_command_context(primary_config=suite_cfg.get_path())
                suite_results = []
                entries = self._plan_dispatch_suite(
                    suite_cfg,
                    test_name=None,
                    reg_level=reg_level,
                    start_level=start_level,
                    run_ids=[None],
                    suite_results=suite_results,
                    seed_mode=seed_mode,
                    master_seed=master_seed,
                )
                prepared_suites.append(
                    {
                        "suite_cfg": suite_cfg,
                        "entries": entries,
                        "suite_results": suite_results,
                        "dispatch_namespace": namespaces[
                            str(Path(suite_cfg.get_path()).resolve())
                        ],
                        # A sweep hook is `exec()`d in this process and may set or unset
                        # `SBATCH_*`, and entering a suite loads its `.rtl-buddy/.env`.
                        # Snapshot the environment right after this suite's planning so
                        # its submission sees exactly that (docs/concepts/dispatch.md).
                        "environ": dict(os.environ),
                    }
                )
            self._validate_dispatch_test_artifacts(prepared_suites)
            # Retries, collection and analysis run from whatever the process holds after
            # every hook has run.
            final_environ = dict(os.environ)

            # Submit every suite before waiting: the fleet spans all suites, so slow
            # suites overlap.
            submitted = []
            all_handles = []
            # Jobs this run inherited (`--orphans adopt`), by identity. Until the
            # fleet-wide wait begins they are not this run's to destroy: a later suite
            # that finds no matching orphan fails this invocation but must leave an
            # earlier suite's fleet queued, recorded `running` and adoptable.
            # From the wait onwards an interrupt takes them down with the rest.
            adopted_handles = set()
            waiting = False
            # One nonce for the whole regression run, shared across suites, so a
            # collector rejects any envelope not from this run.
            run_token = uuid.uuid4().hex
            try:
                for prepared in prepared_suites:
                    suite_cfg = prepared["suite_cfg"]
                    _replace_environ(prepared["environ"])
                    self._enter_command_context(primary_config=suite_cfg.get_path())
                    state = self._dispatch_suite_submit(
                        suite_cfg,
                        dispatch_backend,
                        run_token=run_token,
                        prepared=prepared,
                        dispatch_namespace=prepared["dispatch_namespace"],
                        reg_level=reg_level,
                        start_level=start_level,
                        seed_mode=seed_mode,
                        master_seed=master_seed,
                    )
                    self._announce_dispatched_suite(
                        state,
                        backend=dispatch_backend,
                        suite=self._display_path(
                            suite_cfg.get_path(), base_dir=start_dir
                        ),
                    )
                    submitted.append((suite_cfg, state))
                    # A suite that selected zero tests submits nothing and returns
                    # build_handle=None; a None in all_handles crashes wait_all and
                    # cancel_all.
                    suite_handles = self._state_handles(state)
                    all_handles.extend(suite_handles)
                    if state.get("adopted"):
                        adopted_handles.update(id(handle) for handle in suite_handles)
                    # A backend that runs jobs itself may have freed a slot during the
                    # next suite's submission; give it a chance to refill. A
                    # scheduler-backed backend no-ops.
                    dispatch_backend.advance()
                _replace_environ(final_environ)
                if all_handles:
                    waiting = True
                    dispatch_backend.wait_all(all_handles)
            except BaseException:
                # Interrupt or fatal error on the head: don't leave the fleet running,
                # but "the fleet" is what this run launched. Before the wait, an adopted
                # suite's jobs belong to the run that submitted them.
                _replace_environ(final_environ)
                doomed_states = [
                    state
                    for _suite_cfg, state in submitted
                    if waiting or not state.get("adopted")
                ]
                doomed = [
                    handle
                    for handle in all_handles
                    if waiting or id(handle) not in adopted_handles
                ]
                dispatch_backend.cancel_all(doomed)
                # ...and don't leave a cancelled suite's manifest claiming a live fleet:
                # the next invocation reads them. Only suites whose jobs were taken
                # down, and only once they are gone.
                for submitted_state in doomed_states:
                    self._close_cancelled_run_manifest(
                        dispatch_backend, submitted_state
                    )
                raise
            for suite_cfg, state in submitted:
                # Re-anchor the file log under this suite before collecting, so a
                # collect-time dispatch.result_missing lands in the suite's own
                # rtl_buddy.log.
                self._enter_command_context(primary_config=suite_cfg.get_path())
                suite_results = self._dispatch_collect(dispatch_backend, state)
                suite_display = self._display_path(
                    suite_cfg.get_path(), base_dir=start_dir
                )
                reservation_findings.extend(
                    self._analyze_reservations(
                        suite_results,
                        suite_display=suite_display,
                        suite_config_path=str(Path(suite_cfg.get_path()).resolve()),
                        reg_level=reg_level,
                        backend=dispatch_backend,
                        state=state,
                    )
                )
                reg_results.append(
                    {
                        "test_suite": suite_display,
                        "test_suite_path": str(
                            Path(suite_cfg.get_path()).resolve().parent
                        ),
                        "results": suite_results,
                    }
                )
                exit_code |= self._exit_code_from_results(suite_results)
            reservation_findings = _raise_first(reservation_findings)
        else:
            for suite_cfg in self.reg_cfg.get_suite_configs():
                suite_cfg_dir = os.path.dirname(suite_cfg.get_path())
                log_event(
                    logger,
                    logging.INFO,
                    "regression.suite_start",
                    suite=suite_cfg.get_path(),
                    cwd=suite_cfg_dir,
                )
                self._enter_command_context(primary_config=suite_cfg.get_path())
                suite_results = self._do_test_suite(
                    suite_cfg=suite_cfg,
                    test_name=None,
                    test_runner_mode={"sim_to_stdout": False},
                    reg_level=reg_level,
                    start_level=start_level,
                    run_ids=[None],
                    seed_mode=seed_mode,
                    replay_run_id=None,
                    master_seed=master_seed,
                )
                reg_results.append(
                    {
                        "test_suite": self._display_path(
                            suite_cfg.get_path(), base_dir=start_dir
                        ),
                        # Absolute suite dir, used as the coverage source_root;
                        # recombining the display path with command_root breaks when the
                        # invocation cwd differs.
                        "test_suite_path": str(
                            Path(suite_cfg.get_path()).resolve().parent
                        ),
                        "results": suite_results,
                    }
                )
                exit_code |= self._exit_code_from_results(suite_results)
        # Re-anchor the orchestration log to the regression root so coverage merge
        # artifacts and the final summary land beside regression.yaml.
        self._enter_command_context(command_root=orchestration_ctx.command_root)
        ctx = orchestration_ctx
        _log_reservation_advice(reservation_findings)

        all_suite_results = []
        for reg_result in reg_results:
            all_suite_results.extend(reg_result["results"])

        metadata = [
            self._builder_metadata_line(list(self.reg_cfg.get_suite_configs())),
            f"Builder Mode: {self.rtl_builder_mode}",
        ]
        if master_seed is not None:
            metadata.append(f"Master Seed: {master_seed}")
        dir_summary_paths = self._resolve_coverage_dir_summary_paths(
            coverage_dir_summary=coverage_dir_summary,
            coverage_dir_summary_file=coverage_dir_summary_file,
        )
        self._guard_coverage_requested(
            all_suite_results,
            exit_code,
            coverage_merge=coverage_merge,
            coverage_merge_raw=coverage_merge_raw,
            coverage_merge_info_process=coverage_merge_info_process,
            coverage_html=coverage_html,
            coverage_coverview=coverage_coverview,
            coverage_dir_summary=coverage_dir_summary,
            coverage_dir_summary_file=coverage_dir_summary_file,
            coverage_source_summary=coverage_source_summary,
        )
        # The per-suite HTML branch below drops each payload, so seed the run-level
        # cover points here. Key omitted when there are none, so "absent" keeps meaning
        # "not collected".
        coverage_payload = {
            "merged": None,
            "dir_summary": [],
            "merge_failed": False,
            "failed_metrics": [],
        }
        seed_covers = self.coverage.collect_cover_records(all_suite_results)
        if seed_covers:
            coverage_payload["covers"] = seed_covers
        if (
            coverage_html
            and not coverage_merge
            and not coverage_merge_raw
            and not coverage_merge_info_process
        ):
            reg_outdir = str(ctx.command_root)
            for reg_result in reg_results:
                # Per-suite HTML only: no merge, so no structured merged payload.
                cov_metadata, _ = self.coverage.build_metadata(
                    reg_result["results"],
                    outdir=reg_outdir,
                    suite_name=reg_result["test_suite"],
                    coverage_merge=False,
                    coverage_merge_raw=False,
                    coverage_html=True,
                    coverage_coverview=coverage_coverview,
                    coverage_per_test=coverage_per_test,
                    reg_results=reg_results,
                    coverage_merge_info_process=coverage_merge_info_process,
                    source_roots=[reg_result["test_suite_path"]],
                    dir_summary_paths=dir_summary_paths,
                    source_summary=coverage_source_summary,
                    command="regression",
                    model_mode=coverage_model,
                )
                metadata.extend(cov_metadata)
        else:
            reg_outdir = str(ctx.command_root)
            regression_source_roots = [
                reg_result["test_suite_path"] for reg_result in reg_results
            ]
            cov_metadata, coverage_payload = self.coverage.build_metadata(
                all_suite_results,
                outdir=reg_outdir,
                suite_name=self.reg_cfg.get_path(),
                coverage_merge=coverage_merge,
                coverage_merge_raw=coverage_merge_raw,
                coverage_html=coverage_html,
                coverage_coverview=coverage_coverview,
                coverage_per_test=coverage_per_test,
                reg_results=reg_results,
                coverage_merge_info_process=coverage_merge_info_process,
                source_roots=regression_source_roots,
                dir_summary_paths=dir_summary_paths,
                source_summary=coverage_source_summary,
                command="regression",
                model_mode=coverage_model,
            )
            metadata.extend(cov_metadata)
        # Same rule as `test`, applied once the artefacts are written.
        exit_code |= self._coverage_merge_exit_code(coverage_payload)

        self._refresh_result_side_cars(all_suite_results)
        # Rendered in both modes; in machine mode it emits the "summary" log event and
        # leaves stdout for the envelope.
        self._render_regression_summary(reg_results, metadata=metadata)
        if reservation_findings and not self.machine:
            self._render_reservation_advice(reservation_findings)
        if self.machine:
            payload = {
                "results": [
                    self._machine_test_row(
                        suite_result["test_name"],
                        suite_result["results"],
                        suite=reg_result["test_suite"],
                    )
                    for reg_result in reg_results
                    for suite_result in reg_result["results"]
                ]
            }
            coverage = self._machine_coverage_payload(coverage_payload)
            if coverage is not None:
                payload["coverage"] = coverage
            if dispatch_backend is not None:
                payload["reservation_advice"] = [
                    finding.as_event() for finding in reservation_findings
                ]
            if master_seed is not None:
                payload["master_seed"] = master_seed
            self._emit_machine_result("regression", exit_code, **payload)
        raise typer.Exit(exit_code)

    def do_gen_model_filelist(
        self,
        model_name: Annotated[str, typer.Argument(help="name of model")],
        output_path: Annotated[str, typer.Argument(help="Output filename")] = "run.f",
        model_config: Annotated[
            str, typer.Option("-c", "--model-config", help="model_config.yaml to use")
        ] = "models.yaml",
        unroll: Annotated[
            bool,
            typer.Option("--unroll", "-u", help="Recursively unroll -F in filelists"),
        ] = False,
        flatten: Annotated[
            bool,
            typer.Option(
                "--flatten",
                "-f",
                help="Remove path to a file, leaving just the filename",
            ),
        ] = False,
        strip_options: Annotated[
            bool, typer.Option("--strip", "-s", help="Remove option part of a line")
        ] = False,
        deduplicate: Annotated[
            bool, typer.Option("--deduplicate", "-d", help="Remove duplicates")
        ] = False,
    ):
        """
        generate filelists using models.yaml
        """
        ctx = self._enter_command_context(primary_config=model_config)
        model_cfg = ModelConfigLoader(str(ctx.primary_config)).get_model(model_name)
        resolved_output = str(ctx.resolve_input(output_path))
        vlog_fl = VlogFilelist(
            name=self.name + "/vlog_filelist",
            model_cfg=model_cfg,
            output_path=resolved_output,
        )

        log_event(
            logger,
            logging.INFO,
            "command.filelist",
            model=model_name,
            output=resolved_output,
        )
        vlog_fl.write_output(
            output_filepath=resolved_output,
            unroll=unroll,
            flatten=flatten,
            strip=strip_options,
            deduplicate=deduplicate,
        )
        return

    def do_cmd_hier(
        self,
        name: Annotated[
            str,
            typer.Argument(
                help=(
                    "with --view dut (default): model name from models.yaml; "
                    "with --view tb: test name from tests.yaml (the test "
                    "pins both the model + the testbench top)"
                )
            ),
        ],
        model_config: Annotated[
            str, typer.Option("-c", "--model-config", help="models.yaml to use")
        ] = "models.yaml",
        test_config: Annotated[
            str, typer.Option("--test-config", help="tests.yaml to use (--view tb)")
        ] = "tests.yaml",
        view: Annotated[
            str,
            typer.Option(
                "--view",
                help=(
                    "what to render: 'dut' (default) renders the model "
                    "hierarchy rooted at --top; 'tb' renders the testbench "
                    "hierarchy with the DUT called out as a subtree. With "
                    "--view tb the positional argument is a test name."
                ),
            ),
        ] = "dut",
        fmt: Annotated[
            str,
            typer.Option(
                "--format",
                help="output format: tree, dot, mermaid, json",
            ),
        ] = "tree",
        output: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--output",
                help="write renderer output to file instead of stdout",
            ),
        ] = None,
        frontend: Annotated[
            str | None,
            typer.Option("--frontend", help="parser frontend (verible|slang)"),
        ] = None,
        cdc_annotations: Annotated[
            str | None,
            typer.Option(
                "--cdc-annotations",
                help="clock-domain map JSON from `rtl-buddy-cdc --emit-domain-map`",
            ),
        ] = None,
        rdc_annotations: Annotated[
            str | None,
            typer.Option(
                "--rdc-annotations",
                help="reset-domain map JSON from `rtl-buddy-cdc --emit-reset-domain-map`",
            ),
        ] = None,
        clock_legend: Annotated[
            bool,
            typer.Option(
                "--clock-legend",
                help="dot format only: emit a side legend of clock colors",
            ),
        ] = False,
        block_diagram: Annotated[
            bool,
            typer.Option(
                "--block-diagram",
                help=(
                    "dot format only: render sibling dataflow as a block "
                    "diagram (cluster nesting + net-labeled edges) instead "
                    "of the hierarchy dump; requires rtl-buddy-sch >= "
                    f"{VIEW_BLOCK_DIAGRAM_MIN_VERSION}"
                ),
            ),
        ] = False,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the rtl-buddy-view binary"),
        ] = "rtl-buddy-view",
    ):
        """
        render module hierarchy via rtl-buddy-view
        """
        if view not in ("dut", "tb"):
            raise FatalRtlBuddyError(
                f"hier: --view must be 'dut' or 'tb', got {view!r}"
            )

        if view == "tb":
            from .config.suite import SuiteConfig

            ctx = self._enter_command_context(primary_config=test_config)
            suite = SuiteConfig(str(ctx.primary_config))
            tests = suite.get_tests(name)
            test_cfg = list(tests)[0]
            model_cfg = test_cfg.get_model()
            log_event(
                logger,
                logging.INFO,
                "command.hier",
                command="hier",
                test=name,
                model=model_cfg.name,
                tb=test_cfg.tb.name,
                format=fmt,
                output=output,
                view="tb",
            )
            runner = RtlBuddyView(
                name=self.name + "/hier",
                model_cfg=model_cfg,
                suite_dir=str(ctx.command_root),
                format=fmt,
                output=str(ctx.resolve_input(output)) if output else None,
                frontend=frontend,
                cdc_annotations=cdc_annotations,
                rdc_annotations=rdc_annotations,
                clock_legend=clock_legend,
                block_diagram=block_diagram,
                executable=tool,
                test_cfg=test_cfg,
            )
            raise typer.Exit(runner.run())

        ctx = self._enter_command_context(primary_config=model_config)
        model_cfg = ModelConfigLoader(str(ctx.primary_config)).get_model(name)
        log_event(
            logger,
            logging.INFO,
            "command.hier",
            command="hier",
            model=name,
            format=fmt,
            output=output,
            view="dut",
        )
        runner = RtlBuddyView(
            name=self.name + "/hier",
            model_cfg=model_cfg,
            suite_dir=str(ctx.command_root),
            format=fmt,
            output=str(ctx.resolve_input(output)) if output else None,
            frontend=frontend,
            cdc_annotations=cdc_annotations,
            rdc_annotations=rdc_annotations,
            clock_legend=clock_legend,
            block_diagram=block_diagram,
            executable=tool,
        )
        raise typer.Exit(runner.run())

    def do_cmd_hier_query(
        self,
        name: Annotated[str, typer.Argument(help="model name from models.yaml")],
        verb: Annotated[
            str,
            typer.Argument(
                help=(
                    "query verb: find-module, subtree, instances-of, "
                    "port-connections, or source-snippet"
                )
            ),
        ],
        arg: Annotated[
            str,
            typer.Argument(
                help=(
                    "verb argument: a module name (find-module, "
                    "instances-of) or a dot-separated instance path "
                    "rooted at the model (subtree, port-connections, "
                    "source-snippet)"
                )
            ),
        ],
        model_config: Annotated[
            str, typer.Option("-c", "--model-config", help="models.yaml to use")
        ] = "models.yaml",
        frontend: Annotated[
            str | None,
            typer.Option("--frontend", help="parser frontend (verible|slang)"),
        ] = None,
        fmt: Annotated[
            str | None,
            typer.Option(
                "--format",
                help="subtree only: json (default) or tree",
            ),
        ] = None,
        context: Annotated[
            int | None,
            typer.Option(
                "--context",
                help="source-snippet only: context lines on each side",
            ),
        ] = None,
        line_numbers: Annotated[
            bool,
            typer.Option(
                "--line-numbers/--no-line-numbers",
                help="source-snippet only: prefix lines with source "
                "line numbers (default on)",
            ),
        ] = True,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the rtl-buddy-view binary"),
        ] = "rtl-buddy-view",
    ):
        """
        query the module hierarchy via rtl-buddy-view

        Machine-readable sibling of `rb hier`: prints JSON for shell pipelines and
        agent tools; source-snippet prints line-numbered source text.
        """
        ctx = self._enter_command_context(primary_config=model_config)
        model_cfg = ModelConfigLoader(str(ctx.primary_config)).get_model(name)
        log_event(
            logger,
            logging.INFO,
            "command.hier_query",
            command="hier-query",
            model=name,
            verb=verb,
            arg=arg,
        )
        runner = RtlBuddyViewQuery(
            name=self.name + "/hier-query",
            model_cfg=model_cfg,
            suite_dir=str(ctx.command_root),
            verb=verb,
            arg=arg,
            frontend=frontend,
            subtree_format=fmt,
            context=context,
            line_numbers=line_numbers,
            executable=tool,
        )
        raise typer.Exit(runner.run())

    def _graph_models(
        self,
        ctx: ExecutionContext,
        *,
        model: list[str] | None,
        regression: str | None,
        design_dir: str,
    ) -> list[ModelConfig] | None:
        """Resolve the model selection for `rb graph build`.

        `None` is passed through for `build_graph` to expand to every model under
        `design_dir`.
        """
        if model and regression:
            raise FatalRtlBuddyError(
                "graph build: --model and --regression are mutually exclusive; "
                "--regression already pins the models its suites run"
            )
        if regression:
            return graph_build_mod.models_from_regression(ctx.resolve_input(regression))
        if not model:
            return None

        available = graph_build_mod.models_from_design_tree(design_dir)
        by_name: dict[str, list[ModelConfig]] = {}
        for cfg in available:
            by_name.setdefault(cfg.name, []).append(cfg)
        missing = [name for name in model if name not in by_name]
        if missing:
            known = ", ".join(sorted(by_name)) or "(none)"
            raise FatalRtlBuddyError(
                f"graph build: unknown model(s): {', '.join(missing)}; "
                f"models found under {design_dir}: {known}"
            )
        # Keep every entry that claims a requested name; `build_graph` reports collisions.
        selected: list[ModelConfig] = []
        for name in model:
            for cfg in by_name[name]:
                if cfg not in selected:
                    selected.append(cfg)
        return selected

    def do_graph_build(
        self,
        model: Annotated[
            list[str] | None,
            typer.Option(
                "--model",
                help=(
                    "model name to export in the design tier; repeatable. "
                    "Default: every model declared under --design-dir"
                ),
            ),
        ] = None,
        regression: Annotated[
            str | None,
            typer.Option(
                "-c",
                "--regression",
                help=(
                    "regression.yaml whose suites pin the models to export "
                    "(mutually exclusive with --model)"
                ),
            ),
        ] = None,
        spec_dir: Annotated[
            str | None,
            typer.Option("--spec-dir", help="directory searched for specs.yaml"),
        ] = None,
        verif_dir: Annotated[
            str | None,
            typer.Option("--verif-dir", help="directory searched for tests.yaml"),
        ] = None,
        design_dir: Annotated[
            str | None,
            typer.Option("--design-dir", help="directory searched for models.yaml"),
        ] = None,
        out_dir: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--out-dir",
                help="output directory (default: <project root>/artefacts/graph)",
            ),
        ] = None,
        frontend: Annotated[
            str | None,
            typer.Option("--frontend", help="viewer parser frontend (verible|slang)"),
        ] = None,
        design: Annotated[
            bool,
            typer.Option(
                "--design/--no-design",
                help="run the rtl-buddy-view design tier (default on)",
            ),
        ] = True,
        tb: Annotated[
            bool,
            typer.Option(
                "--tb/--no-tb",
                help=(
                    "also export each testbench's own hierarchy, rooted at "
                    "its toplevel: (default on; --no-tb is DUT-only)"
                ),
            ),
        ] = True,
        flow_tops: Annotated[
            bool,
            typer.Option(
                "--flow-tops/--no-flow-tops",
                help=(
                    "also export each formal/synth/cdc run's top over "
                    "the flow's own filelist when it is not the model "
                    "top (default on)"
                ),
            ),
        ] = True,
        bind: Annotated[
            bool,
            typer.Option(
                "--bind/--no-bind",
                help=(
                    "run the post-merge binding stage that ties cocotb "
                    "tests to the DUT hierarchy (default on)"
                ),
            ),
        ] = True,
        extract: Annotated[
            bool,
            typer.Option(
                "--extract/--no-extract",
                help=(
                    "run the binding tier when the extractor "
                    "(rtl-buddy-graph-extract) is installed"
                ),
            ),
        ] = True,
        extract_cross_check: Annotated[
            bool,
            typer.Option(
                "--extract-cross-check/--no-extract-cross-check",
                help=(
                    "cross-check the internal merge against the extractor's "
                    "`merge-graphs` when it is installed"
                ),
            ),
        ] = True,
        force: Annotated[
            bool,
            typer.Option("--force", help="rebuild even when no input changed"),
        ] = False,
        strict: Annotated[
            bool,
            typer.Option(
                "--strict",
                help="exit non-zero on any per-item failure, not just a dead tier",
            ),
        ] = False,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the rtl-buddy-view binary"),
        ] = "rtl-buddy-view",
    ):
        """
        extract the design, config and (optional) binding tiers and merge
        them into artefacts/graph/graph.json
        """
        root = str(discover_project_root(fallback_cwd=True))
        ctx = self._enter_command_context(command_root=root)
        search_design = (
            str(ctx.resolve_input(design_dir))
            if design_dir is not None
            else os.path.join(root, "design")
        )
        models = self._graph_models(
            ctx, model=model, regression=regression, design_dir=search_design
        )

        # Probe once so the same version string reaches the cache fingerprint.
        view_version = probe_view_version(tool) if design else None
        # An unprobeable extractor still runs; its "unknown" version is fingerprinted.
        extractor = extract_mod.resolve_extractor(self.root_cfg) if extract else None

        log_event(
            logger,
            logging.INFO,
            "command.graph_build",
            command="graph build",
            models=len(models) if models is not None else None,
            design=design,
            tb=tb,
            extractor=extractor is not None,
            force=force,
        )

        build = graph_build_mod.build_graph(
            root,
            models=models,
            spec_dir=str(ctx.resolve_input(spec_dir)) if spec_dir else None,
            verif_dir=str(ctx.resolve_input(verif_dir)) if verif_dir else None,
            design_dir=search_design,
            out_dir=str(ctx.resolve_input(out_dir)) if out_dir else None,
            view_executable=tool,
            view_version=view_version,
            frontend=frontend,
            design=design,
            tb=tb,
            flow_tops=flow_tops,
            bind=bind,
            extract_enabled=extract,
            extract_cross_check=extract_cross_check,
            extract_version=extractor.version if extractor else None,
            extract_executable=(
                extractor.executable if extractor else extract_mod.GRAPH_EXTRACT_BINARY
            ),
            force=force,
        )

        exit_code = 0
        if build.failed_tiers() or (strict and build.has_failures()):
            exit_code = 1

        if self.machine:
            self._emit_machine_result(
                "graph build", exit_code, **build.payload(Path(root))
            )
            raise typer.Exit(exit_code)

        render_summary(
            title="Design Knowledge Graph",
            columns=[
                ("tier", "Tier"),
                ("status", "Status"),
                ("nodes", "Nodes"),
                ("links", "Links"),
                ("detail", "Detail"),
            ],
            rows=[
                {
                    "tier": t.tier,
                    "status": t.status,
                    "nodes": str(t.nodes),
                    "links": str(t.links),
                    "detail": t.row_detail(),
                }
                for t in build.tiers
            ],
            logger=logger,
        )
        verb = "unchanged" if build.unchanged else "wrote"
        emit_console_text(
            f"{verb} {os.path.relpath(build.graph_path, root)}: "
            f"{build.nodes} nodes, {build.links} links "
            f"({build.merge.get('stitch_points', 0)} stitch points)",
            stream="stdout",
        )
        binding = build.binding or {}
        if binding.get("status") == "built":
            emit_console_text(
                f"binding: {binding.get('tests', 0)} cocotb test(s) bound to "
                f"{binding.get('python_modules', 0)} Python module(s), "
                f"{binding.get('drives', 0)} drives edges "
                f"({binding.get('drives_inferred', 0)} inferred), "
                f"{binding.get('checks_against', 0)} golden-model checks",
                stream="stdout",
            )
        dangling = build.merge.get("dangling") or []
        if dangling:
            emit_console_text(
                f"dangling link targets ({len(dangling)}): "
                f"{', '.join(dangling[:5])}"
                f"{' ...' if len(dangling) > 5 else ''}",
                style="yellow",
            )
        raise typer.Exit(exit_code)

    def do_graph_results(
        self,
        verif_dir: Annotated[
            str | None,
            typer.Option("--verif-dir", help="directory searched for tests.yaml"),
        ] = None,
        out_dir: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--out-dir",
                help="output directory (default: <project root>/artefacts/graph)",
            ),
        ] = None,
        graph: Annotated[
            str | None,
            typer.Option(
                "--graph",
                help=(
                    "graph.json to cross-check ids against "
                    "(default: <out-dir>/graph.json); read, never written"
                ),
            ),
        ] = None,
        strict: Annotated[
            bool,
            typer.Option(
                "--strict",
                help=(
                    "exit non-zero when an envelope could not be read, a test "
                    "node has no result, or a result matches no node"
                ),
            ),
        ] = False,
        coverage: Annotated[
            str,
            typer.Option(
                "--coverage",
                # Takes a value: Typer does not forward click's optional-value form.
                help=(
                    "coverage source to join onto the graph's ids: 'auto' "
                    "(cov_dir/manifest.json, then the per-test coverage.dat "
                    "databases this scan finds), 'model' (the manifest "
                    "only), 'none', or a path to a merged LCOV .info file; "
                    "nothing is re-run"
                ),
            ),
        ] = "auto",
        no_coverage: Annotated[
            bool,
            typer.Option(
                "--no-coverage",
                help="skip the coverage join (same as --coverage none)",
            ),
        ] = False,
        cov_dir: Annotated[
            str | None,
            typer.Option(
                "--cov-dir",
                help=(
                    "coverage artefact directory to join from "
                    "(default: the newest cov_dir/ under the project)"
                ),
            ),
        ] = None,
        cov_manifest: Annotated[
            str | None,
            typer.Option(
                "--cov-manifest",
                help="coverage manifest.json to join from, instead of discovery",
            ),
        ] = None,
        run_tag: Annotated[
            str | None,
            typer.Option(
                "--run-tag",
                help="convert one --run-tag run's results: scan "
                "artefacts/.runs/<tag>/ in every suite and write the overlay under "
                "artefacts/.runs/<tag>/graph/ (graph.json is read from artefacts/graph/)",
            ),
        ] = None,
    ):
        """
        refresh the results overlay beside graph.json: last status, run token,
        seed, artefact paths and coverage per test node
        """
        # Before the context is entered, so a tagged run locks its own tree.
        self._run_tag = validate_run_tag(run_tag)
        root = str(discover_project_root(fallback_cwd=True))
        ctx = self._enter_command_context(command_root=root)

        # 'auto' and 'model' pass through, 'none' disables, anything else is an LCOV path.
        cov_source: bool | str = coverage.strip()
        if no_coverage or cov_source == "none":
            cov_source = False
        elif cov_source not in (
            graph_coverage_mod.COVERAGE_SOURCE_AUTO,
            graph_coverage_mod.COVERAGE_SOURCE_MODEL,
        ):
            cov_source = str(ctx.resolve_input(cov_source))

        log_event(
            logger,
            logging.INFO,
            "command.graph_results",
            command="graph results",
            verif_dir=verif_dir,
            strict=strict,
            coverage=cov_source,
            run_tag=self._run_tag,
        )

        overlay = graph_results_mod.refresh_results_overlay(
            root,
            verif_dir=str(ctx.resolve_input(verif_dir)) if verif_dir else None,
            out_dir=str(ctx.resolve_input(out_dir)) if out_dir else None,
            graph_path=str(ctx.resolve_input(graph)) if graph else None,
            coverage=cov_source,
            cov_dir=str(ctx.resolve_input(cov_dir)) if cov_dir else None,
            cov_manifest=str(ctx.resolve_input(cov_manifest)) if cov_manifest else None,
            run_tag=self._run_tag,
        )

        exit_code = 0
        if strict and (overlay.problems or overlay.missing or overlay.unmatched):
            exit_code = 1

        if self.machine:
            self._emit_machine_result(
                "graph results",
                exit_code,
                overlay=os.path.relpath(overlay.path, root),
                graph=overlay.overlay.get("graph"),
                tests=len(overlay.entries),
                with_results=overlay.with_results(),
                statuses=overlay.status_counts(),
                missing=overlay.missing,
                unmatched=overlay.unmatched,
                problems=overlay.problems,
                coverage=overlay.coverage_summary(),
            )
            raise typer.Exit(exit_code)

        render_summary(
            title="Regression Results Overlay",
            columns=[
                ("test", "Test"),
                ("status", "Status"),
                ("run", "Run"),
                ("when", "When"),
                ("line", "Line%"),
                ("artefacts", "Artefacts"),
            ],
            rows=[
                {
                    "test": entry["id"],
                    "status": entry["status"],
                    "run": str(entry.get("run_id"))
                    if entry.get("run_id") is not None
                    else "-",
                    "when": entry.get("timestamp") or "-",
                    "line": _line_ratio_text(entry.get("coverage")),
                    "artefacts": ", ".join(
                        k for k in sorted(entry.get("artefacts", {})) if k != "dir"
                    )
                    or "-",
                }
                for entry in overlay.entries.values()
            ],
            logger=logger,
        )
        counts = ", ".join(f"{k} {v}" for k, v in overlay.status_counts().items())
        emit_console_text(
            f"wrote {os.path.relpath(overlay.path, root)}: "
            f"{len(overlay.entries)} test(s), "
            f"{overlay.with_results()} with a result envelope"
            f"{' (' + counts + ')' if counts else ''}",
            stream="stdout",
        )
        cov_summary = overlay.coverage_summary()
        if cov_summary is not None:
            source = cov_summary.get("source")
            note = f" (from {source})" if source and source != "model" else ""
            emit_console_text(
                f"coverage{note}: "
                f"{cov_summary['tests']} test(s) scored, "
                f"{cov_summary['modules']} module(s), "
                f"{cov_summary[graph_coverage_mod.STATUS_EXERCISED]}/"
                f"{cov_summary['items']} spec item(s) exercised, "
                f"{cov_summary[graph_coverage_mod.STATUS_OBSERVED_UNDECLARED]} "
                "observed but undeclared",
                stream="stdout",
            )
        if overlay.missing:
            emit_console_text(
                f"no results for {len(overlay.missing)} test node(s): "
                f"{', '.join(overlay.missing[:5])}"
                f"{' ...' if len(overlay.missing) > 5 else ''}",
                style="yellow",
            )
        if overlay.problems:
            emit_console_text(
                f"unreadable result envelope(s) ({len(overlay.problems)}): "
                f"{overlay.problems[0]['error']}",
                style="yellow",
            )
        raise typer.Exit(exit_code)

    def _graph_query_context(
        self,
        ctx: ExecutionContext,
        root: str,
        *,
        graph: str | None,
        overlay: str | None,
        results: bool,
    ):
        return graph_query_mod.load_context(
            root,
            graph_path=str(ctx.resolve_input(graph)) if graph else None,
            overlay_path=str(ctx.resolve_input(overlay)) if overlay else None,
            with_results=results,
        )

    def _graph_query_candidates(self, exc: graph_query_mod.GraphQueryError) -> None:
        """Print a failed node reference's near misses before exiting."""
        if not exc.candidates:
            return
        emit_console_text(
            "did you mean: " + ", ".join(exc.candidates[:10]),
            style="yellow",
        )

    def do_graph_query(
        self,
        question: Annotated[
            str,
            typer.Argument(
                help=(
                    "what to look for — an identifier or a plain question, "
                    'e.g. "A-COV-1" or "which tests exercise blk_a"'
                )
            ),
        ],
        node_type: Annotated[
            str | None,
            typer.Option(
                "--type",
                help="restrict to one node type (module, test, coverage_item, ...)",
            ),
        ] = None,
        tier: Annotated[
            str | None,
            typer.Option("--tier", help="restrict to one tier (design|config|binding)"),
        ] = None,
        limit: Annotated[
            int,
            typer.Option("--limit", help="maximum matches to report"),
        ] = graph_query_mod.DEFAULT_LIMIT,
        depth: Annotated[
            int,
            typer.Option(
                "--depth",
                help=(
                    "hops of neighbourhood expansion around each match "
                    f"(0 disables; maximum {graph_query_mod.MAX_DEPTH})"
                ),
            ),
        ] = graph_query_mod.DEFAULT_DEPTH,
        max_neighbors: Annotated[
            int,
            typer.Option(
                "--max-neighbors",
                help=(
                    "neighbours reported per match; anything beyond is "
                    "counted in neighbors_truncated rather than dropped silently"
                ),
            ),
        ] = graph_query_mod.DEFAULT_MAX_NEIGHBORS,
        results: Annotated[
            bool,
            typer.Option(
                "--results/--no-results",
                help="join the regression-results overlay onto every node",
            ),
        ] = True,
        expand: Annotated[
            bool,
            typer.Option(
                "--expand",
                help=(
                    "full node summaries for every neighbour instead of the "
                    "lean id/label/type references"
                ),
            ),
        ] = False,
        graph: Annotated[
            str | None,
            typer.Option(
                "--graph",
                help="graph.json to query (default <project root>/artefacts/graph)",
            ),
        ] = None,
        overlay: Annotated[
            str | None,
            typer.Option(
                "--overlay",
                help="results-overlay.json to join (default: beside graph.json)",
            ),
        ] = None,
    ):
        """
        search the design knowledge graph by keyword and expand the
        neighbourhood around every match, with the results overlay joined in
        """
        root = str(discover_project_root(fallback_cwd=True))
        # list_only keeps read verbs lock-free, so they work while a regression holds the lock.
        ctx = self._enter_command_context(command_root=root, list_only=True)
        log_event(
            logger,
            logging.INFO,
            "command.graph_query",
            command="graph query",
            question=question,
        )
        gctx = self._graph_query_context(
            ctx, root, graph=graph, overlay=overlay, results=results
        )
        payload = graph_query_mod.query(
            gctx,
            question,
            node_type=node_type,
            tier=tier,
            limit=limit,
            depth=depth,
            max_neighbors=max_neighbors,
            results=results,
            expand=expand,
        )
        # Exit 1 (not a crash) when nothing matched, so shell loops can branch.
        exit_code = 0 if payload["matches"] else 1

        if self.machine:
            self._emit_machine_result("graph query", exit_code, **payload)
            raise typer.Exit(exit_code)

        if not payload["matches"]:
            emit_console_text(
                f"no node matches {question!r} in "
                f"{payload['graph']} ({payload['counts']['nodes']} nodes)",
                style="yellow",
            )
            raise typer.Exit(exit_code)

        render_summary(
            title=f"Graph Query — {question}",
            columns=[
                ("id", "Node"),
                ("type", "Type"),
                ("score", "Score"),
                ("status", "Status"),
                ("where", "Where"),
            ],
            rows=[
                {
                    "id": match["id"],
                    "type": match.get("type", "-"),
                    "score": str(match.get("score", 0)),
                    "status": (match.get("results") or {}).get("status", "-"),
                    "where": _graph_where(match),
                }
                for match in payload["matches"]
            ],
            logger=logger,
        )
        for match in payload["matches"]:
            neighbors = match.get("neighbors") or []
            if not neighbors:
                continue
            emit_console_text(
                f"\n{match['id']}", style="bold", stream="stdout", markup=False
            )
            for neighbor in neighbors:
                via = neighbor.get("via", {})
                arrow = "->" if via.get("direction") == "out" else "<-"
                status = (neighbor.get("results") or {}).get("status")
                emit_console_text(
                    f"  {arrow} {via.get('type', '?')} {neighbor['id']}"
                    f"{f' ({status})' if status else ''}",
                    stream="stdout",
                    markup=False,
                )
            truncated = match.get("neighbors_truncated")
            if truncated:
                kinds = ", ".join(
                    f"{count} {kind}" for kind, count in truncated["kinds"].items()
                )
                emit_console_text(
                    f"  ... {truncated['dropped']} more neighbour(s) beyond "
                    f"--max-neighbors: {kinds}",
                    style="yellow",
                    markup=False,
                )
        raise typer.Exit(exit_code)

    def do_graph_path(
        self,
        source: Annotated[
            str, typer.Argument(help="start node id, or a bare unambiguous name")
        ],
        target: Annotated[
            str, typer.Argument(help="end node id, or a bare unambiguous name")
        ],
        directed: Annotated[
            bool,
            typer.Option(
                "--directed/--undirected",
                help=(
                    "follow edge direction; undirected by default because "
                    "edge direction encodes role, not reachability"
                ),
            ),
        ] = False,
        max_paths: Annotated[
            int,
            typer.Option("--max-paths", help="shortest paths to report"),
        ] = graph_query_mod.DEFAULT_MAX_PATHS,
        results: Annotated[
            bool,
            typer.Option(
                "--results/--no-results",
                help="join the regression-results overlay onto every node",
            ),
        ] = True,
        graph: Annotated[
            str | None,
            typer.Option("--graph", help="graph.json to query"),
        ] = None,
        overlay: Annotated[
            str | None,
            typer.Option("--overlay", help="results-overlay.json to join"),
        ] = None,
    ):
        """
        report the shortest chain of edges connecting two graph nodes
        """
        root = str(discover_project_root(fallback_cwd=True))
        ctx = self._enter_command_context(command_root=root, list_only=True)
        log_event(
            logger,
            logging.INFO,
            "command.graph_path",
            command="graph path",
            source=source,
            target=target,
        )
        gctx = self._graph_query_context(
            ctx, root, graph=graph, overlay=overlay, results=results
        )
        try:
            payload = graph_query_mod.path(
                gctx,
                source,
                target,
                directed=directed,
                max_paths=max_paths,
                results=results,
            )
        except graph_query_mod.GraphQueryError as exc:
            if self.machine:
                self._emit_machine_result(
                    "graph path", 2, error=str(exc), candidates=exc.candidates
                )
                raise typer.Exit(2)
            self._graph_query_candidates(exc)
            raise

        exit_code = 0 if payload["found"] else 1
        if self.machine:
            self._emit_machine_result("graph path", exit_code, **payload)
            raise typer.Exit(exit_code)

        if not payload["found"]:
            emit_console_text(
                f"no path between {payload['source']['id']} and "
                f"{payload['target']['id']}"
                f"{' (try --undirected)' if directed else ''}",
                style="yellow",
            )
            raise typer.Exit(exit_code)

        for walk in payload["paths"]:
            emit_console_text(
                f"{walk['length']} hop(s):", style="bold", stream="stdout"
            )
            nodes = walk["nodes"]
            emit_console_text(f"  {nodes[0]['id']}", stream="stdout", markup=False)
            for step, node in zip(walk["edges"], nodes[1:]):
                types = ", ".join(
                    sorted({str(link.get("type")) for link in step["links"]})
                )
                emit_console_text(
                    f"    --{types}--> {node['id']}", stream="stdout", markup=False
                )
        raise typer.Exit(exit_code)

    def do_graph_explain(
        self,
        node: Annotated[
            str, typer.Argument(help="node id, or a bare unambiguous name")
        ],
        results: Annotated[
            bool,
            typer.Option(
                "--results/--no-results",
                help="join the regression-results overlay onto every node",
            ),
        ] = True,
        expand: Annotated[
            bool,
            typer.Option(
                "--expand",
                help=(
                    "full node summaries for every edge peer instead of the "
                    "lean id/label/type references"
                ),
            ),
        ] = False,
        graph: Annotated[
            str | None,
            typer.Option("--graph", help="graph.json to query"),
        ] = None,
        overlay: Annotated[
            str | None,
            typer.Option("--overlay", help="results-overlay.json to join"),
        ] = None,
    ):
        """
        report one node's attributes, every edge on it with the far endpoint
        named (--expand for full peer summaries), its last regression result
        and its coverage
        """
        root = str(discover_project_root(fallback_cwd=True))
        ctx = self._enter_command_context(command_root=root, list_only=True)
        log_event(
            logger,
            logging.INFO,
            "command.graph_explain",
            command="graph explain",
            node=node,
        )
        gctx = self._graph_query_context(
            ctx, root, graph=graph, overlay=overlay, results=results
        )
        try:
            payload = graph_query_mod.explain(
                gctx, node, results=results, expand=expand
            )
        except graph_query_mod.GraphQueryError as exc:
            if self.machine:
                self._emit_machine_result(
                    "graph explain", 2, error=str(exc), candidates=exc.candidates
                )
                raise typer.Exit(2)
            self._graph_query_candidates(exc)
            raise

        if self.machine:
            self._emit_machine_result("graph explain", 0, **payload)
            raise typer.Exit(0)

        summary = payload["node"]
        emit_console_text(
            f"{summary['id']}  ({summary.get('type', '?')}, "
            f"tier {summary.get('tier', '?')})",
            style="bold",
            stream="stdout",
        )
        where = _graph_where(summary)
        if where != "-":
            emit_console_text(f"  source: {where}", stream="stdout")
        cite = summary.get("cite") or {}
        if cite.get("command"):
            emit_console_text(f"  cite:   {cite['command']}", stream="stdout")
        entry = payload.get("results")
        if entry:
            emit_console_text(
                f"  result: {entry.get('status')} "
                f"({entry.get('timestamp', 'unknown time')})",
                stream="stdout",
            )
        for line in _explain_coverage_lines(
            payload.get("coverage"), payload.get("coverage_run")
        ):
            # markup=False: `[affix]` and bracketed SVA labels would parse as Rich tags.
            emit_console_text(line, stream="stdout", markup=False)
        rows = [
            {
                "dir": direction,
                "type": str(edge.get("type")),
                "peer": edge["peer"],
                "peer_type": edge.get("peer_type", "-"),
                "confidence": str(edge.get("confidence", "-")),
            }
            for direction, bucket in (
                ("out", payload["outgoing"]),
                ("in", payload["incoming"]),
            )
            for edge in bucket
        ]
        if rows:
            render_summary(
                title=f"Edges — {summary['id']}",
                columns=[
                    ("dir", "Dir"),
                    ("type", "Edge"),
                    ("peer", "Peer"),
                    ("peer_type", "Peer Type"),
                    ("confidence", "Confidence"),
                ],
                rows=rows,
                logger=logger,
            )
        else:
            emit_console_text("  (no edges)", stream="stdout")
        truncated = payload.get("truncated")
        if truncated:
            kinds = ", ".join(
                f"{count} {kind}" for kind, count in truncated["kinds"].items()
            )
            buckets = truncated.get("buckets") or {}
            where = (
                " (" + ", ".join(f"{name} {n}" for name, n in buckets.items()) + ")"
                if buckets
                else ""
            )
            emit_console_text(
                f"  ... {truncated['dropped']} more edge(s) not shown{where}: {kinds}",
                style="yellow",
                stream="stdout",
                markup=False,
            )
        raise typer.Exit(0)

    def _cov_context(self, verb, *, cov_dir=None, manifest=None):
        """Load the coverage manifest and model for a `rb cov` verb.

        Lock-free, so it works while a regression holds the artefact lock.
        """
        root = str(discover_project_root(fallback_cwd=True))
        ctx = self._enter_command_context(command_root=root, list_only=True)
        log_event(
            logger,
            logging.INFO,
            f"command.{verb.replace(' ', '_')}",
            command=verb,
        )
        try:
            return cov_query_mod.load_context(
                root,
                cov_dir=str(ctx.resolve_input(cov_dir)) if cov_dir else None,
                manifest=str(ctx.resolve_input(manifest)) if manifest else None,
            )
        except cov_query_mod.CovQueryError as exc:
            self._read_query_failed(verb, exc)

    def _read_query_failed(self, command: str, exc):
        """Report a read verb's unanswerable question, then exit 2.

        Shared by `rb cov` and `rb phys` so both list near-miss candidates alike.
        """
        if self.machine:
            self._emit_machine_result(
                command, 2, error=str(exc), candidates=exc.candidates
            )
            raise typer.Exit(2)
        if exc.candidates:
            emit_console_text(str(exc), style="red", markup=False)
            emit_console_text(
                "did you mean: " + ", ".join(exc.candidates[:10]), style="yellow"
            )
            raise typer.Exit(2)
        raise exc

    @staticmethod
    def _cov_pct(entry):
        """Format one `{found, hit, ratio}` total as `hit/found (NN%)`."""
        if not entry or not entry.get("found"):
            return "-"
        return f"{entry['hit']}/{entry['found']} ({entry['ratio'] * 100:.0f}%)"

    def _cov_totals_columns(self):
        return [("scope", "Scope")] + [
            (metric, metric.capitalize()) for metric in cov_metrics
        ]

    def _cov_totals_row(self, scope, totals):
        row = {"scope": scope}
        for metric in cov_metrics:
            row[metric] = self._cov_pct((totals or {}).get(metric))
        return row

    def do_cov_summary(
        self,
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                help="files to report, coldest first (0 for all)",
            ),
        ] = cov_query_mod.DEFAULT_FILE_LIMIT,
        cov_dir: Annotated[
            str | None,
            typer.Option(
                "--cov-dir",
                help="coverage artefact directory to read",
                show_default="newest cov_dir under the project root",
            ),
        ] = None,
        manifest: Annotated[
            str | None,
            typer.Option("--manifest", help="manifest.json to read directly"),
        ] = None,
        by_source: Annotated[
            bool,
            typer.Option(
                "--by-source",
                help="report the coldest files per source point (covered when any elaboration hit it) instead of per elaboration; same files, same order",
            ),
        ] = False,
    ):
        """
        report a run's coverage from its artefacts: run-level and per-test
        scalars, the coldest files, and where every artefact landed
        """
        ctx = self._cov_context("cov summary", cov_dir=cov_dir, manifest=manifest)
        payload = cov_query_mod.summary_payload(ctx, limit=limit)

        if self.machine:
            self._emit_machine_result("cov summary", 0, **payload)
            raise typer.Exit(0)

        emit_console_text(
            f"{payload['run_command']} {payload.get('suite') or ''} — "
            f"{payload['counts']['tests']} test(s), "
            f"{payload['counts']['files']} file(s), "
            f"generated {payload.get('generated_at') or 'unknown'}",
            style="bold",
            stream="stdout",
            markup=False,
        )
        run_rows = [self._cov_totals_row("run", payload["totals"])]
        if payload.get("source_totals"):
            run_rows.append(
                self._cov_totals_row("run (source)", payload["source_totals"])
            )
        elif by_source:
            emit_console_text(
                "cov: this run's model carries no source-point figures; "
                "re-run the coverage command to write them",
                style="yellow",
                stream="stdout",
                markup=False,
            )
        render_summary(
            title="Coverage — Totals",
            columns=self._cov_totals_columns(),
            rows=run_rows
            + [
                self._cov_totals_row(row["name"], row["totals"])
                for row in payload["tests"]
            ],
            logger=logger,
        )
        if payload["files"]:
            totals_key = "source_totals" if by_source else "totals"
            render_summary(
                title=(
                    "Coverage — Coldest Files (source points)"
                    if by_source
                    else "Coverage — Coldest Files"
                ),
                columns=self._cov_totals_columns(),
                rows=[
                    self._cov_totals_row(row["path"], row.get(totals_key))
                    for row in payload["files"]
                ],
                logger=logger,
            )
        artefacts = payload["artefacts"]
        emit_console_text(f"\nmanifest: {artefacts['manifest']}", stream="stdout")
        emit_console_text(f"model:    {artefacts['model']}", stream="stdout")
        for label, key in (
            ("merged:  ", "merged_info"),
            ("html:    ", "html_dir"),
            ("coverview", "coverview_zip"),
        ):
            if artefacts.get(key):
                emit_console_text(f"{label} {artefacts[key]}", stream="stdout")
        raise typer.Exit(0)

    def do_cov_module(
        self,
        module: Annotated[
            str, typer.Argument(help="module name as the coverage model records it")
        ],
        cold: Annotated[
            bool,
            typer.Option(
                "--cold/--all",
                help="list only the points with no hits",
            ),
        ] = True,
        limit: Annotated[
            int,
            typer.Option("--limit", help="points to list per metric (0 for all)"),
        ] = 20,
        cov_dir: Annotated[
            str | None,
            typer.Option(
                "--cov-dir",
                help="coverage artefact directory to read",
                show_default="newest cov_dir under the project root",
            ),
        ] = None,
        manifest: Annotated[
            str | None,
            typer.Option("--manifest", help="manifest.json to read directly"),
        ] = None,
    ):
        """
        report per-file, per-point coverage for one module's sources, with the
        tests behind every point
        """
        ctx = self._cov_context("cov module", cov_dir=cov_dir, manifest=manifest)
        try:
            payload = cov_query_mod.module_payload(ctx, module)
        except cov_query_mod.CovQueryError as exc:
            self._read_query_failed("cov module", exc)

        if self.machine:
            self._emit_machine_result("cov module", 0, **payload)
            raise typer.Exit(0)

        render_summary(
            title=f"Coverage — {payload['module']}",
            columns=self._cov_totals_columns(),
            rows=[self._cov_totals_row("module", payload["totals"])]
            + [
                self._cov_totals_row(row["path"], row["totals"])
                for row in payload["files"]
            ],
            logger=logger,
        )
        for file_row in payload["files"]:
            for metric in cov_metrics:
                points = [
                    point
                    for point in file_row[metric]
                    if not cold or point.get("hits", 0) == 0
                ]
                if not points:
                    continue
                shown = points if limit <= 0 else points[:limit]
                emit_console_text(
                    f"\n{file_row['path']} — {metric}"
                    f"{' (uncovered)' if cold else ''}"
                    f" {len(shown)}/{len(points)}",
                    style="bold",
                    stream="stdout",
                    markup=False,
                )
                for point in shown:
                    name = point.get("name")
                    emit_console_text(
                        f"  line {point.get('line')}"
                        f"{f' {name}' if name else ''}"
                        f"  hits={point.get('hits', 0)}",
                        stream="stdout",
                        markup=False,
                    )
        raise typer.Exit(0)

    def _phys_root(self, verb):
        """The project root a `rb phys` verb reads under, event logged.

        Lock-free, like the other read verbs. Separate from `_phys_context` because
        `rb phys runs` needs no manifest or model.
        """
        root = str(discover_project_root(fallback_cwd=True))
        ctx = self._enter_command_context(command_root=root, list_only=True)
        log_event(
            logger,
            logging.INFO,
            f"command.{verb.replace(' ', '_')}",
            command=verb,
        )
        return root, ctx

    def _phys_context(self, verb, *, phys_dir=None, manifest=None):
        """Load the physical manifest and model for a `rb phys` verb."""
        root, ctx = self._phys_root(verb)
        try:
            return phys_query_mod.load_context(
                root,
                phys_dir=str(ctx.resolve_input(phys_dir)) if phys_dir else None,
                manifest=str(ctx.resolve_input(manifest)) if manifest else None,
            )
        except phys_query_mod.PhysQueryError as exc:
            self._read_query_failed(verb, exc)

    @staticmethod
    def _phys_rank_limit(flag: str, value: str | None):
        """A `--modules-limit`/`--instances-limit` value, or a usage error naming the flag.

        `phys.query.parse_rank_limit` owns the parsing.
        """
        try:
            return phys_query_mod.parse_rank_limit(value)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint=flag) from None

    @staticmethod
    def _phys_num(value, digits: int = 3):
        """Format one model number, or `-` when it was not measured.

        `null` prints as `-`, never `0`, which would read as a measurement.
        """
        if value is None:
            return "-"
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value:,}"
        try:
            return f"{float(value):,.{digits}f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _phys_missing_half_notes(payload) -> None:
        """Say which half of the model is absent and what would produce it.

        The merge is gated on the netlist hash both halves record. A half whose
        producer recorded none (a `netlist-source: pnr` power run) cannot be merged
        onto, and running the other command would replace it. The note then advises
        synthesising and measuring that netlist.
        """
        halves = payload.get("halves") or {}
        for half in payload.get("missing_halves") or []:
            entry = halves.get(half) or {}
            produced_by = entry.get("produced_by")
            noun = "module" if half == "modules" else "instance"
            other = "instances" if half == "modules" else "modules"
            present = halves.get(other) or {}
            if present.get("present") and not present.get("netlist_hash"):
                note = (
                    f"no per-{noun} rows in this model - `{produced_by}` here "
                    f"would replace it rather than complete it: the "
                    f"{'power' if other == 'instances' else 'synthesis'} half "
                    f"records no netlist hash to pair on. Run `rb synth`, then "
                    f"re-run `rb power` on the netlist it writes, so both "
                    f"halves measure the same one"
                )
            else:
                note = (
                    f"no per-{noun} rows in this model - run `{produced_by}` "
                    f"into the same artefact directory to add them"
                )
            emit_console_text(
                note,
                style="yellow",
                stream="stdout",
                markup=False,
            )

    @staticmethod
    def _phys_instance_join_note(payload) -> None:
        """Say when an empty instance list is a namespace miss, not a fact.

        Synthesis rows name RTL modules and leaves name Liberty cells, so an RTL module
        on a mapped hierarchical design matches nothing. The sentence comes from the
        payload so every surface says the same thing.
        """
        note = payload.get("instance_join")
        if note:
            emit_console_text(
                f"\n{note}",
                style="yellow",
                stream="stdout",
                markup=False,
            )

    def _phys_instance_rows(self, rows):
        """Instance rows as summary-table rows, powers already formatted."""
        return [
            {
                "instance": row.get("instance_path"),
                "module": row.get("module") or "-",
                **{
                    column: self._phys_num(row.get(column))
                    for column in phys_query_mod.POWER_COLUMNS
                },
            }
            for row in rows
        ]

    _PHYS_INSTANCE_COLUMNS = [
        ("instance", "Instance"),
        ("module", "Module"),
        ("total_uw", "Total uW"),
        ("internal_uw", "Internal uW"),
        ("switching_uw", "Switching uW"),
        ("leakage_uw", "Leakage uW"),
    ]

    def _phys_artefact_lines(self, artefacts) -> None:
        """Print the artefact paths, skipping the ones this run lacks."""
        emit_console_text(f"\nmanifest: {artefacts['manifest']}", stream="stdout")
        emit_console_text(f"model:    {artefacts['model']}", stream="stdout")
        for label, key in (
            ("netlist: ", "synth_netlist"),
            ("stats:   ", "synth_stats"),
            ("power:   ", "power_report"),
            ("insts:   ", "power_instances"),
        ):
            if artefacts.get(key):
                emit_console_text(f"{label} {artefacts[key]}", stream="stdout")

    @staticmethod
    def _phys_backends(backends) -> str:
        """The two backend names as one cell: `yosys+openroad`.

        A half that did not run is left out.
        """
        names = [
            backends.get(half) for half in ("synth", "power") if backends.get(half)
        ]
        return "+".join(names) if names else "-"

    @staticmethod
    def _phys_power_cell(entry) -> str:
        """The mode and what drove it: `dynamic (saif csr_smoke)`.

        The label comes from the payload so every surface agrees. A run with no power
        half prints `-`.
        """
        mode = entry.get("mode")
        label = (entry.get("activity") or {}).get("label")
        if mode and label:
            return f"{mode} ({label})"
        return mode or label or "-"

    def do_phys_runs(
        self,
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                min=0,
                help=(
                    "runs to list, newest first (0 for all); "
                    "truncates the --machine payload too"
                ),
            ),
        ] = phys_query_mod.DEFAULT_RUNS_LIMIT,
    ):
        """
        list every run with physical artefacts under the project, newest first,
        with the top, backends, power mode and configuration each one recorded
        """
        root, _ctx = self._phys_root("phys runs")
        payload = phys_query_mod.runs_payload(root, limit=limit)

        if self.machine:
            self._emit_machine_result("phys runs", 0, **payload)
            raise typer.Exit(0)

        runs = payload["runs"]
        if not runs:
            emit_console_text(
                f"no {phys_manifest_mod.MANIFEST_FILENAME} under {root} - "
                "run `rb synth` or `rb power` first",
                style="yellow",
                stream="stdout",
                markup=False,
            )
            raise typer.Exit(0)

        metadata = [
            "* the newest run - what `rb phys summary` reads without --phys-dir"
        ]
        if len(runs) < payload["count"]:
            metadata.append(
                f"{len(runs)}/{payload['count']} runs shown; --limit 0 for all"
            )
        render_summary(
            title="Physical - Runs",
            columns=[
                ("run", "Run"),
                ("top", "Top"),
                ("backends", "Backends"),
                ("power", "Power"),
                ("config", "Config"),
                ("xplr", "Experiment"),
                ("generated", "Generated"),
            ],
            rows=[
                {
                    "run": ("* " if entry["newest"] else "") + (entry["run"] or "-"),
                    "top": entry["top"] or "-",
                    "backends": self._phys_backends(entry["backends"]),
                    "power": self._phys_power_cell(entry),
                    "config": entry["fingerprint"] or "-",
                    "xplr": (entry["xplr"] or {}).get("id") or "-",
                    "generated": entry["generated_at"] or "-",
                }
                for entry in runs
            ],
            metadata=metadata,
            logger=logger,
        )
        # Directories go below the table: a wrapped path cannot be copied.
        emit_console_text("\nphys dirs:", stream="stdout", markup=False)
        width = max(len(entry["run"] or "-") for entry in runs)
        for entry in runs:
            marker = "*" if entry["newest"] else " "
            emit_console_text(
                f" {marker} {(entry['run'] or '-'):<{width}}  {entry['phys_dir']}",
                stream="stdout",
                markup=False,
            )
            if entry["error"]:
                emit_console_text(
                    f"    {entry['error']}",
                    style="yellow",
                    stream="stdout",
                    markup=False,
                )
        emit_console_text(
            "\nread one with `rb phys summary --phys-dir <phys dir>`",
            stream="stdout",
            markup=False,
        )
        raise typer.Exit(0)

    def do_phys_summary(
        self,
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                min=0,
                help=(
                    "rows per ranking, heaviest/hottest first "
                    "(0 for all); truncates the --machine payload too"
                ),
            ),
        ] = phys_query_mod.DEFAULT_RANK_LIMIT,
        modules_limit: Annotated[
            str | None,
            typer.Option(
                "--modules-limit",
                metavar="N|none",
                help=(
                    "rows in the modules ranking, overriding --limit "
                    "(0 for all, 'none' for no rows)"
                ),
                show_default="--limit",
            ),
        ] = None,
        instances_limit: Annotated[
            str | None,
            typer.Option(
                "--instances-limit",
                metavar="N|none",
                help=(
                    "rows in the instances ranking, overriding --limit "
                    "(0 for all, 'none' for no rows)"
                ),
                show_default="--limit",
            ),
        ] = None,
        phys_dir: Annotated[
            str | None,
            typer.Option(
                "--phys-dir",
                help="artefact directory holding phys-manifest.json",
                show_default="newest phys-manifest.json under the project root",
            ),
        ] = None,
        manifest: Annotated[
            str | None,
            typer.Option("--manifest", help="phys-manifest.json to read directly"),
        ] = None,
    ):
        """
        report a run's physical metrics from its artefacts: the design totals,
        the heaviest modules, the hottest instances, and where everything landed
        """
        # Validate flags before walking the project for a manifest.
        modules_rows = self._phys_rank_limit("--modules-limit", modules_limit)
        instances_rows = self._phys_rank_limit("--instances-limit", instances_limit)
        ctx = self._phys_context("phys summary", phys_dir=phys_dir, manifest=manifest)
        payload = phys_query_mod.summary_payload(
            ctx,
            limit=limit,
            modules_limit=modules_rows,
            instances_limit=instances_rows,
        )

        if self.machine:
            self._emit_machine_result("phys summary", 0, **payload)
            raise typer.Exit(0)

        backends = payload["backends"]
        emit_console_text(
            f"{payload['run_command']} {payload.get('run') or ''} - "
            f"top {payload.get('top') or 'unknown'}, "
            f"synth {backends.get('synth') or 'none'}, "
            f"power {backends.get('power') or 'none'}, "
            f"generated {payload.get('generated_at') or 'unknown'}",
            style="bold",
            stream="stdout",
            markup=False,
        )
        render_summary(
            title="Physical - Totals",
            columns=[("metric", "Metric"), ("value", "Value")],
            rows=[
                {"metric": key, "value": self._phys_num(value)}
                for key, value in (payload["totals"] or {}).items()
            ],
            logger=logger,
        )
        self._phys_missing_half_notes(payload)
        if payload["modules"]:
            render_summary(
                title="Physical - Heaviest Modules",
                columns=[
                    ("module", "Module"),
                    ("cell_count", "Cells"),
                    ("area_um2", "Area um2"),
                ],
                rows=[
                    {
                        "module": row.get("module"),
                        "cell_count": self._phys_num(row.get("cell_count")),
                        "area_um2": self._phys_num(row.get("area_um2")),
                    }
                    for row in payload["modules"]
                ],
                logger=logger,
            )
        if payload["instances"]:
            render_summary(
                title="Physical - Hottest Instances",
                columns=self._PHYS_INSTANCE_COLUMNS,
                rows=self._phys_instance_rows(payload["instances"]),
                logger=logger,
            )
        self._phys_artefact_lines(payload["artefacts"])
        raise typer.Exit(0)

    def do_phys_module(
        self,
        module: Annotated[
            str,
            typer.Argument(help="module or liberty cell as the model records it"),
        ],
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                min=0,
                help=(
                    "instances to list, hottest first "
                    "(0 for all); truncates the --machine payload too"
                ),
            ),
        ] = phys_query_mod.DEFAULT_RANK_LIMIT,
        phys_dir: Annotated[
            str | None,
            typer.Option(
                "--phys-dir",
                help="artefact directory holding phys-manifest.json",
                show_default="newest phys-manifest.json under the project root",
            ),
        ] = None,
        manifest: Annotated[
            str | None,
            typer.Option("--manifest", help="phys-manifest.json to read directly"),
        ] = None,
    ):
        """
        report one module's cells and area, and the instances of it with the
        power each one burns
        """
        ctx = self._phys_context("phys module", phys_dir=phys_dir, manifest=manifest)
        try:
            payload = phys_query_mod.module_payload(ctx, module, limit=limit)
        except phys_query_mod.PhysQueryError as exc:
            self._read_query_failed("phys module", exc)

        if self.machine:
            self._emit_machine_result("phys module", 0, **payload)
            raise typer.Exit(0)

        row = payload["row"] or {}
        render_summary(
            title=f"Physical - {payload['module']}",
            columns=[
                ("module", "Module"),
                ("cell_count", "Cells"),
                ("area_um2", "Area um2"),
                ("instances", "Instances"),
            ],
            rows=[
                {
                    "module": payload["module"],
                    "cell_count": self._phys_num(row.get("cell_count")),
                    "area_um2": self._phys_num(row.get("area_um2")),
                    "instances": self._phys_num(payload["instance_count"]),
                }
            ],
            logger=logger,
        )
        self._phys_missing_half_notes(payload)
        self._phys_instance_join_note(payload)
        # The payload is already limited; do not truncate again.
        shown = payload["instances"] or []
        if shown:
            emit_console_text(
                f"\ninstances of {payload['module']}: "
                f"{len(shown)}/{payload['instance_count']}",
                style="bold",
                stream="stdout",
                markup=False,
            )
            render_summary(
                title=f"Physical - Instances of {payload['module']}",
                columns=self._PHYS_INSTANCE_COLUMNS,
                rows=self._phys_instance_rows(shown)
                + [
                    {
                        "instance": "total",
                        "module": payload["module"],
                        **{
                            column: self._phys_num(payload["power"][column])
                            for column in phys_query_mod.POWER_COLUMNS
                        },
                    }
                ],
                logger=logger,
            )
        self._phys_artefact_lines(payload["artefacts"])
        raise typer.Exit(0)

    def do_phys_instance(
        self,
        path: Annotated[
            str,
            typer.Argument(help="instance path, exact or the root of a subtree"),
        ],
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                min=0,
                help=(
                    "hottest children to list (0 for all); "
                    "truncates the --machine payload too"
                ),
            ),
        ] = phys_query_mod.DEFAULT_RANK_LIMIT,
        phys_dir: Annotated[
            str | None,
            typer.Option(
                "--phys-dir",
                help="artefact directory holding phys-manifest.json",
                show_default="newest phys-manifest.json under the project root",
            ),
        ] = None,
        manifest: Annotated[
            str | None,
            typer.Option("--manifest", help="phys-manifest.json to read directly"),
        ] = None,
    ):
        """
        report one instance's power, or - when the path names a subtree rather
        than a leaf - the leaves under it and their rolled-up total
        """
        ctx = self._phys_context("phys instance", phys_dir=phys_dir, manifest=manifest)
        try:
            payload = phys_query_mod.instance_payload(ctx, path, limit=limit)
        except phys_query_mod.PhysQueryError as exc:
            self._read_query_failed("phys instance", exc)

        if self.machine:
            self._emit_machine_result("phys instance", 0, **payload)
            raise typer.Exit(0)

        rollup = payload["rollup"]
        emit_console_text(
            f"{payload['instance_path']} - {payload['match']} match, "
            f"{rollup['instances']} leaf instance(s)",
            style="bold",
            stream="stdout",
            markup=False,
        )
        rows = []
        if payload["instance"] is not None:
            rows += self._phys_instance_rows([payload["instance"]])
        children = payload["children"]
        if children:
            rows += self._phys_instance_rows(children)
        rows.append(
            {
                "instance": f"rollup ({rollup['instances']})",
                "module": "-",
                **{
                    column: self._phys_num(rollup[column])
                    for column in phys_query_mod.POWER_COLUMNS
                },
            }
        )
        render_summary(
            title=f"Physical - {payload['instance_path']}",
            columns=self._PHYS_INSTANCE_COLUMNS,
            rows=rows,
            logger=logger,
        )
        if children and len(children) < payload["child_count"]:
            emit_console_text(
                f"{len(children)}/{payload['child_count']} children shown; "
                "--limit 0 for all",
                stream="stdout",
                markup=False,
            )
        if payload["match"] == "exact" and payload["child_count"]:
            # The rollup is the named row alone; say so or the table reads as its total.
            emit_console_text(
                f"{payload['child_count']} row(s) below this path are listed "
                "for navigation; the rollup is the named row alone",
                stream="stdout",
                markup=False,
            )
        self._phys_missing_half_notes(payload)
        self._phys_artefact_lines(payload["artefacts"])
        raise typer.Exit(0)

    def do_cmd_mcp(
        self,
        graph: Annotated[
            str | None,
            typer.Option(
                "--graph",
                help="graph.json to serve (default <project root>/artefacts/graph)",
            ),
        ] = None,
        overlay: Annotated[
            str | None,
            typer.Option("--overlay", help="results-overlay.json to join"),
        ] = None,
        root_dir: Annotated[
            str | None,
            typer.Option(
                "--root",
                help=("project root to serve (default: discovered from cwd)"),
            ),
        ] = None,
        design_dir: Annotated[
            str | None,
            typer.Option("--design-dir", help="directory searched for models.yaml"),
        ] = None,
        frontend: Annotated[
            str | None,
            typer.Option("--frontend", help="viewer parser frontend (verible|slang)"),
        ] = None,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the rtl-buddy-view binary"),
        ] = "rtl-buddy-view",
        list_tools: Annotated[
            bool,
            typer.Option(
                "--list-tools",
                help="print the tool schemas and exit instead of serving",
            ),
        ] = False,
    ):
        """
        serve the design knowledge graph, test status, coverage, physical
        metrics and the hierarchy query verbs — and, when a hub is running,
        the live session — over the Model Context Protocol
        """
        root = str(
            Path(root_dir).resolve()
            if root_dir
            else discover_project_root(fallback_cwd=True)
        )
        toolset = mcp_toolset_mod.build_toolset(
            root,
            graph_path=graph,
            overlay_path=overlay,
            view_executable=tool,
            design_dir=design_dir,
            frontend=frontend,
        )

        if list_tools:
            payload = {
                "project_root": root,
                "hub": toolset.hub.payload(),
                "sdk": {
                    "available": mcp_server_mod.sdk_available(),
                    "version": mcp_server_mod.sdk_version(),
                },
                "tools": [spec.to_mcp_dict() for spec in toolset.specs()],
            }
            if self.machine:
                self._emit_machine_result("mcp --list-tools", 0, **payload)
                raise typer.Exit(0)
            render_summary(
                title="MCP Tools",
                columns=[("name", "Tool"), ("title", "Title"), ("cmd", "CLI Mirror")],
                rows=[
                    {
                        "name": spec.name,
                        "title": spec.title,
                        "cmd": spec.command or "-",
                    }
                    for spec in toolset.specs()
                ],
                logger=logger,
            )
            emit_console_text(
                f"hub: {'connected' if toolset.hub.present else 'not running'}"
                f"{' (' + toolset.hub.reason + ')' if toolset.hub.reason else ''}",
                stream="stdout",
            )
            raise typer.Exit(0)

        # stdout carries the JSON-RPC stream; write to stderr or the log only.
        log_event(
            logger,
            logging.INFO,
            "command.mcp",
            command="mcp",
            project_root=root,
            tools=len(toolset.specs()),
            hub=toolset.hub.present,
        )
        raise typer.Exit(mcp_server_mod.serve_stdio(toolset))

    def do_cmd_axi_profile_discover(
        self,
        model_name: Annotated[str, typer.Argument(help="model from models.yaml")],
        model_config: Annotated[
            str, typer.Option("-c", "--model-config", help="models.yaml to use")
        ] = "models.yaml",
        output: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--output",
                help=(
                    "output path for axi-bundles.yaml (default: the model's "
                    "`axi_bundles:` from models.yaml when set, else "
                    "artefacts/axi/<model>/axi-bundles.yaml)"
                ),
            ),
        ] = None,
        amend: Annotated[
            str | None,
            typer.Option(
                "--amend",
                help=(
                    "existing axi-bundles.yaml to merge user edits from "
                    "(deferred to a follow-up; warns if passed)"
                ),
            ),
        ] = None,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the axi-profiler binary"),
        ] = "axi-profiler",
    ):
        """
        parse RTL to (re)generate the model's axi-bundles.yaml manifest
        """
        ctx = self._enter_command_context(primary_config=model_config)
        model_cfg = ModelConfigLoader(str(ctx.primary_config)).get_model(model_name)
        log_event(
            logger,
            logging.INFO,
            "command.axi_profile_discover",
            command="axi-profile",
            subcommand="discover",
            model=model_name,
            output=output,
        )
        profiler = RtlBuddyAxiProfileDiscover(
            name=self.name + "/axi-profile/discover",
            model_cfg=model_cfg,
            suite_dir=str(ctx.command_root),
            output=str(ctx.resolve_input(output)) if output else None,
            amend=str(ctx.resolve_input(amend)) if amend else None,
            executable=tool,
        )
        raise typer.Exit(profiler.run())

    def do_cmd_axi_profile_run(
        self,
        test_name: Annotated[str, typer.Argument(help="test from tests.yaml")],
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="tests.yaml to use")
        ] = "tests.yaml",
        output: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--output",
                help=(
                    "output path for axi-perf.json "
                    "(default: artefacts/axi/<test>/axi-perf.json)"
                ),
            ),
        ] = None,
        tb_prefix: Annotated[
            str | None,
            typer.Option(
                "--tb-prefix",
                help=(
                    "Override the testbench top scope name used as the "
                    "hierarchical prefix in the FST. Default is the test's "
                    "tb name from tests.yaml. Pass empty string to disable."
                ),
            ),
        ] = None,
        emit_txns_parquet: Annotated[
            bool,
            typer.Option(
                "--emit-txns-parquet",
                help=(
                    "Also emit a per-transaction parquet artifact at "
                    "artefacts/axi/<test>/axi-txns.parquet — the canonical "
                    "location `rb axi-profile notebook` reads. Requires "
                    "the axi-profiler [parquet] extra (pyarrow)."
                ),
            ),
        ] = False,
        emit_txns_parquet_path: Annotated[
            str | None,
            typer.Option(
                "--emit-txns-parquet-path",
                help=(
                    "Explicit path for the per-transaction parquet "
                    "artefact. Implies --emit-txns-parquet."
                ),
            ),
        ] = None,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the axi-profiler binary"),
        ] = "axi-profiler",
    ):
        """
        ingest a test's FST and emit per-test axi-perf.json

        Reads the manifest from `model.axi_bundles` in models.yaml and the FST from
        artefacts/<test>/dump.fst, then runs axi-profiler. Add --emit-txns-parquet to
        also write the parquet that `rb axi-profile notebook` reads.
        """
        ctx = self._enter_command_context(primary_config=test_config)
        suite_cfg = SuiteConfig(str(ctx.primary_config))
        test_cfg = suite_cfg.get_tests(test_name)[0]
        # Empty string (bare flag) lets the wrapper pick the default path; None means no emit.
        parquet_arg: str | None
        if emit_txns_parquet_path is not None:
            parquet_arg = str(ctx.resolve_input(emit_txns_parquet_path))
        elif emit_txns_parquet:
            parquet_arg = ""
        else:
            parquet_arg = None
        log_event(
            logger,
            logging.INFO,
            "command.axi_profile_run",
            command="axi-profile",
            subcommand="run",
            test=test_name,
            model=test_cfg.get_model().name,
            output=output,
            tb_prefix=tb_prefix,
            emit_txns_parquet=parquet_arg,
        )
        profiler = RtlBuddyAxiProfileRun(
            name=self.name + "/axi-profile/run",
            test_cfg=test_cfg,
            suite_dir=str(ctx.command_root),
            output=str(ctx.resolve_input(output)) if output else None,
            tb_prefix_override=tb_prefix,
            emit_txns_parquet=parquet_arg,
            executable=tool,
        )
        raise typer.Exit(profiler.run())

    def do_cmd_axi_profile_gen_monitor(
        self,
        model_name: Annotated[str, typer.Argument(help="model from models.yaml")],
        model_config: Annotated[
            str, typer.Option("-c", "--model-config", help="models.yaml to use")
        ] = "models.yaml",
        output: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--output",
                help=(
                    "output path for the generated SV monitor "
                    "(default: the model's `axi_monitor_out:` "
                    "from models.yaml)"
                ),
            ),
        ] = None,
        time_precision: Annotated[
            str | None,
            typer.Option(
                "--time-precision",
                help=(
                    "IEEE-1800 timeprecision atom (1ns / 100ps / 1ps / ...). "
                    "Must match the testbench's `timeprecision."
                ),
            ),
        ] = None,
        buffer_cap: Annotated[
            int | None,
            typer.Option(
                "--buffer-cap",
                help="Per-bundle FIFO depth cap. Drained only at $finish.",
            ),
        ] = None,
        tool: Annotated[
            str,
            typer.Option("--tool", help="path to the axi-profiler binary"),
        ] = "axi-profiler",
    ):
        """
        emit the SV bind-style AXI monitor for the model's testbench

        Reads the manifest from `model.axi_bundles` and writes to
        `model.axi_monitor_out` (both in models.yaml). Add the generated SV to the
        testbench filelist; pointing `axi_monitor_out` into the verif tree makes that
        a one-time step.
        """
        ctx = self._enter_command_context(primary_config=model_config)
        model_cfg = ModelConfigLoader(str(ctx.primary_config)).get_model(model_name)
        log_event(
            logger,
            logging.INFO,
            "command.axi_profile_gen_monitor",
            command="axi-profile",
            subcommand="gen-monitor",
            model=model_name,
            output=output,
        )
        profiler = RtlBuddyAxiProfileGenMonitor(
            name=self.name + "/axi-profile/gen-monitor",
            model_cfg=model_cfg,
            suite_dir=str(ctx.command_root),
            output=str(ctx.resolve_input(output)) if output else None,
            time_precision=time_precision,
            buffer_cap=buffer_cap,
            executable=tool,
        )
        raise typer.Exit(profiler.run())

    def do_cmd_axi_profile_notebook(
        self,
        test_name: Annotated[str, typer.Argument(help="test from tests.yaml")],
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="tests.yaml to use")
        ] = "tests.yaml",
        port: Annotated[
            int | None,
            typer.Option(
                "--port",
                help="TCP port for marimo's edit server (default: OS-assigned)",
            ),
        ] = None,
        foreground: Annotated[
            bool,
            typer.Option(
                "--foreground/--daemon",
                help=(
                    "Run marimo in the foreground (default). --daemon is "
                    "accepted for compatibility and also runs in the foreground."
                ),
            ),
        ] = True,
        headless: Annotated[
            bool,
            typer.Option(
                "--headless",
                help=(
                    "Forward `--headless --no-token` to marimo for hub launches. "
                    "The hub opens the URL, and the handoff is loopback-only."
                ),
            ),
        ] = False,
        marimo: Annotated[
            str,
            typer.Option(
                "--marimo",
                help="path to the marimo binary (default: 'marimo' on PATH)",
            ),
        ] = "marimo",
    ):
        """
        launch the packaged marimo notebook against a test's per-txn parquet

        Reads artefacts/axi/<test>/axi-txns.parquet (written by `rb axi-profile run
        <test> --emit-txns-parquet`) and opens the notebook shipped with the
        axi-profiler wheel in `marimo edit`, with $AXI_TXNS_PARQUET set.
        """
        ctx = self._enter_command_context(primary_config=test_config)
        suite_cfg = SuiteConfig(str(ctx.primary_config))
        test_cfg = suite_cfg.get_tests(test_name)[0]
        log_event(
            logger,
            logging.INFO,
            "command.axi_profile_notebook",
            command="axi-profile",
            subcommand="notebook",
            test=test_name,
            port=port,
            foreground=foreground,
            headless=headless,
        )
        notebook = RtlBuddyAxiProfileNotebook(
            name=self.name + "/axi-profile/notebook",
            test_cfg=test_cfg,
            suite_dir=str(ctx.command_root),
            port=port,
            foreground=foreground,
            headless=headless,
            marimo_executable=marimo,
        )
        raise typer.Exit(notebook.run())

    def do_docs_list(self):
        pages = [page.to_list_item() for page in list_pages()]
        if self.machine:
            self._emit_machine_result("docs list", 0, pages=pages)
            return

        for page in pages:
            print(f"{page['slug']} - {page['title']}: {page['description']}")

    def do_docs_show(
        self,
        slug: Annotated[
            str,
            typer.Argument(
                help="MkDocs path slug or slug#section-anchor, for example concepts/root-config or agents#local-docs-access"
            ),
        ],
    ):
        if "#" in slug:
            page_slug, anchor = slug.split("#", 1)
            section = get_section(page_slug, anchor)
            if section is None:
                if get_page(page_slug) is None:
                    raise click.ClickException(
                        f"Unknown docs page '{page_slug}'. Run `rtl-buddy docs list` to see available slugs."
                    )
                raise click.ClickException(
                    f"Unknown section '{anchor}' in page '{page_slug}'. Run `rtl-buddy docs show {page_slug}` to see available sections."
                )
            if self.machine:
                print(json.dumps(section, ensure_ascii=True))
                return
            print(section["content"])
            return

        page = get_page(slug)
        if page is None:
            raise click.ClickException(
                f"Unknown docs page '{slug}'. Run `rtl-buddy docs list` to see available slugs."
            )

        if self.machine:
            print(json.dumps(page.to_show_payload(), ensure_ascii=True))
            return

        print(page.content, end="" if page.content.endswith("\n") else "\n")

    def _spec_root(self) -> str:
        """Return the project root directory (where root_config.yaml lives, or CWD)."""
        from .config.root import discover_project_root

        return str(discover_project_root(fallback_cwd=True))

    def do_spec_list(
        self,
        spec_dir: Annotated[
            str,
            typer.Option("--spec-dir", help="Directory to search for specs.yaml files"),
        ] = None,
    ):
        """
        list all spec blocks discovered in the project
        """
        setup_logging(debug=False, verbose=False, color=True, machine=self.machine)
        root = self._spec_root()
        search_dir = spec_dir if spec_dir is not None else os.path.join(root, "spec")

        if not os.path.isdir(search_dir):
            emit_console_text(f"Spec directory not found: {search_dir}", style="yellow")
            if self.machine:
                self._emit_machine_result(
                    "spec list", 1, error="Spec directory not found"
                )
            raise typer.Exit(1)

        specs = discover_spec_configs(search_dir)
        blocks = all_spec_blocks(specs)
        if not blocks:
            emit_console_text("No spec blocks found.", style="yellow")
            if self.machine:
                self._emit_machine_result("spec list", 0, blocks=[])
            raise typer.Exit(0)

        if self.machine:
            self._emit_machine_result(
                "spec list",
                0,
                blocks=[
                    {
                        "block": b.name,
                        "desc": b.desc,
                        "path": cfg.get_path(),
                        "coverage_items": len(b.coverage_items),
                    }
                    for cfg, b in blocks
                ],
            )
            raise typer.Exit(0)

        rows = [
            {
                "block": b.name,
                "desc": b.desc,
                "items": str(len(b.coverage_items)),
                "path": os.path.relpath(cfg.get_path(), root),
            }
            for cfg, b in blocks
        ]
        render_summary(
            title="Spec Blocks",
            columns=[
                ("block", "Block"),
                ("desc", "Description"),
                ("items", "Coverage Items"),
                ("path", "Path"),
            ],
            rows=rows,
            logger=logger,
        )
        raise typer.Exit(0)

    def do_spec_check_testplan(
        self,
        spec_dir: Annotated[
            str,
            typer.Option("--spec-dir", help="Directory to search for specs.yaml files"),
        ] = None,
        design_dir: Annotated[
            str,
            typer.Option(
                "--design-dir", help="Directory to search for models.yaml files"
            ),
        ] = None,
        block: Annotated[
            list[str] | None,
            typer.Option(
                "--block",
                help="Only include spec blocks with this name; may be repeated",
            ),
        ] = None,
    ):
        """
        show which spec blocks have design models referencing them
        """
        setup_logging(debug=False, verbose=False, color=True, machine=self.machine)
        root = self._spec_root()
        search_spec = spec_dir if spec_dir is not None else os.path.join(root, "spec")
        search_design = (
            design_dir if design_dir is not None else os.path.join(root, "design")
        )

        specs = discover_spec_configs(search_spec) if os.path.isdir(search_spec) else []
        models = (
            discover_model_configs(search_design)
            if os.path.isdir(search_design)
            else []
        )
        blocks = all_spec_blocks(specs)

        if block:
            requested = set(block)
            found = {b.name for _, b in blocks}
            missing = requested - found
            if missing:
                raise FatalRtlBuddyError(
                    f"Unknown spec block(s): {', '.join(sorted(missing))}"
                )
            blocks = [(cfg, b) for cfg, b in blocks if b.name in requested]

        if not blocks:
            emit_console_text("No spec blocks found.", style="yellow")
            raise typer.Exit(0)

        spec_to_models = build_spec_to_models_map(specs, models)

        if self.machine:
            self._emit_machine_result(
                "spec check-testplan",
                0,
                blocks=[
                    {
                        "block": b.name,
                        "has_model": bool(
                            spec_to_models.get(f"{cfg.get_path()}::{b.name}")
                        ),
                        "models": [
                            {"path": p, "model": m}
                            for p, m in spec_to_models.get(
                                f"{cfg.get_path()}::{b.name}", []
                            )
                        ],
                    }
                    for cfg, b in blocks
                ],
            )
            raise typer.Exit(0)

        rows = []
        for cfg, b in blocks:
            key = f"{cfg.get_path()}::{b.name}"
            linked = spec_to_models.get(key, [])
            rows.append(
                {
                    "block": b.name,
                    "status": "yes" if linked else "no",
                    "models": ", ".join(m for _, m in linked) if linked else "-",
                }
            )

        render_summary(
            title="Spec Testplan Coverage",
            columns=[("block", "Block"), ("status", "Has Model"), ("models", "Models")],
            rows=rows,
            logger=logger,
        )
        uncovered = [
            b.name
            for cfg, b in blocks
            if not spec_to_models.get(f"{cfg.get_path()}::{b.name}")
        ]
        if uncovered:
            emit_console_text(
                f"Blocks without a design model: {', '.join(uncovered)}", style="yellow"
            )
        raise typer.Exit(0)

    def do_spec_check_coverage(
        self,
        spec_dir: Annotated[
            str,
            typer.Option("--spec-dir", help="Directory to search for specs.yaml files"),
        ] = None,
        verif_dir: Annotated[
            str,
            typer.Option(
                "--verif-dir", help="Directory to search for tests.yaml files"
            ),
        ] = None,
        block: Annotated[
            list[str] | None,
            typer.Option(
                "--block",
                help="Only include spec blocks with this name; may be repeated",
            ),
        ] = None,
    ):
        """
        show which spec coverage items are addressed by tests
        """
        setup_logging(debug=False, verbose=False, color=True, machine=self.machine)
        root = self._spec_root()
        search_spec = spec_dir if spec_dir is not None else os.path.join(root, "spec")
        search_verif = (
            verif_dir if verif_dir is not None else os.path.join(root, "verif")
        )

        specs = discover_spec_configs(search_spec) if os.path.isdir(search_spec) else []
        if os.path.isdir(search_verif):
            suite_tests, suite_load_failures = discover_suite_tests(search_verif)
        else:
            suite_tests, suite_load_failures = [], []
        # fpv/ is not reached by the verif walk; discover it via fpv_regression.yaml.
        fpv_entries, fpv_load_failures = discover_fpv_verifications(root)
        suite_tests = suite_tests + fpv_entries
        suite_load_failures = suite_load_failures + fpv_load_failures
        blocks = all_spec_blocks(specs)

        if block:
            requested = set(block)
            found = {b.name for _, b in blocks}
            missing = requested - found
            if missing:
                raise FatalRtlBuddyError(
                    f"Unknown spec block(s): {', '.join(sorted(missing))}"
                )
            blocks = [(cfg, b) for cfg, b in blocks if b.name in requested]

        if not blocks:
            if suite_load_failures:
                emit_console_text(
                    f"Suite load failures: {', '.join(suite_load_failures)}",
                    style="red",
                )
            else:
                emit_console_text("No spec blocks found.", style="yellow")
            raise typer.Exit(1 if suite_load_failures else 0)

        cov_map = build_coverage_map(suite_tests)

        if self.machine:
            items_out = [
                {
                    "block": b.name,
                    "id": item.id,
                    "desc": item.desc,
                    "covered": bool(cov_map.get(item.id)),
                    "tests": [
                        {"path": p, "test": t} for p, t in cov_map.get(item.id, [])
                    ],
                }
                for cfg, b in blocks
                for item in b.coverage_items
            ]
            exit_code = 1 if suite_load_failures else 0
            self._emit_machine_result(
                "spec check-coverage",
                exit_code,
                items=items_out,
                suite_load_failures=suite_load_failures,
            )
            raise typer.Exit(exit_code)

        rows = []
        for cfg, b in blocks:
            for item in b.coverage_items:
                tests = cov_map.get(item.id, [])
                rows.append(
                    {
                        "block": b.name,
                        "id": item.id,
                        "desc": item.desc,
                        "covered": "yes" if tests else "no",
                        "tests": ", ".join(t for _, t in tests) if tests else "-",
                    }
                )

        render_summary(
            title="Spec Coverage Items",
            columns=[
                ("block", "Block"),
                ("id", "ID"),
                ("desc", "Description"),
                ("covered", "Covered"),
                ("tests", "Tests"),
            ],
            rows=rows,
            logger=logger,
        )
        if suite_load_failures:
            emit_console_text(
                f"Suite load failures: {', '.join(suite_load_failures)}",
                style="red",
            )
        uncovered = [row["id"] for row in rows if row["covered"] == "no"]
        if uncovered:
            emit_console_text(
                f"Uncovered items: {', '.join(uncovered)}", style="yellow"
            )
        raise typer.Exit(1 if suite_load_failures else 0)

    def _synth_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["synth_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in (
            "gate_count",
            "area_um2",
            "wns_ps",
            "tns_ps",
            "timing_repaired",
            "static_function_findings",
            "unresolved_interfaces",
            "phys_model",
            "openroad_threads",
            "blocks",
        ):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _pnr_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["pnr_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in (
            "cell_count",
            "routed_cell_count",
            "physical_cell_count",
            "area_um2",
            "wns_setup_ps",
            "wns_hold_ps",
            "tns_ps",
            "tns_hold_ps",
            "drc_count",
            "worst_setup_corner",
            "worst_hold_corner",
            "corners",
            "gds_path",
            "png_path",
            "gds_mode",
            "gds_status",
            "gds_missing_cells",
            "gds_missing_cell_count",
            "gds_allowed_empty_cells",
            "export_provenance",
            "openroad_threads",
            "checkpoint_dir",
            "checkpoint_stages",
            "last_step",
            "checkpoint_stage",
            "checkpoint_run_id",
            "checkpoint_final",
            "abstract_dir",
            "abstract_manifest",
            "blocks",
            "fail_stage",
            "blocked_by",
            "synth",
        ):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _power_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["power_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in (
            "mode",
            "total_w",
            "internal_w",
            "switching_w",
            "leakage_w",
            "worst_corner",
            "corners",
            "phys_model",
            "unpowered_cells",
            "unpowered_cell_count",
            "unpowered_instance_count",
            "openroad_threads",
            "parasitics",
            "blocks",
        ):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _fpga_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["fpga_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in (
            "lut",
            "ff",
            "bram",
            "dsp",
            "wns_ns",
            "tns_ns",
            "whs_ns",
            "timing_met",
            "fmax_mhz",
            "failing_endpoints",
            "failing_paths",
            "total_power_w",
            "dynamic_power_w",
            "static_power_w",
            "drc_violations",
            "drc_by_severity",
            "methodology_warnings",
        ):
            if k in res and res[k] is not None:
                row[k] = res[k]
        # Kept even when None, so consumers can tell "not requested" from an older payload.
        if "bitstream" in res:
            row["bitstream"] = res["bitstream"]
        return row

    def _cdc_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["cdc_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in ("violations", "suppressed", "crossings", "backend", "findings"):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _fpv_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["fpv_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in ("mode", "depth", "engines", "runtime_s"):
            if k in res and res[k] is not None:
                row[k] = res[k]
        for k in ("vacuity", "coi"):
            if res.get(k):
                row[k] = res[k]
        return row

    def _render_synth_summary(self, title, synth_results, *, metadata=None):
        has_gates = any("gate_count" in r["results"].results for r in synth_results)
        has_area = any("area_um2" in r["results"].results for r in synth_results)
        has_timing = any("wns_ps" in r["results"].results for r in synth_results)
        has_tns = any("tns_ps" in r["results"].results for r in synth_results)
        rows = []
        for r in synth_results:
            res = r["results"].results
            row = {
                "synth_name": r["synth_name"],
                "result": res["result"],
                "desc": res["desc"],
            }
            if has_gates:
                gc = res.get("gate_count")
                row["gates"] = str(gc) if gc is not None else "-"
            if has_area:
                area = res.get("area_um2")
                row["area"] = f"{area:.2f} µm²" if area is not None else "-"
            if has_timing:
                wns = res.get("wns_ps")
                if wns is not None:
                    row["wns"] = f"{'+' if wns >= 0 else ''}{wns / 1000:.3f} ns"
                else:
                    row["wns"] = "-"
            if has_tns:
                tns = res.get("tns_ps")
                if tns is not None:
                    row["tns"] = f"{'+' if tns >= 0 else ''}{tns / 1000:.3f} ns"
                else:
                    row["tns"] = "-"
            rows.append(row)

        columns = [
            ("synth_name", "Synthesis"),
            ("result", "Result"),
            ("desc", "Description"),
        ]
        if has_gates:
            columns.append(("gates", "Gates"))
        if has_area:
            columns.append(("area", "Area"))
        if has_timing:
            columns.append(("wns", "WNS"))
        if has_tns:
            columns.append(("tns", "TNS"))
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def _exit_code_from_synth_results(self, synth_results):
        return 0 if all(r["results"].is_pass() for r in synth_results) else 1

    def _do_synth_suite(
        self,
        suite_cfg,
        synth_name=None,
        reg_level=None,
        effort_override=None,
        accept_stale: bool = False,
    ):
        syntheses = suite_cfg.get_syntheses(synth_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for s in syntheses:
            tool_name = s.get_tool_name()
            t_lvl = s.get_reglvl(tool_name)
            if reg_level is not None and t_lvl > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "synth_suite.skip",
                    synth=s.get_name(),
                    reason="above_regression_level",
                    synth_level=t_lvl,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "synth_name": s.get_name(),
                        "results": SynthSkipResults(
                            name=s.get_name() + "/results",
                            desc=f"lvl {t_lvl} > cmd reg_level {reg_level}",
                        ),
                    }
                )
                continue
            runner = SynthRunner(
                name=self.name + "/synth_runner",
                root_cfg=self.root_cfg,
                synth_cfg=s,
                suite_dir=suite_dir,
                effort_override=effort_override,
                accept_stale=accept_stale,
            )
            res = runner.run()
            if s.is_xfail():
                self._apply_xfail_logged(res, s, "synth_suite.xfail")
            results.append({"synth_name": s.get_name(), "results": res})
        return results

    def do_cmd_synth(
        self,
        synth_config: Annotated[
            str,
            typer.Option("-c", "--synth-config", help="synth.yaml to use"),
        ] = "synth.yaml",
        synth_name: Annotated[
            str,
            typer.Argument(
                help="name of synthesis to run", show_default="run all syntheses"
            ),
        ] = None,
        list_synths: Annotated[
            bool,
            typer.Option(
                "--list", help="list syntheses in the selected config and exit"
            ),
        ] = False,
        effort: Annotated[
            str,
            typer.Option(
                "--effort",
                help="override synthesis effort (must match cfg-synth-efforts entry)",
            ),
        ] = None,
        accept_stale: Annotated[
            bool,
            typer.Option(
                "--accept-stale",
                help=(
                    "consume blocks: abstracts whose recorded inputs changed "
                    "since they were hardened, qualifying the result instead "
                    "of failing"
                ),
            ),
        ] = False,
    ):
        """
        run synthesis
        """
        ctx = self._enter_command_context(
            primary_config=synth_config, list_only=list_synths
        )
        suite_cfg = SynthSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.synth",
            command="synth",
            synth=synth_name or "all",
            synth_config=synth_config,
            effort=effort,
        )

        if list_synths:
            if self.machine:
                self._emit_machine_result(
                    "synth --list", 0, names=list(suite_cfg.get_synth_names())
                )
            else:
                emit_console_text(
                    "  ".join(suite_cfg.get_synth_names()), stream="stdout"
                )
            raise typer.Exit(0)

        synth_results = self._do_synth_suite(
            suite_cfg,
            synth_name=synth_name,
            effort_override=effort,
            accept_stale=accept_stale,
        )
        exit_code = self._exit_code_from_synth_results(synth_results)
        if self.machine:
            self._emit_machine_result(
                "synth",
                exit_code,
                results=[self._synth_result_row(r) for r in synth_results],
            )
        else:
            self._render_synth_summary("Synthesis Results Summary", synth_results)
        raise typer.Exit(exit_code)

    def do_cmd_pnr(
        self,
        pnr_config: Annotated[
            str,
            typer.Option("-c", "--pnr-config", help="pnr.yaml to use"),
        ] = "pnr.yaml",
        pnr_name: Annotated[
            str,
            typer.Argument(
                help="name of pnr run",
                show_default=(
                    "run all entries in the suite, each block before the runs "
                    "that consume it"
                ),
            ),
        ] = None,
        list_runs: Annotated[
            bool,
            typer.Option(
                "--list", help="list pnr runs in the selected config and exit"
            ),
        ] = False,
        reg_level: Annotated[
            int,
            typer.Option(
                "-l",
                "--reg-level",
                help="run only entries with reglvl at or below this value",
            ),
        ] = 0,
        emit_gds: Annotated[
            bool,
            typer.Option(
                "--gds",
                help="stream out GDS via KLayout after a successful P&R",
            ),
        ] = False,
        emit_png: Annotated[
            bool,
            typer.Option(
                "--png",
                help="render a PNG of the routed GDS via KLayout (implies --gds)",
            ),
        ] = False,
        gds_mode: Annotated[
            GdsMode,
            typer.Option(
                "--gds-mode",
                case_sensitive=False,
                help=(
                    "override each run's gds-mode (implies --gds): strict "
                    "fails the run when a cell has no layout, preview keeps "
                    "the incomplete layout and reports the cells"
                ),
                show_default="each run's gds-mode (preview)",
            ),
        ] = None,
        accept_stale: Annotated[
            bool,
            typer.Option(
                "--accept-stale",
                help=(
                    "consume blocks: abstracts whose recorded inputs changed "
                    "since they were hardened, qualifying the result instead "
                    "of failing"
                ),
            ),
        ] = False,
        jobs: Annotated[
            int,
            typer.Option(
                "-j",
                "--jobs",
                min=1,
                help=(
                    "P&R runs at once: independent blocks harden side by side, "
                    "and a top waits for all of its own. Each run is a full OpenROAD "
                    "session sized by its `threads:` setting, so size the two together"
                ),
            ),
        ] = 1,
        run_synth: Annotated[
            bool,
            typer.Option(
                "--synth",
                help=(
                    "run each P&R run's upstream synthesis just before it, once "
                    "per synthesis — so a top built from blocks: is synthesized "
                    "after its blocks are hardened"
                ),
            ),
        ] = False,
    ):
        """run place-and-route"""
        ctx = self._enter_command_context(
            primary_config=pnr_config, list_only=list_runs
        )
        suite_cfg = PnrSuiteConfig(path=str(ctx.primary_config))
        if emit_png or gds_mode is not None:
            emit_gds = True
        log_event(
            logger,
            logging.INFO,
            "command.pnr",
            command="pnr",
            pnr=pnr_name or "all",
            pnr_config=pnr_config,
        )

        if list_runs:
            if self.machine:
                self._emit_machine_result(
                    "pnr --list", 0, names=list(suite_cfg.get_run_names())
                )
            else:
                emit_console_text("  ".join(suite_cfg.get_run_names()), stream="stdout")
            raise typer.Exit(0)

        results = self._do_pnr_suite(
            suite_cfg,
            pnr_name=pnr_name,
            reg_level=reg_level,
            emit_gds=emit_gds,
            emit_png=emit_png,
            gds_mode=gds_mode,
            accept_stale=accept_stale,
            run_synth=run_synth,
            jobs=jobs,
        )
        exit_code = 0 if all(r["results"].is_pass() for r in results) else 1
        if self.machine:
            self._emit_machine_result(
                "pnr",
                exit_code,
                results=[
                    self._pnr_result_row(r, suite=r.get("suite")) for r in results
                ],
            )
        else:
            self._render_pnr_summary("P&R Results Summary", results)
        raise typer.Exit(exit_code)

    def _do_pnr_suite(
        self,
        suite_cfg,
        *,
        pnr_name=None,
        reg_level=0,
        emit_gds: bool = False,
        emit_png: bool = False,
        gds_mode: str | None = None,
        accept_stale: bool = False,
        run_synth: bool = False,
        jobs: int = 1,
    ):
        suite_path = suite_cfg.get_path()

        def _selected(run):
            level = run.get_reglvl(run.get_tool_name())
            return reg_level is None or level <= reg_level

        if pnr_name is None:
            plan = plan_pnr_runs(
                suite_cfg, synth_blocks=_selected if run_synth else None
            )
            log_event(
                logger,
                logging.INFO,
                "pnr_suite.plan",
                order=[
                    p.name if not p.pulled_in else f"{p.name} ({p.suite_path})"
                    for p in plan
                ],
                jobs=jobs,
            )
        else:
            # A named run never re-runs its blocks; it fails fast when an abstract is missing.
            plan = [
                PlannedRun(suite_path=suite_path, cfg=run)
                for run in suite_cfg.get_runs(pnr_name)
            ]

        # Lock every tree here, on this thread, before any worker starts.
        for planned in plan:
            run = planned.cfg
            if not _selected(run):
                continue
            if planned.pulled_in:
                self._artifact_locks.acquire(
                    Path(planned.suite_path).resolve().parent / "artefacts",
                    command="pnr",
                )
            if run_synth:
                # Resolve now so a bad `synth:` fails before any block runs.
                run.resolve_synth_cfg()
                self._artifact_locks.acquire(
                    Path(run.get_synth_suite_path()).resolve().parent / "artefacts",
                    command="pnr",
                )

        synths = OnceMap()

        def _step(planned, outcomes):
            return self._pnr_plan_step(
                planned,
                outcomes,
                synths=synths if run_synth else None,
                reg_level=reg_level,
                emit_gds=emit_gds,
                emit_png=emit_png,
                gds_mode=gds_mode,
                accept_stale=accept_stale,
            )

        def _error(planned, exc):
            # A crash is that run's FAIL; other rows are kept and its consumers are blocked.
            log_event(
                logger,
                logging.ERROR,
                "pnr_suite.run_error",
                pnr=planned.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            row = {"pnr_name": planned.name}
            if planned.pulled_in:
                row["suite"] = planned.suite_path
            return {
                **row,
                "results": PnrFailResults(
                    name=f"{planned.name}/results",
                    desc=f"did not finish: {exc}",
                    fail_stage="error",
                ),
            }

        rows = run_plan(plan, _step, jobs=jobs, on_error=_error)
        return [rows[planned.key] for planned in plan]

    def _pnr_plan_step(
        self,
        planned,
        outcomes,
        *,
        synths,
        reg_level,
        emit_gds,
        emit_png,
        gds_mode,
        accept_stale,
    ) -> dict:
        """One planned run of `_do_pnr_suite`: blocked, skipped, or run.

        `outcomes` holds the finished runs, including this run's blocks. `synths` is
        the `--synth` run-once map, or `None`. Safe on a worker thread (`rb pnr -j`):
        artefact locks are already held.
        """
        run = planned.cfg
        suite_dir = str(Path(planned.suite_path).resolve().parent)
        row = {"pnr_name": run.get_name()}
        if planned.pulled_in:
            row["suite"] = planned.suite_path
        pnr_level = run.get_reglvl(run.get_tool_name())
        if reg_level is not None and pnr_level > reg_level:
            log_event(
                logger,
                logging.INFO,
                "pnr_suite.skip",
                pnr=run.get_name(),
                reason="above_regression_level",
                pnr_level=pnr_level,
                reg_level=reg_level,
            )
            res = PnrSkipResults(
                name=f"{run.get_name()}/results",
                desc=(f"reglvl {pnr_level} above {reg_level}"),
            )
            return {**row, "results": res}
        # An XFAIL passes the suite but delivers no abstract.
        blocked_by = [
            dep
            for dep in planned.deps
            if dep.key in outcomes
            and outcomes[dep.key].results.get("result") not in ("PASS", "XPASS", "SKIP")
        ]
        if blocked_by:
            # FAIL, not SKIP, so the suite cannot pass; no xfail marker excuses this fail_stage.
            names = ", ".join(
                f"'{dep.block}' (pnr run '{dep.key[1]}')" for dep in blocked_by
            )
            log_event(
                logger,
                logging.ERROR,
                "pnr_suite.blocked",
                pnr=run.get_name(),
                blocks=[dep.block for dep in blocked_by],
            )
            noun = "block" if len(blocked_by) == 1 else "blocks"
            res = PnrFailResults(
                name=f"{run.get_name()}/results",
                desc=f"blocked: {noun} {names} did not pass",
                fail_stage="blocked",
                fields={"blocked_by": [dep.block for dep in blocked_by]},
            )
            return {**row, "results": res}
        synth_row = None
        if synths is not None:
            synth_row = self._pnr_upstream_synth(run, synths, accept_stale=accept_stale)
            if synth_row["result"] not in ("PASS", "XPASS"):
                # FAIL with a fail_stage that xfail never excuses; consumers are blocked.
                res = PnrFailResults(
                    name=f"{run.get_name()}/results",
                    desc=(
                        f"synthesis '{synth_row['name']}' did not pass: "
                        f"{synth_row['desc']}"
                    ),
                    fail_stage="synth",
                    fields={"synth": synth_row},
                )
                return {**row, "results": res}
        runner = PnrRunner(
            name=run.get_name(),
            root_cfg=self.root_cfg,
            pnr_cfg=run,
            suite_dir=suite_dir,
            reglvl_filter=reg_level if reg_level else None,
            emit_gds=emit_gds,
            emit_png=emit_png,
            gds_mode=gds_mode,
            accept_stale=accept_stale,
        )
        res = runner.run()
        if run.is_xfail():
            self._apply_xfail_logged(res, run, "pnr_suite.xfail")
        if synth_row is not None:
            res.results["synth"] = synth_row
        return {**row, "results": res}

    def _pnr_upstream_synth(self, run, synths, *, accept_stale=False):
        """Run `run`'s upstream synthesis for `rb pnr --synth`, once per synthesis.

        Runs regardless of `reglvl`. Under `-j`, a second P&R run of the same netlist
        waits for the first. Its artefact tree is already locked. Returns the row the
        P&R result carries as `synth`.
        """
        synth_path = run.get_synth_suite_path()

        def _synthesize():
            [synth] = self._do_synth_suite(
                SynthSuiteConfig(synth_path),
                synth_name=run.get_synth_name(),
                accept_stale=accept_stale,
            )
            res = synth["results"].results
            return {
                "name": run.get_synth_name(),
                "suite": synth_path,
                "result": res.get("result"),
                "desc": res.get("desc"),
            }

        key = (os.path.realpath(synth_path), run.get_synth_name())
        return synths.get(key, _synthesize)

    def _render_pnr_summary(self, title, pnr_results, *, metadata=None):
        has_cells = any("cell_count" in r["results"].results for r in pnr_results)
        has_area = any("area_um2" in r["results"].results for r in pnr_results)
        has_setup = any("wns_setup_ps" in r["results"].results for r in pnr_results)
        has_hold = any("wns_hold_ps" in r["results"].results for r in pnr_results)
        has_drcs = any("drc_count" in r["results"].results for r in pnr_results)
        has_corners = any("corners" in r["results"].results for r in pnr_results)
        has_outputs = any(
            "gds_path" in r["results"].results
            or "png_path" in r["results"].results
            or "gds_status" in r["results"].results
            or "abstract_dir" in r["results"].results
            for r in pnr_results
        )
        rows = []
        for r in pnr_results:
            res = r["results"].results
            row = {
                "pnr_name": r["pnr_name"],
                "result": res["result"],
                "desc": res["desc"],
            }
            if has_cells:
                row["cells"] = (
                    str(res["cell_count"]) if res.get("cell_count") is not None else "-"
                )
            if has_area:
                area = res.get("area_um2")
                row["area"] = f"{area:.2f} µm²" if area is not None else "-"
            if has_setup:
                wns = res.get("wns_setup_ps")
                row["wns_setup"] = (
                    f"{'+' if wns >= 0 else ''}{wns / 1000:.3f} ns"
                    if wns is not None
                    else "-"
                )
            if has_hold:
                wns = res.get("wns_hold_ps")
                row["wns_hold"] = (
                    f"{'+' if wns >= 0 else ''}{wns / 1000:.3f} ns"
                    if wns is not None
                    else "-"
                )
            if has_corners:
                row["worst_corner"] = _pnr_worst_corner_cell(res)
            if has_drcs:
                drcs = res.get("drc_count")
                row["drcs"] = str(drcs) if drcs is not None else "-"
            if has_outputs:
                row["outputs"] = _pnr_outputs_cell(res)
            rows.append(row)

        columns = [
            ("pnr_name", "P&R Run"),
            ("result", "Result"),
            ("desc", "Description"),
        ]
        if has_cells:
            columns.append(("cells", "Cells"))
        if has_area:
            columns.append(("area", "Area"))
        if has_setup:
            columns.append(("wns_setup", "WNS Setup"))
        if has_hold:
            columns.append(("wns_hold", "WNS Hold"))
        if has_corners:
            columns.append(("worst_corner", "Worst Corner"))
        if has_drcs:
            columns.append(("drcs", "DRCs"))
        if has_outputs:
            columns.append(("outputs", "Outputs"))
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def do_cmd_pnr_export(
        self,
        pnr_config: Annotated[
            str,
            typer.Option("-c", "--pnr-config", help="pnr.yaml to use"),
        ] = "pnr.yaml",
        pnr_name: Annotated[
            str,
            typer.Argument(
                help="name of pnr run whose saved result to export",
                show_default="export every entry in the suite",
            ),
        ] = None,
        list_runs: Annotated[
            bool,
            typer.Option(
                "--list", help="list pnr runs in the selected config and exit"
            ),
        ] = False,
        reg_level: Annotated[
            int,
            typer.Option(
                "-l",
                "--reg-level",
                help="export only entries with reglvl at or below this value",
            ),
        ] = 0,
        emit_png: Annotated[
            bool,
            typer.Option("--png", help="render a PNG of the exported GDS"),
        ] = False,
        png_only: Annotated[
            bool,
            typer.Option(
                "--png-only",
                help=(
                    "re-render the PNG from the GDS already in the artefact "
                    "directory; no stream-out, and the GDS is not rewritten"
                ),
            ),
        ] = False,
        def_path: Annotated[
            str,
            typer.Option(
                "--def",
                help=(
                    "export this DEF instead of the run's own routed one; "
                    "needs a single named run, whose platform and top are used"
                ),
                show_default="the run's <top>.def",
            ),
        ] = None,
        checkpoint: Annotated[
            str,
            typer.Option(
                "--checkpoint",
                help=(
                    "export a stage checkpoint (checkpoints: in pnr.yaml) "
                    "instead of the routed result: a stage (floorplan, place, "
                    "cts, global_route) of the latest run, <run-id>/<stage>, "
                    "or a checkpoint file; output goes under the checkpoint "
                    "and is labelled not final"
                ),
                show_default="the run's routed DEF",
            ),
        ] = None,
        lyp: Annotated[
            str,
            typer.Option(
                "--lyp",
                help="layer properties (.lyp) for the render",
                show_default="the PDK's klayout-props",
            ),
        ] = None,
        png_width: Annotated[
            int,
            typer.Option("--png-width", help="rendered PNG width in pixels"),
        ] = DEFAULT_PNG_WIDTH,
        png_height: Annotated[
            int,
            typer.Option("--png-height", help="rendered PNG height in pixels"),
        ] = DEFAULT_PNG_HEIGHT,
        gds_mode: Annotated[
            GdsMode,
            typer.Option(
                "--gds-mode",
                case_sensitive=False,
                help=(
                    "override each run's gds-mode: strict publishes nothing "
                    "when a cell has no layout, preview keeps the incomplete "
                    "layout and reports the cells"
                ),
                show_default="each run's gds-mode (preview)",
            ),
        ] = None,
    ):
        """export GDS/PNG from a saved P&R result (no synthesis, no OpenROAD)"""
        ctx = self._enter_command_context(
            primary_config=pnr_config, list_only=list_runs
        )
        suite_cfg = PnrSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.pnr_export",
            command="pnr-export",
            pnr=pnr_name or "all",
            pnr_config=pnr_config,
            png_only=png_only,
        )

        if list_runs:
            if self.machine:
                self._emit_machine_result(
                    "pnr-export --list", 0, names=list(suite_cfg.get_run_names())
                )
            else:
                emit_console_text("  ".join(suite_cfg.get_run_names()), stream="stdout")
            raise typer.Exit(0)

        results = self._do_pnr_export_suite(
            suite_cfg,
            pnr_name=pnr_name,
            reg_level=reg_level,
            emit_png=emit_png,
            png_only=png_only,
            def_path=def_path,
            checkpoint=checkpoint,
            lyp=lyp,
            png_width=png_width,
            png_height=png_height,
            gds_mode=gds_mode,
        )
        exit_code = 0 if all(r["results"].is_pass() for r in results) else 1
        if self.machine:
            self._emit_machine_result(
                "pnr-export",
                exit_code,
                results=[self._pnr_result_row(r) for r in results],
            )
        else:
            self._render_pnr_summary("P&R Export Results Summary", results)
        raise typer.Exit(exit_code)

    def _do_pnr_export_suite(
        self,
        suite_cfg,
        *,
        pnr_name=None,
        reg_level=0,
        emit_png: bool = False,
        png_only: bool = False,
        def_path: str | None = None,
        checkpoint: str | None = None,
        lyp: str | None = None,
        png_width: int = DEFAULT_PNG_WIDTH,
        png_height: int = DEFAULT_PNG_HEIGHT,
        gds_mode: str | None = None,
    ):
        runs = suite_cfg.get_runs(pnr_name)
        if checkpoint is not None and len(runs) != 1:
            raise FatalRtlBuddyError(
                "--checkpoint needs exactly one pnr run: name the run whose "
                f"checkpoint to export (this selection has {len(runs)})"
            )
        if checkpoint is not None and def_path is not None:
            raise FatalRtlBuddyError(
                "--checkpoint and --def are exclusive: a checkpoint names its own DEF"
            )
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        if def_path is not None and len(runs) != 1:
            raise FatalRtlBuddyError(
                "--def needs exactly one pnr run: name the run to export "
                f"(this selection has {len(runs)})"
            )
        if def_path is not None and png_only:
            raise FatalRtlBuddyError(
                "--def and --png-only are exclusive: a re-render reads the "
                "GDS beside the run, not a DEF"
            )
        results = []
        for run in runs:
            pnr_level = run.get_reglvl(run.get_tool_name())
            if reg_level is not None and pnr_level > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "pnr_export_suite.skip",
                    pnr=run.get_name(),
                    reason="above_regression_level",
                    pnr_level=pnr_level,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "pnr_name": run.get_name(),
                        "results": PnrSkipResults(
                            name=f"{run.get_name()}/results",
                            desc=(f"reglvl {pnr_level} above {reg_level}"),
                        ),
                    }
                )
                continue
            runner = PnrExportRunner(
                name=run.get_name(),
                root_cfg=self.root_cfg,
                pnr_cfg=run,
                suite_dir=suite_dir,
                reglvl_filter=reg_level if reg_level else None,
                emit_png=emit_png,
                gds_mode=gds_mode,
                def_path=def_path,
                checkpoint=checkpoint,
                png_only=png_only,
                klayout_props=lyp,
                png_width=png_width,
                png_height=png_height,
            )
            res = runner.run()
            # No xfail handling: the marker excuses a P&R run, not an export.
            results.append({"pnr_name": run.get_name(), "results": res})
        return results

    def do_cmd_power(
        self,
        power_config: Annotated[
            str,
            typer.Option("-c", "--power-config", help="power.yaml to use"),
        ] = "power.yaml",
        power_name: Annotated[
            str,
            typer.Argument(
                help="name of power run",
                show_default="run all entries in the suite",
            ),
        ] = None,
        list_runs: Annotated[
            bool,
            typer.Option(
                "--list", help="list power runs in the selected config and exit"
            ),
        ] = False,
        reg_level: Annotated[
            int,
            typer.Option(
                "-l",
                "--reg-level",
                help="run only entries with reglvl at or below this value",
            ),
        ] = 0,
    ):
        """run power analysis"""
        ctx = self._enter_command_context(
            primary_config=power_config, list_only=list_runs
        )
        suite_cfg = PowerSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.power",
            command="power",
            power=power_name or "all",
            power_config=power_config,
        )

        if list_runs:
            if self.machine:
                self._emit_machine_result(
                    "power --list", 0, names=list(suite_cfg.get_run_names())
                )
            else:
                emit_console_text("  ".join(suite_cfg.get_run_names()), stream="stdout")
            raise typer.Exit(0)

        results = self._do_power_suite(
            suite_cfg,
            power_name=power_name,
            reg_level=reg_level,
        )
        exit_code = 0 if all(r["results"].is_pass() for r in results) else 1
        if self.machine:
            self._emit_machine_result(
                "power",
                exit_code,
                results=[self._power_result_row(r) for r in results],
            )
        else:
            self._render_power_summary("Power Results Summary", results)
        raise typer.Exit(exit_code)

    def _do_power_suite(
        self,
        suite_cfg,
        *,
        power_name=None,
        reg_level=0,
    ):
        root_cfg = self.root_cfg
        runs = suite_cfg.get_runs(power_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for run in runs:
            power_level = run.get_reglvl(run.get_tool_name())
            if reg_level is not None and power_level > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "power_suite.skip",
                    power=run.get_name(),
                    reason="above_regression_level",
                    power_level=power_level,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "power_name": run.get_name(),
                        "results": PowerSkipResults(
                            name=f"{run.get_name()}/results",
                            desc=f"reglvl {power_level} above {reg_level}",
                        ),
                    }
                )
                continue
            runner = PowerRunner(
                name=run.get_name(),
                root_cfg=root_cfg,
                power_cfg=run,
                suite_dir=suite_dir,
                reglvl_filter=reg_level if reg_level else None,
            )
            res = runner.run()
            if run.is_xfail():
                self._apply_xfail_logged(res, run, "power_suite.xfail")
            results.append({"power_name": run.get_name(), "results": res})
        return results

    def _render_power_summary(self, title, power_results, *, metadata=None):
        def _fmt_w(v):
            if v is None:
                return "-"
            if v == 0:
                return "0 W"
            mag = abs(v)
            if mag >= 1e-3:
                return f"{v * 1e3:.3f} mW"
            if mag >= 1e-6:
                return f"{v * 1e6:.3f} µW"
            return f"{v * 1e9:.3f} nW"

        has_mode = any("mode" in r["results"].results for r in power_results)
        has_source = any(
            "netlist_source" in r["results"].results for r in power_results
        )
        has_activity = any(
            "activity_source" in r["results"].results for r in power_results
        )
        has_parasitics = any(
            "parasitics" in r["results"].results for r in power_results
        )
        has_total = any("total_w" in r["results"].results for r in power_results)
        has_corner = any("worst_corner" in r["results"].results for r in power_results)
        has_breakdown = any(
            "internal_w" in r["results"].results
            or "switching_w" in r["results"].results
            or "leakage_w" in r["results"].results
            for r in power_results
        )

        rows = []
        for r in power_results:
            res = r["results"].results
            row = {
                "power_name": r["power_name"],
                "result": res["result"],
                "desc": res["desc"],
            }
            if has_mode:
                row["mode"] = res.get("mode", "-")
            if has_source:
                row["source"] = res.get("netlist_source", "-")
            if has_activity:
                row["activity"] = res.get("activity_source", "-")
            if has_parasitics:
                row["parasitics"] = res.get("parasitics", "-")
            if has_total:
                row["total"] = _fmt_w(res.get("total_w"))
            if has_corner:
                row["corner"] = res.get("worst_corner", "-")
            if has_breakdown:
                row["internal"] = _fmt_w(res.get("internal_w"))
                row["switching"] = _fmt_w(res.get("switching_w"))
                row["leakage"] = _fmt_w(res.get("leakage_w"))
            rows.append(row)

        columns = [
            ("power_name", "Power Run"),
            ("result", "Result"),
            ("desc", "Description"),
        ]
        if has_mode:
            columns.append(("mode", "Mode"))
        if has_source:
            columns.append(("source", "Source"))
        if has_activity:
            columns.append(("activity", "Activity"))
        if has_parasitics:
            columns.append(("parasitics", "Parasitics"))
        if has_total:
            columns.append(("total", "Total"))
        if has_corner:
            columns.append(("corner", "Worst Corner"))
        if has_breakdown:
            columns.append(("internal", "Internal"))
            columns.append(("switching", "Switching"))
            columns.append(("leakage", "Leakage"))
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def _exit_code_from_power_results(self, power_results):
        return 0 if all(r["results"].is_pass() for r in power_results) else 1

    def do_cmd_fpga(
        self,
        fpga_config: Annotated[
            str,
            typer.Option("-c", "--fpga-config", help="fpga.yaml to use"),
        ] = "fpga.yaml",
        fpga_name: Annotated[
            str,
            typer.Argument(
                help="name of fpga run",
                show_default="run all entries in the suite",
            ),
        ] = None,
        list_runs: Annotated[
            bool,
            typer.Option(
                "--list", help="list fpga runs in the selected config and exit"
            ),
        ] = False,
        reg_level: Annotated[
            int,
            typer.Option(
                "-l",
                "--reg-level",
                help="run only entries with reglvl at or below this value",
            ),
        ] = 0,
        emit_bitstream: Annotated[
            bool,
            typer.Option(
                "--bitstream",
                help="generate a bitstream after route (write_bitstream); "
                "off by default — a smoke/timing run doesn't need bitgen",
            ),
        ] = False,
    ):
        """run FPGA implementation (synth + place + route)"""
        ctx = self._enter_command_context(
            primary_config=fpga_config, list_only=list_runs
        )
        suite_cfg = FpgaSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.fpga",
            command="fpga",
            fpga=fpga_name or "all",
            fpga_config=fpga_config,
            bitstream=emit_bitstream,
        )

        if list_runs:
            if self.machine:
                self._emit_machine_result(
                    "fpga --list", 0, names=list(suite_cfg.get_run_names())
                )
            else:
                emit_console_text("  ".join(suite_cfg.get_run_names()), stream="stdout")
            raise typer.Exit(0)

        results = self._do_fpga_suite(
            suite_cfg,
            fpga_name=fpga_name,
            reg_level=reg_level,
            emit_bitstream=emit_bitstream,
        )
        exit_code = 0 if all(r["results"].is_pass() for r in results) else 1
        if self.machine:
            self._emit_machine_result(
                "fpga",
                exit_code,
                results=[self._fpga_result_row(r) for r in results],
            )
        else:
            self._render_fpga_summary("FPGA Results Summary", results)
        raise typer.Exit(exit_code)

    def _do_fpga_suite(
        self,
        suite_cfg,
        *,
        fpga_name=None,
        reg_level=0,
        emit_bitstream: bool = False,
    ):
        root_cfg = self.root_cfg
        runs = suite_cfg.get_runs(fpga_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for run in runs:
            fpga_level = run.get_reglvl(run.get_tool_name())
            if reg_level is not None and fpga_level > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "fpga_suite.skip",
                    fpga=run.get_name(),
                    reason="above_regression_level",
                    fpga_level=fpga_level,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "fpga_name": run.get_name(),
                        "results": FpgaSkipResults(
                            name=f"{run.get_name()}/results",
                            desc=f"reglvl {fpga_level} above {reg_level}",
                        ),
                    }
                )
                continue
            runner = FpgaRunner(
                name=run.get_name(),
                root_cfg=root_cfg,
                fpga_cfg=run,
                suite_dir=suite_dir,
                reglvl_filter=reg_level if reg_level else None,
                emit_bitstream=emit_bitstream,
            )
            res = runner.run()
            if run.is_xfail():
                self._apply_xfail_logged(res, run, "fpga_suite.xfail")
            results.append({"fpga_name": run.get_name(), "results": res})
        return results

    def _render_fpga_summary(self, title, fpga_results, *, metadata=None):
        def _fmt_util(entry):
            if not entry or entry.get("used") is None:
                return "-"
            used = entry["used"]
            pct = entry.get("util_pct")
            return f"{used} ({pct}%)" if pct is not None else str(used)

        def _fmt_ns(v):
            return f"{'+' if v >= 0 else ''}{v:.3f} ns" if v is not None else "-"

        has_util = any(
            any(k in r["results"].results for k in ("lut", "ff", "bram", "dsp"))
            for r in fpga_results
        )
        has_wns = any("wns_ns" in r["results"].results for r in fpga_results)
        has_whs = any("whs_ns" in r["results"].results for r in fpga_results)
        has_power = any("total_power_w" in r["results"].results for r in fpga_results)
        has_drcs = any("drc_violations" in r["results"].results for r in fpga_results)
        has_meth = any(
            "methodology_warnings" in r["results"].results for r in fpga_results
        )
        has_bit = any(r["results"].results.get("bitstream") for r in fpga_results)
        rows = []
        for r in fpga_results:
            res = r["results"].results
            row = {
                "fpga_name": r["fpga_name"],
                "result": res["result"],
                "desc": res["desc"],
            }
            if has_util:
                row["luts"] = _fmt_util(res.get("lut"))
                row["ffs"] = _fmt_util(res.get("ff"))
                row["brams"] = _fmt_util(res.get("bram"))
                row["dsps"] = _fmt_util(res.get("dsp"))
            if has_wns:
                row["wns"] = _fmt_ns(res.get("wns_ns"))
            if has_whs:
                row["whs"] = _fmt_ns(res.get("whs_ns"))
            if has_power:
                power = res.get("total_power_w")
                row["power"] = f"{power:.3f} W" if power is not None else "-"
            if has_drcs:
                drcs = res.get("drc_violations")
                row["drcs"] = str(drcs) if drcs is not None else "-"
            if has_meth:
                meth = res.get("methodology_warnings")
                row["meth"] = str(len(meth)) if meth is not None else "-"
            if has_bit:
                row["bit"] = "bit" if res.get("bitstream") else "-"
            rows.append(row)

        columns = [
            ("fpga_name", "FPGA Run"),
            ("result", "Result"),
            ("desc", "Description"),
        ]
        if has_util:
            columns.append(("luts", "LUTs"))
            columns.append(("ffs", "FFs"))
            columns.append(("brams", "BRAMs"))
            columns.append(("dsps", "DSPs"))
        if has_wns:
            columns.append(("wns", "WNS"))
        if has_whs:
            columns.append(("whs", "WHS"))
        if has_power:
            columns.append(("power", "Power"))
        if has_drcs:
            columns.append(("drcs", "DRCs"))
        if has_meth:
            columns.append(("meth", "Meth"))
        if has_bit:
            columns.append(("bit", "Outputs"))
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def _resolve_flow_reg_cfg_path(
        self, reg_config: str | None, default_filename: str, flow: str
    ) -> str:
        """Resolve a flow's regression manifest path.

        Precedence: explicit `-c`; `./<flow>_regression.yaml` in the invocation cwd;
        the flow's `cfg-rtl-reg` path from root_config.yaml.
        """
        if reg_config is not None:
            return (
                reg_config
                if os.path.isabs(reg_config)
                else str(self.invocation_cwd / reg_config)
            )
        local = str(self.invocation_cwd / default_filename)
        if os.path.isfile(local):
            return local
        # RootConfig does not exist yet; read only the cfg-rtl-reg block, leniently.
        root_cfg_path = _discover_root_cfg(start_dir=self.invocation_cwd)
        key, _ = REG_CFG_PATH_KEYS[flow]
        if root_cfg_path is not None:
            configured = resolve_reg_cfg_path(
                load_reg_cfg_paths(root_cfg_path), root_cfg_path, flow
            )
            if configured is not None:
                # Check existence so a wrong configured path is reported as such, not as a load failure.
                if os.path.isfile(configured):
                    log_event(
                        logger,
                        logging.INFO,
                        "regression.config_root_default",
                        path=configured,
                        flow=flow,
                    )
                    return configured
                raise FatalRtlBuddyError(
                    f"cfg-rtl-reg.{key} in root_config.yaml points at "
                    f"{configured}, which does not exist; correct the path, "
                    f"pass -c, or add ./{default_filename}"
                )
        raise FatalRtlBuddyError(
            f"{default_filename} not found; pass -c to specify a path or set "
            f"cfg-rtl-reg.{key} in root_config.yaml"
        )

    def do_fpga_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to fpga_regression.yaml",
                show_default="Use ./fpga_regression.yaml if present, "
                "otherwise root_config.yaml fpga-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option("-l", "--reg-level", help="FPGA regression level to stop at"),
        ] = 0,
        emit_bitstream: Annotated[
            bool,
            typer.Option(
                "--bitstream",
                help="generate bitstreams after route (write_bitstream); "
                "off by default — a smoke/timing regression doesn't need bitgen",
            ),
        ] = False,
    ):
        """
        run FPGA implementation regression
        """
        log_event(
            logger,
            logging.INFO,
            "command.fpga_regression",
            reg_config=reg_config,
            reg_level=reg_level,
            bitstream=emit_bitstream,
        )

        reg_cfg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "fpga_regression.yaml", "fpga"
        )

        orchestration_ctx = self._enter_command_context(primary_config=reg_cfg_path)
        fpga_reg = FpgaRegConfig(name=self.name + "/fpga_reg_config", path=reg_cfg_path)
        emit_console_text(
            f"Running FPGA regression from {orchestration_ctx.command_root}",
            style="cyan",
        )

        all_results = []
        machine_rows = []
        for suite_cfg in fpga_reg.get_suite_configs():
            log_event(
                logger,
                logging.INFO,
                "fpga_regression.suite_start",
                suite=suite_cfg.get_path(),
            )
            self._enter_command_context(primary_config=suite_cfg.get_path())
            suite_results = self._do_fpga_suite(
                suite_cfg,
                fpga_name=None,
                reg_level=reg_level,
                emit_bitstream=emit_bitstream,
            )
            all_results.extend(suite_results)
            if self.machine:
                machine_rows.extend(
                    self._fpga_result_row(r, suite=suite_cfg.get_path())
                    for r in suite_results
                )
        self._enter_command_context(command_root=orchestration_ctx.command_root)

        exit_code = 0 if all(r["results"].is_pass() for r in all_results) else 1
        if self.machine:
            self._emit_machine_result(
                "fpga-regression", exit_code, results=machine_rows
            )
        else:
            self._render_fpga_summary(
                "FPGA Regression Summary",
                all_results,
                metadata=[f"Reg Level: {reg_level}"],
            )
        raise typer.Exit(exit_code)

    def do_power_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to power_regression.yaml",
                show_default="Use ./power_regression.yaml if present, "
                "otherwise root_config.yaml power-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option("-l", "--reg-level", help="power regression level to stop at"),
        ] = 0,
    ):
        """
        run power analysis regression
        """
        log_event(
            logger,
            logging.INFO,
            "command.power_regression",
            reg_config=reg_config,
            reg_level=reg_level,
        )

        reg_cfg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "power_regression.yaml", "power"
        )

        orchestration_ctx = self._enter_command_context(primary_config=reg_cfg_path)
        power_reg = PowerRegConfig(
            name=self.name + "/power_reg_config", path=reg_cfg_path
        )
        emit_console_text(
            f"Running power regression from {orchestration_ctx.command_root}",
            style="cyan",
        )

        all_results = []
        machine_rows = []
        for suite_cfg in power_reg.get_suite_configs():
            log_event(
                logger,
                logging.INFO,
                "power_regression.suite_start",
                suite=suite_cfg.get_path(),
            )
            self._enter_command_context(primary_config=suite_cfg.get_path())
            suite_results = self._do_power_suite(
                suite_cfg, power_name=None, reg_level=reg_level
            )
            all_results.extend(suite_results)
            if self.machine:
                machine_rows.extend(
                    self._power_result_row(r, suite=suite_cfg.get_path())
                    for r in suite_results
                )
        self._enter_command_context(command_root=orchestration_ctx.command_root)

        exit_code = self._exit_code_from_power_results(all_results)
        if self.machine:
            self._emit_machine_result(
                "power-regression", exit_code, results=machine_rows
            )
        else:
            self._render_power_summary(
                "Power Regression Summary",
                all_results,
                metadata=[f"Reg Level: {reg_level}"],
            )
        raise typer.Exit(exit_code)

    def do_synth_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to synth_regression.yaml",
                show_default="Use ./synth_regression.yaml if present, "
                "otherwise root_config.yaml synth-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option(
                "-l", "--reg-level", help="synthesis regression level to stop at"
            ),
        ] = 0,
        effort: Annotated[
            str,
            typer.Option(
                "--effort",
                help="override synthesis effort (must match cfg-synth-efforts entry)",
            ),
        ] = None,
    ):
        """
        run synthesis regression
        """
        log_event(
            logger,
            logging.INFO,
            "command.synth_regression",
            reg_config=reg_config,
            reg_level=reg_level,
            effort=effort,
        )

        reg_cfg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "synth_regression.yaml", "synth"
        )

        orchestration_ctx = self._enter_command_context(primary_config=reg_cfg_path)
        synth_reg = SynthRegConfig(
            name=self.name + "/synth_reg_config", path=reg_cfg_path
        )
        emit_console_text(
            f"Running synthesis regression from {orchestration_ctx.command_root}",
            style="cyan",
        )

        all_results = []
        machine_rows = []
        for suite_cfg in synth_reg.get_suite_configs():
            log_event(
                logger,
                logging.INFO,
                "synth_regression.suite_start",
                suite=suite_cfg.get_path(),
            )
            self._enter_command_context(primary_config=suite_cfg.get_path())
            suite_results = self._do_synth_suite(
                suite_cfg,
                synth_name=None,
                reg_level=reg_level,
                effort_override=effort,
            )
            all_results.extend(suite_results)
            if self.machine:
                machine_rows.extend(
                    self._synth_result_row(r, suite=suite_cfg.get_path())
                    for r in suite_results
                )
        self._enter_command_context(command_root=orchestration_ctx.command_root)

        exit_code = self._exit_code_from_synth_results(all_results)
        if self.machine:
            self._emit_machine_result(
                "synth-regression", exit_code, results=machine_rows
            )
        else:
            self._render_synth_summary(
                "Synthesis Regression Summary",
                all_results,
                metadata=[f"Reg Level: {reg_level}"],
            )
        raise typer.Exit(exit_code)

    def _render_cdc_summary(self, title, cdc_results, *, metadata=None):
        rows = []
        for r in cdc_results:
            res = r["results"].results
            row = {
                "cdc_name": r["cdc_name"],
                "result": res["result"],
                "desc": res["desc"],
                "violations": str(res.get("violations", "-")),
                "suppressed": str(res.get("suppressed", "-")),
            }
            crossings = res.get("crossings")
            row["crossings"] = str(crossings) if crossings is not None else "-"
            rows.append(row)

        columns = [
            ("cdc_name", "CDC Analysis"),
            ("result", "Result"),
            ("desc", "Description"),
            ("violations", "Violations"),
            ("suppressed", "Suppressed"),
            ("crossings", "Crossings"),
        ]
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def _exit_code_from_cdc_results(self, cdc_results):
        return 0 if all(r["results"].is_pass() for r in cdc_results) else 1

    def _do_cdc_suite(self, suite_cfg, cdc_name=None, reg_level=None):
        analyses = suite_cfg.get_analyses(cdc_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for a in analyses:
            tool_name = a.get_tool_name()
            t_lvl = a.get_reglvl(tool_name)
            if reg_level is not None and t_lvl > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "cdc_suite.skip",
                    cdc=a.get_name(),
                    reason="above_regression_level",
                    cdc_level=t_lvl,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "cdc_name": a.get_name(),
                        "results": CdcSkipResults(
                            name=a.get_name() + "/results",
                            desc=f"lvl {t_lvl} > cmd reg_level {reg_level}",
                        ),
                    }
                )
                continue
            runner = CdcRunner(
                name=self.name + "/cdc_runner",
                root_cfg=self.root_cfg,
                cdc_cfg=a,
                suite_dir=suite_dir,
            )
            res = runner.run()
            if a.is_xfail():
                self._apply_xfail_logged(res, a, "cdc_suite.xfail")
            results.append({"cdc_name": a.get_name(), "results": res})
        return results

    def _resolve_elab_resources(
        self, cfg: ElabConfig, *, cpus: int | None = None
    ) -> JobResources:
        resolved = JobResources()
        dispatch_cfg = self.root_cfg.get_dispatch_cfg()
        for layer in (dispatch_cfg.resources, cfg.resources):
            if layer is None:
                continue
            if layer.cpus is not None:
                if (
                    not isinstance(layer.cpus, int)
                    or isinstance(layer.cpus, bool)
                    or layer.cpus < 1
                ):
                    raise FatalRtlBuddyError(
                        "elaboration resources.cpus must be a positive integer"
                    )
                resolved.cpus = layer.cpus
            if layer.mem is not None:
                resolved.mem = str(layer.mem)
            if layer.time is not None:
                resolved.time = str(layer.time)
        if cpus is not None:
            if cpus < 1:
                raise FatalRtlBuddyError(f"--cpus must be >= 1 (got {cpus})")
            resolved.cpus = cpus
        return resolved

    @staticmethod
    def _elab_skip(cfg: ElabConfig, reg_level: int) -> ElabResults:
        results = {
            "result": "SKIP",
            "desc": f"lvl {cfg.reglvl} > cmd reg_level {reg_level}",
            "stage": "filtered",
            "top": cfg.top,
            "source_count": 0,
            "input_source_count": 0,
            "diagnostics": {"errors": 0, "warnings": 0},
            "elapsed_sec": 0.0,
            "peak_memory_bytes": 0,
        }
        result_path = cfg.artifact_dir / "result.json"
        write_elab_result_json_best_effort(
            result_path,
            model=cfg.model.name,
            profile=cfg.profile_name,
            results=results,
        )
        return ElabResults(cfg.name, results, result_json=result_path)

    def _run_elab_local(
        self, cfg: ElabConfig, *, resources: JobResources | None = None
    ) -> ElabResults:
        if resources is None:
            resources = self._resolve_elab_resources(cfg)
        return ElabRunner(
            root_cfg=self.root_cfg, elab_cfg=cfg, resources=resources
        ).run()

    def _dispatch_elaborations(
        self,
        configs,
        backend,
        *,
        resources: list[JobResources] | None = None,
    ) -> list[ElabResults]:
        if not configs:
            return []
        if resources is None:
            resources = [self._resolve_elab_resources(cfg) for cfg in configs]
        run_token = uuid.uuid4().hex
        array_root = self.exec_ctx.artifact_root / ".dispatch" / "elab" / run_token
        groups = {}
        for index, (cfg, job_resources) in enumerate(
            zip(configs, resources, strict=True)
        ):
            dispatch_dir = cfg.artifact_dir / "dispatch"
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            result_json = dispatch_dir / f"result-{run_token}.json"
            spec = ElabJobSpec(
                model_name=cfg.model.name,
                profile_name=cfg.profile_name,
                suite_dir=str(cfg.config_dir),
                model_config_path=str(Path(cfg.model.path).resolve()),
                result_json=result_json,
                resources=job_resources,
                log_path=dispatch_dir / f"{backend.name}-{run_token}.log",
            )
            key = (job_resources.cpus, job_resources.mem, job_resources.time)
            groups.setdefault(key, []).append((index, cfg, spec))

        pending = []
        submitted = []
        try:
            dispatch_cfg = self.root_cfg.get_dispatch_cfg()
            for group_index, entries in enumerate(groups.values(), start=1):
                specs = [entry[2] for entry in entries]
                handles = backend.submit_array(
                    specs,
                    array_dir=array_root / f"group-{group_index}",
                    max_parallel=dispatch_cfg.max_jobs_per_array,
                )
                submitted.extend(handles)
                pending.extend(
                    (index, cfg, handle)
                    for (index, cfg, _), handle in zip(entries, handles, strict=True)
                )
            backend.wait_all(submitted)
        except BaseException:
            backend.cancel_all(submitted)
            raise

        telemetry = backend.collect_telemetry(submitted)
        collected: list[ElabResults | None] = [None] * len(configs)
        for index, cfg, handle in pending:
            try:
                remote = load_elab_result_json(
                    handle.spec.result_json,
                    model=cfg.model.name,
                    profile=cfg.profile_name,
                )
                payload = dict(remote.results)
            except FatalRtlBuddyError as exc:
                payload = elab_failure(
                    f"dispatch job {handle.job_id} produced no valid result: {exc}"
                )
                payload["top"] = cfg.top
            job_telemetry = telemetry.get(telemetry_key(handle))
            if job_telemetry:
                payload["telemetry"] = job_telemetry
            durable = cfg.artifact_dir / "result.json"
            write_elab_result_json_best_effort(
                durable,
                model=cfg.model.name,
                profile=cfg.profile_name,
                results=payload,
            )
            collected[index] = ElabResults(cfg.name, payload, result_json=durable)
        return [item for item in collected if item is not None]

    @staticmethod
    def _exit_code_from_elab_results(results) -> int:
        return 0 if all(result.is_pass() for result in results) else 1

    def _render_elab_summary(self, title: str, results, *, metadata=None) -> None:
        rows = []
        for result in results:
            payload = result.results
            diagnostics = payload.get("diagnostics", {})
            rows.append(
                {
                    "name": result.name,
                    "result": payload.get("result", "NA"),
                    "desc": payload.get("desc", ""),
                    "top": payload.get("top", ""),
                    "sources": payload.get("source_count", 0),
                    "errors": diagnostics.get("errors", 0),
                    "warnings": diagnostics.get("warnings", 0),
                    "elapsed": payload.get("elapsed_sec", 0),
                }
            )
        render_summary(
            title=title,
            columns=[
                ("name", "Model/Profile"),
                ("result", "Result"),
                ("desc", "Description"),
                ("top", "Top"),
                ("sources", "Sources"),
                ("errors", "Errors"),
                ("warnings", "Warnings"),
                ("elapsed", "Seconds"),
            ],
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def _finish_elab_command(self, command: str, title: str, results) -> None:
        exit_code = self._exit_code_from_elab_results(results)
        counts = {
            verdict: sum(result.results.get("result") == verdict for result in results)
            for verdict in ("PASS", "FAIL", "SKIP")
        }
        log_event(
            logger,
            logging.INFO,
            "elab.verdict",
            command=command,
            result="PASS" if exit_code == 0 else "FAIL",
            passed=counts["PASS"],
            failed=counts["FAIL"],
            skipped=counts["SKIP"],
        )
        if self.machine:
            self._emit_machine_result(
                command, exit_code, results=[result.to_row() for result in results]
            )
        else:
            self._render_elab_summary(title, results)
        raise typer.Exit(exit_code)

    def do_cmd_elab(
        self,
        model_name: Annotated[
            str | None,
            typer.Argument(help="model to elaborate; required unless --list is used"),
        ] = None,
        models_config: Annotated[
            str,
            typer.Option("-c", "--models-config", help="models.yaml to use"),
        ] = "models.yaml",
        profile_name: Annotated[
            str | None,
            typer.Option("--profile", help="named elaboration profile"),
        ] = None,
        list_elaborations: Annotated[
            bool,
            typer.Option("--list", help="list models and named profiles, then exit"),
        ] = False,
        dispatch: Annotated[
            str | None,
            typer.Option(
                "--dispatch", help="execution backend (local, local-parallel, slurm)"
            ),
        ] = None,
        jobs: Annotated[
            int | None,
            typer.Option("-j", "--jobs", help="local-parallel process count"),
        ] = None,
    ):
        """Elaborate one model using its existing ``models.yaml`` entry."""
        if dispatch is not None:
            validate_backend_name(dispatch)
        ctx = self._enter_command_context(
            primary_config=models_config, list_only=list_elaborations
        )
        loader = ModelConfigLoader(str(ctx.primary_config))
        if list_elaborations:
            names = []
            for model in loader.get_models():
                names.append(model.name)
                names.extend(f"{model.name}:{p.name}" for p in model.elaborations)
            if self.machine:
                self._emit_machine_result("elab --list", 0, names=names)
            else:
                emit_console_text("  ".join(names), stream="stdout")
            raise typer.Exit(0)
        if model_name is None:
            raise FatalRtlBuddyError(
                "elab requires a MODEL argument unless --list is used"
            )
        log_event(
            logger,
            logging.INFO,
            "command.elab",
            model=model_name,
            profile=profile_name,
            models_config=str(ctx.primary_config),
            dispatch=dispatch or "local",
        )
        model = loader.get_model(model_name)
        profile = (
            model.get_elaboration(profile_name) if profile_name is not None else None
        )
        cfg = ElabConfig(model=model, profile=profile)
        if dispatch is None and jobs is not None:
            raise FatalRtlBuddyError("--jobs requires --dispatch local-parallel")
        backend = (
            self._resolve_dispatch_backend(dispatch, jobs=jobs)
            if dispatch is not None
            else None
        )
        results = (
            [self._run_elab_local(cfg)]
            if backend is None
            else self._dispatch_elaborations([cfg], backend)
        )
        self._finish_elab_command("elab", "Elaboration Results", results)

    def do_elab_regression(
        self,
        reg_config: Annotated[
            str | None,
            typer.Option(
                "-c",
                "--reg-config",
                help="elab_regression.yaml to use",
                show_default="Use ./elab_regression.yaml if present, otherwise root_config.yaml elab-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option(
                "-l", "--reg-level", min=0, help="regression level to stop at"
            ),
        ] = 0,
        dispatch: Annotated[
            str | None,
            typer.Option(
                "--dispatch", help="execution backend (local, local-parallel, slurm)"
            ),
        ] = None,
        jobs: Annotated[
            int | None,
            typer.Option("-j", "--jobs", help="local-parallel process count"),
        ] = None,
    ):
        """Run every explicitly declared profile at or below ``reg-level``."""
        reg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "elab_regression.yaml", "elab"
        )
        orchestration_ctx = self._enter_command_context(primary_config=reg_path)
        log_event(
            logger,
            logging.INFO,
            "command.elab_regression",
            reg_config=str(reg_path),
            reg_level=reg_level,
            dispatch=dispatch,
        )
        reg = ElabRegConfig(self.name + "/elab_regression", str(reg_path))
        configs = reg.get_elaborations()
        resources_by_index: dict[int, JobResources] = {}
        for model_path in dict.fromkeys(cfg.model.path for cfg in configs):
            self._enter_command_context(primary_config=model_path)
            for index, cfg in enumerate(configs):
                if cfg.model.path == model_path:
                    resources_by_index[index] = self._resolve_elab_resources(cfg)
        self._enter_command_context(command_root=orchestration_ctx.command_root)
        skipped = [
            self._elab_skip(cfg, reg_level) for cfg in configs if cfg.reglvl > reg_level
        ]
        runnable = [
            (cfg, resources_by_index[index])
            for index, cfg in enumerate(configs)
            if cfg.reglvl <= reg_level
        ]
        backend_name = self._dispatch_backend_name(dispatch)
        validate_backend_name(backend_name)
        self._validate_jobs_flag(backend_name, jobs)
        backend = (
            self._resolve_dispatch_backend(dispatch, jobs=jobs) if runnable else None
        )
        if backend is None:
            executed = []
            for cfg, resources in runnable:
                self._enter_command_context(primary_config=cfg.model.path)
                executed.append(self._run_elab_local(cfg, resources=resources))
            self._enter_command_context(command_root=orchestration_ctx.command_root)
        else:
            executed = self._dispatch_elaborations(
                [cfg for cfg, _ in runnable],
                backend,
                resources=[resources for _, resources in runnable],
            )
        skipped_iter = iter(skipped)
        executed_iter = iter(executed)
        results = [
            next(skipped_iter) if cfg.reglvl > reg_level else next(executed_iter)
            for cfg in configs
        ]
        self._finish_elab_command(
            "elab-regression", "Elaboration Regression Results", results
        )

    def do_cmd_elab_job(
        self,
        model_name: Annotated[str, typer.Argument(help="model to elaborate")],
        result_json: Annotated[
            str, typer.Option("--result-json", help="result JSON envelope path")
        ],
        models_config: Annotated[
            str,
            typer.Option("-c", "--models-config", help="models.yaml to use"),
        ] = "models.yaml",
        profile_name: Annotated[
            str | None, typer.Option("--profile", help="named elaboration profile")
        ] = None,
        cpus: Annotated[
            int | None, typer.Option("--cpus", help="pyslang worker threads")
        ] = None,
    ):
        """Internal dispatch re-entry for one elaboration."""
        result_path = self._abs_invocation_path(result_json)
        ctx = self._enter_command_context(
            primary_config=models_config,
            log_path=job_log_path(result_path),
        )
        model = ModelConfigLoader(str(ctx.primary_config)).get_model(model_name)
        profile = (
            model.get_elaboration(profile_name) if profile_name is not None else None
        )
        cfg = ElabConfig(model=model, profile=profile)
        resources = self._resolve_elab_resources(cfg, cpus=cpus)
        result = ElabRunner(
            root_cfg=self.root_cfg,
            elab_cfg=cfg,
            resources=resources,
            result_json=result_path,
        ).run()
        exit_code = 0 if result.is_pass() else 1
        if self.machine:
            self._emit_machine_result(
                "_elab-job",
                exit_code,
                result=result.to_row(),
                result_json=str(result_path),
            )
        else:
            self._render_elab_summary("Elaboration Job Result", [result])
        raise typer.Exit(exit_code)

    def _render_lint_summary(self, title, lint_results, *, metadata=None):
        rows = []
        for r in lint_results:
            res = r["results"].results
            row = {
                "lint_name": r["lint_name"],
                "result": res["result"],
                "desc": res["desc"],
                "violations": str(res.get("violations", "-")),
                "files": str(res.get("files", "-")),
                "excluded": str(res.get("excluded", "-")),
            }
            rows.append(row)

        columns = [
            ("lint_name", "Lint Check"),
            ("result", "Result"),
            ("desc", "Description"),
            ("violations", "Violations"),
            ("files", "Files"),
            ("excluded", "Excluded"),
        ]
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def _exit_code_from_lint_results(self, lint_results):
        return 0 if all(r["results"].is_pass() for r in lint_results) else 1

    def _lint_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["lint_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in ("violations", "files", "excluded"):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _do_lint_suite(self, suite_cfg, lint_name=None, reg_level=None):
        checks = suite_cfg.get_checks(lint_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for c in checks:
            c_lvl = c.get_reglvl()
            if reg_level is not None and c_lvl > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "lint_suite.skip",
                    check=c.get_name(),
                    reason="above_regression_level",
                    check_level=c_lvl,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "lint_name": c.get_name(),
                        "results": LintSkipResults(
                            name=c.get_name() + "/results",
                            desc=f"lvl {c_lvl} > cmd reg_level {reg_level}",
                        ),
                    }
                )
                continue
            runner = LintRunner(
                name=self.name + "/lint_runner",
                root_cfg=self.root_cfg,
                lint_cfg=c,
                suite_dir=suite_dir,
            )
            res = runner.run()
            if c.is_xfail():
                self._apply_xfail_logged(res, c, "lint_suite.xfail")
            results.append({"lint_name": c.get_name(), "results": res})
        return results

    def do_cmd_lint(
        self,
        lint_config: Annotated[
            str,
            typer.Option("-c", "--lint-config", help="lint.yaml to use"),
        ] = "lint.yaml",
        lint_name: Annotated[
            str,
            typer.Argument(
                help="name of lint check to run", show_default="run all checks"
            ),
        ] = None,
        list_lints: Annotated[
            bool,
            typer.Option("--list", help="list checks in the selected config and exit"),
        ] = False,
    ):
        """
        run style lint (verible)
        """
        ctx = self._enter_command_context(
            primary_config=lint_config, list_only=list_lints
        )
        suite_cfg = LintSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.lint",
            command="lint",
            lint=lint_name or "all",
            lint_config=lint_config,
        )

        if list_lints:
            if self.machine:
                self._emit_machine_result(
                    "lint --list", 0, names=list(suite_cfg.get_check_names())
                )
            else:
                emit_console_text(
                    "  ".join(suite_cfg.get_check_names()), stream="stdout"
                )
            raise typer.Exit(0)

        lint_results = self._do_lint_suite(suite_cfg, lint_name=lint_name)
        exit_code = self._exit_code_from_lint_results(lint_results)
        if self.machine:
            self._emit_machine_result(
                "lint",
                exit_code,
                results=[self._lint_result_row(r) for r in lint_results],
            )
        else:
            self._render_lint_summary("Style Lint Results Summary", lint_results)
        raise typer.Exit(exit_code)

    def do_lint_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to lint_regression.yaml",
                show_default="Use ./lint_regression.yaml if present, "
                "otherwise root_config.yaml lint-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option("-l", "--reg-level", help="lint regression level to stop at"),
        ] = 0,
    ):
        """
        run style lint regression
        """
        log_event(
            logger,
            logging.INFO,
            "command.lint_regression",
            reg_config=reg_config,
            reg_level=reg_level,
        )

        reg_cfg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "lint_regression.yaml", "lint"
        )

        orchestration_ctx = self._enter_command_context(primary_config=reg_cfg_path)
        lint_reg = LintRegConfig(name=self.name + "/lint_reg_config", path=reg_cfg_path)
        emit_console_text(
            f"Running style lint regression from {orchestration_ctx.command_root}",
            style="cyan",
        )

        all_results = []
        machine_rows = []
        for suite_cfg in lint_reg.get_suite_configs():
            log_event(
                logger,
                logging.INFO,
                "lint_regression.suite_start",
                suite=suite_cfg.get_path(),
            )
            self._enter_command_context(primary_config=suite_cfg.get_path())
            suite_results = self._do_lint_suite(
                suite_cfg, lint_name=None, reg_level=reg_level
            )
            all_results.extend(suite_results)
            if self.machine:
                machine_rows.extend(
                    self._lint_result_row(r, suite=suite_cfg.get_path())
                    for r in suite_results
                )
        self._enter_command_context(command_root=orchestration_ctx.command_root)

        exit_code = self._exit_code_from_lint_results(all_results)
        if self.machine:
            self._emit_machine_result(
                "lint-regression", exit_code, results=machine_rows
            )
        else:
            self._render_lint_summary(
                "Style Lint Regression Summary",
                all_results,
                metadata=[f"Reg Level: {reg_level}"],
            )
        raise typer.Exit(exit_code)

    def do_cmd_saif(
        self,
        trace: Annotated[
            str,
            typer.Argument(help="path to input FST or VCD trace"),
        ],
        output: Annotated[
            str,
            typer.Argument(help="path to write SAIF v2.0 file"),
        ],
    ):
        """convert FST/VCD trace to SAIF v2.0"""
        from .tools.saif_from_trace import convert

        ctx = self._enter_command_context(command_root=self.invocation_cwd)
        convert(ctx.resolve_input(trace), ctx.resolve_input(output))

    def do_cmd_cdc(
        self,
        cdc_config: Annotated[
            str,
            typer.Option("-c", "--cdc-config", help="cdc.yaml to use"),
        ] = "cdc.yaml",
        cdc_name: Annotated[
            str,
            typer.Argument(
                help="name of CDC analysis to run", show_default="run all analyses"
            ),
        ] = None,
        list_cdcs: Annotated[
            bool,
            typer.Option(
                "--list", help="list analyses in the selected config and exit"
            ),
        ] = False,
        emit_constraints: Annotated[
            bool,
            typer.Option(
                "--emit-constraints",
                help="generate scoped CDC timing exceptions from the verified "
                "crossing set instead of linting",
            ),
        ] = False,
        emit_format: Annotated[
            str,
            typer.Option(
                "--format",
                help="constraint dialect for --emit-constraints",
                metavar="[sdc|xdc]",
                click_type=click.Choice(["sdc", "xdc"]),
            ),
        ] = "xdc",
        scoped: Annotated[
            bool,
            typer.Option(
                "--scoped",
                help="--emit-constraints: emit IP-relative (SCOPED_TO_REF) "
                "constraints, omitting top-level clock defs/groups",
            ),
        ] = False,
        output: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--output",
                help="--emit-constraints: write to this file (default: stdout)",
            ),
        ] = None,
        check_xdc: Annotated[
            str | None,
            typer.Option(
                "--check-xdc",
                metavar="FILE",
                help="audit a Vivado XDC's CDC exceptions against the verified "
                "crossing set instead of linting",
            ),
        ] = None,
        recognize_sync: Annotated[
            list[str] | None,
            typer.Option(
                "--recognize-sync",
                metavar="REGEX",
                help="--check-xdc: instance-path regex for a synchronizer the "
                "analyzer did not recognize (e.g. a blackboxed xpm_cdc_*); "
                "repeatable. Adds to cdc.yaml's recognized-syncs",
            ),
        ] = None,
    ):
        """
        run CDC lint
        """
        ctx = self._enter_command_context(
            primary_config=cdc_config, list_only=list_cdcs
        )
        if emit_constraints and check_xdc:
            raise FatalRtlBuddyError(
                "--emit-constraints and --check-xdc are mutually exclusive"
            )
        if emit_constraints:
            self._do_emit_constraints(
                ctx, cdc_config, cdc_name, emit_format, scoped, output
            )
            return
        if check_xdc:
            self._do_check_xdc(
                ctx, cdc_config, cdc_name, check_xdc, recognize_sync or []
            )
            return
        suite_cfg = CdcSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.cdc",
            command="cdc",
            cdc=cdc_name or "all",
            cdc_config=cdc_config,
        )

        if list_cdcs:
            if self.machine:
                self._emit_machine_result(
                    "cdc --list", 0, names=list(suite_cfg.get_analysis_names())
                )
            else:
                emit_console_text(
                    "  ".join(suite_cfg.get_analysis_names()), stream="stdout"
                )
            raise typer.Exit(0)

        cdc_results = self._do_cdc_suite(suite_cfg, cdc_name=cdc_name)
        exit_code = self._exit_code_from_cdc_results(cdc_results)
        if self.machine:
            self._emit_machine_result(
                "cdc",
                exit_code,
                results=[self._cdc_result_row(r) for r in cdc_results],
            )
        else:
            self._render_cdc_summary("CDC Lint Results Summary", cdc_results)
        raise typer.Exit(exit_code)

    def _run_single_cdc_with_maps(self, ctx, cdc_config, cdc_name, mode):
        """Resolve a single rtl-buddy-cdc analysis, run it with the structured maps, and return `(analysis, backend, res)`.

        Shared by `--emit-constraints` and `--check-xdc`; `mode` names the flag in
        error messages.
        """
        from .tools.cdc_rtl_buddy import RtlBuddyCdc

        suite_cfg = CdcSuiteConfig(path=str(ctx.primary_config))
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        analyses = suite_cfg.get_analyses(cdc_name)
        if not analyses:
            raise FatalRtlBuddyError(
                f"no CDC analysis named {cdc_name!r} in {cdc_config}"
            )
        if cdc_name is None and len(analyses) > 1:
            raise FatalRtlBuddyError(
                f"{mode} needs a single analysis (the IP); name one of: "
                + ", ".join(a.get_name() for a in analyses)
            )
        analysis = analyses[0]
        tool_name = analysis.get_tool_name()
        if tool_name != "rtl-buddy-cdc":
            raise FatalRtlBuddyError(
                f"{mode} requires the open rtl-buddy-cdc engine; "
                f"analysis '{analysis.get_name()}' uses tool '{tool_name}'"
            )
        tool_cfg = self.root_cfg.get_cdc_tool_cfg(tool_name)
        backend = RtlBuddyCdc(
            name=self.name + "/cdc_maps",
            cdc_cfg=analysis,
            tool_cfg=tool_cfg,
            suite_dir=suite_dir,
            root_cfg=self.root_cfg,
            emit_maps=True,
        )
        return analysis, backend, backend.run()

    def _do_emit_constraints(self, ctx, cdc_config, cdc_name, fmt, scoped, output):
        """`rb cdc --emit-constraints`: generate scoped CDC timing exceptions from
        rtl-buddy-cdc's crossing and reset-sync maps.
        """
        from .tools.cdc_constraints import generate_constraints

        analysis, backend, res = self._run_single_cdc_with_maps(
            ctx, cdc_config, cdc_name, "--emit-constraints"
        )
        domain_map, reset_map = backend.read_emitted_maps()

        if domain_map is None:
            # A failed analysis is a failure; a tool that emitted no map is a SKIP.
            verdict = res.results.get("result")
            failed = verdict not in ("PASS", "SKIP", "XFAIL")
            log_event(
                logger,
                logging.WARNING,
                "cdc.emit.no_maps",
                analysis=analysis.get_name(),
                recognition=verdict,
                failed=failed,
            )
            exit_code = 2 if failed else 0
            status = "FAIL" if failed else "SKIP"
            reason = (
                f"analysis did not pass ({verdict}); see the cdc log"
                if failed
                else "rtl-buddy-cdc produced no domain map"
            )
            if self.machine:
                self._emit_machine_result(
                    "cdc --emit-constraints",
                    exit_code,
                    analysis=analysis.get_name(),
                    status=status,
                    recognition=verdict,
                    reason=reason,
                )
            else:
                emit_console_text(
                    f"emit-constraints {status.lower()} for "
                    f"{analysis.get_name()}: {reason}",
                    style="red" if failed else "yellow",
                )
            raise typer.Exit(exit_code)

        emit = generate_constraints(domain_map, reset_map, fmt=fmt, scoped=scoped)

        if scoped and emit.unscoped:
            # Refuse rather than emit `<top>/*` wildcards that over-constrain the IP.
            raise FatalRtlBuddyError(
                f"--emit-constraints --scoped for {analysis.get_name()}: "
                f"{len(emit.unscoped)} crossing(s) flattened to the design top "
                "(e.g. "
                + ", ".join(emit.unscoped[:3])
                + ") — a hierarchy-preserving frontend is required to scope IP "
                "constraints. Set `frontend: slang` on the CDC analysis (install "
                "rtl-buddy-cdc[slang]), or drop --scoped for top-level output."
            )

        out_path = None
        if output:
            out_path = (
                output
                if os.path.isabs(output)
                else os.path.join(self.invocation_cwd, output)
            )
            Path(out_path).write_text(emit.text)

        log_event(
            logger,
            logging.INFO,
            "cdc.emit.done",
            analysis=analysis.get_name(),
            format=fmt,
            scoped=scoped,
            exceptions=len(emit.entries),
            recognition=res.results.get("result"),
            output=out_path,
        )

        if self.machine:
            self._emit_machine_result(
                "cdc --emit-constraints",
                0,
                analysis=analysis.get_name(),
                format=fmt,
                scoped=scoped,
                output=out_path,
                recognition=res.results.get("result"),
                constraints=emit.manifest,
            )
        elif out_path:
            emit_console_text(
                f"wrote {len(emit.entries)} CDC exception(s) to {out_path}"
            )
        else:
            emit_console_text(emit.text, stream="stdout", markup=False)
        raise typer.Exit(0)

    def _do_check_xdc(self, ctx, cdc_config, cdc_name, xdc, recognize_sync=None):
        """`rb cdc --check-xdc <file>`: audit an XDC's CDC exceptions against
        rtl-buddy-cdc's crossing set.
        """
        from .tools.cdc_xdc_audit import audit_xdc, extract_cdc_constraints

        xdc_path = xdc if os.path.isabs(xdc) else os.path.join(self.invocation_cwd, xdc)
        if not os.path.isfile(xdc_path):
            raise FatalRtlBuddyError(f"--check-xdc: XDC not found: {xdc_path}")

        analysis, backend, res = self._run_single_cdc_with_maps(
            ctx, cdc_config, cdc_name, "--check-xdc"
        )
        domain_map, _reset = backend.read_emitted_maps()
        if domain_map is None:
            verdict = res.results.get("result")
            failed = verdict not in ("PASS", "SKIP", "XFAIL")
            log_event(
                logger,
                logging.WARNING,
                "cdc.check_xdc.no_maps",
                analysis=analysis.get_name(),
                recognition=verdict,
                failed=failed,
            )
            exit_code = 2 if failed else 0
            status = "FAIL" if failed else "SKIP"
            reason = (
                f"analysis did not pass ({verdict}); see the cdc log"
                if failed
                else "rtl-buddy-cdc produced no domain map"
            )
            if self.machine:
                self._emit_machine_result(
                    "cdc --check-xdc",
                    exit_code,
                    analysis=analysis.get_name(),
                    status=status,
                    recognition=verdict,
                    reason=reason,
                )
            else:
                emit_console_text(
                    f"check-xdc {status.lower()} for {analysis.get_name()}: {reason}",
                    style="red" if failed else "yellow",
                )
            raise typer.Exit(exit_code)

        report = backend.read_report()
        xc = extract_cdc_constraints(Path(xdc_path).read_text(), source=str(xdc_path))
        recognized = analysis.get_recognized_syncs() + list(recognize_sync or [])
        audit = audit_xdc(domain_map, report, xc, recognized_syncs=recognized)
        blockers = audit.blockers
        # Only blockers fail the audit; other findings are warnings.
        exit_code = 2 if blockers else 0

        log_event(
            logger,
            logging.INFO if not blockers else logging.WARNING,
            "cdc.check_xdc.done",
            analysis=analysis.get_name(),
            xdc=xdc_path,
            findings=len(audit.findings),
            blockers=len(blockers),
        )

        if self.machine:
            self._emit_machine_result(
                "cdc --check-xdc",
                exit_code,
                analysis=analysis.get_name(),
                xdc=xdc_path,
                blockers=len(blockers),
                findings=audit.to_machine(),
            )
        else:
            if not audit.findings:
                emit_console_text(
                    f"check-xdc clean: {analysis.get_name()} — every verified "
                    "crossing is covered, no over-waives",
                    style="green",
                )
            else:
                for f in audit.findings:
                    style = {"blocker": "red", "warning": "yellow"}.get(
                        f.severity, None
                    )
                    emit_console_text(
                        f"[{f.severity}] {f.kind}: {f.message}",
                        style=style,
                        markup=False,
                    )
        raise typer.Exit(exit_code)

    def do_cdc_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to cdc_regression.yaml",
                show_default="Use ./cdc_regression.yaml if present, "
                "otherwise root_config.yaml cdc-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option("-l", "--reg-level", help="CDC regression level to stop at"),
        ] = 0,
    ):
        """
        run CDC lint regression
        """
        log_event(
            logger,
            logging.INFO,
            "command.cdc_regression",
            reg_config=reg_config,
            reg_level=reg_level,
        )

        reg_cfg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "cdc_regression.yaml", "cdc"
        )

        orchestration_ctx = self._enter_command_context(primary_config=reg_cfg_path)
        cdc_reg = CdcRegConfig(name=self.name + "/cdc_reg_config", path=reg_cfg_path)
        emit_console_text(
            f"Running CDC regression from {orchestration_ctx.command_root}",
            style="cyan",
        )

        all_results = []
        machine_rows = []
        for suite_cfg in cdc_reg.get_suite_configs():
            log_event(
                logger,
                logging.INFO,
                "cdc_regression.suite_start",
                suite=suite_cfg.get_path(),
            )
            self._enter_command_context(primary_config=suite_cfg.get_path())
            suite_results = self._do_cdc_suite(
                suite_cfg, cdc_name=None, reg_level=reg_level
            )
            all_results.extend(suite_results)
            if self.machine:
                machine_rows.extend(
                    self._cdc_result_row(r, suite=suite_cfg.get_path())
                    for r in suite_results
                )
        self._enter_command_context(command_root=orchestration_ctx.command_root)

        exit_code = self._exit_code_from_cdc_results(all_results)
        if self.machine:
            self._emit_machine_result("cdc-regression", exit_code, results=machine_rows)
        else:
            self._render_cdc_summary(
                "CDC Regression Summary",
                all_results,
                metadata=[f"Reg Level: {reg_level}"],
            )
        raise typer.Exit(exit_code)

    def _render_fpv_summary(self, title, fpv_results, *, metadata=None):
        from .tools.fpv_log_parse import summarize_engines

        rows = []
        has_vacuity = False
        has_coi = False
        has_assumes = False
        for r in fpv_results:
            res = r["results"].results
            engines = res.get("engines") or []
            runtime = res.get("runtime_s")
            per_engine = res.get("per_engine") or []
            vacuity = res.get("vacuity")
            vacuity_cell = self._format_vacuity_cell(vacuity)
            has_vacuity |= vacuity_cell is not None
            coi = res.get("coi")
            coi_cell = self._format_coi_cell(coi)
            has_coi |= coi_cell is not None
            assumes_cell = self._format_assumes_cell(coi)
            has_assumes |= assumes_cell is not None
            row = {
                "fpv_name": r["fpv_name"],
                "result": res["result"],
                "desc": res["desc"],
                "mode": str(res.get("mode", "-")),
                "depth": str(res.get("depth", "-")),
                "engines": ", ".join(engines) if engines else "-",
                "engine_results": summarize_engines(per_engine) if per_engine else "-",
                "vacuity": vacuity_cell or "-",
                "coi": coi_cell or "-",
                "assumes": assumes_cell or "-",
                "runtime": f"{runtime:.1f}s" if runtime is not None else "-",
            }
            rows.append(row)

        columns = [
            ("fpv_name", "FPV Run"),
            ("result", "Result"),
            ("desc", "Description"),
            ("mode", "Mode"),
            ("depth", "Depth"),
            ("engines", "Engines"),
            ("engine_results", "Engine Results"),
        ]
        if has_vacuity:
            columns.append(("vacuity", "Vacuity"))
        if has_coi:
            columns.append(("coi", "COI"))
        if has_assumes:
            columns.append(("assumes", "Assumes"))
        columns.append(("runtime", "Runtime"))
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    @staticmethod
    def _format_vacuity_cell(vacuity):
        """Return a Vacuity cell (`<vacuous>/<total> vacuous`), or None if no vacuity pass ran."""
        if not vacuity:
            return None
        total = vacuity.get("candidates", 0)
        if total == 0:
            return None
        vacuous = vacuity.get("vacuous", 0)
        unknown = sum(
            1 for c in vacuity.get("covers", []) if c.get("status") == "unknown"
        )
        if vacuous == 0 and unknown == 0:
            return f"{total} ok"
        parts = []
        if vacuous:
            parts.append(f"{vacuous}/{total} vacuous")
        if unknown:
            parts.append(f"{unknown} unknown")
        return ", ".join(parts)

    @staticmethod
    def _format_coi_cell(coi):
        """Return a COI cell (`<percent>% (<coi>/<total>)`), or None when no COI data was produced."""
        if not coi:
            return None
        total = coi.get("total_cells", 0)
        if total == 0:
            return None
        coi_cells = coi.get("coi_cells", 0)
        percent = coi.get("percent", 0.0)
        return f"{percent:.0f}% ({coi_cells}/{total})"

    @staticmethod
    def _format_assumes_cell(coi):
        """Return an Assumes cell (`N used, M dead`), or None if not applicable.

        Silent when the design has no $assume cells.
        """
        if not coi:
            return None
        assumes = coi.get("assumes")
        if not assumes:
            return None
        total = assumes.get("total", 0)
        if total == 0:
            return None
        used = assumes.get("in_assert_coi", 0)
        dead = assumes.get("dead", 0)
        if dead == 0:
            return f"{used} used"
        return f"{used} used, {dead} dead"

    def _exit_code_from_fpv_results(self, fpv_results):
        return 0 if all(r["results"].is_pass() for r in fpv_results) else 1

    def _do_fpv_suite(self, suite_cfg, fpv_name=None, reg_level=None):
        verifications = suite_cfg.get_verifications(fpv_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for v in verifications:
            tool_name = v.get_tool_name()
            t_lvl = v.get_reglvl(tool_name)
            if reg_level is not None and t_lvl > reg_level:
                log_event(
                    logger,
                    logging.INFO,
                    "fpv_suite.skip",
                    fpv=v.get_name(),
                    reason="above_regression_level",
                    fpv_level=t_lvl,
                    reg_level=reg_level,
                )
                results.append(
                    {
                        "fpv_name": v.get_name(),
                        "results": FpvSkipResults(
                            name=v.get_name() + "/results",
                            desc=f"lvl {t_lvl} > cmd reg_level {reg_level}",
                        ),
                    }
                )
                continue
            runner = FpvRunner(
                name=self.name + "/fpv_runner",
                root_cfg=self.root_cfg,
                fpv_cfg=v,
                suite_dir=suite_dir,
            )
            res = runner.run()
            if v.is_xfail():
                self._apply_xfail_logged(res, v, "fpv_suite.xfail")
            results.append({"fpv_name": v.get_name(), "results": res})
        return results

    def do_cmd_fpv(
        self,
        fpv_config: Annotated[
            str,
            typer.Option("-c", "--fpv-config", help="fpv.yaml to use"),
        ] = "fpv.yaml",
        fpv_name: Annotated[
            str,
            typer.Argument(
                help="name of FPV verification to run",
                show_default="run all verifications",
            ),
        ] = None,
        list_fpvs: Annotated[
            bool,
            typer.Option(
                "--list",
                help="list verifications in the selected config and exit",
            ),
        ] = False,
    ):
        """
        run formal property verification
        """
        ctx = self._enter_command_context(
            primary_config=fpv_config, list_only=list_fpvs
        )
        suite_cfg = FpvSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.fpv",
            command="fpv",
            fpv=fpv_name or "all",
            fpv_config=fpv_config,
        )

        if list_fpvs:
            if self.machine:
                self._emit_machine_result(
                    "fpv --list", 0, names=list(suite_cfg.get_verification_names())
                )
            else:
                emit_console_text(
                    "  ".join(suite_cfg.get_verification_names()), stream="stdout"
                )
            raise typer.Exit(0)

        fpv_results = self._do_fpv_suite(suite_cfg, fpv_name=fpv_name)
        exit_code = self._exit_code_from_fpv_results(fpv_results)
        # Render in machine mode too: the summary goes to the log, stdout stays the envelope.
        self._render_fpv_summary("FPV Results Summary", fpv_results)
        if self.machine:
            self._emit_machine_result(
                "fpv",
                exit_code,
                results=[self._fpv_result_row(r) for r in fpv_results],
            )
        raise typer.Exit(exit_code)

    def do_fpv_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to fpv_regression.yaml",
                show_default="Use ./fpv_regression.yaml if present, "
                "otherwise root_config.yaml fpv-reg-cfg-path",
            ),
        ] = None,
        reg_level: Annotated[
            int,
            typer.Option("-l", "--reg-level", help="FPV regression level to stop at"),
        ] = 0,
    ):
        """
        run FPV regression
        """
        log_event(
            logger,
            logging.INFO,
            "command.fpv_regression",
            reg_config=reg_config,
            reg_level=reg_level,
        )

        reg_cfg_path = self._resolve_flow_reg_cfg_path(
            reg_config, "fpv_regression.yaml", "fpv"
        )

        orchestration_ctx = self._enter_command_context(primary_config=reg_cfg_path)
        fpv_reg = FpvRegConfig(name=self.name + "/fpv_reg_config", path=reg_cfg_path)
        emit_console_text(
            f"Running FPV regression from {orchestration_ctx.command_root}",
            style="cyan",
        )

        all_results = []
        machine_rows = []
        for suite_cfg in fpv_reg.get_suite_configs():
            log_event(
                logger,
                logging.INFO,
                "fpv_regression.suite_start",
                suite=suite_cfg.get_path(),
            )
            self._enter_command_context(primary_config=suite_cfg.get_path())
            suite_results = self._do_fpv_suite(
                suite_cfg, fpv_name=None, reg_level=reg_level
            )
            all_results.extend(suite_results)
            if self.machine:
                machine_rows.extend(
                    self._fpv_result_row(r, suite=suite_cfg.get_path())
                    for r in suite_results
                )
        self._enter_command_context(command_root=orchestration_ctx.command_root)

        exit_code = self._exit_code_from_fpv_results(all_results)
        # Render in machine mode too: the summary goes to the log, stdout stays the envelope.
        self._render_fpv_summary(
            "FPV Regression Summary",
            all_results,
            metadata=[f"Reg Level: {reg_level}"],
        )
        if self.machine:
            self._emit_machine_result("fpv-regression", exit_code, results=machine_rows)
        raise typer.Exit(exit_code)

    def _mut_work_dir(self, suite_cfg: MutSuiteConfig) -> str:
        campaign = suite_cfg.get_config().get_name()
        base = Path(suite_cfg.get_path()).resolve().parent
        return str(base / "artefacts" / "mut" / campaign)

    def _render_mut_summary(self, title, results: MutResults):
        rows = []
        for o in results.outcomes:
            rows.append(
                {
                    "mutant": o.mutant_id,
                    "operator": o.operator,
                    "outcome": o.outcome.upper(),
                    "verdict": o.verdict,
                    "predicted": ", ".join(o.predicted_signals) or "-",
                    "diff": o.diff_summary,
                }
            )
        columns = [
            ("mutant", "Mutant"),
            ("operator", "Operator"),
            ("outcome", "Outcome"),
            ("verdict", "Verdict"),
            ("predicted", "Predicted Signals"),
            ("diff", "Mutation"),
        ]
        score = results.score()
        score_str = f"{score * 100:.1f}%" if score is not None else "n/a"
        metadata = [
            f"Mutation score: {score_str} "
            f"(killed {results.killed()} / scored {results.scored_total()})",
            f"Survived: {results.survived()}   Errored: {results.errored()}   "
            f"Baseline: {results.baseline_verdict}",
        ]
        misses = results.predicted_observable_misses()
        if misses:
            metadata.append(
                f"Predicted-observable misses (weak properties): "
                f"{', '.join(m.mutant_id for m in misses)}"
            )
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

    def do_mut_list(
        self,
        mut_config: Annotated[
            str,
            typer.Option("-c", "--mut-config", help="mut.yaml to use"),
        ] = "mut.yaml",
    ):
        """
        enumerate mutation candidate sites without mutating
        """
        ctx = self._enter_command_context(primary_config=mut_config)
        suite_cfg = MutSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.mut_list",
            command="mut list",
            mut_config=mut_config,
        )
        runner = MutRunner(
            name=self.name + "/mut_runner",
            root_cfg=self.root_cfg,
            mut_cfg=suite_cfg.get_config(),
            work_dir=self._mut_work_dir(suite_cfg),
        )
        sites = runner.list_candidates()
        if self.machine:
            self._emit_machine_result("mut list", 0, sites=sites)
        else:
            rows = [
                {
                    "operator": s["operator"],
                    "loc": f"{s['line']}:{s['column']}",
                    "snippet": s["snippet"],
                }
                for s in sites
            ]
            render_summary(
                title=f"Mutation Candidates ({len(sites)})",
                columns=[
                    ("operator", "Operator"),
                    ("loc", "Line:Col"),
                    ("snippet", "Snippet"),
                ],
                rows=rows,
                logger=logger,
            )
        raise typer.Exit(0)

    def do_mut_run(
        self,
        mut_config: Annotated[
            str,
            typer.Option("-c", "--mut-config", help="mut.yaml to use"),
        ] = "mut.yaml",
    ):
        """
        generate mutants, score them against an FPV proof, and report
        """
        ctx = self._enter_command_context(primary_config=mut_config)
        suite_cfg = MutSuiteConfig(path=str(ctx.primary_config))
        log_event(
            logger,
            logging.INFO,
            "command.mut_run",
            command="mut run",
            mut_config=mut_config,
        )
        work_dir = self._mut_work_dir(suite_cfg)
        runner = MutRunner(
            name=self.name + "/mut_runner",
            root_cfg=self.root_cfg,
            mut_cfg=suite_cfg.get_config(),
            work_dir=work_dir,
            rtl_builder_mode=self.rtl_builder_mode or "debug",
        )
        results = runner.run()

        report_path = os.path.join(work_dir, "mut_report.json")
        with open(report_path, "w") as f:
            json.dump(results.as_report(), f, indent=2)

        exit_code = 0 if results.is_pass() else 1
        if self.machine:
            self._emit_machine_result("mut run", exit_code, report=results.as_report())
        else:
            self._render_mut_summary("Mutation Testing Results", results)
            emit_console_text(f"Report written to {report_path}", style="cyan")
        raise typer.Exit(exit_code)

    def do_mut_score(
        self,
        report: Annotated[
            str,
            typer.Argument(help="path to a mut_report.json from a previous run"),
        ],
    ):
        """
        recompute mutation score from a saved report
        """
        report_path = (
            report if os.path.isabs(report) else str(self.invocation_cwd / report)
        )
        if not os.path.isfile(report_path):
            raise FatalRtlBuddyError(f"mut report not found: {report_path}")
        with open(report_path, "r") as f:
            data = json.load(f)
        results = MutResults.from_report(data)
        log_event(
            logger,
            logging.INFO,
            "command.mut_score",
            command="mut score",
            report=report_path,
        )
        if self.machine:
            self._emit_machine_result("mut score", 0, report=results.as_report())
        else:
            self._render_mut_summary("Mutation Score", results)
        raise typer.Exit(0)

    def _xplr_group_options(
        self,
        ctx: typer.Context,
        root: Annotated[
            str,
            typer.Option(
                "--root",
                help="anchor project-root discovery at this path instead of "
                "the current directory. Group-level: place it between 'xplr' and the "
                "subcommand, e.g. `rb xplr --root <project> list`",
            ),
        ] = None,
    ):
        """Group callback: `--root` plus the full `xplr <sub>` command name, so error envelopes name the subcommand."""
        if ctx.invoked_subcommand:
            self._pending_invoked_subcommand = f"xplr {ctx.invoked_subcommand}"
        if ctx.resilient_parsing:
            return
        self._xplr_root_override = None
        if root is not None:
            path = Path(root)
            if not path.is_absolute():
                path = self.invocation_cwd / path
            path = path.resolve()
            if not path.is_dir():
                raise FatalRtlBuddyError(f"xplr --root: {path} is not a directory")
            self._xplr_root_override = path

    def _enter_xplr_context(self) -> tuple[Path, Path]:
        """Anchor an xplr command and return (project_root, ledger_root).

        The ledger lives at `artefacts/xplr` under the project root; `rb xplr --root
        <path>` anchors discovery there instead of the cwd. Read commands are
        lock-free; write commands lock only the ledger root.
        """
        start = self._xplr_root_override or self.invocation_cwd
        try:
            project_root = discover_project_root(start_dir=start)
        except FatalRtlBuddyError as exc:
            raise FatalRtlBuddyError(
                f"{exc} For xplr commands: rb xplr --root <project> <subcommand>."
            ) from None
        ctx = self._enter_command_context(command_root=project_root, list_only=True)
        return project_root, xplr_ledger.ledger_root(ctx)

    def do_xplr_register(
        self,
        json_input: Annotated[
            str,
            typer.Option(
                "--json",
                help="JSON manifest file, or '-' for stdin: {knobs: [{name, "
                "from, to, rationale?, layer?}], hypothesis?, parent?, "
                "config_snapshot?, source?: {git_sha?, branch?, diff_from?}, "
                "provenance?: {tools?, agent?}}",
            ),
        ] = None,
        baseline: Annotated[
            str,
            typer.Option(
                "--baseline",
                help="git ref to record as source.diff_from (the RTL-diff "
                "baseline). Default: the parent experiment's pinned sha "
                "when 'parent' is given, else HEAD before any snapshot",
            ),
        ] = None,
    ):
        """
        open a new experiment: pin the git ref + record the knob manifest
        """
        project_root, root = self._enter_xplr_context()
        self._artifact_locks.acquire(root, command="xplr register")
        doc = {}
        if json_input is not None:
            doc = xplr_commands.load_json_doc(
                json_input, cwd=self.invocation_cwd, what="register"
            )
        record, path = xplr_commands.register_experiment(
            root, doc, project_root=project_root, baseline=baseline
        )
        if self.machine:
            self._emit_machine_result(
                "xplr register",
                0,
                id=record.id,
                record_path=str(path),
                record=record.to_dict(),
            )
        else:
            emit_console_text(
                f"registered {record.id} ({len(record.knobs)} knob(s), "
                f"source {record.source.git_sha[:12]}) -> {path}",
                style="green",
                markup=False,
            )
        raise typer.Exit(0)

    def do_xplr_attach_outcome(
        self,
        exp_id: Annotated[
            str, typer.Argument(metavar="EXP", help="experiment id, e.g. exp-0001")
        ],
        json_input: Annotated[
            str,
            typer.Option(
                "--json",
                help="JSON outcome file, or '-' for stdin: {status: "
                "'success'|'failed', metrics?, metric_meta?, artifacts?, "
                "provenance?: {tools?, reused_state?}}",
            ),
        ],
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                help="overwrite an outcome that is already terminal (success/failed)",
            ),
        ] = False,
    ):
        """
        attach flow-declared outcome metrics to an experiment
        """
        _, root = self._enter_xplr_context()
        self._artifact_locks.acquire(root, command="xplr attach-outcome")
        doc = xplr_commands.load_json_doc(
            json_input, cwd=self.invocation_cwd, what="attach-outcome"
        )
        record, path = xplr_commands.attach_outcome(root, exp_id, doc, force=force)
        if self.machine:
            self._emit_machine_result(
                "xplr attach-outcome",
                0,
                id=record.id,
                record_path=str(path),
                record=record.to_dict(),
            )
        else:
            emit_console_text(
                f"attached outcome '{record.outcome.status}' to {record.id} -> {path}",
                style="green",
                markup=False,
            )
        raise typer.Exit(0)

    def do_xplr_list(
        self,
        status: Annotated[
            str,
            typer.Option(
                "--status",
                help="only experiments with this outcome status "
                "(pending|running|success|failed)",
            ),
        ] = None,
    ):
        """
        list experiments in the ledger
        """
        _, root = self._enter_xplr_context()
        records = xplr_commands.list_experiments(root, status=status)
        summaries = [xplr_commands.summarize(r) for r in records]
        if self.machine:
            self._emit_machine_result("xplr list", 0, experiments=summaries)
        else:
            rows = [
                {
                    "id": s["id"],
                    "status": s["status"],
                    "git_sha": s["git_sha"][:12],
                    "knobs": str(s["n_knobs"]),
                    "created": s["created"],
                    "hypothesis": s.get("hypothesis", "-"),
                }
                for s in summaries
            ]
            render_summary(
                title=f"xplr experiments ({len(rows)})",
                columns=[
                    ("id", "Experiment"),
                    ("status", "Status"),
                    ("git_sha", "Source"),
                    ("knobs", "Knobs"),
                    ("created", "Created"),
                    ("hypothesis", "Hypothesis"),
                ],
                rows=rows,
                logger=logger,
            )
        raise typer.Exit(0)

    def do_xplr_show(
        self,
        exp_id: Annotated[
            str, typer.Argument(metavar="EXP", help="experiment id, e.g. exp-0001")
        ],
    ):
        """
        show one experiment's full record
        """
        _, root = self._enter_xplr_context()
        record, path = xplr_commands.get_experiment(root, exp_id)
        if self.machine:
            self._emit_machine_result(
                "xplr show",
                0,
                id=record.id,
                record_path=str(path),
                record=record.to_dict(),
            )
        else:
            print(dumps_record(record), end="")
        raise typer.Exit(0)

    def do_xplr_frontier(
        self,
        metrics: Annotated[
            str,
            typer.Option(
                "--metrics",
                help="override/declare dominance directions: "
                "'name:min,name2:max' (record-level metric_meta otherwise)",
            ),
        ] = None,
        prefer: Annotated[
            str,
            typer.Option(
                "--prefer",
                help="scalar preference to sort the frontier (never drops "
                "non-dominated points): comma/plus-separated weight*metric, "
                "e.g. '0.7*lut_pct+0.3*delay_ns'; lower score = better "
                "after direction normalization",
            ),
        ] = None,
    ):
        """
        curate the Pareto frontier over the ledger's outcome metrics
        """
        _, root = self._enter_xplr_context()
        overrides = (
            xplr_analysis.parse_metric_directions(metrics)
            if metrics is not None
            else None
        )
        preference = (
            xplr_analysis.parse_preference(prefer) if prefer is not None else None
        )
        records = xplr_ledger.list_records(root)
        payload = xplr_analysis.pareto_frontier(
            records, direction_overrides=overrides, preference=preference
        )
        if self.machine:
            self._emit_machine_result("xplr frontier", 0, **payload)
        else:
            metric_names = [m["name"] for m in payload["metrics"]]
            columns = [("id", "Experiment")] + [
                (
                    m["name"],
                    f"{m['name']} ({m['direction']})",
                )
                for m in payload["metrics"]
            ]
            if preference is not None:
                columns.append(("score", "Preference"))
            rows = []
            for member in payload["frontier"]:
                row = {"id": member["id"]}
                for name in metric_names:
                    row[name] = str(member["metrics"].get(name, "-"))
                if preference is not None:
                    row["score"] = f"{member['preference_score']:.4g}"
                rows.append(row)
            render_summary(
                title=f"Pareto frontier ({len(rows)} non-dominated)",
                columns=columns,
                rows=rows,
                logger=logger,
            )
            for entry in payload["dominated"]:
                emit_console_text(
                    f"dominated: {entry['id']} by {', '.join(entry['dominated_by'])}",
                    markup=False,
                )
            if payload["infeasible"]:
                emit_console_text(
                    f"infeasible (routed=false): {', '.join(payload['infeasible'])}",
                    style="yellow",
                    markup=False,
                )
            for entry in payload["excluded"]:
                emit_console_text(
                    f"excluded: {entry['id']} — {entry['reason']}",
                    style="yellow",
                    markup=False,
                )
        raise typer.Exit(0)

    def do_xplr_diff(
        self,
        exp_a: Annotated[
            str, typer.Argument(metavar="EXP_A", help="first experiment id")
        ],
        exp_b: Annotated[
            str, typer.Argument(metavar="EXP_B", help="second experiment id")
        ],
        patch: Annotated[
            bool,
            typer.Option(
                "--patch",
                help="include the full git diff patch between the pinned "
                "sources (not just --stat)",
            ),
        ] = False,
    ):
        """
        diff two experiments: knob delta, outcome delta, source diff
        """
        project_root, root = self._enter_xplr_context()
        record_a, _ = xplr_commands.get_experiment(root, exp_a)
        record_b, _ = xplr_commands.get_experiment(root, exp_b)
        payload = xplr_analysis.diff_records(record_a, record_b)
        payload["source"] = xplr_commands.source_diff(
            project_root,
            record_a.source.to_dict(),
            record_b.source.to_dict(),
            patch=patch,
        )
        if self.machine:
            self._emit_machine_result("xplr diff", 0, **payload)
        else:
            print(self._render_xplr_diff(payload))
        raise typer.Exit(0)

    @staticmethod
    def _render_xplr_diff(payload: dict) -> str:
        """Readable text rendering of the ``rb xplr diff`` payload."""
        lines = [f"diff {payload['a']}..{payload['b']}", "knobs:"]
        knobs = payload["knob_delta"]
        for knob in knobs["added"]:
            lines.append(f"  + {knob['name']}: {knob['from']!r} -> {knob['to']!r}")
        for entry in knobs["changed"]:
            lines.append(
                f"  ~ {entry['name']}: {entry['a']['to']!r} -> {entry['b']['to']!r}"
            )
        for knob in knobs["reverted"]:
            lines.append(f"  - {knob['name']} (was -> {knob['to']!r})")
        for name in knobs["unchanged"]:
            lines.append(f"  = {name}")
        if len(lines) == 2:
            lines.append("  (no knobs declared in either experiment)")
        outcome = payload["outcome_delta"]
        lines.append(f"outcome ({outcome['status_a']} -> {outcome['status_b']}):")
        for row in outcome["metrics"]:
            direction = row["direction"] or "?"
            lines.append(
                f"  {row['name']}: {row['a']} -> {row['b']} "
                f"(delta {row['delta']:+g}, {direction}, {row['assessment']})"
            )
        for name, value in outcome["only_a"].items():
            lines.append(f"  {name}: {value} -> (absent)")
        for name, value in outcome["only_b"].items():
            lines.append(f"  {name}: (absent) -> {value}")
        source = payload["source"]
        lines.append(f"source: {source['a']['git_sha']} -> {source['b']['git_sha']}")
        if source.get("note"):
            lines.append(f"  note: {source['note']}")
        if source.get("stat"):
            lines.extend(f"  {line}" for line in source["stat"].splitlines())
        if source.get("patch"):
            lines.append(source["patch"])
        return "\n".join(lines)

    def do_xplr_knob_effect(
        self,
        name: Annotated[
            str,
            typer.Argument(
                metavar="KNOB", help="knob name, e.g. synth.target_freq_mhz"
            ),
        ],
    ):
        """
        per-knob effect history across the ledger
        """
        _, root = self._enter_xplr_context()
        records = xplr_ledger.list_records(root)
        payload = xplr_analysis.knob_effect(records, name)
        if self.machine:
            self._emit_machine_result("xplr knob-effect", 0, **payload)
        else:
            rows = []
            for entry in payload["effects"]:
                deltas = entry.get("metrics_parent_delta", {})
                rows.append(
                    {
                        "exp": entry["exp"],
                        "status": entry["status"],
                        "change": f"{entry['from']!r} -> {entry['to']!r}",
                        "parent": entry.get("parent", "-"),
                        "delta": ", ".join(
                            f"{metric}{value:+g}" for metric, value in deltas.items()
                        )
                        or "-",
                        "rationale": entry.get("rationale", "-"),
                    }
                )
            render_summary(
                title=f"knob-effect: {name} ({len(rows)} experiment(s))",
                columns=[
                    ("exp", "Experiment"),
                    ("status", "Status"),
                    ("change", "Change"),
                    ("parent", "Parent"),
                    ("delta", "Delta vs parent"),
                    ("rationale", "Rationale"),
                ],
                rows=rows,
                logger=logger,
            )
            if "known_knobs" in payload:
                emit_console_text(
                    f"knob '{name}' appears in no experiment's manifest",
                    style="yellow",
                    markup=False,
                )
                if payload["suggestions"]:
                    emit_console_text(
                        "did you mean: " + ", ".join(payload["suggestions"]),
                        style="yellow",
                        markup=False,
                    )
                if payload["known_knobs"]:
                    emit_console_text(
                        "known knobs: " + ", ".join(payload["known_knobs"]),
                        markup=False,
                    )
        raise typer.Exit(0)

    def do_xplr_materialize(
        self,
        exp_id: Annotated[
            str, typer.Argument(metavar="EXP", help="experiment id, e.g. exp-0001")
        ],
        path: Annotated[
            str,
            typer.Option(
                "--path",
                help="worktree location (default: <worktree-root>/<exp>/, "
                "worktree-root from cfg-xplr, under artefacts/ — keep it "
                "gitignored)",
            ),
        ] = None,
    ):
        """
        create a git worktree at the experiment's pinned sha (idempotent)
        """
        project_root, root = self._enter_xplr_context()
        self._artifact_locks.acquire(root, command="xplr materialize")
        record, _ = xplr_commands.get_experiment(root, exp_id)
        cfg = load_xplr_config(project_root)
        worktree_path = None
        if path is not None:
            worktree_path = Path(path)
            if not worktree_path.is_absolute():
                worktree_path = self.invocation_cwd / worktree_path
        info = xplr_gitprov.materialize(
            project_root, root, record, cfg, path=worktree_path
        )
        if self.machine:
            self._emit_machine_result("xplr materialize", 0, **info)
        else:
            verb = "reusing" if info["reused"] else "materialized"
            emit_console_text(
                f"{verb} {record.id} at {info['path']} "
                f"(source {record.source.git_sha[:12]})",
                style="green",
                markup=False,
            )
        raise typer.Exit(0)

    def do_xplr_release(
        self,
        exp_id: Annotated[
            str, typer.Argument(metavar="EXP", help="experiment id, e.g. exp-0001")
        ],
    ):
        """
        remove the experiment's worktree; branch + record are kept
        """
        project_root, root = self._enter_xplr_context()
        self._artifact_locks.acquire(root, command="xplr release")
        xplr_commands.get_experiment(root, exp_id)  # raises on an unknown id
        info = xplr_gitprov.release(project_root, root, exp_id)
        if self.machine:
            self._emit_machine_result("xplr release", 0, **info)
        else:
            message = (
                f"released worktree of {exp_id} ({info['path']})"
                if info["removed"]
                else f"{exp_id} has no worktree to release"
            )
            emit_console_text(message, style="green", markup=False)
        raise typer.Exit(0)

    def do_xplr_gc(
        self,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run",
                help="report what would be evicted without touching anything",
            ),
        ] = False,
        policy: Annotated[
            str,
            typer.Option(
                "--policy",
                help="eviction policy for this run: keep-frontier (default; "
                "frontier members + lineage are never evicted) | "
                "oldest-first | manual (list candidates, evict nothing)",
            ),
        ] = None,
        target_gb: Annotated[
            float,
            typer.Option(
                "--target-gb",
                help="gc down to this usage (default: cfg-xplr disk-high-watermark-gb)",
            ),
        ] = None,
    ):
        """
        evict heavy artifacts/worktrees to keep disk under the threshold
        """
        project_root, root = self._enter_xplr_context()
        self._artifact_locks.acquire(root, command="xplr gc")
        cfg = load_xplr_config(project_root)
        payload = xplr_gitprov.gc(
            project_root,
            root,
            cfg,
            policy=policy,
            target_gb=target_gb,
            dry_run=dry_run,
        )
        if self.machine:
            self._emit_machine_result("xplr gc", 0, **payload)
        else:
            gb = xplr_gitprov.GB
            verb = "would evict" if dry_run else "evicted"
            emit_console_text(
                f"xplr gc ({payload['policy']}): "
                f"{payload['usage_bytes_before'] / gb:.3f} GB used, target "
                f"{payload['target_bytes'] / gb:.3f} GB — {verb} "
                f"{len(payload['evicted'])} experiment(s), "
                f"{payload['bytes_freed_total'] / gb:.3f} GB",
                markup=False,
            )
            for entry in payload["evicted"]:
                emit_console_text(
                    f"  {verb} {entry['id']}: {entry['bytes_freed']} bytes "
                    f"(record.json kept)",
                    markup=False,
                )
            if payload["protected"]:
                emit_console_text(
                    "protected (frontier/lineage/non-terminal): "
                    + ", ".join(payload["protected"]),
                    markup=False,
                )
            for note in payload["notes"]:
                emit_console_text(f"note: {note}", style="yellow", markup=False)
        raise typer.Exit(0)

    def _xplr_mock_group_options(self, ctx: typer.Context):
        """Refine the command name to ``xplr mock <sub>`` (see xplr group)."""
        if ctx.invoked_subcommand:
            self._pending_invoked_subcommand = f"xplr mock {ctx.invoked_subcommand}"

    def do_xplr_mock_info(
        self,
        scenario: Annotated[
            str,
            typer.Option(
                "--scenario",
                help="show one scenario only (rastrigin|zdt1)",
            ),
        ] = None,
    ):
        """
        list mockflow scenarios, knob specs, and the analytic ground truth
        """
        if scenario is not None:
            payload = xplr_mockflow.scenario_info(scenario)
            infos = [payload]
        else:
            infos = [
                xplr_mockflow.scenario_info(s) for s in sorted(xplr_mockflow.SCENARIOS)
            ]
            payload = {"scenarios": infos}
        if self.machine:
            self._emit_machine_result("xplr mock info", 0, **payload)
        else:
            for info in infos:
                emit_console_text(
                    f"{info['name']} ({info['objective']}-objective): "
                    f"{info['description']}",
                    style="bold",
                    markup=False,
                )
                for knob in info["knobs"]:
                    domain = (
                        "|".join(knob["choices"])
                        if knob["type"] == "choice"
                        else f"[{knob['range'][0]}, {knob['range'][1]}]"
                    )
                    emit_console_text(
                        f"  knob {knob['name']}: {knob['type']} {domain} "
                        f"(layer {knob['layer']}, default {knob['default']!r})",
                        markup=False,
                    )
                for combo in info["infeasible_when"]:
                    pairs = ", ".join(f"{k}={v}" for k, v in combo.items())
                    emit_console_text(
                        f"  infeasible (routed=false) when: {pairs}", markup=False
                    )
                emit_console_text(
                    f"  ground truth: {info['ground_truth']['description']}",
                    markup=False,
                )
        raise typer.Exit(0)

    def do_xplr_mock_run(
        self,
        scenario: Annotated[
            str,
            typer.Option("--scenario", help="scenario name (rastrigin|zdt1)"),
        ],
        json_input: Annotated[
            str,
            typer.Option(
                "--json",
                help="JSON knob-value object {name: value}, or '-' for stdin; "
                "omitted knobs take their scenario defaults",
            ),
        ] = None,
        seed: Annotated[
            int,
            typer.Option("--seed", help="noise seed (irrelevant when --noise is 0)"),
        ] = 0,
        noise: Annotated[
            float,
            typer.Option(
                "--noise",
                help="stddev of seeded Gaussian noise added to the objective "
                "metrics (simulated run-to-run variance; default 0 = exact)",
            ),
        ] = 0.0,
        register: Annotated[
            bool,
            typer.Option(
                "--register",
                help="register a ledger experiment AND attach the outcome in "
                "one step (knobs recorded as from=scenario default)",
            ),
        ] = False,
        source_sha: Annotated[
            str,
            typer.Option(
                "--source-sha",
                help="with --register: record this sha verbatim as "
                "source.git_sha (the agent-declared pin path; no dirty bit). "
                "The escape hatch for sandboxes where the project root is "
                "not a git repository",
            ),
        ] = None,
        source_branch: Annotated[
            str,
            typer.Option(
                "--source-branch",
                help="with --source-sha: optional source.branch label, "
                "recorded verbatim",
            ),
        ] = None,
    ):
        """
        evaluate one knob vector against a mockflow scenario
        """
        if source_sha is not None and not register:
            raise FatalRtlBuddyError(
                "mock run: --source-sha only makes sense with --register "
                "(a stateless evaluation pins no source)"
            )
        if source_branch is not None and source_sha is None:
            raise FatalRtlBuddyError(
                "mock run: --source-branch requires --source-sha (a branch "
                "label alone does not pin a revision)"
            )
        values = {}
        if json_input is not None:
            values = xplr_commands.load_json_doc(
                json_input, cwd=self.invocation_cwd, what="mock run"
            )
        result = xplr_mockflow.evaluate(scenario, values, seed=seed, noise=noise)
        payload = dict(result)
        payload["outcome"] = xplr_mockflow.outcome_doc(result)
        if register:
            project_root, root = self._enter_xplr_context()
            self._artifact_locks.acquire(root, command="xplr mock run")
            doc = xplr_mockflow.register_doc(scenario, values, result["knobs"])
            if source_sha is not None:
                source: dict = {"git_sha": source_sha}
                if source_branch is not None:
                    source["branch"] = source_branch
                doc["source"] = source
            try:
                record, _ = xplr_commands.register_experiment(
                    root, doc, project_root=project_root
                )
            except FatalRtlBuddyError as exc:
                if source_sha is None and "not a git repository" in str(exc):
                    raise FatalRtlBuddyError(
                        f"mock run --register: the project root "
                        f"({project_root}) is not a git repository with "
                        "commits, so the source cannot be pinned — pass "
                        "--source-sha <sha> (and optionally --source-branch) "
                        "to declare the pin verbatim"
                    ) from None
                raise
            record, path = xplr_commands.attach_outcome(
                root, record.id, xplr_mockflow.outcome_doc(result)
            )
            payload.update(id=record.id, record_path=str(path), record=record.to_dict())
        if self.machine:
            self._emit_machine_result("xplr mock run", 0, **payload)
        else:
            metrics = ", ".join(
                f"{name}={value}" for name, value in result["metrics"].items()
            )
            emit_console_text(
                f"mockflow {scenario}: {metrics}",
                style="green" if result["routed"] else "yellow",
                markup=False,
            )
            if register:
                emit_console_text(
                    f"registered {payload['id']} -> {payload['record_path']}",
                    style="green",
                    markup=False,
                )
        raise typer.Exit(0)

    def do_xplr_mock_score(
        self,
        scenario: Annotated[
            str,
            typer.Option(
                "--scenario",
                help="score one scenario only (default: every scenario with "
                "mockflow experiments in the ledger)",
            ),
        ] = None,
    ):
        """
        score the ledger's mockflow experiments against the ground truth
        """
        _, root = self._enter_xplr_context()
        records = xplr_ledger.list_records(root)
        if scenario is not None:
            payload = xplr_mockflow.score_records(records, scenario)
            scores = [payload]
        else:
            found = xplr_mockflow.mockflow_scenarios(records)
            if not found:
                raise FatalRtlBuddyError(
                    "no mockflow experiments in the ledger — run "
                    "`rb xplr mock run --scenario <s> --register` first"
                )
            scores = [xplr_mockflow.score_records(records, s) for s in found]
            payload = {"scenarios": scores}
        if self.machine:
            self._emit_machine_result("xplr mock score", 0, **payload)
        else:
            for score in scores:
                if score["objective"] == "single":
                    best = score["best"]
                    detail = (
                        f"best {best['id']} {score['metric']}="
                        f"{best[score['metric']]:g}, regret {score['regret']:g}"
                        if best is not None
                        else "no feasible experiments"
                    )
                else:
                    detail = (
                        f"hypervolume {score['hypervolume']:g} "
                        f"({score['hypervolume_ratio']:.1%} of front), "
                        f"distance-to-front "
                        f"{score['distance_to_front'] if score['distance_to_front'] is not None else '-'}"
                    )
                emit_console_text(
                    f"{score['scenario']}: {score['n_feasible']}/"
                    f"{score['n_experiments']} feasible — {detail}",
                    markup=False,
                )
        raise typer.Exit(0)

    def do_cmd_wave(
        self,
        test_name: Annotated[
            str, typer.Argument(help="name of test to open waveform for")
        ],
        test_config: Annotated[
            str, typer.Option("-c", "--test-config", help="tests.yaml to use")
        ] = "tests.yaml",
        surfer_name: Annotated[
            str | None,
            typer.Option(
                "--surfer",
                help="cfg-surfer entry name "
                "(default: the active platform's cfg-platforms surfer routing, "
                "else surfer-default)",
            ),
        ] = None,
        resim: Annotated[
            bool,
            typer.Option(
                "--resim", help="force re-run of debug sim even if FST exists"
            ),
        ] = False,
        focused_signal: Annotated[
            bool,
            typer.Option(
                "--focused-signal",
                help="annotate only the signal selected via Go to declaration; default annotates all signals in scope",
            ),
        ] = False,
    ):
        """
        open waveform viewer for a test
        """
        from .tools.wave_launcher import WaveLauncher, prepare_surfer_trace
        from .tools.wave_trace import TRACE_CANDIDATES, newest_trace

        self.rtl_builder_mode = "debug"

        ctx = self._enter_command_context(primary_config=test_config)

        surfer_cfg = self.root_cfg.get_surfer_cfg(surfer_name)
        if surfer_cfg is None:
            effective = surfer_name or (
                self.root_cfg.get_platform_tool_name("surfer") or "surfer-default"
            )
            raise FatalRtlBuddyError(
                f'No cfg-surfer entry named "{effective}" in root_config.yaml. '
                f"Add a cfg-surfer section to enable waveform viewing."
            )
        if not surfer_cfg.available:
            raise FatalRtlBuddyError(
                f'Surfer not found at "{surfer_cfg.path}". '
                f"Check cfg-surfer.path in root_config.yaml or install surfer on PATH."
            )

        suite_cfg = SuiteConfig(path=str(ctx.primary_config))
        suite_dir = str(ctx.command_root)
        test_cfg = suite_cfg.get_tests(test_name)[0]

        trace_dir = os.path.join(suite_dir, "artefacts", test_name)
        surfer_file = os.path.join(suite_dir, f"{test_name}.surfer")

        # Newest dump of any format (FST, VCD, VPD), not a fixed dump.fst.
        trace_path = newest_trace(trace_dir)

        log_event(
            logger,
            logging.INFO,
            "command.wave",
            command="wave",
            test=test_name,
            trace=trace_path or "",
        )

        if resim or trace_path is None:
            log_event(
                logger,
                logging.INFO,
                "wave.sim_required",
                test=test_name,
                trace_dir=trace_dir,
            )
            suite_results = self._do_test_suite(
                suite_cfg, test_name=test_name, run_ids=[None]
            )
            result = suite_results[0]["results"] if suite_results else None
            if result is None or not result.is_pass():
                raise FatalRtlBuddyError(
                    f'Debug sim for "{test_name}" failed; cannot open waveform.'
                )
            trace_path = newest_trace(trace_dir)
            if trace_path is None:
                names = " / ".join(TRACE_CANDIDATES)
                raise FatalRtlBuddyError(
                    f'Debug sim for "{test_name}" produced no waveform trace '
                    f"under {trace_dir} (looked for {names})."
                )
        else:
            log_event(
                logger,
                logging.INFO,
                "wave.trace_found",
                test=test_name,
                trace=trace_path,
            )

        builder_cfg = self.root_cfg.resolve_rtl_builder_cfg(test_cfg.get_builder_name())
        trace_path = prepare_surfer_trace(
            trace_path, builder_cfg.get_wave_format(), test_name
        )

        WaveLauncher(
            test_cfg=test_cfg,
            surfer_cfg=surfer_cfg,
            suite_dir=suite_dir,
            fst_path=trace_path,
            surfer_file=surfer_file if os.path.isfile(surfer_file) else None,
            scope_annotation=not focused_signal,
        ).launch()

    def do_cmd_wave_fpv(
        self,
        verif_name: Annotated[
            str,
            typer.Argument(help="name of FPV verification to open CEX for"),
        ],
        fpv_config: Annotated[
            str,
            typer.Option("-c", "--fpv-config", help="fpv.yaml to use"),
        ] = "fpv.yaml",
        surfer_name: Annotated[
            str | None,
            typer.Option(
                "--surfer",
                help="cfg-surfer entry name "
                "(default: the active platform's cfg-platforms surfer routing, "
                "else surfer-default)",
            ),
        ] = None,
    ):
        """
        open SymbiYosys counterexample VCD for a failed FPV verification

        Opens `fpv/<suite>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd` in the
        configured surfer. Fails if the verification has not run, the proof passed, or
        no engine emitted a trace.
        """
        from .tools.fpv_cex_finder import find_cex_vcd

        ctx = self._enter_command_context(primary_config=fpv_config)

        surfer_cfg = self.root_cfg.get_surfer_cfg(surfer_name)
        if surfer_cfg is None:
            effective = surfer_name or (
                self.root_cfg.get_platform_tool_name("surfer") or "surfer-default"
            )
            raise FatalRtlBuddyError(
                f'No cfg-surfer entry named "{effective}" in root_config.yaml. '
                f"Add a cfg-surfer section to enable waveform viewing."
            )
        if not surfer_cfg.available:
            raise FatalRtlBuddyError(
                f'Surfer not found at "{surfer_cfg.path}". '
                f"Check cfg-surfer.path in root_config.yaml or install surfer on PATH."
            )

        suite_cfg = FpvSuiteConfig(path=str(ctx.primary_config))
        suite_dir = str(ctx.command_root)
        # Validates the name; raises FatalRtlBuddyError if unknown.
        suite_cfg.get_verifications(verif_name)

        cex_path = find_cex_vcd(suite_dir, verif_name)
        if cex_path is None:
            raise FatalRtlBuddyError(
                f'No counterexample VCD found for FPV verification "{verif_name}". '
                f"Either the proof passed (no CEX produced), or `rb fpv {verif_name}` "
                f"has not been run yet."
            )

        log_event(
            logger,
            logging.INFO,
            "command.wave_fpv",
            command="wave-fpv",
            verification=verif_name,
            cex=cex_path,
        )

        cmd = [surfer_cfg.get_surfer_exe(), cex_path]
        emit_console_text(
            f"Opening CEX for {verif_name} in surfer (Ctrl-C to exit).",
        )
        proc = subprocess.Popen(cmd)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_event(
            logger,
            logging.INFO,
            "wave_fpv.done",
            verification=verif_name,
        )

    def do_nvim_install(
        self,
        force: Annotated[
            bool,
            typer.Option("--force", help="remove any existing install and re-clone"),
        ] = False,
        update: Annotated[
            bool,
            typer.Option(
                "--update", help="sync an existing install to the pinned revision"
            ),
        ] = False,
        ref: Annotated[
            str | None,
            typer.Option(
                "--ref", help="override the pinned rtl-buddy-nvim git ref (tag/branch)"
            ),
        ] = None,
        source: Annotated[
            str | None,
            typer.Option(
                "--source",
                help="override the rtl-buddy-nvim repo URL or local path "
                "(for offline/dev installs)",
            ),
        ] = None,
        no_lsp: Annotated[
            bool,
            typer.Option(
                "--no-lsp",
                help="omit the verible-verilog-ls autostart from the managed setup",
            ),
        ] = False,
    ):
        """
        install/update the rtl-buddy-nvim editor plugin (hub + wave annotation)

        Clones the pinned, hub-compatible rtl-buddy-nvim revision into the nvim pack
        dir and writes a managed setup file that connects to the hub and shows `rb
        wave` signal values. `rb wave-install-nvim` is an alias.
        """
        from .tools.nvim_install import install

        install(force=force, update=update, ref=ref, source=source, lsp=not no_lsp)

    def do_export(self):
        assert False, "not yet impl"

    def do_gen_vlog_run_script(self):
        assert False, "not yet impl"

    def _select_model_configs(
        self, models: list[str], project_root: str, command: str = "filelist"
    ):
        """Resolve `--model` names against every models.yaml under the root.

        Empty `models` selects every model. Unknown names are fatal. `command` names
        the verible subcommand in error events.
        """
        all_entries = discover_model_configs(project_root)
        if not all_entries:
            log_event(
                logger,
                logging.ERROR,
                "verible_filelist.no_models_discovered",
                project_root=project_root,
                command=command,
            )
            raise FatalRtlBuddyError(f"no models.yaml files found under {project_root}")

        by_name: dict[str, ModelConfig] = {}
        for _, model in all_entries:
            # First found wins across files; ModelConfigLoader rejects duplicates within one.
            by_name.setdefault(model.name, model)
        missing = [name for name in models if name not in by_name]
        if missing:
            log_event(
                logger,
                logging.ERROR,
                "verible_filelist.unknown_models",
                models=missing,
                available=sorted(by_name),
                command=command,
            )
            raise FatalRtlBuddyError(f"unknown model(s): {', '.join(missing)}")
        if models:
            return [by_name[name] for name in models]
        return [model for _, model in all_entries]

    def _verible_model_files(
        self,
        model_names: list[str],
        excludes: list[str],
        project_root: str,
        command: str,
    ) -> list[str]:
        """Expand `--model` names into the source files verible should visit.

        Keeps bare source entries only (no `-v`/`-y` files or `+incdir+`, `+define+`,
        `+libext+` directives), then drops `excludes` (fnmatch globs against the
        root-relative path; `*` crosses directories). Paths are relative to the
        process cwd.
        """
        selected = self._select_model_configs(
            model_names, project_root, command=command
        )
        vlog_fl = VlogFilelist(
            name=self.name + "/verible_model_files",
            model_cfg=None,
            output_path=None,
        )
        cwd = os.getcwd()
        expanded: list[str] = []
        seen: set[str] = set()
        for model_cfg in selected:
            for path in vlog_fl.extract_source_files(model_cfg):
                if path not in seen:
                    seen.add(path)
                    expanded.append(path)
        kept, excluded = apply_exclude_globs(expanded, excludes, project_root)
        files = [os.path.relpath(path, cwd) for path in kept]
        log_event(
            logger,
            logging.INFO,
            "verible.model_files",
            models=model_names,
            files=len(files),
            excluded=excluded,
        )
        if not files:
            log_event(
                logger,
                logging.ERROR,
                "verible.model_files_empty",
                models=model_names,
                excluded=excluded,
            )
            raise FatalRtlBuddyError(
                "--model expansion left no source files (every entry was a "
                "-v/-y library file, a +directive, or matched an exclude glob)"
            )
        return files

    def _run_verible_passthrough(
        self,
        cmd: str,
        verible_args: list[str],
        models: list[str] | None = None,
        excludes: list[str] | None = None,
    ):
        """Shared dispatch for the verible passthrough subcommands.

        Runs the configured verible executable with `verible_args`. `models` appends
        the `--model` expansion, filtered by the cfg-verible `exclude` globs plus
        `excludes`. Always exits through `typer.Exit` with the binary's return code.
        """
        self._enter_command_context(command_root=self.invocation_cwd)
        verible_cfg = self.root_cfg.platform_cfg.get_verible()
        if not verible_cfg.available:
            log_event(logger, logging.ERROR, "verible.unavailable")
            raise typer.Exit(2)

        verible_args = list(verible_args)
        if models:
            patterns = list(verible_cfg.exclude) + list(excludes or [])
            verible_args += self._verible_model_files(
                list(models), patterns, self.root_cfg.get_project_rootdir(), cmd
            )
        elif excludes:
            log_event(
                logger,
                logging.WARNING,
                "verible.exclude_without_model",
                patterns=list(excludes),
            )

        ver = Verible(self.name + "/verible", cfg=verible_cfg)
        log_event(
            logger,
            logging.DEBUG,
            "verible.args",
            command=cmd,
            argv=" ".join(verible_args),
        )
        raise typer.Exit(ver.do_cmd(cmd=cmd, verible_args=verible_args))

    _VERIBLE_MODEL_HELP = (
        "Model name from models.yaml whose filelist supplies the files to "
        "visit (repeatable). Bare source entries only: -v/-y library files "
        "and +incdir+/+define+/+libext+ directives are dropped, then the "
        "cfg-verible `exclude` globs and --exclude filter the rest."
    )
    _VERIBLE_EXCLUDE_HELP = (
        "Glob of project-root-relative paths dropped from --model expansion "
        "(repeatable, fnmatch semantics: * also crosses directory "
        "separators). Adds to the cfg-verible `exclude` list."
    )

    def do_verible_lint(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
        models: Annotated[
            list[str],
            typer.Option("--model", help=_VERIBLE_MODEL_HELP),
        ] = [],
        excludes: Annotated[
            list[str],
            typer.Option("--exclude", help=_VERIBLE_EXCLUDE_HELP),
        ] = [],
    ):
        """run verible-verilog-lint"""
        self._run_verible_passthrough(
            "lint", verible_args, models=models, excludes=excludes
        )

    def do_verible_syntax(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
    ):
        """run verible-verilog-syntax"""
        self._run_verible_passthrough("syntax", verible_args)

    def do_verible_format(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
        models: Annotated[
            list[str],
            typer.Option("--model", help=_VERIBLE_MODEL_HELP),
        ] = [],
        excludes: Annotated[
            list[str],
            typer.Option("--exclude", help=_VERIBLE_EXCLUDE_HELP),
        ] = [],
    ):
        """run verible-verilog-format"""
        self._run_verible_passthrough(
            "format", verible_args, models=models, excludes=excludes
        )

    def do_verible_preprocessor(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
    ):
        """run verible-verilog-preprocessor"""
        self._run_verible_passthrough("preprocessor", verible_args)

    def do_verible_filelist(
        self,
        models: Annotated[
            list[str],
            typer.Option(
                "--model",
                help=(
                    "Model name(s) to include. May be repeated. Default: "
                    "union of every model declared in any models.yaml under "
                    "the project root."
                ),
            ),
        ] = [],
        output: Annotated[
            str | None,
            typer.Option(
                "-o",
                "--output",
                help=(
                    "Output path. Defaults to <project_root>/verible.filelist "
                    "so verible-verilog-ls auto-discovers it."
                ),
            ),
        ] = None,
    ):
        """
        generate verible.filelist from models.yaml so verible-verilog-ls can
        resolve cross-file symbols (go-to-definition, hover, references)
        """
        self._enter_command_context(command_root=self.invocation_cwd)
        project_root = self.root_cfg.get_project_rootdir()
        if output is None:
            output = os.path.join(project_root, "verible.filelist")

        selected = self._select_model_configs(models, project_root)

        log_event(
            logger,
            logging.INFO,
            "command.verible_filelist",
            models=[m.name for m in selected],
            output=output,
        )
        vlog_fl = VlogFilelist(
            name=self.name + "/verible_filelist",
            model_cfg=None,
            output_path=output,
        )
        vlog_fl.write_verible_filelist(selected, output_filepath=output)

    def do_cmd_tool_check(
        self,
        fmt: Annotated[
            str,
            typer.Option(
                "--format",
                help="text | json",
                case_sensitive=False,
            ),
        ] = "text",
        required_for: Annotated[
            str | None,
            typer.Option(
                "--required-for",
                help="check only what `rb <subcommand>` needs",
            ),
        ] = None,
        explain_tool: Annotated[
            str | None,
            typer.Option(
                "--explain",
                help="show install instructions for a single tool and exit",
            ),
        ] = None,
        strict: Annotated[
            bool,
            typer.Option(
                "--strict",
                help="exit non-zero if any required tool is missing/outdated",
            ),
        ] = False,
        include_optional: Annotated[
            bool,
            typer.Option(
                "--include-optional/--no-include-optional",
                help="include optional tools (default: yes)",
            ),
        ] = True,
        probe_versions: Annotated[
            bool,
            typer.Option(
                "--probe-versions/--no-probe-versions",
                help="run `<tool> --version` to capture installed version "
                "(default: yes)",
            ),
        ] = True,
    ):
        """
        Detect installed tool dependencies and report subcommand readiness.
        """
        from . import tool_manifest as tm
        from .config.root import _discover_root_cfg

        setup_logging(debug=False, verbose=False, color=True, machine=self.machine)

        # tool-check works outside a project: suppress the "not found" log.
        root_cfg = None
        root_logger = logging.getLogger("rtl_buddy.config.root")
        prev_level = root_logger.level
        root_logger.setLevel(logging.CRITICAL)
        try:
            if _discover_root_cfg() is not None:
                root_cfg = RootConfig(name=self.name + "/tool-check/root_config")
        except FatalRtlBuddyError:
            root_cfg = None
        finally:
            root_logger.setLevel(prev_level)

        specs = tm.get_manifest(root_cfg)
        project_root = (
            Path(root_cfg.get_project_rootdir()) if root_cfg is not None else None
        )

        if explain_tool is not None:
            # Accept aliases such as `rtl-buddy-sch`; output uses the canonical name.
            spec = tm.resolve_spec(specs, explain_tool)
            if spec is None:
                if self.machine:
                    # `known` holds canonical names only; `aliases` is a separate key.
                    self._emit_machine_result(
                        "tool-check",
                        1,
                        error=f"unknown tool '{explain_tool}'",
                        known=[s.name for s in specs],
                        aliases={s.name: list(s.aliases) for s in specs if s.aliases},
                    )
                    raise typer.Exit(1)
                emit_console_text(
                    f"tool-check: unknown tool '{explain_tool}'. "
                    f"Known: {', '.join(tm.known_tool_names(specs))}",
                    style="red",
                    stream="stderr",
                )
                raise typer.Exit(1)
            status = tm.check_tool(
                spec, project_root=project_root, probe_versions=probe_versions
            )
            if self.machine:
                self._emit_machine_result(
                    "tool-check",
                    0,
                    **tm.build_json_payload(
                        [status],
                        tm.subcommand_readiness([status], [spec]),
                    ),
                    instructions=tm.explain(spec, status),
                )
                raise typer.Exit(0)
            # Plain print: Rich word-wrap would mangle paths.
            print(tm.explain(spec, status))
            raise typer.Exit(0)

        statuses = tm.check_all(
            specs,
            project_root=project_root,
            probe_versions=probe_versions,
            include_optional=include_optional or required_for is not None,
        )
        subcommands = tm.subcommand_readiness(statuses, specs)

        if required_for is not None:
            if required_for not in subcommands:
                if self.machine:
                    self._emit_machine_result(
                        "tool-check",
                        0,
                        **tm.build_json_payload([], {}),
                        note=(
                            f"subcommand '{required_for}' has no declared "
                            f"tool dependencies"
                        ),
                    )
                    raise typer.Exit(0)
                emit_console_text(
                    f"tool-check: subcommand '{required_for}' has no "
                    f"declared tool dependencies",
                    style="yellow",
                )
                raise typer.Exit(0)
            subcommands = {required_for: subcommands[required_for]}
            wanted = set(subcommands[required_for]["tools"])
            statuses = [s for s in statuses if s.name in wanted]

        reported_exit_code = tm.compute_exit_code(
            statuses,
            required_for=required_for,
            subcommands=tm.subcommand_readiness(statuses, specs),
        )

        # --required-for enforces (exit 2), --strict enforces the global check (exit 1); otherwise informational.
        envelope_exit = (
            reported_exit_code if (required_for is not None or strict) else 0
        )

        # --machine wins over --format json.
        if self.machine:
            self._emit_machine_result(
                "tool-check",
                envelope_exit,
                **tm.build_json_payload(statuses, subcommands),
                readiness_exit_code=reported_exit_code,
            )
            raise typer.Exit(envelope_exit)

        # Plain print: Rich word-wrap would mangle JSON and the tool table.
        if fmt.lower() == "json":
            print(tm.render_json(statuses, subcommands, exit_code=reported_exit_code))
        else:
            print(
                tm.render_text(statuses, subcommands, include_optional=include_optional)
            )

        if required_for is not None or strict:
            raise typer.Exit(reported_exit_code)
        raise typer.Exit(0)

    def _project_root_for_git(self) -> str | None:
        """Where git metadata queries run, resolved once; None = inherited cwd.

        List-only invocations use the command root, since they skip root_cfg.
        """
        if not self._git_root_resolved:
            self._git_root_resolved = True
            exec_ctx = getattr(self, "exec_ctx", None)
            root_cfg = getattr(self, "root_cfg", None)
            for candidate in (
                root_cfg.get_project_rootdir() if root_cfg is not None else None,
                str(exec_ctx.command_root) if exec_ctx is not None else None,
            ):
                if candidate and os.path.isdir(candidate):
                    self._git_root = candidate
                    break
        return self._git_root

    def _collect_git_status(self) -> dict | None:
        # check=False does not cover a missing git binary (OSError); degrade to None.
        cwd = self._project_root_for_git()
        # --no-optional-locks: reading status must not depend on a stale .git/index.lock.
        try:
            status_result = subprocess.run(
                ["git", "--no-optional-locks", "status", "-sb"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                cwd=cwd,
            )
            commit_result = subprocess.run(
                ["git", "log", "-1", "--pretty=%h"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                cwd=cwd,
            )
        except OSError:
            return None
        if status_result.returncode != 0 or commit_result.returncode != 0:
            return None
        status_lines = status_result.stdout.splitlines()
        branch = status_lines[0][3:].split("...")[0] if status_lines else "unknown"
        file_lines = status_lines[1:]
        mod = sum(1 for ln in file_lines if len(ln) > 1 and ln[1] not in (" ", "?"))
        staged = sum(1 for ln in file_lines if len(ln) > 0 and ln[0] not in (" ", "?"))
        return {
            "branch": branch,
            "commit": commit_result.stdout.strip(),
            "modified": mod,
            "staged": staged,
        }

    def _emit_machine_result(self, command: str, exit_code: int, **payload) -> None:
        git = self._collect_git_status()
        print(
            json.dumps(
                {
                    "command": command,
                    "exit_code": exit_code,
                    "meta": {
                        "rtl_buddy_version": version("rtl-buddy"),
                        "argv": sys.argv[:],
                        "cwd": os.getcwd(),
                        "git": git,
                        # Always present, so null means the flat tree.
                        "run_tag": self._run_tag,
                    },
                    "payload": payload,
                },
                ensure_ascii=True,
            )
        )

    def show_git_rev(self):
        git = self._collect_git_status()
        if git is None:
            logger.debug("git metadata unavailable for banner")
            return
        branch, commit, mod, staged = (
            git["branch"],
            git["commit"],
            git["modified"],
            git["staged"],
        )
        if mod > 0 or staged > 0:
            git_str = f"git: {branch} | commit {commit} | mod {mod} | staged {staged}"
        else:
            git_str = f"git: {branch} | commit {commit} | clean"
        # Machine mode: git status is already in the envelope meta; skip the banner.
        if is_machine_mode():
            log_event(
                logger,
                logging.INFO,
                "git.status",
                branch=branch,
                commit=commit,
                modified=mod,
                staged=staged,
            )
            return
        emit_console_text(git_str, style="dim")
        log_event(
            logger,
            logging.INFO,
            "git.status",
            branch=branch,
            commit=commit,
            modified=mod,
            staged=staged,
        )
