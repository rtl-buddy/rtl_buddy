"""Unit tests for the structured physical model and its manifest.

Fixtures are captured tool output written inline. The `stat -json` dump is trimmed to the keys the reader uses, keeping Yosys' RTLIL backslash prefix and inconsistent spacing.
"""

import contextlib
import hashlib
import json
import logging
import os
import threading
import time

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
    project_root_for_dir,
    resolve,
    write_manifest,
)
from rtl_buddy.phys.model import (
    BLOCK_PROVENANCE_KEYS,
    MODEL_FILENAME,
    MODEL_SCHEMA_VERSION,
    build_power_model,
    build_synth_model,
    load_model,
    load_model_or_none,
    merge_model,
    provenance_of,
    write_model,
)
from rtl_buddy.phys.provenance import (
    activity_block,
    activity_label,
    experiment_for,
    normalise_activity,
    options_digest,
    trace_test,
)
from rtl_buddy.phys.publish import invalidate_half, publish_power, publish_synth
from rtl_buddy.phys.reports import (
    parse_instance_cells,
    parse_instance_power,
    parse_stat_json,
)


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

NETLIST = """module demo_top (clk);
  input clk;
  DFF_X1 _64_ (.CK(clk));
endmodule
"""

NETLIST_AFTER_AN_RTL_EDIT = NETLIST.replace("DFF_X1", "DFF_X2")

# What a flow that measured `NETLIST` records in its provenance; both halves must record it for either to inherit the other's rows.
NETLIST_SHA256 = hashlib.sha256(NETLIST.encode()).hexdigest()

INSTANCE_CELLS = """_18_ XOR2_X1
u_sub/_45_ NAND2_X1
u_sub/_64_ DFF_X1
"""


def _project(tmp_path):
    """Return a project tree with a synth run's artefact directory."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    artefacts = root / "verif" / "demo" / "artefacts" / "demo_synth"
    artefacts.mkdir(parents=True)
    return root, artefacts


def test_stat_json_rows_strip_the_rtlil_name_prefix():
    assert parse_stat_json(STAT_JSON) == [
        {"module": "sub", "cell_count": 1, "area_um2": 1.064},
        {"module": "top", "cell_count": 1, "area_um2": 5.586},
    ]


def test_stat_json_without_a_liberty_still_yields_cell_counts():
    """`stat -json` drops `area` when given no Liberty; rows are kept with a null area."""
    assert parse_stat_json(STAT_JSON_NO_LIBERTY) == [
        {"module": "sub", "cell_count": 1, "area_um2": None},
        {"module": "top", "cell_count": 4, "area_um2": None},
    ]


def test_stat_json_the_design_rollup_is_not_a_module_row():
    """The sibling `design` block is the whole-design total; the model takes totals from the log scrape so the two can be compared."""
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
    """Without the cell sidecar a row loses only its module column."""
    rows = parse_instance_power(INSTANCE_RPT)

    assert [row["module"] for row in rows] == [None, None, None]
    assert rows[0]["total_uw"] == pytest.approx(0.266)


def test_instance_power_skips_the_header_and_rule_lines():
    assert len(parse_instance_power(INSTANCE_RPT)) == 3


def test_instance_cells_ignores_malformed_lines():
    assert parse_instance_cells("a A\nnot-a-pair\nb B C\n") == {"a": "A"}


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
    """A run that produced no rows still writes the key, so "not produced" (null) differs from "produced and empty"."""
    model = build_synth_model(top="demo_top", modules=None, gate_count=7)

    assert "modules" in model and model["modules"] is None
    assert model["totals"]["cell_count"] == 7


def test_merging_a_power_run_onto_a_synth_model_keeps_both_halves():
    synth = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=5.586,
        gate_count=2,
        netlist_sha256="deadbeef",
    )
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        netlist_sha256="deadbeef",
    )

    merged = merge_model(synth, power, own_half="instances")

    assert len(merged["modules"]) == 2
    assert len(merged["instances"]) == 3
    assert merged["totals"]["area_um2"] == 5.586
    assert merged["totals"]["total_uw"] == pytest.approx(28.3)


def test_merging_is_order_independent():
    """A matched pair (the power run read the netlist the synthesis wrote, per the recorded hash) merges in either order."""
    synth = build_synth_model(
        top="demo_top", modules=[], area_um2=1.0, gate_count=2, netlist_sha256="abc"
    )
    power = build_power_model(
        top="demo_top", instances=[], total_w=2.0e-06, netlist_sha256="abc"
    )

    forwards = merge_model(synth, power, own_half="instances")
    backwards = merge_model(power, synth, own_half="modules")

    assert forwards["totals"] == backwards["totals"]
    assert forwards["modules"] == backwards["modules"] == []
    assert forwards["instances"] == backwards["instances"] == []


def test_a_rerun_of_the_same_half_replaces_its_rows():
    """A rerun replaces its own half's rows, so a module the design no longer has disappears."""
    first = build_synth_model(
        top="demo_top", modules=[{"module": "gone", "cell_count": 1, "area_um2": 1.0}]
    )
    second = build_synth_model(
        top="demo_top", modules=[{"module": "kept", "cell_count": 2, "area_um2": 2.0}]
    )

    merged = merge_model(first, second, own_half="modules")

    assert [row["module"] for row in merged["modules"]] == ["kept"]


def test_a_rerun_that_lost_its_own_breakdown_does_not_inherit_the_old_rows():
    """A command never inherits its own half.

    A synthesis with an unreadable `stat -json` has `modules = None`; inheriting would publish the previous run's module rows under fresh totals.
    """
    good = build_synth_model(
        top="demo_top",
        modules=[{"module": "stale", "cell_count": 1, "area_um2": 1.0}],
        area_um2=1.0,
        gate_count=1,
    )
    blind = build_synth_model(top="demo_top", modules=None, area_um2=2.0, gate_count=2)

    merged = merge_model(good, blind, own_half="modules")

    assert merged["modules"] is None
    assert merged["totals"]["area_um2"] == 2.0
    assert merged["totals"]["cell_count"] == 2


def test_a_power_rerun_that_lost_its_breakdown_keeps_the_synth_half():
    """The mirror case: the half the rerun does not own is kept."""
    existing = build_synth_model(
        top="demo_top",
        modules=[{"module": "top", "cell_count": 2, "area_um2": 5.586}],
        area_um2=5.586,
        gate_count=2,
        netlist_sha256="deadbeef",
    )
    existing["instances"] = [{"instance": "u_dff", "total_uw": 9.0}]
    existing["totals"]["total_uw"] = 9.0
    blind = build_power_model(
        top="demo_top", instances=None, total_w=1.1e-05, netlist_sha256="deadbeef"
    )

    merged = merge_model(existing, blind, own_half="instances")

    assert merged["instances"] is None
    assert merged["totals"]["total_uw"] == pytest.approx(11.0)
    assert [row["module"] for row in merged["modules"]] == ["top"]
    assert merged["totals"]["area_um2"] == 5.586


def test_a_synthesis_inherits_the_power_half_it_measured_the_netlist_of():
    """A re-synthesis that produced the netlist the power run read keeps the power rows."""
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        netlist_sha256="deadbeef",
    )
    resynth = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=5.586,
        netlist_sha256="deadbeef",
    )

    merged = merge_model(power, resynth, own_half="modules")

    assert len(merged["instances"]) == 3
    assert merged["totals"]["total_uw"] == pytest.approx(28.3)
    assert merged["provenance"]["power"]["netlist_sha256"] == "deadbeef"


