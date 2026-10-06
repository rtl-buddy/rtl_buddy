"""The coverage tail (merge, model build, LCOV exports, manifest) as one dispatched job (#650).

Covers the ``cfg-dispatch.coverage`` reservation, the ``cfg-coverage`` merge timeout,
the ``rb _cov-job`` argv and its Slurm submission, the spec/result round trip, the
head's flow over a fake scheduler-backed backend whose coverage job runs the real
``rb _cov-job`` in a subprocess, and the job's run record and orphan sweep (#745).
"""

from __future__ import annotations

import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from serde.yaml import from_yaml
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.config.coverage import CoverageConfigFile
from rtl_buddy.config.dispatch import (
    DispatchConfigFile,
    JobResources,
    resolve_coverage_resources,
)
from rtl_buddy.dispatch import slurm as slurm_module
from rtl_buddy.dispatch.argv import coverage_job_argv, job_log_path
from rtl_buddy.dispatch.base import CoverageJobSpec, DispatchBackend, JobHandle
from rtl_buddy.dispatch.coverage_tail import (
    load_tail_result,
    load_tail_spec,
    write_tail_result,
    write_tail_spec,
)
from rtl_buddy.dispatch.local_parallel import LocalProcessBackend
from rtl_buddy.dispatch.plan import read_plan_token
from rtl_buddy.dispatch.run_manifest import (
    KIND_COVERAGE,
    STATUS_CANCELLED,
    STATUS_COLLECTED,
    STATUS_RUNNING,
    STATUS_STALE,
    decode_spec,
    discover_run_manifests,
    encode_spec,
    handles_from,
    load_run_manifest,
    run_manifest_path,
    write_coverage_manifest,
    write_run_manifest,
)
from rtl_buddy.dispatch.slurm import SlurmDispatchBackend
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.runner.result_io import write_result_json

# Aliased so pytest does not try to collect them as test classes.
from rtl_buddy.runner.test_results import TestPassResults as PassResults
from rtl_buddy.runner.test_results import TestResults as Results
from rtl_buddy.tools.vlog_cov import VlogCov


# cfg-dispatch.coverage


def _dispatch_cfg(text: str):
    return from_yaml(DispatchConfigFile, text).initialise()


def test_the_coverage_reservation_layers_like_compile():
    cfg = _dispatch_cfg(
        "resources: {mem: 2G, cpus: 2}\n"
        "coverage:\n"
        "  mem: 8G\n"
        '  time: "02:00:00"\n'
        "  modes:\n"
        "    cov: {mem: 16G}\n"
    )

    # cfg-dispatch.coverage over cfg-dispatch.resources, field by field.
    assert resolve_coverage_resources(cfg, builder_mode="reg") == JobResources(
        cpus=2, mem="8G", time="02:00:00"
    )
    # modes.cov layered on top: the tail exists only for a coverage run.
    assert resolve_coverage_resources(cfg, builder_mode="cov") == JobResources(
        cpus=2, mem="16G", time="02:00:00"
    )


def test_without_a_coverage_block_the_tail_takes_the_resources_reservation():
    cfg = _dispatch_cfg(
        "resources:\n  mem: 2G\n  modes:\n    cov: {mem: 6G}\n",
    )

    assert resolve_coverage_resources(cfg, builder_mode="cov").mem == "6G"
    assert resolve_coverage_resources(None) == JobResources()


@pytest.mark.parametrize(
    "block,needle",
    [
        ("coverage: {mem: 0}", "greater than zero"),
        ("coverage: {time: 240}", "sexagesimal"),
        ("coverage: {cpus: 0}", "at least 1"),
        ("coverage: {modes: {cov: {parallel: 2}}}", "parallel is not accepted"),
    ],
)
def test_a_bad_coverage_block_is_refused_at_load(block, needle):
    with pytest.raises(FatalRtlBuddyError, match=needle) as excinfo:
        _dispatch_cfg(block + "\n")
    assert "cfg-dispatch.coverage" in str(excinfo.value)


# cfg-coverage merge-timeout


def test_merge_timeout_defaults_to_no_limit():
    cfg = from_yaml(CoverageConfigFile, "name: verilator\n").initialise()
    assert cfg.get_merge_timeout() is None
    cfg = from_yaml(
        CoverageConfigFile, "name: verilator\nmerge-timeout: 900\n"
    ).initialise()
    assert cfg.get_merge_timeout() == 900


@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_non_positive_merge_timeout_is_refused(value):
    with pytest.raises(FatalRtlBuddyError, match="merge-timeout"):
        from_yaml(
            CoverageConfigFile, f"name: verilator\nmerge-timeout: {value}\n"
        ).initialise()


class _CovRootCfg:
    """The slice of RootConfig VlogCov.merge reads."""

    def __init__(self, root, timeout):
        self._root = str(root)
        self._timeout = timeout

    def get_project_rootdir(self):
        return self._root

    def get_coverage_merge_timeout(self, simulator_name):
        assert simulator_name == "verilator"
        return self._timeout


