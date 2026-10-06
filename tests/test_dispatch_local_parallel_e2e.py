"""End-to-end ``--dispatch local-parallel`` regression.

The pool launches real ``rb _build-job`` and ``rb _test-job`` subprocesses with
no scheduler shims. The Slurm shim directory stays on PATH because the fake
``verilator`` lives there, so an existing ``$RB_SHIM_DB`` means a scheduler CLI
was called.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SHIMS = _REPO / "tests" / "dispatch_shims"
_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_project"
_SWEEP_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_sweep_project"
_PARALLEL_FIXTURE = _REPO / "tests" / "fixtures" / "dispatch_parallel_project"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="the fake verilator these fixtures build with needs a POSIX shell",
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


def _run(
    work_dir: Path,
    fixture: Path,
    extra_args=(),
    extra_env=None,
    command=("regression", "-c", "regression.yaml"),
    cwd_rel=".",
    prepare_project=None,
):
    project = work_dir / "proj"
    shutil.copytree(fixture, project)
    if prepare_project is not None:
        prepare_project(project)
    env = dict(os.environ)
    # The fake verilator lives beside the Slurm shims; RB_SHIM_DB is where they record
    # calls.
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_DB"] = str(work_dir / "jobs.db")
    env["RB_SHIM_LOG"] = str(work_dir / "jobs.log")
    env.update(extra_env or {})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "--machine",
            *command,
            "--dispatch",
            "local-parallel",
            *extra_args,
        ],
        cwd=project / cwd_rel,
        capture_output=True,
        text=True,
        env=env,
    )
    envelope = None
    for line in proc.stdout.splitlines():
        if line.startswith('{"command"'):
            envelope = json.loads(line)
    diag = proc.stdout + proc.stderr
    build_logs = sorted(project.glob("verif/*/artefacts/.dispatch/build-*.log"))
    build_logs += sorted(project.glob("verif/*/artefacts/.dispatch/*/build-*.log"))
    for log in sorted(project.glob("verif/*/artefacts/*/dispatch/*.log")) + build_logs:
        diag += f"\n--- {log.name} ---\n" + log.read_text()
    return proc, envelope, project, diag


@pytest.fixture(scope="module")
def pool_run(tmp_path_factory):
    work = tmp_path_factory.mktemp("local_parallel")
    return (*_run(work, _FIXTURE, extra_args=("-j", "2")), work)


def test_pool_regression_runs_the_real_pipeline_to_pass(pool_run):
    proc, envelope, project, diag, _work = pool_run
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    assert results == {"alpha": "PASS", "beta": "PASS"}

    envelopes = list(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    assert len(envelopes) == 2, diag
    assert json.loads(envelopes[0].read_text())["result"]["results"]["result"] == "PASS"

    # Each job's stdout went to its own log; the head's stdout stayed parseable.
    logs = sorted(
        p.name
        for p in project.glob("verif/blk/artefacts/*/dispatch/local-parallel-*.log")
    )
    assert logs == ["local-parallel-single.log", "local-parallel-single.log"], diag
    assert list(project.glob("verif/blk/artefacts/.dispatch/build-*.log")) != [], diag


def test_pool_regression_records_which_binary_each_run_simulated(pool_run):
    """Every run names the build it validated.

    A run's envelope names the compile key and the executable its stamp vouched
    for, and the build job's envelope names the same key.
    """
    proc, _envelope, project, diag, _work = pool_run
    assert proc.returncode == 0, diag

    stamps = {}
    for path in sorted(project.glob("verif/blk/artefacts/*/dispatch/result-*.json")):
        raw = json.loads(path.read_text())
        stamps[raw["test"]] = raw["result"]["results"]["build_stamp"]
    assert sorted(stamps) == ["alpha", "beta"], diag
    for stamp in stamps.values():
        assert "/.shared-builds/obj_dir_" in stamp["build_dir"]
        assert len(stamp["fingerprint_sha"]) == 64
        assert len(stamp["simv"]) == 3

    build_results = sorted(
        project.glob("verif/blk/artefacts/.dispatch/build-result-*.json")
    )
    assert build_results, diag
    recorded = {
        entry["test"]: entry.get("fingerprint_sha")
        for entry in json.loads(build_results[0].read_text())["builds"]
    }
    assert recorded == {
        name: stamp["fingerprint_sha"] for name, stamp in stamps.items()
    }


def test_pool_regression_never_calls_the_scheduler(pool_run):
    proc, _envelope, _project, diag, work = pool_run
    assert proc.returncode == 0, diag
    # The shims record every call; none was made.
    assert not (work / "jobs.db").exists(), "a Slurm CLI was invoked"
    assert not (work / "jobs.log").exists(), "a Slurm CLI was invoked"


def test_pool_regression_reports_no_reservation_advice(pool_run):
    proc, envelope, _project, diag, _work = pool_run
    assert proc.returncode == 0, diag
    # No accounting source, so right-sizing gives no advice.
    assert envelope["payload"]["reservation_advice"] == [], diag


def test_pool_expands_the_sweep_once_across_build_and_sim_jobs(tmp_path_factory):
    """The plan-manifest invariant holds on this backend too.

    The sweep hook runs once, on the head, not again in the build job or sim jobs.
    """
    work = tmp_path_factory.mktemp("local_parallel_sweep")
    counter = work / "sweep_execs.txt"
    proc, envelope, project, diag = _run(
        work,
        _SWEEP_FIXTURE,
        extra_args=("-j", "3"),
        extra_env={"RB_SWEEP_COUNTER": str(counter)},
    )
    assert proc.returncode == 0, diag

    execs = counter.read_text().split() if counter.exists() else []
    assert len(execs) == 1, f"sweep hook ran {len(execs)}x, expected 1: {execs}\n{diag}"

    plans = sorted(project.glob("verif/*/artefacts/.dispatch/plan-*.json"))
    assert len(plans) == 2, [str(p) for p in plans]

    assert envelope is not None, diag
    results = {r["name"]: r["result"] for r in envelope["payload"]["results"]}
    for name in ("wide_v0", "wide_v1", "wide_v2", "solo"):
        assert results.get(name) == "PASS", (name, results, diag)


def test_pool_keeps_colocated_suite_plans_until_queued_jobs_consume_them(
    tmp_path_factory,
):
    work = tmp_path_factory.mktemp("local_parallel_colocated")
    counter = work / "sweep-counter.txt"
    proc, envelope, project, diag = _run(
        work,
        _FIXTURE,
        extra_args=("-j", "1"),
        extra_env={"RB_SWEEP_COUNTER": str(counter)},
        prepare_project=_write_colocated_configs,
    )
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    expected_names = {name for names in _COLOCATED_EXPECTED.values() for name in names}
    assert {row["name"] for row in envelope["payload"]["results"]} == expected_names
    assert all(row["result"] == "PASS" for row in envelope["payload"]["results"])
    # One head-side sweep per base test; a worker falling back to the hook would append
    # more lines.
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
        build_results = list(plan_path.parent.glob("build-result-*.json"))
        assert len(build_results) == 1, [str(path) for path in build_results]
        built = json.loads(build_results[0].read_text())
        assert {entry["test"] for entry in built["builds"]} == set(
            _COLOCATED_EXPECTED[config_name]
        )


def test_pool_runs_a_single_test_from_its_suite_dir(tmp_path_factory):
    """`rb test <name> --dispatch` end to end with real subprocesses.

    Only the named test is planned, built and run, not the whole suite.
    """
    work = tmp_path_factory.mktemp("local_parallel_single_test")
    proc, envelope, project, diag = _run(
        work,
        _FIXTURE,
        # `-M reg`: the fixture declares only a `reg` opts block, and `rb test` defaults
        # to `debug`.
        command=("-M", "reg", "test", "alpha"),
        cwd_rel="verif/blk",
    )
    assert proc.returncode == 0, diag
    assert envelope is not None, diag
    assert [(r["name"], r["result"]) for r in envelope["payload"]["results"]] == [
        ("alpha", "PASS")
    ], diag

    # One sim job's envelope; "beta" was never planned, built or run.
    envelopes = sorted(project.glob("verif/blk/artefacts/*/dispatch/result-*.json"))
    assert [p.parent.parent.name for p in envelopes] == ["alpha"], diag
    assert list(project.glob("verif/blk/artefacts/.dispatch/build-*.log")) != [], diag
    assert not (work / "jobs.db").exists(), "a Slurm CLI was invoked"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - reparented and not ours
        return True
    return True


def _recorded_compiler_pids(pids_file: Path) -> list[int]:
    if not pids_file.exists():
        return []
    return [int(tok) for tok in pids_file.read_text().split() if tok.strip().isdigit()]


def _start_hanging_build_job(project: Path, pids_file: Path, parallel: str):
    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    # Long enough that "it died" cannot be confused with "it finished".
    env["RB_SHIM_HANG"] = "60"
    env["RB_SHIM_PIDS"] = str(pids_file)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rtl_buddy",
            "_build-job",
            "-c",
            "tests.yaml",
            "--parallel",
            parallel,
        ],
        cwd=project / "verif" / "blk",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_cancelling_a_parallel_build_job_kills_its_compilers(tmp_path_factory):
    """SIGTERM to the build job takes the compilers with it.

    This is what ``cancel_all`` does on Ctrl-C or ``--max-wait``: it signals the
    ``rb _build-job`` process group only. The compilers run in their own
    sessions, so the job must terminate them itself. The fake verilator parks in a
    60 s sleep over the two-compile-key fixture.
    """
    work = tmp_path_factory.mktemp("build_job_cancel")
    project = work / "proj"
    shutil.copytree(_PARALLEL_FIXTURE, project)
    pids_file = work / "compiler_pids.txt"
    proc = _start_hanging_build_job(project, pids_file, "2")
    compilers: list[int] = []
    try:
        # Both compilers are in flight; cancelling before the pool filled would prove
        # nothing.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            compilers = _recorded_compiler_pids(pids_file)
            if len(compilers) >= 2:
                break
            time.sleep(0.1)
        assert len(compilers) >= 2, f"compilers never started: {compilers}"

        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=60)[0]
        assert proc.returncode == 128 + signal.SIGTERM, out

        # The grandchildren are gone; polled because init reaps them after the job
        # exits.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in compilers):
            time.sleep(0.1)
        assert [pid for pid in compilers if _pid_alive(pid)] == [], out
        # None reached the far side of its sleep.
        assert "survived" not in pids_file.read_text(), out
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failed run
            proc.kill()
            proc.communicate()
        for pid in compilers:  # pragma: no cover - only on a failed run
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def _append_third_compile_key(project: Path) -> None:
    """A third test with its own plusdefine, i.e. a third distinct build."""
    tests_yaml = project / "verif" / "blk" / "tests.yaml"
    tests_yaml.write_text(
        tests_yaml.read_text()
        + """  - name: gamma
    desc: dispatch ci test gamma, a third compile key queued behind the pool
    model: m
    model_path: models.yaml
    reglvl: 0
    plusargs:
    plusdefines:
      WIDTH: 16
    uvm:
    preproc:
    postproc:
    sweep:
    testbench: tb_blk
    sim_timeout:
