"""LocalProcessBackend pool tests (#360).

These drive the *real* backend against *real* subprocesses — the pool has
no scheduler to mock, and its whole job is process lifecycle, so a mocked
``Popen`` would test nothing. What is stubbed is only the argv the pool
launches: instead of ``rb _test-job`` (which would need a project, a
builder and a simulator) each job is a short ``python -c`` that records
what it saw. Build gating, the concurrency cap, dependency-failure
skipping and cancellation are then observable in plain files.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import pytest

import rtl_buddy.dispatch.local_parallel as lp_module
from rtl_buddy.config.dispatch import (
    DispatchConfigFile,
    DispatchResourcesFile,
    JobResources,
    resolve_resources,
)
from rtl_buddy.dispatch import create_dispatch_backend
from rtl_buddy.dispatch.base import BuildJobSpec, TestJobSpec
from rtl_buddy.dispatch.local_parallel import LocalProcessBackend, default_jobs
from rtl_buddy.errors import FatalRtlBuddyError

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="pool teardown relies on POSIX process groups"
)


def _backend(jobs=None, **cfg_kwargs) -> LocalProcessBackend:
    return LocalProcessBackend(DispatchConfigFile(jobs=jobs, **cfg_kwargs).initialise())


def _sim_spec(tmp_path: Path, name: str) -> TestJobSpec:
    return TestJobSpec(
        test_name=name,
        suite_dir=str(tmp_path),
        test_config_path=str(tmp_path / "tests.yaml"),
        result_json=tmp_path / f"{name}-result.json",
        log_path=tmp_path / f"{name}.log",
    )


def _build_spec(tmp_path: Path) -> BuildJobSpec:
    return BuildJobSpec(
        suite_dir=str(tmp_path),
        test_config_path=str(tmp_path / "tests.yaml"),
        log_path=tmp_path / "build.log",
    )


def _python(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def _stub_argv(monkeypatch, *, sim, build=_python("pass")):
    """Replace the rb re-entry argvs with test programs.

    ``sim``/``build`` may be a list (one program for every job) or a
    callable taking the spec, for per-job programs.
    """

    def _resolve(program, spec):
        return list(program(spec)) if callable(program) else list(program)

    monkeypatch.setattr(lp_module, "test_job_argv", lambda spec: _resolve(sim, spec))
    monkeypatch.setattr(lp_module, "build_job_argv", lambda spec: _resolve(build, spec))


def _states(backend: LocalProcessBackend) -> dict[str, int]:
    counts = {"queued": 0, "running": 0, "finished": 0}
    for job in backend._jobs.values():
        if job.running:
            counts["running"] += 1
        elif job.finished:
            counts["finished"] += 1
        else:
            counts["queued"] += 1
    return counts


# ---- registry and pool sizing -------------------------------------------


def test_registry_exposes_local_parallel():
    backend = create_dispatch_backend(
        "local-parallel", DispatchConfigFile().initialise()
    )
    assert isinstance(backend, LocalProcessBackend)
    # `local` still means "no backend": the sequential in-process path.
    assert create_dispatch_backend("local", DispatchConfigFile().initialise()) is None


def test_unknown_backend_names_local_parallel_in_its_error():
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        create_dispatch_backend("lsf", DispatchConfigFile().initialise())
    assert "local-parallel" in str(excinfo.value)


def test_default_pool_size_is_capped_and_at_least_one():
    assert default_jobs() == max(1, min(4, os.cpu_count() or 1))
    assert _backend().max_jobs == default_jobs()


def test_config_sets_pool_size():
    assert _backend(jobs=7).max_jobs == 7


def test_zero_jobs_in_config_is_rejected():
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        DispatchConfigFile(jobs=0).initialise()
    assert "jobs must be >= 1" in str(excinfo.value)


def _events(caplog) -> list[str]:
    return [r.__dict__.get("rtl_event") for r in caplog.records]


def test_reservations_are_warned_about_at_warning_level(monkeypatch, tmp_path, caplog):
    """A reservation nothing enforces must reach the terminal, not just the log.

    The console handler shows INFO only under ``-v``, so an INFO notice would
    be invisible on the very run it exists to explain.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1, resources=DispatchResourcesFile(cpus=8, mem="16G"))
    spec = _sim_spec(tmp_path, "t0")
    spec.resources = resolve_resources(
        DispatchConfigFile(
            resources=DispatchResourcesFile(cpus=8, mem="16G")
        ).initialise()
    )
    with caplog.at_level(logging.DEBUG):
        backend.submit(spec)
    warnings = [
        r
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.reservations_ignored"
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "not enforced" in warnings[0].message.lower()
    backend.cancel_all([])


def test_per_test_reservations_are_warned_about_too(monkeypatch, tmp_path, caplog):
    """The notice keys off the RESOLVED reservation, not ``cfg-dispatch``.

    A project that reserves only per test or per testbench in tests.yaml —
    where the heavy tests are — would otherwise never be told.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1)  # nothing configured under cfg-dispatch
    spec = _sim_spec(tmp_path, "t0")
    spec.resources = JobResources(cpus=16, mem="64G", time="08:00:00")
    with caplog.at_level(logging.WARNING):
        handle = backend.submit(spec)
        backend.wait_all([handle])
    assert "dispatch.reservations_ignored" in _events(caplog)


def test_reservation_notice_fires_once_per_run(monkeypatch, tmp_path, caplog):
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=2)
    specs = []
    for i in range(4):
        spec = _sim_spec(tmp_path, f"t{i}")
        spec.resources = JobResources(cpus=4)
        specs.append(spec)
    with caplog.at_level(logging.WARNING):
        handles = [backend.submit(spec) for spec in specs]
        backend.wait_all(handles)
    assert _events(caplog).count("dispatch.reservations_ignored") == 1


def test_no_reservation_no_ignored_notice(monkeypatch, tmp_path, caplog):
    _stub_argv(monkeypatch, sim=_python("pass"))
    with caplog.at_level(logging.DEBUG):
        backend = _backend()
        handle = backend.submit(_sim_spec(tmp_path, "t0"))
        backend.wait_all([handle])
    events = _events(caplog)
    assert "dispatch.reservations_ignored" not in events
    # The pool size is always recorded — it explains the run's wall-clock.
    assert "dispatch.pool_configured" in events


# ---- the concurrency cap -------------------------------------------------


def test_cap_bounds_running_jobs_and_queues_the_rest(monkeypatch, tmp_path):
    """Five submits against a two-slot pool: two run, three wait."""
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=2)

    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(5)]
    assert _states(backend) == {"queued": 3, "running": 2, "finished": 0}

    backend.cancel_all(handles)
    assert _states(backend)["running"] == 0


def test_pool_refills_slots_until_every_job_ran(monkeypatch, tmp_path):
    """A capped pool still runs every job — the cap paces, it never drops."""
    _stub_argv(
        monkeypatch,
        sim=lambda spec: _python(
            f"from pathlib import Path;"
            f"Path({str(tmp_path / (spec.test_name + '.done'))!r}).write_text('x')"
        ),
    )
    backend = _backend(jobs=2)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(6)]
    backend.wait_all(handles)

    assert _states(backend) == {"queued": 0, "running": 0, "finished": 6}
    assert sorted(p.name for p in tmp_path.glob("*.done")) == [
        f"t{i}.done" for i in range(6)
    ]
    assert all(backend._jobs[h.job_id].returncode == 0 for h in handles)


def test_jobs_really_run_concurrently_within_the_cap(monkeypatch, tmp_path):
    """Overlap is real (>= 2 at once) and never exceeds the cap.

    Each job records its own start/end wall-clock, so the maximum overlap
    across the four intervals is computable after the fact. The upper bound
    is the property under test; the lower bound proves the pool is actually
    parallel rather than an elaborate sequential loop.
    """
    _stub_argv(
        monkeypatch,
        sim=lambda spec: _python(
            "import time;from pathlib import Path;"
            "start=time.time();time.sleep(0.3);"
            f"Path({str(tmp_path)!r}, ).joinpath({spec.test_name + '.span'!r})"
            ".write_text(f'{start} {time.time()}')"
        ),
    )
    backend = _backend(jobs=2)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(4)]
    backend.wait_all(handles)

    spans = []
    for path in tmp_path.glob("*.span"):
        start, end = path.read_text().split()
        spans.append((float(start), float(end)))
    assert len(spans) == 4

    edges = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
    peak = live = 0
    for _, delta in edges:
        live += delta
        peak = max(peak, live)
    assert peak <= 2, spans
    assert peak >= 2, f"pool never overlapped two jobs: {spans}"


# ---- build gating -------------------------------------------------------


def test_sims_wait_for_the_build_to_succeed(monkeypatch, tmp_path):
    stamp = tmp_path / "build.stamp"
    _stub_argv(
        monkeypatch,
        build=_python(
            "import time;from pathlib import Path;time.sleep(0.2);"
            f"Path({str(stamp)!r}).write_text('built')"
        ),
        # Each sim records whether the shared build had landed by the time it
        # started — the local equivalent of the afterok guarantee.
        sim=lambda spec: _python(
            "from pathlib import Path;"
            f"Path({str(tmp_path)!r}).joinpath({spec.test_name + '.saw'!r})"
            f".write_text(str(Path({str(stamp)!r}).exists()))"
        ),
    )
    backend = _backend(jobs=4)

    build = backend.submit_build(_build_spec(tmp_path))
    sims = [
        backend.submit(_sim_spec(tmp_path, f"t{i}"), dependency=build.job_id)
        for i in range(3)
    ]
    # Slots are free, but the gate is shut: nothing but the build may run.
    assert _states(backend)["running"] == 1

    backend.wait_all([build, *sims])
    saw = {p.name: p.read_text() for p in tmp_path.glob("*.saw")}
    assert saw == {f"t{i}.saw": "True" for i in range(3)}


def test_failed_build_skips_its_sims(monkeypatch, tmp_path, caplog):
    """A build that exits nonzero cancels its dependents, as afterok does.

    The sims never launch, so they leave no result envelope and the head's
    collector reports them as producing no result — which is what happened.
    """
    _stub_argv(
        monkeypatch,
        build=_python("raise SystemExit(3)"),
        sim=lambda spec: _python(
            "from pathlib import Path;"
            f"Path({str(tmp_path)!r}).joinpath({spec.test_name + '.ran'!r})"
            ".write_text('x')"
        ),
    )
    backend = _backend(jobs=4)
    build = backend.submit_build(_build_spec(tmp_path))
    sims = [
        backend.submit(_sim_spec(tmp_path, f"t{i}"), dependency=build.job_id)
        for i in range(2)
    ]

    with caplog.at_level(logging.WARNING):
        backend.wait_all([build, *sims])

    assert list(tmp_path.glob("*.ran")) == []
    assert backend._jobs[build.job_id].returncode == 3
    assert all(backend._jobs[h.job_id].skipped for h in sims)
    assert "dispatch.dependency_failed" in [
        r.__dict__.get("rtl_event") for r in caplog.records
    ]


def test_ungated_sims_run_even_when_another_suites_build_fails(monkeypatch, tmp_path):
    """Only a job's own dependency gates it (mixed-builder suites, #358)."""
    _stub_argv(
        monkeypatch,
        build=_python("raise SystemExit(1)"),
        sim=lambda spec: _python(
            "from pathlib import Path;"
            f"Path({str(tmp_path)!r}).joinpath({spec.test_name + '.ran'!r})"
            ".write_text('x')"
        ),
    )
    backend = _backend(jobs=4)
    build = backend.submit_build(_build_spec(tmp_path))
    gated = backend.submit(_sim_spec(tmp_path, "gated"), dependency=build.job_id)
    free = backend.submit(_sim_spec(tmp_path, "free"))

    backend.wait_all([build, gated, free])
    assert sorted(p.name for p in tmp_path.glob("*.ran")) == ["free.ran"]


def test_build_jobs_start_before_queued_sims(monkeypatch, tmp_path):
    """A second suite's build must not queue behind the first suite's sims.

    A sim unblocks nothing; a build unblocks a whole fan-out. With one slot
    free, the build goes first even though it was submitted last.
    """
    _stub_argv(
        monkeypatch,
        sim=_python("import time; time.sleep(30)"),
        build=_python("import time; time.sleep(30)"),
    )
    backend = _backend(jobs=1)

    sims = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(3)]
    build = backend.submit_build(_build_spec(tmp_path))
    # t0 took the only slot at submit time; free it and pump.
    backend.cancel_all([sims[0]])
    backend._pump()

    assert backend._jobs[build.job_id].running
    assert not any(backend._jobs[h.job_id].running for h in sims[1:])
    backend.cancel_all([build, *sims])


