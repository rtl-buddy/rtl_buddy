# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""What shaped a run, beside what it measured.

Two blocks tell the runs of one project apart. The **activity block**
(power only) records what drove the switching: the source resolved by
:meth:`~rtl_buddy.config.power.PowerConfig.get_activity_source`
(``default``, ``synthetic``, ``saif`` or ``vcd``), plus the trace, its
sha256 and the scope for a trace source, or the toggle/duty pair for a
synthetic one. The **config block** (both halves) records the platform,
effort, constraints file and hash, and a short digest of the effective
tool options.

**What feeds the options digest.** Each producer hands
:func:`options_digest` exactly what its own generated script consumes.
The mapping is rendered as canonical JSON and sha256'd, keeping
:data:`OPTIONS_DIGEST_CHARS` hex characters. A field the script ignores
must not be in it, and one the script reads must be. The digest is over
effective values, so two configs that resolve to the same options are one
experiment.

* Yosys synthesis
  (:meth:`~rtl_buddy.tools.synth_yosys.YosysSynth._phys_options`): the
  elaboration subset
  (:func:`~rtl_buddy.tools.synth_yosys.elaboration_fingerprint`), the
  resolved ``synth-args``, parameters and defines, and which branch the
  script took. A mapped run adds the ABC delay target from the SDC and
  the resolved Liberty list and omits ``abc-args``. An unmapped run adds
  ``abc-args`` and neither of the others. ``strategy`` is in neither.
* OpenROAD synthesis
  (:meth:`~rtl_buddy.tools.synth_openroad.OpenRoadSynth._publish_phys_model`):
  the elaboration subset with ``synth-args`` taken from the effort, the
  command ``strategy`` maps to, the sha256 of the effort's pre-STA Tcl
  (:func:`text_sha256`), the resolved LEF list and the resolved Liberty
  list. Both lists include the config's own ``lib-paths`` and
  ``lef-paths`` on top of the platform's. ``abc-args`` is omitted.
* Power analysis: tool name, netlist source, the upstream identity,
  the technology, mode, activity source and register level. The upstream
  identity is the netlist sha256 for ``netlist-source: synth`` and the
  ODB's project-relative path for ``pnr``
  (:meth:`~rtl_buddy.tools.power_openroad.OpenRoadPower._upstream_identity`).
  The technology is the Liberty and LEF files the generated Tcl names, in
  script order, fingerprinted with
  :func:`~rtl_buddy.tools.synth_yosys.library_fingerprint`
  (:meth:`~rtl_buddy.tools.power_openroad.OpenRoadPower._phys_technology`).
  ``tool_overrides`` is not included because no power backend reads it.

**When hashes are taken.** The constraints digest and the trace digest
are captured by the producer before it launches its tool and confirmed
by :func:`~rtl_buddy.phys.publish.confirm_digest` when the tool returns.
A file that changed during the run records ``null`` and earns a warning.

Nothing here gates the merge. The netlist hash in
:func:`rtl_buddy.phys.model.may_inherit_other_half` decides whether two
halves describe one design; these blocks only tell runs apart in a
listing.

**Derived on read, not stored.** :func:`activity_label` and
:func:`config_summary` phrase a block for a table or dropdown,
:func:`trace_test` names the test whose artefact directory a trace came
from, and :func:`experiment_for` names the ``rb xplr`` experiment a
manifest sits under. Each answers ``None`` when the evidence is missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from ..logging_utils import log_event
from ..tools.artifact_paths import ARTIFACT_DIRNAME, XPLR_WORKTREE_SIDECAR_NAME
from ..xplr.ledger import (
    LEDGER_DIRNAME as XPLR_DIRNAME,
    RECORD_FILENAME as XPLR_RECORD_FILENAME,
    RESERVED_DIRNAMES as XPLR_RESERVED_DIRNAMES,
)

logger = logging.getLogger(__name__)

#: Every key of the config block; ``null`` means "not recorded".
CONFIG_KEYS = (
    "platform",
    "effort",
    "constraints",
    "constraints_sha256",
    "options_sha256",
)

#: Every key of the activity block. ``source`` is the one to read first;
#: the others are detail for the source that uses them.
ACTIVITY_KEYS = (
    "source",
    "trace",
    "trace_sha256",
    "test",
    "scope",
    "toggle_rate",
    "duty",
)

#: Hex characters kept from the options sha256.
OPTIONS_DIGEST_CHARS = 12

#: Joins the parts of a rendered label, on every surface.
LABEL_SEPARATOR = " · "

#: The activity sources that read a trace file.
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

    ``options`` is the backend's resolved option set and is stored as a
    digest (:func:`options_digest`). ``constraints`` is stored as passed;
    the publish path makes it project-relative. ``producer`` names the
    caller for the log line when ``options`` cannot be digested.
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

    ``test`` is derived from ``trace`` by :func:`trace_test`. Each detail
    is recorded only for the source that used it: ``trace``,
    ``trace_sha256``, ``test`` and ``scope`` for a trace source
    (:data:`TRACE_SOURCES`), ``toggle_rate`` and ``duty`` for
    ``synthetic``. A config keeps fields for sources it is not using, and
    recording them would report a stimulus nothing applied.

    ``trace_sha256`` identifies the trace by content, because a trace is
    rewritten in place when its test reruns. The producer captures it
    before the tool runs (:func:`~rtl_buddy.phys.publish.confirm_digest`).
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

    The digest is over canonical JSON (sorted keys, no whitespace), so it
    is stable across machines. It is ``None`` for empty ``options`` and
    for a mapping JSON cannot render, which is logged at DEBUG naming
    ``producer`` and the offending keys. ``None`` is used instead of a
    ``repr`` fallback because ``repr`` can embed object addresses and
    would give the same run a different digest each time.
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