"""
    )


def test_cancelling_a_parallel_build_job_starts_no_queued_compiler(tmp_path_factory):
    """No compiler starts after cancellation began.

    Three compile keys, two pool slots: group 3 is still queued when SIGTERM
    arrives. The fake verilator's pid file records every launch, and a third pid is
    a compiler started after cancellation.

    This is the end-to-end guard, not the regression proof: whether the third
    compiler starts is a race. The deterministic version is
    ``test_a_cancelled_build_job_never_starts_a_queued_group`` in
    ``test_test_job.py``.
    """
    work = tmp_path_factory.mktemp("build_job_cancel_queued")
    project = work / "proj"
    shutil.copytree(_PARALLEL_FIXTURE, project)
    _append_third_compile_key(project)
    pids_file = work / "compiler_pids.txt"
    proc = _start_hanging_build_job(project, pids_file, "2")
    compilers: list[int] = []
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            compilers = _recorded_compiler_pids(pids_file)
            if len(compilers) >= 2:
                break
            time.sleep(0.1)
        assert len(compilers) >= 2, f"compilers never started: {compilers}"

        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=60)[0]
        assert proc.returncode == 128 + signal.SIGTERM, out

        # Two pool slots, so a third pid is a compiler launched after cancellation
        # began.
        after = _recorded_compiler_pids(pids_file)
        assert len(after) == 2, f"a queued worker launched a compiler: {after}\n{out}"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in after):
            time.sleep(0.1)
        assert [pid for pid in after if _pid_alive(pid)] == [], out
        assert "survived" not in pids_file.read_text(), out
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failed run
            proc.kill()
            proc.communicate()
        for pid in _recorded_compiler_pids(pids_file):  # pragma: no cover
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def _wait_for(predicate, *, timeout=60, message=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message or "condition never held")


def test_a_second_build_job_waits_for_the_first_and_reuses_its_build(tmp_path_factory):
    """Two ``rb _build-job`` processes over one compile key compile once.

    The second job queues on the shared-build lock while the first holds it.
    Real subprocesses, because the claim is cross-process. The first job's fake
    verilator parks inside the lock until the test releases it.
    """
    work = tmp_path_factory.mktemp("build_job_lock")
    project = work / "proj"
    shutil.copytree(_FIXTURE, project)
    suite = project / "verif" / "blk"
    pids_file = work / "compiler_pids.txt"
    spans = work / "compiler_spans.txt"
    release = work / "release"
    first_log = work / "first.log"
    second_log = work / "second.log"

    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    # One line per verilator invocation from both processes; the count is the assertion.
    env["RB_SHIM_SPANS"] = str(spans)
    argv = [sys.executable, "-m", "rtl_buddy", "_build-job", "-c", "tests.yaml"]

    first = second = None
    try:
        with open(first_log, "w") as out:
            first = subprocess.Popen(
                argv,
                cwd=suite,
                env={
                    **env,
                    "RB_SHIM_WAIT_FILE": str(release),
                    "RB_SHIM_PIDS": str(pids_file),
                },
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
            )
        # The compiler is running, so the first job is inside the lock.
        _wait_for(
            lambda: _recorded_compiler_pids(pids_file),
            message=f"the first build job never compiled\n{first_log.read_text()}",
        )
        with open(second_log, "w") as out:
            second = subprocess.Popen(
                argv,
                cwd=suite,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
            )
        # The second job is queued on the lock; the line is emitted just before the
        # blocking flock.
        _wait_for(
            lambda: "waiting for another rtl-buddy" in second_log.read_text(),
            message=(
                "the second build job never queued on the build lock\n"
                f"{second_log.read_text()}"
            ),
        )
        release.touch()
        assert first.wait(timeout=180) == 0, first_log.read_text()
        assert second.wait(timeout=180) == 0, second_log.read_text()
    finally:
        release.touch()  # pragma: no cover - only matters on a failed run
        for proc in (first, second):
            if proc is not None and proc.poll() is None:  # pragma: no cover
                proc.kill()
                proc.wait()

    diag = f"--- first ---\n{first_log.read_text()}--- second ---\n{second_log.read_text()}"
    # One compile between the two processes, into the one shared directory.
    compiles = [
        line.split()[0] for line in spans.read_text().splitlines() if line.strip()
    ]
    assert len(compiles) == 1, f"the waiter recompiled instead of reusing\n{diag}"
    assert compiles[0].startswith("obj_dir_"), diag
    # The waiter validated the first job's stamp and reused it.
    assert "reused shared build" in second_log.read_text(), diag
    # The lock lives in the directory it guards.
    shared = sorted((suite / "artefacts" / ".shared-builds").glob("obj_dir_*"))
    assert [d.name for d in shared] == compiles, diag
    assert (shared[0] / ".rb-build.lock").exists(), diag


_SUITE_PREPROC = """\
from pathlib import Path