def test_dependency_on_an_unknown_job_is_fatal(tmp_path):
    backend = _backend()
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend.submit(_sim_spec(tmp_path, "t0"), dependency="nope")
    assert "unknown dependency job id" in str(excinfo.value)


# ---- logs, teardown, telemetry -----------------------------------------


def test_job_output_goes_to_its_log_not_the_heads_stdout(monkeypatch, tmp_path, capfd):
    """A job's stdout is a log file, never the head's stream.

    Every job runs ``rb --machine``, so inherited stdout would interleave
    its result envelope into the head's own machine-mode output.
    """
    _stub_argv(
        monkeypatch,
        sim=_python(
            "import sys;print('to-stdout');print('to-stderr', file=sys.stderr)"
        ),
    )
    backend = _backend(jobs=1)
    handle = backend.submit(_sim_spec(tmp_path, "t0"))
    backend.wait_all([handle])

    log = (tmp_path / "t0.log").read_text()
    assert "to-stdout" in log
    assert "to-stderr" in log  # stderr merges into the log, as sbatch does
    captured = capfd.readouterr()
    assert "to-stdout" not in captured.out
    assert "to-stdout" not in captured.err


def test_log_directory_is_created_for_the_job(monkeypatch, tmp_path):
    _stub_argv(monkeypatch, sim=_python("print('hi')"))
    backend = _backend(jobs=1)
    spec = _sim_spec(tmp_path, "t0")
    spec.log_path = tmp_path / "nested" / "dir" / "t0.log"
    handle = backend.submit(spec)
    backend.wait_all([handle])
    assert spec.log_path.is_file()


