"""
Unit tests for the structured coverage model and its artefact manifest (#399).

The fixtures are captured Verilator record shapes written inline rather than
binary blobs: `coverage.dat` is a text format, so the exact bytes a test needs
are readable in the test that needs them.
"""

import json
import os

import pytest

from rtl_buddy.cov.manifest import (
    MANIFEST_FILENAME,
    build_manifest,
    discover_manifests,
    load_manifest,
    project_root_for,
    resolve,
    write_manifest,
)
from rtl_buddy.cov.model import (
    MODEL_SCHEMA_VERSION,
    TestArtefacts,
    build_model,
    cover_records,
    load_model,
    write_model,
)


def _dat_record(*, file, line, type_, name, module="blk", col=1, hits=1):
    keys = [
        ("f", file),
        ("l", str(line)),
        ("n", str(col)),
        ("t", type_),
        ("page", f"v_{type_}/{module}"),
        ("o", name),
        ("h", f"tb_top.{module}.{name}"),
    ]
    blob = "".join(f"\x01{k}\x02{v}" for k, v in keys)
    return f"C '{blob}' {hits}\n"


def _project(tmp_path):
    root = tmp_path / "repo"
    suite = root / "verif" / "blk"
    (suite / "artefacts" / "basic").mkdir(parents=True)
    (suite / "artefacts" / "random").mkdir(parents=True)
    src = root / "design" / "blk.sv"
    src.parent.mkdir(parents=True)
    src.write_text("module blk;\n  logic q;\nendmodule\n")
    return root, suite


def _write_dat(path, records):
    path.write_text("# SystemC::Coverage-3\n" + "".join(records), encoding="utf-8")
    return str(path)


def _two_test_model(tmp_path):
    root, suite = _project(tmp_path)
    basic = _write_dat(
        suite / "artefacts" / "basic" / "coverage.dat",
        [
            _dat_record(file="../../../design/blk.sv", line=1, type_="line", name=""),
            _dat_record(
                file="../../../design/blk.sv", line=2, type_="line", name="", hits=0
            ),
            _dat_record(
                file="../../../design/blk.sv", line=2, type_="toggle", name="q[0]"
            ),
            _dat_record(
                file="../../../design/blk.sv",
                line=2,
                type_="toggle",
                name="q[1]",
                hits=0,
            ),
            _dat_record(
                file="../../../design/blk.sv",
                line=3,
                type_="user",
                name="BLK_WRITE",
                hits=5,
            ),
        ],
    )
    random = _write_dat(
        suite / "artefacts" / "random" / "coverage.dat",
        [
            _dat_record(
                file="../../../design/blk.sv", line=2, type_="line", name="", hits=4
            ),
            _dat_record(
                file="../../../design/blk.sv",
                line=2,
                type_="toggle",
                name="q[1]",
                hits=2,
            ),
        ],
    )
    tests = [
        TestArtefacts(
            name="basic",
            raw=basic,
            suite="verif/blk/tests.yaml",
            source_roots=(str(suite / "artefacts" / "basic"), str(suite)),
        ),
        TestArtefacts(
            name="random",
            raw=random,
            suite="verif/blk/tests.yaml",
            source_roots=(str(suite / "artefacts" / "random"), str(suite)),
        ),
    ]
    return root, suite, build_model(tests, project_root=root, simulator="verilator")


def test_model_keys_files_by_project_relative_path(tmp_path):
    _root, _suite, model = _two_test_model(tmp_path)

    assert model["schema_version"] == MODEL_SCHEMA_VERSION
    assert [row["path"] for row in model["files"]] == ["design/blk.sv"]


def test_line_points_fold_across_tests_and_carry_attribution(tmp_path):
    _root, _suite, model = _two_test_model(tmp_path)
    (file_row,) = model["files"]

    assert file_row["line"] == [
        {"line": 1, "hits": 1, "tests": {"basic": 1}},
        {"line": 2, "hits": 4, "tests": {"basic": 0, "random": 4}},
    ]
    assert file_row["totals"]["line"] == {"found": 2, "hit": 2, "ratio": 1.0}