def test_a_synthesis_that_replaced_the_netlist_drops_the_power_half():
    """A re-synthesis with a different netlist drops the power half and its totals; `instances: null` is then true."""
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        leakage_w=1.0e-06,
        netlist_sha256="the netlist that was measured",
    )
    resynth = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=7.0,
        netlist_sha256="the netlist this run just wrote",
    )

    merged = merge_model(power, resynth, own_half="modules")

    assert merged["instances"] is None
    assert merged["totals"]["total_uw"] is None
    assert merged["totals"]["leakage_uw"] is None
    assert merged["totals"]["area_um2"] == 7.0
    assert merged["provenance"]["power"]["netlist_sha256"] is None


@pytest.mark.parametrize(
    "measured_on, just_written",
    [
        (None, "a hash"),  # a pnr-sourced power run, or an older document
        ("a hash", None),  # a synthesis whose netlist could not be hashed
        (None, None),  # both predate the provenance block
    ],
)
def test_a_synthesis_without_the_provenance_to_bind_them_drops_the_rows(
    measured_on, just_written
):
    """A synthesis without provenance to bind the halves drops the rows; absent provenance is not a match."""
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        netlist_sha256=measured_on,
    )
    resynth = build_synth_model(
        top="demo_top", modules=[], area_um2=7.0, netlist_sha256=just_written
    )

    assert merge_model(power, resynth, own_half="modules")["instances"] is None


def test_a_power_run_inherits_the_synth_half_of_the_netlist_it_read():
    """A power run inherits the synth half of the netlist it read; the recorded hashes match."""
    synth = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=5.586,
        netlist_sha256="one hash",
    )
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        netlist_sha256="one hash",
    )

    merged = merge_model(synth, power, own_half="instances")

    assert len(merged["modules"]) == 2
    assert merged["totals"]["area_um2"] == 5.586
    assert merged["provenance"]["synth"]["netlist_sha256"] == "one hash"


def test_a_power_run_that_read_another_netlist_drops_the_synth_half():
    """A power run whose netlist differs from the one in the synthesis provenance drops the synth half, so module rows never describe cells the power run did not see."""
    synth = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=5.586,
        gate_count=2,
        netlist_sha256="the netlist that was synthesised",
    )
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        netlist_sha256="the netlist this run read",
    )

    merged = merge_model(synth, power, own_half="instances")

    assert merged["modules"] is None
    assert merged["totals"]["area_um2"] is None
    assert merged["totals"]["cell_count"] is None
    assert merged["totals"]["total_uw"] == pytest.approx(28.3)
    assert merged["provenance"]["synth"]["netlist_sha256"] is None


@pytest.mark.parametrize(
    "synthesised, measured_on",
    [
        (None, "a hash"),  # a synth document written before provenance
        ("a hash", None),  # a pnr-sourced power run, or an unreadable netlist
        (None, None),  # neither side recorded anything
    ],
)
def test_a_power_run_without_the_provenance_to_bind_them_drops_the_rows(
    synthesised, measured_on
):
    """Missing provenance is not a match here either, including a `netlist-source: pnr` run, which reads a routed database."""
    synth = build_synth_model(
        top="demo_top",
        modules=parse_stat_json(STAT_JSON),
        area_um2=5.586,
        netlist_sha256=synthesised,
    )
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        total_w=2.83e-05,
        netlist_sha256=measured_on,
    )

    assert merge_model(synth, power, own_half="instances")["modules"] is None


def test_a_model_for_a_different_top_is_replaced_not_merged():
    """A model for a different top is replaced, not merged; artefact directory names are not unique across designs."""
    other = build_synth_model(top="other_top", modules=[], area_um2=99.0)
    power = build_power_model(top="demo_top", instances=[], total_w=1.0e-06)

    merged = merge_model(other, power, own_half="instances")

    assert merged["modules"] is None
    assert merged["totals"]["area_um2"] is None


def test_merging_ignores_a_document_from_an_incompatible_schema():
    stale = build_synth_model(top="demo_top", modules=[], area_um2=1.0)
    stale["schema_version"] = MODEL_SCHEMA_VERSION + 1

    assert (
        merge_model(stale, build_power_model(top="demo_top"), own_half="instances")[
            "modules"
        ]
        is None
    )


def test_model_round_trips_through_disk(tmp_path):
    _root, artefacts = _project(tmp_path)
    model = build_synth_model(
        top="demo_top", modules=parse_stat_json(STAT_JSON), gate_count=2
    )

    path = write_model(model, artefacts)

    assert load_model(path) == model
    assert load_model_or_none(artefacts) == model


def test_load_model_or_none_swallows_a_truncated_document(tmp_path):
    """A truncated model document counts as nothing to merge, never an error in a successful flow."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "phys-model.json").write_text('{"schema_version": 1')

    assert load_model_or_none(artefacts) is None
    assert load_model_or_none(tmp_path / "nowhere") is None


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
        # The identity block a caller that recorded none still writes: `null` means "said nothing about its configuration", distinct from an unknown key.
        "config": None,
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

    merged = merge_manifest(synth, power, own_block="power")

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

    merged = merge_manifest(old, rerun, own_block="synth")

    # The rerun re-produced the synth half, so its totals stand as written; a scrape that failed this time is null, not last run's number.
    assert merged["totals"] == {"area_um2": None, "cell_count": 9}


def test_manifest_merge_never_inherits_the_producing_commands_own_block(tmp_path):
    """`own_block` is the rule, not a side effect of the `backend` test.

    A caller whose backend name went missing must not resurrect the previous run's report paths under this run's totals.
    """
    root, artefacts = _project(tmp_path)
    old = _synth_manifest(root, artefacts, None, totals={"area_um2": 5.586})
    rerun = build_manifest(
        project_root=root,
        phys_dir=artefacts,
        command="synth",
        run="demo_synth",
        top="demo_top",
        totals={"area_um2": None},
        synth={"backend": None},
    )

    merged = merge_manifest(old, rerun, own_block="synth")

    assert merged["synth"] == {key: None for key in SYNTH_KEYS}
    assert merged["totals"] == {"area_um2": None}


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

    assert merge_manifest(other, power, own_block="power")["synth"]["backend"] is None


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
    """A coverage manifest in the same tree is not mistaken for a physical manifest, which has its own filename."""
    root, artefacts = _project(tmp_path)
    cov_dir = root / "artefacts" / "cov_dir"
    cov_dir.mkdir(parents=True)
    (cov_dir / "manifest.json").write_text('{"schema_version": 1}')
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)

    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


def test_discovery_reaches_a_manifest_behind_a_symlinked_artefact_dir(tmp_path):
    """Discovery reaches a manifest behind a symlinked `artefacts/` (scratch storage); the default `os.walk` would find nothing."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    suite = root / "verif" / "demo"
    suite.mkdir(parents=True)
    physical = tmp_path / "scratch" / "artefacts"
    artefacts = physical / "demo_synth"
    artefacts.mkdir(parents=True)
    (suite / "artefacts").symlink_to(physical, target_is_directory=True)
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)

    found = discover_manifests(root)

    assert found == [str(suite / "artefacts" / "demo_synth" / MANIFEST_FILENAME)]