def test_cancel_all_kills_running_and_disarms_queued(monkeypatch, tmp_path):
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=2)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(4)]
    running = [
        backend._jobs[h.job_id].proc for h in handles if backend._jobs[h.job_id].running
    ]
    assert len(running) == 2

    backend.cancel_all(handles)

    assert all(proc.poll() is not None for proc in running)
    # Cancelled jobs are terminal, so a later wait returns instead of
    # restarting the fleet the head has given up on.
    backend.wait_all(handles)
    assert _states(backend)["queued"] == 0
    assert list(tmp_path.glob("*.ran")) == []


def test_cancel_all_signals_every_job_before_waiting_on_any(monkeypatch, tmp_path):
    """Teardown is bounded by ONE grace period, not one per job.

    Three jobs that ignore SIGTERM must all be signalled first and then
    escalated against a shared deadline; signalling and waiting in the same
    loop would cost `jobs × kill_timeout` and would leave the jobs past an
    interrupted wait unsignalled.
    """
    _stub_argv(
        monkeypatch,
        sim=_python(
            "import signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "time.sleep(30)"
        ),
    )
    backend = _backend(jobs=3)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(3)]
    procs = [backend._jobs[h.job_id].proc for h in handles]
    assert all(p.poll() is None for p in procs)

    monkeypatch.setattr(lp_module, "DEFAULT_KILL_TIMEOUT", 0.3)
    started = time.monotonic()
    backend.cancel_all(handles)
    elapsed = time.monotonic() - started

    # One shared 0.3s grace, not 3 x 0.3s — allow generous slack for process
    # teardown while still failing a per-job serial wait.
    assert elapsed < 0.9, elapsed
    assert all(p.poll() is not None for p in procs)
    # Every job ends terminal, so a later wait_all cannot spin on one.
    assert all(backend._jobs[h.job_id].finished for h in handles)
    backend.wait_all(handles)


