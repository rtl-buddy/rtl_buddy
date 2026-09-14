import hashlib
import os
from enum import StrEnum


class SeedMode(StrEnum):
    DEFAULT = "default"
    NEW = "new"
    REPLAY = "replay"


# Derived seeds stay inside the signed-31-bit range every supported
# simulator accepts for a seed argument.
_SEED_MODULUS = 2**31


def derive_seed(master_seed: int, suite_identity: str, test_name: str, run_id) -> int:
    """The per-test seed for one (suite, test, run) under a master seed.

    A pure function of the expanded test's stable identity — the suite
    config path relative to the invocation root, the sweep-expanded test
    name, and the run id — so the value never depends on dispatch order,
    job timing, or which process computed it. Same inputs, same seed:
    re-running with the same ``--seed`` replays a run exactly without
    reading any artefact.
    """
    material = "\0".join(
        [
            str(master_seed),
            suite_identity,
            test_name,
            "" if run_id is None else str(run_id),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % _SEED_MODULUS


def seed_identity_for(suite_config_path, base_dir) -> str:
    """The suite identity :func:`derive_seed` mixes in.

    The suite config's path relative to the invocation base when it sits
    underneath it — stable for a project layout across checkouts and
    machines — else the absolute path.
    """
    path = os.path.abspath(suite_config_path)
    base = os.path.abspath(base_dir)
    rel = os.path.relpath(path, base)
    if rel == ".." or rel.startswith(f"..{os.sep}"):
        return path
    return rel