def _project_with_symlinked_artefacts(tmp_path):
    """Return a project whose ``artefacts/`` links to scratch storage, with the root and the artefact directory as the project reaches it."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    suite = root / "verif" / "demo"
    suite.mkdir(parents=True)
    physical = tmp_path / "scratch" / "artefacts"
    (physical / "demo_synth").mkdir(parents=True)
    (suite / "artefacts").symlink_to(physical, target_is_directory=True)
    return root, suite / "artefacts" / "demo_synth"


def test_manifest_paths_stay_project_relative_through_a_symlinked_artefacts_dir(
    tmp_path,
):
    """Manifest paths stay project-relative through a symlinked artefacts dir instead of coming out absolute and host-specific."""
    root, artefacts = _project_with_symlinked_artefacts(tmp_path)
    (artefacts / "synth.log").write_text("Chip area: 5.586\n")
    model_path = write_model(build_synth_model(top="demo_top"), artefacts)

    manifest = build_manifest(
        project_root=project_root_for_dir(artefacts),
        phys_dir=artefacts,
        command="synth",
        model_path=model_path,
        synth={"backend": "yosys", "log": str(artefacts / "synth.log")},
    )

    assert manifest["phys_dir"] == "verif/demo/artefacts/demo_synth"
    assert manifest["model"] == "verif/demo/artefacts/demo_synth/phys-model.json"
    assert manifest["synth"]["log"] == "verif/demo/artefacts/demo_synth/synth.log"


def test_a_symlinked_artefacts_dir_still_round_trips_back_to_the_files(tmp_path):
    """A manifest read back through the same root resolves to the files through the link."""
    root, artefacts = _project_with_symlinked_artefacts(tmp_path)
    model_path = write_model(build_synth_model(top="demo_top"), artefacts)
    manifest_path = write_manifest(
        build_manifest(
            project_root=project_root_for_dir(artefacts),
            phys_dir=artefacts,
            command="synth",
            model_path=model_path,
            synth={"backend": "yosys"},
        ),
        artefacts,
    )

    assert project_root_for_dir(artefacts) == str(root)
    assert project_root_for(manifest_path) == str(root)
    resolved = resolve(manifest_path, "verif/demo/artefacts/demo_synth/phys-model.json")
    assert os.path.samefile(resolved, model_path)


def _project_with_in_project_artefact_link(tmp_path):
    """Return a project whose ``artefacts/`` links to storage inside the project.

    Both routes to a run are under the project root, so discovery can report either while the manifest's ``phys_dir`` describes one.
    """
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    store = root / "scratch_artefacts"
    (store / "demo_synth").mkdir(parents=True)
    suite = root / "verif" / "demo"
    suite.mkdir(parents=True)
    (suite / "artefacts").symlink_to(store, target_is_directory=True)
    return root, store / "demo_synth", suite / "artefacts" / "demo_synth"


def test_project_root_survives_a_manifest_read_through_the_link_target(tmp_path):
    """The project root survives a manifest read through the link target.

    When `os.walk` reaches `scratch_artefacts/` before the link, counting `phys_dir` components off that path would climb above the project.
    """
    root, target_route, logical_route = _project_with_in_project_artefact_link(tmp_path)
    model_path = write_model(build_synth_model(top="demo_top"), logical_route)
    write_manifest(_synth_manifest(root, logical_route, model_path), logical_route)

    assert project_root_for(target_route / MANIFEST_FILENAME) == str(root)
    resolved = resolve(
        target_route / MANIFEST_FILENAME,
        "verif/demo/artefacts/demo_synth/phys-model.json",
    )
    assert os.path.samefile(resolved, model_path)


def test_discovery_of_an_in_project_artefact_link_lands_on_the_files(tmp_path):
    """Whichever route the walk reports, the manifest resolves onto its own artefacts, independent of `os.walk` order."""
    root, _target, logical_route = _project_with_in_project_artefact_link(tmp_path)
    model_path = write_model(build_synth_model(top="demo_top"), logical_route)
    write_manifest(_synth_manifest(root, logical_route, model_path), logical_route)

    found = discover_manifests(root)

    assert len(found) == 1
    assert project_root_for(found[0]) == str(root)
    assert os.path.samefile(
        resolve(found[0], load_manifest(found[0])["model"]), model_path
    )


def test_discovery_terminates_on_a_symlink_loop(tmp_path):
    """Discovery admits a directory once by its real path, so a link back to an ancestor is not descended and the run is reported once."""
    root, artefacts = _project(tmp_path)
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)
    (artefacts / "loop").symlink_to(root, target_is_directory=True)

    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


def test_discovery_does_not_enter_a_symlink_outside_the_artefact_layout(tmp_path):
    """Discovery does not follow a symlink outside the artefact layout, such as `vendor/` or a link to `$HOME`."""
    root, artefacts = _project(tmp_path)
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)
    unrelated = tmp_path / "elsewhere"
    (unrelated / "nested" / "artefacts" / "other_synth").mkdir(parents=True)
    write_manifest(
        _synth_manifest(unrelated, unrelated, None),
        unrelated,
    )
    write_manifest(
        _synth_manifest(unrelated, unrelated, None),
        unrelated / "nested" / "artefacts" / "other_synth",
    )
    (root / "vendor").symlink_to(unrelated, target_is_directory=True)

    # Neither the manifest at the link's top nor the one inside it is found; the link is not entered.
    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


def test_discovery_follows_a_link_from_inside_an_artefacts_subtree(tmp_path):
    """A run directory linked from inside an `artefacts/` tree is followed even though the link's name is not `artefacts`."""
    root, artefacts = _project(tmp_path)
    scratch = tmp_path / "scratch" / "big_run"
    scratch.mkdir(parents=True)
    write_manifest(_synth_manifest(root, scratch, None), scratch)
    (artefacts.parent / "big_run").symlink_to(scratch, target_is_directory=True)

    assert discover_manifests(root) == [
        str(artefacts.parent / "big_run" / MANIFEST_FILENAME)
    ]


def test_discovery_refuses_a_link_onto_an_ancestor_of_the_project(tmp_path):
    """A link onto an ancestor of the project is refused, so sibling projects are not pulled into the walk."""
    root, artefacts = _project(tmp_path)
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)
    sibling = tmp_path / "other_project"
    sibling.mkdir()
    write_manifest(_synth_manifest(sibling, sibling, None), sibling)
    (artefacts / "up").symlink_to(tmp_path, target_is_directory=True)

    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


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
    """A synthesis whose `stat -json` never landed still writes the totals it parsed."""
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
    """The publish path never raises into a flow that has produced its product."""
    _root, artefacts = _project(tmp_path)
    blocked = artefacts / "synth_netlist.v"  # a file where a directory must be
    blocked.write_text("module demo_top(); endmodule\n")

    published = publish_synth(artefact_dir=blocked, top="demo_top", backend="yosys")

    assert published["error"] is not None
    assert published["model"] is None and published["manifest"] is None


def test_publishing_a_power_run_over_a_synth_run_merges_on_disk(tmp_path):
    """Two commands into one artefact directory give one document describing both halves."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    (artefacts / "power_instances.cells").write_text(INSTANCE_CELLS)

    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )
    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="synth",
        netlist_sha256=NETLIST_SHA256,
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


def test_a_synth_rerun_that_cannot_read_its_stats_publishes_a_null_breakdown(tmp_path):
    """A synth rerun that cannot read its stats publishes `modules: null`, not the first run's rows, while the power half (same netlist) is still carried forward."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )
    publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=_sha256_of(netlist),
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
    )
    (artefacts / "synth_stat.json").unlink()

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=7.0,
        gate_count=3,
    )

    model = load_model(published["model"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] == 7.0
    assert model["totals"]["cell_count"] == 3
    # The other half is untouched by a synthesis, so it still travels.
    assert len(model["instances"]) == 3
    assert model["totals"]["total_uw"] == pytest.approx(28.3)


def _sha256_of(path):
    """Return what a flow that read ``path`` records in its provenance."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish_the_pair_against(artefacts, netlist):
    """Publish a synthesis and then a power run, both bound to ``netlist``.

    The power half gets the hash of the bytes on disk now, as the real flow captures it; a test that rewrites the file afterwards stands in for a build that replaced it mid-run.
    """
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )
    publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="synth",
        netlist_sha256=_sha256_of(netlist),
        report_path=artefacts / "power.rpt",
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
        leakage_w=1.0e-06,
    )


def test_a_resynthesis_of_the_same_netlist_keeps_the_power_half(tmp_path):
    """Re-running `rb synth` over an unchanged design writes the same netlist, so the power half is kept."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    _publish_the_pair_against(artefacts, netlist)
    netlist.write_text(NETLIST)  # the same bytes, written again

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )

    model = load_model(published["model"])
    assert len(model["instances"]) == 3
    assert model["totals"]["total_uw"] == pytest.approx(28.3)
    assert load_manifest(published["manifest"])["power"]["backend"] == "openroad"


