"""Tests for the raw coverage merge run in parallel shards (#783).

`verilator_coverage --write` is single-threaded, so a tail with several CPUs splits the raw
databases into groups, merges each into a partial in parallel and merges the partials. A fake
`verilator_coverage` sums counts per point, the property the real tool's merge has."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import rtl_buddy.rtl_buddy as rtl_buddy_module
from rtl_buddy.dispatch.coverage_tail import write_tail_spec
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools import vlog_cov as vlog_cov_module
from rtl_buddy.tools.vlog_cov import VlogCov, plan_merge_shards


def _raw_inputs(tmp_path: Path, count: int) -> list[str]:
    """``count`` raw databases over three points with per-test counts."""
    paths = []
    for index in range(count):
        path = tmp_path / "artefacts" / f"t{index:04d}" / "coverage.dat"
        path.parent.mkdir(parents=True)
        path.write_text(f"a {index}\nb 1\nc {index % 3}\n")
        paths.append(str(path))
    return paths


class _FakeVerilatorCoverage:
    """`verilator_coverage --write <out> <in>...`: sums each point's count over the inputs.

    Records each call and the most calls seen running at once. ``fail_when`` names an
    input whose presence makes that call exit 1.
    """

    def __init__(self, *, delay=0.0, fail_when=None):
        self.calls = []
        self.delay = delay
        self.fail_when = fail_when
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, cmd, **kwargs):
        assert cmd[:2] == ["verilator_coverage", "--write"], cmd
        output, inputs = cmd[2], cmd[3:]
        with self.lock:
            self.calls.append({"output": output, "inputs": list(inputs), **kwargs})
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            if self.fail_when is not None and self.fail_when in inputs:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="bad database", timed_out=False
                )
            totals = {}
            for path in inputs:
                for line in Path(path).read_text().splitlines():
                    name, count = line.split()
                    totals[name] = totals.get(name, 0) + int(count)
            Path(output).write_text(
                "".join(f"{name} {totals[name]}\n" for name in sorted(totals))
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="", timed_out=False)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def fake_tool(monkeypatch):
    def install(**kwargs):
        tool = _FakeVerilatorCoverage(**kwargs)
        monkeypatch.setattr(vlog_cov_module, "run_managed_process", tool)
        return tool

    return install


def _merge(tmp_path, raw_paths, *, jobs, mem=None, timeout=None):
    outdir = tmp_path / "cov_dir"
    outdir.mkdir(exist_ok=True)
    root_cfg = SimpleNamespace(get_coverage_merge_timeout=lambda _family: timeout)
    cov = VlogCov("verilator", root_cfg=root_cfg, merge_jobs=jobs, merge_mem_bytes=mem)
    merged = cov._merge_raw(
        raw_paths, str(outdir / "coverage_merged.dat"), outdir=str(outdir)
    )
    return merged, outdir


@pytest.fixture(autouse=True)
def _plenty_of_cpus(monkeypatch):
    """The reservation, not the test host, decides the shard count."""
    monkeypatch.setattr(vlog_cov_module, "usable_cpu_count", lambda: 64)


# --- the shard plan ---


def test_one_job_is_one_merge_over_every_input():
    paths = [f"/r/t{index}/coverage.dat" for index in range(200)]
    assert plan_merge_shards(paths, 1) == [paths]


def test_shards_are_contiguous_near_equal_and_keep_input_order():
    paths = [f"/r/t{index}/coverage.dat" for index in range(100)]
    shards = plan_merge_shards(paths, 4)
    assert [len(shard) for shard in shards] == [25, 25, 25, 25]
    assert [path for shard in shards for path in shard] == paths
    uneven = plan_merge_shards(paths[:99], 4)
    assert [len(shard) for shard in uneven] == [25, 25, 25, 24]


def test_a_small_run_is_not_sharded_below_the_minimum_inputs_per_shard():
    minimum = vlog_cov_module.MERGE_SHARD_MIN_INPUTS
    paths = [f"/r/t{index}" for index in range(2 * minimum - 1)]
    assert len(plan_merge_shards(paths, 32)) == 1
    paths = [f"/r/t{index}" for index in range(2 * minimum)]
    assert len(plan_merge_shards(paths, 32)) == 2


def test_an_input_list_past_the_argv_budget_is_split_even_with_one_job(monkeypatch):
    monkeypatch.setattr(vlog_cov_module, "MERGE_ARGV_BUDGET", 1000)
    paths = [f"/nfs/run/{'x' * 80}/t{index:03d}/coverage.dat" for index in range(50)]
    shards = plan_merge_shards(paths, 1)
    assert len(shards) > 1
    assert all(sum(len(path) + 1 for path in shard) <= 1100 for shard in shards)
    assert [path for shard in shards for path in shard] == paths


# --- the merge ---


def test_one_job_runs_todays_single_merge(tmp_path, fake_tool):
    tool = fake_tool()
    raw = _raw_inputs(tmp_path, 40)
    merged, outdir = _merge(tmp_path, raw, jobs=1)
    assert merged == str(outdir / "coverage_merged.dat")
    assert [(call["output"], call["inputs"]) for call in tool.calls] == [(merged, raw)]


def test_a_sharded_merge_equals_the_single_merge(tmp_path, fake_tool):
    raw = _raw_inputs(tmp_path, 100)
    fake_tool()
    single, outdir = _merge(tmp_path, raw, jobs=1)
    expected = Path(single).read_text()
    Path(single).unlink()

    tool = fake_tool(delay=0.05)
    sharded, _ = _merge(tmp_path, raw, jobs=4)
    assert Path(sharded).read_text() == expected

    shard_calls, final = tool.calls[:-1], tool.calls[-1]
    assert len(shard_calls) == 4
    assert sorted(path for call in shard_calls for path in call["inputs"]) == sorted(
        raw
    )
    assert final["output"] == sharded
    assert sorted(final["inputs"]) == sorted(call["output"] for call in shard_calls)
    assert tool.peak > 1, "the shards ran one after another"
    # The partials are gone; nothing but the merged database is left.
    assert sorted(path.name for path in outdir.iterdir()) == ["coverage_merged.dat"]


def test_no_more_merges_run_at_once_than_the_reservation(tmp_path, fake_tool):
    tool = fake_tool(delay=0.05)
    raw = _raw_inputs(tmp_path, 160)
    assert 160 // vlog_cov_module.MERGE_SHARD_MIN_INPUTS >= 3
    _merge(tmp_path, raw, jobs=3)
    assert len(tool.calls) == 4  # three shards and the final merge
    assert tool.peak <= 3


def test_the_job_count_is_bounded_by_the_cpus_the_process_may_use(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(vlog_cov_module, "usable_cpu_count", lambda: 2)
    raw = _raw_inputs(tmp_path, 4)
    assert VlogCov("verilator", merge_jobs=32)._merge_jobs(raw) == 2
    # In the head, the host's CPUs up to the cap.
    monkeypatch.setattr(vlog_cov_module, "usable_cpu_count", lambda: 64)
    assert VlogCov("verilator")._merge_jobs(raw) == vlog_cov_module.LOCAL_MERGE_JOBS_CAP


def test_the_job_count_fits_the_memory_reservation(tmp_path):
    raw = _raw_inputs(tmp_path, 4)
    floor = vlog_cov_module.MERGE_MEM_FLOOR
    assert (
        VlogCov("verilator", merge_jobs=32, merge_mem_bytes=3 * floor)._merge_jobs(raw)
        == 3
    )
    # A large input raises the per-process estimate above the floor.
    big = Path(raw[0])
    big.write_text("a 1\n" + "#" * (floor // 4))
    assert (
        VlogCov("verilator", merge_jobs=32, merge_mem_bytes=3 * floor)._merge_jobs(raw)
        == 1
    )


def test_a_failed_shard_fails_the_merge_and_names_the_shard(
    tmp_path, fake_tool, caplog
):
    raw = _raw_inputs(tmp_path, 64)
    tool = fake_tool(fail_when=raw[40])
    with caplog.at_level(logging.ERROR):
        merged, outdir = _merge(tmp_path, raw, jobs=4)
    assert merged is None
    assert not (outdir / "coverage_merged.dat").exists()
    assert all(
        call["output"] != str(outdir / "coverage_merged.dat") for call in tool.calls
    )
    failures = [
        record.__dict__["rtl_fields"]
        for record in caplog.records
        if record.__dict__.get("rtl_event") == "coverage.merge.failed"
    ]
    assert len(failures) == 1
    assert failures[0]["shard"] == "3/4"
    assert failures[0]["merged_path"] == str(outdir / "coverage_merged.dat")
    assert raw[40] in failures[0]["inputs"]
    assert list(outdir.iterdir()) == []


def test_a_failed_shard_fails_the_reported_metrics(tmp_path, fake_tool):
    """Through `merge()`: the merge-only metrics read FAIL, as for an unsharded failure."""
    raw = _raw_inputs(tmp_path, 64)
    fake_tool(fail_when=raw[0])
    outdir = tmp_path / "cov_dir"
    outdir.mkdir()
    cov = VlogCov(
        "verilator",
        root_cfg=SimpleNamespace(get_coverage_merge_timeout=lambda _family: None),
        merge_jobs=4,
    )
    cov.is_supported = lambda: True
    metrics = cov.merge(raw, str(outdir))
    assert metrics.merge_failed is True
    assert metrics.merged_path is None
    assert "toggle" in metrics.failed_metrics


def test_the_merge_timeout_bounds_the_whole_sharded_merge(tmp_path, fake_tool):
    tool = fake_tool()
    raw = _raw_inputs(tmp_path, 64)
    _merge(tmp_path, raw, jobs=4, timeout=600)
    timeouts = [call["timeout"] for call in tool.calls]
    assert all(0 < value <= 600 for value in timeouts)
    # The final merge gets what the shards left, never a fresh budget.
    assert timeouts[-1] <= min(timeouts[:-1])


# --- the dispatched tail sizes the merge from its reservation ---


def test_the_coverage_job_hands_its_reservation_to_the_merge(
    minimal_project: Path, monkeypatch
):
    root_cfg = minimal_project / "root_config.yaml"
    root_cfg.write_text(
        root_cfg.read_text() + "cfg-dispatch:\n"
        "  coverage:\n"
        "    cpus: 2\n"
        "    mem: 4G\n"
        "    modes:\n"
        "      cov: {cpus: 16, mem: 24G}\n"
    )
    spec, _ = write_tail_spec(
        minimal_project / "spec.json",
        [{"suite_results": [], "outdir": str(minimal_project), "suite_name": "s"}],
        run_token="tok",
    )
    seen = {}

    def capture(self, suite_results, **kwargs):
        seen["jobs"] = self.merge_jobs
        seen["mem"] = self.merge_mem_bytes
        seen["tool"] = self._get_cov_tool()
        return [], {}

    monkeypatch.setattr(rtl_buddy_module.CoverageReporter, "build_metadata", capture)
    result = CliRunner().invoke(
        RtlBuddy(name="test_cov_merge_jobs").app,
        [
            "-M",
            "cov",
            "_cov-job",
            "--spec",
            str(spec),
            "--result-json",
            str(minimal_project / "result.json"),
            "--command-root",
            str(minimal_project),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["jobs"] == 16
    assert seen["mem"] == 24 * 2**30
    assert seen["tool"].merge_jobs == 16
    assert seen["tool"].merge_mem_bytes == 24 * 2**30
    assert json.loads((minimal_project / "result.json").read_text())["calls"]
