"""The coverage tail (merge, model build, LCOV exports, manifest) as one dispatched job (#650).

Covers the ``cfg-dispatch.coverage`` reservation, the ``cfg-coverage`` merge timeout,
the ``rb _cov-job`` argv and its Slurm submission, the spec/result round trip, and the
head's flow over a fake scheduler-backed backend whose coverage job runs the real
``rb _cov-job`` in a subprocess.
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
        run_dir = Path(spec.suite_dir) / "artefacts" / spec.test_name
        run_dir.mkdir(parents=True, exist_ok=True)
        raw = run_dir / "coverage.dat"
        raw.write_text(
            "# SystemC::Coverage-3\n"
            + _dat_record(file="../../src/example.sv", line=1, type_="line", name="b")
            + _dat_record(
                file="../../src/example.sv", line=2, type_="toggle", name="q", hits=0
            )
        )
        results = PassResults(name=spec.test_name + "/results")
        results.results["coverage"] = {"raw_paths": [str(raw)]}
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
