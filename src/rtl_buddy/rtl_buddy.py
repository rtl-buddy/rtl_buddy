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
import typer
from importlib.metadata import version
from typing_extensions import Annotated
import click

from .config import RegConfig, RootConfig, SuiteConfig, TestConfig
from .config.model import ModelConfigLoader
from .docs_access import get_page, get_section, list_pages
from .errors import FatalRtlBuddyError, FilelistError
from .logging_utils import emit_console_text, is_machine_mode, log_event, render_summary, setup_logging
from .runner.test_results import SetupFailResults, SkipResults
from .runner.test_runner import RunDepth, TestRunner
from .seed_mode import SeedMode
from .skill_install import app as skill_app
from .tools.coverage import CoverageReporter
from .tools.verible import Verible
from .tools.vlog_filelist import VlogFilelist

logger = logging.getLogger(__name__)


class RtlBuddy():
  """
  RTL Buddy Main Class

  Handles cli entry into RTL Buddy
  """

  _GIT_COMMANDS = {"test", "randtest", "regression", "filelist"}

  def cb_builder(value: str | None) -> str | None:
    if value is None:
      return value

    try:
      configured_builders = RootConfig.discover_rtl_builder_names()
    except ValueError as e:
      raise typer.BadParameter(f"Cannot validate builder override: {e}") from e

    if value not in configured_builders:
      raise typer.BadParameter(f"Choose from configured builders: [{', '.join(configured_builders)}]")
    return value

  def cb_version(value: bool):
    if value:
      print(f'rtl_buddy v{version("rtl-buddy")}')
      raise typer.Exit()

  def __init__(self, name):
    self.app = typer.Typer(no_args_is_help=True)
    self.docs_app = typer.Typer(help="browse bundled rtl_buddy documentation", no_args_is_help=True)
    self.app.callback()(self.root_options)
    self.app.command("test", help="run a simple test")(self.do_cmd_test)
    self.app.command("randtest", help="repeat a test with multiple random seeds")(self.do_rand_test)
    self.app.command("regression", help="run rtl regression")(self.do_rtl_regression)
    self.app.command("filelist", help="generate filelists using models.yaml")(self.do_gen_model_filelist)
    self.app.command("verible", help="run verible cmd")(self.do_verible)
    self.app.add_typer(skill_app, name="skill", help="manage the rtl_buddy agent skill")
    self.docs_app.command("list", help="list bundled documentation pages")(self.do_docs_list)
    self.docs_app.command("show", help="show a bundled documentation page")(self.do_docs_show)
    self.app.add_typer(self.docs_app, name="docs", help="browse bundled documentation")

    if '.' not in os.environ["PATH"].split(os.pathsep):
      os.environ["PATH"] = '.' + os.pathsep + os.environ["PATH"]

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

  def root_options(self,
    ctx: typer.Context,
    debug: Annotated[bool, typer.Option("--debug", "-D", help="Print rtl_buddy debug details to console")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Print execution details to console")] = False,
    machine: Annotated[bool, typer.Option("--machine", help="Emit machine-oriented logs and plain console output")] = False,
    color: Annotated[bool, typer.Option(help="Logs without ANSI color codes")] = True,
    rtl_builder_mode: Annotated[str, typer.Option("-M", "--builder-mode", help="Override default builder_mode")] = None,
    builder_override: Annotated[str, typer.Option("-B", "--builder", callback=cb_builder, help="Override platform default builder")] = None,
    run_depth: Annotated[RunDepth, typer.Option("-E", "--early-stop", case_sensitive=False, help="Run step to stop early at", show_default=False)] = RunDepth.POST,
    version_opt: Annotated[bool, typer.Option("--version", callback=cb_version, is_eager=True, help="Prints version")] = False
    ):
    rtl_buddy_argv = sys.argv[1:]
    if "--" in rtl_buddy_argv:
      rtl_buddy_argv = rtl_buddy_argv[:rtl_buddy_argv.index("--")]

    if ctx.resilient_parsing or any(arg in {"--help", "-h"} for arg in rtl_buddy_argv):
      return

    self.machine = machine

    if ctx.invoked_subcommand in {"skill", "docs"}:
      return

    setup_logging(debug=debug, verbose=verbose, color=color, machine=machine)

    log_event(logger, logging.INFO, "cli.start", version=version("rtl-buddy"))

    if ctx.invoked_subcommand in self._GIT_COMMANDS and not self._is_test_list_invocation(ctx):
      self.show_git_rev()

    self.rtl_builder_mode = rtl_builder_mode
    self.root_cfg = RootConfig(name=self.name+"/root_config", builder_override=builder_override)
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
      exit_code |= 0 if suite_result['results'].is_pass() else 1
    return exit_code

  def _render_test_summary(self, title, suite_results, *, include_run_id: bool = False, metadata: list[str] | None = None):
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
        row["run_id"] = "" if suite_result["randmode_i"] is None else suite_result["randmode_i"]
      if cov_summary is not None:
        row["coverage"] = cov_summary
      rows.append(row)

    columns = [("test_name", "Test")]
    if include_run_id:
      columns.append(("run_id", "Run"))
    columns.extend([("result", "Result"), ("desc", "Description")])
    if has_coverage:
      columns.append(("coverage", "Coverage"))
    render_summary(title=title, columns=columns, rows=rows, logger=logger, metadata=metadata)

  def _render_regression_summary(self, reg_results, *, metadata: list[str] | None = None):
    rows = []
    has_coverage = False
    for reg_result in reg_results:
      for suite_result in reg_result["results"]:
        cov_summary = self._format_coverage_summary(suite_result["results"])
        has_coverage |= cov_summary is not None
        rows.append({
          "suite_name": reg_result["test_suite"],
          "test_name": suite_result["test_name"],
          "result": suite_result["results"].results["result"],
          "desc": suite_result["results"].results["desc"],
          "coverage": cov_summary or "",
        })

    columns = [("suite_name", "Suite"), ("test_name", "Test"), ("result", "Result"), ("desc", "Description")]
    if has_coverage:
      columns.append(("coverage", "Coverage"))
    render_summary(
      title="Regression Results Summary",
      columns=columns,
      rows=rows,
      logger=logger,
      metadata=metadata if metadata is not None else [f"Builder: {self.builder}", f"Builder Mode: {self.rtl_builder_mode}"],
    )

  def _display_path(self, path: str, *, base_dir: str | None = None) -> str:
    if base_dir is None:
      return path

    try:
      relpath = os.path.relpath(path, base_dir)
    except ValueError:
      return path

    return relpath if len(relpath) < len(path) else path

  def _resolve_coverage_dir_summary_paths(self, coverage_dir_summary=None, coverage_dir_summary_file=None):
    """
    Resolve configured coverage directory-summary prefixes from repeated CLI
    options and/or a file containing one path per line.
    """
    return self.coverage.resolve_dir_summary_paths(
      dir_summary_paths=coverage_dir_summary,
      dir_summary_file=coverage_dir_summary_file,
    )

  def do_cmd_test(self,
    test_config: Annotated[str, typer.Option("-c", "--test-config", help="test_config.yaml to use")] = "tests.yaml",
    test_name: Annotated[str, typer.Argument(help="name of test", show_default="run all tests")] = None,
    list_tests: Annotated[bool, typer.Option("--list", help="list tests in the selected test-config and exit")] = False,
    coverage_merge: Annotated[bool, typer.Option("--coverage-merge", help="merge coverage across selected tests; uses raw merge for summary/html and info-process for Coverview")] = False,
    coverage_merge_raw: Annotated[bool, typer.Option("--coverage-merge-raw", help="use raw Verilator merge for merged summary/html/Coverview")] = False,
    coverage_merge_info_process: Annotated[bool, typer.Option("--coverage-merge-info-process", help="use info-process merge for merged summary/Coverview; HTML merge is not supported")] = False,
    coverage_html: Annotated[bool, typer.Option("--coverage-html", help="generate merged LCOV HTML output in coverage_merge.html")] = False,
    coverage_coverview: Annotated[bool, typer.Option("--coverage-coverview", help="generate Coverview zip output from coverage info")] = False,
    coverage_dir_summary: Annotated[list[str] | None, typer.Option("--coverage-dir-summary", help="append coverage summary lines for repo-relative directory prefixes; may be repeated")] = None,
    coverage_dir_summary_file: Annotated[str | None, typer.Option("--coverage-dir-summary-file", help="file containing repo-relative directory prefixes, one per line")] = None,
    rnd_new: Annotated[bool, typer.Option("-n", "--rnd-new", help="use a randomly generated seed instead of root config seed", show_default=False)] = None,
    rnd_last: Annotated[bool, typer.Option("-l", "--rnd-last", help="reuse last generated seed", show_default=False)] = None
    ):
    """
    run a simple test
    """
    merge_mode_count = sum(1 for enabled in [coverage_merge, coverage_merge_raw, coverage_merge_info_process] if enabled)
    if merge_mode_count > 1:
      raise FatalRtlBuddyError("--coverage-merge, --coverage-merge-raw, and --coverage-merge-info-process are mutually exclusive")
    if coverage_merge_info_process and coverage_html:
      raise FatalRtlBuddyError("--coverage-html is not supported with --coverage-merge-info-process")

    self.rtl_builder_mode = "debug" if self.rtl_builder_mode is None else self.rtl_builder_mode
    self.suite_cfg = SuiteConfig(path=test_config)
    log_event(logger, logging.INFO, "command.test", command="test", test=test_name or "all", test_config=test_config)

    if list_tests:
      emit_console_text("  ".join(self.suite_cfg.get_test_names()), stream="stdout")
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
      replay_run_id=replay_run_id)
    dir_summary_paths = self._resolve_coverage_dir_summary_paths(
      coverage_dir_summary=coverage_dir_summary,
      coverage_dir_summary_file=coverage_dir_summary_file,
    )
    metadata = [f"Builder: {self.builder}"]
    metadata.extend(self.coverage.build_metadata(
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
    ))
    self._render_test_summary("Test Results Summary", suite_results, metadata=metadata)
    raise typer.Exit(self._exit_code_from_results(suite_results))

  def do_rand_test(self,
    test_name: Annotated[str, typer.Argument(help="name of test", show_default="run all tests")],
    rnd_cnt: Annotated[int, typer.Argument(metavar="RND_CNT", help="number of random iterations to test")] = 2,
    test_config: Annotated[str, typer.Option("-c", "--test-config", help="test_config.yaml to use")] = "tests.yaml",
    rpt_i: Annotated[int, typer.Option("-r", "--rnd-rpt", help="repeat iteration number from previous run", show_default=False)] = None
    ):
    """
    repeat a test with multiple random seeds
    """
    self.rtl_builder_mode = "debug" if self.rtl_builder_mode is None else self.rtl_builder_mode
    self.suite_cfg = SuiteConfig(path=test_config)

    log_event(logger, logging.INFO, "command.randtest", command="randtest", test=test_name, iterations=rnd_cnt, replay_run_id=rpt_i)

    if rpt_i is not None:
      suite_results = self._do_test_suite(
        self.suite_cfg,
        test_name=test_name,
        run_ids=[rpt_i],
        seed_mode=SeedMode.REPLAY,
        replay_run_id=rpt_i)
      self._render_test_summary("RandTest Replay Summary", suite_results, include_run_id=True, metadata=[f"Builder: {self.builder}"])
    else:
      suite_results = self._do_test_suite(
        self.suite_cfg,
        test_name=test_name,
        run_ids=list(range(1, rnd_cnt + 1)),
        seed_mode=SeedMode.NEW,
        replay_run_id=None)
      self._render_test_summary("RandTest Results Summary", suite_results, include_run_id=True, metadata=[f"Builder: {self.builder}"])

    raise typer.Exit(self._exit_code_from_results(suite_results))

  def _append_skip_results(self, test_name, desc, run_ids, suite_results):
    test_results = SkipResults(name=test_name+"/results", desc=desc)
    for run_id in run_ids:
      suite_results.append({'test_name': test_name, 'randmode_i': run_id, 'results': test_results})

  def _append_setup_results(self, test_name, desc, run_ids, suite_results):
    test_results = SetupFailResults(name=test_name+"/results", desc=desc)
    for run_id in run_ids:
      suite_results.append({'test_name': test_name, 'randmode_i': run_id, 'results': test_results})

  def _expand_tests_with_sweep(self, test_cfg):
    script_path = test_cfg.get_sweep_path()
    if script_path is None:
      return [test_cfg], None

    with open(script_path, 'r') as file:
      code = file.read()

    ns = {
      "logger": logger,
      "TestConfig": TestConfig,
      "test_cfg": test_cfg,
      "root_cfg": self.root_cfg,
      "out_test_cfgs": [],
      "__file__": os.path.abspath(script_path),
    }
    try:
      exec(code, ns)
    except Exception as e:
      log_event(logger, logging.ERROR, "sweep.failed", test=test_cfg.name, script=script_path, error=e)
      logger.debug("sweep traceback", exc_info=True)
      return [], f"Setup failed in sweep: {e}"

    log_event(logger, logging.INFO, "sweep.completed", test=test_cfg.name, script=script_path, expanded=len(ns["out_test_cfgs"]))
    return ns["out_test_cfgs"], None

  def _run_test_cfg_for_run_ids(self, test_cfg, run_ids, seed_mode: SeedMode, replay_run_id, test_runner_mode):
    test_runner = TestRunner(
      name=self.name+"/testrunner",
      root_cfg=self.root_cfg,
      test_cfg=test_cfg,
      test_runner_mode=test_runner_mode,
      run_id=run_ids[0],
      seed_mode=seed_mode,
      replay_run_id=replay_run_id,
      rtl_builder_mode=self.rtl_builder_mode,
      run_depth=self.run_depth)

    if len(run_ids) == 1:
      return [test_runner.run()]
    return test_runner.run_multiple(run_ids)

  def _append_results(self, test_name, run_ids, results, suite_results):
    for run_id, test_results in zip(run_ids, results):
      suite_results.append({'test_name': test_name, 'randmode_i': run_id, 'results': test_results})

  def _format_coverage_summary(self, test_results):
    return self.coverage.format_summary(test_results)

  def _do_test_suite(self,
    suite_cfg,
    test_name=None,
    test_runner_mode={'sim_to_stdout': True},
    reg_level=None,
    start_level=None,
    run_ids=None,
    seed_mode: SeedMode = SeedMode.DEFAULT,
    replay_run_id=None):

    if run_ids is None:
      run_ids = [None]

    tests = suite_cfg.get_tests(test_name)
    suite_results = []
    for t in tests:
      t_lvl = t.get_reglvl(self.builder)
      if reg_level is not None and t_lvl > reg_level:
        log_event(logger, logging.INFO, "suite.skip", test=t.name, reason="above_regression_level", test_level=t_lvl, reg_level=reg_level)
        self._append_skip_results(t.name, f'lvl {t_lvl} > cmd end_level {reg_level}', run_ids, suite_results)
        continue

      if start_level is not None and t_lvl < start_level:
        log_event(logger, logging.INFO, "suite.skip", test=t.name, reason="below_start_level", test_level=t_lvl, start_level=start_level)
        self._append_skip_results(t.name, f'lvl {t_lvl} < cmd start_level {start_level}', run_ids, suite_results)
        continue

      expanded_tests, sweep_error = self._expand_tests_with_sweep(t)
      if sweep_error is not None:
        self._append_setup_results(t.name, sweep_error, run_ids, suite_results)
        continue

      for expanded_test_cfg in expanded_tests:
        run_results = self._run_test_cfg_for_run_ids(
          test_cfg=expanded_test_cfg,
          run_ids=run_ids,
          seed_mode=seed_mode,
          replay_run_id=replay_run_id,
          test_runner_mode=test_runner_mode)
        self._append_results(expanded_test_cfg.name, run_ids, run_results, suite_results)
    return suite_results

  def do_rtl_regression(self,
    reg_config: Annotated[str, typer.Option("-c", "--reg-config", help="path to regressions.yaml", show_default="Use ./regression.yaml if present, otherwise root_config.yaml reg-cfg-path")] = None,
    reg_level: Annotated[int, typer.Option("-l", "--reg-level", help="regression level to stop at")] = 0,
    start_level: Annotated[int, typer.Option("-s", "--start-level", help="regression level to start at")] = 0,
    coverage_merge: Annotated[bool, typer.Option("--coverage-merge", help="merge coverage across regression tests; uses raw merge for summary/html and info-process for Coverview")] = False,
    coverage_merge_raw: Annotated[bool, typer.Option("--coverage-merge-raw", help="use raw Verilator merge for merged summary/html/Coverview")] = False,
    coverage_merge_info_process: Annotated[bool, typer.Option("--coverage-merge-info-process", help="use info-process merge for merged summary/Coverview; HTML merge is not supported")] = False,
    coverage_html: Annotated[bool, typer.Option("--coverage-html", help="generate merged LCOV HTML output in coverage_merge.html")] = False,
    coverage_coverview: Annotated[bool, typer.Option("--coverage-coverview", help="generate Coverview zip output from coverage info")] = False,
    coverage_per_test: Annotated[bool, typer.Option("--coverage-per-test", help="package one Coverview dataset per test in regression mode")] = False,
    coverage_dir_summary: Annotated[list[str] | None, typer.Option("--coverage-dir-summary", help="append coverage summary lines for repo-relative directory prefixes; may be repeated")] = None,
    coverage_dir_summary_file: Annotated[str | None, typer.Option("--coverage-dir-summary-file", help="file containing repo-relative directory prefixes, one per line")] = None,
    ):
    """
    run rtl regression
    """
    merge_mode_count = sum(1 for enabled in [coverage_merge, coverage_merge_raw, coverage_merge_info_process] if enabled)
    if merge_mode_count > 1:
      raise FatalRtlBuddyError("--coverage-merge, --coverage-merge-raw, and --coverage-merge-info-process are mutually exclusive")
    if coverage_merge_info_process and coverage_html:
      raise FatalRtlBuddyError("--coverage-html is not supported with --coverage-merge-info-process")

    self.rtl_builder_mode = "reg" if self.rtl_builder_mode is None else self.rtl_builder_mode
    log_event(logger, logging.INFO, "command.regression", reg_config=reg_config, reg_level=reg_level, start_level=start_level)

    start_dir = os.getcwd()
    if reg_config is not None :
      self.reg_cfg = RegConfig(name=self.name+"/reg_config", path=os.path.join(start_dir, reg_config))
      log_event(logger, logging.INFO, "regression.config_override", path=reg_config)
    else:
      local_reg_config = os.path.join(start_dir, "regression.yaml")
      if os.path.isfile(local_reg_config):
        self.reg_cfg = RegConfig(name=self.name+"/reg_config", path=local_reg_config)
        log_event(logger, logging.INFO, "regression.config_local_default", path=local_reg_config)
      else:
        self.reg_cfg = self.root_cfg.get_rtl_reg_cfg()
        log_event(logger, logging.INFO, "regression.config_root_default", path=self.reg_cfg.get_path())

    reg_dir = os.path.dirname(self.reg_cfg.get_path())
    emit_console_text(f"Running regression from {reg_dir}", style="cyan")

    exit_code = 0
    reg_results = []
    try:
      for suite_cfg in self.reg_cfg.get_suite_configs():
        suite_cfg_dir = os.path.dirname(suite_cfg.get_path())
        log_event(logger, logging.INFO, "regression.suite_start", suite=suite_cfg.get_path(), cwd=suite_cfg_dir)
        os.chdir(suite_cfg_dir)
        suite_results = self._do_test_suite(
          suite_cfg=suite_cfg,
          test_name=None,
          test_runner_mode={'sim_to_stdout': False},
          reg_level=reg_level,
          start_level=start_level,
          run_ids=[None],
          seed_mode=SeedMode.DEFAULT,
          replay_run_id=None)
        reg_results.append({
          'test_suite': self._display_path(suite_cfg.get_path(), base_dir=start_dir),
          'results': suite_results,
        })
        exit_code |= self._exit_code_from_results(suite_results)
    finally:
      os.chdir(start_dir)

    all_suite_results = []
    for reg_result in reg_results:
      all_suite_results.extend(reg_result["results"])

    metadata = [f"Builder: {self.builder}", f"Builder Mode: {self.rtl_builder_mode}"]
    dir_summary_paths = self._resolve_coverage_dir_summary_paths(
      coverage_dir_summary=coverage_dir_summary,
      coverage_dir_summary_file=coverage_dir_summary_file,
    )
    if coverage_html and not coverage_merge and not coverage_merge_raw and not coverage_merge_info_process:
      for reg_result in reg_results:
        metadata.extend(self.coverage.build_metadata(
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
          source_roots=[os.path.dirname(os.path.join(start_dir, reg_result["test_suite"]))],
          dir_summary_paths=dir_summary_paths,
        ))
    else:
      regression_source_roots = [
        os.path.dirname(os.path.join(start_dir, reg_result["test_suite"]))
        for reg_result in reg_results
      ]
      metadata.extend(self.coverage.build_metadata(
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
      ))

    self._render_regression_summary(reg_results, metadata=metadata)
    raise typer.Exit(exit_code)

  def do_gen_model_filelist(self,
    model_name: Annotated[str, typer.Argument(help="name of model")],
    output_path: Annotated[str, typer.Argument(help="Output filename")] = "run.f",
    model_config: Annotated[str, typer.Option("-c", "--model-config", help="model_config.yaml to use")] = "models.yaml",
    unroll: Annotated[bool, typer.Option("--unroll", "-u", help="Recursively unroll -F in filelists")] = False,
    flatten: Annotated[bool, typer.Option("--flatten", "-f", help="Remove path to a file, leaving just the filename")] = False,
    strip_options: Annotated[bool, typer.Option("--strip", "-s", help="Remove option part of a line")] = False,
    deduplicate: Annotated[bool, typer.Option("--deduplicate", "-d", help="Remove duplicates")] = False):
    """
    generate filelists using models.yaml
    """
    model_cfg = ModelConfigLoader(model_config).get_model(model_name)
    vlog_fl = VlogFilelist(name=self.name+"/vlog_filelist",
      model_cfg=model_cfg,
      output_path=output_path)

    log_event(logger, logging.INFO, "command.filelist", model=model_name, output=output_path)
    vlog_fl.write_output(output_filepath=output_path, unroll=unroll, flatten=flatten, strip=strip_options, deduplicate=deduplicate)
    return

  def do_docs_list(self):
    pages = [page.to_list_item() for page in list_pages()]
    if self.machine:
      print(json.dumps({"pages": pages}, ensure_ascii=True))
      return

    for page in pages:
      print(f'{page["slug"]} - {page["title"]}: {page["summary"]}')

  def do_docs_show(self,
    slug: Annotated[str, typer.Argument(help="MkDocs path slug or slug#section-anchor, for example concepts/root-config or agents#local-docs-access")],
    ):
    if "#" in slug:
      page_slug, anchor = slug.split("#", 1)
      section = get_section(page_slug, anchor)
      if section is None:
        if get_page(page_slug) is None:
          raise click.ClickException(f"Unknown docs page '{page_slug}'. Run `rtl-buddy docs list` to see available slugs.")
        raise click.ClickException(f"Unknown section '{anchor}' in page '{page_slug}'. Run `rtl-buddy docs show {page_slug}` to see available sections.")
      if self.machine:
        print(json.dumps(section, ensure_ascii=True))
        return
      print(section["content"])
      return

    page = get_page(slug)
    if page is None:
      raise click.ClickException(f"Unknown docs page '{slug}'. Run `rtl-buddy docs list` to see available slugs.")

    if self.machine:
      print(json.dumps(page.to_show_payload(), ensure_ascii=True))
      return

    print(page.content, end="" if page.content.endswith("\n") else "\n")

  def do_lint(self):
    assert False, "not yet impl"

  def do_export(self):
    assert False, "not yet impl"

  def do_gen_vlog_run_script(self):
    assert False, "not yet impl"

  def do_verible(self,
    cmd: Annotated[str, typer.Argument(help="Verible cmd")],
    verible_args: Annotated[list[str], typer.Argument(...)] = []
    ):
    """
    run verible cmd
    """
    verible_cfg = self.root_cfg.platform_cfg.get_verible()
    if not verible_cfg.available:
      log_event(logger, logging.ERROR, "verible.unavailable")
      raise typer.Exit(2)

    ver = Verible(self.name + "/verible", cfg=verible_cfg)
    log_event(logger, logging.DEBUG, "verible.args", command=cmd, argv=" ".join(verible_args))
    exit_code = ver.do_cmd(cmd=cmd, verible_args=verible_args)
    raise typer.Exit(exit_code)

  def show_git_rev(self):
    status_result = subprocess.run(
      ['git', 'status', '-sb'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False
    )
    commit_result = subprocess.run(
      ['git', 'log', '-1', '--pretty=%h'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False
    )

    if status_result.returncode != 0 or commit_result.returncode != 0:
      logger.debug("git metadata unavailable for banner")
      return

    status_lines = status_result.stdout.splitlines()
    git_branch = status_lines[0][3:].split('...')[0] if status_lines else 'unknown'
    file_lines = status_lines[1:]
    mod = sum(1 for l in file_lines if len(l) > 1 and l[1] not in (' ', '?'))
    staged = sum(1 for l in file_lines if len(l) > 0 and l[0] not in (' ', '?'))
    git_commit = commit_result.stdout.strip()

    if mod > 0 or staged > 0:
      git_str = f"git: {git_branch} | commit {git_commit} | mod {mod} | staged {staged}"
    else:
      git_str = f"git: {git_branch} | commit {git_commit} | clean"

    emit_console_text(git_str, style=None if is_machine_mode() else "dim")
    log_event(logger, logging.INFO, "git.status", branch=git_branch, commit=git_commit, modified=mod, staged=staged)