def text_sha256(text: str | None) -> str | None:
    """The sha256 of inline script text, such as an effort's ``pre-sta-tcl``.

    The counterpart of :func:`rtl_buddy.phys.publish.sha256_of` for
    content with no file. ``None`` for empty text, so a run with no
    fragment and one with an empty fragment are the same run.
    """
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unserialisable_keys(options) -> list[str]:
    """The sorted keys of ``options`` whose values JSON cannot render.

    Empty when the failure is in the mapping as a whole, such as a
    non-string key or a cycle across several entries.
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

    ``None`` when nothing was recorded. An empty but present block comes
    back as a block of nulls, so "no config recorded" and "config with
    nothing in it" stay distinguishable.
    """
    if not isinstance(recorded, dict):
        return None
    block = {key: recorded.get(key) for key in CONFIG_KEYS}
    block["summary"] = config_summary(block)
    return block


def normalise_activity(recorded) -> dict | None:
    """A stored activity block with every key present, plus its ``label``.

    ``None`` when nothing was recorded; see :func:`normalise_config`.
    """
    if not isinstance(recorded, dict):
        return None
    block = {key: recorded.get(key) for key in ACTIVITY_KEYS}
    block["label"] = activity_label(block)
    return block


def activity_label(activity) -> str | None:
    """One line naming what drove a power run's switching.

    ``defaults`` for a static run, the toggle and duty for a synthetic
    one, and the trace's test (or its filename) for a trace source.
    ``None`` when no source is recorded.
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

    Platform, effort, an 8-character prefix of the constraints hash and
    the options digest, joined by :data:`LABEL_SEPARATOR` with absent
    parts left out. ``None`` when nothing was recorded.
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

    Derived from the layout: a trace at
    ``<suite>/artefacts/<test>/dump.saif`` came from ``<test>``. A trace
    anywhere else answers ``None``.
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

    The id comes from the path ``artefacts/xplr/<exp-id>/...``. The
    experiment's ``hypothesis`` from its ``record.json`` is read as the
    label, when readable. A manifest inside a materialized checkout under
    the reserved worktree directory is resolved by
    :func:`_experiment_in_worktree`.
    """
    parts = Path(os.path.abspath(str(manifest_path))).parts
    for index in range(len(parts) - 2):
        if parts[index] != ARTIFACT_DIRNAME or parts[index + 1] != XPLR_DIRNAME:
            continue
        exp_id = parts[index + 2]
        if exp_id in XPLR_RESERVED_DIRNAMES:
            return _experiment_in_worktree(parts, index)
        root = Path(*parts[: index + 3])
        return {"id": exp_id, "label": _hypothesis(root / XPLR_RECORD_FILENAME)}
    return None


def _experiment_in_worktree(parts, index: int) -> dict | None:
    """The experiment whose materialized checkout a manifest sits in.

    A checkout lives at ``artefacts/xplr/worktrees/<exp-id>/`` and holds a
    whole project tree, so the manifest must be further down. The id is
    accepted only when the ledger has an entry of that name and the
    worktree sidecar, if present, does not name a different path
    (:func:`_sidecar_names`). Otherwise ``None``.
    """
    if len(parts) <= index + 4:
        return None
    exp_id = parts[index + 3]
    if exp_id in XPLR_RESERVED_DIRNAMES:
        return None
    experiment_dir = Path(*parts[: index + 2]) / exp_id
    if not experiment_dir.is_dir():
        return None
    if not _sidecar_names(experiment_dir, Path(*parts[: index + 4])):
        return None
    return {"id": exp_id, "label": _hypothesis(experiment_dir / XPLR_RECORD_FILENAME)}


def _sidecar_names(experiment_dir: Path, worktree: Path) -> bool:
    """Does this experiment's worktree sidecar agree with ``worktree``?

    ``True`` when the sidecar is missing, malformed or records no path,
    since ``rb xplr release`` deletes it and only a conflicting path
    refutes.
    """
    try:
        with open(experiment_dir / XPLR_WORKTREE_SIDECAR_NAME, encoding="utf-8") as fh:
            sidecar = json.load(fh)
    except (OSError, ValueError):
        return True
    if not isinstance(sidecar, dict):
        return True
    recorded = sidecar.get("path")
    if not isinstance(recorded, str) or not recorded:
        return True
    return os.path.abspath(recorded) == os.path.abspath(str(worktree))


def _hypothesis(record_path) -> str | None:
    """The ``hypothesis`` of an experiment record, or ``None``.

    Read as plain JSON, not through the ``rb xplr`` schema, so an invalid
    record still leaves the run listed.
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
    """A toggle rate or duty for a label, without a trailing ``.0``."""
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
    "text_sha256",
    "trace_test",
]
