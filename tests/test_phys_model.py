"""
Unit tests for the structured physical model and its manifest (#558).

The fixtures are captured tool output written inline rather than files on
disk: both formats are text, so the exact bytes a test needs are readable
in the test that needs them. The `stat -json` dump is trimmed to the keys
the reader looks at, with Yosys' RTLIL backslash prefix and its
inconsistent spacing preserved — those are the parts that break.
"""

import json

import pytest

from rtl_buddy.phys.manifest import (
    MANIFEST_FILENAME,
    POWER_KEYS,
    SYNTH_KEYS,
    build_manifest,
    discover_manifests,
    load_manifest,
    merge_manifest,
    project_root_for,
    resolve,
    write_manifest,
)
from rtl_buddy.phys.model import (
    MODEL_SCHEMA_VERSION,
    build_power_model,
    build_synth_model,
    load_model,
    load_model_or_none,
    merge_model,
    write_model,
)
from rtl_buddy.phys.publish import publish_power, publish_synth
from rtl_buddy.phys.reports import (
    parse_instance_cells,
    parse_instance_power,
    parse_stat_json,
)


# ---------------------------------------------------------------------------
# Fixtures — captured from yosys 0.64 / OpenROAD 26Q2 on a two-module design
# ---------------------------------------------------------------------------


STAT_JSON = """{
   "creator": "Yosys 0.64+193",
   "invocation": "stat -json -liberty Nangate45_typ.lib ",
   "modules": {
      "\\\\top": {
         "num_wires":         6,
         "num_cells":         1,
         "num_submodules":       1,
         "area":              5.586000,
         "sequential_area":    4.522000,
         "num_cells_by_type": {"DFF_X1": 1, "sub": 1}
      },
      "\\\\sub": {
         "num_wires":         6,
         "num_cells":         1,
         "num_submodules":       0,
         "area":              1.064000,
         "sequential_area":    0.000000,
         "num_cells_by_type": {"AND2_X1": 1}
      }
   },
      "design": {
         "num_cells":         2,
         "area":              5.586000
      }
}
"""

STAT_JSON_NO_LIBERTY = """{
   "modules": {
      "\\\\top": {"num_cells": 4},
      "\\\\sub": {"num_cells": 1}
   },
      "design": {"num_cells": 5}
}
"""

INSTANCE_RPT = """   Internal  Switching    Leakage      Total
      Power      Power      Power      Power (Watts)
--------------------------------------------
   2.28e-06   6.75e-08   7.91e-08   2.42e-06 u_sub/_64_
   1.52e-07   7.79e-08   3.62e-08   2.66e-07 _18_
   7.14e-08   0.00e+00   1.74e-08   8.88e-08 u_sub/_45_
"""

INSTANCE_CELLS = """_18_ XOR2_X1
u_sub/_45_ NAND2_X1
u_sub/_64_ DFF_X1
"""


