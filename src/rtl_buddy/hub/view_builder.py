"""On-demand view.json generator for ``rb hub start --model NAME``.

Wraps the existing ``RtlBuddyView`` subprocess wrapper. Result is
written to a stable path under ``<project_root>/.rtl-buddy/cache/``
so the HTTP server's ``/view.json`` endpoint can find it
deterministically across hub restarts. The (re)generation runs
synchronously at hub start; cache invalidation isn't modelled here
because rtl-buddy-view itself is fast enough on the demo designs we
target (~1-3s) and "restart hub to refresh design" is the expected
workflow.

If/when on-demand re-generation per HTTP request becomes desirable
(model picker, file-watch refresh), wrap this builder in a
content-hash cache; the layout was chosen to make that drop-in.
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
from ..tools.hier_rtl_buddy_view import RtlBuddyView
from .resolver import SUPPORTED_VIEW_SCHEMA_MAJOR

logger = logging.getLogger(__name__)


def _assert_view_schema_supported(out_path: Path, label: str) -> None:
    """Floor the view.json contract at its major version.

    rtl_buddy pins no rtl-buddy-view version, so the package floor can't
    guarantee the on-disk ``view.json`` shape. The renderer versions that
    shape with a top-level ``schema_version`` (currently ``"1.1"``); 1.x
    is forward-compatible (minor bumps add fields only), so we accept any
    ``1.x`` and reject a future, breaking major before the SPA loads it.
    Independent of the package version: a too-new renderer can be
    installed against an old rtl_buddy and this still catches it.
    """
    try:
        raw = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view produced an unreadable view.json "
            f"at {out_path} ({exc})."
        ) from exc
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
    """Cache lives under ``.rtl-buddy/cache/`` so it sits next to
    hub.toml + hub.log — one project-local directory,
    .gitignore-friendly via existing ``.rtl-buddy/`` ignores."""
    return project_root / ".rtl-buddy" / "cache"


def view_json_path(project_root: Path, model_name: str) -> Path:
    """Per-model output path. Stable so the SPA's ``/view.json``
    request always hits the same file regardless of generation state.
    """
    return cache_dir(project_root) / f"view-{model_name}.json"


def view_json_path_for_tb(project_root: Path, model_name: str, tb_name: str) -> Path:
    """Per-(model, tb) output path for the TB-rooted view (#99 / 6b).

    Cache key is ``(model, tb)`` rather than the test name: two tests
    that share a testbench elaborate to byte-identical trees, so they
    should share the artefact and the second click is a hot-cache
    hit. The path layout mirrors the artefact tree the CLI wrapper
    writes (``artefacts/hier/<model>/tb/<tb>/hier.f``) so an
    operator reading either side recognises the same key.
    """
    return cache_dir(project_root) / f"view-{model_name}-tb-{tb_name}.json"


def _resolve_viewer_executable() -> str:
    """Locate the ``rtl-buddy-view`` binary or raise a clear error.
    Same lookup convention as ``RtlBuddyView.run``.
    """
    exe = shutil.which("rtl-buddy-view")
    if exe is None:
        raise FatalRtlBuddyError(
            "rb hub --model: 'rtl-buddy-view' not found on PATH. "
            "Install rtl-buddy-view into the active venv "
            "(`uv add rtl-buddy-view` or `pip install rtl-buddy-view`)."
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
    """Generate view.json for ``model_cfg`` at the stable cache path
    and return it. Raises ``FatalRtlBuddyError`` when the
    rtl-buddy-view subprocess fails — the hub treats a missing
    view.json as a fatal startup error, not a degraded mode.

    When ``model_cfg.cdc`` is set, the builder first calls
    ``cdc_builder.build_domain_map`` to produce the clock-domain map
    via ``rtl-buddy-cdc --emit-domain-map`` and feeds the result as
    ``--cdc-annotations`` to rtl-buddy-view. The SPA's clock overlay
    toggle then has data to render against. Models without ``cdc:``
    fall through to the no-overlay path unchanged.

    When ``axi_perf_source`` is supplied (via the hub's
    ``--axi-perf-from`` start-up flag), the builder also passes
    ``--overlay axi-perf=<path>`` so rtl-buddy-view bakes the
    throughput overlay AND records the test/suite_dir metadata that
    the SPA's "Open in marimo" button reads to skip its prompt
    (Phase 2.5 of the marimo umbrella). When not supplied, the
    no-overlay path runs unchanged.

    When ``test_cfg`` is supplied (#99 / 6b), the renderer is invoked
    in TB-rooted mode (``--tb-top <tb.toplevel>`` alongside the
    existing ``--top <model.name>``) and the cache path keys on the
    ``(model, tb)`` pair via :func:`view_json_path_for_tb`. The DUT-
    side CDC overlay is unchanged — the domain map's instance paths
    still resolve into the rendered tree because they live under the
    DUT subtree, which appears in TB elaboration too.
    """

    # Build the domain map FIRST so a misconfigured cdc: back-pointer
    # fails before we spend cycles on rtl-buddy-view. Import locally
    # to avoid a hub→cdc import cycle.
    from . import cdc_builder

    domain_map = cdc_builder.build_domain_map(
        project_root=project_root, model_cfg=model_cfg
    )

    cache = cache_dir(project_root)
    cache.mkdir(parents=True, exist_ok=True)
    if test_cfg is not None:
        out_path = view_json_path_for_tb(project_root, model_cfg.name, test_cfg.tb.name)
    else:
        out_path = view_json_path(project_root, model_cfg.name)

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
    label = (
        f"rb hub --model {model_cfg.name} --test {test_cfg.name}"
        if test_cfg is not None
        else f"rb hub --model {model_cfg.name}"
    )
    rc = runner.run()
    if rc != 0 or not out_path.is_file():
        raise FatalRtlBuddyError(
            f"{label}: rtl-buddy-view exited with "
            f"code {rc}; see {Path(runner.artefact_dir) / 'hier.log'} for "
            f"details."
        )

    _assert_view_schema_supported(out_path, label)
    return out_path
