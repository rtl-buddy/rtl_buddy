# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The structured physical model: what a synthesis or power analysis measured.

One versioned JSON document, ``<artefact dir>/phys-model.json``, built
from artefacts already on disk and pointed at by ``phys-manifest.json``.
The shape is backend-agnostic: a module has cells and area, an instance
has leakage and dynamic components.

* **Per module and per instance.** ``modules`` holds one row per module
  as the tool saw it; ``instances`` holds leaf instances. Hierarchical
  roll-up is the consumer's job.
* **Totals are a sanity block.** ``totals`` carries the numbers the flows
  parse from their logs (Yosys chip area and cell count, the
  ``report_power`` ``Total`` row). They come from a different scrape than
  the rows, so a totals-versus-sum mismatch is information.
* **A run fills only its own half.** A synthesis writes ``modules`` and
  leaves ``instances`` null; a power analysis does the reverse. ``null``
  means "not produced by this run".

**Merging.** When a model for the same top exists in the artefact
directory, the half the new run does not own is carried forward
(:func:`merge_model`), so `rb synth` and `rb power` into one directory
add up to a complete document in either order. The half a run owns is
never inherited, even when the run failed to produce it. A different top
or ``schema_version`` replaces the old model outright.

**The netlist hash gates inheritance.** The other half is inherited only
when both sides recorded the sha256 of the netlist they measured, in
``provenance`` (:data:`PROVENANCE_KEYS`), and the hashes are equal
(:func:`may_inherit_other_half`). Missing provenance is not a match: an
old document, an unreadable netlist or a ``netlist-source: pnr`` power
run drops the half. A power run reads the synthesis output, so in the
usual `rb synth` then `rb power` pair the hashes match; a synthesis
overwrites the netlist under an existing power half, so it is the
direction that usually refuses.

**Invalidation.** A rerun that fails before publishing has already
deleted the raw artefacts behind its previous half.
:func:`rtl_buddy.phys.publish.invalidate_half` nulls that half
(:func:`blank_half`) at clear time and leaves the other half alone.

**Identity.** ``provenance`` also records a config fingerprint on both
halves and, on the power half, the mode and activity
(:mod:`rtl_buddy.phys.provenance`). The merge does not read them; they
tell runs of one design apart.

