"""``rb xplr`` — LLM-native, tool-agnostic experiment ledger.

rb xplr is a bookkeeper, not an optimizer: the agent drives the knobs
and declares what it changed; rb xplr records, pins source revision,
captures outcomes, and curates the Pareto frontier. Experiment unit::

    (git-pinned source + agent-declared knob manifest) -> outcome

Modules:

* :mod:`rtl_buddy.xplr.schema` — the experiment-record contract
  (vendored JSON Schema 1.0 + typed dataclasses).
* :mod:`rtl_buddy.xplr.ledger` — the on-disk ledger layout
  (``artefacts/xplr/<exp-id>/record.json``).
* :mod:`rtl_buddy.xplr.commands` — the ``rb xplr`` command layer
  (register / attach-outcome / list / show).
* :mod:`rtl_buddy.xplr.analysis` — pure analysis views over the ledger
  (Pareto frontier / pairwise diff / per-knob effect history).
* :mod:`rtl_buddy.xplr.gitprov` — git-pinned source provenance: the
  register commit policy, worktree isolation (materialize/release),
  and the frontier-aware gc.
* :mod:`rtl_buddy.xplr.mockflow` — synthetic DSE backend with known
  optima (the ``rb xplr mock`` dev/CI harness).
"""

from .schema import (
    ABSENT,
    SCHEMA_VERSION,
    ExperimentRecord,
    Knob,
    MetricMeta,
    Outcome,
    Provenance,
    SourceRef,
    ToolVersion,
    dumps_record,
    loads_record,
    schema,
    validate_record,
)


__all__ = [
    "ABSENT",
    "SCHEMA_VERSION",
    "ExperimentRecord",
    "Knob",
    "MetricMeta",
    "Outcome",
    "Provenance",
    "SourceRef",
    "ToolVersion",
    "dumps_record",
    "loads_record",
    "schema",
    "validate_record",
]
