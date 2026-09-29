# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Per-run job manifest: every job one head submitted, and whether it was collected.

If a head dies, its fleet keeps running. The manifest, identified by the
head's ``run_token`` (also stamped into the jobs' envelopes), lets a later
invocation find, adopt or cancel that fleet. Nothing here raises on an
unreadable manifest; it is skipped. See :mod:`.plan` and :mod:`.gates` for the
other per-run files.
"""

import dataclasses
import json
import os
from enum import Enum
from pathlib import Path

from ..config.dispatch import JobResources
from ..seed_mode import SeedMode
from .base import BUILD_PHASE_VERILATE, BuildJobSpec, JobHandle, TestJobSpec
from .plan import run_scoped_path

RUN_SCHEMA_VERSION = 1

# Run lifecycle. ``submitting``: written before the first submission and held
# until the fan-out is out, so a head killed midway leaves a record; the ids in
# it are live but may be incomplete. ``warn`` and ``cancel`` treat it like
# ``running``; ``adopt`` refuses it. ``running``: fan-out complete. ``collected``
# and ``cancelled``: a head finished with the fleet. ``stale``: a ``running``
# manifest with nothing left in the queue, retired by discovery.
STATUS_SUBMITTING = "submitting"
STATUS_RUNNING = "running"
STATUS_COLLECTED = "collected"
STATUS_CANCELLED = "cancelled"
STATUS_STALE = "stale"

# Statuses probed for a live fleet.
ACTIVE_STATUSES = (STATUS_SUBMITTING, STATUS_RUNNING)

# Spec fields that are ``Path`` on the dataclass and ``str`` in JSON.
_PATH_FIELDS = frozenset(
    {"result_json", "log_path", "plan_path", "build_result_json", "gates_json"}
)
# `verilate` and `build` are both BuildJobSpec; the spec's `phase` tells them apart.
_SPEC_TYPES = {
    "build": BuildJobSpec,
    "verilate": BuildJobSpec,
    "test": TestJobSpec,
}
_SPEC_KINDS = {BuildJobSpec: "build", TestJobSpec: "test"}


def run_manifest_path(dispatch_root, run_token) -> Path:
    """This head's manifest path in ``dispatch_root``, named like :func:`run_scoped_path` files."""
    return run_scoped_path(dispatch_root, "run", run_token)


def _encode(value):
    """One spec field as JSON: paths become strings, enums values, dataclasses objects."""
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
    if kind == "build" and getattr(spec, "phase", None) == BUILD_PHASE_VERILATE:
        kind = "verilate"
    payload = {"kind": kind}
    for field in dataclasses.fields(spec):
        payload[field.name] = _encode(getattr(spec, field.name))
    return payload


def decode_spec(payload):
    """Rebuild the spec :func:`encode_spec` wrote; raise on anything else.

    Unknown keys are dropped and absent ones take the dataclass default. A
    missing required field raises.
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

    ``results`` is dropped; an adopting head re-derives it. Row identity and
    submit-time reservation metadata are kept.
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
    tmp.replace(path)  # atomic: scanning heads never read a partial file
    return path


def write_run_manifest(
    path,
    *,
    run_token,
    backend,
    started_at,
    submitted_at=None,
    suite_config,
    plan,
    build=None,
    verilate=None,
    pending=(),
    rows,
    status=STATUS_SUBMITTING,
) -> Path:
    """Create one suite's run record; return ``path``.

    Written before the first submission with no handles and ``status:
    "submitting"``, then grown by :func:`record_build_handle` and
    :func:`record_pending_handles`; :func:`finish_submission` closes it.
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
        "verilate": _handle_entry(verilate) if verilate is not None else None,
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


def _amend(path, mutate) -> str | None:
    """Read-modify-write one manifest; ``None`` on success, else why not.

    Best effort: a submission stays valid if its record cannot be grown.
    """
    payload, reason = load_run_manifest(path)
    if payload is None:
        return reason
    mutate(payload)
    try:
        _atomic_write(Path(path), payload)
    except OSError as e:
        return str(e)[:200]
    return None