def test_cancel_all_marks_an_already_exited_job_terminal(monkeypatch, tmp_path):
    """`returncode` is read back, not assumed from the signal.

    A job that exited on its own between the last sweep and cancellation is
    never signalled successfully; if its returncode stayed None it would look
    forever-running and `wait_all` would spin.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1)
    handle = backend.submit(_sim_spec(tmp_path, "t0"))
    backend._jobs[handle.job_id].proc.wait()  # exits on its own, unreaped

    backend.cancel_all([handle])
    job = backend._jobs[handle.job_id]
    assert job.returncode is not None
    assert job.finished
    backend.wait_all([handle])


def test_advance_refills_the_pool_without_waiting(monkeypatch, tmp_path):
    """The head pokes the pool while it plans the next suite.

    Nothing else pumps between submissions, so a slot freed while the head is
    expanding another suite's sweep would sit idle to the end of planning.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(2)]
    # t0 took the only slot; wait for it to exit with nothing pumping.
    backend._jobs[handles[0].job_id].proc.wait()
    assert not backend._jobs[handles[1].job_id].running

    backend.advance()
    assert backend._jobs[handles[1].job_id].running
    backend.wait_all(handles)
    assert all(backend._jobs[h.job_id].returncode == 0 for h in handles)


def test_finished_jobs_leave_the_sweep_sets(monkeypatch, tmp_path):
    """A sweep is O(outstanding), not O(every job ever submitted)."""
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=2)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(5)]
    backend.wait_all(handles)
    assert backend._queued == []
    assert backend._running == {}
    assert len(backend._jobs) == 5  # still addressable by id for collection