def _fake_verilator_coverage(tmp_path: Path, monkeypatch, *, sleep: float) -> None:
    """Put a `verilator_coverage` on PATH whose `--write` sleeps, then writes the merge."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "verilator_coverage"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--write" ]; then\n'
        f"  sleep {sleep}\n"
        '  echo "# SystemC::Coverage-3" > "$2"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _raw(tmp_path: Path) -> str:
    raw = tmp_path / "coverage.dat"
    raw.write_text("# SystemC::Coverage-3\n")
    return str(raw)


def test_a_merge_past_its_timeout_is_stopped_and_reported_failed(
    tmp_path: Path, monkeypatch
):
    _fake_verilator_coverage(tmp_path, monkeypatch, sleep=30)
    cov = VlogCov("verilator", root_cfg=_CovRootCfg(tmp_path, 0.5))
    monkeypatch.setattr(cov, "is_supported", lambda: True)

    metrics = cov.merge(raw_paths=[_raw(tmp_path)], outdir=str(tmp_path))

    assert metrics.merge_failed
    assert metrics.merged_path is None
    assert not (tmp_path / "coverage_merged.dat").exists()


def test_a_merge_without_a_timeout_runs_to_completion(tmp_path: Path, monkeypatch):
    _fake_verilator_coverage(tmp_path, monkeypatch, sleep=1)
    cov = VlogCov("verilator", root_cfg=_CovRootCfg(tmp_path, None))
    monkeypatch.setattr(cov, "is_supported", lambda: True)

    metrics = cov.merge(raw_paths=[_raw(tmp_path)], outdir=str(tmp_path))

    # An empty merge measures nothing and fails nothing, so there are no metrics.
    assert metrics is None or not metrics.merge_failed
    assert (tmp_path / "coverage_merged.dat").is_file()


# rb _cov-job argv and the Slurm submission


def _cov_spec(tmp_path: Path, **overrides) -> CoverageJobSpec:
    fields = dict(
        suite_dir=str(tmp_path),
        spec_json=tmp_path / ".dispatch" / "coverage" / "spec-t.json",
        result_json=tmp_path / ".dispatch" / "coverage" / "result-t.json",
        resources=JobResources(cpus=4, mem="32G", time="00:45:00"),
        log_path=tmp_path / ".dispatch" / "coverage" / "slurm-t.log",
        builder_mode="cov",
    )
    fields.update(overrides)
    return CoverageJobSpec(**fields)


def test_the_cov_job_argv_names_the_spec_result_and_command_root(tmp_path: Path):
    spec = _cov_spec(tmp_path, builder_override="vlt", run_tag="nightly")

    argv = coverage_job_argv(spec)

    assert argv[:4] == [sys.executable, "-m", "rtl_buddy", "--machine"]
    assert argv[argv.index("-M") + 1] == "cov"
    assert argv[argv.index("-B") + 1] == "vlt"
    tail = argv[argv.index("_cov-job") :]
    assert tail == [
        "_cov-job",
        "--spec",
        str(spec.spec_json),
        "--result-json",
        str(spec.result_json),
        "--command-root",
        str(tmp_path),
        "--run-tag",
        "nightly",
    ]
    # The job logs beside its envelope, never into the head's rtl_buddy.log.
    assert job_log_path(spec.result_json).name == "rtl_buddy-t.log"


def test_slurm_submits_the_tail_with_its_own_reservation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
    monkeypatch.delenv("SBATCH_CLUSTERS", raising=False)
    calls = []

    def run(argv, capture_output=True, text=True, cwd=None, timeout=None):
        calls.append((list(argv), cwd))
        return SimpleNamespace(returncode=0, stdout="991\n", stderr="")

    monkeypatch.setattr(slurm_module.subprocess, "run", run)
    backend = SlurmDispatchBackend(
        DispatchConfigFile(sbatch_args=["--partition=verif"]).initialise()
    )
    spec = _cov_spec(tmp_path)

    assert backend.dispatches_coverage_tail
    handle = backend.submit_coverage(spec)

    assert handle.job_id == "991"
    ((argv, cwd),) = calls
    assert cwd == str(tmp_path)
    assert argv[0] == "sbatch"
    for flag in (
        "--job-name=rb:coverage",
        f"--chdir={tmp_path}",
        "--time=00:45:00",
        "--cpus-per-task=4",
        "--mem=32G",
        f"--output={spec.log_path}",
        "--partition=verif",
    ):
        assert flag in argv, flag
    # Submitted after the fleet is collected: nothing to depend on.
    assert not any(a.startswith("--dependency") for a in argv)
    assert shlex.split(argv[argv.index("--wrap") + 1]) == coverage_job_argv(spec)


def test_a_refused_tail_submission_fails_loud(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(slurm_module, "require_tool", lambda name: None)
    monkeypatch.setattr(
        slurm_module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="QOS limit"),
    )
    backend = SlurmDispatchBackend(DispatchConfigFile().initialise())

    with pytest.raises(FatalRtlBuddyError, match="coverage job.*QOS limit"):
        backend.submit_coverage(_cov_spec(tmp_path))


def test_only_a_backend_off_the_submit_host_dispatches_the_tail():
    assert not DispatchBackend.dispatches_coverage_tail
    assert not LocalProcessBackend.dispatches_coverage_tail


# spec and result round trip


def _row(name, coverage):
    results = {"result": "PASS"}
    if coverage is not None:
        results["coverage"] = coverage
    return {"test_name": name, "results": Results(name=name, results=results)}


def test_the_spec_round_trips_and_shares_each_test_between_calls(tmp_path: Path):
    a = _row("a", {"raw_paths": ["/x/a.dat"]})
    b = _row("b", {"raw_paths": ["/x/b.dat"]})
    skipped = _row("c", None)
    reg_results = [
        {"test_suite": "s1/tests.yaml", "test_suite_path": "/p/s1", "results": [a]},
        {"test_suite": "s2/tests.yaml", "test_suite_path": "/p/s2", "results": [b]},
    ]
    calls = [
        dict(
            suite_results=[a, b, skipped],
            reg_results=reg_results,
            outdir="/p",
            suite_name="/p/regression.yaml",
            coverage_merge=True,
            source_roots=["/p/s1", "/p/s2"],
            dir_summary_paths=["rtl"],
            command="regression",
            model_mode="totals",
        )
    ]

    path, results = write_tail_spec(tmp_path / "spec.json", calls, run_token="tok")
    token, loaded, coverages = load_tail_spec(path)

    assert token == "tok"
    assert [r.name for r in results] == ["a", "b", "c"]
    (call,) = loaded
    assert call["outdir"] == "/p" and call["model_mode"] == "totals"
    assert call["coverage_merge"] is True
    assert [row["test_name"] for row in call["suite_results"]] == ["a", "b", "c"]
    assert call["suite_results"][2]["results"].results == {}
    # One object per test: an update through a suite is seen through the flat list.
    suite_a = call["reg_results"][0]["results"][0]
    assert suite_a is call["suite_results"][0]
    suite_a["results"].results["coverage"]["lcov_path"] = "/p/cov_dir/a.info"
    assert coverages[0]["lcov_path"] == "/p/cov_dir/a.info"
    assert call["reg_results"][1]["test_suite_path"] == "/p/s2"


def test_a_stale_or_short_result_is_refused(tmp_path: Path):
    path = write_tail_result(
        tmp_path / "result.json",
        run_token="old",
        outcomes=[(["line"], {"merged": None})],
        tests=[None],
    )

    with pytest.raises(FatalRtlBuddyError, match="different run"):
        load_tail_result(
            path, expected_run_token="new", expected_calls=1, expected_tests=1
        )
    with pytest.raises(FatalRtlBuddyError, match="does not answer"):
        load_tail_result(
            path, expected_run_token="old", expected_calls=2, expected_tests=1
        )
    with pytest.raises(FatalRtlBuddyError, match="missing"):
        load_tail_result(
            tmp_path / "absent.json",
            expected_run_token="old",
            expected_calls=1,
            expected_tests=1,
        )
    outcomes, tests = load_tail_result(
        path, expected_run_token="old", expected_calls=1, expected_tests=1
    )
    assert outcomes == [(["line"], {"merged": None})]
    assert tests == [None]


# the head's flow, over a fake scheduler-backed backend


def _dat_record(*, file, line, type_, name, module="blk", col=1, hits=1):
    keys = [
        ("f", file),
        ("l", str(line)),
        ("n", str(col)),
        ("t", type_),
        ("page", f"v_{type_}/{module}"),
        ("o", name),
        ("h", f"tb.{module}.{name}"),
    ]
    blob = "".join(f"\x01{k}\x02{v}" for k, v in keys)
    return f"C '{blob}' {hits}\n"


def _covered_results(suite_dir: Path, test_name: str, run_id=None) -> PassResults:
    """A passing result whose run wrote a one-line, one-toggle ``coverage.dat``."""
    run_dir = suite_dir / "artefacts" / test_name
    if run_id is not None:
        run_dir /= f"run-{run_id:04d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    source = os.path.relpath(suite_dir / "src" / "example.sv", run_dir)
    raw = run_dir / "coverage.dat"
    raw.write_text(
        "# SystemC::Coverage-3\n"
        + _dat_record(file=source, line=1, type_="line", name="b")
        + _dat_record(file=source, line=2, type_="toggle", name="q", hits=0)
    )
    results = PassResults(name=test_name + "/results")
    results.results["coverage"] = {"raw_paths": [str(raw)]}
    return results


class _TailBackend(DispatchBackend):
    """A scheduler-backed fake: sim jobs 'run' at submit with coverage, and the coverage
    job runs the real ``rb _cov-job`` argv in a subprocess, as a compute node would.
    """

    name = "fake"
    dispatches_coverage_tail = True

    def __init__(self, *, run_tail=True):
        self.run_tail = run_tail
        self.coverage_specs = []
        self.waited = []
        self.cancelled = []
        self.tail_returncode = None

    def submit_build(self, spec, *, dependency=None):
        return JobHandle(job_id="fake-build", spec=spec)

    def submit(self, spec, *, dependency=None, delay_sec=0.0):
        results = _covered_results(Path(spec.suite_dir), spec.test_name, spec.run_id)
        write_result_json(
            spec.result_json,
            test_name=spec.test_name,
            run_id=spec.run_id,
            results=results,
            run_token=read_plan_token(spec.plan_path) if spec.plan_path else None,
        )
        return JobHandle(job_id=f"fake-{spec.test_name}", spec=spec)

    def submit_coverage(self, spec):
        self.coverage_specs.append(spec)
        if self.run_tail:
            proc = subprocess.run(
                coverage_job_argv(spec),
                cwd=spec.suite_dir,
                capture_output=True,
                text=True,
            )
            self.tail_returncode = proc.returncode
            self.tail_output = proc.stdout + proc.stderr
        return JobHandle(job_id="fake-coverage", spec=spec)

    def wait_all(self, handles, *, extra_wait=0.0):
        self.waited.append([h.job_id for h in handles])

    def cancel_all(self, handles):
        self.cancelled.append([h.job_id for h in handles if h is not None])


def _use(monkeypatch, backend):
    monkeypatch.setattr(
        rtl_buddy_module,
        "create_dispatch_backend",
        lambda name, cfg, *, config_path=None: (
            backend if name not in (None, "local") else None
        ),
    )


def _prepare(project: Path) -> None:
    root_cfg = project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text().replace(
            '    builder: "echo"\n',
            '    builder: "echo"\n    simulator-family: "verilator"\n',
        )
        + "cfg-dispatch:\n"
        "  coverage:\n"
        "    mem: 4G\n"
        "    modes:\n"
        '      cov: {mem: 24G, time: "00:40:00"}\n'
    )


def _no_head_tail(monkeypatch):
    """Fail the test if the head runs any part of the tail itself."""

    def refuse(*args, **kwargs):
        raise AssertionError("the head ran the coverage tail in-process")

    monkeypatch.setattr(rtl_buddy_module.CoverageReporter, "build_metadata", refuse)


def _envelope(result) -> dict:
    return json.loads(result.output.strip().splitlines()[-1])


def test_a_dispatched_regression_runs_the_tail_as_one_job(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _TailBackend()
    _use(monkeypatch, backend)
    _no_head_tail(monkeypatch)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        [
            "--machine",
            "-M",
            "cov",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert backend.tail_returncode == 0, backend.tail_output
    (spec,) = backend.coverage_specs
    # Reserved from cfg-dispatch.coverage with modes.cov on top, run from the head's
    # command root.
    assert (spec.resources.mem, spec.resources.time) == ("24G", "00:40:00")
    assert spec.suite_dir == str(minimal_project)
    assert spec.builder_mode == "cov"
    # The head waited on the tail as on the fleet, after the fleet.
    assert backend.waited[-1] == ["fake-coverage"]
    assert backend.cancelled == []

    # The job wrote the model and manifest under the command root...
    cov_dir = minimal_project / "cov_dir"
    model = json.loads((cov_dir / "coverage-model.json").read_text())
    assert model["totals"]["line"] == {"found": 1, "hit": 1, "ratio": 1.0}
    manifest = json.loads((cov_dir / "manifest.json").read_text())
    assert manifest["command"] == "regression"
    # ...and the head reports what it read back.
    coverage = _envelope(result)["payload"]["coverage"]
    assert coverage["artefacts"]["manifest"] == "cov_dir/manifest.json"
    assert "tail_failed" not in coverage


def test_a_dispatched_test_runs_the_tail_as_one_job(minimal_project: Path, monkeypatch):
    _prepare(minimal_project)
    backend = _TailBackend()
    _use(monkeypatch, backend)
    _no_head_tail(monkeypatch)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["--machine", "-M", "cov", "test", "basic", "--dispatch", "slurm"],
    )

    assert result.exit_code == 0, result.output
    assert backend.tail_returncode == 0, backend.tail_output
    assert (minimal_project / "cov_dir" / "manifest.json").is_file()
    coverage = _envelope(result)["payload"]["coverage"]
    assert coverage["artefacts"]["model"] == "cov_dir/coverage-model.json"


def test_a_tail_job_that_leaves_no_answer_fails_the_run_but_keeps_the_results(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _TailBackend(run_tail=False)
    _use(monkeypatch, backend)
    _no_head_tail(monkeypatch)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        [
            "--machine",
            "-M",
            "cov",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ],
    )

    envelope = _envelope(result)
    assert envelope["exit_code"] == 1, result.output
    payload = envelope["payload"]
    # Every collected result is still reported ("extra" is above -l 0).
    assert [row["result"] for row in payload["results"]] == ["PASS", "SKIP"]
    failure = payload["coverage"]["tail_failed"]
    assert failure["job_id"] == "fake-coverage"
    assert "missing" in failure["reason"]
    assert not (minimal_project / "cov_dir" / "manifest.json").exists()


def test_a_refused_tail_submission_fails_the_run_but_keeps_the_results(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _TailBackend()

    def refuse(spec):
        raise FatalRtlBuddyError("sbatch failed for the coverage job (rc=1): QOS")

    backend.submit_coverage = refuse
    _use(monkeypatch, backend)
    _no_head_tail(monkeypatch)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["--machine", "-M", "cov", "test", "basic", "--dispatch", "slurm"],
    )

    envelope = _envelope(result)
    assert envelope["exit_code"] == 1, result.output
    failure = envelope["payload"]["coverage"]["tail_failed"]
    assert failure["job_id"] is None
    assert "QOS" in failure["reason"]


def test_a_run_without_coverage_submits_no_tail(minimal_project: Path, monkeypatch):
    _prepare(minimal_project)
    backend = _TailBackend()
    _use(monkeypatch, backend)
    # Sim jobs that recorded no coverage.

    def submit(spec, *, dependency=None, delay_sec=0.0):
        write_result_json(
            spec.result_json,
            test_name=spec.test_name,
            run_id=spec.run_id,
            results=PassResults(name=spec.test_name + "/results"),
            run_token=read_plan_token(spec.plan_path) if spec.plan_path else None,
        )
        return JobHandle(job_id="fake-sim", spec=spec)

    backend.submit = submit

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["regression", "-c", "regression.yaml", "--dispatch", "slurm"],
    )

    assert result.exit_code == 0, result.output
    assert backend.coverage_specs == []


def test_a_backend_on_the_submit_host_keeps_the_tail_in_process(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _TailBackend()
    backend.dispatches_coverage_tail = False
    _use(monkeypatch, backend)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        [
            "--machine",
            "-M",
            "cov",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert backend.coverage_specs == []
    assert (minimal_project / "cov_dir" / "manifest.json").is_file()


@pytest.mark.skipif(
    shutil.which("verilator_coverage") is None,
    reason="verilator_coverage not installed",
)
def test_the_merge_runs_in_the_tail_job(minimal_project: Path, monkeypatch):
    _prepare(minimal_project)
    backend = _TailBackend()
    _use(monkeypatch, backend)
    _no_head_tail(monkeypatch)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        [
            "--machine",
            "-M",
            "cov",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
            "--coverage-merge",
        ],
    )

    assert result.exit_code == 0, result.output
    assert backend.tail_returncode == 0, backend.tail_output
    assert (minimal_project / "cov_dir" / "coverage_merged.dat").is_file()
    coverage = _envelope(result)["payload"]["coverage"]
    assert coverage["merge_failed"] is False
    assert coverage["artefacts"]["merged_raw"] == "cov_dir/coverage_merged.dat"


def test_a_failed_tail_leaves_no_earlier_verdict_for_readers(
    minimal_project: Path, monkeypatch
):
    from rtl_buddy.cov.manifest import discover_manifests

    _prepare(minimal_project)
    argv = ["--machine", "-M", "cov", "test", "basic", "--dispatch", "slurm"]
    # A successful dispatched run writes the manifest and model...
    good = _TailBackend()
    _use(monkeypatch, good)
    first = RtlBuddy(name="test_cov_tail")
    result = CliRunner().invoke(first.app, argv)
    assert result.exit_code == 0, result.output
    # Both runs share this process; let the second take the artefact lock.
    first._artifact_locks.release_all()
    cov_dir = minimal_project / "cov_dir"
    assert (cov_dir / "manifest.json").is_file()
    assert (cov_dir / "coverage-model.json").is_file()
    assert discover_manifests(minimal_project) == [str(cov_dir / "manifest.json")]

    # ...then a later run's tail job dies without an answer.
    failed = _TailBackend(run_tail=False)
    _use(monkeypatch, failed)
    result = CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, argv)

    envelope = _envelope(result)
    assert envelope["exit_code"] == 1, result.output
    assert "tail_failed" in envelope["payload"]["coverage"]
    # The earlier run's verdict is gone, so no reader presents it as this run's.
    assert not (cov_dir / "manifest.json").exists()
    assert not (cov_dir / "coverage-model.json").exists()
    assert discover_manifests(minimal_project) == []


def test_a_manifest_only_tail_stays_in_process(minimal_project: Path, monkeypatch):
    _prepare(minimal_project)
    backend = _TailBackend()
    _use(monkeypatch, backend)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        [
            "--machine",
            "-M",
            "cov",
            "test",
            "basic",
            "--dispatch",
            "slurm",
            "--coverage-model",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    # No merge, export or model file asked for: nothing worth a job.
    assert backend.coverage_specs == []
    cov_dir = minimal_project / "cov_dir"
    assert (cov_dir / "manifest.json").is_file()
    assert not (cov_dir / "coverage-model.json").exists()
    coverage = _envelope(result)["payload"]["coverage"]
    assert "tail_failed" not in coverage


def test_a_manifest_only_call_is_light_and_any_heavy_flag_is_not():
    from rtl_buddy.dispatch.coverage_tail import is_manifest_only

    base = {"model_mode": "none", "dir_summary_paths": [], "source_summary": False}
    assert is_manifest_only(base)
    assert not is_manifest_only({**base, "model_mode": "full"})
    assert not is_manifest_only({**base, "model_mode": "totals"})
    for flag in (
        "coverage_merge",
        "coverage_merge_raw",
        "coverage_merge_info_process",
        "coverage_html",
        "coverage_coverview",
        "source_summary",
    ):
        assert not is_manifest_only({**base, flag: True}), flag
    assert not is_manifest_only({**base, "dir_summary_paths": ["src"]})


# the coverage job in the run manifest (#745)


def test_a_coverage_job_spec_round_trips_through_its_record(tmp_path: Path):
    spec = _cov_spec(tmp_path, run_tag="nightly", extra_sim_timeout=30)

    assert decode_spec(json.loads(json.dumps(encode_spec(spec)))) == spec


def test_coverage_records_and_fleet_records_are_discovered_apart(tmp_path: Path):
    dispatch = tmp_path / "artefacts" / ".dispatch"
    fleet = write_run_manifest(
        run_manifest_path(dispatch, "fleet"),
        run_token="fleet",
        backend="slurm",
        started_at=0.0,
        suite_config=str(tmp_path / "tests.yaml"),
        plan=str(dispatch / "plan.json"),
        rows=[],
        status=STATUS_RUNNING,
    )
    cov = write_coverage_manifest(
        run_manifest_path(dispatch / "coverage", "cov"),
        run_token="cov",
        backend="slurm",
        started_at=0.0,
        command_root=str(tmp_path),
        handle=JobHandle(job_id="77", spec=_cov_spec(tmp_path), cluster="c1"),
    )

    # Fleet discovery scans `.dispatch/coverage/` too, and must not take the coverage
    # job for a fleet it could adopt.
    assert [p for p, _ in discover_run_manifests(dispatch, run_token=None)] == [fleet]
    found = discover_run_manifests(
        dispatch / "coverage", run_token=None, kind=KIND_COVERAGE
    )
    assert [p for p, _ in found] == [cov]
    ((_, payload),) = found
    (handle,) = handles_from(payload)
    assert (handle.job_id, handle.cluster) == ("77", "c1")
    assert isinstance(handle.spec, CoverageJobSpec)


class _LiveTailBackend(_TailBackend):
    """``_TailBackend`` with a queue: ``live`` holds the ids still on it."""

    def __init__(self, *, live=(), wait_error=None, **kwargs):
        super().__init__(**kwargs)
        self.live = set(live)
        # job id -> exception raised by the wait that includes it.
        self.wait_error = dict(wait_error or {})

    def live_job_ids(self, handles, *, timeout_s=None):
        return {h.job_id for h in handles if h is not None and h.job_id in self.live}

    def wait_all(self, handles, *, extra_wait=0.0):
        super().wait_all(handles, extra_wait=extra_wait)
        for h in handles:
            error = self.wait_error.pop(h.job_id, None)
            if error is not None:
                raise error
        self.live -= {h.job_id for h in handles}

    def cancel_all(self, handles):
        super().cancel_all(handles)
        self.live -= {h.job_id for h in handles if h is not None}


def _coverage_dir(project: Path) -> Path:
    return project / "artefacts" / ".dispatch" / "coverage"


def _coverage_records(project: Path) -> list[dict]:
    return [
        load_run_manifest(path)[0]
        for path in sorted(_coverage_dir(project).glob("run-*.json"))
    ]


def _orphan_coverage_record(project: Path, job_id: str = "old-cov") -> Path:
    """The record a head SIGKILLed while waiting on its coverage job leaves behind."""
    return write_coverage_manifest(
        run_manifest_path(_coverage_dir(project), "earlier"),
        run_token="earlier",
        backend="fake",
        started_at=0.0,
        command_root=str(project),
        handle=JobHandle(job_id=job_id, spec=_cov_spec(project)),
    )


_COV_TEST = ["--machine", "-M", "cov", "test", "basic", "--dispatch", "slurm"]


def test_the_coverage_job_is_recorded_and_retired_once_collected(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _LiveTailBackend()
    _use(monkeypatch, backend)

    result = CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, _COV_TEST)

    assert result.exit_code == 0, result.output
    (record,) = _coverage_records(minimal_project)
    assert record["kind"] == KIND_COVERAGE
    assert record["coverage"]["job_id"] == "fake-coverage"
    assert record["coverage"]["spec"]["kind"] == "coverage"
    assert record["command_root"] == str(minimal_project)
    # The job left the queue under this head, so the next run has nothing to find.
    assert record["status"] == STATUS_COLLECTED


def test_an_interrupted_head_cancels_its_coverage_job_and_says_so(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _LiveTailBackend(wait_error={"fake-coverage": KeyboardInterrupt()})
    backend.submit_coverage = lambda spec: (
        backend.live.add("fake-coverage")
        or JobHandle(job_id="fake-coverage", spec=spec)
    )
    _use(monkeypatch, backend)

    CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, _COV_TEST)

    assert ["fake-coverage"] in backend.cancelled
    (record,) = _coverage_records(minimal_project)
    assert record["status"] == STATUS_CANCELLED


def test_a_coverage_job_that_outlives_its_head_is_waited_for_before_cov_dir(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    orphan = _orphan_coverage_record(minimal_project)
    backend = _LiveTailBackend(live={"old-cov"})
    _use(monkeypatch, backend)

    result = CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, _COV_TEST)

    assert result.exit_code == 0, result.output
    assert "old-cov" in result.output  # dispatch.coverage_orphan_found
    # warn: left running, never cancelled...
    assert backend.cancelled == []
    # ...and waited for before this run's tail job went out.
    waits = backend.waited
    assert waits.index(["old-cov"]) < waits.index(["fake-coverage"])
    assert load_run_manifest(orphan)[0]["status"] == STATUS_STALE
    assert (minimal_project / "cov_dir" / "manifest.json").is_file()


def test_orphans_cancel_takes_an_earlier_coverage_job_down_before_submitting(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    orphan = _orphan_coverage_record(minimal_project)
    backend = _LiveTailBackend(live={"old-cov"})
    _use(monkeypatch, backend)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app, _COV_TEST + ["--orphans", "cancel"]
    )

    assert result.exit_code == 0, result.output
    assert backend.cancelled == [["old-cov"]]
    assert ["old-cov"] not in backend.waited
    assert load_run_manifest(orphan)[0]["status"] == STATUS_CANCELLED
    assert backend.coverage_specs, "this run's own tail still ran"


def test_an_earlier_coverage_job_that_outlasts_max_wait_fails_only_the_tail(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    orphan = _orphan_coverage_record(minimal_project)
    backend = _LiveTailBackend(
        live={"old-cov"},
        wait_error={"old-cov": FatalRtlBuddyError("max-wait 60s exceeded")},
    )
    _use(monkeypatch, backend)

    result = CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, _COV_TEST)

    envelope = _envelope(result)
    assert envelope["exit_code"] == 1, result.output
    failure = envelope["payload"]["coverage"]["tail_failed"]
    assert failure["job_id"] is None
    assert "old-cov" in failure["reason"] and "max-wait" in failure["reason"]
    # No tail of this run's raced the survivor, and it was not cancelled.
    assert backend.coverage_specs == []
    assert backend.cancelled == []
    assert load_run_manifest(orphan)[0]["status"] == STATUS_RUNNING
    assert [r["result"] for r in envelope["payload"]["results"]] == ["PASS"]


def test_an_earlier_coverage_job_that_survives_orphans_cancel_stops_the_run(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    orphan = _orphan_coverage_record(minimal_project)
    backend = _LiveTailBackend(live={"old-cov"})
    backend.cancel_all = lambda handles: backend.cancelled.append(
        [h.job_id for h in handles]
    )
    _use(monkeypatch, backend)
    monkeypatch.setattr(RtlBuddy, "ORPHAN_CANCEL_WAIT_S", 0.0)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app, _COV_TEST + ["--orphans", "cancel"]
    )

    assert result.exit_code != 0
    message = str(result.exception)
    assert "cov_dir" in message and "scancel old-cov" in message
    # Fatal before anything of this run's went out.
    assert backend.coverage_specs == []
    assert not list((minimal_project / "artefacts" / ".dispatch").glob("plan-*.json"))
    assert load_run_manifest(orphan)[0]["status"] == STATUS_RUNNING


def test_an_interrupt_while_waiting_on_an_earlier_coverage_job_leaves_it_running(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    orphan = _orphan_coverage_record(minimal_project)
    backend = _LiveTailBackend(
        live={"old-cov"}, wait_error={"old-cov": KeyboardInterrupt()}
    )
    _use(monkeypatch, backend)

    result = CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, _COV_TEST)

    assert result.exit_code == 130, result.output
    # Not this run's job: left running and recorded as found, for the next run.
    assert backend.cancelled == []
    assert backend.coverage_specs == []
    assert load_run_manifest(orphan)[0]["status"] == STATUS_RUNNING


def test_a_coverage_job_cancelled_at_max_wait_retires_its_record(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _LiveTailBackend(
        wait_error={"fake-coverage": FatalRtlBuddyError("max-wait 60s exceeded")}
    )
    backend.submit_coverage = lambda spec: (
        backend.live.add("fake-coverage")
        or JobHandle(job_id="fake-coverage", spec=spec)
    )
    _use(monkeypatch, backend)

    result = CliRunner().invoke(RtlBuddy(name="test_cov_tail").app, _COV_TEST)

    envelope = _envelope(result)
    assert envelope["exit_code"] == 1, result.output
    assert envelope["payload"]["coverage"]["tail_failed"]["job_id"] == "fake-coverage"
    assert ["fake-coverage"] in backend.cancelled
    (record,) = _coverage_records(minimal_project)
    assert record["status"] == STATUS_CANCELLED


# rb randtest -M cov runs the same tail (#746)


def test_each_seed_is_its_own_coverage_test_on_the_same_result_object():
    first = {"test_name": "basic", "randmode_i": 1, "results": Results(name="a")}
    second = {"test_name": "basic", "randmode_i": 12, "results": Results(name="b")}
    single = {"test_name": "basic", "randmode_i": None, "results": Results(name="c")}

    rows = RtlBuddy._seed_coverage_rows([first, second, single])

    assert [r["test_name"] for r in rows] == [
        "basic/run-0001",
        "basic/run-0012",
        "basic",
    ]
    # Coverage the tail adds lands on the head's rows, which keep their own names.
    assert [r["results"] for r in rows] == [
        first["results"],
        second["results"],
        single["results"],
    ]
    assert first["test_name"] == "basic"


def _fake_local_seeds(monkeypatch, calls):
    """Run randtest's in-process seeds as passes that each wrote coverage."""

    def run(self, suite_cfg, test_name=None, run_ids=None, **kwargs):
        calls.append(list(run_ids))
        suite_dir = Path(suite_cfg.get_path()).resolve().parent
        return [
            {
                "test_name": test_name,
                "randmode_i": run_id,
                "results": _covered_results(suite_dir, test_name, run_id),
            }
            for run_id in run_ids
        ]

    monkeypatch.setattr(RtlBuddy, "_do_test_suite", run)


