from types import SimpleNamespace

from rtl_buddy.tools.coverage import CoverageReporter
from rtl_buddy.tools.coverview import CoverviewPacker
from rtl_buddy.tools import vlog_sim as vlog_sim_module
from rtl_buddy.tools.vlog_cov import CoverageMetrics, VlogCov


class DummyRootCfg:
  def __init__(self, project_root):
    self._project_root = str(project_root)

  def get_project_rootdir(self):
    return self._project_root

  def get_rtl_builder_cfg(self):
    return SimpleNamespace(
      get_simulator_family=lambda: "verilator",
      get_name=lambda: "verilator",
    )

  def get_use_lcov(self, _simulator_name):
    return True

  def get_coverview_cfg(self, _simulator_name):
    return None


def test_vlog_cov_normalize_lcov_paths_uses_suite_source_roots(tmp_path):
  repo_root = tmp_path / "repo"
  suite_dir = repo_root / "verif" / "sandbox"
  suite_dir.mkdir(parents=True)
  tb_top = suite_dir / "tb_top.sv"
  tb_top.write_text("module tb_top;\nendmodule\n")

  lcov_path = repo_root / "basic.coverage.info"
  lcov_path.write_text("SF:tb_top.sv\nDA:1,1\nend_of_record\n")

  cov = VlogCov(simulator_name="verilator", root_cfg=DummyRootCfg(repo_root))
  cov._normalize_lcov_paths(str(lcov_path), source_roots=[str(suite_dir)])

  assert lcov_path.read_text() == f"SF:{tb_top}\nDA:1,1\nend_of_record\n"


def test_vlog_cov_resolve_source_path_prefers_unique_suite_root_match(tmp_path):
  repo_root = tmp_path / "repo"
  sandbox_dir = repo_root / "verif" / "sandbox"
  template_dir = repo_root / "verif" / "template"
  sandbox_dir.mkdir(parents=True)
  template_dir.mkdir(parents=True)
  sandbox_tb = sandbox_dir / "tb_top.sv"
  template_tb = template_dir / "tb_top.sv"
  sandbox_tb.write_text("module tb_top;\nendmodule\n")
  template_tb.write_text("module tb_top;\nendmodule\n")

  cov = VlogCov(simulator_name="verilator", root_cfg=DummyRootCfg(repo_root))

  resolved = cov._resolve_source_path(
    "tb_top.sv",
    base_dir=sandbox_dir / "artefacts",
    source_roots=[str(sandbox_dir)],
  )

  assert resolved == sandbox_tb.resolve()


def test_coverage_reporter_generate_unmerged_artifacts_passes_suite_source_root(monkeypatch, tmp_path):
  captured = {}

  class FakeCov:
    def generate_artifacts(self, raw_path, outdir, html_output, artifact_name, source_roots, html_outdir=None):
      captured["raw_path"] = raw_path
      captured["outdir"] = outdir
      captured["html_output"] = html_output
      captured["artifact_name"] = artifact_name
      captured["source_roots"] = list(source_roots)
      captured["html_outdir"] = html_outdir
      return CoverageMetrics(lcov_path=str(tmp_path / "coverage.info"))

  reporter = CoverageReporter(DummyRootCfg(tmp_path))
  monkeypatch.setattr(reporter, "_get_cov_tool", lambda: FakeCov())
  monkeypatch.setattr(reporter, "_get_coverview_tool", lambda: None)

  suite_results = [{
    "test_name": "basic",
    "results": SimpleNamespace(results={"coverage": {"raw_paths": ["/tmp/basic.dat"]}}),
  }]

  reporter.generate_unmerged_artifacts(
    suite_results,
    outdir=str(tmp_path),
    suite_name="verif/sandbox/tests.yaml",
  )

  assert captured["raw_path"] == "/tmp/basic.dat"
  assert captured["html_output"] is True
  assert captured["artifact_name"].endswith("tests.yaml__basic")
  assert captured["source_roots"] == [str(tmp_path / "verif" / "sandbox")]


def test_coverage_reporter_merge_passes_source_roots(monkeypatch, tmp_path):
  captured = {}

  class FakeCov:
    def merge(self, raw_paths, outdir, merge_basename, html_output, source_roots, html_outdir=None):
      captured["raw_paths"] = list(raw_paths)
      captured["outdir"] = outdir
      captured["merge_basename"] = merge_basename
      captured["html_output"] = html_output
      captured["source_roots"] = list(source_roots)
      captured["html_outdir"] = html_outdir
      return CoverageMetrics(line=0.5)

  reporter = CoverageReporter(DummyRootCfg(tmp_path))
  monkeypatch.setattr(reporter, "_get_cov_tool", lambda: FakeCov())

  suite_results = [{
    "test_name": "basic",
    "results": SimpleNamespace(results={"coverage": {"raw_paths": ["/tmp/basic.dat"]}}),
  }]

  reporter.merge(
    suite_results,
    outdir=str(tmp_path),
    html_output=True,
    source_roots=[str(tmp_path / "verif" / "sandbox")],
  )

  assert captured["raw_paths"] == ["/tmp/basic.dat"]
  assert captured["html_output"] is True
  assert captured["source_roots"] == [str(tmp_path / "verif" / "sandbox")]


def test_vlog_cov_collect_passes_source_roots_to_metric_parsing(monkeypatch, tmp_path):
  raw_path = tmp_path / "basic.coverage.dat"
  raw_path.write_text("raw coverage")

  captured = []
  cov = VlogCov(simulator_name="verilator", root_cfg=DummyRootCfg(tmp_path))

  monkeypatch.setattr(cov, "_write_lcov", lambda raw_path, lcov_path, source_roots=None: False)

  def _fake_parse(raw_path, metric_name, source_roots=None):
    captured.append((metric_name, list(source_roots)))
    return None

  monkeypatch.setattr(cov, "_parse_verilator_metric", _fake_parse)

  cov.collect(str(raw_path), source_roots=[str(tmp_path / "verif" / "sandbox")])

  assert captured == [
    ("toggle", [str(tmp_path / "verif" / "sandbox")]),
    ("functional", [str(tmp_path / "verif" / "sandbox")]),
  ]


