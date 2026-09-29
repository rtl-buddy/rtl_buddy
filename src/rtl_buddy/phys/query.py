# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Read verbs over physical artefacts already on disk.

``rb phys summary``, ``rb phys module <name>`` and ``rb phys instance
<path>`` read ``phys-manifest.json`` and the ``phys-model.json`` it
names and run no tool. ``rb phys runs`` lists every manifest under the
project, newest first, with the identity each run recorded
(:mod:`rtl_buddy.phys.provenance`). It reads manifests only, never
models, so it stays cheap for a project with many runs.

Every payload builder is a plain function taking a context and returning
a dict, as in :mod:`rtl_buddy.cov.query`. The CLI emits the dict through
``_emit_machine_result`` and the MCP tools return it verbatim, so
payloads are never assembled in a command body.

* **Roll-up happens here.** The model holds leaf rows only.
  ``rb phys instance`` sums the rows under the requested path at query
  time (:func:`subtree_rollup`). There is no area roll-up.
* **Two ``module`` namespaces.** ``modules[].module`` is an RTL module
  name as Yosys saw it. ``instances[].module`` is the Liberty cell a leaf
  instantiates. Joining on it answers Liberty-cell questions only and
  cannot attribute power to an RTL module. Module payloads carry the
  ``namespaces`` the name was found in and set ``instance_join`` to
  :data:`INSTANCE_JOIN_LIBERTY_ONLY` or
  :data:`INSTANCE_JOIN_NAME_COLLISION` when the join would mislead.
* **Malformed documents are refused at the read.** A JSON root that is
  not an object (:func:`_require_mapping`), a nested block of the wrong
  shape (:func:`_require_blocks`) and an unknown ``schema_version``
  (:func:`_require_schema`) each raise :class:`PhysQueryError`, so
  ``--machine`` returns an error envelope instead of a traceback.
* **Publication pairs.** The model and manifest are written one after the
  other and carry the same ``publication`` token
  (:func:`rtl_buddy.phys.model.new_publication`). :func:`load_context`
  re-reads on a mismatch.
