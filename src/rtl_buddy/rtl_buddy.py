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
from .config.cdc import CdcRegConfig, CdcSuiteConfig
from .config.fpv import FpvRegConfig, FpvSuiteConfig
from .config.model import ModelConfigLoader
from .config.pnr import PnrSuiteConfig
from .config.power import PowerRegConfig, PowerSuiteConfig
from .config.synth import SynthRegConfig, SynthSuiteConfig
from .docs_access import get_page, get_section, list_pages
from .errors import FatalRtlBuddyError, FilelistError
from .logging_utils import (
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
from .runner.test_results import SetupFailResults, SkipResults
from .runner.test_runner import RunDepth, TestRunner
from .runner.pnr_runner import PnrRunner
from .runner.pnr_results import PnrSkipResults
from .runner.power_runner import PowerRunner
from .runner.power_results import PowerSkipResults
from .runner.synth_runner import SynthRunner
from .runner.synth_results import SynthSkipResults
from .seed_mode import SeedMode
from .skill_install import app as skill_app
from .tools.coverage import CoverageReporter
from .tools.artifact_paths import test_artifact_dir
from .tools.hier_rtl_buddy_view import RtlBuddyView
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
        "synth",
        "synth-regression",
        "power",
        "power-regression",
        "cdc",
        "cdc-regression",
        "fpv",
        "fpv-regression",
        "hier",
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
        self.app.command("verible", help="run verible cmd")(self.do_verible)
        self.app.command("wave", help="open waveform viewer for a test")(
            self.do_cmd_wave
        )
        self.app.command(
            "wave-install-nvim", help="install nvim plugin for rb wave annotation"
        )(self.do_wave_install_nvim)
        self.app.command("synth", help="run synthesis")(self.do_cmd_synth)
        self.app.command("synth-regression", help="run synthesis regression")(
            self.do_synth_regression
        )
        self.app.command("pnr", help="run place-and-route")(self.do_cmd_pnr)
        self.app.command("power", help="run power analysis")(self.do_cmd_power)
        self.app.command("power-regression", help="run power analysis regression")(
            self.do_power_regression
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

        if "." not in os.environ["PATH"].split(os.pathsep):
            os.environ["PATH"] = "." + os.pathsep + os.environ["PATH"]

        self.name = name
        self.rtl_builder_mode = None
        self.builder = None
        self.root_cfg = None
        self.coverage = None
        self.run_depth = RunDepth.POST
        self.machine = False

    def run(self):
        try:
            self.app(standalone_mode=False)
        except click.exceptions.Exit as exc:
            return exc.exit_code
        except click.exceptions.Abort:
            return 1
        except click.ClickException as exc:
            exc.show(file=sys.stderr)
            return exc.exit_code
        except (FatalRtlBuddyError, FilelistError) as exc:
            emit_console_text(str(exc), style="red")
            return 2
        return 0

    def _is_test_list_invocation(self, ctx: typer.Context) -> bool:
        return ctx.invoked_subcommand == "test" and "--list" in sys.argv[1:]

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

        if ctx.invoked_subcommand in {"skill", "docs", "spec"}:
            return

        setup_logging(debug=debug, verbose=verbose, color=color, machine=machine)

        log_event(logger, logging.INFO, "cli.start", version=version("rtl-buddy"))

        if (
            ctx.invoked_subcommand in self._GIT_COMMANDS
            and not self._is_test_list_invocation(ctx)
        ):
            self.show_git_rev()

        self.rtl_builder_mode = rtl_builder_mode
        self.root_cfg = RootConfig(
            name=self.name + "/root_config", builder_override=builder_override
        )
        self.builder = self.root_cfg.get_builder_name()
        self.coverage = CoverageReporter(self.root_cfg)
        self.run_depth = run_depth

        log_event(
            logger,
            logging.DEBUG,
            "cli.context_ready",
            command=ctx.invoked_subcommand,
            builder=self.builder,
            builder_mode=self.rtl_builder_mode,
            run_depth=self.run_depth.value,
        )

    def _exit_code_from_results(self, suite_results):
        exit_code = 0
        for suite_result in suite_results:
            exit_code |= 0 if suite_result["results"].is_pass() else 1
        return exit_code

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
        for suite_result in suite_results:
            cov_summary = self._format_coverage_summary(suite_result["results"])
            has_coverage |= cov_summary is not None
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
            rows.append(row)

        columns = [("test_name", "Test")]
        if include_run_id:
            columns.append(("run_id", "Run"))
        columns.extend([("result", "Result"), ("desc", "Description")])
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
        for reg_result in reg_results:
            for suite_result in reg_result["results"]:
                cov_summary = self._format_coverage_summary(suite_result["results"])
                has_coverage |= cov_summary is not None
                rows.append(
                    {
                        "suite_name": reg_result["test_suite"],
                        "test_name": suite_result["test_name"],
                        "result": suite_result["results"].results["result"],
                        "desc": suite_result["results"].results["desc"],
                        "coverage": cov_summary or "",
                    }
                )

        columns = [
            ("suite_name", "Suite"),
            ("test_name", "Test"),
            ("result", "Result"),
            ("desc", "Description"),
        ]
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
        self.suite_cfg = SuiteConfig(path=test_config)
        log_event(
            logger,
            logging.INFO,
            "command.test",
            command="test",
            test=test_name or "all",
            test_config=test_config,
        )

        if list_tests:
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
                outdir=os.getcwd(),
                suite_name=self.suite_cfg.get_path(),
                coverage_merge=coverage_merge,
                coverage_merge_raw=coverage_merge_raw,
                coverage_html=coverage_html,
                coverage_coverview=coverage_coverview,
                coverage_merge_info_process=coverage_merge_info_process,
                source_roots=[os.getcwd()],
                dir_summary_paths=dir_summary_paths,
            )
        )
        self._render_test_summary(
            "Test Results Summary", suite_results, metadata=metadata
        )
        raise typer.Exit(self._exit_code_from_results(suite_results))

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
        self.suite_cfg = SuiteConfig(path=test_config)

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
            self._render_test_summary(
                "RandTest Results Summary",
                suite_results,
                include_run_id=True,
                metadata=[f"Builder: {self.builder}"],
            )

        raise typer.Exit(self._exit_code_from_results(suite_results))

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
        )

        if len(run_ids) == 1:
            return [test_runner.run()]
        return test_runner.run_multiple(run_ids)

    def _append_results(self, test_name, run_ids, results, suite_results):
        for run_id, test_results in zip(run_ids, results):
            suite_results.append(
                {"test_name": test_name, "randmode_i": run_id, "results": test_results}
            )

    def _format_coverage_summary(self, test_results):
        return self.coverage.format_summary(test_results)

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
        log_event(
            logger,
            logging.INFO,
            "command.regression",
            reg_config=reg_config,
            reg_level=reg_level,
            start_level=start_level,
        )

        start_dir = os.getcwd()
        if reg_config is not None:
            self.reg_cfg = RegConfig(
                name=self.name + "/reg_config", path=os.path.join(start_dir, reg_config)
            )
            log_event(
                logger, logging.INFO, "regression.config_override", path=reg_config
            )
        else:
            local_reg_config = os.path.join(start_dir, "regression.yaml")
            if os.path.isfile(local_reg_config):
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
                self.reg_cfg = self.root_cfg.get_rtl_reg_cfg()
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
        try:
            for suite_cfg in self.reg_cfg.get_suite_configs():
                suite_cfg_dir = os.path.dirname(suite_cfg.get_path())
                log_event(
                    logger,
                    logging.INFO,
                    "regression.suite_start",
                    suite=suite_cfg.get_path(),
                    cwd=suite_cfg_dir,
                )
                os.chdir(suite_cfg_dir)
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
                        "results": suite_results,
                    }
                )
                exit_code |= self._exit_code_from_results(suite_results)
        finally:
            os.chdir(start_dir)

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
            for reg_result in reg_results:
                metadata.extend(
                    self.coverage.build_metadata(
                        reg_result["results"],
                        outdir=start_dir,
                        suite_name=reg_result["test_suite"],
                        coverage_merge=False,
                        coverage_merge_raw=False,
                        coverage_html=True,
                        coverage_coverview=coverage_coverview,
                        coverage_per_test=coverage_per_test,
                        reg_results=reg_results,
                        coverage_merge_info_process=coverage_merge_info_process,
                        source_roots=[
                            os.path.dirname(
                                os.path.join(start_dir, reg_result["test_suite"])
                            )
                        ],
                        dir_summary_paths=dir_summary_paths,
                    )
                )
        else:
            regression_source_roots = [
                os.path.dirname(os.path.join(start_dir, reg_result["test_suite"]))
                for reg_result in reg_results
            ]
            metadata.extend(
                self.coverage.build_metadata(
                    all_suite_results,
                    outdir=start_dir,
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
        model_cfg = ModelConfigLoader(model_config).get_model(model_name)
        vlog_fl = VlogFilelist(
            name=self.name + "/vlog_filelist",
            model_cfg=model_cfg,
            output_path=output_path,
        )

        log_event(
            logger,
            logging.INFO,
            "command.filelist",
            model=model_name,
            output=output_path,
        )
        vlog_fl.write_output(
            output_filepath=output_path,
            unroll=unroll,
            flatten=flatten,
            strip=strip_options,
            deduplicate=deduplicate,
        )
        return

    def do_cmd_hier(
        self,
        model_name: Annotated[str, typer.Argument(help="model from models.yaml")],
        model_config: Annotated[
            str, typer.Option("-c", "--model-config", help="models.yaml to use")
        ] = "models.yaml",
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
        model_cfg = ModelConfigLoader(model_config).get_model(model_name)
        log_event(
            logger,
            logging.INFO,
            "command.hier",
            command="hier",
            model=model_name,
            format=fmt,
            output=output,
        )
        view = RtlBuddyView(
            name=self.name + "/hier",
            model_cfg=model_cfg,
            suite_dir=os.getcwd(),
            format=fmt,
            output=output,
            frontend=frontend,
            cdc_annotations=cdc_annotations,
            clock_legend=clock_legend,
            executable=tool,
        )
        raise typer.Exit(view.run())

    def do_docs_list(self):
        pages = [page.to_list_item() for page in list_pages()]
        if self.machine:
            print(json.dumps({"pages": pages}, ensure_ascii=True))
            return

        for page in pages:
            print(f"{page['slug']} - {page['title']}: {page['summary']}")

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
            raise typer.Exit(1)

        specs = discover_spec_configs(search_dir)
        blocks = all_spec_blocks(specs)
        if not blocks:
            emit_console_text("No spec blocks found.", style="yellow")
            raise typer.Exit(0)

        if self.machine:
            print(
                json.dumps(
                    {
                        "blocks": [
                            {
                                "block": b.name,
                                "desc": b.desc,
                                "path": cfg.get_path(),
                                "coverage_items": len(b.coverage_items),
                            }
                            for cfg, b in blocks
                        ]
                    },
                    ensure_ascii=True,
                )
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
            print(
                json.dumps(
                    {
                        "blocks": [
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
                        ]
                    },
                    ensure_ascii=True,
                )
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
            items_out = []
            for cfg, b in blocks:
                for item in b.coverage_items:
                    tests = cov_map.get(item.id, [])
                    items_out.append(
                        {
                            "block": b.name,
                            "id": item.id,
                            "desc": item.desc,
                            "covered": bool(tests),
                            "tests": [{"path": p, "test": t} for p, t in tests],
                        }
                    )
            print(json.dumps({"items": items_out}, ensure_ascii=True))
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
            results.append({"synth_name": s.get_name(), "results": runner.run()})
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
        suite_cfg = SynthSuiteConfig(path=synth_config)
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
            emit_console_text("  ".join(suite_cfg.get_synth_names()), stream="stdout")
            raise typer.Exit(0)

        synth_results = self._do_synth_suite(
            suite_cfg, synth_name=synth_name, effort_override=effort
        )
        self._render_synth_summary("Synthesis Results Summary", synth_results)
        raise typer.Exit(self._exit_code_from_synth_results(synth_results))

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
        suite_cfg = PnrSuiteConfig(path=pnr_config)
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
            emit_console_text("  ".join(suite_cfg.get_run_names()), stream="stdout")
            raise typer.Exit(0)

        results = self._do_pnr_suite(
            suite_cfg,
            pnr_name=pnr_name,
            reg_level=reg_level,
            emit_gds=emit_gds,
            emit_png=emit_png,
        )
        self._render_pnr_summary("P&R Results Summary", results)
        raise typer.Exit(0 if all(r["results"].is_pass() for r in results) else 1)

    def _do_pnr_suite(
        self,
        suite_cfg,
        *,
        pnr_name=None,
        reg_level=0,
        emit_gds: bool = False,
        emit_png: bool = False,
    ):
        root_cfg = RootConfig(name="pnr")
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
            results.append({"pnr_name": run.get_name(), "results": runner.run()})
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
        suite_cfg = PowerSuiteConfig(path=power_config)
        log_event(
            logger,
            logging.INFO,
            "command.power",
            command="power",
            power=power_name or "all",
            power_config=power_config,
        )

        if list_runs:
            emit_console_text("  ".join(suite_cfg.get_run_names()), stream="stdout")
            raise typer.Exit(0)

        results = self._do_power_suite(
            suite_cfg,
            power_name=power_name,
            reg_level=reg_level,
        )
        self._render_power_summary("Power Results Summary", results)
        raise typer.Exit(0 if all(r["results"].is_pass() for r in results) else 1)

    def _do_power_suite(
        self,
        suite_cfg,
        *,
        power_name=None,
        reg_level=0,
    ):
        root_cfg = RootConfig(name="power")
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
            results.append({"power_name": run.get_name(), "results": runner.run()})
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

        start_dir = os.getcwd()
        if reg_config is not None:
            reg_cfg_path = os.path.join(start_dir, reg_config)
        else:
            local = os.path.join(start_dir, "power_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "power_regression.yaml not found; pass -c to specify a path"
                )

        power_reg = PowerRegConfig(
            name=self.name + "/power_reg_config", path=reg_cfg_path
        )
        emit_console_text(
            f"Running power regression from {os.path.dirname(reg_cfg_path)}",
            style="cyan",
        )

        all_results = []
        try:
            for suite_cfg in power_reg.get_suite_configs():
                suite_dir = os.path.dirname(suite_cfg.get_path())
                log_event(
                    logger,
                    logging.INFO,
                    "power_regression.suite_start",
                    suite=suite_cfg.get_path(),
                )
                os.chdir(suite_dir)
                suite_results = self._do_power_suite(
                    suite_cfg, power_name=None, reg_level=reg_level
                )
                all_results.extend(suite_results)
        finally:
            os.chdir(start_dir)

        self._render_power_summary(
            "Power Regression Summary",
            all_results,
            metadata=[f"Reg Level: {reg_level}"],
        )
        raise typer.Exit(self._exit_code_from_power_results(all_results))

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

        start_dir = os.getcwd()
        if reg_config is not None:
            reg_cfg_path = os.path.join(start_dir, reg_config)
        else:
            local = os.path.join(start_dir, "synth_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "synth_regression.yaml not found; pass -c to specify a path"
                )

        synth_reg = SynthRegConfig(
            name=self.name + "/synth_reg_config", path=reg_cfg_path
        )
        emit_console_text(
            f"Running synthesis regression from {os.path.dirname(reg_cfg_path)}",
            style="cyan",
        )

        all_results = []
        try:
            for suite_cfg in synth_reg.get_suite_configs():
                suite_dir = os.path.dirname(suite_cfg.get_path())
                log_event(
                    logger,
                    logging.INFO,
                    "synth_regression.suite_start",
                    suite=suite_cfg.get_path(),
                )
                os.chdir(suite_dir)
                suite_results = self._do_synth_suite(
                    suite_cfg,
                    synth_name=None,
                    reg_level=reg_level,
                    effort_override=effort,
                )
                all_results.extend(suite_results)
        finally:
            os.chdir(start_dir)

        self._render_synth_summary(
            "Synthesis Regression Summary",
            all_results,
            metadata=[f"Reg Level: {reg_level}"],
        )
        raise typer.Exit(self._exit_code_from_synth_results(all_results))

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
            results.append({"cdc_name": a.get_name(), "results": runner.run()})
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
        from pathlib import Path

        from .tools.saif_from_trace import convert

        convert(Path(trace), Path(output))

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
    ):
        """
        run CDC lint
        """
        suite_cfg = CdcSuiteConfig(path=cdc_config)
        log_event(
            logger,
            logging.INFO,
            "command.cdc",
            command="cdc",
            cdc=cdc_name or "all",
            cdc_config=cdc_config,
        )

        if list_cdcs:
            emit_console_text(
                "  ".join(suite_cfg.get_analysis_names()), stream="stdout"
            )
            raise typer.Exit(0)

        cdc_results = self._do_cdc_suite(suite_cfg, cdc_name=cdc_name)
        self._render_cdc_summary("CDC Lint Results Summary", cdc_results)
        raise typer.Exit(self._exit_code_from_cdc_results(cdc_results))

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

        start_dir = os.getcwd()
        if reg_config is not None:
            reg_cfg_path = os.path.join(start_dir, reg_config)
        else:
            local = os.path.join(start_dir, "cdc_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "cdc_regression.yaml not found; pass -c to specify a path"
                )

        cdc_reg = CdcRegConfig(name=self.name + "/cdc_reg_config", path=reg_cfg_path)
        emit_console_text(
            f"Running CDC regression from {os.path.dirname(reg_cfg_path)}",
            style="cyan",
        )

        all_results = []
        try:
            for suite_cfg in cdc_reg.get_suite_configs():
                suite_dir = os.path.dirname(suite_cfg.get_path())
                log_event(
                    logger,
                    logging.INFO,
                    "cdc_regression.suite_start",
                    suite=suite_cfg.get_path(),
                )
                os.chdir(suite_dir)
                suite_results = self._do_cdc_suite(
                    suite_cfg, cdc_name=None, reg_level=reg_level
                )
                all_results.extend(suite_results)
        finally:
            os.chdir(start_dir)

        self._render_cdc_summary(
            "CDC Regression Summary",
            all_results,
            metadata=[f"Reg Level: {reg_level}"],
        )
        raise typer.Exit(self._exit_code_from_cdc_results(all_results))

    # --- FPV subcommands ----------------------------------------------------

    def _render_fpv_summary(self, title, fpv_results, *, metadata=None):
        rows = []
        for r in fpv_results:
            res = r["results"].results
            engines = res.get("engines") or []
            runtime = res.get("runtime_s")
            row = {
                "fpv_name": r["fpv_name"],
                "result": res["result"],
                "desc": res["desc"],
                "mode": str(res.get("mode", "-")),
                "depth": str(res.get("depth", "-")),
                "engines": ", ".join(engines) if engines else "-",
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
            ("runtime", "Runtime"),
        ]
        render_summary(
            title=title,
            columns=columns,
            rows=rows,
            logger=logger,
            metadata=metadata,
        )

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
            results.append({"fpv_name": v.get_name(), "results": runner.run()})
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
        suite_cfg = FpvSuiteConfig(path=fpv_config)
        log_event(
            logger,
            logging.INFO,
            "command.fpv",
            command="fpv",
            fpv=fpv_name or "all",
            fpv_config=fpv_config,
        )

        if list_fpvs:
            emit_console_text(
                "  ".join(suite_cfg.get_verification_names()), stream="stdout"
            )
            raise typer.Exit(0)

        fpv_results = self._do_fpv_suite(suite_cfg, fpv_name=fpv_name)
        self._render_fpv_summary("FPV Results Summary", fpv_results)
        raise typer.Exit(self._exit_code_from_fpv_results(fpv_results))

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

        start_dir = os.getcwd()
        if reg_config is not None:
            reg_cfg_path = os.path.join(start_dir, reg_config)
        else:
            local = os.path.join(start_dir, "fpv_regression.yaml")
            reg_cfg_path = local if os.path.isfile(local) else None
            if reg_cfg_path is None:
                raise FatalRtlBuddyError(
                    "fpv_regression.yaml not found; pass -c to specify a path"
                )

        fpv_reg = FpvRegConfig(name=self.name + "/fpv_reg_config", path=reg_cfg_path)
        emit_console_text(
            f"Running FPV regression from {os.path.dirname(reg_cfg_path)}",
            style="cyan",
        )

        all_results = []
        try:
            for suite_cfg in fpv_reg.get_suite_configs():
                suite_dir = os.path.dirname(suite_cfg.get_path())
                log_event(
                    logger,
                    logging.INFO,
                    "fpv_regression.suite_start",
                    suite=suite_cfg.get_path(),
                )
                os.chdir(suite_dir)
                suite_results = self._do_fpv_suite(
                    suite_cfg, fpv_name=None, reg_level=reg_level
                )
                all_results.extend(suite_results)
        finally:
            os.chdir(start_dir)

        self._render_fpv_summary(
            "FPV Regression Summary",
            all_results,
            metadata=[f"Reg Level: {reg_level}"],
        )
        raise typer.Exit(self._exit_code_from_fpv_results(all_results))

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

        suite_cfg = SuiteConfig(path=test_config)
        suite_dir = os.path.dirname(os.path.abspath(test_config))
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

    def do_wave_install_nvim(
        self,
        force: Annotated[
            bool,
            typer.Option("--force", help="overwrite existing installation"),
        ] = False,
    ):
        """
        install the rtl_buddy_wave.lua plugin into ~/.local/share/nvim/site/plugin/

        The plugin provides the WaveValue highlight group and VimEnter hook
        needed for rb wave signal value annotation. It is auto-sourced by nvim
        via runtimepath — no changes to init.lua required.
        """
        from importlib.resources import files as _res
        from importlib.metadata import version as _ver
        from pathlib import Path

        dest_dir = Path(os.path.expanduser("~/.local/share/nvim/site/plugin"))
        dest = dest_dir / "rtl_buddy_wave.lua"

        if dest.exists() and not force:
            emit_console_text(f"Already installed: {dest}  (use --force to overwrite)")
            return

        dest_dir.mkdir(parents=True, exist_ok=True)
        src = _res("rtl_buddy.nvim").joinpath("rtl_buddy_wave.lua").read_text()
        dest.write_text(src)
        emit_console_text(f"Installed: {dest}  (rtl-buddy {_ver('rtl-buddy')})")
        emit_console_text("Restart nvim for the plugin to take effect.")

    def do_lint(self):
        assert False, "not yet impl"

    def do_export(self):
        assert False, "not yet impl"

    def do_gen_vlog_run_script(self):
        assert False, "not yet impl"

    def do_verible(
        self,
        cmd: Annotated[str, typer.Argument(help="Verible cmd")],
        verible_args: Annotated[list[str], typer.Argument(...)] = [],
    ):
        """
        run verible cmd
        """
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
        exit_code = ver.do_cmd(cmd=cmd, verible_args=verible_args)
        raise typer.Exit(exit_code)

    def show_git_rev(self):
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
            logger.debug("git metadata unavailable for banner")
            return

        status_lines = status_result.stdout.splitlines()
        git_branch = status_lines[0][3:].split("...")[0] if status_lines else "unknown"
        file_lines = status_lines[1:]
        mod = sum(1 for ln in file_lines if len(ln) > 1 and ln[1] not in (" ", "?"))
        staged = sum(1 for ln in file_lines if len(ln) > 0 and ln[0] not in (" ", "?"))
        git_commit = commit_result.stdout.strip()

        if mod > 0 or staged > 0:
            git_str = (
                f"git: {git_branch} | commit {git_commit} | mod {mod} | staged {staged}"
            )
        else:
            git_str = f"git: {git_branch} | commit {git_commit} | clean"

        emit_console_text(git_str, style=None if is_machine_mode() else "dim")
        log_event(
            logger,
            logging.INFO,
            "git.status",
            branch=git_branch,
            commit=git_commit,
            modified=mod,
            staged=staged,
        )
