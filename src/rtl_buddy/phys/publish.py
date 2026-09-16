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

**Recording what shaped the run.** Beside what a run measured, both
publishes record what it *was* (#568): a config fingerprint — platform,
constraints, a digest of the effective tool options — and, for a power
run, its mode and the activity behind the numbers. The blocks are
:mod:`rtl_buddy.phys.provenance`'s and go into both documents, because
they answer two different questions in two places: in the model, what
this breakdown is a breakdown *of*; in the manifest, which of a
project's runs this is, cheaply enough that a listing of all of them
reads one small file each. None of it gates the merge — the netlist
hash above does, and it is the stronger test.

**One writer at a time.** A synthesis and a power run named the same
thing publish into the same artefact directory, and each writes *both*
documents. Two of them arriving together would read the same pair, merge
their own half onto it, and write model and manifest in an interleaved
order: two tokens crossed between the two documents, or — worse, because
nothing later can tell — a token-consistent pair from whichever finished
last, silently missing the half the other had just published. The
read-merge-write is therefore a critical section, taken in
:func:`_publication_lock` around every path that rewrites the pair
(:func:`_publish` and :func:`invalidate_half` alike, since a withdrawal
races a publish exactly as a publish races a publish).

The lock is a small ``flock`` on :data:`PUBLISH_LOCK_FILENAME` in the
artefact directory rather than either lock in
:mod:`rtl_buddy.artifact_lock`: the tree lock is whole-process, held for
a whole command and fails loud, and the build lock blocks forever by
design because the alternative to waiting there is a corrupt build.
Neither shape fits a sub-second mutex inside a by-product that may never
fail a run. So this one blocks with a bounded timeout, and a lock it
cannot take — timeout, or a directory it cannot open a lock file in — is
caught by the same ``try`` every other failure in here is, and reported
as a publish error for the caller to warn about.

**The failing rerun.** Those six steps run only when the flow gets far
enough to pass, so a rerun that fails earlier would leave the previous
run's half published over artefacts its own stale-clear has just
deleted. :func:`invalidate_half` is the other half of the contract: the
flows call it where they clear, and it nulls the producer's own half of
whatever is already in the directory. A withdrawal that *fails* — the
lock it needs held past its timeout, a directory it cannot write — is
the one by-product failure the flows do not shrug off, because what it
leaves behind is not a by-product: the raw artefacts under the half are
already deleted, so continuing would leave the previous run's rows and
the manifest paths beside them discoverable over files that no longer
exist. :func:`withdrawal_failure_desc` spells that out, and all three
flows fail the run with it.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

from ..tools.artifact_paths import PHYS_PUBLISH_LOCK_NAME as PUBLISH_LOCK_FILENAME
from . import manifest as manifest_mod
from . import model as model_mod
from . import provenance as provenance_mod
from . import reports

#: How long a publisher waits for the artefact directory's lock, and how
#: often it retries while it waits. The critical section is two small
#: reads and two small writes, so a wait of this length means a holder
#: that has died in a way flock did not notice (an NFS mount that leaks
#: the lock, say) rather than a queue — and a by-product that gives up
#: and warns is better than one that hangs a flow's exit.
PUBLISH_LOCK_TIMEOUT_SEC = 30.0
PUBLISH_LOCK_POLL_SEC = 0.02


@contextlib.contextmanager
def _publication_lock(artefact_dir) -> Iterator[None]:
    """Hold this artefact directory's publication mutex, or raise.

    Guards the whole read-merge-write of the model + manifest pair. See
    the module docstring for why the two locks in
    :mod:`rtl_buddy.artifact_lock` are the wrong shape for it, and why
    failing to take this one is a publish error rather than a run
    failure — everything here runs inside the caller's ``try``.

    A poll loop rather than a blocking ``flock``: the bound is the point,
    and a blocking wait cannot be given one without a signal. The
    descriptor is closed on the way out, which is what releases the lock;
    the file itself stays for the next publisher, empty and harmless.

    Cross-process by construction, and cross-thread too — each entry
    opens its own descriptor, and ``flock`` contends between open file
    descriptions rather than between processes.
    """
    directory = Path(artefact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / PUBLISH_LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + PUBLISH_LOCK_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{directory / PUBLISH_LOCK_FILENAME}: another publish "
                        f"has held the lock for more than "
                        f"{PUBLISH_LOCK_TIMEOUT_SEC:g}s"
                    ) from None
                time.sleep(PUBLISH_LOCK_POLL_SEC)
            else:
                break
        yield
    finally:
        os.close(fd)


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

    Takes the same :func:`_publication_lock` a publish does, and for the
    same reason: this rewrites both documents, so a publish landing in
    the middle of it would read a pair one of whose halves is already
    withdrawn and the other not.

    Never raises, for the reason the whole module gives — a by-product
    does not get to fail a run; the caller logs ``error`` at DEBUG.

    :returns: ``{"model", "manifest", "error"}``; a path is ``None``
        when that document was not there to rewrite (nothing published
        into this directory yet, which is the common case).
    """
    nothing = {"model": None, "manifest": None, "error": None}
    try:
        # Unlocked look first, so the common case — a directory nothing has
        # ever published into — neither creates a lock file nor waits on
        # one. Everything it decides is decided again under the lock.
        if (
            model_mod.load_model_or_none(artefact_dir) is None
            and manifest_mod.load_manifest_or_none(artefact_dir) is None
        ):
            return nothing
        with _publication_lock(artefact_dir):
            # Re-read inside the critical section: a publish may have
            # rewritten the pair since the look above, and this withdrawal
            # must blank what is here now rather than what was.
            model = model_mod.load_model_or_none(artefact_dir)
            manifest = manifest_mod.load_manifest_or_none(artefact_dir)
            if model is None and manifest is None:
                return nothing
            # One token across both, as a publish does: what is being written
            # here is a pair, and a reader must be able to tell it caught the
            # two mid-rewrite. `None` when these two were not a pair to begin
            # with — then each keeps its own token and stays unpaired.
            publication = (
                model_mod.new_publication()
                if _is_publication(model, manifest)
                else None
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


def withdrawal_failure_desc(error: str) -> str:
    """Why a run whose own half could not be withdrawn stops (#560).

    :func:`invalidate_half` never raises — the resilience rule this module
    is built on — so a withdrawal that failed comes back as an ``error``
    string, and the flows used to log it at DEBUG and carry on. They
    cannot: the withdrawal is called from the stale-clear, *after* that
    clear has deleted the reports the published half was read from. A run
    that proceeds past a failed withdrawal and then dies before it
    publishes leaves the previous run's rows standing, with the manifest
    still naming reports that are gone — a breakdown of a design this
    directory no longer holds, indistinguishable from a current one.

    So the three flows fail with this instead. The failure is honest and
    re-runnable: whatever holds the publication lock lets go, and the
    rerun both withdraws and republishes.

    :param error: the ``error`` :func:`invalidate_half` returned.
    """
    return (
        "the previous run's physical half could not be withdrawn "
        f"({error}), and the reports behind it have already been cleared — "
        "phys-model.json and phys-manifest.json in the artefact directory "
        "would go on publishing rows over files that no longer exist, so "
        "this run stops rather than proceed over them; re-run once whatever "
        "holds the publication lock has finished"
    )


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
    platform: str | None = None,
    effort: str | None = None,
    constraints=None,
    options=None,
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
    :param platform: the ``cfg-pnr-platforms`` entry this synthesis
        mapped against, ``effort`` the effort level it ran at, and
        ``constraints`` the SDC it read — the three identity fields a
        listing spells out. ``options`` is the backend's *resolved*
        option set, digested rather than stored; see
        :mod:`rtl_buddy.phys.provenance` for exactly what goes into it.
        All four are optional: a backend that records none produces a
        config block of nulls, which reads as "this run said nothing
        about its configuration" and never as "it had none".
    :returns: ``{"model", "manifest", "rows", "error"}``; the paths are
        ``None`` when ``error`` is set, and ``rows`` is ``None`` when the
        breakdown could not be read — which is the caller's cue to warn.
    """

    def _build(recorded):
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
            **recorded,
        )

    return _publish(
        artefact_dir=artefact_dir,
        top=top,
        command="synth",
        run=run,
        build=_build,
        half_key="modules",
        provenance={
            "config": provenance_mod.config_block(
                platform=platform,
                effort=effort,
                constraints=constraints,
                constraints_sha256=sha256_of(constraints),
                options=options,
                producer=f"synth/{run}",
            )
        },
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
    netlist_path=None,
    report_path=None,
    instances_path=None,
    cells_path=None,
    log_path=None,
    internal_w: float | None = None,
    switching_w: float | None = None,
    leakage_w: float | None = None,
    total_w: float | None = None,
    mode: str | None = None,
    activity: dict | None = None,
    platform: str | None = None,
    constraints=None,
    options=None,
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
    :param netlist_path: where those bytes are — the run's own copy,
        which it keeps. The hash says the analysis was pinned to one
        netlist; the path is how a reader coming to the manifest later,
        from an archive or a CI artefact, reaches the netlist to check
        the hash against. ``None`` wherever ``netlist_sha256`` is: the
        two are halves of one identity and a path without the hash names
        bytes nothing vouches for.
    :param mode: ``"static"`` or ``"dynamic"``, and ``activity`` what
        drove the switching
        (:func:`rtl_buddy.phys.provenance.activity_block`). Both are
        recorded because the numbers cannot say it themselves: without
        them a µW figure in a pane or a run list is a quantity with no
        statement of what it measures, and two runs differing only in
        activity are indistinguishable (#568).
    :param platform: as :func:`publish_synth`'s, with ``constraints``
        and ``options`` likewise — the identity of the analysis rather
        than of the synthesis it read.
    :returns: the same ``{"model", "manifest", "rows", "error"}`` shape
        :func:`publish_synth` returns.
    """

    def _build(recorded):
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
            **recorded,
        )

    return _publish(
        artefact_dir=artefact_dir,
        top=top,
        command="power",
        run=run,
        build=_build,
        half_key="instances",
        provenance={
            "config": provenance_mod.config_block(
                platform=platform,
                constraints=constraints,
                constraints_sha256=sha256_of(constraints),
                options=options,
                producer=f"power/{run}",
            ),
            "mode": mode,
            "activity": activity,
        },
        block=(
            "power",
            {
                "backend": backend,
                "run": run,
                "netlist_source": netlist_source,
                "netlist_path": netlist_path,
                "report": report_path,
                "instances": instances_path,
                "cells": cells_path,
                "log": log_path,
            },
        ),
    )


#: The path-valued keys inside the identity blocks a producer records.
#: Named as data because :func:`_relative_provenance` is the only place
#: that knows a `config` holds a constraints file and an `activity` a
#: trace, and a producer that grows another path must add it here rather
#: than relativising it itself — which is how one of the two documents
#: would come to carry an absolute path.
_PROVENANCE_PATHS = {"config": ("constraints",), "activity": ("trace",)}


def _relative_provenance(provenance: dict | None, project_root) -> dict:
    """The identity blocks with their paths made project-relative.

    Done once, here, and handed to both documents: the model records the
    same blocks the manifest does, and relativising them separately in
    each writer is how the two would end up spelling one constraints
    file two ways. Everything else passes through untouched — a platform
    name, a digest and a toggle rate are not paths, and running them
    through :func:`~rtl_buddy.phys.manifest.project_relative` would
    mangle them into filenames.
    """
    normalised = {}
    for key, value in (provenance or {}).items():
        paths = _PROVENANCE_PATHS.get(key)
        if paths and isinstance(value, dict):
            value = {
                inner: (
                    manifest_mod.project_relative(entry, project_root)
                    if inner in paths
                    else entry
                )
                for inner, entry in value.items()
            }
        normalised[key] = value
    return normalised


def _publish(
    *, artefact_dir, top, command, run, build, half_key, block, provenance=None
) -> dict:
    """The shared six steps, with the resilience rule around all of them.

    One ``publication`` token is minted per call and stamped into both
    documents, because they are written one after the other and a reader
    can arrive in between; see
    :func:`rtl_buddy.phys.model.new_publication`. What is merged onto is
    the pair the last publish left, or nothing at all —
    :func:`_existing_pair`.

    Reading that pair and writing this one is one critical section, held
    under :func:`_publication_lock`, so a co-named run publishing at the
    same moment queues behind this one instead of merging onto the pair
    this one is halfway through replacing. Building the fresh half stays
    outside it: parsing this run's own raw artefacts is the slow part and
    races nothing.

    The two merges are handed the same verdict on the half this run does
    not own. The model's is
    :func:`rtl_buddy.phys.model.may_inherit_other_half`; the manifest has
    no netlist hash of its own to test, so a publish the model says may
    not carry the other half forward is given nothing to merge its
    manifest onto either. Otherwise the model would say
    ``instances: null`` while the manifest beside it went on naming the
    power reports and republishing their totals — one publication
    contradicting itself about what this directory holds.

    ``provenance`` is what this run records about *itself* rather than
    about what it measured — the config fingerprint, and for a power run
    its mode and activity (#568). It goes into both documents verbatim:
    into the model's ``provenance`` block for this half (which is why
    ``build`` takes it) and into the manifest block beside the artefact
    paths, so a listing of every run in a project can tell them apart
    without opening a model each. The project root is resolved before
    the build rather than inside the lock because
    :func:`_relative_provenance` needs it and it is a walk of the
    directory's parents, not something another publisher can change.
    """
    try:
        publication = model_mod.new_publication()
        project_root = manifest_mod.project_root_for_dir(artefact_dir)
        recorded = _relative_provenance(provenance, project_root)
        # Outside the lock: parsing this run's own raw artefacts reads
        # nothing another publisher can be writing, and it is the slow part.
        fresh = build(recorded)
        rows = fresh[half_key]
        with _publication_lock(artefact_dir):
            existing_model, existing_manifest = _existing_pair(artefact_dir)
            if not model_mod.may_inherit_other_half(
                existing_model, fresh, own_half=half_key
            ):
                existing_manifest = None
            model = model_mod.merge_model(existing_model, fresh, own_half=half_key)
            model["publication"] = publication
            model_path = model_mod.write_model(model, artefact_dir)
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
                    **{half: {**_only_produced(values), **recorded}},
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
