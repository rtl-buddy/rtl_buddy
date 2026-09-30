# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""Per-run result JSON envelopes.

A remotely dispatched test job (``rb _test-job``) writes its ``TestResults`` as a JSON envelope on a shared filesystem,
and the dispatching head process loads it into normal aggregation. The format does not depend on the scheduler.
"""

import json
import logging
import os
from importlib.metadata import version
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.artifact_paths import atomic_tmp_name
from .test_results import TestResults

logger = logging.getLogger(__name__)

RESULT_JSON_FILETYPE = "test_result"
RESULT_JSON_SCHEMA_VERSION = 1

BUILD_RESULT_FILETYPE = "build_result"
BUILD_RESULT_SCHEMA_VERSION = 1

# Trailing transcript lines a failed build records in its envelope.
COMPILE_ERROR_TAIL_LINES = 15

# Start of every "failed in the build job" desc; the head matches it as a prefix to tell an enriched desc from one it still has to enrich.
BUILD_COMPILE_FAIL_PREFIX = "compile failed in build job"

# Longest error line a one-line desc carries.
_ERROR_LINE_BUDGET = 160


def compile_error_tail(path, limit=COMPILE_ERROR_TAIL_LINES):
    """Return the last ``limit`` non-blank lines of a compile transcript.

    Blank lines are skipped across the whole file because a Verilator error can sit in the stderr section while the stdout section is empty.
    Never raises; an unreadable transcript yields no lines.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except (OSError, ValueError):
        return []
    lines = [line.rstrip() for line in text.splitlines()]
    return [line for line in lines if line.strip()][-limit:]


def _first_error_line(error_tail):
    """Return the single line of ``error_tail`` to show in a summary cell, or None.

    This is the first line that mentions an error, else the last line. Elements with embedded newlines are split first so the result is one line.
    """
    lines = [
        part.strip()
        for line in (error_tail or [])
        for part in str(line).splitlines()
        if part.strip()
    ]
    if not lines:
        return None
    # Skip the echoed "Command:" line; flags like ``--error-limit`` would match "error".
    chosen = next(
        (
            line
            for line in lines
            if "error" in line.lower() and not line.startswith("Command: ")
        ),
        lines[-1],
    )
    if len(chosen) > _ERROR_LINE_BUDGET:
        chosen = chosen[: _ERROR_LINE_BUDGET - 1].rstrip() + "…"
    return chosen


def build_compile_fail_desc(
    *, job_id=None, returncode=None, error_tail=None, logs=None
):
    """Return a one-line desc naming the build job's compile failure, for a summary row.

    Both the gated sim job and the head use it; ``logs`` is named rather than quoted.
    """
    desc = BUILD_COMPILE_FAIL_PREFIX
    if job_id is not None:
        desc += f" {job_id}"
    if returncode is not None:
        desc += f" (exit {returncode})"
    error_line = _first_error_line(error_tail)
    if error_line:
        desc += f": {error_line}"
    if logs:
        desc += f" (see {logs})"
    return desc


