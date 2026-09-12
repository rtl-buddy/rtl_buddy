"""
Unit tests for the `rb phys` payload builders (#558).

These are the dicts the CLI prints under `--machine` and a later MCP tool
wraps verbatim, so they are asserted on directly rather than through the
CLI. The artefacts are written with the phase-1 producers rather than by
hand: a payload test that invented its own document shape would keep
passing after the producers stopped writing that shape.
"""

import json
import os
from pathlib import Path

import pytest

from rtl_buddy.phys import query as query_mod
from rtl_buddy.phys.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    build_manifest,
    write_manifest,
)
from rtl_buddy.phys.model import (
    MODEL_SCHEMA_VERSION,
    build_power_model,
    build_synth_model,
    merge_model,
    write_model,
)
from rtl_buddy.phys.query import (
    INSTANCE_JOIN_LIBERTY_ONLY,
    PHYS_QUERY_SCHEMA_VERSION,
    PUBLICATION_ATTEMPTS,
    PhysQueryError,
    heaviest_modules,
    hottest_instances,
    instance_payload,
    is_descendant,
    level_path,
    load_context,
    module_names,
    module_payload,
    resolve_manifest_path,
    summary_payload,
)

# The two halves' `module` columns are two namespaces: RTL module names
# on the synthesis rows, Liberty cell names on the leaves. The fixtures
# keep them apart, as a real mapped design does, so the collision case
# below is a case rather than the baseline.
MODULE_ROWS = [
    {"module": "blk", "cell_count": 120, "area_um2": 480.5},
    {"module": "sub", "cell_count": 40, "area_um2": 96.0},
    {"module": "tiny", "cell_count": 2, "area_um2": None},
]

INSTANCE_ROWS = [
    {
        "instance_path": "u_sub/_64_",
        "module": "DFF_X1",
        "leakage_uw": 0.079,
        "internal_uw": 2.28,
        "switching_uw": 0.0675,
        "total_uw": 2.42,
    },
    {
        "instance_path": "u_sub/u_leaf/_12_",
        "module": "INV_X1",
        "leakage_uw": 0.001,
        "internal_uw": 0.5,
        "switching_uw": 0.25,
        "total_uw": 0.751,
    },
    {
        "instance_path": "u_other/_9_",
        "module": "DFF_X1",
        "leakage_uw": 0.5,
        "internal_uw": 8.0,
        "switching_uw": 1.5,
        "total_uw": 10.0,
    },
]


#: The pathological but perfectly legal design: a Liberty cell named
#: exactly like one of the RTL modules. Both halves then have rows under
#: `sub`, measuring two different things.
COLLIDING_INSTANCE_ROWS = [
    {**row, "module": "sub"} for row in INSTANCE_ROWS if row["module"] == "DFF_X1"
]


#: Both halves of a fixture run record the same netlist hash, because
#: that is what a `rb synth` then `rb power` pair records and what the
#: merge requires before either half inherits the other (see
#: :func:`rtl_buddy.phys.model.may_inherit_other_half`).
_FIXTURE_NETLIST_SHA256 = "0" * 64


def _write_run(root, run, *, top="blk", modules=None, instances=None, mtime=None):
    """One run's artefact directory, written the way the producers do."""
    phys_dir = root / "verif" / "blk" / "artefacts" / run
    phys_dir.mkdir(parents=True, exist_ok=True)

    model = None
    if modules is not None:
        model = build_synth_model(
            top=top,
            modules=modules,
            area_um2=576.5,
            gate_count=162,
            netlist_sha256=_FIXTURE_NETLIST_SHA256,
        )
    if instances is not None:
        power = build_power_model(
            top=top,
            instances=instances,
            internal_w=10.78e-6,
            switching_w=1.8175e-6,
            leakage_w=0.58e-6,
            total_w=13.171e-6,
            netlist_sha256=_FIXTURE_NETLIST_SHA256,
        )
        model = (
            merge_model(model, power, own_half="instances")
            if model is not None
            else power
        )
    model_path = write_model(model, phys_dir)

    command = "power" if instances is not None else "synth"
    manifest = build_manifest(
        project_root=root,
        phys_dir=phys_dir,
        command=command,
        run=run,
        top=top,
        model_path=model_path,
        totals=model["totals"],
        synth=(
            None
            if modules is None
            else {
                "backend": "yosys",
                "run": run,
                "stats": phys_dir / "synth_stat.json",
                "netlist": phys_dir / "synth_netlist.v",
                "log": phys_dir / "synth.log",
            }
        ),
        power=(
            None
            if instances is None
            else {
                "backend": "openroad",
                "run": run,
                "netlist_source": "synth",
                "report": phys_dir / "power.rpt",
                "instances": phys_dir / "power_instances.rpt",
                "cells": phys_dir / "power_instances.cells",
                "log": phys_dir / "power.log",
            }
        ),
    )
    manifest_path = write_manifest(manifest, phys_dir)
    if mtime is not None:
        os.utime(manifest_path, (mtime, mtime))
    return phys_dir