def record_build_handle(path, handle) -> str | None:
    """Name the build job in the manifest, as soon as it is accepted."""

    def mutate(payload):
        payload["build"] = None if handle is None else _handle_entry(handle)

    return _amend(path, mutate)


def record_verilate_handle(path, handle) -> str | None:
    """Name the verilate job in the manifest, as soon as it is accepted."""

    def mutate(payload):
        payload["verilate"] = None if handle is None else _handle_entry(handle)

    return _amend(path, mutate)


def record_pending_handles(path, pending) -> str | None:
    """Append one accepted group's ``[(row index, JobHandle)]``."""

    def mutate(payload):
        payload["pending"].extend(
            dict(_handle_entry(handle), row=int(row)) for row, handle in pending
        )

    return _amend(path, mutate)


def finish_submission(path, submitted_at) -> str | None:
    """Flip a complete fan-out from ``submitting`` to ``running``."""

    def mutate(payload):
        payload["status"] = STATUS_RUNNING
        payload["submitted_at"] = submitted_at

    return _amend(path, mutate)


def load_run_manifest(path) -> tuple[dict | None, str | None]:
    """Read one manifest as ``(payload, reason)`` without raising."""
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
    """Rewrite one manifest's ``status``; ``None`` on success, else why not."""

    def mutate(payload):
        payload["status"] = status

    return _amend(path, mutate)


def update_pending_job_ids(path, pending) -> str | None:
    """Re-point ``pending`` at a retry round's new job ids, matched by row.

    Rows the round did not touch keep their ids.
    """
    replacement = {int(row): handle for row, handle in pending}

    def mutate(payload):
        for entry in payload["pending"]:
            handle = replacement.get(entry.get("row"))
            if handle is not None:
                entry.update(_handle_entry(handle))

    return _amend(path, mutate)


def _manifest_dirs(root: Path):
    """``root`` and the namespaced directories one level beneath it.

    A regression whose suites share a directory writes to ``.dispatch/<namespace>/``,
    while a plain ``rb test`` writes to ``.dispatch/`` itself (see
    ``_dispatch_regression_namespaces``).
    """
    yield root
    try:
        children = sorted(child for child in root.iterdir() if child.is_dir())
    except OSError:
        return
    yield from children


def discover_run_manifests(
    dispatch_root, *, run_token, suite_config=None
) -> list[tuple[Path, dict]]:
    """Manifests under ``dispatch_root`` whose status is ``submitting`` or ``running``.

    This head's own manifests are excluded by ``run_token``, not pid, since pids
    are reused. ``suite_config`` narrows the scan to one suite. Results are
    sorted by path.
    """
    found = []
    for directory in _manifest_dirs(Path(dispatch_root)):
        try:
            candidates = sorted(directory.glob("run-*.json"))
        except OSError:
            continue
        for candidate in candidates:
            payload, _reason = load_run_manifest(candidate)
            if payload is None:
                continue
            if payload.get("status") not in ACTIVE_STATUSES:
                continue
            if run_token is not None and payload.get("run_token") == run_token:
                continue
            if suite_config is not None and payload.get("suite_config") != suite_config:
                continue
            found.append((candidate, payload))
    return found


def handles_from(payload) -> list[JobHandle]:
    """Every job the manifest names, compile jobs first; raises on a bad spec."""
    handles = []
    for key in ("verilate", "build"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            handles.append(
                JobHandle(
                    job_id=str(entry["job_id"]),
                    spec=decode_spec(entry.get("spec")),
                    cluster=entry.get("cluster"),
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
    return _handle_from(payload, "build")


def verilate_from(payload) -> JobHandle | None:
    """The manifest's verilate-job handle, or ``None`` where it had none."""
    return _handle_from(payload, "verilate")


def _handle_from(payload, key) -> JobHandle | None:
    entry = payload.get(key)
    if not isinstance(entry, dict):
        return None
    return JobHandle(
        job_id=str(entry["job_id"]),
        spec=decode_spec(entry.get("spec")),
        cluster=entry.get("cluster"),
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

    Adoption compares this list, so a different ``-l``/``-s`` selection or an
    edited tests.yaml is a mismatch.
    """
    return [
        (row.get("test_name"), row.get("randmode_i"))
        for row in payload.get("rows") or []
    ]
