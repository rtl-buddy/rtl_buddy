# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import logging
import os
import subprocess
import sys
import json
from pathlib import Path
import typer
from importlib.metadata import version
from typing_extensions import Annotated
import click

from .config import RegConfig, RootConfig, SuiteConfig, TestConfig
from .config.env_file import apply_env_file
from .config.root import _discover_root_cfg, discover_project_root
from .config.cdc import CdcRegConfig, CdcSuiteConfig
from .config.fpga import FpgaRegConfig, FpgaSuiteConfig
from .config.fpv import FpvRegConfig, FpvSuiteConfig
from .config.mut import MutSuiteConfig
from .config.model import ModelConfig, ModelConfigLoader
from .config.pnr import PnrSuiteConfig
from .config.power import PowerRegConfig, PowerSuiteConfig
from .config.synth import SynthRegConfig, SynthSuiteConfig
from .docs_access import get_page, get_section, list_pages
from .artifact_lock import ArtifactLocks
from .errors import FatalRtlBuddyError, FilelistError
from .exec_context import ExecutionContext
from .logging_utils import (
    attach_file_log,
    emit_console_text,
    is_machine_mode,
    log_event,
    render_summary,
    setup_logging,
)
from .runner.cdc_runner import CdcRunner
from .runner.cdc_results import CdcSkipResults
from .runner.fpv_runner import FpvRunner
from .runner.fpv_results import FpvSkipResults
from .runner.mut_runner import MutRunner
from .runner.mut_results import MutResults
from .runner.test_results import SetupFailResults, SkipResults
from .runner.test_runner import RunDepth, TestRunner
from .runner.xfail import apply_xfail
from .runner.fpga_runner import FpgaRunner
from .runner.fpga_results import FpgaSkipResults
from .runner.pnr_runner import PnrRunner
from .runner.pnr_results import PnrSkipResults
from .runner.power_runner import PowerRunner
from .runner.power_results import PowerSkipResults
from .runner.synth_runner import SynthRunner
from .runner.synth_results import SynthSkipResults
from .seed_mode import SeedMode
from .hub.cli import app as hub_app
from .skill_install import app as skill_app
from .tools.axi_profile_rtl_buddy import (
    RtlBuddyAxiProfileDiscover,
    RtlBuddyAxiProfileGenMonitor,
    RtlBuddyAxiProfileNotebook,
    RtlBuddyAxiProfileRun,
)
from .tools.coverage import CoverageReporter
from .tools.artifact_paths import test_artifact_dir
from .tools.hier_rtl_buddy_view import RtlBuddyView, RtlBuddyViewQuery
from .tools.spec_trace import (
    all_spec_blocks,
    build_coverage_map,
    build_spec_to_models_map,
    discover_model_configs,
    discover_spec_configs,
    discover_suite_tests,
)
from .tools.verible import Verible
from .tools.vlog_filelist import VlogFilelist
from .config.xplr import load_xplr_config
from .xplr import analysis as xplr_analysis
from .xplr import commands as xplr_commands
from .xplr import dumps_record
from .xplr import gitprov as xplr_gitprov
from .xplr import ledger as xplr_ledger
from .xplr import mockflow as xplr_mockflow

logger = logging.getLogger(__name__)