@pytest.fixture
def project(tmp_path):
    """A project with two runs: a complete model and an older synth-only one."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write_run(root, "old_synth", modules=MODULE_ROWS, mtime=1_000_000)
    _write_run(
        root,
        "collision",
        modules=MODULE_ROWS,
        instances=COLLIDING_INSTANCE_ROWS,
        mtime=1_500_000,
    )
    _write_run(
        root, "both", modules=MODULE_ROWS, instances=INSTANCE_ROWS, mtime=2_000_000
    )
    return root


def _collision_context(project):
    """The run whose Liberty cell is named after one of the RTL modules."""
    return load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "collision"
    )


# --- discovery --------------------------------------------------------------


def test_discovery_picks_the_newest_manifest(project):
    """No override means the last run that measured anything."""
    ctx = load_context(project)

    assert ctx.manifest["run"] == "both"


def test_phys_dir_overrides_discovery(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    assert ctx.manifest["run"] == "old_synth"


def test_explicit_manifest_beats_phys_dir(project):
    """The most specific request wins, even when both are given."""
    newest = project / "verif" / "blk" / "artefacts" / "both" / MANIFEST_FILENAME

    ctx = load_context(
        project,
        phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth",
        manifest=newest,
    )

    assert ctx.manifest["run"] == "both"


def test_a_manifest_option_naming_a_directory_is_accepted(project):
    ctx = load_context(
        project, manifest=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    assert ctx.manifest["run"] == "old_synth"


def _stamp(path, token):
    """Set one document's `publication` token in place."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document["publication"] = token
    path.write_text(json.dumps(document), encoding="utf-8")


def test_a_matched_pair_is_read_once(project, monkeypatch):
    """The ordinary case: the two documents agree, so nothing re-reads."""
    for run in ("old_synth", "both"):
        phys_dir = project / "verif" / "blk" / "artefacts" / run
        _stamp(phys_dir / "phys-model.json", "pub-1")
        _stamp(phys_dir / MANIFEST_FILENAME, "pub-1")
    monkeypatch.setattr(
        query_mod.time, "sleep", lambda _s: pytest.fail("a matched pair never sleeps")
    )

    ctx = load_context(project)

    assert ctx.model["publication"] == ctx.manifest["publication"] == "pub-1"


def test_a_pair_caught_mid_publication_is_read_again(project, monkeypatch):
    """A publish replaces the model and then the manifest, so a read can land
    between the two and pair a new manifest with the old model. The tokens
    make that visible; the retry is what resolves it."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    model_path = phys_dir / "phys-model.json"
    manifest_path = phys_dir / MANIFEST_FILENAME
    _stamp(model_path, "pub-1")
    _stamp(manifest_path, "pub-2")

    slept = []

    def _the_publish_finishes(seconds):
        slept.append(seconds)
        _stamp(model_path, "pub-2")

    monkeypatch.setattr(query_mod.time, "sleep", _the_publish_finishes)

    ctx = load_context(project)

    assert len(slept) == 1
    assert ctx.model["publication"] == ctx.manifest["publication"] == "pub-2"
    # And the answer is a real one, not a half-loaded document.
    assert len(ctx.model["instances"]) == 3


def test_a_pair_that_never_matches_is_still_answered(project, monkeypatch):
    """An advisory read: nothing here holds a lock, so two documents that
    genuinely disagree are answered from the freshest read of each rather
    than refused."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _stamp(phys_dir / "phys-model.json", "pub-1")
    _stamp(phys_dir / MANIFEST_FILENAME, "pub-2")
    reads = []
    real_load_model = query_mod.load_model

    def _counted(path):
        reads.append(str(path))
        return real_load_model(path)

    monkeypatch.setattr(query_mod, "load_model", _counted)
    monkeypatch.setattr(query_mod.time, "sleep", lambda _s: None)

    ctx = load_context(project)

    assert len(reads) == PUBLICATION_ATTEMPTS
    assert ctx.model["publication"] == "pub-1"
    assert ctx.manifest["publication"] == "pub-2"
    assert summary_payload(ctx)["run"] == "both"


