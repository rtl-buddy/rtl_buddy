"""
Unit tests for the consolidated source-path resolver (#399).

The resolver replaces three divergent copies (`vlog_cov._normalize_lcov_paths`,
`vlog_cov._resolve_source_path`, `coverview._rewrite_sf_relative_to_project_root`).
`tests/test_coverage_paths.py` guards the three call sites' behaviour; these
cases pin the resolver's own contract, the hint ordering especially.
"""

from rtl_buddy.cov.source_paths import SourcePathResolver


def _repo(tmp_path):
    repo_root = tmp_path / "repo"
    suite_dir = repo_root / "verif" / "sandbox"
    run_dir = suite_dir / "artefacts" / "basic" / "run-0001"
    run_dir.mkdir(parents=True)
    return repo_root, suite_dir, run_dir


def test_hint_beats_project_root_for_duplicate_basenames(tmp_path):
    repo_root, suite_dir, run_dir = _repo(tmp_path)
    (repo_root / "verif" / "template").mkdir(parents=True)
    (suite_dir / "tb_top.sv").write_text("module tb_top;\nendmodule\n")
    (repo_root / "verif" / "template" / "tb_top.sv").write_text("module tb_top;\n")

    resolver = SourcePathResolver(
        repo_root, base_dir=run_dir, source_roots=[run_dir, suite_dir]
    )

    assert resolver.resolve("tb_top.sv").project_relative == "verif/sandbox/tb_top.sv"


def test_leading_relative_segments_are_trimmed_onto_the_project_root(tmp_path):
    repo_root, _suite_dir, run_dir = _repo(tmp_path)
    source = repo_root / "design" / "blk.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module blk;\nendmodule\n")

    resolver = SourcePathResolver(repo_root, base_dir=run_dir)
    resolution = resolver.resolve("../../../../design/blk.sv")

    assert resolution.found is True
    assert resolution.project_relative == "design/blk.sv"


def test_generated_trees_never_win_the_basename_search(tmp_path):
    repo_root, suite_dir, run_dir = _repo(tmp_path)
    real = suite_dir / "rtl" / "blk.sv"
    real.parent.mkdir(parents=True)
    real.write_text("module blk;\nendmodule\n")
    # An annotate scratch copy of the same file, under a generated tree.
    stale = suite_dir / "artefacts" / "coverage_annotated" / "blk.sv"
    stale.parent.mkdir(parents=True)
    stale.write_text("module blk;\nendmodule\n")

    resolver = SourcePathResolver(repo_root, base_dir=run_dir, source_roots=[suite_dir])

    assert resolver.resolve("blk.sv").path == real.resolve()


def test_bare_basename_ignores_a_decoy_beside_the_raw_database(tmp_path):
    """The direct-candidate stage must not run for a bare basename.

    For a raw database `base_dir` is the per-run artefact directory, so
    `<base dir>/<name>` would hand a stale copy sitting next to
    `coverage.dat` the win before the generated-tree filter ever looked
    at it — the exact thing the module docstring forbids.
    """
    repo_root, suite_dir, run_dir = _repo(tmp_path)
    real = suite_dir / "rtl" / "tb_top.sv"
    real.parent.mkdir(parents=True)
    real.write_text("module tb_top;\nendmodule\n")
    # The decoy: an older copy left beside this run's `coverage.dat`.
    (run_dir / "coverage.dat").write_text("# SystemC::Coverage-3\n")
    (run_dir / "tb_top.sv").write_text("module tb_top;  // stale\nendmodule\n")

    resolver = SourcePathResolver(repo_root, base_dir=run_dir, source_roots=[suite_dir])
    resolution = resolver.resolve("tb_top.sv")

    assert resolution.found is True
    assert resolution.path == real.resolve()
    assert resolution.project_relative == "verif/sandbox/rtl/tb_top.sv"


def test_missing_file_keeps_the_base_anchored_reading_inside_the_project(tmp_path):
    repo_root, _suite_dir, run_dir = _repo(tmp_path)

    resolution = SourcePathResolver(repo_root, base_dir=run_dir).resolve(
        "../../gone/missing.sv"
    )

    assert resolution.found is False
    assert resolution.project_relative == "verif/sandbox/artefacts/gone/missing.sv"


def test_missing_file_outside_the_project_falls_back_to_the_stripped_reading(tmp_path):
    repo_root, _suite_dir, run_dir = _repo(tmp_path)

    # Six levels up from the run directory lands above the project; the only
    # useful reading left is the path with its `..` segments dropped.
    resolution = SourcePathResolver(repo_root, base_dir=run_dir).resolve(
        "../../../../../../gone/missing.sv"
    )

    assert resolution.found is False
    assert resolution.project_relative == "gone/missing.sv"


def test_file_outside_the_project_is_left_for_the_caller_to_skip(tmp_path):
    repo_root, _suite_dir, run_dir = _repo(tmp_path)
    outside = tmp_path / "vendor" / "ip.sv"
    outside.parent.mkdir(parents=True)
    outside.write_text("module ip;\nendmodule\n")

    resolution = SourcePathResolver(repo_root, base_dir=run_dir).resolve(str(outside))

    assert resolution.found is True
    assert resolution.project_relative is None


def test_rewrite_info_relative_leaves_unrelatable_records_alone(tmp_path):
    repo_root, suite_dir, run_dir = _repo(tmp_path)
    (suite_dir / "tb_top.sv").write_text("module tb_top;\nendmodule\n")
    outside = tmp_path / "vendor" / "ip.sv"
    outside.parent.mkdir(parents=True)
    outside.write_text("module ip;\nendmodule\n")

    info = run_dir / "coverage.info"
    info.write_text(f"SF:tb_top.sv\nDA:1,1\nend_of_record\nSF:{outside}\nDA:2,0\n")

    SourcePathResolver(
        repo_root, base_dir=run_dir, source_roots=[run_dir, suite_dir]
    ).rewrite_info(info, relative=True)

    assert info.read_text() == (
        f"SF:verif/sandbox/tb_top.sv\nDA:1,1\nend_of_record\nSF:{outside}\nDA:2,0\n"
    )


def test_rewrite_info_absolute_resolves_every_record(tmp_path):
    repo_root, suite_dir, run_dir = _repo(tmp_path)
    tb_top = suite_dir / "tb_top.sv"
    tb_top.write_text("module tb_top;\nendmodule\n")

    info = run_dir / "coverage.info"
    info.write_text("SF:tb_top.sv\nDA:1,1\nend_of_record\n")

    SourcePathResolver(
        repo_root, base_dir=run_dir, source_roots=[run_dir, suite_dir]
    ).rewrite_info(info, relative=False)

    assert info.read_text() == f"SF:{tb_top.resolve()}\nDA:1,1\nend_of_record\n"


def test_rewrite_desc_rewrites_sn_records(tmp_path):
    repo_root, suite_dir, run_dir = _repo(tmp_path)
    (suite_dir / "tb_top.sv").write_text("module tb_top;\nendmodule\n")

    desc = run_dir / "coverage.desc"
    desc.write_text("SN:tb_top.sv\nTEST:1,basic\nend_of_record\n")

    SourcePathResolver(
        repo_root, base_dir=run_dir, source_roots=[run_dir, suite_dir]
    ).rewrite_desc(desc)

    assert desc.read_text() == (
        "SN:verif/sandbox/tb_top.sv\nTEST:1,basic\nend_of_record\n"
    )
