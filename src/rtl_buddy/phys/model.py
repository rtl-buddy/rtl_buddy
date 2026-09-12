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
document, in either order. The half the run does own is never inherited,
even when this run failed to produce it: a synthesis whose ``stat
-json`` was unreadable writes ``modules: null`` under its own fresh
totals rather than republishing the previous run's rows, which would
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
mid-rewrite from one written together.

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

#: The two halves of the document, and the totals each one owns. Named
#: once because the merge walks them and would otherwise re-spell the
#: pairing at every step.
_SYNTH_TOTALS = ("area_um2", "cell_count")
_POWER_TOTALS = ("internal_uw", "switching_uw", "leakage_uw", "total_uw")
_HALVES = (("modules", _SYNTH_TOTALS), ("instances", _POWER_TOTALS))

#: Watts in, microwatts out — see :data:`UNITS`.
_W_TO_UW = 1e6


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def _empty_totals() -> dict:
    return {key: None for key in TOTALS_KEYS}


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
    written together", not "which is newer".
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
) -> dict:
    """The synthesis half: per-module rows plus the design totals.

    :param top: the synthesised top module.
    :param modules: rows from :func:`rtl_buddy.phys.reports.parse_stat_json`.
        ``None`` when the ``stat -json`` step produced nothing readable —
        the flow still writes a model, so the totals it *did* parse are
        recorded and the shape says plainly that the breakdown is missing.
    :param area_um2: the design area the flow scraped from its log.
    :param gate_count: the design cell count the flow scraped from its log.
    """
    model = _base(top)
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
) -> dict:
    """The power half: per-instance rows plus the design totals.

    The totals arrive in watts because that is what ``report_power``
    prints and what ``PowerPassResults`` already carries; they are scaled
    to µW here so the block and the rows agree with :data:`UNITS`.

    :param instances: rows from
        :func:`rtl_buddy.phys.reports.parse_instance_power`, or ``None``
        when the per-instance report was not produced or not readable.
    """
    model = _base(top)
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
    that belong to it. Pass ``own_half=None`` only for a fold with no
    producer behind it (two documents being combined after the fact);
    then any half ``new`` left null is inheritable.

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
    for half, totals_keys in _HALVES:
        if half == own_half:
            continue
        if merged.get(half) is not None or existing.get(half) is None:
            continue
        merged[half] = existing[half]
        for key in totals_keys:
            if merged["totals"].get(key) is None:
                merged["totals"][key] = existing.get("totals", {}).get(key)
    return merged


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
    for half, totals_keys in _HALVES:
        if half != own_half:
            continue
        blanked[half] = None
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