def _project(tmp_path):
    """A project tree with a synth run's artefact directory in it."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    artefacts = root / "verif" / "demo" / "artefacts" / "demo_synth"
    artefacts.mkdir(parents=True)
    return root, artefacts


# ---------------------------------------------------------------------------
# reports — the tool-output readers
# ---------------------------------------------------------------------------


def test_stat_json_rows_strip_the_rtlil_name_prefix():
    assert parse_stat_json(STAT_JSON) == [
        {"module": "sub", "cell_count": 1, "area_um2": 1.064},
        {"module": "top", "cell_count": 1, "area_um2": 5.586},
    ]


def test_stat_json_without_a_liberty_still_yields_cell_counts():
    """`stat -json` drops the `area` field when it was given no Liberty; the
    rows are still worth having, with a null area."""
    assert parse_stat_json(STAT_JSON_NO_LIBERTY) == [
        {"module": "sub", "cell_count": 1, "area_um2": None},
        {"module": "top", "cell_count": 4, "area_um2": None},
    ]


def test_stat_json_the_design_rollup_is_not_a_module_row():
    """The sibling `design` block is the whole-design total, and the model
    takes its totals from the log scrape instead — precisely so the two can
    be compared."""
    assert "design" not in [row["module"] for row in parse_stat_json(STAT_JSON)]


def test_stat_json_unreadable_input_yields_no_rows():
    assert parse_stat_json("not json at all") == []
    assert parse_stat_json('{"modules": "wrong shape"}') == []


def test_instance_power_rows_convert_watts_to_microwatts():
    cells = parse_instance_cells(INSTANCE_CELLS)
    rows = parse_instance_power(INSTANCE_RPT, cells)

    assert [row["instance_path"] for row in rows] == [
        "_18_",
        "u_sub/_45_",
        "u_sub/_64_",
    ]
    assert rows[2] == pytest.approx(
        {
            "instance_path": "u_sub/_64_",
            "module": "DFF_X1",
            "leakage_uw": 0.0791,
            "internal_uw": 2.28,
            "switching_uw": 0.0675,
            "total_uw": 2.42,
        }
    )


def test_instance_power_without_the_cell_sidecar_keeps_the_numbers():
    """The sidecar is the half more likely to be missing, and the powers are
    the point; a row without it loses its module column and nothing else."""
    rows = parse_instance_power(INSTANCE_RPT)

    assert [row["module"] for row in rows] == [None, None, None]
    assert rows[0]["total_uw"] == pytest.approx(0.266)


def test_instance_power_skips_the_header_and_rule_lines():
    assert len(parse_instance_power(INSTANCE_RPT)) == 3


def test_instance_cells_ignores_malformed_lines():
    assert parse_instance_cells("a A\nnot-a-pair\nb B C\n") == {"a": "A"}


# ---------------------------------------------------------------------------
# model — construction, the stable-keys rule, and the merge
# ---------------------------------------------------------------------------


def test_a_synth_only_model_leaves_the_power_half_null():
    model = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=5.586,
        gate_count=2,
    )

    assert model["schema_version"] == MODEL_SCHEMA_VERSION
    assert model["design"] == {"top": "demo_top"}
    assert model["units"] == {"area": "um2", "power": "uW"}
    assert model["instances"] is None
    assert [row["module"] for row in model["modules"]] == ["sub", "top"]
    assert model["totals"] == {
        "area_um2": 5.586,
        "cell_count": 2,
        "internal_uw": None,
        "switching_uw": None,
        "leakage_uw": None,
        "total_uw": None,
    }


def test_a_power_only_model_leaves_the_synth_half_null():
    model = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        internal_w=2.53e-05,
        switching_w=1.52e-06,
        leakage_w=1.41e-06,
        total_w=2.83e-05,
    )

    assert model["modules"] is None
    assert model["totals"]["area_um2"] is None
    assert model["totals"]["total_uw"] == pytest.approx(28.3)
    assert model["totals"]["leakage_uw"] == pytest.approx(1.41)


def test_an_unreadable_breakdown_is_null_rather_than_absent():
    """Stable keys: a run that produced no rows still writes the key, so a
    consumer can tell "not produced" from "produced and empty"."""
    model = build_synth_model(top="demo_top", modules=None, gate_count=7)

    assert "modules" in model and model["modules"] is None
    assert model["totals"]["cell_count"] == 7


def test_merging_a_power_run_onto_a_synth_model_keeps_both_halves():
    synth = build_synth_model(
        top="demo_top", modules=parse_stat_json(STAT_JSON), area_um2=5.586, gate_count=2
    )
    power = build_power_model(
        top="demo_top", instances=parse_instance_power(INSTANCE_RPT), total_w=2.83e-05
    )

    merged = merge_model(synth, power)

    assert len(merged["modules"]) == 2
    assert len(merged["instances"]) == 3
    assert merged["totals"]["area_um2"] == 5.586
    assert merged["totals"]["total_uw"] == pytest.approx(28.3)


def test_merging_is_order_independent():
    synth = build_synth_model(top="demo_top", modules=[], area_um2=1.0, gate_count=2)
    power = build_power_model(top="demo_top", instances=[], total_w=2.0e-06)

    forwards = merge_model(synth, power)
    backwards = merge_model(power, synth)

    assert forwards["totals"] == backwards["totals"]
    assert forwards["modules"] == backwards["modules"] == []
    assert forwards["instances"] == backwards["instances"] == []


def test_a_rerun_of_the_same_half_replaces_its_rows():
    """The merge must not make a half grow-only: a module the design no
    longer has must disappear on the next synthesis."""
    first = build_synth_model(
        top="demo_top", modules=[{"module": "gone", "cell_count": 1, "area_um2": 1.0}]
    )
    second = build_synth_model(
        top="demo_top", modules=[{"module": "kept", "cell_count": 2, "area_um2": 2.0}]
    )

    merged = merge_model(first, second)

    assert [row["module"] for row in merged["modules"]] == ["kept"]


def test_a_model_for_a_different_top_is_replaced_not_merged():
    """Artefact directories are keyed on a run's *name*, and names are not
    unique across designs."""
    other = build_synth_model(top="other_top", modules=[], area_um2=99.0)
    power = build_power_model(top="demo_top", instances=[], total_w=1.0e-06)

    merged = merge_model(other, power)

    assert merged["modules"] is None
    assert merged["totals"]["area_um2"] is None


def test_merging_ignores_a_document_from_an_incompatible_schema():
    stale = build_synth_model(top="demo_top", modules=[], area_um2=1.0)
    stale["schema_version"] = MODEL_SCHEMA_VERSION + 1

    assert merge_model(stale, build_power_model(top="demo_top"))["modules"] is None


def test_model_round_trips_through_disk(tmp_path):
    _root, artefacts = _project(tmp_path)
    model = build_synth_model(
        top="demo_top", modules=parse_stat_json(STAT_JSON), gate_count=2
    )

    path = write_model(model, artefacts)

    assert load_model(path) == model
    assert load_model_or_none(artefacts) == model


def test_load_model_or_none_swallows_a_truncated_document(tmp_path):
    """A model half-written by a killed run is "nothing to merge", never an
    error propagated into a flow that has otherwise succeeded."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "phys-model.json").write_text('{"schema_version": 1')

    assert load_model_or_none(artefacts) is None
    assert load_model_or_none(tmp_path / "nowhere") is None


