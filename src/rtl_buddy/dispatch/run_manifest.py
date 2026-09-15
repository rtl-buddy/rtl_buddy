# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Per-run job manifest: what one head submitted, and whether it collected it (#521).

A head that is killed — Ctrl-C, a dropped SSH session, a login node
reboot — takes every job id it held with it. The fleet does not die with
it: the scheduler keeps running the simulations, writing their envelopes,
and the next invocation of the same suite has no way to know they are
there, so it submits a second fleet beside the first.

The plan manifest beside this one (:mod:`.plan`) says what a run *meant*
to do; the gates manifest (:mod:`.gates`) says which job holds which plan
index, for the build job alone and only until it exits. Neither is a
record of the run as a whole. This one is: it names every job the head
submitted, the specs needed to rebuild their handles, and a ``status``
that says whether anybody ever collected them.

That makes the manifest — not the scheduler's job names — the identity of
an interrupted run. Job names are not unique per run (two invocations of
one suite submit the same names, which is exactly what the shared-build
dedup relies on), so adopting by name could collect somebody else's
fleet. A manifest is written by one head, carries that head's pid and its
per-invocation ``run_token``, and the envelopes its jobs write carry the
same token — so a run adopted from here collects its own results by
identity, the same check a live head makes (#362).

Nothing in this module raises on a manifest it cannot read. A run left by
an older rtl_buddy, half-written by a head that was killed mid-``write``,
or owned by another user is a manifest to skip, not a reason to fail a
regression that has not submitted anything yet.
"""

import dataclasses
import json
import os
from enum import Enum
from pathlib import Path

from ..config.dispatch import JobResources
from ..seed_mode import SeedMode
from .base import BuildJobSpec, JobHandle, TestJobSpec

RUN_SCHEMA_VERSION = 1

# The run's lifecycle, as the head records it.
#
# ``running`` is written the moment the fan-out is out and rewritten at
# exactly two ends: ``collected`` when a head has read the fleet's
# envelopes, ``cancelled`` when a head took the fleet down on its way out.
# A manifest still saying ``running`` with nothing of it left in the queue
# is what a dead head leaves behind, and the discovery below retires it as
# ``stale`` so the next run does not probe the scheduler for it again.
STATUS_RUNNING = "running"
STATUS_COLLECTED = "collected"
STATUS_CANCELLED = "cancelled"
STATUS_STALE = "stale"

# Spec fields that are ``Path`` on the dataclass and ``str`` in JSON.
_PATH_FIELDS = frozenset(
    {"result_json", "log_path", "plan_path", "build_result_json", "gates_json"}
)
_SPEC_TYPES = {"build": BuildJobSpec, "test": TestJobSpec}
_SPEC_KINDS = {BuildJobSpec: "build", TestJobSpec: "test"}


def run_manifest_path(dispatch_root, pid=None) -> Path:
    """This head's manifest path in ``dispatch_root``.

    Keyed on the head pid like every other per-invocation file in the
    directory (``plan-``, ``build-result-``, ``gates-``), so two heads
    running one suite at once never overwrite each other's record. The OS
    reuses pids, so the file is never trusted on its path alone — the
    ``run_token`` inside it is the identity.
    """
    return Path(dispatch_root) / f"run-{os.getpid() if pid is None else pid}.json"


def _encode(value):
    """One spec field as JSON: paths stringify, enums flatten, nested
    dataclasses (``JobResources``) become objects."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    return value


def encode_spec(spec) -> dict:
    """One job spec as a JSON object, tagged with which spec it is."""
    kind = _SPEC_KINDS.get(type(spec))
    if kind is None:
        raise TypeError(f"cannot record job spec of type {type(spec).__name__}")
    payload = {"kind": kind}
    for field in dataclasses.fields(spec):
        payload[field.name] = _encode(getattr(spec, field.name))
    return payload


def decode_spec(payload):
    """Rebuild the spec :func:`encode_spec` wrote; raise on anything else.

    Unknown keys are dropped and absent ones left at their dataclass
    default, so a manifest written by a neighbouring rtl_buddy that has
    one field more or fewer still rebuilds a usable spec rather than
    failing the adoption outright. A missing *required* field does raise,
    and the caller turns that into a skipped manifest.
    """
    if not isinstance(payload, dict):
        raise ValueError("job spec is not a JSON object")
    cls = _SPEC_TYPES.get(payload.get("kind"))
    if cls is None:
        raise ValueError(f"unknown job spec kind {payload.get('kind')!r}")
    kwargs = {}
    for field in dataclasses.fields(cls):
        if field.name not in payload:
            continue
        value = payload[field.name]
        if value is not None:
            if field.name in _PATH_FIELDS:
                value = Path(value)
            elif field.name == "resources":
                known = {f.name for f in dataclasses.fields(JobResources)}
                value = JobResources(
                    **{k: v for k, v in dict(value).items() if k in known}
                )
            elif field.name == "seed_mode":
                value = SeedMode(value)
        kwargs[field.name] = value
    return cls(**kwargs)


def json_safe_rows(rows) -> list[dict]:
    """The head's result rows, reduced to what JSON can hold.

    ``results`` is dropped on purpose rather than serialised. At submit
    time a runnable row's entry is a ``None`` placeholder the collector
    fills in, and a skipped or setup-failed row's entry is a live
    ``TestResults`` object that this file has no business reconstructing —
    a head that adopts this run re-derives those rows from its own
    expansion of the same suite. What stays is the row identity and the
    submit-time reservation metadata, which is what reservation advice
    needs and what an adopting head cannot recompute.
    """
    safe = []
    for row in rows:
        kept = {}
        for key, value in row.items():
            if key == "results":
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue
            kept[key] = value
        safe.append(kept)
    return safe


def _atomic_write(path: Path, payload: dict) -> Path:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic: a scanning head never reads a partial manifest
    return path


def write_run_manifest(
    path,
    *,
    run_token,
    backend,
    started_at,
    submitted_at,
    suite_config,
    plan,
    build,
    pending,
    rows,
    status=STATUS_RUNNING,
) -> Path:
    """Record one suite's submitted fleet; return ``path``.

    ``build`` is the suite's build :class:`JobHandle` or ``None``;
    ``pending`` is the head's ``[(row index, JobHandle)]``. Written after
    the whole suite is out, so a manifest never names half an array —
    adopting half a fleet would wait for jobs it could collect and submit
    nothing for the ones it had never heard of.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_token": run_token,
        "pid": os.getpid(),
        "backend": backend,
        "started_at": started_at,
        "submitted_at": submitted_at,
        "suite_config": str(suite_config),
        "plan": str(plan),
        "build": _handle_entry(build) if build is not None else None,
        "pending": [
            dict(_handle_entry(handle), row=int(row)) for row, handle in pending
        ],
        "rows": json_safe_rows(rows),
        "status": status,
    }
    return _atomic_write(path, payload)


def _handle_entry(handle: JobHandle) -> dict:
    return {
        "job_id": handle.job_id,
        "cluster": getattr(handle, "cluster", None),
        "spec": encode_spec(handle.spec),
    }


def load_run_manifest(path) -> tuple[dict | None, str | None]:
    """``(payload, why not)`` — read one manifest without ever raising."""
    try:
        payload = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None, "not written"
    except (OSError, ValueError) as e:
        return None, str(e)[:200]
    if not isinstance(payload, dict):
        return None, "run manifest is not a JSON object"
    version = payload.get("schema_version")
    if version != RUN_SCHEMA_VERSION:
        return None, (
            f"run manifest has schema_version {version!r}, expected "
            f"{RUN_SCHEMA_VERSION}"
        )
    if not isinstance(payload.get("pending"), list):
        return None, "run manifest has no `pending` list"
    return payload, None


def set_run_status(path, status) -> str | None:
    """Rewrite one manifest's ``status``; ``None`` on success, else why not.

    Read-modify-write rather than a blind overwrite, because the fields
    beside ``status`` are the only record of what was submitted and a
    truncated manifest would strand the fleet it names. Best effort: a
    status that cannot be written costs the next run one wasted
    ``squeue``, and nothing else.
    """
    payload, reason = load_run_manifest(path)
    if payload is None:
        return reason
    payload["status"] = status
    try:
        _atomic_write(Path(path), payload)
    except OSError as e:
        return str(e)[:200]
    return None


def update_pending_job_ids(path, pending) -> str | None:
    """Re-point the manifest's ``pending`` at a retry round's job ids.

    A retried job is a fresh submission with a fresh id, and the row it
    belongs to is the identity that survives. Rows the round did not
    touch keep the ids they had, so the manifest always names the fleet
    that is actually outstanding.
    """
    payload, reason = load_run_manifest(path)
    if payload is None:
        return reason
    replacement = {int(row): handle for row, handle in pending}
    for entry in payload["pending"]:
        handle = replacement.get(entry.get("row"))
        if handle is None:
            continue
        entry.update(_handle_entry(handle))
    try:
        _atomic_write(Path(path), payload)
    except OSError as e:
        return str(e)[:200]
    return None


def discover_run_manifests(dispatch_root, *, pid=None) -> list[tuple[Path, dict]]:
    """Manifests in ``dispatch_root`` that claim to be running elsewhere.

    This head's own manifest is excluded by pid: it writes one in this
    very directory, and a regression that submits several suites would
    otherwise rediscover the suite it just submitted. A manifest already
    marked ``collected``, ``cancelled`` or ``stale`` is settled and needs
    no scheduler probe.

    Sorted by path so a run with several orphans reports them in a stable
    order — the message names them, and a set that reshuffles per
    invocation is a message nobody can diff.
    """
    root = Path(dispatch_root)
    mine = os.getpid() if pid is None else pid
    found = []
    try:
        candidates = sorted(root.glob("run-*.json"))
    except OSError:
        return []
    for candidate in candidates:
        payload, _reason = load_run_manifest(candidate)
        if payload is None:
            continue
        if payload.get("status") != STATUS_RUNNING:
            continue
        if payload.get("pid") == mine:
            continue
        found.append((candidate, payload))
    return found


def handles_from(payload) -> list[JobHandle]:
    """Every job the manifest names, build job first; raises on a bad spec."""
    handles = []
    build = payload.get("build")
    if isinstance(build, dict):
        handles.append(
            JobHandle(
                job_id=str(build["job_id"]),
                spec=decode_spec(build.get("spec")),
                cluster=build.get("cluster"),
            )
        )
    for entry in payload.get("pending") or []:
        handles.append(
            JobHandle(
                job_id=str(entry["job_id"]),
                spec=decode_spec(entry.get("spec")),
                cluster=entry.get("cluster"),
            )
        )
    return handles


def build_from(payload) -> JobHandle | None:
    """The manifest's build-job handle, or ``None`` where it had none."""
    build = payload.get("build")
    if not isinstance(build, dict):
        return None
    return JobHandle(
        job_id=str(build["job_id"]),
        spec=decode_spec(build.get("spec")),
        cluster=build.get("cluster"),
    )


def pending_from(payload) -> list[tuple[int, JobHandle]]:
    """The manifest's ``[(row index, JobHandle)]``, as the head held it."""
    pending = []
    for entry in payload.get("pending") or []:
        pending.append(
            (
                int(entry["row"]),
                JobHandle(
                    job_id=str(entry["job_id"]),
                    spec=decode_spec(entry.get("spec")),
                    cluster=entry.get("cluster"),
                ),
            )
        )
    return pending


def row_identities(payload) -> list[tuple]:
    """``(test name, run id)`` per recorded row, in the head's order.

    The comparison an adoption stands on: the same suite planned the same
    way produces the same list, including the skipped and setup-failed
    rows, so a different ``-l``/``-s`` selection or an edited tests.yaml
    is a mismatch rather than a fleet collected against the wrong rows.
    """
    return [
        (row.get("test_name"), row.get("randmode_i"))
        for row in payload.get("rows") or []
    ]
