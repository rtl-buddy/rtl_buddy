"""End-to-end dispatch integration test against Slurm CLI shims (#351 P4).

Unlike the unit tests (fake backend / mocked subprocess), this drives the
*real* ``SlurmDispatchBackend`` through shim ``sbatch``/``squeue``/
``sacct``/``scancel`` executables on PATH, and a fake ``verilator`` that
emits a ``simv`` printing ``PASS``. So the real subprocess boundary, the
real array manifest/script, the real ``rb _test-job`` compile→sim→post
path, envelope collection, sacct telemetry parsing, and reservation
right-sizing all run — with no scheduler or simulator. This is the CI
job the issue calls for: "mocked sbatch/sacct shim on PATH".

Skipped where bash or the shims aren't usable (non-POSIX).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rtl_buddy.dispatch.argv import job_log_path

_REPO = Path(__file__).resolve().parent.parent
_SHIMS = _REPO / "tests" / "dispatch_shims"
# `scontrol` is an optional Slurm binary and only the build job's per-key
# release (#548) needs one, so its shim sits in its own directory: every
# other test in this module runs with no `scontrol` on PATH, which is the
# configuration those tests were written against.
_SCONTROL_SHIM = _SHIMS / "scontrol_shim"
_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_project"
_SWEEP_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_sweep_project"
_PARALLEL_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_parallel_project"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="dispatch shim e2e needs a POSIX shell",
)


_COLOCATED_BASES = {
    "tests-left.yaml": ["left_alpha", "left_beta"],
    "tests-right.yaml": ["right_alpha", "right_beta"],
}
_COLOCATED_EXPECTED = {
    config: [f"{name}_planned" for name in names]
    for config, names in _COLOCATED_BASES.items()
}


def _write_colocated_configs(project: Path) -> None:
    source = (project / "verif" / "blk" / "tests.yaml").read_text()
    for config_name, names in _COLOCATED_BASES.items():
        (project / "verif" / "blk" / config_name).write_text(
            source.replace("  - name: alpha\n", f"  - name: {names[0]}\n")
            .replace("  - name: beta\n", f"  - name: {names[1]}\n")
            .replace("    sweep:\n", "    sweep:\n      path: colocated-sweep.py\n")
        )
    (project / "verif" / "blk" / "colocated-sweep.py").write_text(
        "import copy\n"
        "import os\n"
        "counter = os.environ.get('RB_SWEEP_COUNTER')\n"
        "if counter:\n"
        "    with open(counter, 'a') as stream:\n"
        "        stream.write('1\\n')\n"
        "cfg = copy.deepcopy(test_cfg)\n"
        "cfg.name += '_planned'\n"
        "out_test_cfgs = [cfg]\n"
    )
    (project / "regression.yaml").write_text(
        "rtl-buddy-filetype: reg_config\n"
        "test-configs:\n"
        "  - verif/blk/tests-left.yaml\n"
        "  - verif/blk/tests-right.yaml\n"
    )


def _run_regression(
    work_dir: Path,
    extra_args=(),
    fixture=_FIXTURE,
    extra_env=None,
    prepare_project=None,
    extra_path_dirs=(),
):
    project = work_dir / "proj"
    shutil.copytree(fixture, project)
    if prepare_project is not None:
        prepare_project(project)
    return _rerun_regression(
        project,
        work_dir,
        extra_args=extra_args,
        extra_env=extra_env,
        extra_path_dirs=extra_path_dirs,
    )


def _rerun_regression(
    project: Path,
    work_dir: Path,
    extra_args=(),
    extra_env=None,
    extra_path_dirs=(),
):
    """A second `rb regression` over a project a previous one already ran.

    Same shim environment and the same job db, so the two runs share the
    scheduler's id space exactly as two invocations against one cluster do
    — which is what an adoption has to be judged against (#521).
    """
    env = dict(os.environ)
    prefix = os.pathsep.join(str(path) for path in (*extra_path_dirs, _SHIMS))
    env["PATH"] = f"{prefix}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(work_dir / "jobs.db")
    env["RB_SHIM_LOG"] = str(work_dir / "jobs.log")
    env.update(extra_env or {})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
            *extra_args,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    envelope = None
    for line in proc.stdout.splitlines():
        if line.startswith('{"command"'):
            envelope = json.loads(line)
    # The shim tees each _test-job's own stdout/stderr here — the likely
    # failure mode is a broken job, so surface it in the assertion context.
    job_log = work_dir / "jobs.log"
    diag = proc.stdout + proc.stderr
    if job_log.exists():
        diag += "\n--- job log ---\n" + job_log.read_text()
    return proc, envelope, project, diag


@pytest.fixture(scope="module")
def shim_run(tmp_path_factory):
    # One real regression subprocess shared by the assertions below (both
    # inspect different slices of the same byte-identical run).
    return _run_regression(tmp_path_factory.mktemp("dispatch"))


def test_shim_regression_runs_real_pipeline_to_pass(shim_run):
    proc, envelope, project, diag = shim_run
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    # Both tests ran through the real backend -> real _test-job -> fake
    # verilator's PASS simv.
    assert results == {"alpha": "PASS", "beta": "PASS"}

    # The array manifest + per-element result envelopes really exist.
    dispatch_dirs = list(project.glob("verif/blk/artefacts/*/dispatch"))
    assert dispatch_dirs, "no dispatch artefacts written"
    envelopes = list(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    assert len(envelopes) == 2
    one = json.loads(envelopes[0].read_text())
    assert one["result"]["results"]["result"] == "PASS"

    # Each job logged beside its own envelope, not into the head's
    # verif/blk/rtl_buddy.log (#437).
    for env in envelopes:
        assert job_log_path(env).exists(), f"no job log beside {env}"
    head_log = (project / "verif" / "blk" / "rtl_buddy.log").read_text()
    assert "command.test_job" not in head_log


def test_shim_regression_attaches_sacct_telemetry_and_advice(shim_run):
    proc, envelope, project, diag = shim_run
    assert proc.returncode == 0, diag

    # sacct telemetry (5s of a 60-min limit; 20M of 512M) travels into the
    # envelope and drives over-reservation advice.
    envelopes = list(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    tele = json.loads(envelopes[0].read_text())["telemetry"]
    assert tele["state"] == "COMPLETED"
    assert tele["timelimit_s"] == 3600  # TimelimitRaw 60 min -> seconds
    assert tele["max_rss_bytes"] == 20480 * 1024

    advice = envelope["payload"]["reservation_advice"]
    assert advice, "expected reservation advice from over-reserved shim jobs"
    directions = {(a["resource"], a["direction"]) for a in advice}
    assert ("time", "reduce") in directions
    assert ("mem", "reduce") in directions
    # Edit hint points at the real tests.yaml field.
    hint = advice[0]["edit_hint"]
    assert hint["path"].startswith("tests[name=")
    assert hint["path"].endswith((".time", ".mem", ".cpus"))


def test_shim_sweep_hook_runs_once_across_builds_and_arrays(tmp_path_factory):
    """The sweep hook expands exactly once — on the head — no matter how
    many sim jobs or build jobs the dispatch fans out to.

    Regression against the pre-plan-manifest behaviour where the hook ran
    in the head fan-out, again in the build job, and again in every sim
    job. This drives the *real* backend through the shims: a two-suite
    regression (two model builds) whose first suite carries a `sweep` that
    expands to three variants (an array > 1). The hook appends its pid to
    ``$RB_SWEEP_COUNTER`` on every execution, so the file must end up with
    a single line.
    """
    work = tmp_path_factory.mktemp("sweep_idem")
    project = work / "proj"
    shutil.copytree(_SWEEP_FIXTURE, project)
    counter = work / "sweep_execs.txt"

    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(work / "jobs.db")
    env["RB_SHIM_LOG"] = str(work / "jobs.log")
    env["RB_SWEEP_COUNTER"] = str(counter)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "--machine",
            "regression",
            "-c",
            "regression.yaml",
            "--dispatch",
            "slurm",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
    )
    job_log = work / "jobs.log"
    diag = proc.stdout + proc.stderr
    if job_log.exists():
        diag += "\n--- job log ---\n" + job_log.read_text()
    assert proc.returncode == 0, diag

    # (1) The idempotency invariant: exactly one hook execution, on the head.
    execs = counter.read_text().split() if counter.exists() else []
    assert len(execs) == 1, (
        f"sweep hook ran {len(execs)}x, expected 1 "
        f"(pre-plan-manifest this would be 1 head + 1 build + 3 sims = 5): "
        f"{execs}\n{diag}"
    )

    # (2) Multiple builds: one plan manifest (== one build job) per suite.
    plans = sorted(project.glob("verif/*/artefacts/.dispatch/plan-*.json"))
    assert len(plans) == 2, [str(p) for p in plans]

    # (3) Array > 1: the swept suite's array manifest holds all three variants.
    manifests = list(project.glob("verif/swept/artefacts/.dispatch/*/manifest.txt"))
    assert manifests, f"no array manifest written\n{diag}"
    elements = sum(len(m.read_text().splitlines()) for m in manifests)
    assert elements == 3, f"expected a 3-element array, got {elements}\n{diag}"

    # (4) And all three variants + the second suite's test really ran to PASS.
    envelope = None
    for line in proc.stdout.splitlines():
        if line.startswith('{"command"'):
            envelope = json.loads(line)
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    for name in ("wide_v0", "wide_v1", "wide_v2", "solo"):
        assert results.get(name) == "PASS", (name, results, diag)


def test_shim_delays_colocated_suites_without_plan_or_array_collisions(
    tmp_path_factory,
):
    """Real Slurm jobs consume both configs only after all submissions."""
    work = tmp_path_factory.mktemp("colocated_dispatch")
    deferred = work / "deferred"
    counter = work / "sweep-counter.txt"
    proc, envelope, project, diag = _run_regression(
        work,
        extra_env={
            "RB_SHIM_DEFER_DIR": str(deferred),
            "RB_SWEEP_COUNTER": str(counter),
        },
        prepare_project=_write_colocated_configs,
    )
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    expected_names = {name for names in _COLOCATED_EXPECTED.values() for name in names}
    assert {row["name"] for row in envelope["payload"]["results"]} == expected_names
    assert all(row["result"] == "PASS" for row in envelope["payload"]["results"])
    assert counter.read_text().splitlines() == ["1"] * 4, diag

    dispatch_root = project / "verif" / "blk" / "artefacts" / ".dispatch"
    plans = list(dispatch_root.glob("*/plan-*.json"))
    assert len(plans) == 2, [str(path) for path in plans]
    for plan_path in plans:
        plan = json.loads(plan_path.read_text())
        config_name = Path(plan["suite_config"]).name
        assert [test["name"] for test in plan["tests"]] == _COLOCATED_EXPECTED[
            config_name
        ]

        namespace = plan_path.parent
        build_results = list(namespace.glob("build-result-*.json"))
        assert len(build_results) == 1, [str(path) for path in build_results]
        built = json.loads(build_results[0].read_text())
        assert {entry["test"] for entry in built["builds"]} == set(
            _COLOCATED_EXPECTED[config_name]
        )
        assert list(namespace.glob("*/manifest.txt"))
        assert list(namespace.glob("*/array.sh"))

    assert not list(deferred.glob("*.sh")), "the delayed queue did not drain"


@pytest.fixture(scope="module")
def parallel_shim_run(tmp_path_factory):
    """One regression over the two-compile-key fixture, spans recorded.

    Its own fixture directory and its own run: the ``shim_run`` above is
    shared by assertions that would all change meaning if the project it
    ran on grew a second compile key and a concurrency knob.
    """
    work = tmp_path_factory.mktemp("dispatch_parallel")
    spans = work / "compile_spans.txt"
    argv = work / "sbatch_argv.txt"
    proc, envelope, project, diag = _run_regression(
        work,
        fixture=_PARALLEL_FIXTURE,
        extra_env={"RB_SHIM_SPANS": str(spans), "RB_SHIM_ARGV": str(argv)},
    )
    return proc, envelope, project, diag, spans, argv


def test_shim_parallel_build_job_compiles_two_distinct_builds_at_once(
    parallel_shim_run,
):
    """The concurrency claim, end to end through the real backend (#495).

    The shim ``sbatch`` runs each submission synchronously, so nothing
    *between* Slurm jobs can overlap here — but the build job's compile pool
    lives inside one ``--wrap`` invocation, so this is the one place the
    whole path (config knob → scaled reservation → ``--parallel`` argv →
    grouping by compile key → the pool) is observable at once.
    """
    proc, envelope, project, diag, spans, _argv = parallel_shim_run
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    assert results == {"alpha": "PASS", "beta": "PASS"}, diag

    rows = [line.split() for line in spans.read_text().splitlines() if line.strip()]
    # First span per build dir: the build job's. A sim job that had to
    # recompile would append a later one, which is not what is being timed.
    first = {}
    for tag, start, end in rows:
        first.setdefault(tag, (float(start), float(end)))
    assert len(first) == 2, f"expected two distinct builds, got {first}\n{diag}"

    (a_start, a_end), (b_start, b_end) = first.values()
    overlap = min(a_end, b_end) - max(a_start, b_start)
    assert overlap > 0, f"the two distinct builds did not overlap: {first}\n{diag}"


def test_shim_parallel_build_job_reservation_is_scaled(parallel_shim_run):
    """The build job asks for parallel x the per-build cpus, and says so.

    The fixture reserves 1 cpu per job and no compile block of its own, so
    a build job at ``parallel: 2`` is the one submission asking for 2 —
    everything else on this run asks for 1.
    """
    proc, _envelope, _project, diag, _spans, argv = parallel_shim_run
    assert proc.returncode == 0, diag

    lines = [line for line in argv.read_text().splitlines() if line.strip()]
    wrapped = [line for line in lines if "--wrap" in line]
    assert len(wrapped) == 1, f"expected exactly one build job\n{lines}\n{diag}"
    assert "--cpus-per-task=2" in wrapped[0], wrapped[0]
    # And the job it wrapped was told the budget it is paying for.
    assert "--parallel 2" in wrapped[0], wrapped[0]
    # The sim array is untouched: scaling is the build job's alone.
    arrays = [line for line in lines if "--array=" in line]
    assert arrays and all("--cpus-per-task=1" in line for line in arrays), arrays


def test_shim_build_job_envelope_gains_telemetry_and_compile_records(
    parallel_shim_run,
):
    """The build job's own accounting reaches its artifact (#495).

    The sacct shim already knows the wrap job's id, so this is the whole
    round trip: build job writes `builds`, head collects the build handle's
    sacct row, attaches it, and folds each compile back onto its sim.
    """
    proc, _envelope, project, diag, _spans, _argv = parallel_shim_run
    assert proc.returncode == 0, diag

    build_envelopes = list(
        project.glob("verif/*/artefacts/.dispatch/build-result-*.json")
    )
    assert len(build_envelopes) == 1, f"{build_envelopes}\n{diag}"
    raw = json.loads(build_envelopes[0].read_text())
    assert raw["telemetry"]["state"] == "COMPLETED"
    assert raw["telemetry"]["timelimit_s"] == 3600

    by_test = {entry["test"]: entry for entry in raw["builds"]}
    assert set(by_test) == {"alpha", "beta"}, raw["builds"]
    for entry in by_test.values():
        assert entry["builder"], entry
        assert entry["duration_sec"] is not None, entry
    # Two distinct compile keys in this fixture -> two distinct group dirs.
    assert len({entry["group"] for entry in by_test.values()}) == 2, raw["builds"]

    # And each sim envelope carries the compile the build job ran for it.
    sim_envelopes = list(project.glob("verif/*/artefacts/*/dispatch/result-*.json"))
    assert sim_envelopes, diag
    for path in sim_envelopes:
        compile_block = json.loads(path.read_text())["result"]["results"]["compile"]
        assert compile_block["builder"], (path, compile_block)
        assert compile_block["reused"] in (True, False), compile_block


# ------------------------------ per-key release, end to end (#548)


@pytest.fixture(scope="module")
def release_shim_run(tmp_path_factory):
    """One regression over the two-compile-key fixture, with `scontrol`.

    The shim ``sbatch`` runs each submission synchronously, so without
    ``RB_SHIM_DEFER_DIR`` the build job would run *before* the head has
    submitted a single sim job and there would be nothing to release yet.
    Deferring is what makes the real ordering observable here: head submits
    everything and writes the gates manifest, then the jobs run.
    """
    work = tmp_path_factory.mktemp("dispatch_release")
    scontrol_log = work / "scontrol.txt"
    argv = work / "sbatch_argv.txt"
    proc, envelope, project, diag = _run_regression(
        work,
        fixture=_PARALLEL_FIXTURE,
        extra_path_dirs=(_SCONTROL_SHIM,),
        extra_env={
            "RB_SHIM_DEFER_DIR": str(work / "deferred"),
            "RB_SHIM_SCONTROL_LOG": str(scontrol_log),
            "RB_SHIM_ARGV": str(argv),
        },
    )
    return proc, envelope, project, diag, scontrol_log, argv


def test_shim_build_job_releases_each_compile_key_it_finishes(release_shim_run):
    """Both keys built, so both keys' array elements were released (#548).

    The ids are the ones ``sbatch`` actually handed out, so this covers the
    whole loop: head → gates manifest → build job → ``scontrol update``.
    """
    proc, envelope, project, diag, scontrol_log, argv = release_shim_run
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    assert results == {"alpha": "PASS", "beta": "PASS"}, diag

    # The array element ids the head submitted, read off the recorded argv.
    array_line = [line for line in argv.read_text().splitlines() if "--array=" in line]
    assert len(array_line) == 1, array_line
    # Both elements were submitted gated on the build job, and stay that way:
    # the release clears the dependency, it does not replace the gate.
    assert "--dependency=afterok:" in array_line[0], array_line[0]
    assert "--kill-on-invalid-dep=yes" in array_line[0], array_line[0]

    manifests = list(project.glob("verif/*/artefacts/.dispatch/gates-*.json"))
    assert len(manifests) == 1, [str(path) for path in manifests]
    manifest = json.loads(manifests[0].read_text())
    submitted = {entry["test"]: entry["job_id"] for entry in manifest["entries"]}
    assert set(submitted) == {"alpha", "beta"}, manifest

    released = [
        line.split()[-2].removeprefix("JobId=")
        for line in scontrol_log.read_text().splitlines()
        if " update " in f" {line} "
    ]
    assert sorted(released) == sorted(submitted.values()), (
        released,
        submitted,
        diag,
    )
    assert all(
        line.endswith("Dependency=") for line in scontrol_log.read_text().splitlines()
    ), scontrol_log.read_text()

    # ...and the build job said so on its own log, per key.
    build_logs = list(project.glob("verif/*/artefacts/.dispatch/build-rtl_buddy-*.log"))
    assert build_logs, diag
    events = [
        json.loads(line)
        for log in build_logs
        for line in log.read_text().splitlines()
        if line.strip().startswith("{")
    ]
    keys = [e for e in events if e.get("event") == "dispatch.key_released"]
    assert len(keys) == 2, keys
    assert {name for e in keys for name in e["tests"]} == {"alpha", "beta"}
    assert sorted(job for e in keys for job in e["job_ids"]) == sorted(released)


def test_shim_regression_without_scontrol_says_so_and_still_passes(shim_run):
    """The default shim PATH has no `scontrol`: every sim job keeps its
    `afterok` and the build job logs one line about why (#548)."""
    proc, _envelope, project, diag = shim_run
    assert proc.returncode == 0, diag

    build_logs = list(project.glob("verif/*/artefacts/.dispatch/build-rtl_buddy-*.log"))
    assert build_logs, diag
    events = [
        json.loads(line).get("event")
        for log in build_logs
        for line in log.read_text().splitlines()
        if line.strip().startswith("{")
    ]
    assert events.count("dispatch.release_unavailable") == 1, events
    assert "dispatch.key_released" not in events


# ------------------------------ adopting an interrupted run (#521)


def test_shim_adopt_collects_an_interrupted_fleet_without_resubmitting(
    tmp_path_factory,
):
    """`--orphans adopt` waits on the old fleet instead of launching one.

    The first run is a complete regression, so its envelopes and its build
    result really exist on disk; its manifest is then rewound to `running`,
    which is exactly the state a head killed between the fan-out and
    collection leaves behind — a synchronous shim head cannot be killed
    there without losing the envelopes the adoption is supposed to find.
    `RB_SHIM_LIVE` makes the orphan probe see that fleet still queued, once,
    so the second run discovers it and the wait that follows sees it drain.

    The assertion that matters is the job db: `sbatch` appends a line per
    submission, so an adoption that submitted anything at all would grow it.
    """
    work = tmp_path_factory.mktemp("dispatch_adopt")
    proc, _envelope, project, diag = _run_regression(work)
    assert proc.returncode == 0, diag

    manifests = list(project.glob("verif/blk/artefacts/.dispatch/run-*.json"))
    assert len(manifests) == 1, [str(path) for path in manifests]
    manifest = json.loads(manifests[0].read_text())
    assert manifest["status"] == "collected"
    assert manifest["backend"] == "slurm"
    job_ids = [entry["job_id"] for entry in manifest["pending"]]
    assert job_ids, manifest
    if manifest["build"] is not None:
        job_ids.append(manifest["build"]["job_id"])

    manifest["status"] = "running"
    manifests[0].write_text(json.dumps(manifest))
    submissions_before = (work / "jobs.db").read_text().splitlines()

    proc, envelope, _project, diag = _rerun_regression(
        project,
        work,
        extra_args=("--orphans", "adopt"),
        extra_env={"RB_SHIM_LIVE": ",".join(job_ids)},
    )
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    assert (work / "jobs.db").read_text().splitlines() == submissions_before, diag

    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    assert results == {"alpha": "PASS", "beta": "PASS"}, diag
    # ...and the adopted run is the one that retires the manifest.
    assert json.loads(manifests[0].read_text())["status"] == "collected"

    events = [
        json.loads(line)
        for line in (project / "verif" / "blk" / "rtl_buddy.log")
        .read_text()
        .splitlines()
        if line.strip().startswith("{")
    ]
    adopted = [e for e in events if e.get("event") == "dispatch.orphans_adopted"]
    assert len(adopted) == 1, [e.get("event") for e in events]
    assert adopted[0]["run_token"] == manifest["run_token"]
