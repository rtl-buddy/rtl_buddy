from types import SimpleNamespace

import pytest

from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.rtl_buddy import RtlBuddy
from rtl_buddy.tools import vlog_sim as vlog_sim_module
from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.vcs_cov import VcsCov, VcsCoverageMergeResult


class DummyRootCfg:
  def __init__(self, project_root, family="vcs"):
    self._project_root = str(project_root)
    self._family = family

  def get_project_rootdir(self):
    return self._project_root

  def get_rtl_builder_cfg(self):
    return SimpleNamespace(
      get_simulator_family=lambda: self._family,
      get_name=lambda: self._family,
    )

  def get_use_lcov(self, _simulator_name):
    return self._family == "verilator"

  def get_coverview_cfg(self, _simulator_name):
    return None


def test_validate_coverage_flags_allows_vcs_coverage_merge():
  rb = RtlBuddy(name="rtl_buddy")
  rb.root_cfg = DummyRootCfg("/tmp", family="vcs")

  rb._validate_coverage_flags(coverage_merge=True)


@pytest.mark.parametrize(
  ("kwargs", "message"),
  [
    ({"coverage_merge_raw": True}, "--coverage-merge-raw is only supported for Verilator"),
    ({"coverage_merge_info_process": True}, "--coverage-merge-info-process is only supported for Verilator"),
    ({"coverage_html": True}, "--coverage-html is not supported for VCS coverage merge"),
    ({"coverage_coverview": True}, "--coverage-coverview is not supported for VCS coverage merge"),
    ({"coverage_per_test": True}, "--coverage-per-test is not supported for VCS coverage merge"),
  ],
)
def test_validate_coverage_flags_rejects_unsupported_vcs_modes(kwargs, message):
  rb = RtlBuddy(name="rtl_buddy")
  rb.root_cfg = DummyRootCfg("/tmp", family="vcs")

  with pytest.raises(FatalRtlBuddyError, match=message):
    rb._validate_coverage_flags(**kwargs)


def test_vlog_sim_post_records_vcs_vdb_path(monkeypatch, tmp_path):
  class FakePost:
    def __init__(self, **_kwargs):
      pass

    def get_results(self):
      return SimpleNamespace(results={"result": "PASS", "desc": "ok"})

  vdb_path = tmp_path / "artefacts" / "basic" / "simv.vdb"
  vdb_path.mkdir(parents=True)

  sim = vlog_sim_module.VlogSim.__new__(vlog_sim_module.VlogSim)
  sim.test_cfg = SimpleNamespace(uvm=None)
  sim.test_name = "basic"
  sim.root_cfg = DummyRootCfg(tmp_path, family="vcs")
  sim.run_id = None
  sim.vlog_post = None
  sim._coverage_enabled = lambda: True
  sim._get_simulator_family = lambda: "vcs"
  sim._get_vdb_abspath = lambda run_id=None: str(vdb_path)
  sim._get_log_path = lambda run_id=None: str(tmp_path / "artefacts" / "basic" / "test.log")

  monkeypatch.setattr(vlog_sim_module, "VlogPost", FakePost)

  results = sim.post()

  assert results.results["coverage"] == {
    "backend": "vcs",
    "vdb_paths": [str(vdb_path)],
  }


def test_vlog_sim_post_skips_missing_vcs_vdb(monkeypatch, tmp_path):
  class FakePost:
    def __init__(self, **_kwargs):
      pass

    def get_results(self):
      return SimpleNamespace(results={"result": "PASS", "desc": "ok"})

  sim = vlog_sim_module.VlogSim.__new__(vlog_sim_module.VlogSim)
  sim.test_cfg = SimpleNamespace(uvm=None)
  sim.test_name = "basic"
  sim.root_cfg = DummyRootCfg(tmp_path, family="vcs")
  sim.run_id = None
  sim.vlog_post = None
  sim._coverage_enabled = lambda: True
  sim._get_simulator_family = lambda: "vcs"
  sim._get_vdb_abspath = lambda run_id=None: str(tmp_path / "artefacts" / "basic" / "simv.vdb")
  sim._get_log_path = lambda run_id=None: str(tmp_path / "artefacts" / "basic" / "test.log")

  monkeypatch.setattr(vlog_sim_module, "VlogPost", FakePost)

  results = sim.post()

  assert "coverage" not in results.results


def test_coverage_reporter_collect_vdb_paths_filters_results(tmp_path):
  vdb_pass = tmp_path / "pass.vdb"
  vdb_skip = tmp_path / "skip.vdb"
  vdb_fail = tmp_path / "fail.vdb"
  vdb_pass.mkdir()
  vdb_skip.mkdir()
  vdb_fail.mkdir()

  reporter = CoverageReporter(DummyRootCfg(tmp_path, family="vcs"))
  suite_results = [
    {
      "test_name": "pass",
      "results": SimpleNamespace(results={"result": "PASS", "coverage": {"vdb_paths": [str(vdb_pass), str(vdb_pass)]}}),
    },
    {
      "test_name": "skip",
      "results": SimpleNamespace(results={"result": "SKIP", "coverage": {"vdb_paths": [str(vdb_skip)]}}),
    },
    {
      "test_name": "fail",
      "results": SimpleNamespace(results={"result": "FAIL", "coverage": {"vdb_paths": [str(vdb_fail)]}}),
    },
  ]

  assert reporter.collect_vdb_paths(suite_results) == [str(vdb_pass.resolve()), str(vdb_skip.resolve())]


