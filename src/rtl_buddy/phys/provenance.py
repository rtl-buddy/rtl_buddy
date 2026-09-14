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
trace, the scope, and the toggle/duty pair a synthetic run used.

**The config block** (both halves). Two experiments of one design share
a ``top``, and auto-generated variants share everything a run name would
tell you apart. :func:`config_block` records the platform, the
constraints file and its hash, and a short digest over the effective
tool options — enough to read ``nangate45 / timing-opt`` apart from
``nangate45 / area-opt`` without decoding run names.

**What feeds the options digest**, exactly: the mapping its producer
hands :func:`options_digest`, rendered as canonical JSON (sorted keys,
no whitespace) and sha256'd to :data:`OPTIONS_DIGEST_CHARS` hex
characters. The synthesis flows pass the *resolved*
:class:`~rtl_buddy.config.synth.SynthToolOpts` they already compute —
tool-level defaults with the effort's ``synth-args``/``abc-args`` and
the per-synthesis ``tool_overrides`` folded in — plus the elaboration
parameters and defines, which shape the netlist as surely as an ABC
script does. The power flow passes its tool name, netlist source,
register level and tool overrides. The digest is over the *effective*
values, not the files they came from: two configs that spell one
setting differently and resolve to the same options are one experiment,
which is the comparison a reader wants.

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
import os
from pathlib import Path

from ..tools.artifact_paths import ARTIFACT_DIRNAME
from ..xplr.ledger import (
    LEDGER_DIRNAME as XPLR_DIRNAME,
    RECORD_FILENAME as XPLR_RECORD_FILENAME,
    RESERVED_DIRNAMES as XPLR_RESERVED_DIRNAMES,
)

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
#: vocabulary, and the other five are the detail behind whichever of the
#: four it names.
ACTIVITY_KEYS = ("source", "trace", "test", "scope", "toggle_rate", "duty")

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
    """
    return {
        "platform": platform or None,
        "effort": effort or None,
        "constraints": str(constraints) if constraints else None,
        "constraints_sha256": constraints_sha256,
        "options_sha256": options_digest(options),
    }


def activity_block(
    *,
    source: str | None = None,
    trace=None,
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
    """
    trace = str(trace) if trace else None
    synthetic = source == "synthetic"
    return {
        "source": source or None,
        "trace": trace,
        "test": trace_test(trace),
        "scope": scope or None,
        "toggle_rate": toggle_rate if synthetic else None,
        "duty": duty if synthetic else None,
    }


def options_digest(options) -> str | None:
    """A short, deterministic digest of an effective option set.

    Canonical JSON — keys sorted, no whitespace, non-JSON values through
    ``repr`` — so the same options digest the same on every machine and
    in every Python. ``None`` for a producer that passed nothing, which
    is the honest answer for a backend that has no options to fingerprint
    rather than a digest of the empty mapping.
    """
    if not options:
        return None
    canonical = json.dumps(options, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:OPTIONS_DIGEST_CHARS]


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