def test_a_resynthesis_of_a_changed_netlist_drops_the_power_half(tmp_path):
    """A re-synthesis with changed RTL withdraws the per-instance watts from the model, totals and manifest."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    _publish_the_pair_against(artefacts, netlist)
    netlist.write_text(NETLIST_AFTER_AN_RTL_EDIT)

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=7.0,
        gate_count=3,
    )

    model = load_model(published["model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] is None
    assert model["totals"]["leakage_uw"] is None
    assert len(model["modules"]) == 2 and model["totals"]["area_um2"] == 7.0
    # The manifest agrees, so the publication does not contradict itself about a power measurement.
    manifest = load_manifest(published["manifest"])
    assert all(manifest["power"][key] is None for key in POWER_KEYS)
    assert manifest["totals"]["total_uw"] is None
    assert manifest["synth"]["backend"] == "yosys"


def test_a_power_run_over_a_regenerated_netlist_drops_the_synth_half(tmp_path):
    """A power run over a regenerated netlist drops the synth half from the model, totals and manifest."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="synth",
        netlist_sha256=hashlib.sha256(NETLIST_AFTER_AN_RTL_EDIT.encode()).hexdigest(),
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
    )

    model = load_model(published["model"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] is None
    assert model["totals"]["cell_count"] is None
    assert len(model["instances"]) == 3
    manifest = load_manifest(published["manifest"])
    assert all(manifest["synth"][key] is None for key in SYNTH_KEYS)
    assert manifest["power"]["backend"] == "openroad"


def test_a_power_run_over_the_netlist_it_read_keeps_the_synth_half(tmp_path):
    """`rb power` reads what `rb synth` wrote, so the hashes match and the two commands add up to one document."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    _publish_the_pair_against(artefacts, netlist)

    model = load_model(artefacts / "phys-model.json")
    assert len(model["modules"]) == 2 and len(model["instances"]) == 3
    assert model["totals"]["area_um2"] == 5.586


def test_a_resynthesis_drops_a_power_half_that_recorded_no_netlist(tmp_path):
    """A `netlist-source: pnr` run, or a model written before provenance, records no hash; such rows are dropped, not assumed to match."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="pnr",
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
    )

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )

    model = load_model(published["model"])
    assert model["provenance"]["power"]["netlist_sha256"] is None
    assert model["instances"] is None
    assert model["totals"]["total_uw"] is None
    assert load_manifest(published["manifest"])["power"]["backend"] is None


def test_a_publish_records_the_hash_of_the_netlist_it_touched(tmp_path):
    """Both halves record the hash and agree when about one file."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    expected = hashlib.sha256(NETLIST.encode()).hexdigest()
    _publish_the_pair_against(artefacts, netlist)

    provenance = load_model(artefacts / "phys-model.json")["provenance"]
    assert provenance["synth"]["netlist_sha256"] == expected
    assert provenance["power"]["netlist_sha256"] == expected


def test_a_power_rerun_that_cannot_read_its_report_publishes_a_null_breakdown(tmp_path):
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        area_um2=5.586,
        gate_count=2,
    )
    publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
    )
    (artefacts / "power_instances.rpt").unlink()

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
        total_w=1.0e-05,
    )

    model = load_model(published["model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] == pytest.approx(10.0)
    assert [row["module"] for row in model["modules"]] == ["sub", "top"]
    assert model["totals"]["area_um2"] == 5.586


def test_the_manifest_does_not_name_an_artefact_that_was_never_written(tmp_path):
    """`null` means "not produced"; a filename the tool skipped does not appear in the manifest."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "synth.log").write_text("Chip area: 5.586\n")

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo_synth",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=artefacts / "synth_netlist.v",
        log_path=artefacts / "synth.log",
        area_um2=5.586,
    )

    block = load_manifest(published["manifest"])["synth"]
    assert block["stats"] is None
    assert block["netlist"] is None
    assert block["log"] == "verif/demo/artefacts/demo_synth/synth.log"
    assert block["backend"] == "yosys"


def test_the_power_manifest_nulls_the_reports_the_tcl_catch_skipped(tmp_path):
    _root, artefacts = _project(tmp_path)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo_power",
        netlist_source="synth",
        report_path=artefacts / "power.rpt",
        instances_path=artefacts / "power_instances.rpt",
        cells_path=artefacts / "power_instances.cells",
        total_w=2.83e-05,
    )

    block = load_manifest(published["manifest"])["power"]
    assert block["report"] is None
    assert block["cells"] is None
    assert block["instances"].endswith("power_instances.rpt")
    assert block["netlist_source"] == "synth"


def test_the_model_and_manifest_are_replaced_atomically(tmp_path, monkeypatch):
    """A model and manifest overwrite is one atomic rename (temp then `os.replace`): a concurrent `rb phys` read sees old or new bytes, and no `.tmp` remains."""
    root, artefacts = _project(tmp_path)
    renamed = []
    real_replace = os.replace

    def spy(src, dst):
        renamed.append(str(dst))
        return real_replace(src, dst)

    # `os` is one shared module object, so one patch covers both writers.
    monkeypatch.setattr(os, "replace", spy)

    write_model(build_synth_model(top="demo_top", modules=[]), artefacts)
    model_path = write_model(build_synth_model(top="demo_top"), artefacts)
    manifest_path = write_manifest(
        _synth_manifest(root, artefacts, model_path), artefacts
    )

    assert renamed == [model_path, model_path, manifest_path]
    assert not list(artefacts.glob("*.tmp"))
    assert load_model(model_path)["design"]["top"] == "demo_top"
    assert load_manifest(manifest_path)["command"] == "synth"


def test_the_incomplete_model_warnings_have_dedicated_human_messages():
    """Both events are logged at WARNING, so each needs a dedicated human message."""
    from rtl_buddy.logging_utils import _human_message

    synth = _human_message(
        "synth.phys_model_incomplete",
        {"synth": "demo_synth", "stats": "artefacts/demo_synth/synth_stat.json"},
    )
    assert "demo_synth" in synth
    assert "artefacts/demo_synth/synth_stat.json" in synth
    assert "per-module breakdown" in synth
    # The message names the verb that would have reported the missing rows and what is missing; a power half may still have rows.
    assert "`rb phys module`" in synth
    assert "per-module synthesis rows" in synth
    assert "per-instance power rows in the same model still answer" in synth

    power = _human_message(
        "power.phys_model_incomplete",
        {
            "power": "demo_power",
            "instances": "artefacts/demo_power/power_instances.rpt",
        },
    )
    assert "demo_power" in power
    assert "artefacts/demo_power/power_instances.rpt" in power
    assert "per-instance breakdown" in power
    assert "`rb phys instance`" in power
    assert "per-instance power rows" in power
    assert "per-module synthesis rows in the same model still answer" in power


@pytest.mark.parametrize(
    ("event", "run_key", "run"),
    [
        ("synth.phys_model_incomplete", "synth", "demo_synth"),
        ("power.phys_model_incomplete", "power", "demo_power"),
    ],
)
def test_a_publication_that_failed_is_not_reported_as_a_partial_model(
    event, run_key, run
):
    """A failed publication is not reported as a partial model.

    With `error` set nothing was published and `_publish` returns a null model path, so the message must not describe what phys-model.json records; any file there is another run's.
    """
    from rtl_buddy.logging_utils import _human_message

    rendered = _human_message(
        event,
        {
            run_key: run,
            "stats": "artefacts/x/synth_stat.json",
            "instances": "artefacts/x/power_instances.rpt",
            "error": "timed out waiting for phys-publish.lock",
        },
    )

    assert run in rendered
    assert "timed out waiting for phys-publish.lock" in rendered
    assert "was not written" in rendered
    # The published-but-incomplete wording makes claims that fail when nothing was written.
    assert "are still recorded" not in rendered
    assert "half null" not in rendered
    assert "breakdown" not in rendered


