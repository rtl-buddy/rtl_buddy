# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Per-run result JSON artifacts (#351 P0).

A remotely dispatched test job (``rb _test-job``) writes its
:class:`~rtl_buddy.runner.test_results.TestResults` as a small JSON
envelope; the dispatching head process loads the file and feeds the
reconstructed result into the normal aggregation path. The format is
scheduler-independent: anything that can run ``rb _test-job`` against a
shared filesystem can produce collectable results.
"""

import json
import os
from importlib.metadata import version
from pathlib import Path

from ..errors import FatalRtlBuddyError
from .test_results import TestResults

RESULT_JSON_FILETYPE = "test_result"
RESULT_JSON_SCHEMA_VERSION = 1

BUILD_RESULT_FILETYPE = "build_result"
BUILD_RESULT_SCHEMA_VERSION = 1


def write_result_json(path, *, test_name, run_id, results):
    """Atomically write one run's result envelope to ``path``.

    The write goes through a sibling ``.tmp`` file and ``os.replace`` so
    a collector polling a shared filesystem never observes a partially
    written envelope. Parent directories are created as needed. Returns
    the resolved path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "rtl-buddy-filetype": RESULT_JSON_FILETYPE,
        "schema_version": RESULT_JSON_SCHEMA_VERSION,
        "rtl_buddy_version": version("rtl-buddy"),
        "test": test_name,
        "run_id": run_id,
        "result": results.to_json_dict(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def attach_telemetry_json(path, telemetry: dict):
    """Fold scheduler usage telemetry into an existing result envelope.

    Called by the collecting head after the job finished (the job cannot
    know its own final accounting numbers). Atomic like the writer. A
    missing envelope is the caller's DispatchFail case — not raised here.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    raw["telemetry"] = telemetry
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)


def load_result_json(path):
    """Load an envelope written by :func:`write_result_json`.

    Returns the envelope dict with ``result`` replaced by a
    reconstructed :class:`TestResults`. Raises ``FatalRtlBuddyError`` on
    a missing, malformed, or schema-incompatible file — a dispatch
    backend maps that to an infrastructure-failure result rather than
    silently dropping the run.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise FatalRtlBuddyError(f"result JSON missing: {path}") from e
    except json.JSONDecodeError as e:
        raise FatalRtlBuddyError(f"result JSON malformed: {path}: {e}") from e

    if (
        not isinstance(raw, dict)
        or raw.get("rtl-buddy-filetype") != RESULT_JSON_FILETYPE
    ):
        raise FatalRtlBuddyError(f"not a {RESULT_JSON_FILETYPE} JSON: {path}")

    schema = raw.get("schema_version")
    if schema != RESULT_JSON_SCHEMA_VERSION:
        raise FatalRtlBuddyError(
            f"unsupported {RESULT_JSON_FILETYPE} schema_version {schema!r} in "
            f"{path} (expected {RESULT_JSON_SCHEMA_VERSION})"
        )

    try:
        raw["result"] = TestResults.from_json_dict(raw.get("result"))
    except ValueError as e:
        raise FatalRtlBuddyError(f"result JSON malformed: {path}: {e}") from e
    return raw


def write_build_result_json(path, *, built, failed):
    """Atomically write a build job's compile outcome to ``path``.

    ``built``/``failed`` are lists of expanded test names whose shared
    build succeeded/failed on the compute node. The head loads this at
    collect time so a compile failure surfaces as a CompileFail (parity
    with the in-process path) instead of the sim job's downstream
    DispatchFail. Best-effort on the head — a missing/unreadable file just
    means no compile-fail annotation.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "rtl-buddy-filetype": BUILD_RESULT_FILETYPE,
        "schema_version": BUILD_RESULT_SCHEMA_VERSION,
        "rtl_buddy_version": version("rtl-buddy"),
        "built": list(built),
        "failed": list(failed),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def load_build_result_json(path):
    """Load a build-result file, or ``None`` if absent/unusable.

    Returns ``{"built": [...], "failed": [...]}``. Unlike
    :func:`load_result_json` this never raises: the annotation it feeds is
    advisory, so a build job that died before writing simply yields no
    compile-fail mapping.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("rtl-buddy-filetype") != BUILD_RESULT_FILETYPE
        or raw.get("schema_version") != BUILD_RESULT_SCHEMA_VERSION
    ):
        return None
    return {
        "built": list(raw.get("built") or []),
        "failed": list(raw.get("failed") or []),
    }