"""

from __future__ import annotations

import difflib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from . import manifest as manifest_mod
from . import provenance as provenance_mod
from .model import MODEL_SCHEMA_VERSION, load_model, provenance_of

logger = logging.getLogger(__name__)

#: Bumped when a payload's shape changes incompatibly; on every payload.
PHYS_QUERY_SCHEMA_VERSION = 1

#: Reads of a pair whose ``publication`` tokens disagree before giving up.
PUBLICATION_ATTEMPTS = 3

#: Seconds between those reads.
PUBLICATION_RETRY_SECONDS = 0.05

#: Runs listed by ``rb phys runs`` before truncation.
DEFAULT_RUNS_LIMIT = 20

#: Rows per ranking in ``rb phys summary`` before truncation.
DEFAULT_RANK_LIMIT = 10

#: The per-ranking limit that lists no rows. ``--limit 0`` means all, so
#: "none" needs a word; it is passed through the builders and MCP tools
#: as this string and appears as itself in ``limits``.
RANK_NONE = "none"

#: A per-ranking limit: an ``int`` head (``0`` for all), :data:`RANK_NONE`,
#: or ``None`` to use the shared limit.
RankLimit = int | str | None

#: The power columns of an instance row: the total, then its parts.
POWER_COLUMNS = ("total_uw", "internal_uw", "switching_uw", "leakage_uw")

#: The command that fills each model half.
HALF_PRODUCER = {"modules": "rb synth", "instances": "rb power"}

#: The ``provenance`` block each half's producer fills.
# tests/test_phys_verbs.py pins it against the model's ``_HALVES``.
HALF_PROVENANCE = {"modules": "synth", "instances": "power"}

#: Instance path separators. OpenSTA prints ``/`` and RTL-side consumers
#: use ``.``, so both are accepted.
_PATH_SEPARATORS = ("/", ".")

#: Starts a Verilog escaped identifier (``\gen[0].u_x``), whose ``.`` is
#: part of the name. Only a backslash at the start of a segment counts.
_ESCAPE_LEAD = "\\"

#: The separator all path comparisons use. Output rows keep the model's
#: own spelling.
_CANONICAL_SEPARATOR = "/"

#: ``instance_join`` note for a module found only in the synthesis half
#: when the populated power half has no row for it. A string, so machine
#: readers gate on ``is not None`` and all surfaces show one reason.
INSTANCE_JOIN_LIBERTY_ONLY = (
    "liberty-cell names only: no instance row carries this RTL module, so "
    "power cannot be attributed to it until the hierarchy join lands"
)

#: Namespace of the synthesis half's ``module`` column: an RTL module name.
NAMESPACE_RTL = "rtl"

#: Namespace of the power half's ``module`` column: a Liberty cell name.
NAMESPACE_LIBERTY = "liberty"

#: Both namespaces, in payload order.
NAMESPACES = (NAMESPACE_RTL, NAMESPACE_LIBERTY)

#: ``instance_join`` note for a name that is an RTL module and also a
#: Liberty cell, so the two halves measure different things.
INSTANCE_JOIN_NAME_COLLISION = (
    "name collision: this name is an RTL module in the synthesis half *and* "
    "a liberty cell in the power half, so the cells and area below are the "
    "module's while the power is that cell type's - they are two "
    "measurements of two things, not one module's totals"
)


def level_path(path: str) -> str:
    r"""One instance path in the separator every comparison here uses.

    Every ``/`` and ``.`` is a level, except inside a Verilog escaped
    identifier: a backslash that opens a segment runs to the next
    whitespace or the end of the path, and its ``.`` is part of the name.
    The stored form keeps the backslash and drops the terminator
    (``u_top/\gen[0].u_x``), because the readers keep only paths without
    spaces. A terminated form typed by a user is accepted and the
    terminator dropped.

    The hub's `/phy` pane levels paths the same way in ``toWirePath``;
    change the two together.
    """
    text = str(path)
    levelled: list[str] = []
    at_segment_start = True
    index = 0
    while index < len(text):
        char = text[index]
        if at_segment_start and char == _ESCAPE_LEAD:
            end = index
            while end < len(text) and not text[end].isspace():
                end += 1
            levelled.append(text[index:end])
            while end < len(text) and text[end].isspace():
                end += 1
            index = end
            at_segment_start = False
            continue
        if char in _PATH_SEPARATORS:
            levelled.append(_CANONICAL_SEPARATOR)
            at_segment_start = True
        else:
            levelled.append(char)
            at_segment_start = False
        index += 1
    return "".join(levelled)


class PhysQueryError(FatalRtlBuddyError):
    """A physical question that cannot be answered as asked.

    ``candidates`` holds near misses for an unknown module name or
    instance path.
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

    ``manifest`` names the document (or its directory), ``phys_dir`` names
    the directory holding it, and with neither the newest manifest in the
    project is used, as in the coverage verbs.
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
    """Load the manifest and the model it names, as one publication.

    A manifest with no readable model is an error. A run that measured
    nothing still writes a model with both halves ``null``, which the
    payloads report rather than refuse.

    If the ``publication`` tokens differ, the pair is re-read up to
    :data:`PUBLICATION_ATTEMPTS` times, :data:`PUBLICATION_RETRY_SECONDS`
    apart, to ride out a publish between its two writes. If they still
    differ, the last read is returned and a DEBUG event is logged, because
    the read holds no lock and refusing would be worse.
    """
    manifest_path = resolve_manifest_path(
        project_root, phys_dir=phys_dir, manifest=manifest
    )
    for attempt in range(PUBLICATION_ATTEMPTS):
        ctx = _read_publication(manifest_path, project_root)
        if ctx.manifest.get("publication") == ctx.model.get("publication"):
            return ctx
        if attempt + 1 < PUBLICATION_ATTEMPTS:
            time.sleep(PUBLICATION_RETRY_SECONDS)
    log_event(
        logger,
        logging.DEBUG,
        "phys.publication_mismatch",
        manifest=ctx.manifest_path,
        model=ctx.model_path,
        attempts=PUBLICATION_ATTEMPTS,
    )
    return ctx