def test_toggle_detail_survives_per_signal(tmp_path):
    _root, _suite, model = _two_test_model(tmp_path)
    (file_row,) = model["files"]

    assert [(p["name"], p["hits"]) for p in file_row["toggle"]] == [
        ("q[0]", 1),
        ("q[1]", 2),
    ]
    assert file_row["toggle"][1]["tests"] == {"basic": 0, "random": 2}


def test_cover_points_are_reported_in_the_run_level_shape(tmp_path):
    _root, _suite, model = _two_test_model(tmp_path)

    assert cover_records(model) == [
        {
            "name": "BLK_WRITE",
            "file": "design/blk.sv",
            "line": 3,
            "module": "blk",
            "hits": 5,
        }
    ]


def test_modules_index_maps_module_to_its_sources(tmp_path):
    _root, _suite, model = _two_test_model(tmp_path)

    assert model["modules"] == {"blk": ["design/blk.sv"]}
    assert model["counts"] == {"files": 1, "tests": 2, "modules": 1}


def test_per_test_totals_are_that_test_only(tmp_path):
    _root, _suite, model = _two_test_model(tmp_path)
    by_name = {row["name"]: row for row in model["tests"]}

    assert by_name["basic"]["totals"]["line"] == {"found": 2, "hit": 1, "ratio": 0.5}
    assert by_name["random"]["totals"]["line"] == {"found": 1, "hit": 1, "ratio": 1.0}


def test_info_fallback_when_a_test_has_no_raw_database(tmp_path):
    root, suite = _project(tmp_path)
    info = suite / "artefacts" / "basic" / "coverage.info"
    info.write_text(
        "SF:design/blk.sv\nDA:1,3\nDA:2,0\nBRDA:1,0,0,2\nBRDA:1,0,1,-\nend_of_record\n"
    )

    model = build_model(
        [TestArtefacts(name="basic", info=str(info))],
        project_root=root,
        simulator="verilator",
    )
    (file_row,) = model["files"]

    assert file_row["totals"]["line"] == {"found": 2, "hit": 1, "ratio": 0.5}
    assert file_row["totals"]["branch"] == {"found": 2, "hit": 1, "ratio": 0.5}
    assert file_row["toggle"] == []


def test_model_round_trips_through_disk(tmp_path):
    root, suite, model = _two_test_model(tmp_path)
    cov_dir = suite / "cov_dir"

    path = write_model(model, cov_dir)

    assert load_model(path) == model


def test_manifest_paths_are_project_relative_and_keys_stable(tmp_path):
    root, suite, model = _two_test_model(tmp_path)
    cov_dir = suite / "cov_dir"
    model_path = write_model(model, cov_dir)

    manifest = build_manifest(
        project_root=root,
        cov_dir=cov_dir,
        command="regression",
        suite=str(suite / "regression.yaml"),
        builder="verilator",
        simulator_family="verilator",
        merge_mode="raw",
        model_path=model_path,
        totals=model["totals"],
        merged={"info": str(cov_dir / "coverage_merged.info")},
        tests=[
            {
                "name": "basic",
                "raw": str(suite / "artefacts" / "basic" / "coverage.dat"),
            }
        ],
    )

    assert manifest["cov_dir"] == "verif/blk/cov_dir"
    assert manifest["model"] == "verif/blk/cov_dir/coverage-model.json"
    assert manifest["merged"] == {
        "info": "verif/blk/cov_dir/coverage_merged.info",
        "raw": None,
        "desc": None,
        "html_dir": None,
    }
    assert manifest["datasets"] == {
        "line": None,
        "branch": None,
        "toggle": None,
        "expression": None,
    }
    assert manifest["tests"][0]["raw"] == "verif/blk/artefacts/basic/coverage.dat"


def test_manifest_discovery_and_project_root_inference(tmp_path):
    root, suite, model = _two_test_model(tmp_path)
    cov_dir = suite / "cov_dir"
    model_path = write_model(model, cov_dir)
    manifest_path = write_manifest(
        build_manifest(
            project_root=root,
            cov_dir=cov_dir,
            command="test",
            model_path=model_path,
        ),
        cov_dir,
    )

    assert discover_manifests(root) == [manifest_path]
    assert project_root_for(manifest_path) == str(root)
    assert resolve(manifest_path, "verif/blk/cov_dir/coverage-model.json") == str(
        model_path
    )
    assert load_manifest(manifest_path)["schema_version"] == 1
    assert json.loads((cov_dir / MANIFEST_FILENAME).read_text())["command"] == "test"


