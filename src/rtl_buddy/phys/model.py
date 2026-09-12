# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The structured physical model (#558, delivering #114).

One versioned JSON document describing what a synthesis or a power
analysis measured, built from artefacts already on disk. Its shape is
**backend-agnostic** — a module has cells and area, an instance has
leakage and dynamic components — even though Yosys' ``stat -json`` and
OpenSTA's ``report_power`` are the only producers today.

Three properties are the point of the exercise:

**Per module and per instance, not per design.** Both flows already
compute the breakdown on their way to the four scalars they report;
recording it means "which module owns the area" and "which instance owns
the leakage" are a read rather than a re-run with a hand-written script.

**Leaf values only.** ``instances`` holds leaf instances and ``modules``
holds one row per module as the tool saw it. Hierarchical roll-up is the
consumer's job — the same rule #114 set, and the same reason: a subtree
sum depends on which hierarchy the consumer is projecting onto, and the
producer does not know.

**Totals are a sanity block, not a derivation.** ``totals`` carries the
numbers the flows *already* parse out of their logs — Yosys' chip area
and cell count, ``report_power``'s ``Total`` row. They come from a
different scrape than the rows do, so a totals-versus-sum mismatch is
information rather than a tautology.

A run fills only its own half. A synthesis writes ``modules`` and leaves
``instances`` null; a power analysis does the reverse. ``null`` means
"this run did not produce it" — absent never does, per the same
stable-keys rule the coverage manifest keeps.

**Merging.** When a model for the same top already exists in the
artefact directory, the half the new run does not *own* is carried
forward from it rather than being clobbered to null
(:func:`merge_model`). That is what makes a `rb synth` and a `rb power`
run configured into one artefact directory add up to a complete
document, in either order.

**One rule, in both directions.** A half is inherited only when the
netlist it was measured on is the netlist this run measured. Both sides
record that netlist's sha256 in ``provenance`` (:data:`PROVENANCE_KEYS`)
and the comparison is of recorded hashes — not of timestamps, and not of
the fact that the rows happen to be sitting in the directory
(:func:`may_inherit_other_half`). Editing the RTL and re-running
`rb synth` must not carry per-instance watts forward onto a netlist that
no longer exists; and, the other way round, a `rb power` run measuring a
netlist some other build has since replaced must not republish module
rows that were counted off a netlist it never read. Missing provenance
on either side is not a match — an old document, an unreadable netlist,
or a power run whose ``netlist-source`` was the routed database rather
than a netlist all drop the half, which is the safe direction.

The two *flows* stay asymmetric even though the check no longer is, and
that asymmetry is now an argument about how often the check passes, not
about whether it is made. A power analysis runs *against* the synthesis
output, so in the ordinary `rb synth` then `rb power` pair the two
hashes are of one file and the ``modules`` half travels; a synthesis
inherits the other way, against a netlist it has just overwritten, so
that is the direction where the check routinely refuses. Making it
unconditional there would have been a shortcut on the usual case: a
power run whose recorded hash does not match the synthesis provenance
beside it measured a *different* netlist, and republishing the local
module rows under it would describe a design that run never saw.

The half the run does own is never inherited, even when this run failed
to produce it: a synthesis whose ``stat -json`` was unreadable writes
``modules: null`` under its own fresh totals rather than republishing
the previous run's rows, which would
otherwise read as a breakdown of a design that has since changed. A
different top means a different design, so the old model is replaced
outright — carrying rows across would attribute one design's instances
to another's modules.

**Invalidation.** The half a run owns is republished only when that run
gets far enough to publish. A rerun that fails earlier has already
deleted the raw artefacts behind the previous run's half, so leaving
that half in place would leave a measurement discoverable whose evidence
is gone — :func:`rtl_buddy.phys.publish.invalidate_half` nulls it
(:func:`blank_half`) at clear time instead, and the other half stays.

