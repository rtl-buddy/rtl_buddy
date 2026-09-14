# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""What shaped a run, beside what it measured (rtl-buddy/rtl_buddy#568).

The model says what a synthesis or a power analysis *measured*. This
module is the other question a project with more than one run has to
answer: **which run is this**. Two shapes, and the derivations that read
them back.

**The activity block** (power only). A power number is meaningless
without the switching activity behind it: 697 µW of static leakage and
697 µW driven by a SAIF trace from a named test are different
measurements of different things, and until now the model recorded
neither. :func:`activity_block` writes down what
:meth:`~rtl_buddy.config.power.PowerConfig.get_activity_source` resolved
— ``default``, ``synthetic``, ``saif`` or ``vcd`` — together with the
trace, its sha256 and the scope a run that read one used, and the
toggle/duty pair a synthetic one used. Each detail is recorded only for the source that
consumed it: a config keeps fields the run it describes never reads,
and a block that repeated them would report a stimulus nothing applied.

**The config block** (both halves). Two experiments of one design share
a ``top``, and auto-generated variants share everything a run name would
tell you apart. :func:`config_block` records the platform, the
constraints file and its hash, and a short digest over the effective
tool options — enough to read ``nangate45 / timing-opt`` apart from
``nangate45 / area-opt`` without decoding run names.

**What feeds the options digest**, exactly: the mapping its producer
hands :func:`options_digest`, rendered as canonical JSON (sorted keys,
no whitespace, and nothing that JSON cannot render) and sha256'd to
:data:`OPTIONS_DIGEST_CHARS` hex characters. The synthesis flows pass the *resolved*
:class:`~rtl_buddy.config.synth.SynthToolOpts` they already compute —
tool-level defaults with the effort's ``synth-args``/``abc-args`` and
the per-synthesis ``tool_overrides`` folded in — plus the elaboration
parameters and defines, which shape the netlist as surely as an ABC
script does. The power flow passes its tool name, netlist source, mode,
activity source and register level. The digest is over the *effective*
values, not the files they came from: two configs that spell one
setting differently and resolve to the same options are one experiment,
which is the comparison a reader wants.

That cuts both ways, and it is why the power flow's ``tool_overrides``
is *not* in there: no power backend reads the field, so two analyses
that differ only in it are the same analysis, and digesting it would
report a difference the numbers cannot have. Only what a backend
actually resolved and used belongs in the mapping it hands over.

Nothing here is a claim about correctness, and nothing here gates the
merge. The netlist hash in
:func:`rtl_buddy.phys.model.may_inherit_other_half` is what decides
whether two halves describe one design, and it is strictly stronger
than any config comparison: the same options can produce two netlists
(a source edit in between) and two option sets can produce one. These
blocks are for *telling runs apart* in a listing.

**Derived, not recorded.** Two labels and two identities are computed
on read rather than stored, so the wording can improve without
rewriting documents already on disk: :func:`activity_label` and
:func:`config_summary` phrase a block for a table or a dropdown,
:func:`trace_test` names the test whose artefact directory a trace came
out of, and :func:`experiment_for` names the ``rb xplr`` experiment a
manifest sits under. All four answer ``None`` when the evidence is not
there — an unlabelled run is reported as unlabelled, never guessed at.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from ..logging_utils import log_event
from ..tools.artifact_paths import ARTIFACT_DIRNAME
from ..xplr.ledger import (
    LEDGER_DIRNAME as XPLR_DIRNAME,
    RECORD_FILENAME as XPLR_RECORD_FILENAME,
    RESERVED_DIRNAMES as XPLR_RESERVED_DIRNAMES,
)

logger = logging.getLogger(__name__)

#: Every key of the config block, so a run that resolved none of them
#: still writes them all. Stable keys, ``null`` for "not recorded" — the
#: same rule the manifest's own blocks keep.
CONFIG_KEYS = (
    "platform",
    "effort",
    "constraints",
    "constraints_sha256",
    "options_sha256",
)

#: Every key of the activity block, likewise. ``source`` is the one to
#: read first: it is
#: :meth:`~rtl_buddy.config.power.PowerConfig.get_activity_source`'s own
#: vocabulary, and the other six are the detail behind whichever of the
#: four it names.
ACTIVITY_KEYS = (
    "source",
    "trace",
    "trace_sha256",
    "test",
    "scope",
    "toggle_rate",
    "duty",
)