# ---------------------------------------------------------------------------
# manifest — paths, stable keys, discovery
# ---------------------------------------------------------------------------


def _synth_manifest(root, artefacts, model_path, totals=None):
    return build_manifest(
        project_root=root,
        phys_dir=artefacts,
        command="synth",
        run="demo_synth",
        top="demo_top",
        model_path=model_path,
        totals=totals,
        synth={
            "backend": "yosys",
            "run": "demo_synth",
            "stats": str(artefacts / "synth_stat.json"),
            "netlist": str(artefacts / "synth_netlist.v"),
            "log": str(artefacts / "synth.log"),
        },
    )


def test_manifest_paths_are_project_relative_and_keys_stable(tmp_path):
    root, artefacts = _project(tmp_path)
    model_path = write_model(build_synth_model(top="demo_top"), artefacts)

    manifest = _synth_manifest(root, artefacts, model_path)

    assert manifest["phys_dir"] == "verif/demo/artefacts/demo_synth"
    assert manifest["model"] == "verif/demo/artefacts/demo_synth/phys-model.json"
    assert manifest["synth"] == {
        "backend": "yosys",
        "run": "demo_synth",
        "stats": "verif/demo/artefacts/demo_synth/synth_stat.json",
        "netlist": "verif/demo/artefacts/demo_synth/synth_netlist.v",
        "log": "verif/demo/artefacts/demo_synth/synth.log",
    }
    # The half this run did not produce: present, and null throughout.
    assert manifest["power"] == {key: None for key in POWER_KEYS}
    assert set(manifest["synth"]) == set(SYNTH_KEYS)


def test_manifest_keeps_a_path_outside_the_project_verbatim(tmp_path):
    root, artefacts = _project(tmp_path)
    scratch = tmp_path / "scratch" / "synth.log"

    manifest = build_manifest(
        project_root=root,
        phys_dir=artefacts,
        command="synth",
        synth={"backend": "yosys", "log": str(scratch)},
    )

    assert manifest["synth"]["log"] == str(scratch)


def test_manifest_merge_carries_the_other_halfs_block_forward(tmp_path):
    root, artefacts = _project(tmp_path)
    synth = _synth_manifest(root, artefacts, None, totals={"area_um2": 5.586})
    power = build_manifest(
        project_root=root,
        phys_dir=artefacts,
        command="power",
        top="demo_top",
        totals={"total_uw": 28.3},
        power={"backend": "openroad", "report": str(artefacts / "power.rpt")},
    )

    merged = merge_manifest(synth, power)

    assert merged["command"] == "power"
    assert merged["synth"]["backend"] == "yosys"
    assert merged["power"]["backend"] == "openroad"
    assert merged["totals"] == {"total_uw": 28.3, "area_um2": 5.586}


def test_manifest_merge_keeps_a_reproduced_halfs_totals_as_written(tmp_path):
    root, artefacts = _project(tmp_path)
    old = _synth_manifest(
        root, artefacts, None, totals={"area_um2": 5.586, "cell_count": 31}
    )
    rerun = _synth_manifest(
        root, artefacts, None, totals={"area_um2": None, "cell_count": 9}
    )

    merged = merge_manifest(old, rerun)

    # The rerun re-produced the synth half, so its totals stand as
    # written: a scrape that failed this time is null, not last run's
    # number — the manifest mirror of the model's shrinking rerun rule.
    assert merged["totals"] == {"area_um2": None, "cell_count": 9}


