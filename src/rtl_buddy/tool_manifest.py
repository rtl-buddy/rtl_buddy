"""Declarative tool manifest for ``rb tool-check``.

This module is the single source of truth for "which external tools does
``rtl_buddy`` rely on, where do we look for them, and what versions do we
expect."  The :data:`MANIFEST` list is consumed by:

* ``rb tool-check`` — to report install status.
* per-tool subcommand wrappers — via :func:`require` to surface a uniform
  "tool missing" error message.
* future docs / setup-script generation.

The manifest is reconciled against a project's ``root_config.yaml`` when
one is available: ``cfg-verible``, ``cfg-surfer``, ``cfg-synth-tools``,
``cfg-pnr-tools``, ``cfg-power-tools``, ``cfg-cdc-tools``, and
``cfg-fpv-tools`` may pin the executable used at runtime, and the
optional ``cfg-tools`` block lets a project pin a stricter
``min-version`` than what rtl_buddy ships with.

Versions are not part of the manifest API — they are an output of
:func:`check_tool` after probing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Iterable

from .errors import FatalRtlBuddyError
from .logging_utils import log_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detectors


@dataclass
class DetectionResult:
    """Outcome of a single detector.

    Attributes:
      found: True if the detector located something usable.
      path: Filesystem path (binary or vendor dir entry). ``None`` for
        python packages.
      kind: One of ``"path"``, ``"vendor"``, ``"python"``. Used for the
        display string (``(python)`` vs. a path) and for downstream
        diagnostics.
      version: Pre-resolved version string (only python-package
        detectors fill this; binary detectors leave version probing to
        :func:`probe_version`).
    """

    found: bool
    path: str | None = None
    kind: str = "path"
    version: str | None = None


class Detector:
    """Strategy for locating one of a tool's binaries / packages."""

    def detect(
        self, spec: "ToolSpec", project_root: Path | None
    ) -> DetectionResult:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class PathDetector(Detector):
    """Look up a tool's binaries on ``$PATH`` via :func:`shutil.which`."""

    def detect(self, spec: "ToolSpec", project_root: Path | None) -> DetectionResult:
        for binary in spec.binaries:
            resolved = shutil.which(binary)
            if resolved:
                return DetectionResult(found=True, path=resolved, kind="path")
        return DetectionResult(found=False, kind="path")


@dataclass
class VendorDetector(Detector):
    """Look up a tool inside a project-relative or absolute vendor directory.

    The directory pattern mirrors what setup scripts write
    (e.g. ``vendor/surfer/bin/surfer``, ``tools/verible/macos/active/bin``).
    Either an absolute path (already resolved by ``RootConfig``) or one
    relative to ``project_root`` is accepted.
    """

    rel_path: str

    def detect(self, spec: "ToolSpec", project_root: Path | None) -> DetectionResult:
        base = Path(self.rel_path)
        if not base.is_absolute():
            if project_root is None:
                return DetectionResult(found=False, kind="vendor")
            base = (project_root / self.rel_path).resolve()
        for binary in spec.binaries:
            candidate = base / binary if base.is_dir() else base
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return DetectionResult(found=True, path=str(candidate), kind="vendor")
        return DetectionResult(found=False, kind="vendor")


@dataclass
class AbsolutePathDetector(Detector):
    """Detect a tool installed at a fully-qualified absolute path.

    Used when ``RootConfig`` already resolved a path for us (e.g. the
    ``cfg-surfer`` entry overrides PATH with an absolute Surfer binary).
    If the path points at a directory we look for ``spec.binaries[0]``
    inside it; otherwise we treat the path as the binary itself.
    """

    abs_path: str

    def detect(self, spec: "ToolSpec", project_root: Path | None) -> DetectionResult:
        p = Path(self.abs_path)
        if p.is_dir():
            for binary in spec.binaries:
                candidate = p / binary
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return DetectionResult(
                        found=True, path=str(candidate), kind="vendor"
                    )
        elif p.is_file() and os.access(p, os.X_OK):
            return DetectionResult(found=True, path=str(p), kind="vendor")
        return DetectionResult(found=False, kind="vendor")


@dataclass
class PythonPackageDetector(Detector):
    """Detect an installed Python package via ``importlib.metadata``."""

    package: str

    def detect(self, spec: "ToolSpec", project_root: Path | None) -> DetectionResult:
        try:
            ver = importlib_metadata.version(self.package)
        except importlib_metadata.PackageNotFoundError:
            return DetectionResult(found=False, kind="python")
        return DetectionResult(found=True, path=None, kind="python", version=ver)


