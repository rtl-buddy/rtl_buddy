"""Experiment-record contract for ``rb xplr``: the vendored JSON Schema, strict validation, typed dataclasses and canonical (de)serialization.

Knob values and ``config_snapshot`` are untyped. :data:`ABSENT` marks a missing key, as distinct from an explicit ``null``.
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
    """Sentinel type for a key absent from the JSON document (``None`` is an explicit ``null``)."""

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


# The stock "date-time" checker needs the optional rfc3339-validator package; use fromisoformat instead.
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
    """Validate ``record`` against the schema.

    Raises :class:`FatalRtlBuddyError` naming the JSON pointer of the first violation.
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
    """The ``source`` block: the git-pinned design state."""

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
    """One agent-declared knob change."""

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
    """Direction and unit of one outcome metric."""

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
    """The ``outcome`` block: status, metrics and artifacts."""

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
    """One ``provenance.tools`` entry."""

    name: str
    version: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolVersion":
        return cls(name=data["name"], version=data["version"])

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}


@dataclass
class Provenance:
    """The ``provenance`` block: who and what produced the record, and when."""

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
    """Typed view of one experiment record.

    ``to_dict`` emits keys in schema order, so load and dump round-trip byte-identically.
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
        """Validate ``data`` and build the record; raises :class:`FatalRtlBuddyError` on a schema violation."""

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
        """Return the JSON-compatible dict in schema key order."""

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
    """Set ``out[key]`` unless ``value`` is ABSENT."""

    if not isinstance(value, _Absent):
        out[key] = value


# ---------------------------------------------------------------------------
# canonical serialization
# ---------------------------------------------------------------------------


def dumps_record(record: ExperimentRecord) -> str:
    """Serialize a record as indented JSON with a trailing newline, keys in schema order."""

    return json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n"


def loads_record(text: str | bytes) -> ExperimentRecord:
    """Parse and validate a JSON document; raises :class:`FatalRtlBuddyError` on malformed JSON or a schema violation."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FatalRtlBuddyError(
            f"experiment record is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    return ExperimentRecord.from_dict(data)
