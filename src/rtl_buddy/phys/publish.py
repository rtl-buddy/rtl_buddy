# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Writing the physical model and manifest from a tool backend.

The two synthesis flows and the power flow share one publish path: read
the raw artefact, parse it, build a half-model, merge it onto what is
already in the artefact directory, write the model, then write the
manifest beside it.

* **Failures never fail a run.** A model is a by-product. Every failure
  is caught and returned as an ``error`` string for the caller to log at
  WARNING; nothing raises. The one exception is a failed withdrawal, see
  :func:`withdrawal_failure_desc`. A half whose raw artefact is missing
  or unreadable is written as ``null``, and the totals the flow scraped
  are still recorded.
* **Only a whole publication is merged onto.** The directory must hold a
  model and a manifest with the same ``publication`` token
  (:func:`_existing_pair`). Otherwise nothing is inherited and this run
  writes its own half under a fresh token.
* **Halves are bound to the netlist they measured.** Both publishes
  record the netlist sha256 in the model's provenance, and the other half
  is inherited only if the hashes match
  (:func:`rtl_buddy.phys.model.may_inherit_other_half`). The manifest
  merge gets the same verdict. A synthesis hashes the netlist it wrote
  (:func:`sha256_of`). A power analysis takes the hash its caller
  captured of the private copy it handed the tool, because hashing the
  upstream path at publish time could describe a netlist replaced while
  the tool ran. A caller that captured none records none.
* **Identity is recorded in both documents.** The config fingerprint and,
  for power, the mode and activity (:mod:`rtl_buddy.phys.provenance`) go
  into the model and into the manifest, so a run listing can read the
  manifest alone. They do not gate the merge.
* **One writer at a time.** Synthesis and power runs with the same name
  publish into one directory and each rewrites both documents.
  :func:`_publication_lock` makes the read-merge-write a critical section
  for :func:`_publish` and :func:`invalidate_half`. It is a bounded
  ``flock`` on :data:`PUBLISH_LOCK_FILENAME`, not one of the locks in
  :mod:`rtl_buddy.artifact_lock`, which are whole-command locks. A lock
  that cannot be taken is a publish error like any other.
* **A failing rerun withdraws its half.** Publishing happens only when
  the flow passes, so :func:`invalidate_half` nulls the producer's half
  where the flow clears its stale artefacts.
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

#: How long a publisher waits for the directory lock, and the retry
#: interval. The critical section is two small reads and two small
#: writes, so a long wait means a dead holder, and giving up beats
#: hanging a flow's exit.
PUBLISH_LOCK_TIMEOUT_SEC = 30.0
PUBLISH_LOCK_POLL_SEC = 0.02


