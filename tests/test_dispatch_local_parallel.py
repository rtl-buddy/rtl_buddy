"""Tests for LocalProcessBackend against real subprocesses.

Only the argv the pool launches is stubbed: each job is a short ``python -c``
that records what it saw, so gating, the concurrency cap, dependency-failure
skipping and cancellation are observable in plain files.
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

    ``sim`` and ``build`` are a list (one program for every job) or a callable
    taking the spec.
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


def test_registry_exposes_local_parallel():
    backend = create_dispatch_backend(
        "local-parallel", DispatchConfigFile().initialise()
    )
    assert isinstance(backend, LocalProcessBackend)
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
    """A reservation the pool cannot enforce warns at WARNING, visible without -v."""
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
    """The notice keys off the resolved reservation, not just cfg-dispatch.

    Per-test and per-testbench reservations also trigger it.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1)
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
    assert "dispatch.pool_configured" in events


def _parallel_build_spec(tmp_path: Path, parallel: int, cpus: int) -> BuildJobSpec:
    """A build spec shaped the way the head submits one.

    The head multiplies the resolved compile cpus by ``parallel``.
    """
    spec = _build_spec(tmp_path)
    spec.parallel = parallel
    spec.resources = JobResources(cpus=cpus * parallel)
    return spec


def test_compile_parallel_alone_is_not_a_reservation_to_ignore(
    monkeypatch, tmp_path, caplog
):
    """`compile: {parallel: 2}` alone stays quiet.

    The scaled cpus are not a reservation the project wrote.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend()
    spec = _parallel_build_spec(tmp_path, parallel=2, cpus=JobResources().cpus)
    with caplog.at_level(logging.DEBUG):
        handle = backend.submit_build(spec)
        backend.wait_all([handle])
    assert "dispatch.reservations_ignored" not in _events(caplog)


def test_a_real_compile_reservation_still_warns_under_parallel(
    monkeypatch, tmp_path, caplog
):
    """A project that reserved 4 cpus per build still gets the notice, quoting 4."""
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend()
    spec = _parallel_build_spec(tmp_path, parallel=2, cpus=4)
    with caplog.at_level(logging.WARNING):
        handle = backend.submit_build(spec)
        backend.wait_all([handle])
    warnings = [
        r
        for r in caplog.records
        if r.__dict__.get("rtl_event") == "dispatch.reservations_ignored"
    ]
    assert len(warnings) == 1
    assert warnings[0].__dict__["rtl_fields"]["cpus"] == 4


def test_cap_bounds_running_jobs_and_queues_the_rest(monkeypatch, tmp_path):
    """Five submits against a two-slot pool: two run, three wait."""
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=2)

    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(5)]
    assert _states(backend) == {"queued": 3, "running": 2, "finished": 0}

    backend.cancel_all(handles)
    assert _states(backend)["running"] == 0


def test_pool_refills_slots_until_every_job_ran(monkeypatch, tmp_path):
    """A capped pool still runs every job."""
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
    """Overlap across the four jobs is at least 2 and never exceeds the cap.

    Each job records its own start and end time; the overlap is computed afterwards.
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


def test_sims_wait_for_the_build_to_succeed(monkeypatch, tmp_path):
    stamp = tmp_path / "build.stamp"
    _stub_argv(
        monkeypatch,
        build=_python(
            "import time;from pathlib import Path;time.sleep(0.2);"
            f"Path({str(stamp)!r}).write_text('built')"
        ),
        # Each sim records whether the shared build had landed when it started.
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
    # Slots are free but the gate is shut: only the build runs.
    assert _states(backend)["running"] == 1

    backend.wait_all([build, *sims])
    saw = {p.name: p.read_text() for p in tmp_path.glob("*.saw")}
    assert saw == {f"t{i}.saw": "True" for i in range(3)}


def test_failed_build_skips_its_sims(monkeypatch, tmp_path, caplog):
    """A build that exits nonzero cancels its dependents, as afterok does.

    The sims leave no result envelope, so the collector reports no result.
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
    """Only a job's own dependency gates it."""
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
    """A second suite's build does not queue behind the first suite's sims.

    With one slot free, the build goes first even though it was submitted last.
    """
    _stub_argv(
        monkeypatch,
        sim=_python("import time; time.sleep(30)"),
        build=_python("import time; time.sleep(30)"),
    )
    backend = _backend(jobs=1)

    sims = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(3)]
    build = backend.submit_build(_build_spec(tmp_path))
    # t0 took the only slot; free it and pump.
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