def test_coverage_reporter_build_metadata_for_vcs_merge(monkeypatch, tmp_path):
  reporter = CoverageReporter(DummyRootCfg(tmp_path, family="vcs"))
  expected = VcsCoverageMergeResult(
    coverage_file=str(tmp_path / "cov_dir" / "coverage.f"),
    merged_vdb=str(tmp_path / "cov_dir" / "merged.vdb"),
    report_dir=str(tmp_path / "cov_dir" / "urgReport"),
    input_count=2,
  )
  monkeypatch.setattr(reporter, "merge_vcs", lambda suite_results, outdir: expected)

  metadata = reporter.build_metadata(
    suite_results=[],
    outdir=str(tmp_path),
    suite_name="tests.yaml",
    coverage_merge=True,
  )

  assert metadata == [
    "Merged Coverage Inputs: 2",
    f"Merged Coverage File: {expected.coverage_file}",
    f"Merged VDB: {expected.merged_vdb}",
    f"Merged URG Report: {expected.report_dir}",
  ]


def test_vcs_cov_merge_invokes_urg(monkeypatch, tmp_path):
  cov = VcsCov()
  outdir = tmp_path / "cov_dir"
  vdb_a = tmp_path / "a.vdb"
  vdb_b = tmp_path / "b.vdb"
  vdb_a.mkdir()
  vdb_b.mkdir()
  captured = {}

  class Result:
    returncode = 0
    stdout = ""
    stderr = ""

  def _fake_run(cmd, capture_output, text, cwd):
    captured["cmd"] = cmd
    captured["cwd"] = cwd
    (outdir / "merged.vdb").mkdir(parents=True)
    (outdir / "urgReport").mkdir()
    return Result()

  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.shutil.which", lambda exe: f"/usr/bin/{exe}")
  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.subprocess.run", _fake_run)

  result = cov.merge([str(vdb_a), str(vdb_b)], str(outdir))

  assert captured["cwd"] == str(outdir)
  assert captured["cmd"] == [
    "/usr/bin/urg",
    "-f", str(outdir / "coverage.f"),
    "-dbname", str(outdir / "merged.vdb"),
    "-report", str(outdir / "urgReport"),
    "-lca",
    "-format", "both",
    "-show", "tests",
  ]
  assert (outdir / "coverage.f").read_text() == f"{vdb_a.resolve()}\n{vdb_b.resolve()}\n"
  assert result.input_count == 2


def test_vcs_cov_merge_requires_urg(tmp_path):
  cov = VcsCov()
  vdb_path = tmp_path / "a.vdb"
  vdb_path.mkdir()
  monkeypatch = pytest.MonkeyPatch()
  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.shutil.which", lambda exe: None)

  try:
    with pytest.raises(FatalRtlBuddyError, match="requires `urg` on PATH"):
      cov.merge([str(vdb_path)], str(tmp_path / "cov_dir"))
  finally:
    monkeypatch.undo()


def test_vcs_cov_merge_requires_inputs(tmp_path):
  cov = VcsCov()

  with pytest.raises(FatalRtlBuddyError, match="No eligible VCS coverage databases found for merge"):
    cov.merge([], str(tmp_path / "cov_dir"))


def test_vcs_cov_merge_raises_on_urg_failure(monkeypatch, tmp_path):
  cov = VcsCov()
  vdb_path = tmp_path / "a.vdb"
  vdb_path.mkdir()

  class Result:
    returncode = 3
    stdout = ""
    stderr = "urg failed"

  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.shutil.which", lambda exe: f"/usr/bin/{exe}")
  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.subprocess.run", lambda *args, **kwargs: Result())

  with pytest.raises(FatalRtlBuddyError, match="urg coverage merge failed: urg failed"):
    cov.merge([str(vdb_path)], str(tmp_path / "cov_dir"))


def test_vcs_cov_merge_raises_when_outputs_missing(monkeypatch, tmp_path):
  cov = VcsCov()
  vdb_path = tmp_path / "a.vdb"
  vdb_path.mkdir()

  class Result:
    returncode = 0
    stdout = ""
    stderr = ""

  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.shutil.which", lambda exe: f"/usr/bin/{exe}")
  monkeypatch.setattr("rtl_buddy.tools.vcs_cov.subprocess.run", lambda *args, **kwargs: Result())

  with pytest.raises(FatalRtlBuddyError, match="expected outputs are missing"):
    cov.merge([str(vdb_path)], str(tmp_path / "cov_dir"))
