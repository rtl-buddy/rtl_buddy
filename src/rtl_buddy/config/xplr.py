"""Optional ``cfg-xplr`` block in ``root_config.yaml`` for ``rb xplr``.

Sets the commit mode, the auto-commit source scope, the disk watermark and hard cap in GB, the eviction policy and the worktree root. Every key is optional. ``worktree-root`` is relative to the project root and must be gitignored.

:func:`load_xplr_config` reads the block directly from ``root_config.yaml`` without loading the full RootConfig.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

import yaml
from serde import field, from_dict, serde

from ..errors import FatalRtlBuddyError


COMMIT_MODES = ("auto", "self-managed")
EVICTION_POLICIES = ("keep-frontier", "oldest-first", "manual")

DEFAULT_COMMIT_MODE = "auto"
DEFAULT_SOURCE_SCOPE = ["."]
DEFAULT_DISK_HIGH_WATERMARK_GB = 50.0
DEFAULT_DISK_HARD_CAP_GB = 80.0
DEFAULT_EVICTION_POLICY = "keep-frontier"
DEFAULT_WORKTREE_ROOT = "artefacts/xplr/worktrees"

_KNOWN_KEYS = (
    "commit-mode",
    "source-scope",
    "disk-high-watermark-gb",
    "disk-hard-cap-gb",
    "eviction-policy",
    "worktree-root",
)


@dataclass(frozen=True)
class XplrConfig:
    """Validated cfg-xplr settings."""

    commit_mode: str = DEFAULT_COMMIT_MODE
    source_scope: list[str] = dc_field(
        default_factory=lambda: list(DEFAULT_SOURCE_SCOPE)
    )
    disk_high_watermark_gb: float = DEFAULT_DISK_HIGH_WATERMARK_GB
    disk_hard_cap_gb: float = DEFAULT_DISK_HARD_CAP_GB
    eviction_policy: str = DEFAULT_EVICTION_POLICY
    worktree_root: str = DEFAULT_WORKTREE_ROOT

    def worktree_dir(self, project_root: Path) -> Path:
        """Return the worktree root, resolved against `project_root`."""

        root = Path(self.worktree_root)
        return root if root.is_absolute() else project_root / root


@serde
class XplrConfigFile:
    """YAML form of the cfg-xplr block."""

    commit_mode: str = field(rename="commit-mode", default=DEFAULT_COMMIT_MODE)
    source_scope: list[str] = field(
        rename="source-scope",
        default_factory=lambda: list(DEFAULT_SOURCE_SCOPE),
    )
    disk_high_watermark_gb: float = field(
        rename="disk-high-watermark-gb", default=DEFAULT_DISK_HIGH_WATERMARK_GB
    )
    disk_hard_cap_gb: float = field(
        rename="disk-hard-cap-gb", default=DEFAULT_DISK_HARD_CAP_GB
    )
    eviction_policy: str = field(
        rename="eviction-policy", default=DEFAULT_EVICTION_POLICY
    )
    worktree_root: str = field(rename="worktree-root", default=DEFAULT_WORKTREE_ROOT)

    def initialise(self) -> XplrConfig:
        """Validate the block and return an :class:`XplrConfig`."""

        if self.commit_mode not in COMMIT_MODES:
            raise FatalRtlBuddyError(
                f"cfg-xplr: invalid commit-mode {self.commit_mode!r}: must be "
                f"one of {', '.join(COMMIT_MODES)}"
            )
        if self.eviction_policy not in EVICTION_POLICIES:
            raise FatalRtlBuddyError(
                f"cfg-xplr: invalid eviction-policy {self.eviction_policy!r}: "
                f"must be one of {', '.join(EVICTION_POLICIES)}"
            )
        scope = [str(p) for p in self.source_scope]
        if not scope or any(not p.strip() for p in scope):
            raise FatalRtlBuddyError(
                "cfg-xplr: source-scope must be a non-empty list of paths"
            )
        high = float(self.disk_high_watermark_gb)
        cap = float(self.disk_hard_cap_gb)
        if high < 0 or cap < 0:
            raise FatalRtlBuddyError(
                "cfg-xplr: disk-high-watermark-gb and disk-hard-cap-gb must "
                "be non-negative"
            )
        if cap < high:
            raise FatalRtlBuddyError(
                f"cfg-xplr: disk-hard-cap-gb ({cap:g}) must be >= "
                f"disk-high-watermark-gb ({high:g}) — the hard cap is the "
                "backstop behind the gc trigger"
            )
        if not str(self.worktree_root).strip():
            raise FatalRtlBuddyError("cfg-xplr: worktree-root must be a path")
        return XplrConfig(
            commit_mode=self.commit_mode,
            source_scope=scope,
            disk_high_watermark_gb=high,
            disk_hard_cap_gb=cap,
            eviction_policy=self.eviction_policy,
            worktree_root=str(self.worktree_root),
        )


def load_xplr_config(project_root: Path) -> XplrConfig:
    """Read the ``cfg-xplr`` block from ``root_config.yaml``.

    A missing file or block yields the defaults. A malformed block or an unknown key raises :class:`FatalRtlBuddyError`.
    """

    path = Path(project_root) / "root_config.yaml"
    if not path.is_file():
        return XplrConfigFile().initialise()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FatalRtlBuddyError(f"{path}: not valid YAML: {exc}") from exc
    block = (data or {}).get("cfg-xplr")
    if block is None:
        return XplrConfigFile().initialise()
    if not isinstance(block, dict):
        raise FatalRtlBuddyError(
            f"{path}: cfg-xplr must be a mapping, got {type(block).__name__}"
        )
    unknown = sorted(set(block) - set(_KNOWN_KEYS))
    if unknown:
        raise FatalRtlBuddyError(
            f"{path}: cfg-xplr has unknown key(s) "
            f"{', '.join(repr(k) for k in unknown)}; allowed keys: "
            f"{', '.join(_KNOWN_KEYS)}"
        )
    try:
        cfg_file = from_dict(XplrConfigFile, block)
    except Exception as exc:
        raise FatalRtlBuddyError(f"{path}: invalid cfg-xplr block: {exc}") from exc
    return cfg_file.initialise()
