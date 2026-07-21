import time

from rtl_buddy.process_utils import run_managed_process
from rtl_buddy.tools.vcs_license import VcsLicenseQueueMonitor


def _append(path, text):
    with open(path, "a") as fh:
        fh.write(text)


def test_marker_line_enters_queue(tmp_path):
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("")
    entered = []

    monitor = VcsLicenseQueueMonitor(
        log_path, err_path, on_enter_queue=lambda: entered.append(True)
    )
    assert monitor.is_waiting() is False

    _append(log_path, "Queuing for License ......\n")
    assert monitor.is_waiting() is True
    assert entered == [True]


def test_dots_only_continuation_stays_queued(tmp_path):
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("Queuing for License ......\n")
    err_path.write_text("")

    monitor = VcsLicenseQueueMonitor(log_path, err_path)
    assert monitor.is_waiting() is True

    _append(log_path, "..........\n")
    assert monitor.is_waiting() is True

    _append(log_path, "   ....   \n")
    assert monitor.is_waiting() is True


def test_real_output_line_exits_queue_and_reports_queued_sec(tmp_path):
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("Queuing for License ......\n")
    err_path.write_text("")

    exited = []
    monitor = VcsLicenseQueueMonitor(
        log_path, err_path, on_exit_queue=lambda queued_sec: exited.append(queued_sec)
    )
    assert monitor.is_waiting() is True

    time.sleep(0.05)
    _append(log_path, "simulation resumed\n")
    assert monitor.is_waiting() is False
    assert len(exited) == 1
    assert exited[0] >= 0.0
    assert monitor.queue_wait_sec == exited[0]


def test_licensed_number_of_users_marker_also_triggers(tmp_path):
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("")
    err_path.write_text("")

    monitor = VcsLicenseQueueMonitor(log_path, err_path)
    assert monitor.is_waiting() is False

    _append(
        err_path,
        "Licensed number of users already reached for VCS-BASE-RUNTIME/VCSRuntime_Net.\n",
    )
    assert monitor.is_waiting() is True


def test_cap_exceeded_returns_false_permanently(tmp_path):
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("Queuing for License ......\n")
    err_path.write_text("")

    monitor = VcsLicenseQueueMonitor(log_path, err_path, max_queue_wait_sec=0.2)
    assert monitor.is_waiting() is True
    assert monitor.cap_exceeded is False

    time.sleep(0.3)
    assert monitor.is_waiting() is False
    assert monitor.cap_exceeded is True

    # Even if real output now appears, the monitor stays permanently disabled.
    _append(log_path, "simulation resumed\n")
    assert monitor.is_waiting() is False
    assert monitor.cap_exceeded is True


def test_partial_line_is_buffered_until_newline(tmp_path):
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("")
    err_path.write_text("")

    monitor = VcsLicenseQueueMonitor(log_path, err_path)

    _append(log_path, "Queuing for Lic")
    assert monitor.is_waiting() is False

    _append(log_path, "ense ......\n")
    assert monitor.is_waiting() is True


def test_marker_without_trailing_newline_enters_queue(tmp_path):
    # VCS appends queue-polling dots to the banner without a newline, so the
    # marker may never complete as a line; it must still pause the clock.
    log_path = tmp_path / "test.log"
    err_path = tmp_path / "test.err"
    log_path.write_text("")
    err_path.write_text("")

    monitor = VcsLicenseQueueMonitor(log_path, err_path)

    _append(log_path, "Queuing for License ")
    assert monitor.is_waiting() is True

    _append(log_path, "....")
    assert monitor.is_waiting() is True

    _append(log_path, "\nChronologic VCS simulator copyright 1991-2024\n")
    assert monitor.is_waiting() is False


def test_missing_files_are_tolerated(tmp_path):
    log_path = tmp_path / "does-not-exist.log"
    err_path = tmp_path / "does-not-exist.err"

    monitor = VcsLicenseQueueMonitor(log_path, err_path)
    assert monitor.is_waiting() is False

    log_path.write_text("Queuing for License ......\n")
    assert monitor.is_waiting() is True


def test_run_managed_process_does_not_time_out_while_queuing(tmp_path):
    """Mirrors the real vlog_sim wiring: a real monitor watches the file the
    sim's stdout is redirected to, and pauses run_managed_process's timeout
    clock while the VCS license-queue banner is the most recent output.
    """
    script_path = tmp_path / "fake_vcs.sh"
    script_path.write_text(
        "#!/bin/sh\n"
        "echo 'Queuing for License ......'\n"
        "sleep 1.5\n"
        "echo 'simulation resumed'\n"
        "sleep 0.2\n"
        "exit 0\n"
    )
    script_path.chmod(0o755)

    log_path = tmp_path / "sim.log"
    err_path = tmp_path / "sim.err"

    monitor = VcsLicenseQueueMonitor(log_path, err_path)

    with open(log_path, "w") as out_fp, open(err_path, "w") as err_fp:
        result = run_managed_process(
            ["/bin/sh", str(script_path)],
            stdout=out_fp,
            stderr=err_fp,
            timeout=1.0,
            timeout_pauser=monitor.is_waiting,
        )

    assert result.timed_out is False
    assert result.returncode == 0
