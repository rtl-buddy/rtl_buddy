"""Isolated pyslang worker used by local and dispatched elaborations."""

import argparse
import resource
import shlex
import sys
import time
from importlib.metadata import version

from .runner.elab_results import elab_failure, write_elab_result_json


def _peak_memory_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(peak)
    return int(peak * 1024)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--top", required=True)
    parser.add_argument("--input-source-count", required=True, type=int)
    parser.add_argument("slang_args", nargs=argparse.REMAINDER)
    return parser


def _parameter_names(slang_args: list[str]) -> set[str]:
    return {
        arg[2:].split("=", 1)[0]
        for arg in slang_args
        if arg.startswith("-G") and "=" in arg[2:]
    }


def _available_top_parameters(compilation, top: str) -> set[str]:
    return {
        parameter.name
        for instance in compilation.getRoot().topInstances
        if instance.name == top
        for parameter in instance.body.parameters
        if not parameter.isLocalParam
    }


def _print_build_summary(passed: bool, errors: int, warnings: int) -> None:
    status = "succeeded" if passed else "failed"
    print(
        f"Build {status}: {errors} error{'s' if errors != 1 else ''}, "
        f"{warnings} warning{'s' if warnings != 1 else ''}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    slang_args = args.slang_args
    if slang_args and slang_args[0] == "--":
        slang_args = slang_args[1:]
    started = time.perf_counter()
    stage = "import"
    results = elab_failure("pyslang worker did not complete", stage=stage)
    source_count = 0
    errors = 0
    warnings = 0
    try:
        try:
            from pyslang.driver import CommandLineOptions, Driver
        except ModuleNotFoundError:
            from pyslang import CommandLineOptions, Driver

        driver = Driver()
        driver.addStandardArgs()
        if hasattr(driver, "setTerminalColorsEnabled"):
            driver.setTerminalColorsEnabled(False)
        stage = "arguments"
        ok = driver.parseCommandLine(shlex.join(slang_args), CommandLineOptions())
        if ok:
            stage = "options"
            ok = driver.processOptions()
        if ok:
            stage = "parsing"
            ok = driver.parseAllSources()
            source_count = len(driver.syntaxTrees)
        if ok:
            stage = "elaboration"
            compilation = driver.createCompilation()
            driver.reportCompilation(compilation, False)
            requested_parameters = _parameter_names(slang_args)
            missing_parameters = requested_parameters - _available_top_parameters(
                compilation, args.top
            )
            for parameter in sorted(missing_parameters):
                print(
                    f"error: top {args.top!r} has no overridable parameter "
                    f"{parameter!r}",
                    file=sys.stderr,
                )
            driver.runAnalysis(compilation)
            ok = driver.reportDiagnostics(True) and not missing_parameters
        else:
            missing_parameters = set()
        errors = int(driver.diagEngine.numErrors) + len(missing_parameters)
        warnings = int(driver.diagEngine.numWarnings)
        passed = bool(ok) and errors == 0
        _print_build_summary(passed, errors, warnings)
        results = {
            "result": "PASS" if passed else "FAIL",
            "desc": (
                f"elaborated {source_count} source(s) with {warnings} warning(s)"
                if passed
                else f"{errors} error(s), {warnings} warning(s) during {stage}"
            ),
            "stage": (
                "complete" if passed else "parameters" if missing_parameters else stage
            ),
            "top": args.top,
            "source_count": source_count,
            "input_source_count": args.input_source_count,
            "diagnostics": {"errors": errors, "warnings": warnings},
            "pyslang_version": version("pyslang"),
        }
    except Exception as exc:  # noqa: BLE001 - the envelope is the worker contract
        results = elab_failure(f"{type(exc).__name__}: {exc}", stage=stage)
        results["top"] = args.top
        results["source_count"] = source_count
        results["input_source_count"] = args.input_source_count
        results["diagnostics"] = {"errors": errors, "warnings": warnings}
    results["elapsed_sec"] = round(time.perf_counter() - started, 6)
    results["peak_memory_bytes"] = _peak_memory_bytes()
    write_elab_result_json(
        args.result_json,
        model=args.model,
        profile=args.profile,
        results=results,
    )
    return 0 if results["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