prog = Path(suite_dir) / f"prog_{test_cfg.get_name()}"
prog.mkdir(parents=True, exist_ok=True)
(prog / "data.txt").write_text(open(Path(suite_dir) / "nonce.txt").read())
"""


def _write_same_key_preproc_suite(project: Path, *, precreate: bool = True) -> None:
    """Two tests on one compile key whose preproc writes under the suite dir.

    The model filelist puts the suite directory on ``+incdir+``, and the build job
    runs every PRE before any compile, so one member's output moves between the
    next member's fingerprint and the stamp it should reuse.

    ``precreate`` selects which half of the listing moves. With the directories
    already present only their content changes. On a cold tree the names change
    too, which only the group adopt handles.
    """
    suite = project / "verif" / "blk"
    (suite / "models.yaml").write_text(
        "rtl-buddy-filetype: model_config\nmodels:\n"
        "  - name: m\n    filelist:\n      - src.sv\n      - +incdir+.\n"
    )
    (suite / "nonce.txt").write_text("second run\n")
    (suite / "preproc.py").write_text(_SUITE_PREPROC)
    if precreate:
        for name in ("alpha", "beta"):
            prog = suite / f"prog_{name}"
            prog.mkdir()
            (prog / "data.txt").write_text("first run\n")
    tests = (
        (suite / "tests.yaml")
        .read_text()
        .replace("    preproc:\n", "    preproc:\n      path: preproc.py\n")
    )
    (suite / "tests.yaml").write_text(tests)


def _run_same_key_build_job(work: Path, *, precreate: bool):
    """Run one ``_build-job`` over the two-tests-one-key suite.

    Returns ``(suite dir, compiled obj_dir basenames, job output)``.
    """
    project = work / "proj"
    shutil.copytree(_FIXTURE, project)
    _write_same_key_preproc_suite(project, precreate=precreate)
    suite = project / "verif" / "blk"
    spans = work / "compiler_spans.txt"
    log = work / "build.log"

    env = dict(os.environ)
    env["PATH"] = f"{_SHIMS}{os.pathsep}{env['PATH']}"
    env["RB_SHIM_SPANS"] = str(spans)
    # The narrowing applies to builds that report their dependencies, as real Verilator
    # does.
    env["RB_SHIM_DEPS"] = "1"
    with open(log, "w") as out:
        proc = subprocess.run(
            [sys.executable, "-m", "rtl_buddy", "_build-job", "-c", "tests.yaml"],
            cwd=suite,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
        )
    diag = log.read_text()
    assert proc.returncode == 0, diag
    compiles = [
        line.split()[0] for line in spans.read_text().splitlines() if line.strip()
    ]
    return suite, compiles, diag


def test_a_build_job_compiles_one_key_once_when_a_preproc_writes_beside_it(
    tmp_path_factory,
):
    """One compile key, two tests, one Verilation.

    The members' fingerprints agree with the stamp despite the preproc output.
    """
    work = tmp_path_factory.mktemp("build_job_same_key")
    suite, compiles, diag = _run_same_key_build_job(work, precreate=True)

    assert len(compiles) == 1, f"a same-key sibling recompiled\n{diag}"
    assert "reused shared build" in diag, diag
    # Both tests were on one key, and the preproc wrote into the directory the stamp
    # lists.
    shared = sorted((suite / "artefacts" / ".shared-builds").glob("obj_dir_*"))
    assert [d.name for d in shared] == compiles, diag
    assert (suite / "prog_beta" / "data.txt").read_text() == "second run\n"


def test_a_build_job_compiles_one_key_once_on_a_cold_tree(tmp_path_factory):
    """The same key compiles once with nothing generated yet.

    Member B's PRE creates ``prog_beta``, a name A's stamp never listed. Only the
    group adopt, where B takes the build its leader just made, compiles once.
    """
    work = tmp_path_factory.mktemp("build_job_same_key_cold")
    suite, compiles, diag = _run_same_key_build_job(work, precreate=False)

    assert len(compiles) == 1, f"a same-key sibling recompiled\n{diag}"
    shared = sorted((suite / "artefacts" / ".shared-builds").glob("obj_dir_*"))
    assert [d.name for d in shared] == compiles, diag
    # Both PREs ran, and B's created a directory not in A's stamp listing.
    for name in ("alpha", "beta"):
        assert (suite / f"prog_{name}" / "data.txt").read_text() == "second run\n"


def test_a_failed_key_compiles_once_and_every_test_names_the_error(tmp_path_factory):
    """Two tests on one compile key whose compile fails: the build job compiles once, the sibling adopts the failure, and both rows report the compile error rather than a missing result."""
    work = tmp_path_factory.mktemp("failed_key")
    compiles = work / "compiles.txt"
    proc, envelope, _project, diag = _run(
        work, _FIXTURE, extra_env={"RB_SHIM_FAIL": str(compiles)}
    )

    assert proc.returncode != 0, diag
    assert envelope is not None, diag
    assert len(compiles.read_text().splitlines()) == 1, diag
    rows = {r["name"]: r for r in envelope["payload"]["results"]}
    assert set(rows) == {"alpha", "beta"}, diag
    for row in rows.values():
        assert row["result"] == "FAIL", diag
        assert "Exiting due to 15 warning(s)" in row["desc"], diag
