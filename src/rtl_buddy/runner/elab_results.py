"""Structured results for one pyslang elaboration."""

import json
import logging
import os
from importlib.metadata import version
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

logger = logging.getLogger(__name__)

ELAB_RESULT_FILETYPE = "elab_result"
ELAB_RESULT_SCHEMA_VERSION = 1


class ElabResults:
    def __init__(self, name: str, results: dict, *, result_json: str | Path):
        self.name = name
        self.results = results
        self.result_json = Path(result_json)

    def is_pass(self) -> bool:
        return self.results.get("result") in {"PASS", "SKIP"}

    def to_row(self) -> dict:
        row = {"name": self.name, **self.results}
        row["result_json"] = str(self.result_json)
        return row


def write_elab_result_json(
    path: str | Path, *, model: str, profile: str | None, results: dict
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "rtl-buddy-filetype": ELAB_RESULT_FILETYPE,
        "schema_version": ELAB_RESULT_SCHEMA_VERSION,
        "rtl_buddy_version": version("rtl-buddy"),
        "model": model,
        "profile": profile,
        "result": results,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=True, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def write_elab_result_json_best_effort(
    path: str | Path, *, model: str, profile: str | None, results: dict
) -> Path:
    path = Path(path)
    try:
        return write_elab_result_json(
            path,
            model=model,
            profile=profile,
            results=results,
        )
    except Exception as exc:  # noqa: BLE001 - a sidecar cannot change the verdict
        log_event(
            logger,
            logging.WARNING,
            "elab.result_json_write_failed",
            model=model,
            profile=profile,
            path=str(path),
            error=str(exc),
        )
        return path


def load_elab_result_json(
    path: str | Path, *, model: str | None = None, profile: str | None = None
) -> ElabResults:
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise FatalRtlBuddyError(f'failed to load elaboration result "{path}"') from exc
    if raw.get("rtl-buddy-filetype") != ELAB_RESULT_FILETYPE:
        raise FatalRtlBuddyError(f'{path}: not an "{ELAB_RESULT_FILETYPE}" envelope')
    if raw.get("schema_version") != ELAB_RESULT_SCHEMA_VERSION:
        raise FatalRtlBuddyError(
            f"{path}: unsupported elaboration result schema "
            f"{raw.get('schema_version')!r}"
        )
    if model is not None and raw.get("model") != model:
        raise FatalRtlBuddyError(
            f"{path}: result is for model {raw.get('model')!r}, expected {model!r}"
        )
    if model is not None and raw.get("profile") != profile:
        raise FatalRtlBuddyError(
            f"{path}: result is for profile {raw.get('profile')!r}, "
            f"expected {profile!r}"
        )
    results = raw.get("result")
    if not isinstance(results, dict) or results.get("result") not in {
        "PASS",
        "FAIL",
        "SKIP",
    }:
        raise FatalRtlBuddyError(f"{path}: malformed elaboration result payload")
    name = raw["model"]
    if raw.get("profile") is not None:
        name += f":{raw['profile']}"
    return ElabResults(name, results, result_json=path)


def elab_failure(desc: str, *, stage: str = "infrastructure") -> dict:
    return {
        "result": "FAIL",
        "desc": desc,
        "stage": stage,
        "source_count": 0,
        "input_source_count": 0,
        "diagnostics": {"errors": 0, "warnings": 0},
        "elapsed_sec": 0.0,
        "peak_memory_bytes": 0,
    }