def _project_with_both_inputs(tmp_path):
    """Return a project whose artefact directory holds both flows' raw output."""
    root, artefacts = _project(tmp_path)
    (artefacts / "synth_netlist.v").write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    (artefacts / "power_instances.cells").write_text(INSTANCE_CELLS)
    return root, artefacts


def _publish_synth_half(artefacts, **overrides):
    """Publish a synthesis into ``artefacts`` with rows and totals filled."""
    return publish_synth(
        **{
            "artefact_dir": artefacts,
            "top": "demo_top",
            "backend": "yosys",
            "run": "demo",
            "stats_path": artefacts / "synth_stat.json",
            "netlist_path": artefacts / "synth_netlist.v",
            "area_um2": 5.586,
            "gate_count": 2,
            **overrides,
        }
    )


def _publish_power_half(artefacts, **overrides):
    """Publish a power run into ``artefacts`` with rows and totals filled."""
    return publish_power(
        **{
            "artefact_dir": artefacts,
            "top": "demo_top",
            "backend": "openroad",
            "run": "demo",
            "netlist_sha256": NETLIST_SHA256,
            "instances_path": artefacts / "power_instances.rpt",
            "cells_path": artefacts / "power_instances.cells",
            "total_w": 2.83e-05,
            "leakage_w": 1.0e-06,
            **overrides,
        }
    )


def _publish_both_halves_dir(tmp_path):
    """Return a project whose artefact directory holds a complete model."""
    root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    _publish_power_half(artefacts)
    return root, artefacts