class RtlBuddy:
    """
    RTL Buddy Main Class

    Handles cli entry into RTL Buddy
    """

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
        self.verible_app.command("lint", help="run verible-verilog-lint")(
            self.do_verible_lint
        )
        self.verible_app.command("syntax", help="run verible-verilog-syntax")(
            self.do_verible_syntax
        )
        self.verible_app.command("format", help="run verible-verilog-format")(
            self.do_verible_format
        )
        self.verible_app.command(
            "preprocessor", help="run verible-verilog-preprocessor"
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
        # Back-compat alias for the pre-#272 annotation-only command.
        self.app.command("wave-install-nvim", help="alias for nvim-install")(
            self.do_nvim_install
        )
        self.app.command("synth", help="run synthesis")(self.do_cmd_synth)
        self.app.command("synth-regression", help="run synthesis regression")(
            self.do_synth_regression
        )
        self.app.command("pnr", help="run place-and-route")(self.do_cmd_pnr)
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
                "rb xplr is a bookkeeper, not an optimizer: you declare the "
                "knob deltas you made and the outcomes your flow produced; it "
                "pins the source revision and records everything under "
                "artefacts/xplr/<exp-id>/record.json. Agent-facing: pass the "
                "global --machine flag for a JSON envelope on stdout, and feed "
                "JSON manifests in via --json <file|->"
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
            help="reclaim experiment disk space, non-interactively: evict "
            "heavy artifacts + worktrees per policy (default keep-frontier "
            "never touches Pareto-frontier members or their lineage); "
            "record.json and the pinned sha always survive, so evicted "
            "experiments can be re-materialized",
        )(self.do_xplr_gc)
        self.xplr_mock_app = typer.Typer(
            help=(
                "synthetic DSE backend with known optima (dev/CI harness). "
                "EDA-flavored knobs and metrics over multi-modal benchmark "
                "landscapes (Rastrigin, ZDT1) with feasibility cliffs and a "
                "synthetic cost model — instant, deterministic, license-free, "
                "and self-scoring against the analytic optimum / Pareto front"
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
        self.run_depth = RunDepth.POST
        self.share_build = False
        self.machine = False
        self.invocation_cwd: Path = Path.cwd()
        self.exec_ctx: ExecutionContext | None = None
        self._builder_override: str | None = None
        self._artifact_locks = ArtifactLocks()
        self._xplr_root_override: Path | None = None

    def run(self):
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
                # Machine consumers parse stdout JSON; a silent stdout
                # forces them to scrape stderr for an ad-hoc message.
                # Emit an envelope so the failure surface matches the
                # success surface.
                command = (
                    getattr(self, "_pending_invoked_subcommand", None) or "rtl_buddy"
                )
                self._emit_machine_result(command, 2, error=str(exc))
            return 2
        # standalone_mode=False makes click *return* the exit code from
        # `typer.Exit(code=N)` rather than re-raise it, so we have to
        # surface it here. Existing commands that return None continue
        # to exit cleanly with code 0.
        return rv if isinstance(rv, int) else 0

    # Subcommands that expose a `--list` flag whose only job is to emit
    # configured names from the primary config file. The `--list` paths
    # do not need RootConfig, the selected builder, or CoverageReporter,
    # so list-only invocations short-circuit those setup steps.
    _LIST_FLAG_COMMANDS = {"test", "synth", "pnr", "power", "fpga", "cdc", "fpv"}

    def _is_list_invocation(self, ctx: typer.Context) -> bool:
        return (
            ctx.invoked_subcommand in self._LIST_FLAG_COMMANDS
            and "--list" in sys.argv[1:]
        )

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
        self.invocation_cwd = Path.cwd().resolve()

        if ctx.invoked_subcommand in {"skill", "docs", "spec", "hub", "tool-check"}:
            return

        # Phase 1: console logging only. The file handler is attached in
        # phase 2 once the command's ExecutionContext is known so
        # rtl_buddy.log lands under the command root rather than the
        # invocation directory.
        setup_logging(debug=debug, verbose=verbose, color=color, machine=machine)

        log_event(logger, logging.INFO, "cli.start", version=version("rtl-buddy"))

        if (
            ctx.invoked_subcommand in self._GIT_COMMANDS
            and not self._is_list_invocation(ctx)
        ):
            self.show_git_rev()

        # RootConfig + CoverageReporter construction is deferred to
        # _enter_command_context() so root_config.yaml is discovered by
        # walking up from the command root, not the invocation cwd.
        self.rtl_builder_mode = rtl_builder_mode
        self._builder_override = builder_override
        self.run_depth = run_depth
        self._pending_invoked_subcommand = ctx.invoked_subcommand

    def _enter_command_context(
        self,
        *,
        primary_config: str | Path | None = None,
        command_root: str | Path | None = None,
        list_only: bool = False,
    ) -> ExecutionContext:
        """Build the command's ExecutionContext and attach the file log.

        Pass exactly one of:
        - ``primary_config``: the command's ``-c`` argument (e.g.
          ``tests.yaml``); the command root is its parent directory.
        - ``command_root``: an explicit directory anchor for commands that
          don't have a single primary config file.

        Constructs :attr:`root_cfg` and :attr:`coverage` once the command
        root is known so ``root_config.yaml`` is discovered relative to
        the command rather than the invocation cwd. Subsequent calls
        within the same process re-anchor the file log handler so a
        long-running session (e.g. ``rb regression`` iterating suites)
        keeps each suite's log under its own root.

        ``list_only=True`` skips ``RootConfig``, builder, and
        ``CoverageReporter`` setup. The metadata-only ``--list`` paths
        only need to read the suite config; skipping the root-config
        load keeps them usable when the surrounding project config is
        invalid or unrelated to the listed suite.
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
            )
        else:
            ctx = ExecutionContext.for_dir(
                invocation_cwd=self.invocation_cwd,
                command_root=command_root,
            )

        ctx.command_root.mkdir(parents=True, exist_ok=True)
        attach_file_log(ctx.log_path)
        self.exec_ctx = ctx

        if list_only:
            return ctx

        # Fail loud if another rtl-buddy process is already using this
        # artefact tree (#73). Held until process exit; metadata-only
        # --list paths above stay lock-free.
        self._artifact_locks.acquire(
            ctx.artifact_root,
            command=getattr(self, "_pending_invoked_subcommand", None),
        )

        # Build root_cfg on first entry; on later entries, only rebuild if
        # the new command root walks up to a different root_config.yaml —
        # so regression loops whose suites span project roots get the
        # right tool/platform defaults per suite. Suites that share a
        # root keep the cached instance.
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
            )
            # Project-local env defaults (.rtl-buddy/.env): applied as
            # soon as the project root is known, before any tool config
            # or subprocess reads the environment. Never overrides vars
            # already set, so within one process the first project's
            # values win for cross-root regressions.
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

        return ctx

    def _exit_code_from_results(self, suite_results):
        exit_code = 0
        for suite_result in suite_results:
            exit_code |= 0 if suite_result["results"].is_pass() else 1
        return exit_code

    def _apply_xfail_logged(self, res, cfg, event):
        """Re-interpret one result under cfg's xfail marker, and log it.

        Shared by every command whose per-item config exposes
        ``is_xfail()`` / ``get_xfail_strict()`` (test, fpv, synth, cdc,
        pnr, power). Call only when ``cfg.is_xfail()`` is true.
        """
        observed = res.results.get("result")
        strict = cfg.get_xfail_strict()
        apply_xfail(res, strict=strict)
        log_event(
            logger,
            logging.INFO,
            event,
            name=cfg.get_name(),
            observed=observed,
            reported=res.results.get("result"),
            strict=strict,
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
        for suite_result in suite_results:
            cov_summary = self._format_coverage_summary(suite_result["results"])
            has_coverage |= cov_summary is not None
            assert_summary = self._format_assertions_summary(suite_result["results"])
            has_assertions |= assert_summary is not None
            row = {
                "test_name": suite_result["test_name"],
                "result": suite_result["results"].results["result"],
                "desc": suite_result["results"].results["desc"],
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
        for reg_result in reg_results:
            for suite_result in reg_result["results"]:
                cov_summary = self._format_coverage_summary(suite_result["results"])
                has_coverage |= cov_summary is not None
                assert_summary = self._format_assertions_summary(
                    suite_result["results"]
                )
                has_assertions |= assert_summary is not None
                rows.append(
                    {
                        "suite_name": reg_result["test_suite"],
                        "test_name": suite_result["test_name"],
                        "result": suite_result["results"].results["result"],
                        "desc": suite_result["results"].results["desc"],
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

    def _display_path(self, path: str, *, base_dir: str | None = None) -> str:
        if base_dir is None:
            return path

        try:
            relpath = os.path.relpath(path, base_dir)
        except ValueError:
            return path

        return relpath if len(relpath) < len(path) else path

    def _resolve_coverage_dir_summary_paths(
        self, coverage_dir_summary=None, coverage_dir_summary_file=None
    ):
        """
        Resolve configured coverage directory-summary prefixes from repeated CLI
        options and/or a file containing one path per line.
        """
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
            str, typer.Argument(help="name of test", show_default="run all tests")
        ] = None,
        list_tests: Annotated[
            bool,
            typer.Option(
                "--list", help="list tests in the selected test-config and exit"
            ),
        ] = False,
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
        share_build: Annotated[
            bool,
            typer.Option(
                "--share-build",
                help="reuse one compiled simv across tests with identical compile inputs (Verilator builders only)",
            ),
        ] = False,
    ):
        """
        run a simple test
        """
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
        log_event(
            logger,
            logging.INFO,
            "command.test",
            command="test",
            test=test_name or "all",
            test_config=test_config,
        )

        if list_tests:
            if self.machine:
                self._emit_machine_result(
                    "test --list", 0, names=list(self.suite_cfg.get_test_names())
                )
            else:
                emit_console_text(
                    "  ".join(self.suite_cfg.get_test_names()), stream="stdout"
                )
            raise typer.Exit(0)

        seed_mode: SeedMode = SeedMode.DEFAULT
        replay_run_id = None
        if rnd_new:
            seed_mode = SeedMode.NEW
        elif rnd_last:
            seed_mode = SeedMode.REPLAY
        self.share_build = share_build

        suite_results = self._do_test_suite(
            self.suite_cfg,
            test_name=test_name,
            run_ids=[None],
            seed_mode=seed_mode,
            replay_run_id=replay_run_id,
        )
        dir_summary_paths = self._resolve_coverage_dir_summary_paths(
            coverage_dir_summary=coverage_dir_summary,
            coverage_dir_summary_file=coverage_dir_summary_file,
        )
        metadata = [f"Builder: {self.builder}"]
        metadata.extend(
            self.coverage.build_metadata(
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
            )
        )
        exit_code = self._exit_code_from_results(suite_results)
        if self.machine:
            self._emit_machine_result(
                "test",
                exit_code,
                results=[
                    {
                        "name": r["test_name"],
                        "result": r["results"].results["result"],
                        "desc": r["results"].results["desc"],
                    }
                    for r in suite_results
                ],
            )
        else:
            self._render_test_summary(
                "Test Results Summary", suite_results, metadata=metadata
            )
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
    ):
        """
        repeat a test with multiple random seeds
        """
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
        )

        if rpt_i is not None:
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
                    metadata=[f"Builder: {self.builder}"],
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
                    metadata=[f"Builder: {self.builder}"],
                )

        exit_code = self._exit_code_from_results(suite_results)
        if self.machine:
            self._emit_machine_result(
                "randtest",
                exit_code,
                results=[
                    {
                        "name": r["test_name"],
                        "run_id": r["randmode_i"],
                        "result": r["results"].results["result"],
                        "desc": r["results"].results["desc"],
                    }
                    for r in suite_results
                ],
            )
        raise typer.Exit(exit_code)

    def _append_skip_results(self, test_name, desc, run_ids, suite_results):
        test_results = SkipResults(name=test_name + "/results", desc=desc)
        for run_id in run_ids:
            suite_results.append(
                {"test_name": test_name, "randmode_i": run_id, "results": test_results}
            )

    def _append_setup_results(self, test_name, desc, run_ids, suite_results):
        test_results = SetupFailResults(name=test_name + "/results", desc=desc)
        for run_id in run_ids:
            suite_results.append(
                {"test_name": test_name, "randmode_i": run_id, "results": test_results}
            )

    def _expand_tests_with_sweep(self, test_cfg, suite_dir):
        script_path = test_cfg.get_sweep_path()
        if script_path is None:
            return [test_cfg], None

        with open(script_path, "r") as file:
            code = file.read()

        ns = {
            "logger": logger,
            "TestConfig": TestConfig,
            "test_cfg": test_cfg,
            "root_cfg": self.root_cfg,
            "suite_dir": suite_dir,
            "artifact_dir": str(test_artifact_dir(suite_dir, test_cfg.get_name())),
            "out_test_cfgs": [],
            "__file__": os.path.abspath(script_path),
        }
        try:
            exec(code, ns)
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
        )

        if len(run_ids) == 1:
            results = [test_runner.run()]
        else:
            results = test_runner.run_multiple(run_ids)
        if test_cfg.is_xfail():
            # FAIL->XFAIL (pass) / PASS->XPASS (a failure only when strict)
            # so a known-failing test can live in a suite/regression.
            for res in results:
                self._apply_xfail_logged(res, test_cfg, "suite.xfail")
        return results

    def _append_results(self, test_name, run_ids, results, suite_results):
        for run_id, test_results in zip(run_ids, results):
            suite_results.append(
                {"test_name": test_name, "randmode_i": run_id, "results": test_results}
            )

    def _format_coverage_summary(self, test_results):
        return self.coverage.format_summary(test_results)

    @staticmethod
    def _format_assertions_summary(test_results):
        """Return a short Assertions cell, or None when the test didn't enable SVA.

        Shape: `"<fired> fired"` so the column doubles as a hit-counter and a
        pass/fail signal (anything > 0 fired is a FAIL already reflected in the
        Result column).
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
    ):

        if run_ids is None:
            run_ids = [None]

        tests = suite_cfg.get_tests(test_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        suite_results = []
        for t in tests:
            t_lvl = t.get_reglvl(self.builder)
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
                )
                continue

            expanded_tests, sweep_error = self._expand_tests_with_sweep(
                t, suite_dir=suite_dir
            )
            if sweep_error is not None:
                self._append_setup_results(t.name, sweep_error, run_ids, suite_results)
                continue

            for expanded_test_cfg in expanded_tests:
                run_results = self._run_test_cfg_for_run_ids(
                    test_cfg=expanded_test_cfg,
                    run_ids=run_ids,
                    seed_mode=seed_mode,
                    replay_run_id=replay_run_id,
                    test_runner_mode=test_runner_mode,
                    suite_dir=suite_dir,
                )
                self._append_results(
                    expanded_test_cfg.name, run_ids, run_results, suite_results
                )
        return suite_results

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
        share_build: Annotated[
            bool,
            typer.Option(
                "--share-build",
                help="reuse one compiled simv across tests with identical compile inputs (Verilator builders only)",
            ),
        ] = False,
    ):
        """
        run rtl regression
        """
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
        log_event(
            logger,
            logging.INFO,
            "command.regression",
            reg_config=reg_config,
            reg_level=reg_level,
            start_level=start_level,
            share_build=share_build,
        )

        start_dir = str(self.invocation_cwd)
        if reg_config is not None:
            resolved_reg_config = str(
                (self.invocation_cwd / reg_config).resolve()
                if not os.path.isabs(reg_config)
                else Path(reg_config).resolve()
            )
            # Anchor the orchestration to dirname(regression.yaml). Each
            # suite below will re-anchor to its own tests.yaml directory.
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
                # Defer to root_config.yaml — its reg-cfg-path is anchored
                # to the root config directory; use that as the command root.
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
                )

        reg_dir = os.path.dirname(self.reg_cfg.get_path())
        emit_console_text(f"Running regression from {reg_dir}", style="cyan")

        exit_code = 0
        reg_results = []
        # Per-suite ExecutionContext re-anchors the file log under each
        # tests.yaml directory. The process CWD is intentionally not
        # changed; the test runner already passes suite_dir explicitly to
        # every consumer.
        orchestration_ctx = ctx
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
                seed_mode=SeedMode.DEFAULT,
                replay_run_id=None,
            )
            reg_results.append(
                {
                    "test_suite": self._display_path(
                        suite_cfg.get_path(), base_dir=start_dir
                    ),
                    # Absolute suite dir — used as the coverage source_root.
                    # Avoid recombining the display path with command_root,
                    # which breaks when invocation cwd differs from
                    # command_root.
                    "test_suite_path": str(Path(suite_cfg.get_path()).resolve().parent),
                    "results": suite_results,
                }
            )
            exit_code |= self._exit_code_from_results(suite_results)
        # Re-anchor the orchestration log to the regression root for the
        # summary phase so coverage merge artifacts and final summary land
        # next to regression.yaml.
        self._enter_command_context(command_root=orchestration_ctx.command_root)
        ctx = orchestration_ctx

        all_suite_results = []
        for reg_result in reg_results:
            all_suite_results.extend(reg_result["results"])

        metadata = [
            f"Builder: {self.builder}",
            f"Builder Mode: {self.rtl_builder_mode}",
        ]
        dir_summary_paths = self._resolve_coverage_dir_summary_paths(
            coverage_dir_summary=coverage_dir_summary,
            coverage_dir_summary_file=coverage_dir_summary_file,
        )
        if (
            coverage_html
            and not coverage_merge
            and not coverage_merge_raw
            and not coverage_merge_info_process
        ):
            reg_outdir = str(ctx.command_root)
            for reg_result in reg_results:
                metadata.extend(
                    self.coverage.build_metadata(
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
                    )
                )
        else:
            reg_outdir = str(ctx.command_root)
            regression_source_roots = [
                reg_result["test_suite_path"] for reg_result in reg_results
            ]
            metadata.extend(
                self.coverage.build_metadata(
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
                )
            )

        if self.machine:
            self._emit_machine_result(
                "regression",
                exit_code,
                results=[
                    {
                        "suite": reg_result["test_suite"],
                        "name": suite_result["test_name"],
                        "result": suite_result["results"].results["result"],
                        "desc": suite_result["results"].results["desc"],
                    }
                    for reg_result in reg_results
                    for suite_result in reg_result["results"]
                ],
            )
        else:
            self._render_regression_summary(reg_results, metadata=metadata)
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
            # The test pins both a model and a TB top — resolve via
            # the existing SuiteConfig loader so the same parse path
            # the simulator uses runs here too.
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
                executable=tool,
                test_cfg=test_cfg,
            )
            raise typer.Exit(runner.run())

        # --view dut (default): anchor on models.yaml.
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
        query the module hierarchy via rtl-buddy-view (rb hier's
        machine-readable sibling): JSON answers on stdout for shell
        pipelines and agent tool use; source-snippet emits
        line-number-prefixed citation text
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

        Looks up `<test>` in tests.yaml, resolves the model, the
        checked-in axi-bundles.yaml manifest (model.axi_bundles in
        models.yaml), and the FST at artefacts/<test>/dump.fst, then
        invokes axi-profiler run. Pass --emit-txns-parquet to also
        produce the per-transaction parquet artefact that
        `rb axi-profile notebook` consumes.
        """
        ctx = self._enter_command_context(primary_config=test_config)
        suite_cfg = SuiteConfig(str(ctx.primary_config))
        test_cfg = suite_cfg.get_tests(test_name)[0]
        # Resolve parquet emit:
        #   explicit path → use it
        #   bare --emit-txns-parquet → empty-string sentinel → wrapper picks default
        #   neither → None → no emit (legacy behaviour)
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

        Reads the manifest path from `model.axi_bundles` and the
        output path from `model.axi_monitor_out` (both in
        models.yaml). The generated SV must be added to the
        testbench's filelist; pointing `axi_monitor_out:` at the
        verif tree (e.g. `../verif/<tb>/gen/axi_perf_mon.sv`) makes
        that a one-time step.
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
                    "accepted but currently falls back to foreground; "
                    "background detach is a follow-up."
                ),
            ),
        ] = True,
        headless: Annotated[
            bool,
            typer.Option(
                "--headless",
                help=(
                    "Forward `--headless --no-token` to marimo. Used by the "
                    "hub-initiated 'Open in marimo' flow (Phase 2 of the "
                    "marimo umbrella) — the SPA opens the URL itself, so "
                    "marimo shouldn't auto-pop a browser and the auth "
                    "token is disabled for the loopback-only handoff."
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

        Resolves the per-test parquet at
        artefacts/axi/<test>/axi-txns.parquet (produced by
        `rb axi-profile run <test> --emit-txns-parquet`), locates the
        notebook template shipped with the axi-profiler wheel, and
        spawns `marimo edit <template>` with $AXI_TXNS_PARQUET set so
        the template's first cell loads the parquet automatically.
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
        suite_tests = (
            discover_suite_tests(search_verif) if os.path.isdir(search_verif) else []
        )
        blocks = all_spec_blocks(specs)

        if not blocks:
            emit_console_text("No spec blocks found.", style="yellow")
            raise typer.Exit(0)

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
            self._emit_machine_result("spec check-coverage", 0, items=items_out)
            raise typer.Exit(0)

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
        uncovered = [row["id"] for row in rows if row["covered"] == "no"]
        if uncovered:
            emit_console_text(
                f"Uncovered items: {', '.join(uncovered)}", style="yellow"
            )
        raise typer.Exit(0)

    def _synth_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["synth_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in ("gate_count", "area_um2", "wns_ps", "tns_ps"):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _pnr_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["pnr_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in ("cell_count", "area_um2", "wns_setup_ps", "wns_hold_ps", "drc_count"):
            if k in res and res[k] is not None:
                row[k] = res[k]
        return row

    def _power_result_row(self, r, *, suite: str | None = None) -> dict:
        res = r["results"].results
        row = {"name": r["power_name"], "result": res["result"], "desc": res["desc"]}
        if suite is not None:
            row["suite"] = suite
        for k in ("mode", "total_w", "internal_w", "switching_w", "leakage_w"):
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
        # bitstream rides through even when None: machine consumers can
        # tell "bitgen not requested" apart from a pre-fpga payload.
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
        self, suite_cfg, synth_name=None, reg_level=None, effort_override=None
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
            suite_cfg, synth_name=synth_name, effort_override=effort
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
                help="name of pnr run", show_default="run all entries in the suite"
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
    ):
        """run place-and-route"""
        ctx = self._enter_command_context(
            primary_config=pnr_config, list_only=list_runs
        )
        suite_cfg = PnrSuiteConfig(path=str(ctx.primary_config))
        if emit_png:
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
        )
        exit_code = 0 if all(r["results"].is_pass() for r in results) else 1
        if self.machine:
            self._emit_machine_result(
                "pnr",
                exit_code,
                results=[self._pnr_result_row(r) for r in results],
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
    ):
        root_cfg = self.root_cfg
        runs = suite_cfg.get_runs(pnr_name)
        suite_dir = str(Path(suite_cfg.get_path()).resolve().parent)
        results = []
        for run in runs:
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
            runner = PnrRunner(
                name=run.get_name(),
                root_cfg=root_cfg,
                pnr_cfg=run,
                suite_dir=suite_dir,
                reglvl_filter=reg_level if reg_level else None,
                emit_gds=emit_gds,
                emit_png=emit_png,
            )
            res = runner.run()
            if run.is_xfail():
                self._apply_xfail_logged(res, run, "pnr_suite.xfail")
            results.append({"pnr_name": run.get_name(), "results": res})
        return results

    def _render_pnr_summary(self, title, pnr_results, *, metadata=None):
        has_cells = any("cell_count" in r["results"].results for r in pnr_results)
        has_area = any("area_um2" in r["results"].results for r in pnr_results)
        has_setup = any("wns_setup_ps" in r["results"].results for r in pnr_results)
        has_hold = any("wns_hold_ps" in r["results"].results for r in pnr_results)
        has_drcs = any("drc_count" in r["results"].results for r in pnr_results)
        has_outputs = any(
            "gds_path" in r["results"].results or "png_path" in r["results"].results
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
            if has_drcs:
                drcs = res.get("drc_count")
                row["drcs"] = str(drcs) if drcs is not None else "-"
            if has_outputs:
                tags = []
                if res.get("gds_path"):
                    tags.append("gds")
                if res.get("png_path"):
                    tags.append("png")
                row["outputs"] = "+".join(tags) if tags else "-"
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
        has_total = any("total_w" in r["results"].results for r in power_results)
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
            if has_total:
                row["total"] = _fmt_w(res.get("total_w"))
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
        if has_total:
            columns.append(("total", "Total"))
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

    def do_fpga_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to fpga_regression.yaml",
                show_default="Use ./fpga_regression.yaml if present",
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

        if reg_config is not None:
            reg_cfg_path = (
                reg_config
                if os.path.isabs(reg_config)
                else str(self.invocation_cwd / reg_config)
            )
        else:
            local = str(self.invocation_cwd / "fpga_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "fpga_regression.yaml not found; pass -c to specify a path"
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
                show_default="Use ./power_regression.yaml if present",
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

        if reg_config is not None:
            reg_cfg_path = (
                reg_config
                if os.path.isabs(reg_config)
                else str(self.invocation_cwd / reg_config)
            )
        else:
            local = str(self.invocation_cwd / "power_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "power_regression.yaml not found; pass -c to specify a path"
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
                show_default="Use ./synth_regression.yaml if present",
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

        if reg_config is not None:
            reg_cfg_path = (
                reg_config
                if os.path.isabs(reg_config)
                else str(self.invocation_cwd / reg_config)
            )
        else:
            local = str(self.invocation_cwd / "synth_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "synth_regression.yaml not found; pass -c to specify a path"
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

    # --- CDC subcommands ----------------------------------------------------

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
        """Resolve a single rtl-buddy-cdc analysis, run it requesting the
        structured maps, and return ``(analysis, backend, res)``.

        Shared by ``--emit-constraints`` (#291) and ``--check-xdc`` (#290);
        ``mode`` names the flag for error messages.
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
            # Both modes use the open engine's structured crossing map; the
            # vendor backend has no such map.
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
        """`rb cdc --emit-constraints`: generate scoped CDC timing exceptions
        (#291) from rtl-buddy-cdc's verified crossing + reset-sync maps."""
        from .tools.cdc_constraints import generate_constraints

        analysis, backend, res = self._run_single_cdc_with_maps(
            ctx, cdc_config, cdc_name, "--emit-constraints"
        )
        domain_map, reset_map = backend.read_emitted_maps()

        if domain_map is None:
            # No map can mean two very different things; don't collapse them
            # into a single exit-0 SKIP.
            #   (a) the analysis itself failed -> surface it as a failure;
            #   (b) it ran but the (optional/old) tool emitted no map -> SKIP.
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
        rtl-buddy-cdc's verified crossing set (#290)."""
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
        xc = extract_cdc_constraints(Path(xdc_path).read_text())
        # Recognized-synchronizer patterns: cdc.yaml's `recognized-syncs` plus
        # any --recognize-sync overrides given on the command line.
        recognized = analysis.get_recognized_syncs() + list(recognize_sync or [])
        audit = audit_xdc(domain_map, report, xc, recognized_syncs=recognized)
        blockers = audit.blockers
        # A completeness gap or a dangerous over-waive fails the audit; the
        # softer findings (bus-skew / clock-graph) are warnings.
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
                show_default="Use ./cdc_regression.yaml if present",
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

        if reg_config is not None:
            reg_cfg_path = (
                reg_config
                if os.path.isabs(reg_config)
                else str(self.invocation_cwd / reg_config)
            )
        else:
            local = str(self.invocation_cwd / "cdc_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "cdc_regression.yaml not found; pass -c to specify a path"
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

    # --- FPV subcommands ----------------------------------------------------

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
        """Return a short Vacuity cell, or None if the run didn't do a vacuity pass.

        Shape: `"<vacuous>/<total> vacuous"` so the column is silent when
        every antecedent is reachable, loud when it isn't.
        """
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
        """Return a short COI cell, or None when no COI data was produced.

        Shape: `"<percent>% (<coi>/<total>)"` so the column carries both
        the rolled-up ratio and the raw counts behind it. A coverage of
        100% is still surfaced so the user sees the pass came with full
        structural reach.
        """
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
        """Return a short Assumes cell ('N used, M dead'), or None if N/A.

        Built from the COI pass's $assume cell counts. Silent when the
        design has no $assume cells at all (`0 used, 0 dead` is just
        clutter). Loud when any are dead so the user knows to look.
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
                # FAIL->XFAIL (pass) / PASS->XPASS (a failure only when
                # strict) so an expected-fail verification can live in a
                # regression.
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
        if self.machine:
            self._emit_machine_result(
                "fpv",
                exit_code,
                results=[self._fpv_result_row(r) for r in fpv_results],
            )
        else:
            self._render_fpv_summary("FPV Results Summary", fpv_results)
        raise typer.Exit(exit_code)

    def do_fpv_regression(
        self,
        reg_config: Annotated[
            str,
            typer.Option(
                "-c",
                "--reg-config",
                help="path to fpv_regression.yaml",
                show_default="Use ./fpv_regression.yaml if present",
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

        if reg_config is not None:
            reg_cfg_path = (
                reg_config
                if os.path.isabs(reg_config)
                else str(self.invocation_cwd / reg_config)
            )
        else:
            local = str(self.invocation_cwd / "fpv_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "fpv_regression.yaml not found; pass -c to specify a path"
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
        if self.machine:
            self._emit_machine_result("fpv-regression", exit_code, results=machine_rows)
        else:
            self._render_fpv_summary(
                "FPV Regression Summary",
                all_results,
                metadata=[f"Reg Level: {reg_level}"],
            )
        raise typer.Exit(exit_code)

    # --- mutation testing (rb mut) -----------------------------------------

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

    # ------------------------------------------------------------------
    # rb xplr — design-space exploration experiment ledger
    # ------------------------------------------------------------------

    def _xplr_group_options(
        self,
        ctx: typer.Context,
        root: Annotated[
            str,
            typer.Option(
                "--root",
                help="anchor project-root discovery at this path instead of "
                "the current directory (root_config.yaml/.git are resolved "
                "from here). Group-level: place it between 'xplr' and the "
                "subcommand, e.g. `rb xplr --root <project> list`. For "
                "driving a ledger from outside its project checkout",
            ),
        ] = None,
    ):
        """Group callback: ``--root`` + the full ``xplr <sub>`` command name.

        ``root_options`` only sees the group (``xplr``), so the exit-2
        machine envelope emitted by :meth:`run` would attribute errors
        to the bare group name while the success path reports the full
        subcommand (e.g. ``xplr frontier``). Refining it here keeps the
        error surface consistent with the success surface.
        """
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

        The ledger lives at the project root (``artefacts/xplr``), not a
        suite directory, so every experiment ends up in one ledger no
        matter where the agent invoked ``rb`` from. ``rb xplr --root
        <path>`` anchors the discovery at that path instead of the
        invocation cwd. ``list_only=True``: xplr needs no
        RootConfig/builder, and read commands stay lock-free; write
        commands take a lock on the ledger root only, so a running
        flow's suite artefact lock is never contended.
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

    # ------------------------------------------------------------------
    # rb xplr analysis — frontier / diff / knob-effect (curation only)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # rb xplr provenance — worktree isolation + frontier-aware gc (#298)
    # ------------------------------------------------------------------

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
        xplr_commands.get_experiment(root, exp_id)  # fail loudly on unknown id
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

    # ------------------------------------------------------------------
    # rb xplr mock — synthetic DSE backend with known optima (#304)
    # ------------------------------------------------------------------

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
        # `outcome` is shaped exactly as an attach-outcome --json input so
        # a stateless `mock run` can be piped straight into attach-outcome.
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
            str, typer.Option("--surfer", help="cfg-surfer entry name")
        ] = "surfer-default",
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
        from .tools.wave_launcher import WaveLauncher

        self.rtl_builder_mode = "debug"

        ctx = self._enter_command_context(primary_config=test_config)

        surfer_cfg = self.root_cfg.get_surfer_cfg(surfer_name)
        if surfer_cfg is None:
            raise FatalRtlBuddyError(
                f'No cfg-surfer entry named "{surfer_name}" in root_config.yaml. '
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

        fst_path = os.path.join(suite_dir, "artefacts", test_name, "dump.fst")
        surfer_file = os.path.join(suite_dir, f"{test_name}.surfer")

        log_event(
            logger,
            logging.INFO,
            "command.wave",
            command="wave",
            test=test_name,
            fst=fst_path,
        )

        if resim or not os.path.isfile(fst_path):
            log_event(
                logger, logging.INFO, "wave.sim_required", test=test_name, fst=fst_path
            )
            suite_results = self._do_test_suite(
                suite_cfg, test_name=test_name, run_ids=[None]
            )
            result = suite_results[0]["results"] if suite_results else None
            if result is None or not result.is_pass():
                raise FatalRtlBuddyError(
                    f'Debug sim for "{test_name}" failed; cannot open waveform.'
                )
        else:
            log_event(
                logger, logging.INFO, "wave.fst_found", test=test_name, fst=fst_path
            )

        WaveLauncher(
            test_cfg=test_cfg,
            surfer_cfg=surfer_cfg,
            suite_dir=suite_dir,
            fst_path=fst_path,
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
            str, typer.Option("--surfer", help="cfg-surfer entry name")
        ] = "surfer-default",
    ):
        """
        open SymbiYosys counterexample VCD for a failed FPV verification

        Resolves the CEX VCD by convention at
        ``fpv/<suite>/artefacts/<verif>/sby_workdir/engine_<N>/trace.vcd`` and
        opens it in the configured surfer. Raises if the verification has
        not been run, the proof passed (no CEX produced), or no engine
        emitted a trace.
        """
        from .tools.fpv_cex_finder import find_cex_vcd

        ctx = self._enter_command_context(primary_config=fpv_config)

        surfer_cfg = self.root_cfg.get_surfer_cfg(surfer_name)
        if surfer_cfg is None:
            raise FatalRtlBuddyError(
                f'No cfg-surfer entry named "{surfer_name}" in root_config.yaml. '
                f"Add a cfg-surfer section to enable waveform viewing."
            )
        if not surfer_cfg.available:
            raise FatalRtlBuddyError(
                f'Surfer not found at "{surfer_cfg.path}". '
                f"Check cfg-surfer.path in root_config.yaml or install surfer on PATH."
            )

        suite_cfg = FpvSuiteConfig(path=str(ctx.primary_config))
        suite_dir = str(ctx.command_root)
        # Validate the verification name resolves; raises FatalRtlBuddyError otherwise.
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
        install/update the unified rtl-buddy-nvim editor plugin (hub + wave annotation)

        Clones the pinned, hub-compatible rtl-buddy-nvim revision into the nvim
        pack dir and writes a managed setup file that auto-connects to the hub and
        renders rb wave signal-value annotations — no manual git clone or init.lua
        edits. (rb wave-install-nvim is a back-compat alias for this command.)
        """
        from .tools.nvim_install import install

        install(force=force, update=update, ref=ref, source=source, lsp=not no_lsp)

    def do_lint(self):
        assert False, "not yet impl"

    def do_export(self):
        assert False, "not yet impl"

    def do_gen_vlog_run_script(self):
        assert False, "not yet impl"

    def _run_verible_passthrough(self, cmd: str, verible_args: list[str]):
        """Shared dispatch for the verible passthrough subcommands.

        Resolves the configured verible executable via root_config and
        invokes it with the trailing ``verible_args``. Always exits via
        ``typer.Exit`` so the binary's return code propagates.
        """
        self._enter_command_context(command_root=self.invocation_cwd)
        verible_cfg = self.root_cfg.platform_cfg.get_verible()
        if not verible_cfg.available:
            log_event(logger, logging.ERROR, "verible.unavailable")
            raise typer.Exit(2)

        ver = Verible(self.name + "/verible", cfg=verible_cfg)
        log_event(
            logger,
            logging.DEBUG,
            "verible.args",
            command=cmd,
            argv=" ".join(verible_args),
        )
        raise typer.Exit(ver.do_cmd(cmd=cmd, verible_args=verible_args))

    def do_verible_lint(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
    ):
        """run verible-verilog-lint"""
        self._run_verible_passthrough("lint", verible_args)

    def do_verible_syntax(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
    ):
        """run verible-verilog-syntax"""
        self._run_verible_passthrough("syntax", verible_args)

    def do_verible_format(
        self,
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
    ):
        """run verible-verilog-format"""
        self._run_verible_passthrough("format", verible_args)

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

        all_entries = discover_model_configs(project_root)
        if not all_entries:
            log_event(
                logger,
                logging.ERROR,
                "verible_filelist.no_models_discovered",
                project_root=project_root,
            )
            raise FatalRtlBuddyError(f"no models.yaml files found under {project_root}")

        if models:
            by_name: dict[str, ModelConfig] = {}
            for _, model in all_entries:
                # First-found wins on duplicate names across files. Within a
                # single models.yaml, ModelConfigLoader already rejects dupes.
                by_name.setdefault(model.name, model)
            missing = [name for name in models if name not in by_name]
            if missing:
                log_event(
                    logger,
                    logging.ERROR,
                    "verible_filelist.unknown_models",
                    models=missing,
                    available=sorted(by_name),
                )
                raise FatalRtlBuddyError(f"unknown model(s): {', '.join(missing)}")
            selected = [by_name[name] for name in models]
        else:
            selected = [model for _, model in all_entries]

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

        # Opportunistic root_config discovery. tool-check must work outside a
        # project, so we suppress the "not found" error log entirely.
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
            spec = next((s for s in specs if s.name == explain_tool), None)
            if spec is None:
                if self.machine:
                    self._emit_machine_result(
                        "tool-check",
                        1,
                        error=f"unknown tool '{explain_tool}'",
                        known=[s.name for s in specs],
                    )
                    raise typer.Exit(1)
                emit_console_text(
                    f"tool-check: unknown tool '{explain_tool}'. "
                    f"Known: {', '.join(s.name for s in specs)}",
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
            # Plain stdout — Rich's word-wrap would mangle paths.
            print(tm.explain(spec, status))
            raise typer.Exit(0)

        statuses = tm.check_all(
            specs,
            project_root=project_root,
            probe_versions=probe_versions,
            include_optional=include_optional,
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

        # --required-for always enforces (exit 2 on miss); --strict enforces
        # the global "any required tool missing" check (exit 1). Without
        # either flag the command is purely informational.
        envelope_exit = (
            reported_exit_code if (required_for is not None or strict) else 0
        )

        # The global --machine flag wins: emit a single JSON envelope on
        # stdout like every other command (SKILL.md's top rule). The
        # command-specific --format json is kept for back-compat / non-machine
        # callers who want the bare manifest dict.
        if self.machine:
            self._emit_machine_result(
                "tool-check",
                envelope_exit,
                **tm.build_json_payload(statuses, subcommands),
                readiness_exit_code=reported_exit_code,
            )
            raise typer.Exit(envelope_exit)

        # Use raw stdout — Rich's word-wrap would mangle JSON and break the
        # alignment of the tool table.
        if fmt.lower() == "json":
            print(tm.render_json(statuses, subcommands, exit_code=reported_exit_code))
        else:
            print(
                tm.render_text(statuses, subcommands, include_optional=include_optional)
            )

        if required_for is not None or strict:
            raise typer.Exit(reported_exit_code)
        raise typer.Exit(0)

    def _collect_git_status(self) -> dict | None:
        status_result = subprocess.run(
            ["git", "status", "-sb"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        commit_result = subprocess.run(
            ["git", "log", "-1", "--pretty=%h"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
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
        # The git status already rides inside every machine-mode JSON
        # envelope via _emit_machine_result.meta.git — skip the human
        # banner so machine consumers don't see redundant stderr noise.
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
