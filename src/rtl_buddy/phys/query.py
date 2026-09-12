# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Read verbs over physical artefacts already on disk (#558).

``rb phys summary``, ``rb phys module <name>`` and ``rb phys instance
<path>`` answer the three questions the four scalars in a synthesis or
power summary cannot: *what did this run measure*, *which module owns
the area*, and *which instance owns the power*. None of them runs a
tool; all three read ``phys-manifest.json`` and the ``phys-model.json``
it points at.

Every payload builder here is a **plain function taking a context and
returning a dict**, exactly as :mod:`rtl_buddy.cov.query` does it. The
CLI hands the dict straight to ``_emit_machine_result`` and a later MCP
tool wraps the same dict verbatim — the payload *is* the contract, so it
may not be assembled inside a command body where only one of the two
surfaces would see it.

**Roll-up happens here, not in the model.** The model is leaf-only on
purpose (see :mod:`rtl_buddy.phys.model`): a subtree sum depends on the
hierarchy the consumer is projecting onto, and the producer does not
know it. ``rb phys instance`` *is* a consumer, and the hierarchy it
projects onto is the instance path it was asked about — so it sums the
rows under that path at query time and the document on disk stays leaf
values only.

**The two ``module`` columns are two namespaces, and this module does
not pretend otherwise.** ``modules[].module`` is an *RTL module* name,
as Yosys' ``stat`` saw it after elaboration. ``instances[].module`` is
the *Liberty cell* each leaf is an instance of — ``DFF_X1``,
``NAND2_X1`` — because that is what ``report_power`` and the cell
sidecar name, and a mapped netlist's leaves are cells, not RTL modules.
So the join these verbs make on that column answers Liberty-cell
questions ("how much do all the DFFs burn") and, on a flat netlist whose
one RTL module is also its top, the top's question. It does *not*
attribute power to an RTL module on a hierarchical design: no leaf row
carries ``u_cpu``'s name, so the join simply misses.

Rather than invent an instance→RTL-module mapping here, the surfaces say
so. ``module_payload`` sets :data:`INSTANCE_JOIN_LIBERTY_ONLY` on
``instance_join`` when it can see that shape, and ``subtree_rollup``
reports ``modules_matched`` so a caller can tell a joined area from an
unjoined one. Real RTL-module↔instance attribution needs the hierarchy
join, which is tracked as its own phase on the epic.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import FatalRtlBuddyError
from . import manifest as manifest_mod
from .model import load_model

#: Bumped when a payload's shape changes incompatibly. Rides on every
#: payload so an agent surface can tell.
PHYS_QUERY_SCHEMA_VERSION = 1

#: Rows per ranking in ``rb phys summary`` before truncation. A headline,
#: not a report: the whole breakdown is in the model the payload names,
#: and a terminal listing ten thousand leaf instances is a terminal
#: nobody reads to the end.
DEFAULT_RANK_LIMIT = 10

#: The four power columns every instance row carries, in the order a
#: reader wants them: the total first, then what it decomposes into.
POWER_COLUMNS = ("total_uw", "internal_uw", "switching_uw", "leakage_uw")

#: Which command fills each half of the model, so a summary over a
#: one-sided document can say what to run rather than only what is
#: absent. Keyed by the model's own block names.
HALF_PRODUCER = {"modules": "rb synth", "instances": "rb power"}

#: The characters a hierarchical instance path is built from. Both are
#: admitted because the separator is the *tool's* choice — OpenSTA
#: prints ``/`` for a netlist read from Verilog and ``.`` is what every
#: RTL-side consumer spells the same path with — and a user typing the
#: parent of a path they read elsewhere should not have to know which.
_PATH_SEPARATORS = ("/", ".")

#: The one separator every path comparison here is made in. Which of the
#: two a path is spelled with is the *producer's* choice, so a comparison
#: that kept it would make ``u_top.u_sub`` and ``u_top/u_sub`` different
#: instances; levelling both sides first is the only way a query typed in
#: one spelling can find rows stored in the other. Output rows keep the
#: model's own spelling — this is a comparison rule, not a rewrite.
_CANONICAL_SEPARATOR = "/"

#: What ``module_payload`` puts on ``instance_join`` when the module it
#: was asked about lives only in the synthesis half and the power half,
#: though populated, carries no row for it. The two halves spell
#: ``module`` in different namespaces (see this module's docstring), so
#: "no instances" and "the join cannot see them" are different answers and
#: a consumer must be able to tell them apart. The value is the sentence
#: rather than a code, so a machine reader gates on ``is not None`` and
#: every surface — CLI, hub pane, MCP client — reports the same reason.
INSTANCE_JOIN_LIBERTY_ONLY = (
    "liberty-cell names only: no instance row carries this RTL module, so "
    "power cannot be attributed to it until the hierarchy join lands"
)