**Publication token.** The model and manifest are written one after the
other and carry the same ``publication`` token
(:func:`new_publication`). A reader uses it to detect a pair caught
mid-rewrite, and the publisher uses it to merge only onto a pair that
was written together
(:func:`rtl_buddy.phys.publish._existing_pair`).
"""

from __future__ import annotations

import json
import os
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: Bumped when the document's shape changes incompatibly.
MODEL_SCHEMA_VERSION = 1

# Defined in `tools.artifact_paths`; re-exported for consumers.
from ..tools.artifact_paths import (  # noqa: E402
    PHYS_MODEL_NAME as MODEL_FILENAME,
)

#: Units of every value in the document. Yosys area is in the Liberty
#: unit (um2 for every shipped PDK); the readers scale OpenSTA watts to uW.
UNITS = {"area": "um2", "power": "uW"}

#: Every key of the totals block; ``null`` means "not measured by this run".
TOTALS_KEYS = (
    "area_um2",
    "cell_count",
    "internal_uw",
    "switching_uw",
    "leakage_uw",
    "total_uw",
)

#: What both halves record about the run behind them: the netlist hash
#: and the config fingerprint (:func:`rtl_buddy.phys.provenance.config_block`).
PROVENANCE_KEYS = ("netlist_sha256", "config")

#: What the power half records in addition: the kind of power and what
#: drove the switching (:func:`rtl_buddy.phys.provenance.activity_block`).
POWER_PROVENANCE_KEYS = ("mode", "activity")

# The two halves, the totals each owns and the provenance block each fills.
_SYNTH_TOTALS = ("area_um2", "cell_count")
_POWER_TOTALS = ("internal_uw", "switching_uw", "leakage_uw", "total_uw")
_HALVES = (
    ("modules", _SYNTH_TOTALS, "synth"),
    ("instances", _POWER_TOTALS, "power"),
)

#: Every provenance key each block carries; :func:`provenance_of` and
#: :func:`blank_half` normalise against it.
BLOCK_PROVENANCE_KEYS = {
    block: PROVENANCE_KEYS + (POWER_PROVENANCE_KEYS if block == "power" else ())
    for _half, _totals, block in _HALVES
}


#: The provenance block each half's producer fills, and the block the
#: other producer fills; the two ends of the hash comparison.
_PROVENANCE_BLOCK = {half: block for half, _totals, block in _HALVES}
_OTHER_PROVENANCE_BLOCK = {
    half: block
    for half, _totals, _block in _HALVES
    for other, _t, block in _HALVES
    if other != half
}

#: Watts in, microwatts out.
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
        block: {key: None for key in keys}
        for block, keys in BLOCK_PROVENANCE_KEYS.items()
    }


def new_publication() -> str:
    """A fresh token identifying one model+manifest write.

    Opaque and never ordered: it answers "were these two written
    together". :func:`rtl_buddy.phys.query.load_context` re-reads on a
    mismatch and then answers from the freshest read of each.
    :func:`rtl_buddy.phys.publish._existing_pair` merges onto neither
    document of a mismatched pair.
    """
    return uuid.uuid4().hex


def _base(top: str | None) -> dict:
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        # Stamped by `phys.publish._publish`; null until published.
        "publication": None,
        "generator": _generator(),
        "design": {"top": top},
        "units": dict(UNITS),
        # What each half was measured on, filled by that half's producer.
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
    config: dict | None = None,
) -> dict:
    """The synthesis half: per-module rows plus the design totals.

    :param top: the synthesised top module.
    :param modules: rows from :func:`rtl_buddy.phys.reports.parse_stat_json`,
        or ``None`` when ``stat -json`` produced nothing readable. The
        parsed totals are still recorded.
    :param area_um2: the design area the flow scraped from its log.
    :param gate_count: the design cell count the flow scraped from its log.
    :param netlist_sha256: the hash of the netlist this synthesis wrote,
        or ``None`` when none could be read. A power half already in the
        directory is inherited only if it was measured on this hash.
    :param config: the config fingerprint
        (:func:`rtl_buddy.phys.provenance.config_block`). It identifies
        the run and does not affect the merge.
    """
    model = _base(top)
    model["provenance"]["synth"]["netlist_sha256"] = netlist_sha256
    model["provenance"]["synth"]["config"] = config
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
    config: dict | None = None,
    mode: str | None = None,
    activity: dict | None = None,
) -> dict:
    """The power half: per-instance rows plus the design totals.

    Totals arrive in watts and are stored in uW (:data:`UNITS`).

    :param instances: rows from
        :func:`rtl_buddy.phys.reports.parse_instance_power`, or ``None``
        when the per-instance report was missing or unreadable.
    :param netlist_sha256: the hash of the netlist this analysis read, or
        ``None`` for a post-PnR routed database or an unhashable file.
    :param config: the config fingerprint; see :func:`build_synth_model`.
    :param mode: ``"static"`` or ``"dynamic"``.
    :param activity: what drove the switching
        (:func:`rtl_buddy.phys.provenance.activity_block`).
    """
    model = _base(top)
    model["provenance"]["power"]["netlist_sha256"] = netlist_sha256
    model["provenance"]["power"]["config"] = config
    model["provenance"]["power"]["mode"] = mode
    model["provenance"]["power"]["activity"] = activity
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

    ``own_half`` (``"modules"`` for a synthesis, ``"instances"`` for a
    power analysis) is the half the producing command owns. It is never
    inherited, so a rerun can shrink its half to ``None``. It is required
    because an unreadable ``stat -json`` also yields ``modules = None``,
    which by shape looks like a half the run does not fill. Pass
    ``own_half=None`` only for a fold with no producer, where any half
    ``new`` left null is inheritable.

    The other half is carried forward with its totals and ``provenance``
    entry, but only if :func:`may_inherit_other_half` allows it. Otherwise
    it is dropped and left null.

    ``existing`` is ignored when missing, unreadable, of a different
    ``schema_version`` or of a different top. The result keeps ``new``'s
    ``publication`` token.
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

    True when the ``netlist_sha256`` recorded by the other half's producer
    equals the one this run recorded. A missing hash on either side is not
    a match. A fold with no producer (``own_half`` naming neither half)
    answers ``True``.

    The manifest merge uses the same answer; see
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
    """``model``'s provenance block with every stable key present.

    Documents written before provenance existed read as ``None``
    throughout, which :func:`may_inherit_other_half` treats as no
    evidence.
    """
    recorded = model.get("provenance") if isinstance(model, dict) else None
    recorded = recorded if isinstance(recorded, dict) else {}
    normalised = {}
    for block, keys in BLOCK_PROVENANCE_KEYS.items():
        entry = recorded.get(block)
        entry = entry if isinstance(entry, dict) else {}
        normalised[block] = {key: entry.get(key) for key in keys}
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
    """``model`` with ``own_half``, its totals and its provenance nulled out.

    The counterpart of :func:`merge_model`, used by
    :func:`rtl_buddy.phys.publish.invalidate_half`. The other half and its
    totals are untouched.
    """
    blanked = dict(model)
    blanked["totals"] = dict(model.get("totals") or {})
    blanked["provenance"] = provenance_of(model)
    for half, totals_keys, block in _HALVES:
        if half != own_half:
            continue
        blanked[half] = None
        blanked["provenance"][block] = {
            key: None for key in BLOCK_PROVENANCE_KEYS[block]
        }
        for key in totals_keys:
            blanked["totals"][key] = None
    return blanked


def write_model(model: dict, artefact_dir) -> str:
    """Write the model into ``artefact_dir`` and return its path.

    Written through a temp file and :func:`os.replace`, so a reader never
    sees a partial file.
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
    """The model in ``artefact_dir``, or ``None`` if absent or unreadable.

    Unlike :func:`load_model`, a bad previous model is "nothing to merge"
    and never an error for the flow that is publishing.
    """
    try:
        model = load_model(Path(artefact_dir) / MODEL_FILENAME)
    except (OSError, ValueError):
        return None
    return model if isinstance(model, dict) else None