**Publication token.** The model and the manifest are two files written
one after the other, so a reader can pair a fresh model with a stale
manifest. Both carry the same ``publication`` token
(:func:`new_publication`), which is how a reader tells a pair it caught
mid-rewrite from one written together — and, on the *writing* side, how
the next publish tells a directory it may merge onto from one whose last
publish did not finish: an unpaired model and manifest are inherited
from neither half, and the run rewrites its own half under a fresh token
(:func:`rtl_buddy.phys.publish._existing_pair`).

The model is written to ``<artefact dir>/phys-model.json`` and pointed
at by ``<artefact dir>/phys-manifest.json``.
"""

from __future__ import annotations

import json
import os
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: Bumped when the document's shape changes incompatibly.
MODEL_SCHEMA_VERSION = 1

#: Filename inside the producing run's artefact directory.
# Defined in `tools.artifact_paths` — the bottom of the import graph, and
# where the artefact-clearing helpers protect it from a co-named run's
# suffix clear (#469). Re-exported here, where consumers already look.
from ..tools.artifact_paths import (  # noqa: E402
    PHYS_MODEL_NAME as MODEL_FILENAME,
)

#: The unit every value in the document is in. Recorded rather than
#: implied because the producers disagree: Yosys reports area in whatever
#: the Liberty's ``area`` unit is (µm² for every PDK rtl_buddy ships) and
#: OpenSTA reports power in watts, which the readers scale to µW so a
#: leakage figure is legible without an exponent.
UNITS = {"area": "um2", "power": "uW"}

#: Every key of the totals block, so a half-filled run still writes them
#: all. Stable keys, ``null`` for "not measured by this run".
TOTALS_KEYS = (
    "area_um2",
    "cell_count",
    "internal_uw",
    "switching_uw",
    "leakage_uw",
    "total_uw",
)

#: What each half records about the netlist it measured. One key today,
#: named as a tuple for the same reason :data:`TOTALS_KEYS` is: a half
#: that recorded nothing still writes it, ``null``.
PROVENANCE_KEYS = ("netlist_sha256",)

#: The two halves of the document, the totals each one owns, and the
#: ``provenance`` block each one fills. Named once because the merge and
#: the blanking both walk them and would otherwise re-spell the pairing
#: at every step.
_SYNTH_TOTALS = ("area_um2", "cell_count")
_POWER_TOTALS = ("internal_uw", "switching_uw", "leakage_uw", "total_uw")
_HALVES = (
    ("modules", _SYNTH_TOTALS, "synth"),
    ("instances", _POWER_TOTALS, "power"),
)


#: The provenance block each half's producer fills, and the one the
#: *other* producer fills — the two ends of the hash comparison
#: :func:`may_inherit_other_half` makes. Derived from :data:`_HALVES` so
#: the pairing cannot be re-spelled and drift.
_PROVENANCE_BLOCK = {half: block for half, _totals, block in _HALVES}
_OTHER_PROVENANCE_BLOCK = {
    half: block
    for half, _totals, _block in _HALVES
    for other, _t, block in _HALVES
    if other != half
}

#: Watts in, microwatts out — see :data:`UNITS`.
_W_TO_UW = 1e6


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def _empty_totals() -> dict:
    return {key: None for key in TOTALS_KEYS}


def _empty_provenance() -> dict:
    return {
        block: {key: None for key in PROVENANCE_KEYS}
        for _half, _totals, block in _HALVES
    }


def new_publication() -> str:
    """A fresh publication token, identifying one model+manifest write.

    The model and the manifest that names it are written as two files,
    one after the other, so a reader can arrive between the two writes
    and pair a new model with the old manifest — each document is
    atomic (see :func:`write_model`) but the *pair* is not. Both halves
    of a publication carry the same token, which is what lets
    :func:`rtl_buddy.phys.query.load_context` notice it caught the pair
    mid-rewrite and read again.

    Opaque and never compared for order: it answers "were these two
    written together", not "which is newer". Both readers ask exactly
    that and act on the answer differently:
    :func:`rtl_buddy.phys.query.load_context` re-reads and, still
    mismatched, answers from the freshest read of each, because it holds
    no lock and refusing would be worse; the publish path
    (:func:`rtl_buddy.phys.publish._existing_pair`) merges onto neither
    document, because it is about to write both and would otherwise
    stamp its own token on a pair that was never written together.
    """
    return uuid.uuid4().hex


def _base(top: str | None) -> dict:
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        # Filled by `phys.publish._publish`, which stamps the same token
        # into the manifest it writes beside this. Null in a document
        # built but never published — the builders below do not know
        # which publication they will end up in.
        "publication": None,
        "generator": _generator(),
        "design": {"top": top},
        "units": dict(UNITS),
        # What each half was measured on, filled by that half's producer;
        # see the merging note above for the one decision it drives.
        "provenance": _empty_provenance(),
        "totals": _empty_totals(),
        "modules": None,
        "instances": None,
    }


def build_synth_model(
    *,
    top: str | None,
    modules=None,
    area_um2: float | None = None,
    gate_count: int | None = None,
    netlist_sha256: str | None = None,
) -> dict:
    """The synthesis half: per-module rows plus the design totals.

    :param top: the synthesised top module.
    :param modules: rows from :func:`rtl_buddy.phys.reports.parse_stat_json`.
        ``None`` when the ``stat -json`` step produced nothing readable —
        the flow still writes a model, so the totals it *did* parse are
        recorded and the shape says plainly that the breakdown is missing.
    :param area_um2: the design area the flow scraped from its log.
    :param gate_count: the design cell count the flow scraped from its log.
    :param netlist_sha256: the hash of the netlist this synthesis wrote,
        which is what a power half already in the directory has to have
        been measured on to be inherited — see :func:`merge_model`.
        ``None`` when the flow wrote no netlist this could read.
    """
    model = _base(top)
    model["provenance"]["synth"]["netlist_sha256"] = netlist_sha256
    model["modules"] = None if modules is None else [dict(row) for row in modules]
    model["totals"]["area_um2"] = area_um2
    model["totals"]["cell_count"] = gate_count
    return model


def build_power_model(
    *,
    top: str | None,
    instances=None,
    internal_w: float | None = None,
    switching_w: float | None = None,
    leakage_w: float | None = None,
    total_w: float | None = None,
    netlist_sha256: str | None = None,
) -> dict:
    """The power half: per-instance rows plus the design totals.

    The totals arrive in watts because that is what ``report_power``
    prints and what ``PowerPassResults`` already carries; they are scaled
    to µW here so the block and the rows agree with :data:`UNITS`.

    :param instances: rows from
        :func:`rtl_buddy.phys.reports.parse_instance_power`, or ``None``
        when the per-instance report was not produced or not readable.
    :param netlist_sha256: the hash of the netlist this analysis read, so
        a later synthesis into the same directory can tell whether these
        rows still describe what it just wrote. ``None`` when the run
        read something that is not a netlist at all — a post-PnR routed
        database — or when the file could not be hashed.
    """
    model = _base(top)
    model["provenance"]["power"]["netlist_sha256"] = netlist_sha256
    model["instances"] = None if instances is None else [dict(row) for row in instances]
    model["totals"]["internal_uw"] = _to_uw(internal_w)
    model["totals"]["switching_uw"] = _to_uw(switching_w)
    model["totals"]["leakage_uw"] = _to_uw(leakage_w)
    model["totals"]["total_uw"] = _to_uw(total_w)
    return model


def _to_uw(watts: float | None) -> float | None:
    return None if watts is None else watts * _W_TO_UW


def merge_model(existing: dict | None, new: dict, *, own_half: str | None) -> dict:
    """Fold ``new`` onto an ``existing`` model for the same top.

    ``own_half`` names the half the producing command owns — ``"modules"``
    for a synthesis, ``"instances"`` for a power analysis — and that half
    is *never* inherited. It is required rather than inferred, because
    the thing to infer it from is exactly the thing that goes wrong: a
    synthesis whose ``stat -json`` was unreadable produces
    ``modules = None``, which is indistinguishable by shape from "this
    run does not fill that half", and inheriting there would republish
    the *previous* run's module rows underneath this run's totals. A
    rerun of the flow that owns a half must be able to shrink it to
    nothing — ``None`` stays ``None``, with the half's totals keys as
    this run wrote them, and the reader sees "this run did not produce
    it" rather than a stale breakdown.

    Only the *other* half is carried forward, together with the totals
    and the ``provenance`` entry that belong to it. Pass ``own_half=None``
    only for a fold with no producer behind it (two documents being
    combined after the fact); then any half ``new`` left null is
    inheritable.

    The other half has one more condition to meet, whichever half it is:
    it must have been measured on the netlist this run measured
    (:func:`may_inherit_other_half`). It was produced by an earlier
    command, and nothing about its being in the same directory says the
    two runs were looking at the same file. When the hashes do not match
    — or either side recorded none — the half is dropped along with its
    totals, and the document says ``instances: null`` (or
    ``modules: null``): this publication has no such breakdown, which is
    true, where the alternative is a breakdown of a design that is gone.

    ``existing`` is ignored entirely when it is missing, unreadable, of a
    different ``schema_version``, or describes a different top. The last
    of those is the one that matters: an artefact directory is keyed on a
    run's *name*, and names are not unique across designs, so a
    same-directory model is only evidence about the same top.

    The merged document keeps ``new``'s ``publication`` token — a merge
    is part of the publication being written, not of the one that put
    the inherited half there, and the manifest written alongside it
    carries the same token.
    """
    if not _mergeable(existing, new):
        return new
    merged = dict(new)
    merged["totals"] = dict(new["totals"])
    merged["provenance"] = provenance_of(new)
    bound = may_inherit_other_half(existing, new, own_half=own_half)
    for half, totals_keys, block in _HALVES:
        if half == own_half:
            continue
        if merged.get(half) is not None or existing.get(half) is None:
            continue
        if not bound:
            continue
        merged[half] = existing[half]
        merged["provenance"][block] = provenance_of(existing)[block]
        for key in totals_keys:
            if merged["totals"].get(key) is None:
                merged["totals"][key] = existing.get("totals", {}).get(key)
    return merged


def may_inherit_other_half(existing: dict | None, new: dict, *, own_half) -> bool:
    """Does ``existing``'s other half describe the netlist ``new`` measured?

    The one gate both directions go through. A publication owns one half
    and may carry the other forward; this asks whether the run that
    produced that other half was looking at the same netlist this run
    was. A fold with no producer behind it (``own_half`` naming neither
    half) has no netlist of its own to compare and answers ``True``,
    inheriting on the rules :func:`merge_model` documents.

    The evidence is the pair of hashes in ``provenance``: the netlist the
    *other* half's producer recorded against the netlist this run
    recorded. Equal means the rows are still about this design, whatever
    has happened to the RTL in between — a re-synthesis that changed
    nothing publishes the same bytes. Anything else is not a match,
    ``None`` included: a model written before this build, a netlist that
    could not be hashed, and a power run whose ``netlist-source`` was a
    routed database all leave nothing to compare, and a half whose
    binding cannot be shown is dropped rather than assumed. That is the
    strict direction, and the cost of being wrong the other way is a
    breakdown attributed to a netlist that never produced it.

    Symmetric because the failure is. The synthesis direction is the
    obvious one — it overwrites the netlist under a power half it did not
    produce — but the power direction fails too: a `rb power` run whose
    netlist does not hash equal to the synthesis provenance in the
    directory read something the local ``modules`` rows do not describe,
    and inheriting them would publish per-module areas for a netlist this
    publication did not measure. What is asymmetric is how often each
    side refuses, not whether it is asked (see the module docstring).

    Exposed rather than folded into the loop because the manifest merge
    has to make the same call about the same publication — see
    :func:`rtl_buddy.phys.publish._publish`.
    """
    mine = _PROVENANCE_BLOCK.get(own_half)
    if mine is None:
        return True
    theirs = _OTHER_PROVENANCE_BLOCK[own_half]
    measured_on = provenance_of(existing)[theirs]["netlist_sha256"]
    just_measured = provenance_of(new)[mine]["netlist_sha256"]
    return measured_on is not None and measured_on == just_measured


def provenance_of(model) -> dict:
    """``model``'s provenance block, with every stable key present.

    Normalising on read rather than trusting the document, because the
    documents this is asked about include ones written by an rtl_buddy
    that had no provenance block at all. A missing entry reads as
    ``None``, which :func:`may_inherit_other_half` treats as "no evidence"
    — the same answer it gives a hash that does not match.
    """
    recorded = model.get("provenance") if isinstance(model, dict) else None
    recorded = recorded if isinstance(recorded, dict) else {}
    normalised = {}
    for _half, _totals, block in _HALVES:
        entry = recorded.get(block)
        entry = entry if isinstance(entry, dict) else {}
        normalised[block] = {key: entry.get(key) for key in PROVENANCE_KEYS}
    return normalised


def _mergeable(existing, new: dict) -> bool:
    if not isinstance(existing, dict):
        return False
    if existing.get("schema_version") != new.get("schema_version"):
        return False
    return _top_of(existing) == _top_of(new)


def _top_of(model: dict) -> str | None:
    design = model.get("design")
    return design.get("top") if isinstance(design, dict) else None


def blank_half(model: dict, own_half: str) -> dict:
    """``model`` with ``own_half`` and the totals it owns nulled out.

    The counterpart of :func:`merge_model`, walking the same
    :data:`_HALVES` pairing so the two cannot disagree about which
    totals travel with which half. Used when a rerun has cleared the raw
    artefacts behind a half but will not republish it — see
    :func:`rtl_buddy.phys.publish.invalidate_half` for why that is a
    state worth writing down rather than leaving alone.

    The other half is untouched, including its totals: a `rb power` run
    that failed says nothing about the synthesis rows a `rb synth` put
    in the same directory.
    """
    blanked = dict(model)
    blanked["totals"] = dict(model.get("totals") or {})
    blanked["provenance"] = provenance_of(model)
    for half, totals_keys, block in _HALVES:
        if half != own_half:
            continue
        blanked[half] = None
        blanked["provenance"][block] = {key: None for key in PROVENANCE_KEYS}
        for key in totals_keys:
            blanked["totals"][key] = None
    return blanked


def write_model(model: dict, artefact_dir) -> str:
    """Write the model into ``artefact_dir`` and return its path.

    Through a sibling ``.tmp`` and :func:`os.replace`, the same way the
    dispatch plan and the result envelopes are written: a `rb phys` read
    racing a synthesis that is rewriting the document must see one
    version or the other, never a truncated one.
    """
    artefact_dir = Path(artefact_dir)
    artefact_dir.mkdir(parents=True, exist_ok=True)
    path = artefact_dir / MODEL_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(model, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return str(path)


def load_model(path) -> dict:
    """Read a model document back."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_model_or_none(artefact_dir) -> dict | None:
    """The model already in ``artefact_dir``, or ``None``.

    The merge's read side, and the reason it is separate from
    :func:`load_model`: a previous model that is absent, truncated by a
    killed run, or written by an rtl_buddy that predates the document is
    simply "nothing to merge", never an error to propagate into a flow
    that has otherwise succeeded.
    """
    try:
        model = load_model(Path(artefact_dir) / MODEL_FILENAME)
    except (OSError, ValueError):
        return None
    return model if isinstance(model, dict) else None
