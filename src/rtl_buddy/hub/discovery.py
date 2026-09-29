"""Per-project hub discovery through ``<project_root>/.rtl-buddy/hub.json``.

The hub writes the file at startup and deletes it on clean exit only if the PID still matches. Clients walk up from the current directory to find it; ``$RTL_BUDDY_HUB`` overrides the lookup. A second hub cannot start while the recorded PID is live.
"""

from __future__ import annotations

import json
import os
import signal as signal_module
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


HUB_DIR_NAME = ".rtl-buddy"
HUB_DISCOVERY_FILENAME = "hub.json"
HUB_DISCOVERY_SCHEMA_VERSION = 1

ENV_OVERRIDE = "RTL_BUDDY_HUB"


class HubDiscoveryError(Exception):
    """``hub.json`` is unreadable or malformed."""


class HubAlreadyRunningError(HubDiscoveryError):
    """Another hub is already live for the project."""

    def __init__(self, pid: int, path: Path) -> None:
        super().__init__(
            f"hub already running for this project (pid {pid}); "
            f"see {path}. Use `rb hub stop` or kill the process first."
        )
        self.pid = pid
        self.path = path


@dataclass(frozen=True, slots=True)
class HubRecord:
    """The contents of ``hub.json``.

    ``http_port`` is set only when the hub serves the viewer. ``active_model`` is the model served by ``GET /view.json`` without a query; it is ``None`` until a model loads.
    """

    v: int
    pid: int
    tcp: str
    server_version: str
    project_root: str
    started_at: str
    http_port: int | None = None
    active_model: str | None = None

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        # Absent optional fields are omitted from the file.
        if self.http_port is None:
            out.pop("http_port", None)
        if self.active_model is None:
            out.pop("active_model", None)
        return out


def hub_dir(project_root: Path) -> Path:
    """Return ``<project_root>/.rtl-buddy/`` without creating it."""

    return project_root / HUB_DIR_NAME


def discovery_path(project_root: Path) -> Path:
    """Return the per-project ``hub.json`` path."""

    return hub_dir(project_root) / HUB_DISCOVERY_FILENAME


def ensure_hub_dir(project_root: Path) -> Path:
    """Create the ``.rtl-buddy/`` directory if missing; return its path."""

    target = hub_dir(project_root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_record(
    project_root: Path,
    *,
    pid: int,
    tcp: str,
    server_version: str,
    http_port: int | None = None,
    active_model: str | None = None,
) -> HubRecord:
    """Write ``hub.json`` atomically.

    Raises :class:`HubAlreadyRunningError` if a live record already exists for the project.
    """

    ensure_hub_dir(project_root)
    target = discovery_path(project_root)

    existing = _read_record_if_present(target)
    if existing is not None and _pid_is_live(existing.pid):
        raise HubAlreadyRunningError(existing.pid, target)

    record = HubRecord(
        v=HUB_DISCOVERY_SCHEMA_VERSION,
        pid=pid,
        tcp=tcp,
        server_version=server_version,
        project_root=str(project_root.resolve()),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        http_port=http_port,
        active_model=active_model,
    )

    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return record


def update_active_model(project_root: Path, active_model: str | None) -> bool:
    """Set ``active_model`` in ``hub.json``, keeping the other fields.

    Returns ``False`` when there is no discovery file. Does not check liveness.
    """

    target = discovery_path(project_root)
    current = _read_record_if_present(target)
    if current is None:
        return False
    updated = HubRecord(
        v=current.v,
        pid=current.pid,
        tcp=current.tcp,
        server_version=current.server_version,
        project_root=current.project_root,
        started_at=current.started_at,
        http_port=current.http_port,
        active_model=active_model,
    )
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(updated.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return True


def read_record(project_root: Path) -> HubRecord | None:
    """Return the on-disk ``HubRecord`` for ``project_root`` or ``None``."""

    return _read_record_if_present(discovery_path(project_root))


def delete_record_if_owner(project_root: Path, *, expected_pid: int) -> bool:
    """Remove ``hub.json`` only if its ``pid`` is ``expected_pid``. Returns whether it was deleted."""

    target = discovery_path(project_root)
    current = _read_record_if_present(target)
    if current is None or current.pid != expected_pid:
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def env_override() -> str | None:
    """Return the ``$RTL_BUDDY_HUB`` ``host:port`` string, or ``None`` when unset."""

    return os.environ.get(ENV_OVERRIDE) or None


def find_project_root_with_hub(start: Path) -> Path | None:
    """Walk up from ``start`` and return the first directory containing ``.rtl-buddy/hub.json``, or ``None``."""

    candidate = start.resolve()
    while True:
        if (candidate / HUB_DIR_NAME / HUB_DISCOVERY_FILENAME).is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _read_record_if_present(path: Path) -> HubRecord | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubDiscoveryError(f"{path}: invalid hub.json — {exc}") from exc

    if not isinstance(raw, dict):
        raise HubDiscoveryError(f"{path}: hub.json must be a JSON object")
    try:
        http_port_raw = raw.get("http_port")
        http_port: int | None = None
        if http_port_raw is not None:
            http_port = int(http_port_raw)
        active_model_raw = raw.get("active_model")
        active_model: str | None = (
            str(active_model_raw) if active_model_raw is not None else None
        )
        return HubRecord(
            v=int(raw["v"]),
            pid=int(raw["pid"]),
            tcp=str(raw["tcp"]),
            server_version=str(raw["server_version"]),
            project_root=str(raw["project_root"]),
            started_at=str(raw["started_at"]),
            http_port=http_port,
            active_model=active_model,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HubDiscoveryError(
            f"{path}: hub.json missing or malformed field — {exc}"
        ) from exc


def _pid_is_live(pid: int) -> bool:
    """Return whether ``pid`` is a live process, including one owned by another user (POSIX only)."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user's process — still live.
        return True
    return True


def signal_process(pid: int, *, sig: int = signal_module.SIGTERM) -> None:
    """Send ``sig`` to ``pid``."""

    os.kill(pid, sig)


__all__ = [
    "HUB_DIR_NAME",
    "HUB_DISCOVERY_FILENAME",
    "HUB_DISCOVERY_SCHEMA_VERSION",
    "ENV_OVERRIDE",
    "HubDiscoveryError",
    "HubAlreadyRunningError",
    "HubRecord",
    "hub_dir",
    "discovery_path",
    "ensure_hub_dir",
    "write_record",
    "update_active_model",
    "read_record",
    "delete_record_if_owner",
    "env_override",
    "find_project_root_with_hub",
    "signal_process",
]