#: How much of the options sha256 is kept. Long enough that two option
#: sets in one project will not collide, short enough to sit in a table
#: column and a dropdown entry beside the platform.
OPTIONS_DIGEST_CHARS = 12

#: What joins the parts of a rendered label. One spelling, because the
#: CLI table, the pane's dropdown and the run header all use it and a
#: reader comparing two surfaces should see one format.
LABEL_SEPARATOR = " · "

#: The activity sources that read a trace file, as opposed to the two
#: that do not. Named because both :func:`activity_label` and the
#: producers branch on the distinction.
TRACE_SOURCES = ("saif", "vcd")


def config_block(
    *,
    platform: str | None = None,
    effort: str | None = None,
    constraints=None,
    constraints_sha256: str | None = None,
    options=None,
    producer: str | None = None,
) -> dict:
    """The config fingerprint of one run, as the documents store it.

    ``options`` is digested rather than stored: the mapping is the
    backend's own resolved option set, which is long, tool-shaped and
    of no use to a reader scanning a list of runs — what a reader needs
    is whether two runs' options were the *same*, and a digest answers
    that in a column. See this module's docstring for exactly what each
    producer puts in it.

    ``constraints`` is kept as it was handed in; the publish path makes
    it project-relative before either document is written, for the same
    reason every other path in a manifest is
    (:func:`rtl_buddy.phys.manifest.project_relative`).

    ``producer`` names who filled ``options``, and is only ever used to
    say whose mapping could not be digested (:func:`options_digest`).
    """
    return {
        "platform": platform or None,
        "effort": effort or None,
        "constraints": str(constraints) if constraints else None,
        "constraints_sha256": constraints_sha256,
        "options_sha256": options_digest(options, producer=producer),
    }


def activity_block(
    *,
    source: str | None = None,
    trace=None,
    trace_sha256: str | None = None,
    scope: str | None = None,
    toggle_rate: float | None = None,
    duty: float | None = None,
) -> dict:
    """What drove a power analysis' switching, as the documents store it.

    ``test`` is not a parameter: it is derived from ``trace`` by
    :func:`trace_test`, because the producer has no test object to ask —
    a SAIF is a path in a power config, and the run that produced it
    finished in another command, possibly on another day. The
    derivation is from the artefact layout and is reported as ``null``
    whenever the layout does not say.

    ``toggle_rate`` and ``duty`` are recorded only for a run that
    actually used them. A ``saif`` run carries the same two numbers in
    its config — they are the defaults for signals the trace does not
    cover — but recording them beside a trace would read as "this is
    what drove the numbers", which is exactly the confusion the block
    exists to end.

    ``trace``, ``test`` and ``scope`` are gated the same way, on
    :data:`TRACE_SOURCES`, and for the same reason. A config keeps its
    ``activity.saif``/``activity.vcd``/``activity.scope`` across an edit
    that turns the run static — commenting out ``mode: dynamic``, or a
    variant generated from a base that had a trace — and
    :meth:`~rtl_buddy.config.power.PowerConfig.get_activity_source` then
    resolves ``default``. The Tcl emits no ``read_saif`` at all on that
    run, so the trace was not read, the test did not drive it, and the
    scope selected nothing; recording all three anyway put a named test
    beside a leakage number and made two static runs that differ only in
    a trace neither of them opened look like two different measurements.
    A ``synthetic`` run is the same case with the other pair of fields.

    ``trace_sha256`` identifies the trace by its *bytes*, and is taken by
    the producer for the same reason ``constraints_sha256`` is
    (:func:`config_block`): a path is a name, and a name is not an
    identity. ``dump.saif`` is rewritten in place every time its test is
    rerun, so a power run against a re-captured trace measures different
    switching under a path that has not changed — and without the hash
    the two runs' activity blocks are identical, which is the one thing
    a fingerprint must not say about two different measurements. The
    label does not carry it (a label is for a table cell, and twelve hex
    characters of a SAIF are not what a reader is scanning for), but a
    listing's activity block does, so the identity distinguishes what
    the label cannot.
    """
    reads_trace = source in TRACE_SOURCES
    trace = str(trace) if trace and reads_trace else None
    synthetic = source == "synthetic"
    return {
        "source": source or None,
        "trace": trace,
        "trace_sha256": trace_sha256 if reads_trace else None,
        "test": trace_test(trace),
        "scope": (scope or None) if reads_trace else None,
        "toggle_rate": toggle_rate if synthetic else None,
        "duty": duty if synthetic else None,
    }