@dataclass
class PythonSiblingDetector(Detector):
    """Detect a python-sibling tool by *both* PyPI metadata and PATH.

    Python siblings (``rtl-buddy-view``, ``rtl-buddy-cdc``,
    ``rtl-buddy-axi-profiler``) ship a wheel plus a script entry-point.
    Reporting "kind=python" alone hides where the binary actually lives;
    reporting PATH alone hides the version. This detector returns both
    when both are present, so the table shows ``version + path`` for the
    common "fully installed" case.
    """

    package: str

    def detect(self, spec: "ToolSpec", project_root: Path | None) -> DetectionResult:
        try:
            version = importlib_metadata.version(self.package)
        except importlib_metadata.PackageNotFoundError:
            version = None
        binary_path: str | None = None
        for binary in spec.binaries:
            resolved = shutil.which(binary)
            if resolved:
                binary_path = resolved
                break
        if version is None and binary_path is None:
            return DetectionResult(found=False, kind="python")
        # Prefer "path" kind when the binary is on PATH so the table shows
        # the absolute path; fall back to "python" when only the wheel is
        # installed (uncommon, but possible with `pip install --no-scripts`).
        kind = "path" if binary_path else "python"
        return DetectionResult(found=True, path=binary_path, kind=kind, version=version)


# ---------------------------------------------------------------------------
# ToolSpec


@dataclass
class ToolSpec:
    """Declarative description of a single tool dependency.

    Attributes:
      name: Canonical key (used for ``--explain``, JSON output, and
        ``require()``).
      binaries: Binary names to look for. The first one found wins. For
        Python packages this is typically a single human-readable name
        (e.g. ``("pyslang",)``) used only for display.
      version_cmd: Argv prefix that, when run, prints a version. ``None``
        skips probing for this tool.
      version_regex: Pattern applied to combined stdout+stderr of
        ``version_cmd``. The first match wins.
      minimum_version: Lower-bound version string. ``None`` means "any
        version is acceptable as long as the tool is present."
      detection: Ordered detectors. First ``found=True`` wins.
      install_hint: Per-platform install instructions for ``--explain``.
      used_by: Subcommands that this tool participates in. Drives the
        "Subcommand readiness" section of the report.
      optional: ``True`` means missing this tool does not gate
        subcommand readiness.
      description: Short one-liner shown by ``--explain``.
      notes: Free-form additional context for ``--explain``.
    """

    name: str
    binaries: tuple[str, ...]
    version_cmd: tuple[str, ...] | None
    version_regex: str | None
    minimum_version: str | None
    detection: tuple[Detector, ...]
    install_hint: dict[str, str] = field(default_factory=dict)
    used_by: tuple[str, ...] = ()
    optional: bool = False
    description: str = ""
    notes: str = ""


@dataclass
class ToolStatus:
    """Result of evaluating a single :class:`ToolSpec` against the env."""

    name: str
    status: str  # "ok" | "missing" | "outdated"
    version: str | None
    path: str | None
    optional: bool
    minimum_version: str | None
    kind: str | None  # "path" | "vendor" | "python" | None
    used_by: tuple[str, ...]


# ---------------------------------------------------------------------------
# Built-in manifest


def _builtin_manifest() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="verible",
            binaries=(
                "verible-verilog-syntax",
                "verible-verilog-lint",
                "verible-verilog-format",
            ),
            version_cmd=("verible-verilog-syntax", "--version"),
            version_regex=r"v\d+\.\d+-\d+",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install verible",
                "linux": "download release tarball from "
                "https://github.com/chipsalliance/verible/releases",
                "vendor": "extract into tools/verible/<os>/active/bin",
            },
            used_by=("hier", "verible"),
            optional=False,
            description="Verilog/SystemVerilog parser, linter, formatter",
        ),
        ToolSpec(
            name="yosys",
            binaries=("yosys",),
            version_cmd=("yosys", "-V"),
            version_regex=r"Yosys\s+([\w.+\-]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install yosys",
                "linux": "apt install yosys (Debian) or build from "
                "https://github.com/YosysHQ/yosys",
                "source": "https://github.com/rtl-buddy/yosys",
            },
            used_by=("synth", "synth-regression", "cdc", "cdc-regression"),
            optional=False,
            description="Yosys open-source synthesis framework",
        ),
        ToolSpec(
            name="verilator",
            binaries=("verilator",),
            version_cmd=("verilator", "--version"),
            version_regex=r"Verilator\s+([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install verilator",
                "linux": "apt install verilator (Ubuntu 22.04+: ≥ 5.0)",
                "source": "https://verilator.org/guide/latest/install.html",
            },
            used_by=("test", "randtest", "regression"),
            optional=False,
            description="Verilog/SystemVerilog simulator (used in --binary mode)",
            notes="rtl_buddy invokes Verilator in --binary mode; older 4.x "
            "will fail compile.",
        ),
        ToolSpec(
            name="surfer",
            binaries=("surfer",),
            version_cmd=("surfer", "--version"),
            version_regex=r"surfer\s+([\d.]+(?:[-+][\w.]+)?)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "source": "https://github.com/rtl-buddy/surfer (branch rtl-buddy)",
                "build": "cd ../surfer && cargo build --release",
            },
            used_by=("wave", "wave-fpv", "hub"),
            optional=False,
            description="Web-native waveform viewer",
        ),
        ToolSpec(
            name="gtkwave",
            binaries=("gtkwave", "vcd2fst"),
            version_cmd=("gtkwave", "--version"),
            version_regex=r"GTKWave Analyzer\s+v?([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install --cask gtkwave",
                "linux": "apt install gtkwave",
            },
            used_by=("wave-fpv", "axi-profile"),
            optional=True,
            description=(
                "Legacy waveform viewer (fallback for rb wave-fpv); its "
                "vcd2fst compacts converted VCS traces for rb axi-profile"
            ),
        ),
        ToolSpec(
            name="vpd2vcd",
            binaries=("vpd2vcd",),
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "any": (
                    "ships with Synopsys VCS — source your Synopsys "
                    "environment so vpd2vcd is on PATH"
                ),
            },
            used_by=("axi-profile",),
            optional=True,
            description=(
                "VPD-to-VCD converter (ships with VCS) — used by "
                "rb axi-profile run to ingest VCS $vcdpluson traces"
            ),
        ),
        ToolSpec(
            name="graphviz",
            binaries=("dot",),
            version_cmd=("dot", "-V"),
            version_regex=r"version\s+([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install graphviz",
                "linux": "apt install graphviz",
            },
            used_by=("hier",),
            optional=True,
            description="Graphviz layout engine — renders rb hier --format dot to SVG/PNG",
        ),
        ToolSpec(
            name="openroad",
            binaries=("openroad",),
            version_cmd=("openroad", "-version"),
            version_regex=r"([\d.]+(?:-[\w.]+)?)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "source": "https://github.com/The-OpenROAD-Project/OpenROAD",
                "macos": "brew install --cask openroad (community tap)",
            },
            used_by=("pnr", "power", "power-regression", "synth"),
            optional=True,
            description="OpenROAD place-and-route + STA engine",
        ),
        ToolSpec(
            name="klayout",
            binaries=("klayout",),
            version_cmd=("klayout", "-v"),
            version_regex=r"KLayout\s+([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install --cask klayout",
                "linux": "apt install klayout (Debian) or download from "
                "https://www.klayout.de",
            },
            used_by=("pnr",),
            optional=True,
            description="GDS viewer (optional, used by rb pnr to render layout)",
        ),
        ToolSpec(
            name="sby",
            binaries=("sby",),
            version_cmd=("sby", "--version"),
            version_regex=r"sby\s+([\w.+\-]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "source": "https://github.com/YosysHQ/sby",
                "linux": "apt install yosys-sby (Debian)",
            },
            used_by=("fpv", "fpv-regression"),
            optional=True,
            description="SymbiYosys formal property verification driver",
        ),
        ToolSpec(
            name="git",
            binaries=("git",),
            version_cmd=("git", "--version"),
            version_regex=r"git version\s+([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install git (or use Xcode CLT)",
                "linux": "apt install git",
            },
            used_by=(
                "test",
                "regression",
                "synth",
                "synth-regression",
                "cdc",
                "cdc-regression",
                "fpv",
                "fpv-regression",
                "wave",
                "hier",
            ),
            optional=False,
            description="Required for revision banners and regression reporting",
        ),
        ToolSpec(
            name="lcov",
            binaries=("genhtml",),
            version_cmd=("genhtml", "--version"),
            version_regex=r"genhtml:\s+LCOV version\s+([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "macos": "brew install lcov",
                "linux": "apt install lcov",
            },
            used_by=("test", "regression"),
            optional=True,
            description="genhtml — coverage HTML reports for Verilator runs",
        ),
        ToolSpec(
            name="info-process",
            binaries=("info-process",),
            # No --version flag in upstream info-process; skip the probe to
            # avoid printing whatever is on the first line of --help.
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "linux": "build from "
                "https://github.com/antmicro/coverview (info-process tool)",
            },
            used_by=("test", "regression"),
            optional=True,
            description="LCOV info-process — coverage merging for Coverview",
        ),
        ToolSpec(
            name="marimo",
            binaries=("marimo",),
            version_cmd=("marimo", "--version"),
            version_regex=r"([\d.]+)",
            minimum_version=None,
            detection=(PathDetector(),),
            install_hint={
                "any": "uv tool install marimo  (or pipx install marimo)",
            },
            used_by=("axi-profile",),
            optional=True,
            description="Marimo notebook launcher — used by rb axi-profile notebook",
        ),
        # ----- python siblings -----
        ToolSpec(
            name="rtl-buddy-view",
            binaries=("rtl-buddy-view",),
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PythonSiblingDetector("rtl-buddy-view"),),
            install_hint={
                "any": "uv tool install rtl-buddy-view  (or pip install rtl-buddy-view)",
            },
            used_by=("hier", "hub"),
            optional=False,
            description="Hierarchy viewer + JSON exporter for rtl_buddy",
        ),
        ToolSpec(
            name="rtl-buddy-cdc",
            binaries=("rtl-buddy-cdc",),
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PythonSiblingDetector("rtl-buddy-cdc"),),
            install_hint={
                "any": "uv tool install rtl-buddy-cdc  (or pip install rtl-buddy-cdc)",
            },
            used_by=("cdc", "cdc-regression", "hub"),
            optional=False,
            description="CDC lint analyzer used by rb cdc",
        ),
        ToolSpec(
            name="rtl-buddy-axi-profiler",
            binaries=("axi-profiler",),
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PythonSiblingDetector("rtl-buddy-axi-profiler"),),
            install_hint={
                "any": "uv tool install rtl-buddy-axi-profiler",
            },
            used_by=("axi-profile",),
            optional=False,
            description="AXI interconnect profiler used by rb axi-profile",
        ),
        # ----- python extras -----
        ToolSpec(
            name="pyslang",
            binaries=("pyslang",),
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PythonPackageDetector("pyslang"),),
            install_hint={
                "any": "uv pip install pyslang  (only needed for --frontend slang)",
            },
            used_by=("hier", "synth", "cdc"),
            optional=True,
            description="Python slang frontend — alternative parser for rb hier / synth / cdc",
        ),
        ToolSpec(
            name="cocotb",
            binaries=("cocotb",),
            version_cmd=None,
            version_regex=None,
            minimum_version=None,
            detection=(PythonPackageDetector("cocotb"),),
            install_hint={
                "any": "uv pip install cocotb  (only needed for cocotb-runner tests)",
            },
            used_by=("test", "regression"),
            optional=True,
            description="cocotb Python testbench runner",
        ),
        # ----- FPV solver engines (driven by sby) -----
        # Mirrors tools/fpv_solver_pin._PROBES — that module is the runtime
        # source of truth, this list keeps tool-check in sync. All optional:
        # rb fpv only needs the solvers listed in the active engines: line of
        # the user's fpv.yaml. Projects can pin exact versions through
        # cfg-fpv-tools[*].opts.solver-versions, which _reconcile_with_root_cfg
        # surfaces as minimum_version below.
        *_fpv_solver_specs(),
    ]


def _fpv_solver_specs() -> list[ToolSpec]:
    """Build solver ToolSpecs from the runtime probe table.

    Importing :data:`fpv_solver_pin._PROBES` directly means tool-check and
    the runtime pin check share the same binary / regex pair — adding a
    solver in one place updates the other.
    """
    from .tools.fpv_solver_pin import _PROBES

    specs: list[ToolSpec] = []
    for name, (binary, args, pattern) in _PROBES.items():
        specs.append(
            ToolSpec(
                name=name,
                binaries=(binary,),
                version_cmd=(binary, *args),
                version_regex=pattern,
                minimum_version=None,
                detection=(PathDetector(),),
                install_hint=_FPV_SOLVER_INSTALL_HINTS.get(name, {}),
                used_by=("fpv", "fpv-regression"),
                optional=True,
                description=_FPV_SOLVER_DESCRIPTIONS.get(name, f"FPV solver: {name}"),
                notes=(
                    "Pinned exactly via cfg-fpv-tools.opts.solver-versions at "
                    "runtime (`rb fpv` hard-fails on mismatch). tool-check "
                    "uses a ≥ comparison against any project pin."
                ),
            )
        )
    return specs


_FPV_SOLVER_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "yices": {
        "macos": "brew install SRI-CSL/sri-csl/yices2",
        "linux": "apt install yices2 (Debian) or build from "
        "https://github.com/SRI-CSL/yices2",
    },
    "z3": {
        "macos": "brew install z3",
        "linux": "apt install z3",
    },
    "boolector": {
        "source": "https://github.com/Boolector/boolector",
        "macos": "brew install boolector",
    },
    "bitwuzla": {
        "source": "https://github.com/bitwuzla/bitwuzla",
    },
    "btormc": {
        "source": "https://github.com/Boolector/boolector "
        "(builds btormc alongside boolector)",
    },
    "abc": {
        "any": "ships with yosys as `yosys-abc` — installing yosys is enough",
    },
}

_FPV_SOLVER_DESCRIPTIONS: dict[str, str] = {
    "yices": "Yices SMT solver — default smtbmc engine",
    "z3": "Z3 SMT solver",
    "boolector": "Boolector BV/QF_AUFBV solver",
    "bitwuzla": "Bitwuzla SMT solver (successor to Boolector)",
    "btormc": "BtorMC bounded model checker (btor2 backend)",
    "abc": "Yosys-ABC — combinational/sequential synthesis + BMC engine",
}


# ---------------------------------------------------------------------------
# Reconciliation with root_config.yaml


def _reconcile_with_root_cfg(specs: list[ToolSpec], root_cfg) -> list[ToolSpec]:
    """Apply root-config overrides to manifest defaults.

    Four kinds of override are honored:

    * ``cfg-verible`` — the active platform's verible directory is added
      to the verible spec's detector chain as the *preferred* lookup,
      with PATH retained as the fallback.
    * ``cfg-surfer`` — the ``surfer-default`` entry's resolved path is
      added to the surfer spec's detector chain in the same way.
    * ``cfg-tools`` — overrides ``minimum_version`` for any matching
      tool. Project pins always win over manifest defaults.
    * ``cfg-fpv-tools[*].opts.solver-versions`` — pins a project-wide
      version for each FPV solver. Runtime semantics is exact-equality
      (``rb fpv`` hard-fails on mismatch via
      :func:`fpv_solver_pin.check_solver_pins`); tool-check surfaces the
      pin as ``minimum_version`` so users see a single "outdated"
      indication for solvers that don't match.
    """
    if root_cfg is None:
        return specs

    by_name = {s.name: s for s in specs}

    # Verible vendor path
    try:
        verible_cfg = root_cfg.get_verible_cfg()
    except Exception:
        verible_cfg = None
    if verible_cfg is not None and verible_cfg.path:
        spec = by_name["verible"]
        by_name["verible"] = _replace(
            spec,
            detection=(AbsolutePathDetector(verible_cfg.path), *spec.detection),
        )

    # Surfer override path
    try:
        surfer_cfg = root_cfg.get_surfer_cfg()
    except Exception:
        surfer_cfg = None
    if surfer_cfg is not None:
        try:
            surfer_path = surfer_cfg.get_surfer_exe()
        except Exception:
            surfer_path = None
        if surfer_path and surfer_path != surfer_cfg.path:
            # An absolute path was resolved — prepend an AbsolutePathDetector
            spec = by_name["surfer"]
            by_name["surfer"] = _replace(
                spec,
                detection=(AbsolutePathDetector(surfer_path), *spec.detection),
            )

    # cfg-tools min-version pins
    for name, ver_cfg in getattr(root_cfg, "tool_version_cfgs", {}).items():
        if name not in by_name:
            log_event(
                logger,
                logging.DEBUG,
                "tool_manifest.unknown_pin",
                name=name,
                min_version=ver_cfg.min_version,
            )
            continue
        if ver_cfg.min_version:
            by_name[name] = _replace(by_name[name], minimum_version=ver_cfg.min_version)

    # cfg-fpv-tools[*].opts.solver-versions pins. Multiple fpv-tool
    # entries can each pin different solvers; later entries win for the
    # same solver — matches dict-merge semantics.
    fpv_pins: dict[str, str] = {}
    for tool_cfg in getattr(root_cfg, "fpv_tool_cfgs", {}).values():
        opts = tool_cfg.get_opts()
        for solver, version in opts.solver_versions.items():
            fpv_pins[solver] = version
    for solver, version in fpv_pins.items():
        if solver not in by_name:
            log_event(
                logger,
                logging.DEBUG,
                "tool_manifest.unknown_solver_pin",
                solver=solver,
                pinned=version,
            )
            continue
        by_name[solver] = _replace(by_name[solver], minimum_version=version)

    return list(by_name.values())


def _replace(spec: ToolSpec, **changes) -> ToolSpec:
    """:func:`dataclasses.replace` without importing it everywhere."""
    from dataclasses import replace

    return replace(spec, **changes)


def get_manifest(root_cfg=None) -> list[ToolSpec]:
    """Return the full tool manifest, optionally reconciled with ``root_cfg``."""
    return _reconcile_with_root_cfg(_builtin_manifest(), root_cfg)


# ---------------------------------------------------------------------------
# Version handling


_VERSION_TOKEN_RE = re.compile(r"\d+")


def _version_tuple(value: str) -> tuple[int, ...]:
    """Extract a tuple of ints from a version-looking string.

    ``v0.0-3724`` → ``(0, 0, 3724)``. Non-numeric tokens are ignored.
    Returns ``()`` if no digits found — used as a sentinel for
    incomparable strings.
    """
    return tuple(int(t) for t in _VERSION_TOKEN_RE.findall(value))


def _version_satisfies(actual: str | None, minimum: str | None) -> bool:
    """Tolerant ``actual >= minimum`` check.

    Strategy: extract integer tuples from each, lexicographically
    compare. If either side has no extractable digits, we err on the
    side of "satisfies" — the tool is present, after all, and a stricter
    comparator should be opt-in.
    """
    if minimum is None:
        return True
    if actual is None:
        # No version captured but a minimum was requested — can't prove
        # satisfaction. Treat as outdated so the user investigates.
        return False
    a, m = _version_tuple(actual), _version_tuple(minimum)
    if not a or not m:
        return True
    return a >= m


# ---------------------------------------------------------------------------
# Version cache (~/.cache/rtl_buddy/tool_versions.json)


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "rtl_buddy" / "tool_versions.json"


def _load_cache() -> dict:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    except OSError:
        log_event(
            logger,
            logging.DEBUG,
            "tool_manifest.cache_write_failed",
            path=str(path),
        )


def probe_version(
    spec: ToolSpec, binary_path: str | None, cache: dict | None = None
) -> str | None:
    """Run ``spec.version_cmd`` and return the parsed version string.

    ``binary_path`` is substituted as ``argv[0]`` when set so vendor
    binaries are probed instead of whatever PATH would resolve. Results
    are cached keyed by ``(binary_path, mtime)`` in
    ``~/.cache/rtl_buddy/tool_versions.json`` so repeated calls don't
    re-fork.
    """
    if spec.version_cmd is None or spec.version_regex is None:
        return None

    cmd = list(spec.version_cmd)
    if binary_path:
        cmd[0] = binary_path

    cache_key = None
    if cache is not None and binary_path:
        try:
            mtime = os.path.getmtime(binary_path)
            cache_key = f"{binary_path}@{int(mtime)}"
            if cache_key in cache:
                cached = cache[cache_key]
                if cached.get("regex") == spec.version_regex:
                    return cached.get("version")
        except OSError:
            cache_key = None

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(spec.version_regex, blob)
    version = None
    if match:
        # Prefer first capture group if present, else full match.
        version = match.group(1) if match.groups() else match.group(0)

    if cache is not None and cache_key is not None:
        cache[cache_key] = {"regex": spec.version_regex, "version": version}

    return version


# ---------------------------------------------------------------------------
# Detection + status


def detect_tool(spec: ToolSpec, project_root: Path | None = None) -> DetectionResult:
    """Run detectors in order, returning the first hit (or last miss)."""
    last = DetectionResult(found=False)
    for det in spec.detection:
        result = det.detect(spec, project_root)
        if result.found:
            return result
        last = result
    return last


def check_tool(
    spec: ToolSpec,
    *,
    project_root: Path | None = None,
    probe_versions: bool = True,
    cache: dict | None = None,
) -> ToolStatus:
    """Resolve detection + optional version probe for one ToolSpec."""
    det = detect_tool(spec, project_root=project_root)

    if not det.found:
        return ToolStatus(
            name=spec.name,
            status="missing",
            version=None,
            path=None,
            optional=spec.optional,
            minimum_version=spec.minimum_version,
            kind=None,
            used_by=spec.used_by,
        )

    version = det.version
    if version is None and probe_versions and det.kind != "python":
        version = probe_version(spec, det.path, cache=cache)

    status = "ok"
    if not _version_satisfies(version, spec.minimum_version):
        status = "outdated"

    return ToolStatus(
        name=spec.name,
        status=status,
        version=version,
        path=det.path,
        optional=spec.optional,
        minimum_version=spec.minimum_version,
        kind=det.kind,
        used_by=spec.used_by,
    )


def check_all(
    specs: Iterable[ToolSpec],
    *,
    project_root: Path | None = None,
    probe_versions: bool = True,
    include_optional: bool = True,
) -> list[ToolStatus]:
    cache = _load_cache() if probe_versions else None
    statuses: list[ToolStatus] = []
    for spec in specs:
        if not include_optional and spec.optional:
            continue
        statuses.append(
            check_tool(
                spec,
                project_root=project_root,
                probe_versions=probe_versions,
                cache=cache,
            )
        )
    if cache is not None:
        _save_cache(cache)
    return statuses


def subcommand_readiness(
    statuses: list[ToolStatus],
    specs: Iterable[ToolSpec],
) -> dict[str, dict]:
    """Group tool statuses by the subcommands they gate.

    Each subcommand entry has:
        ``status`` — overall ``ok`` / ``outdated`` / ``missing``
        ``missing`` — list of required tools that are absent
        ``outdated`` — list of required tools that are too old
        ``optional_feature`` — True iff *all* gating tools are optional
            (i.e. the subcommand only runs when the user opts in)
    """
    by_name = {s.name: s for s in statuses}

    subcommands: dict[str, dict] = {}
    for spec in specs:
        for sub in spec.used_by:
            slot = subcommands.setdefault(
                sub,
                {
                    "missing": [],
                    "outdated": [],
                    "tools": [],
                    "optional_only": True,
                },
            )
            slot["tools"].append(spec.name)
            if not spec.optional:
                slot["optional_only"] = False
            st = by_name.get(spec.name)
            if st is None:
                continue
            if st.status == "missing" and not spec.optional:
                slot["missing"].append(spec.name)
            elif st.status == "outdated" and not spec.optional:
                slot["outdated"].append(spec.name)

    out: dict[str, dict] = {}
    for sub, slot in sorted(subcommands.items()):
        status = "ok"
        if slot["missing"]:
            status = "missing"
        elif slot["outdated"]:
            status = "outdated"
        out[sub] = {
            "status": status,
            "missing": slot["missing"],
            "outdated": slot["outdated"],
            "tools": slot["tools"],
            "optional_feature": slot["optional_only"],
        }
    return out


# ---------------------------------------------------------------------------
# Public helpers used by subcommand wrappers


def require(name: str, root_cfg=None) -> ToolStatus:
    """Assert that ``name`` is installed (and not outdated), else raise.

    Subcommand entry points may call this to surface a uniform
    "missing tool" error pointing the user at ``rb tool-check --explain``.
    """
    spec = next((s for s in get_manifest(root_cfg) if s.name == name), None)
    if spec is None:
        raise FatalRtlBuddyError(f"tool_manifest: unknown tool '{name}'")
    status = check_tool(spec)
    if status.status == "missing":
        raise FatalRtlBuddyError(
            f"{name} not found — run `rb tool-check --explain {name}` "
            "for install instructions"
        )
    if status.status == "outdated":
        raise FatalRtlBuddyError(
            f"{name} {status.version} is older than the required minimum "
            f"{spec.minimum_version} — run `rb tool-check --explain {name}` "
            "for upgrade instructions"
        )
    return status


