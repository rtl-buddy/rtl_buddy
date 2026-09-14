"""
Unit tests for the structured physical model and its manifest (#558).

The fixtures are captured tool output written inline rather than files on
disk: both formats are text, so the exact bytes a test needs are readable
in the test that needs them. The `stat -json` dump is trimmed to the keys
the reader looks at, with Yosys' RTLIL backslash prefix and its
inconsistent spacing preserved — those are the parts that break.
"""

import contextlib
import hashlib
import json
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

NETLIST = """module demo_top (clk);
  input clk;
  DFF_X1 _64_ (.CK(clk));
endmodule
"""

NETLIST_AFTER_AN_RTL_EDIT = NETLIST.replace("DFF_X1", "DFF_X2")

#: What a flow that measured `NETLIST` records in its provenance. Both
#: halves have to record it for either to inherit the other's rows.
NETLIST_SHA256 = hashlib.sha256(NETLIST.encode()).hexdigest()

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
    """For a matched pair — the power run read the netlist the synthesis
    wrote, which is what the recorded hash says and what the synthesis
    direction requires before it will inherit anything."""
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
    """The merge must not make a half grow-only: a module the design no
    longer has must disappear on the next synthesis."""
    first = build_synth_model(
        top="demo_top", modules=[{"module": "gone", "cell_count": 1, "area_um2": 1.0}]
    )
    second = build_synth_model(
        top="demo_top", modules=[{"module": "kept", "cell_count": 2, "area_um2": 2.0}]
    )

    merged = merge_model(first, second, own_half="modules")

    assert [row["module"] for row in merged["modules"]] == ["kept"]


