# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Stable per-test seed derivation."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


MAX_SIM_SEED = (1 << 31) - 1
SEED_DERIVATION_VERSION = 1


@dataclass(frozen=True)
class SeedResolution:
    """One test's resolved runtime seed and the identity that produced it."""

    seed: int
    source: str
    identity: str


def validate_master_seed(seed: int) -> int:
    """Return a valid exact master seed, or raise ``ValueError``."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("master seed must be an integer")
    if seed < 0:
        raise ValueError("master seed must be a nonnegative integer")
    return seed


def validate_sim_seed(seed: int) -> int:
    """Return a simulator-safe positive seed, or raise ``ValueError``."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("sim-rand-seed must be an integer")
    if not 1 <= seed <= MAX_SIM_SEED:
        raise ValueError(f"sim-rand-seed must be between 1 and {MAX_SIM_SEED}")
    return seed


def suite_seed_identity(suite_config_path: str, seed_root: str) -> str:
    """Return a checkout-independent suite path relative to ``seed_root``."""
    suite = Path(suite_config_path).resolve()
    root = Path(seed_root).resolve()
    try:
        relative = suite.relative_to(root)
    except ValueError:
        relative = Path(os.path.relpath(suite, root))
    return relative.as_posix()


def expanded_test_seed_identity(
    suite_identity: str, test_name: str, run_id: int | None
) -> str:
    """Return the documented stable identity used by seed derivation."""
    run = "single" if run_id is None else str(run_id)
    return f"{suite_identity}::{test_name}::{run}"


def derive_test_seed(
    master_seed: int,
    *,
    suite_identity: str,
    test_name: str,
    run_id: int | None,
) -> SeedResolution:
    """Derive a stable simulator seed from a master and expanded-test identity."""
    validate_master_seed(master_seed)
    identity = expanded_test_seed_identity(suite_identity, test_name, run_id)
    payload = json.dumps(
        [SEED_DERIVATION_VERSION, master_seed, suite_identity, test_name, run_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    seed = value % MAX_SIM_SEED + 1
    return SeedResolution(seed=seed, source="master", identity=identity)