def level_path(path: str) -> str:
    """One instance path in the separator every comparison here uses.

    Every ``/`` and every ``.`` is a level, whichever the tool wrote —
    the same rule the hub's `/phy` pane levels a schematic path with
    before it puts one on the wire.
    """
    levelled = str(path)
    for separator in _PATH_SEPARATORS:
        levelled = levelled.replace(separator, _CANONICAL_SEPARATOR)
    return levelled


class PhysQueryError(FatalRtlBuddyError):
    """A physical question that cannot be answered as asked.

    Carries ``candidates`` when the failure was an unknown module name
    or instance path, so the CLI and a later MCP server can show near
    misses instead of only the miss.
    """

    def __init__(self, message: str, *, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class PhysContext:
    """One run's physical artefacts, loaded."""

    project_root: str
    manifest_path: str
    manifest: dict
    model: dict
    model_path: str | None


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def resolve_manifest_path(project_root, *, phys_dir=None, manifest=None) -> str:
    """Locate the manifest to read, most explicit request first.

    Three ways in, in descending order of how specific the user was:
    ``--manifest`` names the document, ``--phys-dir`` names the
    directory holding it, and neither means the newest manifest in the
    project. The precedence is the coverage verbs' verbatim, because a
    user who has learned one should not have to learn the other.

    Unlike coverage, discovery cannot shortcut on a directory name:
    synthesis and power write into whatever ``artefacts/<run>/`` the run
    was named into, so the filename is the only marker and
    :func:`~rtl_buddy.phys.manifest.discover_manifests` walks for it.
    """
    if manifest is not None:
        candidate = Path(manifest)
        if candidate.is_dir():
            candidate = candidate / manifest_mod.MANIFEST_FILENAME
        if not candidate.exists():
            raise PhysQueryError(f"phys: no physical manifest at {candidate}")
        return str(candidate)

    if phys_dir is not None:
        candidate = Path(phys_dir) / manifest_mod.MANIFEST_FILENAME
        if not candidate.exists():
            raise PhysQueryError(
                f"phys: no {manifest_mod.MANIFEST_FILENAME} in {phys_dir}; "
                "run `rb synth` or `rb power` there first"
            )
        return str(candidate)

    found = manifest_mod.discover_manifests(project_root)
    if not found:
        raise PhysQueryError(
            f"phys: no {manifest_mod.MANIFEST_FILENAME} under {project_root}; "
            "run `rb synth` or `rb power` first"
        )
    return found[0]


def load_context(project_root, *, phys_dir=None, manifest=None) -> PhysContext:
    """Load the manifest and the model it names.

    A manifest with no readable model is an error rather than an empty
    answer: both documents are written by the same code path, so a
    missing model means the artefacts were truncated, not that the run
    measured nothing. A run that measured nothing still writes a model —
    with both halves ``null`` — and that is a state the payloads report
    rather than refuse.
    """
    manifest_path = resolve_manifest_path(
        project_root, phys_dir=phys_dir, manifest=manifest
    )
    try:
        document = manifest_mod.load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise PhysQueryError(f"phys: cannot read {manifest_path}: {exc}")

    root = manifest_mod.project_root_for(manifest_path) or str(project_root)
    model_path = manifest_mod.resolve(manifest_path, document.get("model"))
    if model_path is None or not os.path.exists(model_path):
        raise PhysQueryError(
            f"phys: {manifest_path} names no physical model "
            f"({document.get('model')}); re-run `rb synth` or `rb power`"
        )
    try:
        model = load_model(model_path)
    except (OSError, ValueError) as exc:
        raise PhysQueryError(f"phys: cannot read {model_path}: {exc}")

    return PhysContext(
        project_root=root,
        manifest_path=manifest_path,
        manifest=document,
        model=model,
        model_path=model_path,
    )


# ---------------------------------------------------------------------------
# shared payload pieces
# ---------------------------------------------------------------------------


def artefacts_block(ctx: PhysContext) -> dict:
    """Every artefact path this run produced, project-relative.

    The block a machine consumer gates on. Both producer blocks are
    flattened into it under a prefix rather than nested, so a consumer
    reading ``artefacts["synth_netlist"]`` need not first ask whether a
    synthesis ever ran here — the manifest's stable-keys rule already
    guarantees the key, and ``null`` already means "not produced".
    """
    document = ctx.manifest
    synth = document.get("synth") or {}
    power = document.get("power") or {}
    block = {
        "manifest": manifest_mod.project_relative(ctx.manifest_path, ctx.project_root),
        "phys_dir": document.get("phys_dir"),
        "model": document.get("model"),
    }
    for key in manifest_mod.SYNTH_KEYS:
        block[f"synth_{key}"] = synth.get(key)
    for key in manifest_mod.POWER_KEYS:
        block[f"power_{key}"] = power.get(key)
    return block


def halves_block(model: dict) -> dict:
    """Which halves of the model this document actually has.

    The first thing every payload here reports, because the model is
    routinely half-filled by design: a synthesis writes ``modules`` and
    a power analysis writes ``instances``, and only a directory that saw
    both runs holds both. ``rows`` is the row count when present and
    ``null`` when not — ``0`` is a real answer (a design with no cells)
    and must not read as "missing".
    """
    block = {}
    for half, command in HALF_PRODUCER.items():
        rows = model.get(half)
        block[half] = {
            "present": rows is not None,
            "rows": None if rows is None else len(rows),
            "produced_by": command,
        }
    return block


def missing_halves(model: dict) -> list[str]:
    """The halves this model does not have, in a stable order."""
    return [half for half in HALF_PRODUCER if model.get(half) is None]


def _run_block(ctx: PhysContext) -> dict:
    document = ctx.manifest
    synth = document.get("synth") or {}
    power = document.get("power") or {}
    return {
        "schema_version": PHYS_QUERY_SCHEMA_VERSION,
        "manifest": manifest_mod.project_relative(ctx.manifest_path, ctx.project_root),
        "model": document.get("model"),
        "generated_at": document.get("generated_at"),
        # Not `command`: the machine envelope already spends that key on
        # the verb being run, and `**payload` would collide with it.
        "run_command": document.get("command"),
        "run": document.get("run"),
        "top": document.get("top"),
        "backends": {"synth": synth.get("backend"), "power": power.get("backend")},
        "units": ctx.model.get("units", {}),
    }


def _module_rows(model: dict) -> list[dict]:
    return list(model.get("modules") or [])


def _instance_rows(model: dict) -> list[dict]:
    return list(model.get("instances") or [])


def _sort_key_desc(value):
    """Order a possibly-``None`` metric descending, nulls last.

    A row whose metric was never measured is not a zero — Yosys writes
    no ``area`` without a Liberty — so it sinks rather than ranking
    alongside the genuinely small.
    """
    return (0, -value) if isinstance(value, (int, float)) else (1, 0.0)


def heaviest_modules(model: dict, limit: int | None = None) -> list[dict]:
    """Module rows ranked by cell count, then area, then name.

    Cells lead and area breaks the tie rather than the other way round,
    because ``area_um2`` is ``null`` for every unmapped run while
    ``cell_count`` is always there — ranking on the column that can be
    absent would order a whole class of runs alphabetically.
    """

    def key(row):
        return (
            _sort_key_desc(row.get("cell_count")),
            _sort_key_desc(row.get("area_um2")),
            str(row.get("module") or ""),
        )

    ordered = sorted(_module_rows(model), key=key)
    return ordered if limit is None or limit <= 0 else ordered[:limit]


def hottest_instances(model: dict, limit: int | None = None) -> list[dict]:
    """Instance rows ranked by total power, then path."""

    def key(row):
        return (
            _sort_key_desc(row.get("total_uw")),
            str(row.get("instance_path") or ""),
        )

    ordered = sorted(_instance_rows(model), key=key)
    return ordered if limit is None or limit <= 0 else ordered[:limit]


def _power_sum(rows) -> dict:
    """Add up the four power columns over ``rows``.

    ``null`` in, ``null`` out: a column no row measured stays ``None``
    rather than becoming ``0.0``, so a rollup over instances whose
    switching power was never reported cannot be read as "it switches
    nothing".
    """
    totals: dict[str, float | None] = {column: None for column in POWER_COLUMNS}
    for row in rows:
        for column in POWER_COLUMNS:
            value = row.get(column)
            if isinstance(value, (int, float)):
                totals[column] = (totals[column] or 0.0) + value
    return totals


# ---------------------------------------------------------------------------
# payload builders
# ---------------------------------------------------------------------------


def summary_payload(ctx: PhysContext, *, limit: int = DEFAULT_RANK_LIMIT) -> dict:
    """The run header, the totals sanity block, and the two rankings.

    The totals come from the flows' own log scrapes and the rows from a
    different scrape entirely, so both are reported and neither is
    derived from the other — a totals-versus-sum mismatch is information
    (see :mod:`rtl_buddy.phys.model`), and folding one into the other
    here would destroy it.
    """
    model = ctx.model
    payload = _run_block(ctx)
    payload.update(
        {
            "totals": model.get("totals", {}),
            "counts": {
                half: (None if model.get(half) is None else len(model[half]))
                for half in HALF_PRODUCER
            },
            "halves": halves_block(model),
            "missing_halves": missing_halves(model),
            "limit": limit,
            "modules": heaviest_modules(model, limit),
            "instances": hottest_instances(model, limit),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload


def module_names(model: dict) -> list[str]:
    """Every module name the model can be asked about.

    The union of both halves, not just the synthesis one: the power
    half's ``module`` column names the Liberty cell each leaf instance is
    an instance of, and "how much power do all the DFFs burn" is a
    question a power-only model can answer perfectly well.
    """
    names = {str(row["module"]) for row in _module_rows(model) if row.get("module")}
    names.update(
        str(row["module"]) for row in _instance_rows(model) if row.get("module")
    )
    return sorted(names)


def resolve_module_name(model: dict, module: str, *, where=None) -> str:
    """A user's module name -> the name the model spells it with.

    Raises :class:`PhysQueryError` with near misses when there is no
    such module, for the same reason the coverage verbs do: an unknown
    name is a typo far more often than it is a design that lacks the
    block, and the near-miss list is the cheaper fix. When the model is
    half-filled the message says which command would add the missing
    half, since that is the other way a name goes missing.
    """
    known = module_names(model)
    if module in known:
        return module
    lowered = {name.lower(): name for name in known}
    if module.lower() in lowered:
        return lowered[module.lower()]
    raise PhysQueryError(
        f"phys: no module {module!r} in {where or 'the physical model'}"
        f"{_missing_half_hint(model)}",
        candidates=difflib.get_close_matches(module, known, n=10, cutoff=0.4)
        or known[:10],
    )


def _missing_half_hint(model: dict) -> str:
    absent = missing_halves(model)
    if not absent:
        return ""
    listed = ", ".join(f"{half} (run `{HALF_PRODUCER[half]}`)" for half in absent)
    return f"; this model has no {listed}"


def module_payload(ctx: PhysContext, module: str) -> dict:
    """One module's synthesis row and the instances of it, with power.

    The join the two halves make where they can: ``modules`` says how
    many cells and how much area the block is, ``instances`` says what
    the leaves *of that Liberty cell* burn. Either side may be ``null`` —
    the payload reports what it has and names the command that would
    supply the rest.

    ``instance_join`` is the honest signal about the join itself. It is
    ``null`` when there is nothing to qualify, and
    :data:`INSTANCE_JOIN_LIBERTY_ONLY` when the name resolved out of the
    synthesis half alone, matched no instance row, and the power half is
    populated — the shape of an RTL module on a mapped hierarchical
    design, whose leaves are named after Liberty cells and so can never
    match it. Without it a consumer cannot tell "this module has no
    instances" from "the join cannot see this module's instances", and
    the two call for opposite reactions.
    """
    resolved = resolve_module_name(ctx.model, module, where=ctx.model_path)
    model = ctx.model
    row = next(
        (r for r in _module_rows(model) if str(r.get("module")) == resolved), None
    )
    instances = None
    if model.get("instances") is not None:
        instances = sorted(
            (r for r in _instance_rows(model) if str(r.get("module")) == resolved),
            key=lambda r: (
                _sort_key_desc(r.get("total_uw")),
                str(r.get("instance_path") or ""),
            ),
        )

    payload = _run_block(ctx)
    payload.update(
        {
            "module": resolved,
            "row": row,
            "instances": instances,
            "instance_count": None if instances is None else len(instances),
            "power": None if instances is None else _power_sum(instances),
            "instance_join": _instance_join_note(model, row, instances),
            "halves": halves_block(model),
            "missing_halves": missing_halves(model),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload


def _instance_join_note(model: dict, row, instances) -> str | None:
    """Is this module's empty instance list a miss rather than a fact?

    Only when all three hold: the name came out of the synthesis half
    (so it is an RTL module name), the power half exists and has rows (so
    "no instances" is not simply "no power run"), and nothing matched.
    A Liberty cell that *did* match, or a genuinely instance-free design,
    gets no note — the point is to mark the one case the reader would
    otherwise misread.
    """
    if row is None or instances is None or instances:
        return None
    if not _instance_rows(model):
        return None
    return INSTANCE_JOIN_LIBERTY_ONLY


def instance_paths(model: dict) -> list[str]:
    """Every instance path the model records, sorted."""
    return sorted(
        str(row["instance_path"])
        for row in _instance_rows(model)
        if row.get("instance_path")
    )


def is_descendant(path: str, prefix: str) -> bool:
    """Is ``path`` strictly below ``prefix`` in the instance hierarchy?

    Both sides are levelled first (:func:`level_path`): OpenSTA writes
    ``/`` for a netlist read from Verilog while every RTL-side surface
    spells the same path with ``.``, and a user asking for the subtree
    under a path they read in the schematic must not miss the rows
    because of the separator.

    A separator has to follow the prefix, or ``u_cpu`` would claim
    ``u_cpu_regs`` — a different block whose name merely starts the
    same way, and the kind of miscount a rollup must never make.
    """
    path = level_path(path)
    prefix = level_path(prefix)
    if not path.startswith(prefix) or len(path) <= len(prefix):
        return False
    return path[len(prefix)] == _CANONICAL_SEPARATOR


def subtree_rollup(model: dict, rows: list[dict]) -> dict:
    """Sum ``rows`` into one subtree figure, area joined in when known.

    The roll-up the model deliberately does not do. Power adds up
    straightforwardly because every row is a leaf. Area does not come
    from the rows at all — the power half has no area column — so it is
    joined from the synthesis half through each row's ``module``, and
    ``modules_matched`` reports how much of the subtree that join
    actually covered. A join that matched nothing yields ``null`` rather
    than ``0.0``: an unjoined subtree has unknown area, not no area.

    ``modules_matched`` is the honesty mechanism here, and on a mapped
    hierarchical design it is routinely ``0``. The two halves spell
    ``module`` in different namespaces — Liberty cells on the leaves, RTL
    modules on the synthesis rows (see this module's docstring) — so the
    join lands only where the two coincide: a Liberty cell that Yosys
    also emitted a ``stat`` row for, or a flat netlist. A caller must read
    ``area_um2`` against ``modules_matched`` and not as the subtree's
    area; the real attribution arrives with the hierarchy join.
    """
    rollup = {"instances": len(rows), **_power_sum(rows)}
    areas = {
        str(row["module"]): row.get("area_um2")
        for row in _module_rows(model)
        if row.get("module")
    }
    matched = 0
    area_total: float | None = None
    for row in rows:
        area = areas.get(str(row.get("module")))
        if isinstance(area, (int, float)):
            matched += 1
            area_total = (area_total or 0.0) + area
    rollup["area_um2"] = area_total
    rollup["modules_matched"] = matched
    return rollup


def instance_payload(ctx: PhysContext, path: str) -> dict:
    """One instance's row, or the subtree its path is the root of.

    Exact match first, prefix second — in that order because a path that
    is both a leaf and a prefix is a real netlist shape, and the row the
    user named is the answer they asked for. The subtree case lists the
    leaves under the path and rolls them up at query time; the model on
    disk stays leaf-only.

    Both comparisons are made on levelled paths (:func:`level_path`), so
    a dotted query finds slash-stored rows and the reverse. The rows
    themselves are returned with the model's own spelling, and
    ``instance_path`` echoes what the user asked.
    """
    model = ctx.model
    if model.get("instances") is None:
        raise PhysQueryError(
            f"phys: {ctx.model_path} has no per-instance rows "
            f"(run `{HALF_PRODUCER['instances']}`)"
        )

    rows = _instance_rows(model)
    wanted = level_path(path)
    exact = next(
        (r for r in rows if level_path(str(r.get("instance_path"))) == wanted), None
    )
    children = sorted(
        (r for r in rows if is_descendant(str(r.get("instance_path") or ""), path)),
        key=lambda r: str(r.get("instance_path") or ""),
    )
    if exact is None and not children:
        known = instance_paths(model)
        raise PhysQueryError(
            f"phys: no instance {path!r} in {ctx.model_path}",
            candidates=difflib.get_close_matches(path, known, n=10, cutoff=0.4)
            or known[:10],
        )

    covered = ([exact] if exact is not None else []) + children
    payload = _run_block(ctx)
    payload.update(
        {
            "instance_path": path,
            "match": "exact" if exact is not None else "prefix",
            "instance": exact,
            "children": children,
            "rollup": subtree_rollup(model, covered),
            "halves": halves_block(model),
            "missing_halves": missing_halves(model),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload
