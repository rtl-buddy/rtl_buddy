"""On-demand domain_map.json generator for ``rb hub`` clock overlay.

When a ``ModelConfig.cdc`` back-pointer is set (#168 schema), the hub
invokes ``rtl-buddy-cdc lint --emit-domain-map ...`` to produce the
clock-domain map that ``rtl-buddy-view --cdc-annotations`` consumes.
The result is then baked into ``view.json`` and the SPA's clock
overlay toggle works without further configuration.

Two stages:

  1. Resolve the back-pointer (``models.yaml::entry.cdc``) → load
     the named ``cdc.yaml`` and pick the right analysis. The
     fragment (``cdc.yaml#analysis_name``) wins when present;
     otherwise we find the analysis whose ``model:`` field matches
     ``model_cfg.name``.
  2. Run ``rtl-buddy-cdc lint`` with ``--emit-domain-map`` plus the
     SDC + waivers from the analysis, writing the domain map into
     ``.rtl-buddy/cache/domain-<model>.json``. Lint output itself is
     discarded — the hub doesn't surface CDC violations, only the
     overlay.
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
from ..tools.vlog_filelist import VlogFilelist
from .view_builder import cache_dir

logger = logging.getLogger(__name__)


def domain_map_path(project_root: Path, model_name: str) -> Path:
    """Stable cache path for the model's domain map.

    Mirrors ``view_builder.view_json_path`` so the two cache files
    sit next to each other under ``.rtl-buddy/cache/``.
    """
    return cache_dir(project_root) / f"domain-{model_name}.json"


def _resolve_cdc_analysis(model_cfg: ModelConfig) -> CdcConfig | None:
    """Resolve ``model_cfg.cdc`` back-pointer to a ``CdcConfig``.

    Returns ``None`` when the back-pointer is unset (no overlay
    requested). Raises ``FatalRtlBuddyError`` when the back-pointer
    is set but the referenced file / analysis can't be loaded —
    failing loud at hub start beats a silent dark overlay.
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

    # If the back-pointer carried a ``#analysis_name`` fragment,
    # honour it verbatim — the model author picked one specific
    # analysis as canonical.
    if analysis_name is not None:
        analyses = suite.get_analyses(analysis_name)
        return analyses[0]

    # Otherwise pick the analysis whose ``model:`` field matches
    # this model's name. Ambiguity (multiple analyses for the same
    # model) is the user's bug — tell them to add a #fragment.
    matches = [a for a in suite.get_analyses() if a.get_top() == model_cfg.name]
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


# Filelist entries we drop from the rtl-buddy-cdc command line —
# CDC takes plain SystemVerilog source paths; ``-y`` / ``-F`` /
# ``+incdir+`` directives don't apply.
_FILELIST_SKIP_PREFIXES = ("+incdir+", "+libext+", "-y ", "-F ", "-f ")
_FILELIST_SOURCE_PREFIX = "-v "


def _source_files_from_filelist(fl_path: str) -> list[str]:
    """Same extraction logic as RtlBuddyCdc, inlined for the hub
    use case so we don't import a tool-internal helper."""
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


def build_domain_map(
    *,
    project_root: Path,
    model_cfg: ModelConfig,
) -> Path | None:
    """Generate the domain_map.json for ``model_cfg``'s clock overlay.

    Returns:
      - ``Path`` to the domain map when the model has a ``cdc:``
        back-pointer that resolves to a valid analysis.
      - ``None`` when the model has no ``cdc:`` field — overlay
        unavailable, caller should fall back to running
        rtl-buddy-view without ``--cdc-annotations``.

    Raises ``FatalRtlBuddyError`` when the back-pointer IS set but
    the resolution / lint subprocess fails — the user asked for the
    overlay and a dark toggle would be worse than a clear startup
    error.
    """
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
    out_path = domain_map_path(project_root, model_cfg.name)

    # Build a CDC-friendly filelist (unrolled + deduplicated). We
    # write into the same artefacts area RtlBuddyCdc uses so a
    # subsequent ``rb cdc`` invocation re-uses the elaboration.
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
    log_path = artefact_dir / "cdc.log"
    # ``--format json --output /dev/null`` keeps lint's chatter off
    # the hub log; we only care about the emitted domain map. The
    # ``--format text`` path is skipped — no human-facing lint
    # report is needed for the hub.
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
    # rtl-buddy-cdc returns 0 for clean, 1 for rule violations,
    # 2+ for elaboration failures. We tolerate violations because
    # they don't impede the overlay (the domain map still gets
    # emitted); we hard-fail on anything worse.
    if proc.returncode not in (0, 1):
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
