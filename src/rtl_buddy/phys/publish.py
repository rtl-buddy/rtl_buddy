# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Writing the model + manifest from a tool backend (#558).

Three backends produce physical metrics — the two synthesis flows and
the power flow — and all three want the same six steps: read the raw
artefact, parse it, build a half-model, merge it onto whatever is
already in the artefact directory, write it, write the manifest beside
it. Doing that inline would have put the same block in three files, and
the resilience rule below in three ``try`` statements that could drift.

**The resilience rule.** A model is a *by-product*. A synthesis that
produced a netlist has succeeded whether or not Yosys also wrote a
readable ``stat -json``, and a power analysis that parsed its ``Total``
row has succeeded whether or not the per-instance walk survived. So
every failure in here is caught and returned as a string for the caller
to log at WARNING; nothing raises out of these functions. The caller's
own gates already decide whether the *run* passed, and they ran first.

A half whose raw artefact is missing or unreadable is written as
``null`` rather than omitted, which is the same statement the manifest's
stable keys make: this run did not produce it. The totals the flow
scraped from its log are still recorded, so a model always says
something even when the breakdown is gone.

**Only a whole publication is merged onto.** Both merges read what is
already in the directory, and what is already there is a *pair* — a
model and the manifest that names it, written one after the other under
one ``publication`` token. A previous publish killed between those two
writes leaves the two documents disagreeing about which write they came
from, and folding each onto its own fresh half independently would
re-stamp both with this write's token and hand every later reader a
model and a manifest that say they were written together when they were
not. So :func:`_existing_pair` hands the merges ``(None, None)`` unless
both documents are there, both read, and both carry the same token: the
directory holds no publication to inherit from, this run writes its own
half under a fresh token, and the next run of the other flow fills the
other half back in.

**Binding a half to the netlist it measured.** Both publishes record
the sha256 of the netlist they touched in the model's ``provenance``
block, and that is what lets either flow tell rows that still describe
its netlist from rows measured on one that has since been replaced; see
:func:`rtl_buddy.phys.model.may_inherit_other_half` for the rule and
:func:`sha256_of` for the cost. The manifest merge is told the same
answer, so the two documents cannot end up disagreeing about what this
directory holds.

The two sides get the hash at different moments, and the difference is
not cosmetic. A synthesis *wrote* the netlist it names, so hashing it
here is hashing what the run produced. A power analysis only read one,
minutes before this publish runs, so hashing it here would describe
whatever is at that path *now* — a netlist a concurrent `rb synth` into
the upstream directory may have replaced while OpenROAD was working,
which is exactly the mismatch the hash exists to catch, recorded as a
match. So :func:`publish_power` does not take a path at all: it takes
the ``netlist_sha256`` its caller captured of the copy it handed the
tool (:meth:`rtl_buddy.tools.power_openroad.OpenRoadPower._snapshot_netlist`),
and a caller that captured none records none, which the merge reads as
"no evidence" and refuses to inherit on. That caller closes the window
rather than narrowing it: the analysis reads its *own* copy of the
netlist, inside its own artefact directory, so the hashed bytes and the
parsed bytes are one file no other command can rewrite.

**The failing rerun.** Those six steps run only when the flow gets far
enough to pass, so a rerun that fails earlier would leave the previous
run's half published over artefacts its own stale-clear has just
deleted. :func:`invalidate_half` is the other half of the contract: the
flows call it where they clear, and it nulls the producer's own half of
whatever is already in the directory.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import manifest as manifest_mod
from . import model as model_mod
from . import reports


#: Which manifest block each model half is published from, so
#: :func:`invalidate_half` withdraws both sides of one producer's
#: contribution from one argument. Same pairing the two merges keep,
#: spelled here because this is the only place that crosses between the
#: model's vocabulary and the manifest's.
_HALF_BLOCK = {"modules": "synth", "instances": "power"}