def options_digest(options, *, producer: str | None = None) -> str | None:
    """A short, deterministic digest of an effective option set.

    Canonical JSON — keys sorted, no whitespace — so the same options
    digest the same on every machine and in every Python. ``None`` for a
    producer that passed nothing, which is the honest answer for a
    backend that has no options to fingerprint rather than a digest of
    the empty mapping.

    **Strict, and ``None`` when it cannot be.** The rendering used to
    fall back to ``repr`` for anything JSON could not take, which quietly
    voids the promise above: ``repr`` of most objects embeds the address
    they happen to live at, so the first non-primitive to reach an
    options mapping would fingerprint the same run differently on every
    invocation — and nothing about the digest says so, because a digest
    of garbage looks exactly like a digest. Every producer today passes
    JSON-safe values, so the fallback never fired; that is luck, and this
    makes it a checked invariant instead. A mapping that will not render
    is reported at DEBUG — naming ``producer`` and, where they can be
    told apart, the keys that would not go — and digests to ``None``. An
    absent fingerprint is honest about knowing nothing; an unstable
    present one is not.

    ``ValueError`` is caught alongside ``TypeError`` because a self-
    referential mapping fails that way rather than as an unknown type,
    and it is the same failure to a reader: no canonical rendering.
    """
    if not options:
        return None
    try:
        canonical = json.dumps(options, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        log_event(
            logger,
            logging.DEBUG,
            "phys.options_not_serialisable",
            producer=producer,
            keys=_unserialisable_keys(options),
            error=str(exc),
        )
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:OPTIONS_DIGEST_CHARS]


def _unserialisable_keys(options) -> list[str]:
    """Which of ``options``' values JSON would not take, for the DEBUG line.

    Empty when the mapping as a whole is what failed rather than any one
    value — a key that is not a string, a cycle closed through several
    entries — so the event says "these" only when it can.
    """
    if not isinstance(options, dict):
        return []
    bad = []
    for key, value in options.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            bad.append(str(key))
    return sorted(bad)


def normalise_config(recorded) -> dict | None:
    """A stored config block with every key present, plus its ``summary``.

    ``None`` when there is nothing recorded, which is what a document
    written before #568 carries and what a half that never ran carries.
    A reader must be able to tell "this run recorded no config" from
    "this run recorded a config with nothing in it", so an empty-but-
    present block comes back as a block of nulls rather than as
    ``None``.

    ``summary`` is derived here rather than stored; see the module
    docstring.
    """
    if not isinstance(recorded, dict):
        return None
    block = {key: recorded.get(key) for key in CONFIG_KEYS}
    block["summary"] = config_summary(block)
    return block


def normalise_activity(recorded) -> dict | None:
    """A stored activity block with every key present, plus its ``label``.

    The same rule :func:`normalise_config` keeps, for the same reason.
    """
    if not isinstance(recorded, dict):
        return None
    block = {key: recorded.get(key) for key in ACTIVITY_KEYS}
    block["label"] = activity_label(block)
    return block


def activity_label(activity) -> str | None:
    """One line naming what drove a power run's switching.

    Written for a table cell and a dropdown entry, so it is the shortest
    phrase that still distinguishes the four sources: ``defaults`` for a
    static run, the toggle and duty for a synthetic one, and the trace's
    test — or its filename, when the layout does not name a test — for a
    run that read one. ``None`` when the block records no source at all.
    """
    if not isinstance(activity, dict):
        return None
    source = activity.get("source")
    if not source:
        return None
    if source == "default":
        return "defaults"
    if source == "synthetic":
        parts = []
        if activity.get("toggle_rate") is not None:
            parts.append(f"toggle {_number(activity['toggle_rate'])}")
        if activity.get("duty") is not None:
            parts.append(f"duty {_number(activity['duty'])}")
        return ", ".join(parts) if parts else "synthetic"
    trace = activity.get("trace")
    detail = activity.get("test") or (os.path.basename(str(trace)) if trace else None)
    return f"{source} {detail}" if detail else str(source)