def _read_publication(manifest_path: str, project_root) -> PhysContext:
    """One read of the manifest and the model it names."""
    try:
        document = manifest_mod.load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise PhysQueryError(f"phys: cannot read {manifest_path}: {exc}")
    _require_mapping(document, manifest_path, "manifest")
    _require_schema(
        document, manifest_mod.MANIFEST_SCHEMA_VERSION, manifest_path, "manifest"
    )
    _require_blocks(document, manifest_path, "manifest")

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
    _require_mapping(model, model_path, "model")
    _require_schema(model, MODEL_SCHEMA_VERSION, model_path, "model")
    _require_blocks(model, model_path, "model")

    return PhysContext(
        project_root=root,
        manifest_path=manifest_path,
        manifest=document,
        model=model,
        model_path=model_path,
    )


#: JSON's names for value kinds, used in refusal messages.
_JSON_KINDS = {
    type(None): "null",
    bool: "a boolean",
    int: "a number",
    float: "a number",
    str: "a string",
    list: "an array",
    dict: "an object",
}

#: The shapes a nested block is read as, worded as the refusal words them.
_SHAPE_MAPPING = "an object"
_SHAPE_ROWS = "an array of objects"
_SHAPE_TEXT = "a string"

#: Every nested field that is dereferenced, and the shape it is read as.
#: ``null`` is admitted everywhere, since a half-filled model is an
#: ordinary state. ``phys_dir`` is included because
#: :func:`~rtl_buddy.phys.manifest.project_root_for` walks it. Header
#: fields that are only echoed are not checked.
_NESTED_SHAPES = {
    "manifest": (
        ("model", _SHAPE_TEXT),
        ("phys_dir", _SHAPE_TEXT),
        ("synth", _SHAPE_MAPPING),
        ("power", _SHAPE_MAPPING),
        ("totals", _SHAPE_MAPPING),
    ),
    "model": (
        ("units", _SHAPE_MAPPING),
        ("totals", _SHAPE_MAPPING),
        ("modules", _SHAPE_ROWS),
        ("instances", _SHAPE_ROWS),
    ),
}


def _json_kind(value) -> str:
    """What ``value`` is, in JSON's vocabulary."""
    return _JSON_KINDS.get(type(value), "not a JSON value")


def _require_mapping(document, path, what: str) -> None:
    """Refuse a document whose JSON root is not an object.

    Checked first, so a list or ``null`` raises :class:`PhysQueryError`
    (an error envelope under ``--machine``) and not ``AttributeError``.
    """
    if isinstance(document, dict):
        return
    raise PhysQueryError(
        f"phys: {path} is not a {what} document: its JSON root is "
        f"{_json_kind(document)}, and a {what} "
        "is an object; re-run `rb synth` or `rb power` to rewrite it"
    )


def _shape_ok(value, shape: str) -> bool:
    """Whether ``value`` is the ``shape`` a reader here indexes it as."""
    if shape == _SHAPE_MAPPING:
        return isinstance(value, dict)
    if shape == _SHAPE_TEXT:
        return isinstance(value, str)
    return isinstance(value, list) and all(isinstance(row, dict) for row in value)


def _require_blocks(document: dict, path, what: str) -> None:
    """Refuse a document whose nested blocks are not the shapes they are read as.

    Shallow on purpose: rows are checked to be objects and keys are never
    checked, since every column is read with ``.get`` and may legitimately
    be ``None``.
    """
    for field, shape in _NESTED_SHAPES[what]:
        value = document.get(field)
        if value is None or _shape_ok(value, shape):
            continue
        found = (
            "an array whose rows are not all objects"
            if shape == _SHAPE_ROWS and isinstance(value, list)
            else _json_kind(value)
        )
        raise PhysQueryError(
            f"phys: {path} is not a readable {what}: its `{field}` is {found}, "
            f"and a {what}'s `{field}` is {shape} or null; re-run `rb synth` "
            "or `rb power` to rewrite it"
        )


def _require_schema(document: dict, supported: int, path, what: str) -> None:
    """Refuse a document whose ``schema_version`` is not ``supported``.

    Older and newer versions are both refused, and so is an absent one.
    The message names both versions so the user knows whether to re-run
    the flow or upgrade rtl_buddy.
    """
    found = document.get("schema_version")
    if found == supported:
        return
    raise PhysQueryError(
        f"phys: {path} is a {what} of schema_version "
        f"{'(absent)' if found is None else found}, and this rtl-buddy reads "
        f"{supported}; re-run `rb synth` or `rb power` to rewrite it, or "
        "upgrade rtl-buddy to the version that wrote it"
    )