def _manifest(project: Path) -> dict:
    return json.loads((project / "cov_dir" / "manifest.json").read_text())


def test_a_local_cov_randtest_writes_the_model_and_manifest(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    calls = []
    _fake_local_seeds(monkeypatch, calls)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["--machine", "-M", "cov", "randtest", "basic", "3"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [[1, 2, 3]]
    manifest = _manifest(minimal_project)
    assert manifest["command"] == "randtest"
    # One coverage test per seed, named like its artefact directory.
    assert [t["name"] for t in manifest["tests"]] == [
        "basic/run-0001",
        "basic/run-0002",
        "basic/run-0003",
    ]
    assert (minimal_project / "cov_dir" / "coverage-model.json").is_file()
    coverage = _envelope(result)["payload"]["coverage"]
    assert coverage["artefacts"]["manifest"] == "cov_dir/manifest.json"
    # The machine rows keep the plain test name and the run id.
    rows = _envelope(result)["payload"]["results"]
    assert [(r["name"], r["run_id"]) for r in rows] == [
        ("basic", 1),
        ("basic", 2),
        ("basic", 3),
    ]


def test_a_cov_replay_runs_the_tail_in_process(minimal_project: Path, monkeypatch):
    _prepare(minimal_project)
    calls = []
    _fake_local_seeds(monkeypatch, calls)
    backend = _TailBackend()
    _use(monkeypatch, backend)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        [
            "--machine",
            "-M",
            "cov",
            "randtest",
            "basic",
            "-r",
            "7",
            "--dispatch",
            "slurm",
        ],
    )

    assert result.exit_code == 0, result.output
    # A replay stays local, and so does its tail.
    assert calls == [[7]]
    assert backend.coverage_specs == []
    assert [t["name"] for t in _manifest(minimal_project)["tests"]] == [
        "basic/run-0007"
    ]


def test_a_dispatched_cov_randtest_runs_the_tail_as_one_job(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    backend = _LiveTailBackend()
    _use(monkeypatch, backend)
    _no_head_tail(monkeypatch)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["--machine", "-M", "cov", "randtest", "basic", "2", "--dispatch", "slurm"],
    )

    assert result.exit_code == 0, result.output
    assert backend.tail_returncode == 0, backend.tail_output
    (spec,) = backend.coverage_specs
    assert (spec.resources.mem, spec.resources.time) == ("24G", "00:40:00")
    assert backend.waited[-1] == ["fake-coverage"]
    manifest = _manifest(minimal_project)
    assert manifest["command"] == "randtest"
    assert [t["name"] for t in manifest["tests"]] == [
        "basic/run-0001",
        "basic/run-0002",
    ]
    assert "artefacts" in _envelope(result)["payload"]["coverage"]
    # ...and the job is in the run manifest like `test`'s and `regression`'s (#745).
    (record,) = _coverage_records(minimal_project)
    assert record["status"] == STATUS_COLLECTED


def test_a_dispatched_randtest_waits_for_an_earlier_coverage_job(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)
    _orphan_coverage_record(minimal_project)
    backend = _LiveTailBackend(live={"old-cov"})
    _use(monkeypatch, backend)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["--machine", "-M", "cov", "randtest", "basic", "2", "--dispatch", "slurm"],
    )

    assert result.exit_code == 0, result.output
    waits = backend.waited
    assert waits.index(["old-cov"]) < waits.index(["fake-coverage"])


def test_a_randtest_asking_for_a_merge_without_coverage_fails_loud(
    minimal_project: Path, monkeypatch
):
    _prepare(minimal_project)

    def run(self, suite_cfg, test_name=None, run_ids=None, **kwargs):
        return [
            {
                "test_name": test_name,
                "randmode_i": run_id,
                "results": PassResults(name=f"{test_name}/results"),
            }
            for run_id in run_ids
        ]

    monkeypatch.setattr(RtlBuddy, "_do_test_suite", run)

    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_tail").app,
        ["randtest", "basic", "2", "--coverage-merge"],
    )

    assert isinstance(result.exception, FatalRtlBuddyError)
    assert "no coverage data" in str(result.exception)