def test_missing_manifest_names_the_commands_that_write_one(tmp_path):
    with pytest.raises(PhysQueryError) as excinfo:
        resolve_manifest_path(tmp_path)

    message = str(excinfo.value)
    assert MANIFEST_FILENAME in message
    assert "rb synth" in message and "rb power" in message


def test_a_named_directory_without_a_manifest_says_so(project):
    empty = project / "verif" / "blk" / "artefacts" / "nothing"
    empty.mkdir()

    with pytest.raises(PhysQueryError) as excinfo:
        resolve_manifest_path(project, phys_dir=empty)

    assert "run `rb synth` or `rb power` there first" in str(excinfo.value)


def test_a_manifest_naming_a_vanished_model_is_an_error(project):
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    (phys_dir / "phys-model.json").unlink()

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    assert "names no physical model" in str(excinfo.value)


def _restamp(path, schema_version):
    """Rewrite one document's ``schema_version`` in place."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if schema_version is None:
        document.pop("schema_version", None)
    else:
        document["schema_version"] = schema_version
    Path(path).write_text(json.dumps(document), encoding="utf-8")


def test_a_manifest_from_a_future_rtl_buddy_is_refused(project):
    """Every payload reads the manifest's blocks by name, so a document whose
    shape this build does not know would be answered from whatever keys
    happened to survive the change — wrong rather than absent."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _restamp(phys_dir / MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION + 1)

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    message = str(excinfo.value)
    assert f"schema_version {MANIFEST_SCHEMA_VERSION + 1}" in message
    assert f"reads {MANIFEST_SCHEMA_VERSION}" in message
    assert "upgrade rtl-buddy" in message


def test_a_model_from_a_future_rtl_buddy_is_refused(project):
    """The manifest is only half the read; the model carries its own version
    and the payloads index into its blocks just as directly."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _restamp(phys_dir / "phys-model.json", MODEL_SCHEMA_VERSION + 1)

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    message = str(excinfo.value)
    assert "phys-model.json" in message
    assert f"schema_version {MODEL_SCHEMA_VERSION + 1}" in message
    assert f"reads {MODEL_SCHEMA_VERSION}" in message


def test_a_document_older_than_this_build_is_refused_too(project):
    """Not only the newer direction: an older document is missing keys this
    build treats as guaranteed, and the message names both versions so the
    user can tell which way to move."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _restamp(phys_dir / MANIFEST_FILENAME, MANIFEST_SCHEMA_VERSION - 1)

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    assert "re-run `rb synth` or `rb power`" in str(excinfo.value)