# ---------------------------------------------------------------------------
# shared payload pieces
# ---------------------------------------------------------------------------


def artefacts_block(ctx: PhysContext) -> dict:
    """Every artefact path this run produced, project-relative.

    Both producer blocks are flattened under ``synth_`` and ``power_``
    prefixes, so every key is always present and ``null`` means "not
    produced".
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
    """Which halves of the model this document has.

    Per half: ``present``; ``rows`` (a count, or ``null`` when absent, so
    ``0`` is a real answer); ``produced_by``, the command that fills it;
    ``netlist_hash``, whether its producer recorded a netlist hash (the
    merge is gated on it, and a ``netlist-source: pnr`` power half has
    none); and ``mode`` and ``activity``, which are ``null`` on
    ``modules``.
    """
    block = {}
    provenance = provenance_of(model)
    for half, command in HALF_PRODUCER.items():
        rows = model.get(half)
        recorded = provenance[HALF_PROVENANCE[half]]
        block[half] = {
            "present": rows is not None,
            "rows": None if rows is None else len(rows),
            "produced_by": command,
            "netlist_hash": recorded["netlist_sha256"] is not None,
            "mode": recorded.get("mode"),
            "activity": provenance_mod.normalise_activity(recorded.get("activity")),
        }
    return block


def missing_halves(model: dict) -> list[str]:
    """The halves this model does not have, in a stable order."""
    return [half for half in HALF_PRODUCER if model.get(half) is None]


def _run_block(ctx: PhysContext) -> dict:
    """The header every payload opens with: which run this is.

    Identity comes from the manifest: ``power_mode`` and
    ``power_activity`` say which kind of power the numbers are, ``config``
    holds each half's fingerprint, and ``xplr`` names the ``rb xplr``
    experiment the manifest sits under, or ``null``.
    """
    document = ctx.manifest
    synth = document.get("synth") or {}
    power = document.get("power") or {}
    return {
        "schema_version": PHYS_QUERY_SCHEMA_VERSION,
        "manifest": manifest_mod.project_relative(ctx.manifest_path, ctx.project_root),
        "model": document.get("model"),
        "generated_at": document.get("generated_at"),
        # Not `command`: the machine envelope uses that key.
        "run_command": document.get("command"),
        "run": document.get("run"),
        "top": document.get("top"),
        "backends": {"synth": synth.get("backend"), "power": power.get("backend")},
        "power_mode": power.get("mode"),
        "power_activity": provenance_mod.normalise_activity(power.get("activity")),
        "config": {
            "synth": provenance_mod.normalise_config(synth.get("config")),
            "power": provenance_mod.normalise_config(power.get("config")),
        },
        "xplr": provenance_mod.experiment_for(ctx.manifest_path),
        "units": ctx.model.get("units", {}),
    }


def _module_rows(model: dict) -> list[dict]:
    return list(model.get("modules") or [])


def _instance_rows(model: dict) -> list[dict]:
    return list(model.get("instances") or [])


def truncate(rows: list, limit: int | None) -> list:
    """The head of ``rows`` for a ``limit``.

    ``None`` and ``0`` (from a CLI flag) mean the complete list, and a
    positive number is a head. Payloads carry the applied ``limit`` and
    the untruncated count.
    """
    return rows if limit is None or limit <= 0 else rows[:limit]


def _sort_key_desc(value):
    """Order a possibly-``None`` metric descending, nulls last.

    An unmeasured metric is not zero (Yosys writes no ``area`` without a
    Liberty), so it sorts after every measured value.
    """
    return (0, -value) if isinstance(value, (int, float)) else (1, 0.0)


def heaviest_modules(model: dict, limit: int | None = None) -> list[dict]:
    """Module rows ranked by cell count, then area, then name.

    Cell count leads because ``area_um2`` is ``null`` for unmapped runs.
    """

    def key(row):
        return (
            _sort_key_desc(row.get("cell_count")),
            _sort_key_desc(row.get("area_um2")),
            str(row.get("module") or ""),
        )

    return truncate(sorted(_module_rows(model), key=key), limit)


def hottest_key(row) -> tuple:
    """The order every list of instance rows is presented in.

    Total power descending with nulls last, then path. Shared by the
    global ranking, a module's instances and a subtree's children.
    """
    return (
        _sort_key_desc(row.get("total_uw")),
        str(row.get("instance_path") or ""),
    )


def hottest_instances(model: dict, limit: int | None = None) -> list[dict]:
    """Instance rows ranked by total power, then path."""
    return truncate(sorted(_instance_rows(model), key=hottest_key), limit)


def _power_sum(rows) -> dict:
    """Add up the power columns over ``rows``.

    A column no row measured stays ``None`` and does not become ``0.0``.
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