def invalidate_half(artefact_dir, own_half: str) -> dict:
    """Withdraw this producer's half from a model+manifest already here.

    Publication happens only on a pass, which leaves a hole: a rerun
    that *fails* has already cleared the raw artefacts behind its half
    (see :func:`rtl_buddy.tools.artifact_paths.clear_stale_artefacts`)
    but publishes nothing, so the previous run's rows stay discoverable
    with nothing left on disk to back them — a breakdown of a design
    that has since changed, pointing at reports that no longer exist.
    Called from the same place the clear happens, this nulls the half
    and the manifest block that go with those artefacts.

    Only this producer's side moves. ``modules`` and the ``synth`` block
    for a synthesis, ``instances`` and ``power`` for a power run; the
    other half is another command's measurement, its artefacts are still
    on disk, and a failure here says nothing about it. A subsequent
    successful publish rewrites the half exactly as before, so this is
    invisible to every run that gets that far.

    Deliberately unconditional on the ``top`` the model records, unlike
    the merges: whatever design the half described, the clear that
    precedes this deleted the fixed-path artefacts it was read from.

    Both documents are rewritten under one fresh ``publication`` token,
    as a publish does — but only when the two that are here *are* a
    publication. An unpaired pair (see :func:`_existing_pair`) is still
    blanked, because the artefacts behind the half really have gone,
    and each document keeps the token it came with: a withdrawal must
    not be what makes two documents that were never written together
    start claiming they were.

    Never raises, for the reason the whole module gives — a by-product
    does not get to fail a run; the caller logs ``error`` at DEBUG.

    :returns: ``{"model", "manifest", "error"}``; a path is ``None``
        when that document was not there to rewrite (nothing published
        into this directory yet, which is the common case).
    """
    try:
        model = model_mod.load_model_or_none(artefact_dir)
        manifest = manifest_mod.load_manifest_or_none(artefact_dir)
        if model is None and manifest is None:
            return {"model": None, "manifest": None, "error": None}
        # One token across both, as a publish does: what is being written
        # here is a pair, and a reader must be able to tell it caught the
        # two mid-rewrite. `None` when these two were not a pair to begin
        # with — then each keeps its own token and stays unpaired.
        publication = (
            model_mod.new_publication() if _is_publication(model, manifest) else None
        )
        model_path = None
        manifest_path = None
        if model is not None:
            blanked = model_mod.blank_half(model, own_half)
            if publication is not None:
                blanked["publication"] = publication
            model_path = model_mod.write_model(blanked, artefact_dir)
        if manifest is not None:
            blanked = manifest_mod.blank_block(manifest, _HALF_BLOCK[own_half])
            if publication is not None:
                blanked["publication"] = publication
            manifest_path = manifest_mod.write_manifest(blanked, artefact_dir)
    except Exception as e:  # noqa: BLE001 - a by-product never fails a run
        return {"model": None, "manifest": None, "error": str(e)}
    return {"model": model_path, "manifest": manifest_path, "error": None}


def publish_synth(
    *,
    artefact_dir,
    top: str | None,
    backend: str,
    run: str | None = None,
    stats_path=None,
    netlist_path=None,
    log_path=None,
    area_um2: float | None = None,
    gate_count: int | None = None,
) -> dict:
    """Write the synthesis half of the model + manifest.

    :param stats_path: the ``stat -json`` dump the generated script
        wrote. A missing or unparsable file leaves ``modules`` null.
    :param netlist_path: the netlist this synthesis wrote. Hashed into
        the model's provenance, which is what decides whether a power
        half already in this directory measured this very netlist and
        may be inherited.
    :param backend: ``"yosys"`` or ``"openroad"`` — the manifest's
        record of which flow produced this half, and the flag
        :func:`~rtl_buddy.phys.manifest.merge_manifest` reads to tell a
        block that was filled from one that was not.
    :returns: ``{"model", "manifest", "rows", "error"}``; the paths are
        ``None`` when ``error`` is set, and ``rows`` is ``None`` when the
        breakdown could not be read — which is the caller's cue to warn.
    """

    def _build():
        # An empty list collapses to `None`: a synthesis that ran has at
        # least its top module, so nothing-parsed means the file is
        # unreadable, not that the design has no modules.
        modules = _rows(stats_path, reports.parse_stat_json) or None
        return model_mod.build_synth_model(
            top=top,
            modules=modules,
            area_um2=area_um2,
            gate_count=gate_count,
            netlist_sha256=sha256_of(netlist_path),
        )

    return _publish(
        artefact_dir=artefact_dir,
        top=top,
        command="synth",
        run=run,
        build=_build,
        half_key="modules",
        block=(
            "synth",
            {
                "backend": backend,
                "run": run,
                "stats": stats_path,
                "netlist": netlist_path,
                "log": log_path,
            },
        ),
    )


def publish_power(
    *,
    artefact_dir,
    top: str | None,
    backend: str,
    run: str | None = None,
    netlist_source: str | None = None,
    netlist_sha256: str | None = None,
    report_path=None,
    instances_path=None,
    cells_path=None,
    log_path=None,
    internal_w: float | None = None,
    switching_w: float | None = None,
    leakage_w: float | None = None,
    total_w: float | None = None,
) -> dict:
    """Write the power half of the model + manifest.

    :param instances_path: the per-instance ``report_power`` text. A
        missing or unparsable file leaves ``instances`` null.
    :param cells_path: the ``<instance> <liberty cell>`` sidecar the
        generated Tcl writes; its absence costs the rows their ``module``
        column and nothing else.
    :param netlist_sha256: the hash of the netlist this analysis read,
        recorded in the model's provenance so a later synthesis can tell
        whether these rows still describe the design it writes, and so
        this publish can tell whether the module rows already here
        describe the netlist it read. Taken as a hash rather than a path
        because the bytes to identify are the ones OpenROAD was given —
        the run's own copy of the netlist, hashed when it was made — and
        not whatever is at the upstream path when this runs; see the
        module docstring. ``None`` for a ``netlist-source: pnr`` run, which
        reads a routed database and not a netlist, and for a caller that
        could not read the file; nothing is then inherited in either
        direction, which is the strict reading and the safe one.
    :returns: the same ``{"model", "manifest", "rows", "error"}`` shape
        :func:`publish_synth` returns.
    """

    def _build():
        cells = _rows(cells_path, reports.parse_instance_cells) or {}
        # An empty parse collapses to `None`, exactly as the synthesis
        # half does: the generated Tcl writes `power_instances.rpt` only
        # after `get_cells` came back non-empty, so a report that yields
        # no rows is one this could not read, not a design without cells.
        instances = (
            _rows(
                instances_path, lambda text: reports.parse_instance_power(text, cells)
            )
            or None
        )
        return model_mod.build_power_model(
            top=top,
            instances=instances,
            internal_w=internal_w,
            switching_w=switching_w,
            leakage_w=leakage_w,
            total_w=total_w,
            netlist_sha256=netlist_sha256,
        )

    return _publish(
        artefact_dir=artefact_dir,
        top=top,
        command="power",
        run=run,
        build=_build,
        half_key="instances",
        block=(
            "power",
            {
                "backend": backend,
                "run": run,
                "netlist_source": netlist_source,
                "report": report_path,
                "instances": instances_path,
                "cells": cells_path,
                "log": log_path,
            },
        ),
    )


def _publish(*, artefact_dir, top, command, run, build, half_key, block) -> dict:
    """The shared six steps, with the resilience rule around all of them.

    One ``publication`` token is minted per call and stamped into both
    documents, because they are written one after the other and a reader
    can arrive in between; see
    :func:`rtl_buddy.phys.model.new_publication`. What is merged onto is
    the pair the last publish left, or nothing at all —
    :func:`_existing_pair`.

    The two merges are handed the same verdict on the half this run does
    not own. The model's is
    :func:`rtl_buddy.phys.model.may_inherit_other_half`; the manifest has
    no netlist hash of its own to test, so a publish the model says may
    not carry the other half forward is given nothing to merge its
    manifest onto either. Otherwise the model would say
    ``instances: null`` while the manifest beside it went on naming the
    power reports and republishing their totals — one publication
    contradicting itself about what this directory holds.
    """
    try:
        publication = model_mod.new_publication()
        fresh = build()
        rows = fresh[half_key]
        existing_model, existing_manifest = _existing_pair(artefact_dir)
        if not model_mod.may_inherit_other_half(
            existing_model, fresh, own_half=half_key
        ):
            existing_manifest = None
        model = model_mod.merge_model(existing_model, fresh, own_half=half_key)
        model["publication"] = publication
        model_path = model_mod.write_model(model, artefact_dir)
        project_root = manifest_mod.project_root_for_dir(artefact_dir)
        half, values = block
        manifest = manifest_mod.merge_manifest(
            existing_manifest,
            manifest_mod.build_manifest(
                project_root=project_root,
                phys_dir=artefact_dir,
                command=command,
                run=run,
                top=top,
                model_path=model_path,
                totals=model["totals"],
                **{half: _only_produced(values)},
            ),
            own_block=half,
        )
        manifest["publication"] = publication
        manifest_path = manifest_mod.write_manifest(manifest, artefact_dir)
    except Exception as e:  # noqa: BLE001 - a by-product never fails a run
        return {"model": None, "manifest": None, "rows": None, "error": str(e)}
    return {
        "model": model_path,
        "manifest": manifest_path,
        "rows": None if rows is None else len(rows),
        "error": None,
    }