@contextlib.contextmanager
def _publication_lock(artefact_dir) -> Iterator[None]:
    """Hold this artefact directory's publication mutex, or raise.

    Polls a non-blocking ``flock`` until :data:`PUBLISH_LOCK_TIMEOUT_SEC`
    and then raises :class:`TimeoutError`. Closing the descriptor releases
    the lock; the lock file stays.
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


#: The manifest block each model half is published from.
_HALF_BLOCK = {"modules": "synth", "instances": "power"}


def invalidate_half(artefact_dir, own_half: str) -> dict:
    """Null this producer's half in the model and manifest already here.

    Called where a flow clears its stale artefacts, so a rerun that fails
    before publishing does not leave the previous rows and report paths
    over deleted files. Only this producer's side moves (``modules`` and
    ``synth``, or ``instances`` and ``power``). It applies whatever top
    the model records.

    Both documents are rewritten under one fresh ``publication`` token
    when they are a publication (:func:`_is_publication`). Otherwise each
    is blanked and keeps its own token. Takes :func:`_publication_lock`.
    Never raises.

    :returns: ``{"model", "manifest", "error"}``; a path is ``None`` when
        that document was not there to rewrite.
    """
    nothing = {"model": None, "manifest": None, "error": None}
    try:
        # Unlocked look first, so an empty directory gets no lock file.
        if (
            model_mod.load_model_or_none(artefact_dir) is None
            and manifest_mod.load_manifest_or_none(artefact_dir) is None
        ):
            return nothing
        with _publication_lock(artefact_dir):
            # Re-read: a publish may have rewritten the pair since the look above.
            model = model_mod.load_model_or_none(artefact_dir)
            manifest = manifest_mod.load_manifest_or_none(artefact_dir)
            if model is None and manifest is None:
                return nothing
            # `None` when the two were not a pair: each then keeps its own token.
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
    except Exception as e:  # noqa: BLE001 - by-product
        return {"model": None, "manifest": None, "error": str(e)}
    return {"model": model_path, "manifest": manifest_path, "error": None}


def confirm_digest(path, captured: str | None) -> tuple[str | None, bool]:
    """Does ``path`` still hash to ``captured``?

    Producers hash an input (an SDC, a SAIF trace) before launching the
    tool and call this when it returns. A match returns the digest. A
    mismatch means the file changed during the run, so the identity is
    unknown and ``None`` is returned. Nothing captured stays nothing and
    is not a mismatch.

    :returns: ``(digest, changed)``: the digest to record, and whether the
        caller should warn that the file moved under the run.
    """
    if captured is None:
        return None, False
    if sha256_of(path) == captured:
        return captured, False
    return None, True


def withdrawal_failure_desc(error: str) -> str:
    """The error text for a run whose previous half could not be withdrawn.

    :func:`invalidate_half` runs after the stale-clear has deleted the
    reports behind the published half, so continuing past a failed
    withdrawal could leave rows and paths pointing at deleted files.
    All three flows fail the run with this text instead; a rerun withdraws
    and republishes.

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
    constraints_sha256: str | None = None,
    options=None,
) -> dict:
    """Write the synthesis half of the model and manifest.

    :param stats_path: the ``stat -json`` dump the generated script wrote.
        A missing or unparsable file leaves ``modules`` null.
    :param netlist_path: the netlist this synthesis wrote; its hash goes
        into the provenance and decides whether a power half already here
        may be inherited.
    :param backend: ``"yosys"`` or ``"openroad"``; recorded in the
        manifest, where ``merge_manifest`` reads it to tell a filled block
        from an empty one.
    :param platform: the ``cfg-pnr-platforms`` entry mapped against.
    :param effort: the effort level.
    :param constraints: the SDC read.
    :param constraints_sha256: the SDC's digest, captured by the caller
        before the tool ran and confirmed after (:func:`confirm_digest`),
        or ``None`` if there was no SDC, it was unreadable or it changed.
    :param options: the backend's resolved options, digested; see
        :mod:`rtl_buddy.phys.provenance`. Any of the config fields may be
        omitted, which records nulls.
    :returns: ``{"model", "manifest", "rows", "paired", "error"}``. The
        paths are ``None`` when ``error`` is set. ``rows`` is ``None``
        when the breakdown could not be read, which is the caller's cue to
        warn. ``paired`` is described in :func:`_pairing_verdict`.
    """

    def _build(recorded):
        # A synthesis has at least its top module, so an empty parse is an unreadable file.
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
                constraints_sha256=constraints_sha256,
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
    constraints_sha256: str | None = None,
    options=None,
) -> dict:
    """Write the power half of the model and manifest.

    :param instances_path: the per-instance ``report_power`` text. A
        missing or unparsable file leaves ``instances`` null.
    :param cells_path: the ``<instance> <liberty cell>`` sidecar the
        generated Tcl writes. Without it the rows lose their ``module``
        column.
    :param netlist_sha256: the hash of the netlist copy the analysis read,
        taken when the run made that copy. It is a hash and not a path so
        that it names the bytes OpenROAD was given. ``None`` for
        ``netlist-source: pnr`` or an unreadable file; nothing is then
        inherited in either direction.
    :param netlist_path: that copy, kept so an archived manifest can be
        checked against the hash. ``None`` wherever ``netlist_sha256`` is.
    :param mode: ``"static"`` or ``"dynamic"``.
    :param activity: what drove the switching
        (:func:`rtl_buddy.phys.provenance.activity_block`).
    :param platform: as :func:`publish_synth`, with ``constraints``,
        ``constraints_sha256`` and ``options``.
    :returns: the same shape as :func:`publish_synth`. A run given an
        explicit ``phys-run:`` warns when ``paired`` is ``False``.
    """

    def _build(recorded):
        cells = _rows(cells_path, reports.parse_instance_cells) or {}
        # The Tcl writes the report only after `get_cells` is non-empty, so no rows means unreadable.
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
                constraints_sha256=constraints_sha256,
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


