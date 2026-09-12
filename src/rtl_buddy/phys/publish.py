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
"""

from __future__ import annotations

from pathlib import Path

from . import manifest as manifest_mod
from . import model as model_mod
from . import reports


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
            top=top, modules=modules, area_um2=area_um2, gate_count=gate_count
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
    :returns: the same ``{"model", "manifest", "rows", "error"}`` shape
        :func:`publish_synth` returns.
    """

    def _build():
        cells = _rows(cells_path, reports.parse_instance_cells) or {}
        instances = _rows(
            instances_path, lambda text: reports.parse_instance_power(text, cells)
        )
        return model_mod.build_power_model(
            top=top,
            instances=instances,
            internal_w=internal_w,
            switching_w=switching_w,
            leakage_w=leakage_w,
            total_w=total_w,
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
    """The shared six steps, with the resilience rule around all of them."""
    try:
        fresh = build()
        rows = fresh[half_key]
        model = model_mod.merge_model(
            model_mod.load_model_or_none(artefact_dir), fresh, own_half=half_key
        )
        model_path = model_mod.write_model(model, artefact_dir)
        project_root = manifest_mod.project_root_for_dir(artefact_dir)
        half, values = block
        manifest = manifest_mod.merge_manifest(
            manifest_mod.load_manifest_or_none(artefact_dir),
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
        manifest_path = manifest_mod.write_manifest(manifest, artefact_dir)
    except Exception as e:  # noqa: BLE001 - a by-product never fails a run
        return {"model": None, "manifest": None, "rows": None, "error": str(e)}
    return {
        "model": model_path,
        "manifest": manifest_path,
        "rows": None if rows is None else len(rows),
        "error": None,
    }


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


def _rows(path, parse):
    """Parse ``path`` with ``parse``, or return ``None`` if it is not there.

    ``None`` and an empty result are different answers and both are
    reachable: no file at all means "this run did not produce it", while
    a file whose rows all failed to parse means "produced, and it said
    nothing" — which is the honest reading of, say, a design with no
    cells.
    """
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return parse(text)