def parse_rank_limit(value: str | None) -> RankLimit:
    """Parse a per-ranking limit from a CLI flag.

    ``None`` stays ``None``, meaning the shared ``--limit``. The word
    :data:`RANK_NONE` lists no rows, a non-negative integer is a head, and
    ``0`` means all. Anything else raises :class:`ValueError`, which the
    CLI turns into a usage error.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if text == RANK_NONE:
        return RANK_NONE
    try:
        number = int(text, 10)
    except ValueError:
        raise ValueError(
            f"expected an integer or '{RANK_NONE}', not {value!r}; "
            "0 lists every row, a positive number heads the ranking, and "
            f"'{RANK_NONE}' lists none of it"
        ) from None
    if number < 0:
        raise ValueError(
            f"expected 0 or greater, not {number}; 0 lists every row and "
            f"'{RANK_NONE}' lists none of it"
        )
    return number


def _rank_rows(builder, model: dict, limit: RankLimit) -> list[dict]:
    """One ranking, or ``[]`` for :data:`RANK_NONE` without calling ``builder``.

    Skipping the builder avoids sorting rows that would be dropped.
    """
    if limit == RANK_NONE:
        return []
    return builder(model, limit)


def summary_payload(
    ctx: PhysContext,
    *,
    limit: int = DEFAULT_RANK_LIMIT,
    modules_limit: RankLimit = None,
    instances_limit: RankLimit = None,
) -> dict:
    """The run header, the totals sanity block and the two rankings.

    Totals and rows come from different scrapes and are both reported,
    never derived from each other. ``limit`` heads both rankings;
    ``modules_limit`` and ``instances_limit`` override it per ranking and
    may be :data:`RANK_NONE`. The applied values are in ``limits``.
    ``counts`` is always the model's row count, so a suppressed ranking is
    not mistaken for a half the run never produced, which is
    ``counts[half] is None``.
    """
    model = ctx.model
    modules_limit = limit if modules_limit is None else modules_limit
    instances_limit = limit if instances_limit is None else instances_limit
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
            "limits": {"modules": modules_limit, "instances": instances_limit},
            "modules": _rank_rows(heaviest_modules, model, modules_limit),
            "instances": _rank_rows(hottest_instances, model, instances_limit),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload


def _names_in(rows) -> set[str]:
    return {str(row["module"]) for row in rows if row.get("module")}


def namespaces_of(model: dict, name: str) -> list[str]:
    """Which halves' ``module`` column spells ``name``.

    ``["rtl"]``, ``["liberty"]`` or both, in :data:`NAMESPACES` order. A
    name can be in both, for example an RTL module called ``DFF_X1``.
    """
    found = {
        NAMESPACE_RTL: _names_in(_module_rows(model)),
        NAMESPACE_LIBERTY: _names_in(_instance_rows(model)),
    }
    return [space for space in NAMESPACES if name in found[space]]


def module_names(model: dict) -> list[str]:
    """Every module name the model can be asked about, sorted.

    The union of both halves, since a power-only model can still answer
    Liberty-cell questions.
    """
    names = {str(row["module"]) for row in _module_rows(model) if row.get("module")}
    names.update(
        str(row["module"]) for row in _instance_rows(model) if row.get("module")
    )
    return sorted(names)


def resolve_module_name(model: dict, module: str, *, where=None) -> str:
    """Resolve a user's module name to the name the model spells it with.

    An exact match wins. Otherwise a case-insensitive match is accepted
    only if unambiguous; several case variants raise
    :class:`PhysQueryError` with the variants as candidates. An unknown
    name raises with near misses, and the message names the command that
    would add a missing half.
    """
    known = module_names(model)
    if module in known:
        return module
    variants = [name for name in known if name.lower() == module.lower()]
    if len(variants) == 1:
        return variants[0]
    if variants:
        raise PhysQueryError(
            f"phys: {module!r} is ambiguous in {where or 'the physical model'}: "
            f"{len(variants)} modules differ from it only by case",
            candidates=variants,
        )
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


def module_payload(ctx: PhysContext, module: str, *, limit: int | None = None) -> dict:
    """One module's synthesis row and its instances, with power.

    ``row`` is the synthesis row and ``instances`` are the power rows
    whose Liberty cell is that name. Either may be ``null``, and
    ``missing_halves`` names what is absent. ``namespaces`` says which
    halves the name was found in. ``instance_join`` qualifies the join
    (:func:`_instance_join_note`).

    ``limit`` heads ``instances`` and defaults to the complete list.
    ``instance_count`` and ``power`` are always over every matching
    instance.
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
            key=hottest_key,
        )

    namespaces = namespaces_of(model, resolved)
    payload = _run_block(ctx)
    payload.update(
        {
            "module": resolved,
            "namespaces": namespaces,
            "row": row,
            "instances": None if instances is None else truncate(instances, limit),
            "instance_count": None if instances is None else len(instances),
            "limit": limit,
            "power": None if instances is None else _power_sum(instances),
            "instance_join": _instance_join_note(model, row, instances, namespaces),
            "halves": halves_block(model),
            "missing_halves": missing_halves(model),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload


def _instance_join_note(model: dict, row, instances, namespaces) -> str | None:
    """The note on a module payload's instance join, or ``None``.

    A name in both namespaces gets :data:`INSTANCE_JOIN_NAME_COLLISION`.
    Otherwise :data:`INSTANCE_JOIN_LIBERTY_ONLY` is returned only when the
    name came from the synthesis half, the power half has rows and
    nothing matched.
    """
    if len(namespaces) > 1:
        return INSTANCE_JOIN_NAME_COLLISION
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

    Both sides are levelled first (:func:`level_path`). A separator must
    follow the prefix, so ``u_cpu`` does not claim ``u_cpu_regs``.
    """
    path = level_path(path)
    prefix = level_path(prefix)
    if not path.startswith(prefix) or len(path) <= len(prefix):
        return False
    return path[len(prefix)] == _CANONICAL_SEPARATOR


def subtree_rollup(rows: list[dict]) -> dict:
    """Sum ``rows`` into a leaf count and the power columns.

    There is no area: the model has no per-instance area, and adding the
    synthesis row's module area per leaf would count it once per instance.
    """
    return {"instances": len(rows), **_power_sum(rows)}


def instance_payload(ctx: PhysContext, path: str, *, limit: int | None = None) -> dict:
    """One instance's row, or the subtree its path is the root of.

    An exact match wins, then a prefix match. Paths are compared levelled
    (:func:`level_path`), rows keep the model's spelling, and
    ``instance_path`` echoes the query. ``match`` is ``"exact"`` or
    ``"prefix"``.

    ``rollup`` covers only what ``match`` says: the named row for an
    exact match, the whole subtree for a prefix match. ``children`` lists
    the descendants in :func:`hottest_key` order either way, as
    navigation and not as summands. ``limit`` heads ``children`` and
    defaults to the complete list. ``child_count`` and ``rollup`` are
    always over every row.
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
        key=hottest_key,
    )
    if exact is None and not children:
        known = instance_paths(model)
        raise PhysQueryError(
            f"phys: no instance {path!r} in {ctx.model_path}",
            candidates=difflib.get_close_matches(path, known, n=10, cutoff=0.4)
            or known[:10],
        )

    covered = [exact] if exact is not None else children
    payload = _run_block(ctx)
    payload.update(
        {
            "instance_path": path,
            "match": "exact" if exact is not None else "prefix",
            "instance": exact,
            "children": truncate(children, limit),
            "child_count": len(children),
            "limit": limit,
            "rollup": subtree_rollup(covered),
            "halves": halves_block(model),
            "missing_halves": missing_halves(model),
            "artefacts": artefacts_block(ctx),
        }
    )
    return payload


# ---------------------------------------------------------------------------
# the run listing
# ---------------------------------------------------------------------------


def runs_payload(project_root, *, limit: int | None = None) -> dict:
    """Every run with physical artefacts under a project, newest first.

    The order is that of
    :func:`~rtl_buddy.phys.manifest.discover_manifests`, so ``runs[0]`` is
    the run the other verbs use by default, and each entry also carries
    ``newest``. Only manifests are read, so row counts and totals are not
    included; ``rb phys summary --phys-dir`` gives them. A manifest that
    cannot be read is listed with ``error`` set, so the listing never
    reports fewer runs than exist.
    """
    found = manifest_mod.discover_manifests(project_root)
    shown = truncate(found, limit)
    return {
        "schema_version": PHYS_QUERY_SCHEMA_VERSION,
        "count": len(found),
        "limit": limit,
        "runs": [
            _run_entry(path, project_root, newest=index == 0)
            for index, path in enumerate(shown)
        ],
    }


def _run_entry(manifest_path, project_root, *, newest: bool) -> dict:
    """One manifest as a row of the run listing.

    Never raises and never omits a key. ``phys_dir`` is derived from where
    the manifest was found, relative to ``project_root``, so it can be
    passed straight back to ``--phys-dir`` even for an unreadable row.
    """
    entry = {
        "manifest": manifest_mod.project_relative(manifest_path, project_root),
        "phys_dir": manifest_mod.project_relative(
            os.path.dirname(os.path.abspath(str(manifest_path))), project_root
        ),
        "run": None,
        "top": None,
        "run_command": None,
        "generated_at": None,
        "backends": {"synth": None, "power": None},
        "mode": None,
        "activity": None,
        "config": {"synth": None, "power": None},
        # From the path, so present even for an unreadable document.
        "xplr": provenance_mod.experiment_for(manifest_path),
        "fingerprint": None,
        "newest": newest,
        "error": None,
    }
    try:
        document = manifest_mod.load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        entry["error"] = f"cannot read {entry['manifest']}: {exc}"
        return entry
    if not isinstance(document, dict):
        entry["error"] = (
            f"{entry['manifest']} is not a manifest document: its JSON root "
            f"is {_json_kind(document)}"
        )
        return entry
    found = document.get("schema_version")
    if found != manifest_mod.MANIFEST_SCHEMA_VERSION:
        # Per row and not raised, so one bad manifest keeps the others listed.
        entry["error"] = (
            f"{entry['manifest']} is a manifest of schema_version "
            f"{'(absent)' if found is None else found}, and this rtl-buddy "
            f"reads {manifest_mod.MANIFEST_SCHEMA_VERSION}"
        )
        return entry
    synth = _mapping(document.get("synth"))
    power = _mapping(document.get("power"))
    entry.update(
        {
            "run": document.get("run"),
            "top": document.get("top"),
            "run_command": document.get("command"),
            "generated_at": document.get("generated_at"),
            "backends": {
                "synth": synth.get("backend"),
                "power": power.get("backend"),
            },
            "mode": power.get("mode"),
            "activity": provenance_mod.normalise_activity(power.get("activity")),
            "config": {
                "synth": provenance_mod.normalise_config(synth.get("config")),
                "power": provenance_mod.normalise_config(power.get("config")),
            },
        }
    )
    entry["fingerprint"] = config_label(entry["config"])
    return entry


def config_label(config) -> str | None:
    """The one config summary a listing shows for a run.

    The synthesis half's when there is one, the power half's otherwise,
    because the netlist is what an optimisation experiment varies. Shared
    so the CLI table, MCP payload and pane dropdown agree.
    """
    for half in ("synth", "power"):
        block = (config or {}).get(half) if isinstance(config, dict) else None
        if isinstance(block, dict) and block.get("summary"):
            return block["summary"]
    return None


def _mapping(value) -> dict:
    """``value`` when it is an object, otherwise an empty dict.

    The run listing's lenient counterpart to :func:`_require_blocks`.
    """
    return value if isinstance(value, dict) else {}