def test_cancel_all_tolerates_none_handles(monkeypatch, tmp_path):
    """A None must not disarm the last defence against an orphaned fleet (#361)."""
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=1)
    handle = backend.submit(_sim_spec(tmp_path, "t0"))
    proc = backend._jobs[handle.job_id].proc

    backend.cancel_all([None, handle])
    assert proc.poll() is not None


def test_cancel_all_on_empty_fleet_is_a_noop():
    _backend().cancel_all([])


def test_wait_all_on_empty_fleet_is_a_noop():
    _backend().wait_all([])


def test_submit_array_falls_back_to_one_process_per_spec(monkeypatch, tmp_path):
    """There are no arrays without a scheduler: the ABC's loop is the impl."""
    _stub_argv(
        monkeypatch,
        sim=lambda spec: _python(
            "from pathlib import Path;"
            f"Path({str(tmp_path)!r}).joinpath({spec.test_name + '.ran'!r})"
            ".write_text('x')"
        ),
    )
    backend = _backend(jobs=2)
    specs = [_sim_spec(tmp_path, f"t{i}") for i in range(3)]
    handles = backend.submit_array(specs, array_dir=tmp_path / "arr", max_parallel=1)
    assert len(handles) == 3
    backend.wait_all(handles)
    assert sorted(p.name for p in tmp_path.glob("*.ran")) == [
        f"t{i}.ran" for i in range(3)
    ]


def test_no_telemetry_without_an_accounting_source(monkeypatch, tmp_path):
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1)
    handle = backend.submit(_sim_spec(tmp_path, "t0"))
    backend.wait_all([handle])
    assert backend.collect_telemetry([handle]) == {}


def test_failed_launch_is_fatal(monkeypatch, tmp_path):
    _stub_argv(monkeypatch, sim=[str(tmp_path / "does-not-exist")])
    backend = _backend(jobs=1)
    with pytest.raises(FatalRtlBuddyError) as excinfo:
        backend.submit(_sim_spec(tmp_path, "t0"))
    assert "could not start" in str(excinfo.value)


def test_cancel_all_survives_a_repeated_handle(monkeypatch, tmp_path):
    """Teardown must not abort partway through on a duplicated handle.

    `cancel_all` is the last line of defence against an orphaned fleet, so a
    caller that lists one job twice (the shape that produced #361) must not
    make it raise and leave the rest of the fleet running.
    """
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=1)
    running = backend.submit(_sim_spec(tmp_path, "t0"))
    queued = backend.submit(_sim_spec(tmp_path, "t1"))
    proc = backend._jobs[running.job_id].proc

    backend.cancel_all([running, running, queued, queued, None])

    assert proc.poll() is not None
    assert backend._jobs[queued.job_id].skipped
    assert backend._queued == []
    backend.wait_all([running, queued])


# ---- progress reporting (#435) -------------------------------------------


def test_wait_all_drives_the_progress_reporter(monkeypatch, tmp_path, caplog):
    """The pool's liveness signal is the reporter, not the old DEBUG line.

    `dispatch.waiting` was DEBUG-only, so a laptop regression that takes an
    hour said nothing between submit and drain either.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=2)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(3)]

    with caplog.at_level(logging.INFO):
        backend.wait_all(handles)

    progress = [
        r for r in caplog.records if r.__dict__.get("rtl_event") == "dispatch.progress"
    ]
    assert progress, "expected the pool's wait to report progress"
    assert progress[0].levelno == logging.INFO
    assert progress[0].__dict__["rtl_fields"]["total"] == 3
    # The DEBUG placeholder it replaces is gone.
    assert "dispatch.waiting" not in _events(caplog)
    # ...and the suite is reported as finished, not as passed.
    drained = [
        r
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.suite_drained"
    ]
    assert drained and "finished" in drained[0].message


def test_cancelled_warning_names_the_jobs(monkeypatch, tmp_path, caplog):
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=1)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(2)]

    with caplog.at_level(logging.WARNING):
        backend.cancel_all(handles)

    (record,) = [
        r for r in caplog.records if r.__dict__.get("rtl_event") == "dispatch.cancelled"
    ]
    assert record.__dict__["rtl_fields"]["job_ids"] == ["lp-1", "lp-2"]
