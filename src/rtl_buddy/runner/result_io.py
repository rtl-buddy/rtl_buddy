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
