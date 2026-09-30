"""Generate the clock-domain map for a model's ``cdc:`` back-pointer by running ``rtl-buddy-cdc lint --emit-domain-map``.

The map is cached under ``.rtl-buddy/cache/`` and baked into ``view.json`` for the clock overlay.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..config.cdc import CdcConfig, CdcSuiteConfig
from ..config.model import ModelConfig, resolve_back_pointer
from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from ..tools.artifact_paths import clear_stale_artefacts
from ..tools.cdc_rtl_buddy import warn_unsupported_incdirs
from ..tools.vlog_filelist import VlogFilelist
from .view_builder import cache_dir

logger = logging.getLogger(__name__)


def domain_map_path(project_root: Path, model_name: str) -> Path:
    """Return the cache path of the model's domain map."""
    return cache_dir(project_root) / f"domain-{model_name}.json"


def _resolve_cdc_analysis(model_cfg: ModelConfig) -> CdcConfig | None:
    """Resolve the ``cdc:`` back-pointer to a ``CdcConfig``.

    Returns ``None`` when the back-pointer is unset. Raises ``FatalRtlBuddyError`` when the referenced file or analysis cannot be resolved.
    """
    resolved = resolve_back_pointer(model_cfg, "cdc")
    if resolved is None:
        return None
    cdc_yaml_path, analysis_name = resolved
    if not Path(cdc_yaml_path).is_file():
        raise FatalRtlBuddyError(
            f"model {model_cfg.name!r} cdc back-pointer points at "
            f"{cdc_yaml_path}: file does not exist"
        )

    suite = CdcSuiteConfig(cdc_yaml_path)

    if analysis_name is not None:
        analyses = suite.get_analyses(analysis_name)
        return analyses[0]

    # Match on the model name, not get_top(): the analysis top follows the model's ``top:`` override.
    matches = [a for a in suite.get_analyses() if a.get_model().name == model_cfg.name]
    if len(matches) == 0:
        names = ", ".join(suite.get_analysis_names()) or "(none)"
        raise FatalRtlBuddyError(
            f"model {model_cfg.name!r} cdc back-pointer points at "
            f"{cdc_yaml_path}, but no analysis there has "
            f"model: {model_cfg.name!r}. Analyses found: {names}. "
            f"Add a '#analysis_name' fragment to the cdc: field to "
            f"pick one explicitly."
        )
    if len(matches) > 1:
        names = ", ".join(a.get_name() for a in matches)
        raise FatalRtlBuddyError(
            f"model {model_cfg.name!r} cdc back-pointer points at "
            f"{cdc_yaml_path}, which has multiple analyses for this "
            f"model ({names}). Pick one with 'cdc.yaml#analysis_name' "
            f"in models.yaml."
        )
    return matches[0]


def _resolve_cdc_executable() -> str:
    exe = shutil.which("rtl-buddy-cdc")
    if exe is None:
        raise FatalRtlBuddyError(
            "rb hub --model: model has a 'cdc:' back-pointer but "
            "'rtl-buddy-cdc' is not on PATH. Either install "
            "rtl-buddy-cdc into the active venv, or remove the cdc: "
            "field from the model entry to skip the overlay."
        )
    return exe


# rtl-buddy-cdc takes plain source paths only; these filelist options are dropped.
_FILELIST_SKIP_PREFIXES = ("+incdir+", "+libext+", "+define+", "-y ", "-F ", "-f ")
_FILELIST_SOURCE_PREFIX = "-v "