def config_summary(config) -> str | None:
    """One line naming the configuration a run was shaped by.

    Platform, effort, a short prefix of the constraints hash and the
    options digest, joined by :data:`LABEL_SEPARATOR` and with every
    absent part left out. ``None`` when nothing was recorded — a summary
    of an empty block would be an empty string, which reads in a table
    cell as a value rather than as an absence.
    """
    if not isinstance(config, dict):
        return None
    parts = []
    for key in ("platform", "effort"):
        if config.get(key):
            parts.append(str(config[key]))
    if config.get("constraints_sha256"):
        parts.append(f"sdc {str(config['constraints_sha256'])[:8]}")
    if config.get("options_sha256"):
        parts.append(f"opts {config['options_sha256']}")
    return LABEL_SEPARATOR.join(parts) or None


def trace_test(trace) -> str | None:
    """The test whose artefact directory ``trace`` came out of, if any.

    rtl_buddy writes a test's waveform into
    ``<suite>/artefacts/<test>/``, and ``rb saif`` converts it in place,
    so a SAIF at ``verif/demo/artefacts/csr_smoke/dump.saif`` was
    produced by ``csr_smoke``. That is a *derivation from the layout*,
    not a recorded fact: a trace kept anywhere else — a checked-in
    golden, a path outside the project — answers ``None`` rather than
    naming the directory it happens to sit in, because a directory that
    is not an artefact directory is not a test.
    """
    if not trace:
        return None
    parts = Path(str(trace)).parts
    parent = len(parts) - 2
    if parent >= 1 and parts[parent - 1] == ARTIFACT_DIRNAME:
        return parts[parent]
    return None


def experiment_for(manifest_path) -> dict | None:
    """The ``rb xplr`` experiment a manifest sits under, or ``None``.

    The ledger is one directory per experiment under
    ``artefacts/xplr/<exp-id>/`` (see :mod:`rtl_buddy.xplr.ledger`), so a
    flow run inside one writes its artefacts — and its manifest —
    somewhere below that directory. The id is therefore *derivable from
    the path*, which is the cheap half and the half that always works:
    it needs no read at all, and it holds for an experiment whose record
    has not been written yet.

    ``record.json`` is opened only for the label, and only when it is
    right there: the experiment's ``hypothesis`` is the one field that
    makes a listing readable, and the rest of the record is `rb xplr`'s
    to report. A record that is missing, unreadable or has no hypothesis
    costs the label and nothing else — the id still identifies the run.

    The ledger's own reserved directory names are refused, so
    ``artefacts/xplr/worktrees/...`` — the default worktree root, not an
    experiment — is not reported as an experiment called ``worktrees``.
    """
    parts = Path(os.path.abspath(str(manifest_path))).parts
    for index in range(len(parts) - 2):
        if parts[index] != ARTIFACT_DIRNAME or parts[index + 1] != XPLR_DIRNAME:
            continue
        exp_id = parts[index + 2]
        if exp_id in XPLR_RESERVED_DIRNAMES:
            return None
        root = Path(*parts[: index + 3])
        return {"id": exp_id, "label": _hypothesis(root / XPLR_RECORD_FILENAME)}
    return None


def _hypothesis(record_path) -> str | None:
    """The ``hypothesis`` of an experiment record, or ``None``.

    Read as plain JSON rather than through
    :func:`rtl_buddy.xplr.schema.loads_record`: this is a label on a
    listing, and a record that fails validation is `rb xplr`'s problem
    to report, not a reason for `rb phys runs` to refuse a row.
    """
    try:
        with open(record_path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    hypothesis = record.get("hypothesis")
    return hypothesis if isinstance(hypothesis, str) and hypothesis else None


def _number(value) -> str:
    """A toggle rate or duty as a label spells it: no trailing zeros."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


__all__ = [
    "ACTIVITY_KEYS",
    "CONFIG_KEYS",
    "LABEL_SEPARATOR",
    "OPTIONS_DIGEST_CHARS",
    "TRACE_SOURCES",
    "activity_block",
    "activity_label",
    "config_block",
    "config_summary",
    "experiment_for",
    "normalise_activity",
    "normalise_config",
    "options_digest",
    "trace_test",
]