def write_result_json(
    path, *, test_name, run_id, results, run_token=None, run_tag=None
):
    """Atomically write one run's result envelope to ``path`` and return the path.

    The write goes through a temp file (:func:`atomic_tmp_name`) and ``os.replace``, so a collector never sees a partial envelope.
    Parent directories are created.

    ``run_token`` is the head's per-invocation nonce; :func:`load_result_json` uses it to reject an envelope left by an earlier run.
    ``run_tag`` is the ``--run-tag`` artefact namespace; it is written only when a tag was named.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "rtl-buddy-filetype": RESULT_JSON_FILETYPE,
        "schema_version": RESULT_JSON_SCHEMA_VERSION,
        "rtl_buddy_version": version("rtl-buddy"),
        "run_token": run_token,
        "test": test_name,
        "run_id": run_id,
        "result": results.to_json_dict(),
    }
    if run_tag is not None:
        envelope["run_tag"] = run_tag
    tmp = path.with_name(atomic_tmp_name(path.name))
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def attach_telemetry_json(path, telemetry: dict):
    """Fold scheduler usage telemetry into an existing envelope, atomically.

    The head calls this after the job finishes. It works on build and test envelopes alike, so it does not check the filetype.
    A missing envelope is not raised here; the caller treats it as a dispatch failure.
    """

    def _fold(raw):
        raw["telemetry"] = telemetry
        return True

    _rewrite_envelope(path, _fold)


def attach_result_key(path, key: str, value):
    """Fold one key into an envelope's nested ``result.results`` dict, atomically.

    Use it for facts about the run's result, such as the per-test compile record; `rb graph results` reads only that dict.
    A missing, unreadable or differently shaped envelope is a no-op, never a raise.
    """

    def _fold(raw):
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("results"), dict):
            return False
        result["results"][key] = value
        return True

    _rewrite_envelope(path, _fold)


def _write_envelope_best_effort(path, raw, *, what):
    """Atomically write ``raw`` to ``path`` and return whether the write landed; never raises.

    Annotations are advisory, so a full or read-only filesystem or an unserialisable value must not lose collected results.
    On failure the envelope is left as found.
    """
    tmp = path.with_name(atomic_tmp_name(path.name))
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=True, indent=2) + "\n")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        log_event(
            logger,
            logging.WARNING,
            "result_io.annotate_failed",
            path=str(path),
            what=what,
            error=str(e),
        )
        # Remove the temp file so a full filesystem is not left with a partial one.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _rewrite_envelope(path, fold):
    """Read-modify-write one JSON envelope atomically.

    ``fold`` mutates the parsed envelope in place and returns False to abandon the write.
    Unreadable, missing and non-object files, and failed writes, are no-ops.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict) or not fold(raw):
        return
    _write_envelope_best_effort(path, raw, what="annotation")


def refresh_result_json(path, results):
    """Re-persist an envelope's ``result`` block after coverage post-processing mutated it.

    Only ``result`` is replaced; ``run_token``, ``run_id`` and the filetype header are kept.
    Never raises: a missing or unreadable envelope or a failed write returns ``None`` and leaves the file as found.
    Otherwise it returns the path.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    raw["result"] = results.to_json_dict()
    if not _write_envelope_best_effort(path, raw, what="coverage refresh"):
        return None
    return path


def load_result_json(path, *, expected_run_token=None):
    """Load an envelope written by :func:`write_result_json`.

    Returns the envelope dict with ``result`` replaced by a reconstructed :class:`TestResults`.
    Raises ``FatalRtlBuddyError`` for a missing, malformed or schema-incompatible file.
    With ``expected_run_token``, an envelope whose ``run_token`` differs is a leftover from an earlier run and raises the same way.
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

    if expected_run_token is not None and raw.get("run_token") != expected_run_token:
        raise FatalRtlBuddyError(
            f"result JSON is from a different run (run_token "
            f"{raw.get('run_token')!r} != {expected_run_token!r}): {path}"
        )

    try:
        raw["result"] = TestResults.from_json_dict(raw.get("result"))
    except ValueError as e:
        raise FatalRtlBuddyError(f"result JSON malformed: {path}: {e}") from e
    return raw


def write_build_result_json(path, *, built, failed, builds=None, partial=False):
    """Atomically write a build job's compile outcome to ``path`` and return the path.

    ``built`` and ``failed`` list the expanded test names whose shared build succeeded or failed. The head uses them to report a compile failure instead of a dispatch failure.

    ``builds`` is an optional per-config record list in plan order, ``{test, builder, duration_sec, reused, group}``.
    A failed build's record also has ``returncode``, ``error_tail`` (see :data:`COMPILE_ERROR_TAIL_LINES`) and a suite-relative ``transcript``.
    The schema version does not change for these keys, and readers must tolerate their absence.

    ``partial`` marks an envelope the build job is still rewriting after each group.
    A reader must treat a test that a partial envelope does not list as one the job never reached.
    The key is written only when true; absence means complete.
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
    if builds is not None:
        envelope["builds"] = [dict(entry) for entry in builds]
    if partial:
        envelope["partial"] = True
    tmp = path.with_name(atomic_tmp_name(path.name))
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def load_build_result_json(path):
    """Load a build-result file, or return ``None`` if it is absent or unusable; never raises.

    Returns ``{"built": [...], "failed": [...], "builds": [...], "partial": bool}``. ``builds`` may be empty.
    Records are returned as written, so read their failure keys with ``.get()``.
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
        "builds": [
            entry for entry in (raw.get("builds") or []) if isinstance(entry, dict)
        ],
        "partial": raw.get("partial") is True,
    }