def _source_files_from_filelist(fl_path: str) -> list[str]:
    """Return the source paths listed in a filelist, resolved against its directory."""
    fl_dir = os.path.dirname(os.path.abspath(fl_path))
    paths: list[str] = []
    with open(fl_path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            if any(line.startswith(opt) for opt in _FILELIST_SKIP_PREFIXES):
                continue
            if line.startswith(_FILELIST_SOURCE_PREFIX):
                line = line[len(_FILELIST_SOURCE_PREFIX) :]
            paths.append(os.path.normpath(os.path.join(fl_dir, line)))
    return paths


def _clear_cached_map(out_path: Path, model_name: str) -> None:
    """Remove the cached domain map."""
    removed = clear_stale_artefacts([out_path], owner=model_name)
    if removed:
        log_event(
            logger,
            logging.DEBUG,
            "hub.cdc_builder.stale_artefacts_removed",
            model=model_name,
            paths=removed,
        )


def build_domain_map(
    *,
    project_root: Path,
    model_cfg: ModelConfig,
) -> Path | None:
    """Generate the domain map for ``model_cfg``'s clock overlay.

    Returns the map path, or ``None`` when the model has no ``cdc:`` back-pointer. Raises ``FatalRtlBuddyError`` when a back-pointer is set but resolution or lint fails.
    """
    # Clear before any step that can raise, so a failed build never leaves a previous build's map to be served.
    out_path = domain_map_path(project_root, model_cfg.name)
    _clear_cached_map(out_path, model_cfg.name)

    analysis = _resolve_cdc_analysis(model_cfg)
    if analysis is None:
        return None

    sdc_path = analysis.get_constraints()
    if not os.path.isfile(sdc_path):
        raise FatalRtlBuddyError(
            f"cdc analysis {analysis.get_name()!r}: SDC not found at {sdc_path}"
        )
    waivers_path = analysis.get_waivers()
    if waivers_path is not None and not os.path.isfile(waivers_path):
        raise FatalRtlBuddyError(
            f"cdc analysis {analysis.get_name()!r}: waivers file not "
            f"found at {waivers_path}"
        )

    cache = cache_dir(project_root)
    cache.mkdir(parents=True, exist_ok=True)

    artefact_dir = project_root / "artefacts" / "hub-cdc" / model_cfg.name
    artefact_dir.mkdir(parents=True, exist_ok=True)
    fl_path = str(artefact_dir / "cdc.f")
    vlog_fl = VlogFilelist(
        name=f"hub/cdc/{model_cfg.name}/filelist",
        model_cfg=model_cfg,
        output_path=fl_path,
    )
    vlog_fl.write_output(
        output_filepath=fl_path, unroll=True, strip=False, deduplicate=True
    )
    sources = _source_files_from_filelist(fl_path)
    if not sources:
        raise FatalRtlBuddyError(
            f"cdc analysis {analysis.get_name()!r}: filelist {fl_path} "
            f"produced no sources"
        )

    cdc_exe = _resolve_cdc_executable()
    warn_unsupported_incdirs(analysis.get_name(), fl_path)
    log_path = artefact_dir / "cdc.log"
    # Lint output is discarded; only the emitted domain map is used.
    cmd = [
        cdc_exe,
        "lint",
        "--top",
        analysis.get_top(),
        "--sdc",
        sdc_path,
        "--emit-domain-map",
        str(out_path),
        "--format",
        "json",
        "--output",
        os.devnull,
    ]
    if waivers_path is not None:
        cmd += ["--waivers", waivers_path]
    if analysis.frontend is not None:
        cmd += ["--frontend", analysis.frontend]
    cmd += sources

    log_event(
        logger,
        logging.INFO,
        "hub.cdc_builder.generating",
        model=model_cfg.name,
        analysis=analysis.get_name(),
        path=str(out_path),
    )
    with open(log_path, "w") as logf:
        logf.write("$ " + " ".join(cmd) + "\n")
        logf.flush()
        proc = subprocess.run(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    # Exit 0 is clean and 1 is rule violations (the map is still emitted); 2+ is a failure.
    if proc.returncode not in (0, 1):
        # The analyzer may already have written the map; clear it.
        _clear_cached_map(out_path, model_cfg.name)
        raise FatalRtlBuddyError(
            f"cdc analysis {analysis.get_name()!r}: rtl-buddy-cdc "
            f"exited with code {proc.returncode}; see {log_path}"
        )
    if not out_path.is_file():
        raise FatalRtlBuddyError(
            f"cdc analysis {analysis.get_name()!r}: rtl-buddy-cdc "
            f"completed but produced no domain map at {out_path}; "
            f"see {log_path}"
        )
    return out_path