def test_manifest_merge_ignores_a_manifest_for_a_different_top(tmp_path):
    root, artefacts = _project(tmp_path)
    other = _synth_manifest(root, artefacts, None)
    other["top"] = "other_top"
    power = build_manifest(
        project_root=root,
        phys_dir=artefacts,
        command="power",
        top="demo_top",
        power={"backend": "openroad"},
    )

    assert merge_manifest(other, power)["synth"]["backend"] is None


def test_manifest_discovery_and_project_root_inference(tmp_path):
    root, artefacts = _project(tmp_path)
    model_path = write_model(build_synth_model(top="demo_top"), artefacts)
    manifest_path = write_manifest(
        _synth_manifest(root, artefacts, model_path), artefacts
    )

    assert discover_manifests(root) == [manifest_path]
    assert project_root_for(manifest_path) == str(root)
    assert resolve(
        manifest_path, "verif/demo/artefacts/demo_synth/phys-model.json"
    ) == str(model_path)
    assert load_manifest(manifest_path)["schema_version"] == 1
    assert json.loads((artefacts / MANIFEST_FILENAME).read_text())["command"] == "synth"


def test_discovery_does_not_pick_up_a_coverage_manifest(tmp_path):
    """The physical manifest carries its own filename precisely because it
    has no `cov_dir` to be found by; a coverage manifest in the same tree
    must not be mistaken for one."""
    root, artefacts = _project(tmp_path)
    cov_dir = root / "artefacts" / "cov_dir"
    cov_dir.mkdir(parents=True)
    (cov_dir / "manifest.json").write_text('{"schema_version": 1}')
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)

    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


# ---------------------------------------------------------------------------
# publish — the entry point the backends call
# ---------------------------------------------------------------------------


def test_publish_writes_both_documents_and_reports_the_row_count(tmp_path):
    _root, artefacts = _project(tmp_path)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo_synth",
        stats_path=artefacts / "synth_stat.json",
        area_um2=5.586,
        gate_count=2,
    )

    assert published["error"] is None
    assert published["rows"] == 2
    assert load_model(published["model"])["modules"][0]["module"] == "sub"
    assert load_manifest(published["manifest"])["synth"]["backend"] == "yosys"


def test_publish_without_the_stats_file_still_writes_the_totals(tmp_path):
    """The resilience rule: a synthesis whose `stat -json` never landed has
    still succeeded, and the numbers it did parse are still worth recording."""
    _root, artefacts = _project(tmp_path)

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        stats_path=artefacts / "synth_stat.json",
        area_um2=5.586,
        gate_count=2,
    )

    assert published["error"] is None
    assert published["rows"] is None  # the caller's cue to warn
    model = load_model(published["model"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] == 5.586


def test_publish_reports_an_unwritable_directory_rather_than_raising(tmp_path):
    """Nothing in the publish path may raise into a flow that has already
    produced its product."""
    _root, artefacts = _project(tmp_path)
    blocked = artefacts / "synth_netlist.v"  # a file where a directory must be
    blocked.write_text("module demo_top(); endmodule\n")

    published = publish_synth(artefact_dir=blocked, top="demo_top", backend="yosys")

    assert published["error"] is not None
    assert published["model"] is None and published["manifest"] is None


def test_publishing_a_power_run_over_a_synth_run_merges_on_disk(tmp_path):
    """The end-to-end merge: two commands into one artefact directory add up
    to one document describing both halves."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    (artefacts / "power_instances.cells").write_text(INSTANCE_CELLS)

    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        area_um2=5.586,
        gate_count=2,
    )
    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="synth",
        report_path=artefacts / "power.rpt",
        instances_path=artefacts / "power_instances.rpt",
        cells_path=artefacts / "power_instances.cells",
        total_w=2.83e-05,
    )

    model = load_model(published["model"])
    assert [row["module"] for row in model["modules"]] == ["sub", "top"]
    assert [row["module"] for row in model["instances"]] == [
        "XOR2_X1",
        "NAND2_X1",
        "DFF_X1",
    ]
    assert model["totals"]["area_um2"] == 5.586
    assert model["totals"]["total_uw"] == pytest.approx(28.3)

    manifest = load_manifest(published["manifest"])
    assert manifest["synth"]["backend"] == "yosys"
    assert manifest["power"]["instances"].endswith("power_instances.rpt")