def explain(spec: ToolSpec, status: ToolStatus | None = None) -> str:
    """Render a multi-line, human-readable description for ``--explain``."""
    lines: list[str] = []
    headline = spec.name
    if spec.description:
        headline = f"{headline} — {spec.description}"
    lines.append(headline)
    if status is not None:
        lines.append(f"  Status:  {status.status}")
        if status.version:
            lines.append(f"  Version: {status.version}")
        if status.path:
            lines.append(f"  Path:    {status.path}")
    if spec.used_by:
        lines.append(f"  Used by: {', '.join(f'rb {s}' for s in spec.used_by)}")
    if spec.install_hint:
        lines.append("  Install:")
        for platform, hint in spec.install_hint.items():
            lines.append(f"    {platform:8s} {hint}")
    if spec.minimum_version:
        lines.append(f"  Minimum version: {spec.minimum_version}")
    if spec.optional:
        lines.append("  Optional: yes (subcommands using it are opt-in)")
    if spec.notes:
        lines.append(f"  Notes: {spec.notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering


def _status_glyph(status: str) -> str:
    # ASCII only — see issue #204: must stay grep-friendly for CI logs.
    return status


def render_text(
    statuses: list[ToolStatus],
    subcommands: dict[str, dict],
    *,
    include_optional: bool = True,
) -> str:
    ok = sum(1 for s in statuses if s.status == "ok")
    missing = sum(1 for s in statuses if s.status == "missing" and not s.optional)
    outdated = sum(1 for s in statuses if s.status == "outdated" and not s.optional)
    header = f"Tools ({ok} ok, {missing} missing, {outdated} outdated)\n" + ("-" * 70)

    name_w = max(20, max((len(s.name) for s in statuses), default=0) + 2)
    rows: list[str] = []
    rows.append(f"{'Tool':<{name_w}}{'Status':12}{'Version':14}Path")
    for st in statuses:
        path_display = st.path or "—"
        if st.kind == "python":
            path_display = "(python)"
        version = st.version or "—"
        suffix = "  (optional)" if st.optional else ""
        if st.status == "outdated" and st.minimum_version:
            suffix = f"  (need ≥ {st.minimum_version})" + suffix
        rows.append(
            f"{st.name:<{name_w}}{_status_glyph(st.status):12}{version:14}"
            f"{path_display}{suffix}"
        )

    sub_lines: list[str] = []
    sub_lines.append("\nSubcommand readiness")
    sub_lines.append("-" * 70)
    for sub, info in subcommands.items():
        gloss_parts = []
        if info["missing"]:
            gloss_parts.append(f"needs: {', '.join(info['missing'])}")
        if info["outdated"]:
            gloss_parts.append(f"outdated: {', '.join(info['outdated'])}")
        if not gloss_parts:
            gloss_parts.append(", ".join(info["tools"]) or "no external deps")
        opt = "  (optional feature)" if info["optional_feature"] else ""
        sub_lines.append(
            f"  {_status_glyph(info['status']):9} rb {sub:20} ({gloss_parts[0]}){opt}"
        )

    hint = "\nHint: `rb tool-check --explain <tool>` for install instructions."
    return "\n".join([header, *rows, *sub_lines, hint])


def render_json(
    statuses: list[ToolStatus],
    subcommands: dict[str, dict],
    *,
    exit_code: int,
) -> str:
    tools_out: dict[str, dict] = {}
    for st in statuses:
        entry: dict = {
            "status": st.status,
            "version": st.version,
            "path": st.path,
            "optional": st.optional,
        }
        if st.minimum_version:
            entry["minimum_version"] = st.minimum_version
        tools_out[st.name] = entry

    subs_out: dict[str, dict] = {}
    for sub, info in subcommands.items():
        entry = {
            "status": info["status"],
            "missing": info["missing"],
            "outdated": info["outdated"],
        }
        if info["optional_feature"]:
            entry["optional_feature"] = True
        subs_out[sub] = entry

    return json.dumps(
        {"tools": tools_out, "subcommands": subs_out, "exit_code": exit_code},
        indent=2,
        sort_keys=False,
    )


def compute_exit_code(
    statuses: list[ToolStatus],
    *,
    required_for: str | None = None,
    subcommands: dict[str, dict] | None = None,
) -> int:
    """Map a checked manifest to a process exit code.

    Mapping (per issue #204):
        0 — all required tools present and up-to-date.
        1 — at least one required tool missing/outdated.
        2 — ``--required-for <sub>`` and that sub's deps are missing.
    """
    if required_for is not None and subcommands is not None:
        info = subcommands.get(required_for)
        if info is None:
            return 1
        if info["status"] != "ok":
            return 2
        return 0
    for st in statuses:
        if st.optional:
            continue
        if st.status != "ok":
            return 1
    return 0