#: The path-valued keys inside the identity blocks; a new path must be
#: added here so it is made project-relative in both documents.
_PROVENANCE_PATHS = {"config": ("constraints",), "activity": ("trace",)}


def _relative_provenance(provenance: dict | None, project_root) -> dict:
    """The identity blocks with their paths made project-relative.

    Done once for both documents so they spell each path the same way.
    Non-path values pass through.
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
    """The shared publish steps, with every failure caught.

    One ``publication`` token is stamped into both documents. The merge
    reads the pair from :func:`_existing_pair` and writes the new pair
    inside :func:`_publication_lock`; building the fresh half stays
    outside it because it reads only this run's artefacts.

    If :func:`rtl_buddy.phys.model.may_inherit_other_half` refuses the
    other half, the manifest is given nothing to merge either, so the two
    documents agree on what the directory holds.

    ``provenance`` (config, and for power the mode and activity) goes into
    the model through ``build`` and into the manifest block.
    """
    try:
        publication = model_mod.new_publication()
        project_root = manifest_mod.project_root_for_dir(artefact_dir)
        recorded = _relative_provenance(provenance, project_root)
        fresh = build(recorded)
        rows = fresh[half_key]
        with _publication_lock(artefact_dir):
            existing_model, existing_manifest = _existing_pair(artefact_dir)
            inherit = model_mod.may_inherit_other_half(
                existing_model, fresh, own_half=half_key
            )
            paired = _pairing_verdict(existing_model, half_key, inherit)
            if not inherit:
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
    except Exception as e:  # noqa: BLE001 - by-product
        return {
            "model": None,
            "manifest": None,
            "rows": None,
            "paired": None,
            "error": str(e),
        }
    return {
        "model": model_path,
        "manifest": manifest_path,
        "rows": None if rows is None else len(rows),
        "paired": paired,
        "error": None,
    }


#: The half each producer does not own, and may inherit.
_OTHER_HALF = {
    half: other for half in _HALF_BLOCK for other in _HALF_BLOCK if other != half
}


def _pairing_verdict(existing_model, half_key: str, inherit: bool) -> bool | None:
    """Whether this publish paired with a half that was already here.

    ``True``: the other half was here and its netlist hash matched, so the
    written model is whole. ``False``: it was here and the hashes differ,
    so it was discarded. ``None``: there was nothing to pair with, the
    normal state before the other producer has run. A caller given an
    explicit run to publish beside warns on ``False``.
    """
    if not isinstance(existing_model, dict):
        return None
    if existing_model.get(_OTHER_HALF[half_key]) is None:
        return None
    return inherit


def _existing_pair(artefact_dir) -> tuple[dict | None, dict | None]:
    """The publication in ``artefact_dir``, or ``(None, None)``.

    Both documents are inherited from or neither. A model and manifest
    from different writes mean the last publish did not finish, and
    merging onto them would stamp a fresh token on the inconsistency.
    Never raises.
    """
    model = model_mod.load_model_or_none(artefact_dir)
    manifest = manifest_mod.load_manifest_or_none(artefact_dir)
    if not _is_publication(model, manifest):
        return None, None
    return model, manifest


def _is_publication(model, manifest) -> bool:
    """Were these two documents written by the same publish?

    Both must be present with the same non-null ``publication`` token. Two
    missing tokens are not a match. This is stricter than
    :func:`rtl_buddy.phys.query.load_context`, which only decides whether
    to re-read.
    """
    if not isinstance(model, dict) or not isinstance(manifest, dict):
        return False
    token = model.get("publication")
    return token is not None and token == manifest.get("publication")


def _only_produced(values: dict) -> dict:
    """Null the block's path values whose file is not on disk.

    The flows name artefacts before knowing whether the tool wrote them,
    and a manifest path must exist or be ``null``. Non-path values pass
    through.
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
    """The sha256 of ``path``, or ``None`` if it is missing or unreadable.

    Reads in chunks. Public because the power flow hashes its private
    netlist copy itself and passes the result to :func:`publish_power`.
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
    """Parse ``path`` with ``parse``, or return ``None`` if it is unreadable.

    An empty parse is returned as is. The callers for ``stat`` and
    instance power turn it into ``None``, since an existing report with
    no rows is garbled; the cells sidecar keeps an empty result.
    """
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return parse(text)