def test_vlog_cov_build_annotate_cwd_keeps_basename_paths_in_suite_root(tmp_path):
  outer_root = tmp_path / "workspace"
  repo_root = outer_root / "repo"
  suite_dir = repo_root / "verif" / "sandbox"
  outer_suite_dir = outer_root / "other" / "verif" / "sandbox"
  suite_dir.mkdir(parents=True)
  outer_suite_dir.mkdir(parents=True)

  expected_tb = suite_dir / "tb_top.sv"
  expected_tb.write_text("module tb_top;\nendmodule\n")
  (outer_suite_dir / "tb_top.sv").write_text("module tb_top;\nendmodule\n")

  raw_path = repo_root / "coverage_merged.dat"
  raw_path.write_text("raw")

  cov = VlogCov(simulator_name="verilator", root_cfg=DummyRootCfg(repo_root))

  def _fake_extract(_raw_path):
    return ["tb_top.sv"]

  cov._extract_raw_source_paths = _fake_extract

  annotate_cwd = cov._build_annotate_cwd(
    str(raw_path),
    str(tmp_path / "annotate"),
    source_roots=[str(suite_dir)],
  )

  copied_tb = tmp_path / "annotate" / "annotate_root" / "lvl_0" / "tb_top.sv"
  assert annotate_cwd.endswith("annotate_root/lvl_0")
  assert copied_tb.read_text() == expected_tb.read_text()


def test_coverview_metric_source_roots_include_suite_root_for_nested_artefacts(tmp_path):
  repo_root = tmp_path / "repo"
  suite_dir = repo_root / "verif" / "sandbox"
  suite_dir.mkdir(parents=True)
  raw_path = suite_dir / "artefacts" / "basic" / "run-0001" / "coverage.dat"
  raw_path.parent.mkdir(parents=True)
  raw_path.write_text("raw")

  packer = CoverviewPacker(cfg=None, project_root=str(repo_root))

  assert packer._metric_source_roots_from_raw_path(str(raw_path)) == [
    str(raw_path.parent.resolve()),
    str(suite_dir.resolve()),
  ]


def test_coverview_rewrite_prefers_suite_root_for_nested_artefacts_duplicate_basenames(tmp_path):
  repo_root = tmp_path / "repo"
  suite_dir = repo_root / "verif" / "sandbox"
  other_dir = repo_root / "verif" / "template"
  suite_dir.mkdir(parents=True)
  other_dir.mkdir(parents=True)
  suite_tb = suite_dir / "tb_top.sv"
  other_tb = other_dir / "tb_top.sv"
  suite_tb.write_text("module tb_top;\nendmodule\n")
  other_tb.write_text("module tb_top;\nendmodule\n")

  raw_path = suite_dir / "artefacts" / "basic" / "run-0001" / "coverage.dat"
  raw_path.parent.mkdir(parents=True)
  raw_path.write_text("raw")
  info_path = tmp_path / "coverage_toggle.info"
  info_path.write_text(f"SF:{suite_tb.name}\nDA:1,1\nend_of_record\n")

  packer = CoverviewPacker(cfg=None, project_root=str(repo_root))
  packer._rewrite_sf_relative_to_project_root(
    str(info_path),
    base_dir=str(raw_path.parent),
    source_roots=packer._metric_source_roots_from_raw_path(str(raw_path)),
  )

  assert info_path.read_text() == "SF:verif/sandbox/tb_top.sv\nDA:1,1\nend_of_record\n"


def test_vlog_sim_post_passes_suite_work_dir_as_coverage_source_root(monkeypatch, tmp_path):
  captured = {}

  class FakeCov:
    def collect(self, raw_path, source_roots=None):
      captured["raw_path"] = raw_path
      captured["source_roots"] = list(source_roots)
      return CoverageMetrics(toggle=0.3)

  class FakePost:
    def __init__(self, **_kwargs):
      pass

    def get_results(self):
      return SimpleNamespace(results={"result": "PASS", "desc": "(nwrn=  0)"})

  sim = vlog_sim_module.VlogSim.__new__(vlog_sim_module.VlogSim)
  sim.test_cfg = SimpleNamespace(uvm=None)
  sim.test_name = "basic"
  sim.root_cfg = DummyRootCfg(tmp_path)
  sim.run_id = None
  sim.vlog_post = None
  sim.suite_work_dir = str(tmp_path / "verif" / "sandbox")
  sim._coverage_enabled = lambda: True
  sim._get_simulator_family = lambda: "verilator"
  sim._get_cov_abspath = lambda run_id=None: str(tmp_path / "artefacts" / "basic" / "coverage.dat")
  sim._get_log_path = lambda run_id=None: str(tmp_path / "artefacts" / "basic" / "test.log")

  monkeypatch.setattr(vlog_sim_module, "VlogPost", FakePost)
  monkeypatch.setattr(vlog_sim_module, "VlogCov", lambda **_kwargs: FakeCov())
  (tmp_path / "verif" / "sandbox").mkdir(parents=True, exist_ok=True)

  results = sim.post()

  assert captured["raw_path"].endswith("coverage.dat")
  assert captured["source_roots"] == [str(tmp_path / "verif" / "sandbox")]
  assert results.results["coverage"]["toggle"] == 0.3