def test_a_failed_power_rerun_withdraws_its_own_half_and_keeps_the_synth_one(
    tmp_path,
):
    """A failed power rerun withdraws its own half and keeps the synth one; a measurement whose evidence was deleted must not stay discoverable."""
    _root, artefacts = _publish_both_halves_dir(tmp_path)

    result = invalidate_half(artefacts, "instances")

    assert result["error"] is None
    model = load_model(result["model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] is None and model["totals"]["leakage_uw"] is None
    # The withdrawn half's netlist hash goes too; a synthesis must not read it as a binding.
    assert model["provenance"]["power"]["netlist_sha256"] is None
    assert model["provenance"]["synth"]["netlist_sha256"] is not None
    # The other half is another command's measurement, still backed by its own artefacts.
    assert [row["module"] for row in model["modules"]] == ["sub", "top"]
    assert model["totals"]["area_um2"] == 5.586
    assert model["totals"]["cell_count"] == 2

    manifest = load_manifest(result["manifest"])
    assert set(manifest["power"]) == set(POWER_KEYS)
    assert all(manifest["power"][key] is None for key in POWER_KEYS)
    assert manifest["synth"]["backend"] == "yosys"
    assert manifest["totals"]["area_um2"] == 5.586
    assert manifest["totals"]["total_uw"] is None


def test_a_failed_synth_rerun_withdraws_its_own_half_and_keeps_the_power_one(
    tmp_path,
):
    _root, artefacts = _publish_both_halves_dir(tmp_path)

    result = invalidate_half(artefacts, "modules")

    model = load_model(result["model"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] is None and model["totals"]["cell_count"] is None
    assert len(model["instances"]) == 3
    assert model["totals"]["total_uw"] == pytest.approx(28.3)

    manifest = load_manifest(result["manifest"])
    assert all(manifest["synth"][key] is None for key in SYNTH_KEYS)
    assert manifest["power"]["backend"] == "openroad"


def test_invalidation_survives_the_crash_that_never_reached_publish(tmp_path):
    """Invalidation happens where the clear happens, so a crash before `_publish_phys_model` gives the same outcome as an orderly failure."""
    _root, artefacts = _publish_both_halves_dir(tmp_path)
    (artefacts / "power_instances.rpt").unlink()
    (artefacts / "power_instances.cells").unlink()

    invalidate_half(artefacts, "instances")

    model = load_model(artefacts / "phys-model.json")
    assert model["instances"] is None
    assert [row["module"] for row in model["modules"]] == ["sub", "top"]


def test_invalidating_a_directory_with_nothing_published_is_a_no_op(tmp_path):
    _root, artefacts = _project(tmp_path)

    result = invalidate_half(artefacts, "modules")

    assert result == {"model": None, "manifest": None, "error": None}
    assert not list(artefacts.iterdir())


def test_invalidation_never_raises_out_of_a_flow(tmp_path):
    """Invalidation never raises out of a flow; a by-product cannot change whether the run passed."""
    _root, artefacts = _project(tmp_path)
    blocked = artefacts / "not_a_dir"
    blocked.write_text("")

    result = invalidate_half(blocked, "modules")

    assert result["model"] is None and result["manifest"] is None


def test_a_successful_publish_after_an_invalidation_restores_the_half(tmp_path):
    _root, artefacts = _publish_both_halves_dir(tmp_path)
    invalidate_half(artefacts, "instances")

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
        cells_path=artefacts / "power_instances.cells",
        total_w=2.83e-05,
    )

    model = load_model(published["model"])
    assert len(model["instances"]) == 3
    assert len(model["modules"]) == 2
    assert load_manifest(published["manifest"])["power"]["backend"] == "openroad"


def test_a_publish_stamps_one_publication_token_into_both_documents(tmp_path):
    _root, artefacts = _project(tmp_path)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
    )

    model = load_model(published["model"])
    manifest = load_manifest(published["manifest"])
    assert model["publication"]
    assert model["publication"] == manifest["publication"]


def test_each_publication_mints_a_new_token(tmp_path):
    """Each publication mints a new token, so a reader can detect an old model beside a new manifest."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    args = dict(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
    )

    first = load_model(publish_synth(**args)["model"])["publication"]
    second = load_model(publish_synth(**args)["model"])["publication"]

    assert first != second


def test_an_invalidation_republishes_the_pair_under_one_token(tmp_path):
    """Invalidation rewrites both documents under one token."""
    _root, artefacts = _publish_both_halves_dir(tmp_path)
    before = load_model(artefacts / "phys-model.json")["publication"]

    result = invalidate_half(artefacts, "instances")

    model = load_model(result["model"])
    manifest = load_manifest(result["manifest"])
    assert model["publication"] == manifest["publication"] != before


def test_a_publish_merges_onto_a_pair_that_was_written_together(tmp_path):
    """Control: an intact publication is merged onto, both halves and both blocks."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)

    published = _publish_power_half(artefacts)

    model = load_model(published["model"])
    manifest = load_manifest(published["manifest"])
    assert len(model["modules"]) == 2 and len(model["instances"]) == 3
    assert model["totals"]["area_um2"] == 5.586
    assert manifest["synth"]["backend"] == "yosys"


def test_a_publish_inherits_nothing_from_an_unpaired_model_and_manifest(tmp_path):
    """A publish inherits nothing from an unpaired model and manifest.

    A publish writes the model then the manifest, so a kill between them leaves mismatched tokens. Merging onto each would re-stamp both and hide the inconsistency.
    """
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    # The manifest is the second write, so it is the one left behind.
    stale = load_manifest(artefacts / MANIFEST_FILENAME)
    stale["publication"] = "a token from a write that finished"
    write_manifest(stale, artefacts)

    published = _publish_power_half(artefacts)

    model = load_model(published["model"])
    manifest = load_manifest(published["manifest"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] is None and model["totals"]["cell_count"] is None
    assert manifest["synth"]["backend"] is None
    assert all(manifest["synth"][key] is None for key in SYNTH_KEYS)
    # This run's own half is published in full under a token the pair shares.
    assert len(model["instances"]) == 3
    assert model["publication"] == manifest["publication"]
    assert model["publication"] not in (None, "a token from a write that finished")


def test_a_publish_inherits_nothing_when_only_one_document_is_there(tmp_path):
    """A model with no manifest beside it is not a publication to inherit from."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    (artefacts / MANIFEST_FILENAME).unlink()

    published = _publish_power_half(artefacts)

    model = load_model(published["model"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] is None
    assert len(model["instances"]) == 3


def test_a_publish_inherits_nothing_from_a_model_that_carries_no_token(tmp_path):
    """A missing token does not match another missing token; there is no evidence the documents were written together."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    for path, load, write in (
        (artefacts / "phys-model.json", load_model, write_model),
        (artefacts / MANIFEST_FILENAME, load_manifest, write_manifest),
    ):
        document = load(path)
        document["publication"] = None
        write(document, artefacts)

    published = _publish_power_half(artefacts)

    assert load_model(published["model"])["modules"] is None


def test_an_invalidation_leaves_an_unpaired_pair_unpaired(tmp_path):
    """Withdrawal still happens, but an invalidation leaves a mismatched pair mismatched."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    _publish_power_half(artefacts)
    stale = load_manifest(artefacts / MANIFEST_FILENAME)
    stale["publication"] = "a token from a write that finished"
    write_manifest(stale, artefacts)

    result = invalidate_half(artefacts, "instances")

    model = load_model(result["model"])
    manifest = load_manifest(result["manifest"])
    assert model["instances"] is None
    assert all(manifest["power"][key] is None for key in POWER_KEYS)
    assert model["publication"] != manifest["publication"]
    assert manifest["publication"] == "a token from a write that finished"


def test_a_merge_keeps_the_new_documents_token(tmp_path):
    """The merged document keeps the new write's token, not the inherited run's."""
    synth = build_synth_model(
        top="demo_top", modules=parse_stat_json(STAT_JSON), netlist_sha256="deadbeef"
    )
    synth["publication"] = "old"
    power = build_power_model(
        top="demo_top",
        instances=parse_instance_power(INSTANCE_RPT),
        netlist_sha256="deadbeef",
    )
    power["publication"] = "new"

    merged = merge_model(synth, power, own_half="instances")

    assert merged["publication"] == "new"
    assert len(merged["modules"]) == 2


def test_publish_power_reads_an_unparsable_report_as_no_breakdown(tmp_path):
    """An unparsable power report reads as no breakdown; the generated Tcl writes `power_instances.rpt` only once `get_cells` is non-empty, as `publish_synth` reads an empty `stat -json`."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "power_instances.rpt").write_text("garbled output, no rows here\n")

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
    )

    assert published["rows"] is None  # the caller's cue to warn
    model = load_model(published["model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] == pytest.approx(28.3)


def _serialisation_probe(monkeypatch, *, dwell=0.15):
    """Widen the critical section and record whether two writers ever share it.

    ``write_model`` is called only inside the lock, by the publish path and the withdrawal, so a counter around it sees the critical sections. The dwell makes an unserialised pair collide.
    """
    from rtl_buddy.phys import model as model_mod

    real = model_mod.write_model
    seen = {"now": 0, "max": 0}
    guard = threading.Lock()

    def _tracked(model, artefact_dir):
        with guard:
            seen["now"] += 1
            seen["max"] = max(seen["max"], seen["now"])
        try:
            time.sleep(dwell)
            return real(model, artefact_dir)
        finally:
            with guard:
                seen["now"] -= 1

    monkeypatch.setattr(model_mod, "write_model", _tracked)
    return seen


def _run_together(*calls):
    """Run each callable in its own thread and return the results in order."""
    results = [None] * len(calls)

    def _capture(index, call):
        results[index] = call()

    threads = [
        threading.Thread(target=_capture, args=(i, call))
        for i, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a publisher hung"
    return results


@pytest.mark.parametrize("power_first", [False, True])
def test_two_concurrent_publishes_keep_both_halves(tmp_path, monkeypatch, power_first):
    """Concurrent co-named `rb synth` and `rb power` publishes into one directory end with one pair carrying both halves, in either order."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    seen = _serialisation_probe(monkeypatch)
    calls = [
        lambda: _publish_power_half(artefacts),
        lambda: _publish_synth_half(artefacts),
    ]
    if not power_first:
        calls.reverse()

    results = _run_together(*calls)

    assert [r["error"] for r in results] == [None, None]
    model = load_model(artefacts / MODEL_FILENAME)
    manifest = load_manifest(artefacts / MANIFEST_FILENAME)
    assert model["publication"] == manifest["publication"]
    assert len(model["modules"]) == 2
    assert len(model["instances"]) == 3
    assert manifest["synth"]["backend"] == "yosys"
    assert manifest["power"]["backend"] == "openroad"
    assert seen["max"] == 1


def test_a_withdrawal_and_a_publish_do_not_interleave(tmp_path, monkeypatch):
    """`invalidate_half` takes the same lock as publish, so a publish never reads a half-withdrawn pair."""
    _root, artefacts = _publish_both_halves_dir(tmp_path)
    seen = _serialisation_probe(monkeypatch)

    results = _run_together(
        lambda: invalidate_half(artefacts, "instances"),
        lambda: _publish_synth_half(artefacts),
    )

    assert [r["error"] for r in results] == [None, None]
    model = load_model(artefacts / MODEL_FILENAME)
    manifest = load_manifest(artefacts / MANIFEST_FILENAME)
    assert seen["max"] == 1
    # Whoever wrote last wrote a whole pair with this run's own synthesis half.
    assert model["publication"] == manifest["publication"]
    assert len(model["modules"]) == 2
    assert manifest["synth"]["backend"] == "yosys"
    # The withdrawal is kept either way: it ran first and the publish inherited nothing, or it ran second and blanked the publish.
    assert model["instances"] is None
    assert all(manifest["power"][key] is None for key in POWER_KEYS)


def test_a_lock_that_cannot_be_taken_is_a_publish_error(tmp_path, monkeypatch):
    """A lock that cannot be taken costs the run its by-product and a warning, never the run."""
    from rtl_buddy.phys import publish as publish_mod

    _root, artefacts = _publish_both_halves_dir(tmp_path)

    @contextlib.contextmanager
    def _held_by_someone_else(_artefact_dir):
        raise TimeoutError("another publish has held the lock")
        yield  # pragma: no cover - unreachable, keeps this a context manager

    monkeypatch.setattr(publish_mod, "_publication_lock", _held_by_someone_else)

    published = _publish_synth_half(artefacts)
    withdrawn = invalidate_half(artefacts, "instances")

    for result in (published, withdrawn):
        assert "another publish has held the lock" in result["error"]
        assert result["model"] is None and result["manifest"] is None


def test_the_lock_file_is_not_mistaken_for_a_published_document(tmp_path):
    """The lock file lives beside the pair, so readers ignore it and a suffix clear spares it."""
    from rtl_buddy.tools.artifact_paths import (
        PHYS_PUBLISH_LOCK_NAME,
        PROTECTED_OUTPUT_PATTERNS,
    )

    _root, artefacts = _publish_both_halves_dir(tmp_path)

    assert (artefacts / PHYS_PUBLISH_LOCK_NAME).exists()
    assert PHYS_PUBLISH_LOCK_NAME in PROTECTED_OUTPUT_PATTERNS
    assert discover_manifests(artefacts) == [str(artefacts / MANIFEST_FILENAME)]


def test_a_power_publish_records_its_mode_and_the_activity_behind_it(tmp_path):
    """A power publish records its mode and activity; two runs of one netlist that differ in stimulus are different measurements."""
    root, artefacts = _project(tmp_path)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    trace = root / "verif" / "demo" / "artefacts" / "csr_smoke" / "dump.saif"
    trace.parent.mkdir(parents=True)
    trace.write_text("(SAIFILE)\n")

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="synth",
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
        mode="dynamic",
        activity=activity_block(source="saif", trace=trace, scope="tb/u_dut"),
    )

    recorded = load_model(published["model"])["provenance"]["power"]
    assert recorded["mode"] == "dynamic"
    assert recorded["activity"]["source"] == "saif"
    assert recorded["activity"]["scope"] == "tb/u_dut"
    # Derived from the artefact layout, not passed in, since the producer holds a path and the test ran in another command.
    assert recorded["activity"]["test"] == "csr_smoke"
    # Echoed into the manifest so a run listing reads one small file per run.
    block = load_manifest(published["manifest"])["power"]
    assert block["mode"] == "dynamic"
    assert block["activity"] == recorded["activity"]


def test_a_static_run_records_defaults_rather_than_a_toggle_rate(tmp_path):
    """A static run records defaults, not a toggle rate; no activity command drove any stimulus."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        instances_path=artefacts / "power_instances.rpt",
        mode="static",
        activity=activity_block(source="default", toggle_rate=0.1, duty=0.5),
    )

    activity = load_manifest(published["manifest"])["power"]["activity"]
    assert activity["source"] == "default"
    assert activity["toggle_rate"] is None and activity["duty"] is None
    assert activity_label(activity) == "defaults"


def test_a_synthetic_run_records_the_toggle_and_duty_that_drove_it():
    activity = activity_block(source="synthetic", toggle_rate=0.2, duty=0.5)
    assert activity["toggle_rate"] == 0.2 and activity["duty"] == 0.5
    assert activity_label(activity) == "toggle 0.2, duty 0.5"


def test_a_run_that_reads_no_trace_records_none_even_when_the_config_keeps_one():
    """A run that reads no trace records no activity source, test or scope, even when the config keeps `activity.saif` and `activity.scope`.

    `get_activity_source()` answers `default`, no `read_saif` is emitted, and recording the fields would put a named test beside a leakage number.
    """
    retained = dict(trace="verif/demo/artefacts/csr_smoke/dump.saif", scope="tb/u_dut")

    for source in ("default", "synthetic"):
        block = activity_block(source=source, **retained)
        assert block["source"] == source
        assert block["trace"] is None, source
        assert block["test"] is None, source
        assert block["scope"] is None, source

    # A run that did read the trace records all three; the gate is on the source.
    read = activity_block(source="saif", **retained)
    assert read["trace"] == retained["trace"]
    assert read["test"] == "csr_smoke"
    assert read["scope"] == "tb/u_dut"


def test_a_trace_is_identified_by_its_bytes_and_not_only_by_its_path():
    """A trace is identified by its bytes as well as its path: `rb test` rewrites `dump.saif` in place, so the hash tells re-captured traces apart."""
    path = "verif/demo/artefacts/csr_smoke/dump.saif"

    first = normalise_activity(
        activity_block(source="saif", trace=path, trace_sha256="a" * 64)
    )
    second = normalise_activity(
        activity_block(source="saif", trace=path, trace_sha256="b" * 64)
    )

    assert first["trace_sha256"] == "a" * 64
    assert first != second
    # The label is unchanged; it is a table cell and not a place for a hash.
    assert first["label"] == second["label"] == "saif csr_smoke"

    # Absent evidence stays null: an unreadable trace and a document written before the field both answer null.
    assert (
        normalise_activity(activity_block(source="saif", trace=path))["trace_sha256"]
        is None
    )
    assert normalise_activity({"source": "saif"})["trace_sha256"] is None

    # A run that read no trace records no hash of one.
    assert (
        activity_block(source="default", trace=path, trace_sha256="a" * 64)[
            "trace_sha256"
        ]
        is None
    )


def test_a_trace_outside_an_artefact_directory_names_no_test():
    """A trace outside an artefact directory names no test; a checked-in golden trace is not a test."""
    assert trace_test("verif/demo/artefacts/csr_smoke/dump.saif") == "csr_smoke"
    assert trace_test("golden/traces/dump.saif") is None
    assert trace_test("dump.saif") is None
    assert trace_test(None) is None


def test_a_synth_publish_records_the_configuration_that_shaped_it(tmp_path):
    """A synth publish records platform, effort and constraints, and the option set as a digest, enough to tell two experiments of one design apart."""
    root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    sdc = root / "verif" / "demo" / "demo.sdc"
    sdc.write_text("create_clock -period 2.0 [get_ports clk]\n")

    published = publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        platform="nangate45",
        effort="timing-opt",
        constraints=sdc,
        # Taken by the caller before its tool ran, not computed here, since this function runs minutes after the tool.
        constraints_sha256=hashlib.sha256(sdc.read_bytes()).hexdigest(),
        options={"strategy": "TIMING"},
    )

    config = load_manifest(published["manifest"])["synth"]["config"]
    assert config["platform"] == "nangate45"
    assert config["effort"] == "timing-opt"
    # Project-relative like every other path, and spelled the same in the model, because the publish relativises both blocks once.
    assert config["constraints"] == "verif/demo/demo.sdc"
    assert load_model(published["model"])["provenance"]["synth"]["config"] == config
    assert config["constraints_sha256"] == hashlib.sha256(sdc.read_bytes()).hexdigest()
    assert config["options_sha256"] == options_digest({"strategy": "TIMING"})


def test_two_option_sets_that_resolve_the_same_digest_the_same():
    """The digest is over the effective values in canonical form, so key order and equivalent spellings do not change it."""
    assert options_digest({"a": 1, "b": 2}) == options_digest({"b": 2, "a": 1})
    assert options_digest({"strategy": "AREA"}) != options_digest(
        {"strategy": "TIMING"}
    )
    # Nothing recorded is `None`, not a digest of the empty mapping.
    assert options_digest(None) is None and options_digest({}) is None


def test_an_undigestible_option_set_is_absent_rather_than_unstable(caplog):
    """An option set that cannot be rendered strictly digests to `None` with a DEBUG line naming the option, instead of a `repr` digest that changes per invocation."""

    class _Opaque:
        pass

    options = {"strategy": "TIMING", "hook": _Opaque()}
    with caplog.at_level(logging.DEBUG):
        assert options_digest(options, producer="synth/blk") is None

    record = next(
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "phys.options_not_serialisable"
    )
    assert record.levelno == logging.DEBUG
    assert record.rtl_fields["producer"] == "synth/blk"
    # The offending key only, so the producer can be fixed.
    assert record.rtl_fields["keys"] == ["hook"]

    # Two `repr`s of two instances of one class differ, so a `repr` digest never fingerprinted the option set.
    assert repr(_Opaque()) != repr(_Opaque())


def test_the_strict_rendering_still_digests_every_option_set_in_use(caplog):
    """Nothing a producer passes today goes through the fallback."""

    with caplog.at_level(logging.DEBUG):
        digest = options_digest(
            {"synth_args": ["-flatten"], "defines": {"W": 8}, "abc": None, "keep": True}
        )
    assert digest is not None and len(digest) == len(
        options_digest({"strategy": "TIMING"})
    )
    assert not [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "phys.options_not_serialisable"
    ]


def test_a_config_that_differs_does_not_stop_the_halves_from_merging(tmp_path):
    """A differing config does not stop the halves from merging; the netlist hash decides that."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
        platform="nangate45",
        options={"strategy": "AREA"},
    )
    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_source="synth",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
        total_w=2.83e-05,
        platform="sky130hd",
        options={"reglvl": 1},
    )

    model = load_model(published["model"])
    assert model["modules"] is not None and model["instances"] is not None
    # Each half keeps its own identity; the inherited block travels with its rows.
    assert model["provenance"]["synth"]["config"]["platform"] == "nangate45"
    assert model["provenance"]["power"]["config"]["platform"] == "sky130hd"


def test_withdrawing_a_half_withdraws_the_identity_that_went_with_it(tmp_path):
    """Withdrawing a half withdraws its mode and activity, since the artefacts behind them are gone."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        instances_path=artefacts / "power_instances.rpt",
        mode="dynamic",
        activity=activity_block(source="synthetic", toggle_rate=0.1, duty=0.5),
        platform="nangate45",
    )

    invalidate_half(artefacts, "instances")

    recorded = load_model(artefacts / "phys-model.json")["provenance"]["power"]
    assert recorded == {key: None for key in BLOCK_PROVENANCE_KEYS["power"]}
    block = load_manifest(artefacts / MANIFEST_FILENAME)["power"]
    assert block["mode"] is None and block["activity"] is None
    assert block["config"] is None


