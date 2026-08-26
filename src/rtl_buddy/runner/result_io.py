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
import logging
import os
from importlib.metadata import version
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .test_results import TestResults

logger = logging.getLogger(__name__)

RESULT_JSON_FILETYPE = "test_result"
RESULT_JSON_SCHEMA_VERSION = 1

BUILD_RESULT_FILETYPE = "build_result"
BUILD_RESULT_SCHEMA_VERSION = 1


def write_result_json(path, *, test_name, run_id, results, run_token=None):
    """Atomically write one run's result envelope to ``path``.

    The write goes through a sibling ``.tmp`` file and ``os.replace`` so
    a collector polling a shared filesystem never observes a partially
    written envelope. Parent directories are created as needed. Returns
    the resolved path.

    ``run_token`` is the head's per-invocation nonce (from the dispatch
    plan). Stamping it here lets the head reject a stale envelope from an
    earlier run by identity, so it need not pre-unlink the path — see
    :func:`load_result_json` and #362.
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
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def attach_telemetry_json(path, telemetry: dict):
    """Fold scheduler usage telemetry into an existing result envelope.

    Called by the collecting head after the job finished (the job cannot
    know its own final accounting numbers). Atomic like the writer. A
    missing envelope is the caller's DispatchFail case — not raised here.

    Envelope-shape-agnostic on purpose: a *build* envelope gets its sacct
    row folded in by this very function (#495), which is why nothing here
    validates the filetype.
    """

    def _fold(raw):
        raw["telemetry"] = telemetry
        return True

    _rewrite_envelope(path, _fold)


def attach_result_key(path, key: str, value):
    """Fold one key into a result envelope's nested ``result.results`` dict.

    The sibling of :func:`attach_telemetry_json` for facts that belong to
    the *run's result* rather than to the envelope around it — the
    per-test compile record the build job observed (#495). ``result`` is
    where `rb graph results` reads a run's payload from, so a top-level
    key would travel with the artifact and still be invisible to the
    overlay.

    Best-effort and atomic, exactly like the telemetry attach: a missing,
    unreadable or differently-shaped envelope is a no-op, never a raise —
    a collected run must not be re-scored because an annotation failed.
    """

    def _fold(raw):
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("results"), dict):
            return False
        result["results"][key] = value
        return True

    _rewrite_envelope(path, _fold)


def _write_envelope_best_effort(path, raw, *, what):
    """Atomically write ``raw`` to ``path``; report failure, never raise.

    The write half of every *annotating* rewrite in this module. The read
    half was always guarded, but the serialize-and-write half was not, and
    the collector now performs one of these per collected row: a full or
    read-only shared filesystem (ENOSPC/EROFS/EACCES) at collect time would
    otherwise turn a fully finished fleet into a traceback and lose every
    result it had already gathered. A value the caller could not serialise
    (``TypeError``) is the same class of problem — the annotation is
    advisory, the run's verdict is not. Returns whether the write landed;
    on failure the envelope is left exactly as it was found.
    """
    tmp = path.with_name(path.name + ".tmp")
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
        # A half-written sibling would be read by nothing (only `path` is
        # ever loaded), but leaving one behind on a full filesystem is
        # gratuitous.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _rewrite_envelope(path, fold):
    """Read-modify-write one JSON envelope through a temp file + replace.

    ``fold`` mutates the parsed envelope in place and returns whether the
    write is still wanted; False abandons it (the envelope was not the
    shape the caller expected). Unreadable, missing and non-object files
    are no-ops, and so is a write that fails — every caller here is
    annotating a finished run, and an annotation must never be the thing
    that raises.
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
    """Re-persist an envelope's ``result`` block after post-run mutation.

    Coverage post-processing runs *after* the side-car is written and
    mutates the per-test coverage dict in place — the LCOV export, the
    HTML tree and the Coverview archive only exist by then. Without this
    the envelope keeps the truncated dict it was born with
    (``lcov_path`` / ``html_dir`` / ``merged_path`` all null) and the
    artefacts are reachable only from that run's console output (#399).

    Only ``result`` is replaced; ``run_token``, ``run_id`` and the
    filetype header are the identity of the envelope and are preserved.
    Atomic like the writer, and silent on a missing or unreadable
    envelope — or on a write that cannot land — because this is a
    best-effort side-car, never a run's verdict.
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

    Returns the envelope dict with ``result`` replaced by a
    reconstructed :class:`TestResults`. Raises ``FatalRtlBuddyError`` on
    a missing, malformed, or schema-incompatible file — a dispatch
    backend maps that to an infrastructure-failure result rather than
    silently dropping the run.

    When ``expected_run_token`` is given, an envelope whose ``run_token``
    does not match is treated as *not this run's result*: it is a leftover
    from an earlier run (the head no longer pre-unlinks stale envelopes, to
    avoid the NFS negative-dentry blindness of #362), so it raises the same
    way a missing file does rather than being mistaken for a real result.
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


def write_build_result_json(path, *, built, failed, builds=None):
    """Atomically write a build job's compile outcome to ``path``.

    ``built``/``failed`` are lists of expanded test names whose shared
    build succeeded/failed on the compute node. The head loads this at
    collect time so a compile failure surfaces as a CompileFail (parity
    with the in-process path) instead of the sim job's downstream
    DispatchFail. Best-effort on the head — a missing/unreadable file just
    means no compile-fail annotation.

    ``builds`` is the optional per-config compile record (#495), in plan
    order: ``{test, builder, duration_sec, reused, group}``. It is
    *additive* and the schema version deliberately does not move — an old
    head reading a new envelope ignores the key and keeps its
    compile-fail parity, and a new head reading an old one sees an empty
    list. Omitted entirely when the caller has nothing to say, so a
    ``parallel: 1`` job's envelope is byte-for-byte what it always was.
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
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def load_build_result_json(path):
    """Load a build-result file, or ``None`` if absent/unusable.

    Returns ``{"built": [...], "failed": [...], "builds": [...]}``. Unlike
    :func:`load_result_json` this never raises: the annotation it feeds is
    advisory, so a build job that died before writing simply yields no
    compile-fail mapping.

    ``builds`` is the per-config compile record (#495) and is empty for an
    envelope written before it existed — the key is additive, so a mixed
    version fleet degrades to today's behaviour instead of failing.
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
    }