def test_a_rerun_that_lost_its_own_breakdown_does_not_inherit_the_old_rows():
    """The half a command owns is never carried forward.

    A synthesis whose `stat -json` was unreadable produces
    `modules = None`, which by shape alone is indistinguishable from a
    power run's empty synth half — inheriting there would publish the
    *previous* run's module rows underneath this run's fresh totals.
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
    """The mirror, and the half the rerun does *not* own still travels."""
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
    """The bind (#560 review, Codex P1). A re-synthesis that produced the
    very netlist the power run read carries its rows forward — that is the
    ordinary two-command directory, and nothing about it has changed."""
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
    """Editing the RTL and re-running `rb synth` writes a different netlist,
    and the watts measured on the old one are not a breakdown of it. They go,
    with their totals — `instances: null` is true where the rows are not."""
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
    """Strict: a half whose binding cannot be *shown* is not inherited. The
    alternative reading — absent provenance means "probably still fine" —
    is the one that publishes watts against a netlist nothing measured."""
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
    """The other direction of the same rule, and the case that makes it
    usually pass: a power analysis runs *against* the synthesis output, so
    the netlist it read is the one those module rows were counted off, and
    the recorded hashes say so."""
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
    """The finding (#560 round-9 review, Codex P1). The gate is symmetric
    because the failure is: a power run whose netlist is not the one the
    synthesis provenance records measured a different design, and carrying
    the local module rows forward would describe cells this publication
    never saw — a re-synthesis between the two runs, or a power run pointed
    at another suite's netlist."""
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
    """Missing evidence is not a match in this direction either — including
    the `netlist-source: pnr` run, which reads a routed database and can say
    nothing about which netlist the module rows beside it came from."""
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
    """Artefact directories are keyed on a run's *name*, and names are not
    unique across designs."""
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
        # The identity block a caller that recorded none still writes
        # (#568): `null` is "this run said nothing about its
        # configuration", and absent would be indistinguishable from a
        # key this build does not know.
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

    # The rerun re-produced the synth half, so its totals stand as
    # written: a scrape that failed this time is null, not last run's
    # number — the manifest mirror of the model's shrinking rerun rule.
    assert merged["totals"] == {"area_um2": None, "cell_count": 9}


def test_manifest_merge_never_inherits_the_producing_commands_own_block(tmp_path):
    """`own_block` is the rule, not the `backend` test's side effect.

    A producer always names its own backend today, so the null-backend
    test already excludes its block; this pins the behaviour for a caller
    whose backend name went missing, which must not resurrect the
    previous run's report paths under this run's totals.
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
    """The physical manifest carries its own filename precisely because it
    has no `cov_dir` to be found by; a coverage manifest in the same tree
    must not be mistaken for one."""
    root, artefacts = _project(tmp_path)
    cov_dir = root / "artefacts" / "cov_dir"
    cov_dir.mkdir(parents=True)
    (cov_dir / "manifest.json").write_text('{"schema_version": 1}')
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)

    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


def test_discovery_reaches_a_manifest_behind_a_symlinked_artefact_dir(tmp_path):
    """`artefacts/` linked onto scratch storage is an ordinary setup — the
    same one the filelist writer is pinned against — and `os.walk`'s default
    would report a project with no physical data at all."""
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
    """A project whose ``artefacts/`` is a link onto scratch storage.

    Returns the root and the artefact directory *as the project reaches
    it* — the path every producer holds, and the one the manifest's paths
    have to be expressed in.
    """
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
    """The finding (#560 round-9 review, Codex P2). Resolving both operands
    put the scratch path on both sides of the comparison, so every path came
    out absolute and host-specific — breaking the project-relative contract
    for exactly the layout discovery goes out of its way to support."""
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
    """The other end of the same contract: the root a written manifest is
    read back through is the one its paths were written against, so
    `resolve` lands on the file through the link rather than counting parts
    off a scratch path that has none of them."""
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


def test_discovery_terminates_on_a_symlink_loop(tmp_path):
    """Following links costs a loop risk, so a directory is admitted once by
    its real path: the link back to an ancestor is not descended into, and
    the run it circles is still reported exactly once."""
    root, artefacts = _project(tmp_path)
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)
    (artefacts / "loop").symlink_to(root, target_is_directory=True)

    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


def test_discovery_does_not_enter_a_symlink_outside_the_artefact_layout(tmp_path):
    """The finding (#560 round-10 review, Codex P2). Following *every*
    directory link made a `vendor/` link — or one to `$HOME` — part of the
    project's walk, so an unrelated tree was scanned and its
    `phys-manifest.json` reported as this project's own run."""
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

    # Neither the manifest at the link's top nor the one buried inside it:
    # the link is not entered at all, so nothing under it is even scanned.
    assert discover_manifests(root) == [str(artefacts / MANIFEST_FILENAME)]


def test_discovery_follows_a_link_from_inside_an_artefacts_subtree(tmp_path):
    """The other half of the boundary: a single run directory linked out of
    an `artefacts/` tree is still the documented layout, so it is followed
    even though the link's own name is not `artefacts`."""
    root, artefacts = _project(tmp_path)
    scratch = tmp_path / "scratch" / "big_run"
    scratch.mkdir(parents=True)
    write_manifest(_synth_manifest(root, scratch, None), scratch)
    (artefacts.parent / "big_run").symlink_to(scratch, target_is_directory=True)

    assert discover_manifests(root) == [
        str(artefacts.parent / "big_run" / MANIFEST_FILENAME)
    ]


def test_discovery_refuses_a_link_onto_an_ancestor_of_the_project(tmp_path):
    """Inside the artefact layout a link is followed, so the loop guard has
    to stand on its own: a link to the directory the project itself lives in
    would otherwise pull every sibling project into the walk."""
    root, artefacts = _project(tmp_path)
    write_manifest(_synth_manifest(root, artefacts, None), artefacts)
    sibling = tmp_path / "other_project"
    sibling.mkdir()
    write_manifest(_synth_manifest(sibling, sibling, None), sibling)
    (artefacts / "up").symlink_to(tmp_path, target_is_directory=True)

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
    """The on-disk shape of the own-half rule (#560 review).

    A first synthesis records its modules; a power run lands beside it;
    then a synthesis rerun whose `stat -json` never appeared must publish
    `modules: null` rather than the first run's rows — while the power
    half, which it does not own and whose netlist the rerun reproduced
    unchanged, is still carried forward.
    """
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
    """What a flow that read ``path`` records in its provenance."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish_the_pair_against(artefacts, netlist):
    """A synthesis and then a power run, both bound to ``netlist``.

    The power half is handed the hash of the bytes on disk *now*, which is
    what the real flow captures at the moment it gives the netlist to
    OpenROAD — a test that rewrites the file afterwards is standing in for
    a build that replaced it mid-run.
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
    """On disk, the ordinary case: `rb synth` re-run over an unchanged design
    writes the same netlist, so the watts beside it still describe it."""
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
    """The finding (#560 review, Codex P1). The RTL was edited and `rb synth`
    re-run into the same directory: the per-instance watts were measured on
    the netlist that run has just replaced, so they are withdrawn — from the
    model, from the totals, and from the manifest that would otherwise go on
    naming the reports behind them."""
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
    # The manifest is told the same thing, so the publication does not
    # contradict itself about whether a power measurement is here.
    manifest = load_manifest(published["manifest"])
    assert all(manifest["power"][key] is None for key in POWER_KEYS)
    assert manifest["totals"]["total_uw"] is None
    assert manifest["synth"]["backend"] == "yosys"


def test_a_power_run_over_a_regenerated_netlist_drops_the_synth_half(tmp_path):
    """The finding on disk (#560 round-9 review, Codex P1). A synthesis
    published its module rows; the netlist was rebuilt; the power run
    measured the new one. The rows in the directory are a breakdown of the
    netlist this analysis did *not* read, so they go — from the model, from
    the totals, and from the manifest that would otherwise go on naming the
    synthesis reports behind them."""
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
    """The ordinary pair on disk, and the reason the check above is
    affordable: `rb power` reads what `rb synth` wrote, so the hashes match
    and the two commands still add up to one complete document."""
    _root, artefacts = _project(tmp_path)
    netlist = artefacts / "synth_netlist.v"
    netlist.write_text(NETLIST)
    _publish_the_pair_against(artefacts, netlist)

    model = load_model(artefacts / "phys-model.json")
    assert len(model["modules"]) == 2 and len(model["instances"]) == 3
    assert model["totals"]["area_um2"] == 5.586


def test_a_resynthesis_drops_a_power_half_that_recorded_no_netlist(tmp_path):
    """A `netlist-source: pnr` run reads the routed database, not a netlist,
    and records no hash — as does any model written before provenance
    existed. Nothing binds those rows to what this synthesis wrote, so they
    are dropped rather than assumed."""
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
    """Both halves record it, and they agree when they are about one file."""
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
    """`null` means "not produced" — so a filename the flow intended but
    the tool skipped must not appear as though it were on disk."""
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
    """A `rb phys` read racing a rerun must never see a torn document.

    Both writers go temp-then-`os.replace`, so an overwrite is one
    rename: a concurrent reader gets the old bytes or the new ones, and
    no `.tmp` is left behind for discovery to trip over.
    """
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
    """Both events are logged at WARNING, so neither may fall through to
    the "foo bar" fallback."""
    from rtl_buddy.logging_utils import _human_message

    synth = _human_message(
        "synth.phys_model_incomplete",
        {"synth": "demo_synth", "stats": "artefacts/demo_synth/synth_stat.json"},
    )
    assert "demo_synth" in synth
    assert "artefacts/demo_synth/synth_stat.json" in synth
    assert "per-module breakdown" in synth
    # The verbs exist as of this branch, so the message names the one that
    # would have reported the missing rows — and names *what* is missing,
    # since a power half in the same model still has rows to answer from.
    assert "`rb phys module`" in synth
    assert "per-module synthesis rows" in synth
    assert "per-instance power rows in the same model still answer" in synth

    power = _human_message(
        "power.phys_model_incomplete",
        {
            "power": "demo_power",
            "instances": "artefacts/demo_power/power_instances.rpt",
            "error": "disk full",
        },
    )
    assert "demo_power" in power
    assert "artefacts/demo_power/power_instances.rpt" in power
    assert "per-instance breakdown" in power
    assert "disk full" in power
    assert "`rb phys instance`" in power
    assert "per-instance power rows" in power
    assert "per-module synthesis rows in the same model still answer" in power


# ---------------------------------------------------------------------------
# The failing rerun — a half whose artefacts have gone must not stay published
# ---------------------------------------------------------------------------


def _project_with_both_inputs(tmp_path):
    """A project whose artefact directory holds both flows' raw output."""
    root, artefacts = _project(tmp_path)
    (artefacts / "synth_netlist.v").write_text(NETLIST)
    (artefacts / "synth_stat.json").write_text(STAT_JSON)
    (artefacts / "power_instances.rpt").write_text(INSTANCE_RPT)
    (artefacts / "power_instances.cells").write_text(INSTANCE_CELLS)
    return root, artefacts


def _publish_synth_half(artefacts, **overrides):
    """A synthesis publication into ``artefacts``, rows and totals filled."""
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
    """A power publication into ``artefacts``, rows and totals filled."""
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
    """A project whose artefact directory already holds a complete model."""
    root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    _publish_power_half(artefacts)
    return root, artefacts


def test_a_failed_power_rerun_withdraws_its_own_half_and_keeps_the_synth_one(
    tmp_path,
):
    """The rerun cleared `power_instances.rpt` and then failed before it could
    publish. Leaving the previous run's watts in the model would leave a
    measurement discoverable whose evidence has just been deleted."""
    _root, artefacts = _publish_both_halves_dir(tmp_path)

    result = invalidate_half(artefacts, "instances")

    assert result["error"] is None
    model = load_model(result["model"])
    assert model["instances"] is None
    assert model["totals"]["total_uw"] is None and model["totals"]["leakage_uw"] is None
    # Including what the withdrawn half was measured on: there is no half
    # left for that hash to bind, and a synthesis must not read it as one.
    assert model["provenance"]["power"]["netlist_sha256"] is None
    assert model["provenance"]["synth"]["netlist_sha256"] is not None
    # The other half is another command's measurement, still backed by its
    # own artefacts on disk.
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
    """The crash case: the clear ran, the tool died, `_publish_phys_model` was
    never called. Same outcome as the orderly failure above — invalidation
    happens where the clear happens, not where the publish would have."""
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
    """Same resilience rule as the publish path: the caller has already
    decided whether the run passed, and a by-product may not change that."""
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


# ---------------------------------------------------------------------------
# The publication token — the model and its manifest are two files
# ---------------------------------------------------------------------------


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
    """The token identifies one write of the pair, so a reader that saw the
    old model and the new manifest can tell."""
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
    """Invalidation rewrites both documents, so it is a publication too and
    must not leave the two disagreeing about which write they came from."""
    _root, artefacts = _publish_both_halves_dir(tmp_path)
    before = load_model(artefacts / "phys-model.json")["publication"]

    result = invalidate_half(artefacts, "instances")

    model = load_model(result["model"])
    manifest = load_manifest(result["manifest"])
    assert model["publication"] == manifest["publication"] != before


def test_a_publish_merges_onto_a_pair_that_was_written_together(tmp_path):
    """The control for the two tests below: an intact publication is merged
    onto exactly as before, both halves and both blocks."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)

    published = _publish_power_half(artefacts)

    model = load_model(published["model"])
    manifest = load_manifest(published["manifest"])
    assert len(model["modules"]) == 2 and len(model["instances"]) == 3
    assert model["totals"]["area_um2"] == 5.586
    assert manifest["synth"]["backend"] == "yosys"


def test_a_publish_inherits_nothing_from_an_unpaired_model_and_manifest(tmp_path):
    """The interrupted publish (#560 review, Codex P1).

    A publish writes the model and then the manifest, so a kill between the
    two leaves the pair disagreeing about which write it came from. Merging
    each document onto its own fresh half would re-stamp both with this
    write's token and hand every later reader an inconsistency that looks
    exactly like a pair written together. Neither half is inherited — the
    directory holds no publication to merge onto."""
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
    # This run's own half is published in full, under a token the pair shares.
    assert len(model["instances"]) == 3
    assert model["publication"] == manifest["publication"]
    assert model["publication"] not in (None, "a token from a write that finished")


def test_a_publish_inherits_nothing_when_only_one_document_is_there(tmp_path):
    """Same rule, one document short: a model with no manifest beside it (the
    kill landed before the second write ever ran) is not half a publication
    to inherit from either."""
    _root, artefacts = _project_with_both_inputs(tmp_path)
    _publish_synth_half(artefacts)
    (artefacts / MANIFEST_FILENAME).unlink()

    published = _publish_power_half(artefacts)

    model = load_model(published["model"])
    assert model["modules"] is None
    assert model["totals"]["area_um2"] is None
    assert len(model["instances"]) == 3


def test_a_publish_inherits_nothing_from_a_model_that_carries_no_token(tmp_path):
    """A missing token is not a match with another missing token: nothing
    stamped either document, so there is no evidence they were written
    together and no basis for putting this write's token on them."""
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
    """Withdrawal still happens — the artefacts behind the half really have
    gone — but it is not a publication of the two documents, so it must not
    be what makes a mismatched pair start claiming it was written together."""
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
    """The token names the write in progress, not the run whose half was
    inherited into it."""
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


# ---------------------------------------------------------------------------
# An empty parse is an unreadable report, not a design without cells
# ---------------------------------------------------------------------------


def test_publish_power_reads_an_unparsable_report_as_no_breakdown(tmp_path):
    """The generated Tcl writes `power_instances.rpt` only once `get_cells`
    has come back non-empty, so a report that parses to zero rows is garbled
    — the same reading `publish_synth` makes of an empty `stat -json`."""
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


# ---------------------------------------------------------------------------
# Two publishers, one artefact directory (#560)
# ---------------------------------------------------------------------------


def _serialisation_probe(monkeypatch, *, dwell=0.15):
    """Widen the critical section and record whether two ever share it.

    ``write_model`` is called from inside the lock by both writers here —
    the publish path and the withdrawal — and from nowhere else, so a
    counter around it sees exactly the critical sections. The dwell is what
    makes an unserialised pair actually collide: without the lock the second
    thread reads the pair while the first is between its two writes, which
    is the interleaving the finding is about.
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
    """Run each callable in its own thread; return their results in order."""
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
    """The finding (#560 round-11 review, Codex P1). A co-named `rb synth`
    and `rb power` publish into one artefact directory and each writes both
    documents. Unserialised, the two read the same pair and write in an
    interleaved order: crossed tokens, or a token-consistent pair from
    whichever finished last that silently drops the other's just-published
    half. Whichever order they arrive in, the directory must end up holding
    one pair carrying both halves."""
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
    """`invalidate_half` rewrites the same two documents, so it takes the same
    lock: a publish landing inside it would read a pair one of whose halves is
    already withdrawn and the other not."""
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
    # Whoever wrote last wrote a whole pair, and the synthesis half it
    # carries is this run's own — never a half-withdrawn document.
    assert model["publication"] == manifest["publication"]
    assert len(model["modules"]) == 2
    assert manifest["synth"]["backend"] == "yosys"
    # The withdrawal is not lost either way round: either it ran first and
    # the publish inherited nothing to carry the power half forward, or it
    # ran second and blanked what the publish had just written.
    assert model["instances"] is None
    assert all(manifest["power"][key] is None for key in POWER_KEYS)


def test_a_lock_that_cannot_be_taken_is_a_publish_error(tmp_path, monkeypatch):
    """The resilience rule reaches the lock too: a mutex this publisher
    cannot take costs the run its by-product and a warning, never the run."""
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
    """It lives beside the pair it guards and outlives the publish that made
    it, so the readers must ignore it and a suffix clear must spare it."""
    from rtl_buddy.tools.artifact_paths import (
        PHYS_PUBLISH_LOCK_NAME,
        PROTECTED_OUTPUT_PATTERNS,
    )

    _root, artefacts = _publish_both_halves_dir(tmp_path)

    assert (artefacts / PHYS_PUBLISH_LOCK_NAME).exists()
    assert PHYS_PUBLISH_LOCK_NAME in PROTECTED_OUTPUT_PATTERNS
    assert discover_manifests(artefacts) == [str(artefacts / MANIFEST_FILENAME)]


# ---------------------------------------------------------------------------
# identity — what shaped the run, beside what it measured (#568)
# ---------------------------------------------------------------------------


def test_a_power_publish_records_its_mode_and_the_activity_behind_it(tmp_path):
    """The whole point of the block: two runs of one netlist that differ
    only in stimulus are two measurements, and a µW figure with no mode
    beside it does not say which of the two it is."""
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
    # Derived from the artefact layout, not passed in: the producer holds
    # a path, and the test that wrote it finished in another command.
    assert recorded["activity"]["test"] == "csr_smoke"
    # And echoed into the manifest, so a listing of every run in a project
    # reads one small file each rather than a model apiece.
    block = load_manifest(published["manifest"])["power"]
    assert block["mode"] == "dynamic"
    assert block["activity"] == recorded["activity"]


def test_a_static_run_records_defaults_rather_than_a_toggle_rate(tmp_path):
    """A static analysis emits no activity command at all, so recording the
    config's toggle/duty pair beside it would claim a stimulus that never
    drove anything."""
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


def test_a_trace_outside_an_artefact_directory_names_no_test():
    """The derivation is from rtl_buddy's own layout. A checked-in golden
    trace sits in a directory that is not a test, and reporting its name as
    one would be an invention."""
    assert trace_test("verif/demo/artefacts/csr_smoke/dump.saif") == "csr_smoke"
    assert trace_test("golden/traces/dump.saif") is None
    assert trace_test("dump.saif") is None
    assert trace_test(None) is None


def test_a_synth_publish_records_the_configuration_that_shaped_it(tmp_path):
    """Platform, effort and constraints spelled out; the option set as a
    digest. Enough to read two experiments of one design apart without
    decoding their run names."""
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
        options={"strategy": "TIMING"},
    )

    config = load_manifest(published["manifest"])["synth"]["config"]
    assert config["platform"] == "nangate45"
    assert config["effort"] == "timing-opt"
    # Project-relative, like every other path in the document — and the
    # SAME spelling in the model, because both blocks are relativised once
    # by the publish rather than by each writer.
    assert config["constraints"] == "verif/demo/demo.sdc"
    assert load_model(published["model"])["provenance"]["synth"]["config"] == config
    assert config["constraints_sha256"] == hashlib.sha256(sdc.read_bytes()).hexdigest()
    assert config["options_sha256"] == options_digest({"strategy": "TIMING"})


def test_two_option_sets_that_resolve_the_same_digest_the_same():
    """The digest is over the effective values, canonically rendered, so
    key order is not an experiment and two spellings of one configuration
    are one."""
    assert options_digest({"a": 1, "b": 2}) == options_digest({"b": 2, "a": 1})
    assert options_digest({"strategy": "AREA"}) != options_digest(
        {"strategy": "TIMING"}
    )
    # Nothing recorded is `None`, not a digest of the empty mapping: a
    # backend that fingerprinted nothing must not look like one that did.
    assert options_digest(None) is None and options_digest({}) is None


def test_a_config_that_differs_does_not_stop_the_halves_from_merging(tmp_path):
    """Identity is for telling runs apart, never for gating the merge. The
    netlist hash decides that, and it is the stronger test — the same
    options can produce two netlists and two option sets can produce one."""
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
    # Each half keeps its OWN identity: the inherited block travels with
    # the rows it describes.
    assert model["provenance"]["synth"]["config"]["platform"] == "nangate45"
    assert model["provenance"]["power"]["config"]["platform"] == "sky130hd"


def test_withdrawing_a_half_withdraws_the_identity_that_went_with_it(tmp_path):
    """A failed rerun has deleted the artefacts behind its half, so the
    mode and activity that described them go too — leaving them would say
    this directory holds a dynamic-power measurement it no longer has."""
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
    """`mode` and `activity` are power concepts. A synthesis has neither,
    and a block of nulls saying so would be shape for its own sake."""
    assert set(BLOCK_PROVENANCE_KEYS["synth"]) == {"netlist_sha256", "config"}
    assert set(BLOCK_PROVENANCE_KEYS["power"]) == {
        "netlist_sha256",
        "config",
        "mode",
        "activity",
    }


def test_an_older_document_reads_back_as_nulls_not_as_a_missing_key(tmp_path):
    """Every reader normalises against the key table, so a model written
    before #568 answers "not recorded" rather than raising."""
    _root, artefacts = _project(tmp_path)
    model = build_power_model(top="demo_top", instances=[])
    del model["provenance"]["power"]["mode"]
    del model["provenance"]["power"]["activity"]
    write_model(model, artefacts)

    recorded = provenance_of(load_model(artefacts / MODEL_FILENAME))
    assert recorded["power"]["mode"] is None
    assert recorded["power"]["activity"] is None
    assert normalise_activity(recorded["power"]["activity"]) is None


# --- the xplr experiment a manifest sits under ------------------------------


def test_an_experiment_id_is_derived_from_the_ledger_path(tmp_path):
    """No read at all for the id: the ledger is one directory per
    experiment, so the path already says which one, and it says so for an
    experiment whose record has not been written yet."""
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
    """A malformed record is `rb xplr`'s to report; it is not a reason to
    stop identifying the experiment."""
    experiment = tmp_path / "artefacts" / "xplr" / "exp-0009"
    (experiment / "artefacts" / "s").mkdir(parents=True)
    (experiment / "record.json").write_text("{not json")
    found = experiment_for(experiment / "artefacts" / "s" / MANIFEST_FILENAME)
    assert found == {"id": "exp-0009", "label": None}


def test_the_ledgers_reserved_directories_are_not_experiments(tmp_path):
    """`artefacts/xplr/worktrees/` is the default worktree root, not an
    experiment called `worktrees`."""
    worktree = tmp_path / "artefacts" / "xplr" / "worktrees" / "wt"
    worktree.mkdir(parents=True)
    assert experiment_for(worktree / MANIFEST_FILENAME) is None


def test_a_manifest_outside_a_ledger_has_no_experiment(tmp_path):
    plain = tmp_path / "verif" / "demo" / "artefacts" / "demo_synth"
    plain.mkdir(parents=True)
    assert experiment_for(plain / MANIFEST_FILENAME) is None
