"""view.json generator for ``rb hub start --model NAME``.

Runs ``rtl-buddy-view`` through ``RtlBuddyView`` and writes the result to a stable path under ``<project_root>/.rtl-buddy/cache/``, where the HTTP server's ``/view.json`` endpoint finds it. Each call regenerates the file; nothing is cached across calls.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from ..config.model import ModelConfig
from ..config.test import TestConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.artifact_paths import clear_stale_artefacts
from ..tools.hier_rtl_buddy_view import RtlBuddyView
from .resolver import SUPPORTED_VIEW_SCHEMA_MAJOR

logger = logging.getLogger(__name__)


def _assert_view_schema_supported(out_path: Path, label: str) -> None:
    """Raise ``FatalRtlBuddyError`` unless ``view.json`` is an object whose ``schema_version`` has a supported major version.

    Minor versions of the supported major are accepted.
    """
    try:
        raw = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view produced an unreadable view.json "
            f"at {out_path} ({exc})."
        ) from exc
    # A non-object top level must raise FatalRtlBuddyError here, not AttributeError on .get.
    if not isinstance(raw, dict):
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view produced a view.json whose top level is "
            f"{type(raw).__name__}, not an object, at {out_path}."
        )
    schema = raw.get("schema_version", "")
    try:
        major = int(str(schema).split(".", 1)[0])
    except ValueError as exc:
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view view.json schema_version unparseable: {schema!r}."
        ) from exc
    if major != SUPPORTED_VIEW_SCHEMA_MAJOR:
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view emitted view.json schema major {major}, "
            f"but this rtl_buddy supports {SUPPORTED_VIEW_SCHEMA_MAJOR}.x. "
            "Upgrade rtl_buddy to a version that understands the new view.json "
            "schema, or pin an rtl-buddy-view release that still emits "
            f"{SUPPORTED_VIEW_SCHEMA_MAJOR}.x."
        )


def cache_dir(project_root: Path) -> Path:
    """Return the view cache directory, ``<project_root>/.rtl-buddy/cache``."""
    return project_root / ".rtl-buddy" / "cache"


def view_json_path(project_root: Path, model_name: str) -> Path:
    """Return the per-model ``view.json`` path."""
    return cache_dir(project_root) / f"view-{model_name}.json"


def view_json_path_for_tb(project_root: Path, model_name: str, tb_name: str) -> Path:
    """Return the ``view.json`` path for a TB-rooted view.

    Keyed on ``(model, tb)`` rather than the test name, so tests sharing a testbench share the file.
    """
    return cache_dir(project_root) / f"view-{model_name}-tb-{tb_name}.json"


def _resolve_viewer_executable() -> str:
    """Return the ``rtl-buddy-view`` path from ``PATH``, or raise ``FatalRtlBuddyError``."""
    exe = shutil.which("rtl-buddy-view")
    if exe is None:
        raise FatalRtlBuddyError(
            "rb hub --model: 'rtl-buddy-view' not found on PATH. "
            "Install rtl-buddy-sch into the active venv "
            "(`uv add rtl-buddy-sch` or `pip install rtl-buddy-sch`) — "
            "the dist that ships the rtl-buddy-view executable."
        )
    return exe


def build_view_json(
    *,
    project_root: Path,
    model_cfg: ModelConfig,
    axi_perf_source: Path | None = None,
    test_cfg: TestConfig | None = None,
    test_suite_dir: Path | None = None,
) -> Path:
    """Generate ``view.json`` for ``model_cfg`` and return its path.

    Raises ``FatalRtlBuddyError`` when ``rtl-buddy-view`` fails or its output is unreadable or has an unsupported schema major version; the file is removed in those cases.

    - ``model_cfg.cdc`` set: a clock-domain map from ``cdc_builder.build_domain_map`` is passed as ``--cdc-annotations``.
    - ``axi_perf_source`` set: passed as ``--overlay axi-perf=<path>``, which also records the test and suite dir for the SPA's "Open in marimo" button.
    - ``test_cfg`` set: the viewer renders from the testbench top (``--tb-top``), and the output goes to the ``(model, tb)`` path from :func:`view_json_path_for_tb`.
    """

    cache = cache_dir(project_root)
    cache.mkdir(parents=True, exist_ok=True)
    if test_cfg is not None:
        out_path = view_json_path_for_tb(project_root, model_cfg.name, test_cfg.tb.name)
    else:
        out_path = view_json_path(project_root, model_cfg.name)
    label = (
        f"rb hub --model {model_cfg.name} --test {test_cfg.name}"
        if test_cfg is not None
        else f"rb hub --model {model_cfg.name}"
    )

    # Clear first: a failed rebuild must not leave the previous view.json to be served.
    removed = clear_stale_artefacts([out_path], owner=label)
    if removed:
        log_event(
            logger,
            logging.DEBUG,
            "hub.view_builder.stale_artefacts_removed",
            model=model_cfg.name,
            paths=removed,
        )

    # Local import avoids a hub-to-cdc import cycle.
    from . import cdc_builder

    domain_map = cdc_builder.build_domain_map(
        project_root=project_root, model_cfg=model_cfg
    )

    viewer_exe = _resolve_viewer_executable()

    log_event(
        logger,
        logging.INFO,
        "hub.view_builder.generating",
        model=model_cfg.name,
        tb=test_cfg.tb.name if test_cfg is not None else "",
        path=str(out_path),
        cdc_annotations=str(domain_map) if domain_map else "",
        axi_perf=str(axi_perf_source) if axi_perf_source else "",
    )
    runner = RtlBuddyView(
        name=f"hub/view/{model_cfg.name}"
        + (f"/tb/{test_cfg.tb.name}" if test_cfg is not None else ""),
        model_cfg=model_cfg,
        suite_dir=str(project_root),
        format="json",
        output=str(out_path),
        executable=viewer_exe,
        cdc_annotations=str(domain_map) if domain_map else None,
        axi_perf_annotations=str(axi_perf_source) if axi_perf_source else None,
        test_cfg=test_cfg,
        test_suite_dir=str(test_suite_dir) if test_suite_dir is not None else None,
    )
    rc = runner.run()
    if rc != 0 or not out_path.is_file():
        # The renderer can write the file and then fail.
        clear_stale_artefacts([out_path], owner=label)
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view exited with "
            f"code {rc}; see {Path(runner.artefact_dir) / 'hier.log'} for "
            f"details."
        )

    try:
        _assert_view_schema_supported(out_path, label)
    except Exception:
        # Catch every exception, not just FatalRtlBuddyError: a rejected file must not stay on disk.
        clear_stale_artefacts([out_path], owner=label)
        raise
    return out_path