def test_a_document_with_no_schema_version_is_refused(project):
    """Both producers have written the key since version 1, so its absence
    says this is not one of these documents rather than that it is early."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _restamp(phys_dir / "phys-model.json", None)

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    assert "schema_version (absent)" in str(excinfo.value)


@pytest.mark.parametrize(
    "root_json, described",
    [
        ("null", "null"),
        ("[]", "an array"),
        ('"a string"', "a string"),
        ("7", "a number"),
    ],
)
@pytest.mark.parametrize("document", [MANIFEST_FILENAME, "phys-model.json"])
def test_a_document_whose_json_root_is_not_an_object_is_refused(
    project, document, root_json, described
):
    """The finding (#561 review, Codex P2). Every payload indexes the root by
    key, so a list or a `null` would raise `AttributeError` straight past
    `PhysQueryError` — and past the machine envelope that is the only thing
    an agent sees. Both documents, every non-object root."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    (phys_dir / document).write_text(root_json, encoding="utf-8")

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    message = str(excinfo.value)
    assert document in message
    assert described in message
    assert "is an object" in message


def _rewrite_field(path, field, value):
    """Replace one nested field of a document that is otherwise well formed."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    document[field] = value
    Path(path).write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    "document, field, malformed, described",
    [
        # The manifest's producer blocks. `[]` is the shape an emptied
        # block is most likely to be hand-edited into, and it is the one
        # `document.get("synth") or {}` silently survives while a
        # non-empty list does not.
        (MANIFEST_FILENAME, "synth", [], "an array"),
        (MANIFEST_FILENAME, "power", ["openroad"], "an array"),
        (MANIFEST_FILENAME, "power", "openroad", "a string"),
        (MANIFEST_FILENAME, "totals", 7, "a number"),
        (MANIFEST_FILENAME, "model", ["phys-model.json"], "an array"),
        # `phys_dir` is not indexed by any payload — it is dereferenced
        # one level down, by `project_root_for`, which calls
        # `os.path.isabs` on it and then walks its `parts`. A list
        # reached both and raised `TypeError` past the envelope.
        (MANIFEST_FILENAME, "phys_dir", ["artefacts"], "an array"),
        (MANIFEST_FILENAME, "phys_dir", 7, "a number"),
        # The model's two halves and the totals beside them.
        ("phys-model.json", "modules", 7, "a number"),
        ("phys-model.json", "modules", {"blk": 120}, "an object"),
        ("phys-model.json", "instances", "u_sub/_64_", "a string"),
        (
            "phys-model.json",
            "instances",
            ["u_sub/_64_"],
            "an array whose rows are not all objects",
        ),
        ("phys-model.json", "totals", "x", "a string"),
        ("phys-model.json", "units", [], "an array"),
    ],
)
def test_a_document_whose_blocks_are_the_wrong_shape_is_refused(
    project, document, field, malformed, described
):
    """The finding (#561 review, Codex P2). A mapping root and a supported
    `schema_version` say the document is one of these; neither says its
    blocks are the shapes the builders index them as. Each of these used to
    reach a `len()` on a number or a `.get` on a string deep inside a
    payload — a `TypeError` or an `AttributeError` past `PhysQueryError`
    and past the machine envelope."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _rewrite_field(phys_dir / document, field, malformed)

    with pytest.raises(PhysQueryError) as excinfo:
        load_context(project)

    message = str(excinfo.value)
    assert document in message
    assert f"`{field}`" in message
    assert described in message


def test_a_half_that_is_null_is_not_a_malformed_half(project):
    """The refusal is about shape, not about absence: a model half is `null`
    on every one-sided run, and that is a state the payloads report."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _rewrite_field(phys_dir / "phys-model.json", "instances", None)
    _rewrite_field(phys_dir / MANIFEST_FILENAME, "power", None)

    payload = summary_payload(load_context(project))

    assert payload["missing_halves"] == ["instances"]


def test_an_empty_half_is_not_a_malformed_half_either(project):
    """`[]` is a design with no rows measured, which is an answer; only a
    list whose rows are not objects is unreadable."""
    phys_dir = project / "verif" / "blk" / "artefacts" / "both"
    _rewrite_field(phys_dir / "phys-model.json", "instances", [])

    payload = summary_payload(load_context(project))

    assert payload["counts"]["instances"] == 0
    assert payload["missing_halves"] == []


# --- summary ----------------------------------------------------------------


def test_summary_reports_the_header_totals_and_both_rankings(project):
    payload = summary_payload(load_context(project))

    assert payload["schema_version"] == PHYS_QUERY_SCHEMA_VERSION
    assert payload["run_command"] == "power"
    assert payload["run"] == "both"
    assert payload["top"] == "blk"
    assert payload["backends"] == {"synth": "yosys", "power": "openroad"}
    assert payload["manifest"] == "verif/blk/artefacts/both/phys-manifest.json"
    assert payload["model"] == "verif/blk/artefacts/both/phys-model.json"
    assert payload["units"] == {"area": "um2", "power": "uW"}
    # The synth totals are the log scrape, the power totals the watts the
    # flow reported, scaled. Neither is a sum of the rows.
    assert payload["totals"]["cell_count"] == 162
    assert payload["totals"]["total_uw"] == pytest.approx(13.171)
    assert payload["counts"] == {"modules": 3, "instances": 3}
    assert [row["module"] for row in payload["modules"]] == ["blk", "sub", "tiny"]
    assert [row["instance_path"] for row in payload["instances"]] == [
        "u_other/_9_",
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]
    assert payload["artefacts"]["synth_netlist"] == (
        "verif/blk/artefacts/both/synth_netlist.v"
    )
    assert payload["artefacts"]["power_instances"] == (
        "verif/blk/artefacts/both/power_instances.rpt"
    )


def test_summary_limit_truncates_both_rankings(project):
    payload = summary_payload(load_context(project), limit=1)

    assert len(payload["modules"]) == 1
    assert len(payload["instances"]) == 1
    assert payload["limit"] == 1


def test_summary_of_a_synth_only_model_names_the_missing_half(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    payload = summary_payload(ctx)

    assert payload["missing_halves"] == ["instances"]
    assert payload["halves"]["instances"] == {
        "present": False,
        "rows": None,
        "produced_by": "rb power",
    }
    assert payload["halves"]["modules"]["rows"] == 3
    assert payload["counts"]["instances"] is None
    assert payload["instances"] == []
    assert payload["backends"]["power"] is None


def test_rankings_sink_the_rows_nobody_measured():
    """A `null` metric is unmeasured, not smallest — it goes last."""
    model = {
        "modules": [
            {"module": "unmapped", "cell_count": None, "area_um2": None},
            {"module": "small", "cell_count": 1, "area_um2": 1.0},
        ],
        "instances": [
            {"instance_path": "b", "total_uw": None},
            {"instance_path": "a", "total_uw": 0.0},
        ],
    }

    assert [row["module"] for row in heaviest_modules(model)] == ["small", "unmapped"]
    assert [row["instance_path"] for row in hottest_instances(model)] == ["a", "b"]


# --- module -----------------------------------------------------------------


def test_module_payload_sums_the_power_of_a_liberty_cells_instances(project):
    """A cell type's question — "what do all the DFFs burn" — is the one
    the power half can answer on its own, and the synthesis half has no
    row to add to it."""
    payload = module_payload(load_context(project), "DFF_X1")

    assert payload["module"] == "DFF_X1"
    assert payload["namespaces"] == ["liberty"]
    assert payload["row"] is None
    assert [row["instance_path"] for row in payload["instances"]] == [
        "u_other/_9_",
        "u_sub/_64_",
    ]
    assert payload["instance_count"] == 2
    assert payload["power"]["total_uw"] == pytest.approx(12.42)
    assert payload["power"]["leakage_uw"] == pytest.approx(0.579)


def test_module_payload_lists_every_instance_by_default(project):
    """The finding (#561 review, Codex P2). No limit means the complete
    list, which is what the MCP tools -- who pass none -- were registered
    with, and `limit` says so rather than leaving it to be inferred."""
    payload = module_payload(load_context(project), "DFF_X1")

    assert payload["limit"] is None
    assert len(payload["instances"]) == payload["instance_count"] == 2


def test_module_payload_heads_its_instances_at_the_limit(project):
    """The finding (#561 review, Codex P2). `--limit 1` used to head the
    console table while the machine payload carried every row, so the flag
    was silently ignored by the only surface that cannot re-count."""
    payload = module_payload(load_context(project), "DFF_X1", limit=1)

    assert [row["instance_path"] for row in payload["instances"]] == ["u_other/_9_"]
    assert payload["limit"] == 1
    # Self-describing: the count is how many there are, not how many are
    # listed, and the power is still the whole cell type's.
    assert payload["instance_count"] == 2
    assert payload["power"]["total_uw"] == pytest.approx(12.42)


def test_a_zero_module_limit_means_every_instance(project):
    """`0` is what the flag documents as "all", so the builder reads it
    the way the rankings already do rather than as an empty list."""
    payload = module_payload(load_context(project), "DFF_X1", limit=0)

    assert len(payload["instances"]) == 2
    assert payload["limit"] == 0


def test_module_payload_reports_an_rtl_modules_own_row(project):
    payload = module_payload(load_context(project), "sub")

    assert payload["namespaces"] == ["rtl"]
    assert payload["row"] == {"module": "sub", "cell_count": 40, "area_um2": 96.0}


def test_a_name_in_both_namespaces_is_reported_as_a_collision(project):
    """The finding (#561 review, Codex P2). `sub` is an RTL module *and* a
    Liberty cell here, so the payload holds a module's cells and area beside
    an unrelated cell type's power. Both are real; what would be false is
    presenting them as one module's totals, so the payload names both
    namespaces and the join note says what happened."""
    payload = module_payload(_collision_context(project), "sub")

    assert payload["namespaces"] == ["rtl", "liberty"]
    assert payload["instance_join"] == query_mod.INSTANCE_JOIN_NAME_COLLISION
    assert "name collision" in payload["instance_join"]
    # Neither half is dropped or folded into the other — the note is what
    # keeps the pairing from being read as one measurement.
    assert payload["row"] == {"module": "sub", "cell_count": 40, "area_um2": 96.0}
    assert payload["instance_count"] == 2


def test_a_collision_note_outranks_the_liberty_only_note(project):
    """Both conditions can be true of one name only in the collision case,
    and it is the one the reader would not otherwise suspect: that payload
    looks complete."""
    ctx = _collision_context(project)

    payload = module_payload(ctx, "blk")
    assert payload["namespaces"] == ["rtl"]
    assert payload["instance_join"] == INSTANCE_JOIN_LIBERTY_ONLY


def test_module_names_span_both_halves(project):
    """A liberty cell only the power half knows is still askable."""
    ctx = load_context(project)
    ctx.model["modules"] = [{"module": "blk", "cell_count": 1, "area_um2": None}]

    assert module_names(ctx.model) == ["DFF_X1", "INV_X1", "blk"]
    assert query_mod.namespaces_of(ctx.model, "DFF_X1") == ["liberty"]
    assert query_mod.namespaces_of(ctx.model, "blk") == ["rtl"]


def test_module_name_matching_is_case_insensitive(project):
    assert module_payload(load_context(project), "SUB")["module"] == "sub"


def test_module_payload_over_a_synth_only_model_has_no_instances(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    payload = module_payload(ctx, "sub")

    assert payload["instances"] is None
    assert payload["power"] is None
    assert payload["missing_halves"] == ["instances"]


def test_a_module_only_the_synth_half_knows_says_the_join_cannot_see_it(project):
    """ "No instances" and "the join misses" are different answers.

    `blk` is the top: the synthesis half has a row for it, and the power
    half — which is populated — names Liberty cells on its leaves, so
    nothing can ever match. Without the note the empty table reads as
    "the top burns no power".
    """
    payload = module_payload(load_context(project), "blk")

    assert payload["instances"] == []
    assert payload["instance_count"] == 0
    assert payload["instance_join"] == INSTANCE_JOIN_LIBERTY_ONLY
    assert "liberty-cell names only" in payload["instance_join"]


def test_a_module_the_join_does_reach_carries_no_note(project):
    """A Liberty cell name matches, so there is nothing to qualify."""
    assert module_payload(load_context(project), "DFF_X1")["instance_join"] is None


def test_a_synth_only_model_carries_no_join_note(project):
    """With no power half at all, `missing_halves` is the honest signal
    and a join note would only compete with it."""
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    assert module_payload(ctx, "blk")["instance_join"] is None


def test_unknown_module_reports_near_misses(project):
    with pytest.raises(PhysQueryError) as excinfo:
        module_payload(load_context(project), "subb")

    assert "sub" in excinfo.value.candidates


def test_an_unknown_module_on_a_half_model_names_the_missing_command(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    with pytest.raises(PhysQueryError) as excinfo:
        module_payload(ctx, "nowhere")

    assert "run `rb power`" in str(excinfo.value)


# --- instance ---------------------------------------------------------------


def test_instance_payload_answers_an_exact_leaf(project):
    payload = instance_payload(load_context(project), "u_sub/_64_")

    assert payload["match"] == "exact"
    assert payload["instance"]["module"] == "DFF_X1"
    assert payload["children"] == []
    assert payload["rollup"]["instances"] == 1
    assert payload["rollup"]["total_uw"] == pytest.approx(2.42)


def test_instance_payload_rolls_up_a_subtree_prefix(project):
    payload = instance_payload(load_context(project), "u_sub")

    assert payload["match"] == "prefix"
    assert payload["instance"] is None
    assert [row["instance_path"] for row in payload["children"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]
    assert payload["rollup"]["instances"] == 2
    assert payload["rollup"]["total_uw"] == pytest.approx(3.171)


def test_instance_payload_lists_every_child_by_default(project):
    """The finding (#561 review, Codex P2), the subtree half of it."""
    payload = instance_payload(load_context(project), "u_sub")

    assert payload["limit"] is None
    assert len(payload["children"]) == payload["child_count"] == 2


def test_instance_payload_heads_its_children_at_the_limit(project):
    """The finding (#561 review, Codex P2). The rollup is the subtree's,
    not the listed rows': a total that changed with `--limit` would be a
    different number for the same question."""
    payload = instance_payload(load_context(project), "u_sub", limit=1)

    assert [row["instance_path"] for row in payload["children"]] == ["u_sub/_64_"]
    assert payload["limit"] == 1
    assert payload["child_count"] == 2
    assert payload["rollup"]["instances"] == 2
    assert payload["rollup"]["total_uw"] == pytest.approx(3.171)


def test_a_zero_instance_limit_means_every_child(project):
    payload = instance_payload(load_context(project), "u_sub", limit=0)

    assert len(payload["children"]) == 2
    assert payload["limit"] == 0


def test_a_prefix_must_end_on_a_separator(project):
    """`u_sub` must not claim `u_subsystem` — a different block."""
    assert is_descendant("u_sub/_64_", "u_sub")
    assert is_descendant("u_sub.x", "u_sub")
    assert not is_descendant("u_subsystem/_1_", "u_sub")
    assert not is_descendant("u_sub", "u_sub")


def test_descendancy_is_decided_on_levelled_paths():
    """Which separator a path is spelled with is the tool's choice, so it
    cannot be allowed to decide whether two paths are the same instance."""
    assert level_path("u_top.u_sub/_64_") == "u_top/u_sub/_64_"
    assert is_descendant("u_top/u_sub/_64_", "u_top.u_sub")
    assert is_descendant("u_top.u_sub._64_", "u_top/u_sub")
    # The boundary rule survives levelling.
    assert not is_descendant("u_subsystem._1_", "u_sub")


def test_a_dotted_query_finds_slash_stored_rows(project):
    """The model stores OpenSTA's `/`; the pane and the RTL side spell the
    same path with `.`, and a user pasting one must still get the row."""
    ctx = load_context(project)

    exact = instance_payload(ctx, "u_sub._64_")
    assert exact["match"] == "exact"
    assert exact["instance"]["instance_path"] == "u_sub/_64_"
    # The payload echoes what the user asked, not the model's spelling.
    assert exact["instance_path"] == "u_sub._64_"

    subtree = instance_payload(ctx, "u_sub")
    assert [row["instance_path"] for row in subtree["children"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]


def test_a_slash_query_finds_dot_stored_rows(project):
    ctx = load_context(project)
    ctx.model["instances"] = [
        {"instance_path": "u_top.u_sub._64_", "module": "sub", "total_uw": 1.0}
    ]

    payload = instance_payload(ctx, "u_top/u_sub")

    assert payload["match"] == "prefix"
    assert [row["instance_path"] for row in payload["children"]] == ["u_top.u_sub._64_"]


def test_unknown_instance_reports_near_misses(project):
    with pytest.raises(PhysQueryError) as excinfo:
        instance_payload(load_context(project), "u_sub/_65_")

    assert "u_sub/_64_" in excinfo.value.candidates


def test_instance_over_a_synth_only_model_names_the_power_command(project):
    ctx = load_context(
        project, phys_dir=project / "verif" / "blk" / "artefacts" / "old_synth"
    )

    with pytest.raises(PhysQueryError) as excinfo:
        instance_payload(ctx, "u_sub")

    assert "run `rb power`" in str(excinfo.value)
