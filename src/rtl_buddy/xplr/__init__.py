"""``rb xplr``: a tool-agnostic experiment ledger.

The agent drives the knobs and declares what it changed; rb xplr records each experiment (git-pinned source, knob manifest, outcome) and curates the Pareto frontier.
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