def test_job_output_goes_to_its_log_not_the_heads_stdout(monkeypatch, tmp_path, capfd):
    """A job's stdout goes to a log file, never the head's stream.

    Every job runs rb --machine; inherited stdout would interleave its
    envelope into the head's output.
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
    assert "to-stderr" in log  # stderr merges into the log, as sbatch does.
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
    # Cancelled jobs are terminal, so a later wait returns.
    backend.wait_all(handles)
    assert _states(backend)["queued"] == 0
    assert list(tmp_path.glob("*.ran")) == []


def test_cancel_all_signals_every_job_before_waiting_on_any(monkeypatch, tmp_path):
    """Teardown is bounded by one grace period, not one per job.

    All jobs are signalled first, then escalated against a shared deadline.
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

    # One shared 0.3s grace, not 3 x 0.3s; the slack allows for process teardown.
    assert elapsed < 0.9, elapsed
    assert all(p.poll() is not None for p in procs)
    assert all(backend._jobs[h.job_id].finished for h in handles)
    backend.wait_all(handles)


def test_cancel_all_marks_an_already_exited_job_terminal(monkeypatch, tmp_path):
    """`returncode` is read back, not assumed from the signal.

    A job that exited on its own before cancellation must not look still running.
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
    """The head pumps the pool while it plans the next suite.

    Nothing else pumps between submissions, so a freed slot would sit idle.
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
    """A sweep visits only outstanding jobs, not every job ever submitted."""
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=2)
    handles = [backend.submit(_sim_spec(tmp_path, f"t{i}")) for i in range(5)]
    backend.wait_all(handles)
    assert backend._queued == []
    assert backend._running == {}
    assert len(backend._jobs) == 5  # still addressable by id for collection


def test_cancel_all_tolerates_none_handles(monkeypatch, tmp_path):
    """A None handle does not disarm cancel_all."""
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
    """Without a scheduler, arrays fall back to the ABC's per-element loop."""
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
    """cancel_all does not abort on a duplicated handle.

    A duplicate must not leave the rest of the fleet running.
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


def test_wait_all_drives_the_progress_reporter(monkeypatch, tmp_path, caplog):
    """The pool reports progress through the reporter, not a DEBUG-only line."""
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
    assert "dispatch.waiting" not in _events(caplog)
    # The suite is reported as finished, not as passed.
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


def test_a_delayed_job_waits_without_taking_a_slot(monkeypatch, tmp_path):
    """The pool holds a retry PENDING for its backoff, as Slurm does with ``--begin``.

    The hold does not occupy a slot.
    """
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=1)

    delayed = backend.submit(_sim_spec(tmp_path, "retried"), delay_sec=30)
    prompt = backend.submit(_sim_spec(tmp_path, "fresh"))
    backend.advance()

    assert not backend._jobs[delayed.job_id].running
    assert backend._jobs[prompt.job_id].running  # the slot was never held

    backend.cancel_all([delayed, prompt])


def test_a_delayed_job_runs_once_its_backoff_elapses(monkeypatch, tmp_path):
    _stub_argv(
        monkeypatch,
        sim=lambda spec: _python(
            f"from pathlib import Path;"
            f"Path({str(tmp_path / (spec.test_name + '.done'))!r}).write_text('x')"
        ),
    )
    backend = _backend(jobs=2)
    handle = backend.submit(_sim_spec(tmp_path, "retried"), delay_sec=0.2)

    backend.advance()
    assert not (tmp_path / "retried.done").exists()  # still inside the backoff

    backend.wait_all([handle])
    assert (tmp_path / "retried.done").is_file()
    assert backend._jobs[handle.job_id].returncode == 0


def test_a_delayed_job_stays_outstanding_for_the_wait(monkeypatch, tmp_path):
    """wait_all does not declare the fleet drained while a retry is pending."""
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=2)
    handle = backend.submit(_sim_spec(tmp_path, "retried"), delay_sec=0.2)

    assert not backend._jobs[handle.job_id].finished
    started = time.monotonic()
    backend.wait_all([handle])
    assert time.monotonic() - started >= 0.2


def test_undelayed_submit_is_unchanged(monkeypatch, tmp_path):
    _stub_argv(monkeypatch, sim=_python("import time; time.sleep(30)"))
    backend = _backend(jobs=1)
    handle = backend.submit(_sim_spec(tmp_path, "t0"))
    assert backend._jobs[handle.job_id].not_before is None
    assert backend._jobs[handle.job_id].running
    backend.cancel_all([handle])


def test_a_held_job_does_not_spin_the_sweep_loop(monkeypatch, tmp_path):
    """While every outstanding job is held, the sweep sleeps until the earliest is due.

    The alternative is polling 20 times a second through the whole backoff.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=2)
    handle = backend.submit(_sim_spec(tmp_path, "retried"), delay_sec=30)

    held = [backend._jobs[handle.job_id]]
    # Capped at a second so cancellation and heartbeats stay responsive.
    assert backend._sweep_interval(held) == pytest.approx(1.0)

    # A running job is polled at the normal cadence.
    running = backend.submit(_sim_spec(tmp_path, "fresh"))
    backend.advance()
    outstanding = [backend._jobs[h.job_id] for h in (handle, running)]
    assert backend._sweep_interval(outstanding) == lp_module._POLL_INTERVAL_SEC

    backend.cancel_all([handle, running])