def test_the_power_block_carries_the_two_keys_the_synth_block_does_not():
    """The power block carries `mode` and `activity`; a synthesis has neither, so its block has no null placeholders for them."""
    assert set(BLOCK_PROVENANCE_KEYS["synth"]) == {"netlist_sha256", "config"}
    assert set(BLOCK_PROVENANCE_KEYS["power"]) == {
        "netlist_sha256",
        "config",
        "mode",
        "activity",
    }


def test_an_older_document_reads_back_as_nulls_not_as_a_missing_key(tmp_path):
    """A model written before the identity block reads back as nulls, not a missing key or an error."""
    _root, artefacts = _project(tmp_path)
    model = build_power_model(top="demo_top", instances=[])
    del model["provenance"]["power"]["mode"]
    del model["provenance"]["power"]["activity"]
    write_model(model, artefacts)

    recorded = provenance_of(load_model(artefacts / MODEL_FILENAME))
    assert recorded["power"]["mode"] is None
    assert recorded["power"]["activity"] is None
    assert normalise_activity(recorded["power"]["activity"]) is None


def test_an_experiment_id_is_derived_from_the_ledger_path(tmp_path):
    """The experiment id comes from the ledger path (one directory per experiment) without reading a record, even one not yet written."""
    manifest = tmp_path / "artefacts" / "xplr" / "exp-0007" / "artefacts" / "s"
    manifest.mkdir(parents=True)
    found = experiment_for(manifest / MANIFEST_FILENAME)
    assert found == {"id": "exp-0007", "label": None}


