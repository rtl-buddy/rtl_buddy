"""``<project_root>/.rtl-buddy/hub.json`` — per-project hub discovery.

Lifecycle in plain English (matches §4.1, §4.2 of the protocol spec):

1. **Startup**: the hub writes a ``HubRecord`` to disk containing its
   PID, TCP listen address, ``rtl_buddy`` version, and an ISO-8601
   start timestamp. The file is per-project (in
   ``<project_root>/.rtl-buddy/``); there is no user-global fallback.
2. **Discovery**: clients walk up from their CWD until they find the
   ``.rtl-buddy/hub.json`` and connect. The
   ``$RTL_BUDDY_HUB`` env var overrides the file lookup (useful for
   tests, scripted launches, and child processes the hub spawns).
3. **Shutdown**: on clean exit the hub deletes the file *only if its
   PID still matches* — that way a crashed-and-replaced hub doesn't
   clobber the live one's file.
4. **Conflict detection**: starting a second hub for the same project
   fails fast (the in-memory record points at a still-live PID).

The module is small on purpose — discovery is the one piece of
hub-state every adapter touches, so it has to be obvious to read and
hard to corrupt.
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
    """Raised when the on-disk discovery file is unreadable or malformed."""


class HubAlreadyRunningError(HubDiscoveryError):
    """Raised when a hub start is attempted while another is live for the project."""

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

    Kept intentionally narrow; anything that's not needed by a client
    to *connect* belongs in the runtime log, not in the discovery file.

    ``http_port`` is present only when the hub was started with
    ``--serve-viewer``; it carries the bound port of the viewer HTTP+WS
    layer so users can ``open http://localhost:<http_port>/``.
    """

    v: int
    pid: int
    tcp: str
    server_version: str
    project_root: str
    started_at: str
    http_port: int | None = None

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        # Keep optional fields off the wire when absent so older readers
        # don't trip on unexpected keys.
        if self.http_port is None:
            out.pop("http_port", None)
        return out


def hub_dir(project_root: Path) -> Path:
    """Return ``<project_root>/.rtl-buddy/`` (does not create it)."""

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
) -> HubRecord:
    """Write ``hub.json`` after enforcing the one-hub-per-project rule.

    Uses an atomic ``rename`` so a reader will never see a half-written
    file. Raises :class:`HubAlreadyRunningError` if a live record already
    exists for this project.
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
    )

    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return record


def read_record(project_root: Path) -> HubRecord | None:
    """Return the on-disk ``HubRecord`` for ``project_root`` or ``None``."""

    return _read_record_if_present(discovery_path(project_root))


def delete_record_if_owner(project_root: Path, *, expected_pid: int) -> bool:
    """Remove ``hub.json`` iff its ``pid`` matches ``expected_pid``.

    Returns ``True`` when the file was deleted. The PID check prevents
    a stale shutdown handler from clobbering a fresh hub that grabbed
    the file in between (the "crashed and replaced" race).
    """

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
    """Return ``$RTL_BUDDY_HUB`` if set, else ``None``.

    The override is a literal ``host:port`` string; callers parse it.
    """

    return os.environ.get(ENV_OVERRIDE) or None


def find_project_root_with_hub(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``.rtl-buddy/hub.json``.

    Used by clients (the nvim plugin, ``rb wave``, the CLI's
    ``rb hub status`` outside the start directory) to locate the hub
    without a hard-coded project path. Returns the directory containing
    ``.rtl-buddy/`` or ``None``.
    """

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
        return HubRecord(
            v=int(raw["v"]),
            pid=int(raw["pid"]),
            tcp=str(raw["tcp"]),
            server_version=str(raw["server_version"]),
            project_root=str(raw["project_root"]),
            started_at=str(raw["started_at"]),
            http_port=http_port,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HubDiscoveryError(
            f"{path}: hub.json missing or malformed field — {exc}"
        ) from exc


def _pid_is_live(pid: int) -> bool:
    """Return ``True`` if ``pid`` names a live process owned by anyone.

    Uses ``os.kill(pid, 0)``: signal 0 doesn't actually deliver, but
    POSIX requires the caller to have permission to signal the target
    (or get EPERM, which still confirms liveness). Windows would need a
    different path, but the hub is POSIX-only in v1.
    """

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
    """Send ``sig`` to ``pid``.

    Thin wrapper kept here so ``rb hub stop`` doesn't have to import
    ``signal`` directly; the discovery module owns the
    "talk to the running hub by PID" concern.
    """

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
    "read_record",
    "delete_record_if_owner",
    "env_override",
    "find_project_root_with_hub",
    "signal_process",
]