def test_the_wait_deadline_allows_for_a_backoff_it_was_told_about(
    monkeypatch, tmp_path
):
    """max-wait is not spent on a hold the head announced.

    A held job is outstanding for its whole backoff.
    """
    _stub_argv(monkeypatch, sim=_python("pass"))

    # Unannounced, the hold trips max-wait.
    strict = _backend(jobs=1, max_wait=0.1)
    held = strict.submit(_sim_spec(tmp_path, "unannounced"), delay_sec=0.5)
    with pytest.raises(FatalRtlBuddyError, match="max-wait"):
        strict.wait_all([held])
    strict.cancel_all([held])

    # Announced, the same delay is not charged against the budget.
    forgiving = _backend(jobs=1, max_wait=0.1)
    retried = forgiving.submit(_sim_spec(tmp_path, "announced"), delay_sec=0.5)
    forgiving.wait_all([retried], extra_wait=0.5)
    assert forgiving._jobs[retried.job_id].returncode == 0


def test_build_outcome_reports_the_build_process_exit_status(monkeypatch, tmp_path):
    """A pool-run build reports how it ended.

    `collect_telemetry` returns nothing here, so this distinguishes a build job
    that died from one that finished but lost the write of its result.
    """
    _stub_argv(monkeypatch, sim=_python("pass"), build=_python("pass"))
    backend = _backend(jobs=2)

    build = backend.submit_build(_build_spec(tmp_path))
    backend.wait_all([build])
    assert backend.build_outcome(build) == "COMPLETED"
    # The build's gate opens on the same outcome.
    assert backend.collect_telemetry([build]) == {}


def test_build_outcome_reports_a_failed_build(monkeypatch, tmp_path):
    _stub_argv(monkeypatch, sim=_python("pass"), build=_python("raise SystemExit(3)"))
    backend = _backend(jobs=2)

    build = backend.submit_build(_build_spec(tmp_path))
    backend.wait_all([build])
    assert backend.build_outcome(build) == "FAILED"


def test_build_outcome_is_none_for_a_job_this_pool_never_ran(monkeypatch, tmp_path):
    """``None`` keeps the caller's conservative reading."""
    from rtl_buddy.dispatch.base import JobHandle

    _stub_argv(monkeypatch, sim=_python("pass"))
    backend = _backend(jobs=1)
    assert backend.build_outcome(JobHandle(job_id="not-ours", spec=None)) is None
