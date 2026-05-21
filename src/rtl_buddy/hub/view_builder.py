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

import logging
import shutil
from pathlib import Path

from ..config.model import ModelConfig
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.hier_rtl_buddy_view import RtlBuddyView

logger = logging.getLogger(__name__)


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
) -> Path:
    """Generate view.json for ``model_cfg`` at the stable cache path
    and return it. Raises ``FatalRtlBuddyError`` when the
    rtl-buddy-view subprocess fails — the hub treats a missing
    view.json as a fatal startup error, not a degraded mode.
    """

    cache = cache_dir(project_root)
    cache.mkdir(parents=True, exist_ok=True)
    out_path = view_json_path(project_root, model_cfg.name)

    viewer_exe = _resolve_viewer_executable()

    log_event(
        logger,
        logging.INFO,
        "hub.view_builder.generating",
        model=model_cfg.name,
        path=str(out_path),
    )
    runner = RtlBuddyView(
        name=f"hub/view/{model_cfg.name}",
        model_cfg=model_cfg,
        suite_dir=str(project_root),
        format="json",
        output=str(out_path),
        executable=viewer_exe,
    )
    rc = runner.run()
    if rc != 0 or not out_path.is_file():
        raise FatalRtlBuddyError(
            f"rb hub --model {model_cfg.name}: rtl-buddy-view exited with "
            f"code {rc}; see {Path(runner.artefact_dir) / 'hier.log'} for "
            f"details."
        )

    return out_path