def test_an_experiment_label_comes_from_the_record_when_there_is_one(tmp_path):
    experiment = tmp_path / "artefacts" / "xplr" / "exp-0008"
    (experiment / "artefacts" / "s").mkdir(parents=True)
    (experiment / "record.json").write_text(
        json.dumps({"id": "exp-0008", "hypothesis": "abc9 buys 5% area"})
    )
    found = experiment_for(experiment / "artefacts" / "s" / MANIFEST_FILENAME)
    assert found == {"id": "exp-0008", "label": "abc9 buys 5% area"}


def test_a_record_that_cannot_be_read_costs_the_label_and_nothing_else(tmp_path):
    """A record that cannot be read costs the label only; `rb xplr` reports the malformed record."""
    experiment = tmp_path / "artefacts" / "xplr" / "exp-0009"
    (experiment / "artefacts" / "s").mkdir(parents=True)
    (experiment / "record.json").write_text("{not json")
    found = experiment_for(experiment / "artefacts" / "s" / MANIFEST_FILENAME)
    assert found == {"id": "exp-0009", "label": None}


def test_the_ledgers_reserved_directories_are_not_experiments(tmp_path):
    """The ledger's reserved directories are not experiments: `artefacts/xplr/worktrees/` itself, and a directory under it with no ledger entry."""
    worktree = tmp_path / "artefacts" / "xplr" / "worktrees" / "wt"
    worktree.mkdir(parents=True)
    assert experiment_for(worktree / MANIFEST_FILENAME) is None
    assert experiment_for(worktree / "verif" / "d" / MANIFEST_FILENAME) is None
    nested = tmp_path / "artefacts" / "xplr" / "worktrees" / "worktrees" / "s"
    nested.mkdir(parents=True)
    assert experiment_for(nested / MANIFEST_FILENAME) is None


def test_a_run_inside_a_materialized_worktree_keeps_its_experiment(tmp_path):
    """A run inside a materialized worktree (`artefacts/xplr/worktrees/<exp-id>/`, the default for `rb xplr materialize`) keeps its experiment id and hypothesis."""
    ledger = tmp_path / "artefacts" / "xplr"
    (ledger / "exp-0011").mkdir(parents=True)
    (ledger / "exp-0011" / "record.json").write_text(
        json.dumps({"id": "exp-0011", "hypothesis": "a tighter clock buys area"})
    )
    checkout = ledger / "worktrees" / "exp-0011"
    manifest_dir = checkout / "verif" / "demo" / "artefacts" / "demo_synth"
    manifest_dir.mkdir(parents=True)

    found = experiment_for(manifest_dir / MANIFEST_FILENAME)

    assert found == {"id": "exp-0011", "label": "a tighter clock buys area"}


def test_a_worktree_sidecar_naming_somewhere_else_refutes_the_checkout(tmp_path):
    """A `worktree.json` naming another path refutes the checkout; the layout alone cannot show that."""
    ledger = tmp_path / "artefacts" / "xplr"
    (ledger / "exp-0012").mkdir(parents=True)
    checkout = ledger / "worktrees" / "exp-0012"
    manifest_dir = checkout / "verif" / "demo" / "artefacts" / "demo_synth"
    manifest_dir.mkdir(parents=True)

    # Agreeing: the experiment is reported, record or no record.
    (ledger / "exp-0012" / "worktree.json").write_text(
        json.dumps({"id": "exp-0012", "path": str(checkout)})
    )
    assert experiment_for(manifest_dir / MANIFEST_FILENAME) == {
        "id": "exp-0012",
        "label": None,
    }

    # Naming somewhere else: this checkout is not that experiment's.
    (ledger / "exp-0012" / "worktree.json").write_text(
        json.dumps({"id": "exp-0012", "path": str(tmp_path / "elsewhere")})
    )
    assert experiment_for(manifest_dir / MANIFEST_FILENAME) is None

    # Malformed, or deleted by `rb xplr release`: refutes nothing, since a listing may not drop a row over a missing bookkeeping file.
    (ledger / "exp-0012" / "worktree.json").write_text("{not json")
    assert experiment_for(manifest_dir / MANIFEST_FILENAME) is not None
    (ledger / "exp-0012" / "worktree.json").unlink()
    assert experiment_for(manifest_dir / MANIFEST_FILENAME) is not None


def test_a_manifest_outside_a_ledger_has_no_experiment(tmp_path):
    plain = tmp_path / "verif" / "demo" / "artefacts" / "demo_synth"
    plain.mkdir(parents=True)
    assert experiment_for(plain / MANIFEST_FILENAME) is None


def test_a_publish_into_an_empty_directory_reports_nothing_to_pair_with(tmp_path):
    """A publish into an empty directory reports `None`, not `False`: it says nothing about this run's netlist and is not a refusal."""
    _root, artefacts = _project(tmp_path)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
    )
    assert published["paired"] is None


def test_a_publish_reports_pairing_with_a_half_measured_on_its_netlist(tmp_path):
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
    )

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
    )

    assert published["paired"] is True


def test_a_publish_reports_a_half_the_gate_dropped(tmp_path):
    """A publish reports a half the gate discarded, which is what a run told to publish beside a named synthesis warns on."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST_AFTER_AN_RTL_EDIT)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    publish_synth(
        artefact_dir=artefacts,
        top="demo_top",
        backend="yosys",
        run="demo",
        stats_path=artefacts / "synth_stat.json",
        netlist_path=netlist,
    )

    published = publish_power(
        artefact_dir=artefacts,
        top="demo_top",
        backend="openroad",
        run="demo",
        netlist_sha256=NETLIST_SHA256,
        instances_path=artefacts / "power_instances.rpt",
    )

    assert published["paired"] is False
    assert load_model(published["model"])["modules"] is None