def _symlink_or_skip(link, target):
    """Link ``link`` at directory ``target``, or skip where it cannot.

    The link is the whole subject of the tests below, so a platform that
    refuses to make one has nothing to assert rather than a failure to
    report.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - POSIX CI
        pytest.skip("platform does not support directory symlinks")


def _bare_manifest(cov_dir, project_root):
    """A manifest with nothing in it but the two paths discovery reads."""
    return write_manifest(
        build_manifest(project_root=project_root, cov_dir=cov_dir, command="test"),
        cov_dir,
    )


def test_discovery_reaches_a_cov_dir_behind_a_symlinked_artefact_dir(tmp_path):
    """``artefacts/`` linked onto scratch storage is an ordinary, documented
    setup, and the default ``cov_dir`` lives inside it — so a walk that did
    not follow the link reported "no coverage found" for a run sitting right
    there (rtl-buddy/rtl_buddy#564)."""
    root = tmp_path / "repo"
    suite = root / "verif" / "blk"
    suite.mkdir(parents=True)
    physical = tmp_path / "scratch" / "artefacts"
    (physical / "cov_dir").mkdir(parents=True)
    _symlink_or_skip(suite / "artefacts", physical)
    _bare_manifest(suite / "artefacts" / "cov_dir", root)

    assert discover_manifests(root) == [
        str(suite / "artefacts" / "cov_dir" / MANIFEST_FILENAME)
    ]


def test_discovery_terminates_on_a_symlink_loop(tmp_path):
    """Following links costs a loop risk, and either guard alone stops this
    one: the boundary refuses a link whose realpath is an ancestor of the
    project, and behind it a directory is admitted once by its real path. The
    run the link circles is still reported exactly once."""
    root, suite = _project(tmp_path)
    cov_dir = suite / "artefacts" / "cov_dir"
    manifest_path = _bare_manifest(cov_dir, root)
    _symlink_or_skip(cov_dir / "loop", root)

    assert discover_manifests(root) == [manifest_path]


def test_discovery_reports_a_cov_dir_reachable_two_ways_once(tmp_path):
    """An ``artefacts/`` link whose target is itself inside the project puts
    one ``cov_dir`` on two paths. Admitting a directory once by its real path
    keeps the same run from being reported — and reported on — twice."""
    root = tmp_path / "repo"
    store = root / "scratch_artefacts"
    (store / "cov_dir").mkdir(parents=True)
    suite = root / "verif" / "blk"
    suite.mkdir(parents=True)
    _symlink_or_skip(suite / "artefacts", store)
    _bare_manifest(store / "cov_dir", root)

    found = discover_manifests(root)

    assert len(found) == 1
    assert os.path.samefile(found[0], store / "cov_dir" / MANIFEST_FILENAME)


def test_discovery_does_not_enter_a_symlink_outside_the_artefact_layout(tmp_path):
    """The other half of the boundary, and the reason the coverage walk asks
    the same question the physical one does (its #560 round-10 review finding):
    following *every* link made a ``vendor/`` link — or one to ``$HOME`` — part
    of the project's walk, so an unrelated tree was scanned and its coverage
    reported as this project's own run."""
    root, suite = _project(tmp_path)
    manifest_path = _bare_manifest(suite / "artefacts" / "cov_dir", root)
    unrelated = tmp_path / "elsewhere"
    (unrelated / "verif" / "blk" / "artefacts").mkdir(parents=True)
    _bare_manifest(unrelated / "cov_dir", unrelated)
    _bare_manifest(unrelated / "verif" / "blk" / "artefacts" / "cov_dir", unrelated)
    _symlink_or_skip(root / "vendor", unrelated)

    # Neither the manifest at the link's top nor the one buried inside it:
    # the link is not entered at all, so nothing under it is even scanned.
    assert discover_manifests(root) == [manifest_path]
