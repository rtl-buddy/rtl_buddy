"""Experiment-record contract for ``rb xplr`` — schema + typed accessors.

The canonical contract is the vendored JSON Schema
``xplr-experiment-1.0.json`` (draft 2020-12), packaged next to this
module so it can also be published as-is. An experiment record is the
tool-agnostic unit of design-space exploration::

    (git-pinned source + agent-declared knob manifest) -> outcome

This module exposes:

* :func:`schema` — a deep copy of the vendored JSON Schema.
* :func:`validate_record` — strict validation (``additionalProperties:
  false`` everywhere, ``format: date-time`` actually checked) raising
  :class:`~rtl_buddy.errors.FatalRtlBuddyError` with a JSON pointer.
* :class:`ExperimentRecord` and its nested dataclasses — a typed view
  with ``from_dict`` / ``to_dict`` that round-trips byte-identically
  for valid records.
* :func:`dumps_record` / :func:`loads_record` — the single canonical
  serialization convention (``json.dumps(..., indent=2)``, trailing
  newline, keys in schema declaration order).

Design notes:

* Knob ``from`` / ``to`` values and ``config_snapshot`` are stored
  untyped — rb xplr is a bookkeeper, not a knob taxonomy owner.
* JSON distinguishes an absent ``parent`` from an explicit ``null``;
  the :data:`ABSENT` sentinel preserves that distinction so the
  round-trip stays byte-identical either way.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from ..errors import FatalRtlBuddyError


SCHEMA_VERSION = "1.0"
SCHEMA_RESOURCE = f"xplr-experiment-{SCHEMA_VERSION}.json"


class _Absent:
    """Sentinel type for "key not present in the JSON document".

    Distinct from ``None``, which maps to an explicit JSON ``null``
    (the schema allows ``"parent": null``).
    """

    _instance: "_Absent | None" = None

    def __new__(cls) -> "_Absent":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "ABSENT"


ABSENT = _Absent()


def _load_schema() -> dict[str, Any]:
    text = (
        resources.files("rtl_buddy.xplr")
        .joinpath(SCHEMA_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def schema() -> dict[str, Any]:
    """Return a deep copy of the vendored experiment-record JSON Schema."""

    return json.loads(json.dumps(_SCHEMA))


# ``format`` is annotation-only in draft 2020-12 unless a FormatChecker is
# supplied. jsonschema's stock "date-time" checker additionally requires the
# optional rfc3339-validator package, which is not a dependency — so register
# an explicit checker backed by datetime.fromisoformat (Python 3.11+ accepts
# the full RFC 3339 profile, including a trailing "Z").
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _check_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True  # non-strings are handled by the "type" keyword
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no timezone offset (RFC 3339 requires one)")
    return True


_SCHEMA: dict[str, Any] = _load_schema()
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=_FORMAT_CHECKER)


def validate_record(record: Any) -> None:
    """Validate ``record`` against the experiment schema.

    Raises :class:`FatalRtlBuddyError` with the JSON pointer of the
    first (path-ordered) violation; returns ``None`` when valid.
    """

    if not isinstance(record, dict):
        raise FatalRtlBuddyError(
            f"experiment record must be a JSON object, got {type(record).__name__}"
        )
    errors = sorted(_VALIDATOR.iter_errors(record), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    first = errors[0]
    pointer = "/" + "/".join(str(p) for p in first.absolute_path)
    raise FatalRtlBuddyError(
        f"experiment record failed schema validation at {pointer}: {first.message}"
    )


# ---------------------------------------------------------------------------
# typed view
# ---------------------------------------------------------------------------


@dataclass
class SourceRef:
    """``source`` block — the git-pinned design state of an experiment."""

    git_sha: str
    branch: str | _Absent = ABSENT
    diff_from: str | _Absent = ABSENT
    dirty: bool | _Absent = ABSENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        return cls(
            git_sha=data["git_sha"],
            branch=data.get("branch", ABSENT),
            diff_from=data.get("diff_from", ABSENT),
            dirty=data.get("dirty", ABSENT),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"git_sha": self.git_sha}
        _put(out, "branch", self.branch)
        _put(out, "diff_from", self.diff_from)
        _put(out, "dirty", self.dirty)
        return out


@dataclass
class Knob:
    """One agent-declared knob delta. ``from``/``to`` values are untyped."""

    name: str
    from_: Any
    to: Any
    rationale: str | _Absent = ABSENT
    layer: str | _Absent = ABSENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Knob":
        return cls(
            name=data["name"],
            from_=data["from"],
            to=data["to"],
            rationale=data.get("rationale", ABSENT),
            layer=data.get("layer", ABSENT),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "from": self.from_, "to": self.to}
        _put(out, "rationale", self.rationale)
        _put(out, "layer", self.layer)
        return out


@dataclass
class MetricMeta:
    """Self-describing metadata for one outcome metric."""

    direction: str | _Absent = ABSENT
    unit: str | _Absent = ABSENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricMeta":
        return cls(
            direction=data.get("direction", ABSENT),
            unit=data.get("unit", ABSENT),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        _put(out, "direction", self.direction)
        _put(out, "unit", self.unit)
        return out


@dataclass
class Outcome:
    """``outcome`` block — flow-declared status + open metric map."""

    status: str
    metrics: dict[str, float | bool] | _Absent = ABSENT
    metric_meta: dict[str, MetricMeta] | _Absent = ABSENT
    artifacts: list[str] | _Absent = ABSENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Outcome":
        metric_meta: dict[str, MetricMeta] | _Absent = ABSENT
        if "metric_meta" in data:
            metric_meta = {
                name: MetricMeta.from_dict(meta)
                for name, meta in data["metric_meta"].items()
            }
        return cls(
            status=data["status"],
            metrics=dict(data["metrics"]) if "metrics" in data else ABSENT,
            metric_meta=metric_meta,
            artifacts=list(data["artifacts"]) if "artifacts" in data else ABSENT,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if not isinstance(self.metrics, _Absent):
            out["metrics"] = dict(self.metrics)
        if not isinstance(self.metric_meta, _Absent):
            out["metric_meta"] = {
                name: meta.to_dict() for name, meta in self.metric_meta.items()
            }
        if not isinstance(self.artifacts, _Absent):
            out["artifacts"] = list(self.artifacts)
        return out


@dataclass
class ToolVersion:
    """One ``provenance.tools`` entry — opaque tool name + version strings."""

    name: str
    version: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolVersion":
        return cls(name=data["name"], version=data["version"])

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass
class Provenance:
    """``provenance`` block — who/what/when produced the record."""

    created: str
    tools: list[ToolVersion] | _Absent = ABSENT
    reused_state: str | _Absent = ABSENT
    agent: str | _Absent = ABSENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provenance":
        tools: list[ToolVersion] | _Absent = ABSENT
        if "tools" in data:
            tools = [ToolVersion.from_dict(t) for t in data["tools"]]
        return cls(
            created=data["created"],
            tools=tools,
            reused_state=data.get("reused_state", ABSENT),
            agent=data.get("agent", ABSENT),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"created": self.created}
        if not isinstance(self.tools, _Absent):
            out["tools"] = [t.to_dict() for t in self.tools]
        _put(out, "reused_state", self.reused_state)
        _put(out, "agent", self.agent)
        return out


@dataclass
class ExperimentRecord:
    """Typed view of one validated experiment record.

    ``from_dict`` validates first, so an instance built that way is
    guaranteed schema-conformant; ``to_dict`` emits keys in the schema's
    canonical declaration order so ``loads_record``/``dumps_record``
    round-trip byte-identically.
    """

    id: str
    source: SourceRef
    knobs: list[Knob]
    outcome: Outcome
    provenance: Provenance
    schema_version: str = SCHEMA_VERSION
    parent: str | None | _Absent = ABSENT
    hypothesis: str | _Absent = ABSENT
    config_snapshot: dict[str, Any] | _Absent = field(default=ABSENT)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperimentRecord":
        """Validate ``data`` and build the typed record.

        Raises :class:`FatalRtlBuddyError` on any schema violation.
        """

        validate_record(data)
        config_snapshot: dict[str, Any] | _Absent = ABSENT
        if "config_snapshot" in data:
            config_snapshot = copy.deepcopy(data["config_snapshot"])
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            parent=data.get("parent", ABSENT),
            hypothesis=data.get("hypothesis", ABSENT),
            source=SourceRef.from_dict(data["source"]),
            knobs=[Knob.from_dict(k) for k in data["knobs"]],
            config_snapshot=config_snapshot,
            outcome=Outcome.from_dict(data["outcome"]),
            provenance=Provenance.from_dict(data["provenance"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible dict in canonical key order."""

        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
        }
        _put(out, "parent", self.parent)
        _put(out, "hypothesis", self.hypothesis)
        out["source"] = self.source.to_dict()
        out["knobs"] = [k.to_dict() for k in self.knobs]
        if not isinstance(self.config_snapshot, _Absent):
            out["config_snapshot"] = copy.deepcopy(self.config_snapshot)
        out["outcome"] = self.outcome.to_dict()
        out["provenance"] = self.provenance.to_dict()
        return out


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    """Set ``out[key] = value`` unless the value is the ABSENT sentinel."""

    if not isinstance(value, _Absent):
        out[key] = value


# ---------------------------------------------------------------------------
# canonical serialization
# ---------------------------------------------------------------------------


def dumps_record(record: ExperimentRecord) -> str:
    """Serialize a record with the canonical convention.

    ``json.dumps(..., indent=2, ensure_ascii=False)`` plus a trailing
    newline; keys in schema declaration order. Every ``record.json`` in
    the ledger and every fixture uses exactly this shape, which is what
    makes the round-trip byte-identical.
    """

    return json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n"


def loads_record(text: str | bytes) -> ExperimentRecord:
    """Parse + validate a JSON document into an :class:`ExperimentRecord`.

    Raises :class:`FatalRtlBuddyError` for malformed JSON or any schema
    violation.
    """

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FatalRtlBuddyError(
            f"experiment record is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    return ExperimentRecord.from_dict(data)