def _existing_pair(artefact_dir) -> tuple[dict | None, dict | None]:
    """The publication already in ``artefact_dir``, or ``(None, None)``.

    The merges' read side, and it reads the two documents as one thing.
    Either both are inherited from or neither is: a model paired with a
    manifest from a different write is not a smaller publication to
    merge half of, it is a directory whose last publish did not finish,
    and inheriting the half that happens to be readable would put this
    run's fresh token on the inconsistency and make it indistinguishable
    from a pair that was written together.

    Nothing-to-merge is the normal case anyway — the first run into a
    directory finds nothing — so an unfinished publish takes the path
    that is already the common one, and the run after it republishes a
    consistent pair. Never raises: each half is loaded through the
    ``_or_none`` reader that already treats an absent or truncated
    document as nothing to merge.
    """
    model = model_mod.load_model_or_none(artefact_dir)
    manifest = manifest_mod.load_manifest_or_none(artefact_dir)
    if not _is_publication(model, manifest):
        return None, None
    return model, manifest


def _is_publication(model, manifest) -> bool:
    """Were these two documents written by the same publish?

    Both must be here and carry the same non-null ``publication`` token.
    A missing token is not a match with another missing token: it is a
    document nothing stamped, so there is no evidence the two belong
    together, and this side of the contract is a *write* — it decides
    what gets republished under a fresh token, where
    :func:`rtl_buddy.phys.query.load_context` only decides whether to
    read again and can afford to take the freshest read of each.
    """
    if not isinstance(model, dict) or not isinstance(manifest, dict):
        return False
    token = model.get("publication")
    return token is not None and token == manifest.get("publication")


def _only_produced(values: dict) -> dict:
    """Null out the block's path values whose file is not on disk.

    The flows name their artefacts before they know whether the tool
    wrote them: Yosys' generated script tees ``stat -json`` to a path it
    may skip entirely, and the power Tcl wraps each report in a ``catch``
    that leaves the intended filename with nothing behind it. Naming a
    path the manifest's own contract says exists — ``null`` means "not
    produced", never absent — would send every consumer that joins on it
    to a missing file.

    Applied uniformly to every path key rather than only the ones known
    to be optional, so a flow that grows a new artefact cannot reopen the
    hole. Non-path values (``backend``, ``run``, ``netlist_source``) are
    plain strings and pass through.
    """
    return {
        key: (
            None
            if key in manifest_mod.PATH_KEYS
            and value is not None
            and not Path(value).exists()
            else value
        )
        for key, value in values.items()
    }


def sha256_of(path) -> str | None:
    """The sha256 of ``path``, or ``None`` when there is nothing to hash.

    One sequential read of a file the flow has just written or is about
    to hand to a tool that will read it several times over, so the cost
    sits below the noise of either flow — a synthesis and a power
    analysis are minutes of work, and this is one pass over their
    smallest artefact. Chunked rather than slurped so a flat netlist from
    a large design does not have to fit in memory to be identified.

    An unreadable file is ``None`` — the same "no evidence" a document
    that never recorded a hash carries, and the merge treats the two
    alike.

    Public because the power flow calls it itself: the bytes worth
    identifying there are the ones it hands OpenROAD — the private copy
    it stages in its own artefact directory, minutes before it publishes
    (see the module docstring) — so the hash is captured of that copy
    and threaded into :func:`publish_power`.
    """
    if path is None:
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _rows(path, parse):
    """Parse ``path`` with ``parse``, or return ``None`` if it is not there.

    A missing or unreadable file is ``None``: this run did not produce
    it. An empty parse is returned as it comes — both callers above then
    collapse it to ``None`` themselves, because for both of them a
    report that exists and yields nothing means the file is garbled
    rather than that the design is empty (a synthesis has at least its
    top module, and the power Tcl writes its report only once
    ``get_cells`` has come back non-empty). The distinction is kept
    *here* rather than decided here so the reason lives with the flow
    that knows it, and the ``cells`` sidecar — whose empty parse really
    does just mean "no cell names to join on" — keeps it.
    """
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return parse(text)
