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
questions ("how much do all the DFFs burn") and nothing else. It does
*not* attribute power to an RTL module, and a flat netlist is no
exception: the join matches the power half's ``module`` field as it
stands, and no leaf row carries ``u_cpu``'s name — or the top's — so it
simply misses.

Rather than invent an instance→RTL-module mapping here, the surfaces say
so: every module payload carries the ``namespaces`` the name it resolved
was found in, and ``module_payload`` sets :data:`INSTANCE_JOIN_LIBERTY_ONLY`
on ``instance_join`` when it can see that shape. One name can be in
*both* — a design with an RTL module called ``DFF_X1``, or a cell named
after a block — and then the two halves are answering about two
different things under one word; that payload says so
(:data:`INSTANCE_JOIN_NAME_COLLISION`) rather than presenting the
module's cells and area beside the cell type's power as one row. Nothing here attributes
*area* to an instance or a subtree at all — see :func:`subtree_rollup`.
Real RTL-module↔instance attribution needs the hierarchy join, which is
tracked as its own phase on the epic (rtl-buddy/rtl_buddy#558).

**A document that is not an object is refused where it is read.** Both
files are JSON, and JSON's root may be a list, a string, a number, or
``null`` — an empty artefact directory rebuilt by hand, a truncated
write, a path pointed at the wrong file. Every payload here indexes into
the root by key, so an unchecked non-object would raise ``AttributeError``
out of the query layer, past :class:`PhysQueryError` and past the
machine-mode envelope that turns a refusal into a result an agent can
read. :func:`_require_mapping` makes it the same kind of refusal as an
unreadable file, naming the path and what was found there.

**And so is a block that is not the shape it is read as.** An object at
the root and a ``schema_version`` this build knows say the document is
one of these; neither says anything about its insides. A hand-edited or
half-written file can carry both over ``"synth": []``, ``"modules": 7``
or ``"totals": "x"`` and reach the builders, where ``len()`` on a number
and ``.get`` on a string raise past the envelope exactly as a non-object
root did. :func:`_require_blocks` refuses those at the read too, naming
the field and what it holds. It is deliberately shallow: each block is
checked for the shape it is *indexed* as and never for its keys, every
one of which is optional by design.

**A version this build does not know is an error, not a guess.** Both
documents carry a ``schema_version`` that their producers bump when the
shape changes incompatibly, and every payload here reads their blocks by
name. A document from a future rtl_buddy would be read with today's key
names and answered from whatever happened to still be spelled the same —
a summary that is wrong rather than absent. :func:`_read_publication`
refuses both documents outright, naming the version it found and the one
this build reads, which is the same class of answer as a document that
cannot be parsed at all.

**Reading a publication.** The model and the manifest that names it are
two files, written one after the other, so a reader can arrive between
the two writes and pair a new model with the old manifest. Both carry
the same ``publication`` token (see
:func:`rtl_buddy.phys.model.new_publication`) and
:func:`load_context` re-reads on a mismatch.
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
from .model import MODEL_SCHEMA_VERSION, load_model, provenance_of

logger = logging.getLogger(__name__)

#: Bumped when a payload's shape changes incompatibly. Rides on every
#: payload so an agent surface can tell.
PHYS_QUERY_SCHEMA_VERSION = 1

#: How many times :func:`load_context` re-reads a model+manifest pair
#: whose ``publication`` tokens disagree. Small because the only window
#: it covers is the gap between two ``os.replace`` calls in the same
#: function — a reader that is still mismatched after this is looking at
#: two documents that were never written together, not at a race.
PUBLICATION_ATTEMPTS = 3

#: Seconds between those attempts. Long enough for a publish to finish
#: its second write, short enough that a `rb phys summary` on a document
#: nobody is writing never notices.
PUBLICATION_RETRY_SECONDS = 0.05

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

#: Which ``provenance`` block each half's producer fills. The read side of
#: the model's own pairing (its ``_HALVES`` table), spelled here rather
#: than reached for privately — ``tests/test_phys_verbs.py`` pins the two
#: against each other so a third half cannot be added to one alone.
HALF_PROVENANCE = {"modules": "synth", "instances": "power"}

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

#: The namespace of the synthesis half's ``module`` column: an RTL module
#: name, as Yosys' ``stat`` saw it after elaboration.
NAMESPACE_RTL = "rtl"

#: The namespace of the power half's ``module`` column: the Liberty cell
#: a leaf instance is an instance of.
NAMESPACE_LIBERTY = "liberty"

#: Both namespaces, in the order a payload lists them.
NAMESPACES = (NAMESPACE_RTL, NAMESPACE_LIBERTY)

#: What ``module_payload`` puts on ``instance_join`` when the name it
#: resolved exists in *both* namespaces. The two halves are then about
#: two different things — an RTL module's cells and area, an unrelated
#: cell type's power — and combining them into one answer silently is
#: precisely the failure the namespace split exists to prevent. Said
#: rather than guessed at, because which of the two the user meant is
#: not knowable from the name they typed, and both halves are real.
INSTANCE_JOIN_NAME_COLLISION = (
    "name collision: this name is an RTL module in the synthesis half *and* "
    "a liberty cell in the power half, so the cells and area below are the "
    "module's while the power is that cell type's - they are two "
    "measurements of two things, not one module's totals"
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
    """Load the manifest and the model it names, as one publication.

    A manifest with no readable model is an error rather than an empty
    answer: both documents are written by the same code path, so a
    missing model means the artefacts were truncated, not that the run
    measured nothing. A run that measured nothing still writes a model —
    with both halves ``null`` — and that is a state the payloads report
    rather than refuse.

    **The pair, not the two files.** A publish replaces the model and
    then the manifest, each atomically but not together, so a read that
    lands between the two ``os.replace`` calls gets a new model under an
    old manifest — the totals from one run beside the artefact paths of
    another. Both documents carry the ``publication`` token of the write
    that produced them (:func:`rtl_buddy.phys.model.new_publication`), so
    the mismatch is *visible*: this re-reads the pair up to
    :data:`PUBLICATION_ATTEMPTS` times, :data:`PUBLICATION_RETRY_SECONDS`
    apart, which is far longer than the window.

    Still mismatched after that is **not** an error, and this is an
    advisory read: nothing here holds a lock, so the only alternatives
    are to refuse a question the documents can very nearly answer or to
    answer it from the freshest read of each. It takes the latter — the
    last pair read, which is what the two files say right now — and logs
    it. Two documents that genuinely disagree (one written by an
    rtl_buddy that predates the token, say, and one that does not) are
    the case that would otherwise never resolve. A document that cannot
    be *read* is still an error, as above.
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


#: JSON's own names for what a value is, so the refusals below say what
#: a document holds in the vocabulary of the format it is written in
#: rather than in Python's.
_JSON_KINDS = {
    type(None): "null",
    bool: "a boolean",
    int: "a number",
    float: "a number",
    str: "a string",
    list: "an array",
    dict: "an object",
}

#: The three shapes a nested block is read as here, spelled as the
#: refusal spells them.
_SHAPE_MAPPING = "an object"
_SHAPE_ROWS = "an array of objects"
_SHAPE_TEXT = "a string"

#: Every nested field a read here dereferences, and the shape each one
#: is dereferenced as. Named once, as data, because the check belongs at
#: the read and not in whichever payload builder happens to touch a block
#: first: one malformed document must be one refusal, whichever verb was
#: asked. ``null`` is admitted everywhere — a half-filled model and a
#: manifest with no power block are the ordinary states these payloads
#: report rather than refuse.
#:
#: "A read here" includes the manifest helpers this module reads
#: *through*: ``phys_dir`` is never indexed by a payload, but
#: :func:`~rtl_buddy.phys.manifest.project_root_for` calls
#: ``os.path.isabs`` on it and walks its ``parts`` on the way to every
#: artefact path, which a list or a number fails with a ``TypeError``
#: past the envelope. The header fields a payload only *echoes*
#: (``run``, ``top``, ``generated_at``, ``command``, ``publication``) are
#: deliberately absent: nothing dereferences them, so nothing here can
#: fail on their shape, and refusing a whole document over a field that
#: is passed through untouched would be strictness with no failure behind
#: it.
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

    Read before the ``schema_version`` check, because that check is
    itself a key lookup: everything downstream — the version, the
    blocks, the halves — assumes a mapping, and the first thing to touch
    a list or a ``null`` would raise ``AttributeError`` rather than
    :class:`PhysQueryError`. That distinction is the whole point: a
    ``PhysQueryError`` reaches ``--machine`` as an error envelope with a
    message in it, and an ``AttributeError`` reaches it as a traceback
    and no envelope at all.
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

    The row-level test is a plain ``all()`` over the list rather than a
    per-column schema, and that is the whole of the strictness on
    purpose: every column a payload reads is read with ``.get`` and
    every one of them is legitimately ``None`` somewhere (no Liberty, no
    ``area_um2``), so anything finer would refuse documents these
    payloads answer about correctly today. What it does catch is the
    thing that cannot be answered about at all — a block of the wrong
    *kind*, which is a truncated write or a hand edit rather than a
    measurement that did not happen.
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
    """Refuse a document whose ``schema_version`` this build cannot read.

    The producers bump the version when the shape changes incompatibly,
    so a value other than the one compiled in here means the blocks the
    payloads index into are not the blocks that were written. Both
    directions are refused rather than only the newer one: an older
    document is missing keys this build treats as guaranteed, and a
    newer one has moved them. The message names both versions, because
    which of the two is bigger is what tells a user whether to re-run
    the flow or upgrade rtl_buddy.

    A document with no ``schema_version`` at all is refused the same
    way, and reported as such — the producers have written the key since
    the first version of both documents, so its absence says this is not
    one of these documents rather than that it is an early one.
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

    ``netlist_hash`` is the provenance echo: whether this half's producer
    recorded the hash of the netlist it measured. It is what a surface
    needs before telling anyone how to fill the *other* half, because the
    merge is gated on that hash
    (:func:`rtl_buddy.phys.model.may_inherit_other_half`). A half without
    one — a ``netlist-source: pnr`` power run reads a routed database and
    has no netlist to hash — cannot be paired with, so the run that would
    otherwise complete the model replaces it instead. A boolean rather
    than the hash itself: whether there is one is the whole of what a
    consumer can act on, and the digest belongs to the model.
    """
    block = {}
    provenance = provenance_of(model)
    for half, command in HALF_PRODUCER.items():
        rows = model.get(half)
        block[half] = {
            "present": rows is not None,
            "rows": None if rows is None else len(rows),
            "produced_by": command,
            "netlist_hash": (
                provenance[HALF_PROVENANCE[half]]["netlist_sha256"] is not None
            ),
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


def truncate(rows: list[dict], limit: int | None) -> list[dict]:
    """The rows a payload lists, given the ``limit`` its caller asked for.

    One rule, named once, because three payloads and two surfaces obey
    it: ``None`` means the caller wants the complete list (the builders'
    default, and what the MCP tools pass), ``0`` means the same thing
    said by a CLI flag whose help documents ``0`` as "all", and anything
    positive is a head. Nothing here reports *that* it truncated — the
    payload carries the applied ``limit`` and the untruncated count
    beside the list, so a consumer can tell without being told.
    """
    return rows if limit is None or limit <= 0 else rows[:limit]


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

    return truncate(sorted(_module_rows(model), key=key), limit)


def hottest_key(row) -> tuple:
    """The order every list of instance rows is presented in.

    Total power descending with nulls last, path breaking the tie. Named
    once because three payloads sort by it — the ranking
    :func:`hottest_instances` is, the instances of a module, and the
    children of a subtree — and a list that quietly used a different one
    would head to a different set of rows under ``limit`` than the
    surface above it says it is heading (#563 review).
    """
    return (
        _sort_key_desc(row.get("total_uw")),
        str(row.get("instance_path") or ""),
    )


def hottest_instances(model: dict, limit: int | None = None) -> list[dict]:
    """Instance rows ranked by total power, then path."""
    return truncate(sorted(_instance_rows(model), key=hottest_key), limit)


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


def _names_in(rows) -> set[str]:
    return {str(row["module"]) for row in rows if row.get("module")}


def namespaces_of(model: dict, name: str) -> list[str]:
    """Which halves' ``module`` column spells ``name``.

    ``["rtl"]`` for a synthesis row, ``["liberty"]`` for the cell a leaf
    is an instance of, and *both* when one word is in both columns —
    which is a real shape, not a corner case: nothing stops a design
    from having a module called ``DFF_X1``, and a cell library from
    naming a cell after a block. The two halves then measure two
    different things under one name, and a payload that reported only
    the union of their rows would read as one.

    Ordered by :data:`NAMESPACES` rather than by which half was looked at
    first, so a consumer can compare the field for equality.
    """
    found = {
        NAMESPACE_RTL: _names_in(_module_rows(model)),
        NAMESPACE_LIBERTY: _names_in(_instance_rows(model)),
    }
    return [space for space in NAMESPACES if name in found[space]]


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

    An exact match is taken first and case is only a fallback, and that
    fallback applies only where it is unambiguous. Verilog is
    case-sensitive and a Liberty library need not agree with the RTL
    about case, so ``CPU`` and ``cpu`` can both be real names in one
    model — in one half, or one in each (see :func:`namespaces_of`). A
    single lowercase key cannot hold both, and the old lookup silently
    answered with whichever the dict had kept, reporting one block's
    cells and area under the other's name. Two or more case-variants is
    therefore a refusal that lists them as candidates: the user knows
    which they meant and spelling it exactly gets it, where a guess here
    is wrong half the time and says nothing about being a guess
    (#561 review).
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
    """One module's synthesis row and the instances of it, with power.

    The join the two halves make where they can: ``modules`` says how
    many cells and how much area the block is, ``instances`` says what
    the leaves *of that Liberty cell* burn. Either side may be ``null`` —
    the payload reports what it has and names the command that would
    supply the rest.

    ``namespaces`` says which halves' ``module`` column the resolved name
    was found in (:func:`namespaces_of`), so a reader knows what kind of
    name it is holding before it reads either half.

    ``instance_join`` is the honest signal about the join itself. It is
    ``null`` when there is nothing to qualify, and
    :data:`INSTANCE_JOIN_LIBERTY_ONLY` when the name resolved out of the
    synthesis half alone, matched no instance row, and the power half is
    populated — the shape of an RTL module on a mapped hierarchical
    design, whose leaves are named after Liberty cells and so can never
    match it. Without it a consumer cannot tell "this module has no
    instances" from "the join cannot see this module's instances", and
    the two call for opposite reactions.

    When ``namespaces`` holds both, it is :data:`INSTANCE_JOIN_NAME_COLLISION`
    instead: the row and the instances are then measurements of two
    unrelated things — an RTL module and a Liberty cell that happen to
    share a name — and the payload puts them side by side only because
    the user named one word. Nothing here picks a winner (both halves
    really do have rows under that name), and nothing sums across them;
    the note is what stops the pairing from being read as a module's
    own power.

    ``limit`` heads the ``instances`` list, and defaults to the complete
    one. It lives here rather than in the CLI's rendering because the
    machine payload is the CLI's *whole* output under ``--machine``: a
    flag honoured only in the table would print one row and emit ten
    thousand, which is the flag lying to exactly the consumer that cannot
    re-count. The MCP tools pass nothing and so keep the complete list,
    which is the contract they were registered with.

    A truncated list stays self-describing: ``instance_count`` is the
    number of instances there *are* — never the number listed — and
    ``limit`` is what was applied, so ``len(instances) < instance_count``
    is a head rather than a miss. ``power`` sums every matching instance
    for the same reason: it is the module's total, and a total over the
    first ``n`` rows of an arbitrary ranking is not a figure anyone
    asked for.
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
    """What, if anything, the reader would otherwise misread about the join.

    Two things can go wrong under one name, and they are opposites. A
    name in *both* namespaces joins rows that should never have been put
    together, and the note says so first, because that payload looks
    complete — a row, instances, a power total — and is the one nobody
    would think to question.

    Otherwise the note marks the empty join, and only when all three
    hold: the name came out of the synthesis half (so it is an RTL module
    name), the power half exists and has rows (so "no instances" is not
    simply "no power run"), and nothing matched. A Liberty cell that
    *did* match, or a genuinely instance-free design, gets no note.
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


def subtree_rollup(rows: list[dict]) -> dict:
    """Sum ``rows`` into one subtree figure: the leaf count and the power.

    The roll-up the model deliberately does not do. Power adds up
    straightforwardly because every row is a leaf and every column is a
    watt figure of that leaf alone.

    **No area.** There is no per-cell area anywhere in the model, so
    there is nothing here to sum. The obvious substitute — join each
    leaf's ``module`` to the synthesis half and add that row's area — is
    wrong twice over: the synthesis row's ``area_um2`` is the *whole
    module's* area, not one instance's, so a subtree with fifty
    ``DFF_X1`` leaves would add the ``DFF_X1`` row's total fifty times;
    and the two halves spell ``module`` in different namespaces (see this
    module's docstring), so on a Liberty/RTL name collision the figure
    would be some other module's area entirely — reported as covered.
    Area attribution to an instance needs the hierarchy join, which is
    Phase 5 of the epic (rtl-buddy/rtl_buddy#558); until then this says
    nothing about area rather than saying something arbitrary.
    """
    return {"instances": len(rows), **_power_sum(rows)}


def instance_payload(ctx: PhysContext, path: str, *, limit: int | None = None) -> dict:
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

    ``children`` is ordered by total power descending, nulls last, with
    the path breaking the tie — :func:`hottest_key`, the same ranking the
    other two instance lists use. It was lexicographic, which read the
    same as long as nothing was cut off it but headed the wrong rows the
    moment something was: ``limit`` truncates *after* the sort, and every
    surface that heads this list describes it as the heaviest children
    (#563 review).

    ``limit`` heads the ``children`` list and defaults to the complete
    one, exactly as :func:`module_payload`'s does and for the same
    reason. ``child_count`` is how many children there are and ``rollup``
    sums every leaf under the path, listed or not — a subtree total that
    counted only the rows that fitted on a terminal would be a different
    number under ``--limit 5`` than under ``--limit 0``.
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

    covered = ([exact] if exact is not None else []) + children
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
