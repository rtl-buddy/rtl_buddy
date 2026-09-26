import logging
import math
import os
import pprint
from dataclasses import dataclass, field as dc_field
from enum import StrEnum
from typing import Literal

from serde import field, serde
from serde.yaml import from_yaml

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .openroad_threads import validate_threads
from .synth import SynthSuiteConfig
from .toolpath import resolve_tool_path

logger = logging.getLogger(__name__)


class GdsMode(StrEnum):
    """How complete a requested KLayout stream-out has to be (#619).

    ``PREVIEW`` keeps a layout whose cells could not all be resolved, and
    reports which ones; ``STRICT`` refuses to publish it. Preview is the
    default: a platform with LEF-only macros (ORFS `fakeram45`) streams a
    usable picture with those macros as empty placeholders, and the defect
    the mode exists to fix is claiming that picture is a *complete*
    stream-out, not producing it. A flow that signs off on the GDS asks
    for ``strict`` in `pnr.yaml`, or on the command line for one run.
    """

    STRICT = "strict"
    PREVIEW = "preview"


@serde
class PnrToolConfigFile:
    name: str
    tool: str | list[str]


class PnrToolConfig:
    def __init__(self, cfg: PnrToolConfigFile, base_dir: str | None = None):
        self._cfg = cfg
        # Directory relative `tool:` candidates are existence-tested
        # against: the one holding root_config.yaml, never the process
        # cwd (rb is routinely invoked from a suite directory).
        self._base_dir = base_dir

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        """Effective tool executable, with ``~`` / ``$VAR`` expanded.

        ``tool:`` may be a single value or a list of candidates in
        preference order; see :mod:`rtl_buddy.config.toolpath`.
        """
        return resolve_tool_path(
            self._cfg.tool,
            base_dir=self._base_dir,
            block="cfg-pnr-tools",
            name=self._cfg.name,
            field="tool",
        )


class MacroAnchor(StrEnum):
    """The core corner the macro packer starts from (#105).

    The packer fills rows away from this corner, so the edges opposite it
    stay clear of macros — where a design's IO pins, or its abutting
    neighbour, want the boundary free. ``LOWER_LEFT`` is the packing the
    flow has always done.
    """

    LOWER_LEFT = "lower-left"
    LOWER_RIGHT = "lower-right"
    UPPER_LEFT = "upper-left"
    UPPER_RIGHT = "upper-right"


class BlockageType(StrEnum):
    """Standard-cell placement blockage kinds, as OpenROAD has them (#105).

    ``HARD`` keeps every standard cell out, and the macro packer treats it
    as a keep-out too; ``SOFT`` keeps cells out of initial (global)
    placement only, so repair and legalization may still use it;
    ``PARTIAL`` caps the placement density inside it at ``max-density``.
    """

    HARD = "hard"
    SOFT = "soft"
    PARTIAL = "partial"


@serde
class PnrBlockageFile:
    rect: list[float]
    type: str = BlockageType.HARD.value
    max_density: float | None = field(rename="max-density", default=None)


@dataclass(frozen=True)
class PnrBlockage:
    """One placement blockage: a die-coordinate rectangle in microns."""

    rect: tuple[float, float, float, float]
    type: BlockageType
    max_density: float | None = None


@serde
class PnrFloorplanFile:
    utilization: float = 0.55
    aspect: float = 1.0
    core_margin: float = field(rename="core-margin", default=2.0)
    macro_anchor: str = field(
        rename="macro-anchor", default=MacroAnchor.LOWER_LEFT.value
    )
    blockages: list[PnrBlockageFile] = field(default_factory=list)


@dataclass
class PnrFloorplan:
    utilization: float
    aspect: float
    core_margin: float
    macro_anchor: MacroAnchor = MacroAnchor.LOWER_LEFT
    blockages: list[PnrBlockage] = dc_field(default_factory=list)


_MIN_BLOCKAGE_SPAN = 0.001 - 1e-9


def _load_blockage(run: str, index: int, entry: PnrBlockageFile) -> PnrBlockage:
    """Validate one `floorplan.blockages` entry; the geometry is checked here
    so a typo fails at load time rather than an hour into the flow."""
    where = f"pnr run '{run}': floorplan.blockages[{index}]"
    if len(entry.rect) != 4:
        raise FatalRtlBuddyError(
            f"{where}: rect must be [x0, y0, x1, y1] in microns, got {entry.rect!r}"
        )
    x0, y0, x1, y1 = (float(v) for v in entry.rect)
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        raise FatalRtlBuddyError(
            f"{where}: rect coordinates must be finite numbers, got {entry.rect!r}"
        )
    # The flow writes coordinates to the nanometre (`_tcl_microns`), so a
    # rectangle narrower than that would reach OpenROAD with no area at all.
    if round(x1, 3) - round(x0, 3) < _MIN_BLOCKAGE_SPAN or (
        round(y1, 3) - round(y0, 3) < _MIN_BLOCKAGE_SPAN
    ):
        raise FatalRtlBuddyError(
            f"{where}: rect must have x0 < x1 and y0 < y1, at least 0.001 um "
            f"apart, got {entry.rect!r}"
        )
    if not (x0 < x1 and y0 < y1):
        raise FatalRtlBuddyError(
            f"{where}: rect must have x0 < x1 and y0 < y1, got {entry.rect!r}"
        )
    # Die coordinates start at the origin: `initialize_floorplan` puts the
    # die's lower-left corner there.
    if x0 < 0.0 or y0 < 0.0:
        raise FatalRtlBuddyError(
            f"{where}: rect is in die coordinates, which start at 0, got {entry.rect!r}"
        )
    try:
        kind = BlockageType(entry.type)
    except ValueError:
        raise FatalRtlBuddyError(
            f"{where}: unknown type {entry.type!r} "
            f"(expected one of {', '.join(t.value for t in BlockageType)})"
        ) from None
    max_density = entry.max_density
    if kind is BlockageType.PARTIAL:
        if max_density is None:
            raise FatalRtlBuddyError(
                f"{where}: a partial blockage needs max-density (0 < max-density < 1)"
            )
        max_density = float(max_density)
        if not 0.0 < max_density < 1.0:
            raise FatalRtlBuddyError(
                f"{where}: max-density must be between 0 and 1, exclusive, got "
                f"{max_density} (use type: hard for 0; drop the blockage for 1)"
            )
    elif max_density is not None:
        raise FatalRtlBuddyError(
            f"{where}: max-density applies to partial blockages only, not {kind.value}"
        )
    return PnrBlockage(rect=(x0, y0, x1, y1), type=kind, max_density=max_density)


#: The stage checkpoints `checkpoints:` can ask for, in flow order (#653).
#: Each is written on the way *out* of the stage it names: `floorplan` holds
#: the floorplan, pins, tie cells, placed macros and PDN; `place` the
#: legalized global placement; `cts` the clock tree with hold repair
#: legalized; `global_route` a successful global route, with its guides and
#: segments. None of them is detail-routed, and none is a final output.
CHECKPOINT_STAGES = ("floorplan", "place", "cts", "global_route")


def _normalise_checkpoints(run: str, value) -> tuple[str, ...] | None:
    """`checkpoints:` as the stages to write, in flow order; ``None`` = off.

    ``true`` is every stage and ``false`` (the default) is none — and no
    progress file either, so a run that never sets the key renders the flow
    it always has. A name or a list of names picks stages; an empty list
    keeps the progress file and the manifest but writes no database.
    """
    if value is False or value is None:
        return None
    if value is True:
        return CHECKPOINT_STAGES
    names = [value] if isinstance(value, str) else list(value)
    unknown = [n for n in names if n not in CHECKPOINT_STAGES]
    if unknown:
        raise FatalRtlBuddyError(
            f"pnr run '{run}': unknown 'checkpoints' stage(s) "
            f"{', '.join(repr(n) for n in unknown)} "
            f"(expected true, false or any of {', '.join(CHECKPOINT_STAGES)})"
        )
    return tuple(s for s in CHECKPOINT_STAGES if s in names)


@serde
class PnrConfigFile:
    name: str
    desc: str
    tool: str = "openroad"
    synth: str = ""
    synth_path: str = field(rename="synth-path", default="")
    constraints: str | None = None
    pin_constraints: str | None = field(rename="pin-constraints", default=None)
    platform: str = ""
    floorplan: PnrFloorplanFile = field(default_factory=PnrFloorplanFile)
    lef_paths: list[str] = field(rename="lef-paths", default_factory=list)
    lib_paths: list[str] = field(rename="lib-paths", default_factory=list)
    # Layout for the macros `lef-paths` describes (e.g. an OpenRAM SRAM).
    # P&R never reads it; KLayout stream-out cannot do without it (#617).
    gds_paths: list[str] = field(rename="gds-paths", default_factory=list)
    # How complete the stream-out has to be, and which cells are allowed to
    # have no layout at all — a preview macro the design carries on purpose
    # (#619). Names or fnmatch globs; matched case-sensitively.
    gds_mode: str = field(rename="gds-mode", default=GdsMode.PREVIEW.value)
    gds_allow_empty: list[str] = field(rename="gds-allow-empty", default_factory=list)
    # Stage checkpoints + a progress file for long or failed runs (#653).
    # `str` sits before the list so a single stage name is not read as a
    # list of characters.
    checkpoints: bool | str | list[str] = False
    # Publish the routed result as a hard-macro abstract — LEF, Liberty
    # timing model, GDS and a fingerprint manifest under `abstract/` — for
    # a parent run to instance (#95). Forces a strict GDS export.
    harden: bool = False
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    # OpenROAD worker threads: a positive integer or `auto`; unset keeps
    # OpenROAD's single-thread default (#654). See config/openroad_threads.
    threads: int | str | None = None
    # Expected-fail markers (pytest-style). Either marks this run
    # expected-to-fail; `xfail` is non-strict (an unexpected pass still
    # passes), `xfail_strict` is strict (an unexpected pass is a failure).
    # See docs/concepts/expected-failures.md.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)

    def initialise(self, config_dir: str) -> "PnrConfig":
        if not self.synth:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': missing 'synth' (name of upstream rb synth entry)"
            )
        if not self.synth_path:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': missing 'synth-path' "
                "(path to the synth.yaml that defines the synth entry)"
            )
        if not self.platform:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': missing 'platform' "
                "(name of a cfg-pnr-platforms entry)"
            )
        try:
            gds_mode = GdsMode(self.gds_mode)
        except ValueError:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': unknown 'gds-mode' {self.gds_mode!r} "
                f"(expected one of {', '.join(m.value for m in GdsMode)})"
            ) from None
        threads = validate_threads(self.threads, where=f"pnr run '{self.name}'")

        try:
            macro_anchor = MacroAnchor(self.floorplan.macro_anchor)
        except ValueError:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': unknown 'floorplan.macro-anchor' "
                f"{self.floorplan.macro_anchor!r} "
                f"(expected one of {', '.join(a.value for a in MacroAnchor)})"
            ) from None
        blockages = [
            _load_blockage(self.name, i, entry)
            for i, entry in enumerate(self.floorplan.blockages)
        ]
        checkpoints = _normalise_checkpoints(self.name, self.checkpoints)

        synth_path_abs = os.path.normpath(os.path.join(config_dir, self.synth_path))
        constraints = (
            os.path.normpath(os.path.join(config_dir, self.constraints))
            if self.constraints is not None
            else None
        )
        lef_paths = [
            os.path.normpath(os.path.join(config_dir, p)) for p in self.lef_paths
        ]
        lib_paths = [
            os.path.normpath(os.path.join(config_dir, p)) for p in self.lib_paths
        ]
        gds_paths = [
            os.path.normpath(os.path.join(config_dir, p)) for p in self.gds_paths
        ]
        return PnrConfig(
            name=self.name,
            desc=self.desc,
            tool=self.tool,
            synth_name=self.synth,
            synth_suite_path=synth_path_abs,
            constraints=constraints,
            pin_constraints=(
                os.path.abspath(os.path.join(config_dir, self.pin_constraints))
                if self.pin_constraints is not None
                else None
            ),
            platform=self.platform,
            floorplan=PnrFloorplan(
                utilization=self.floorplan.utilization,
                aspect=self.floorplan.aspect,
                core_margin=self.floorplan.core_margin,
                macro_anchor=macro_anchor,
                blockages=blockages,
            ),
            lef_paths=lef_paths,
            lib_paths=lib_paths,
            gds_paths=gds_paths,
            gds_mode=gds_mode,
            gds_allow_empty=list(self.gds_allow_empty),
            checkpoints=checkpoints,
            harden=bool(self.harden),
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            threads=threads,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
        )


@dataclass
class PnrConfig:
    name: str
    desc: str
    tool: str
    synth_name: str
    synth_suite_path: str
    constraints: str | None
    platform: str
    floorplan: PnrFloorplan
    _reglvl: int | dict | None
    tool_overrides: dict | None
    pin_constraints: str | None = None
    lef_paths: list[str] = dc_field(default_factory=list)
    lib_paths: list[str] = dc_field(default_factory=list)
    gds_paths: list[str] = dc_field(default_factory=list)
    gds_mode: GdsMode = GdsMode.PREVIEW
    gds_allow_empty: list[str] = dc_field(default_factory=list)
    threads: int | str | None = None
    checkpoints: tuple[str, ...] | None = None
    harden: bool = False
    xfail: bool = False
    xfail_strict: bool = False

    def is_xfail(self) -> bool:
        """Whether this run is expected to fail (either flag set)."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_name(self) -> str:
        return self.name

    def get_desc(self) -> str:
        return self.desc

    def get_tool_name(self) -> str:
        return self.tool

    def get_synth_name(self) -> str:
        return self.synth_name

    def get_synth_suite_path(self) -> str:
        return self.synth_suite_path

    def get_constraints(self) -> str | None:
        return self.constraints

    def get_platform(self) -> str:
        return self.platform

    def get_floorplan(self) -> PnrFloorplan:
        return self.floorplan

    def get_lef_paths(self) -> list[str]:
        return list(self.lef_paths)

    def get_lib_paths(self) -> list[str]:
        return list(self.lib_paths)

    def get_gds_paths(self) -> list[str]:
        return list(self.gds_paths)

    def get_gds_mode(self) -> GdsMode:
        return self.gds_mode

    def get_gds_allow_empty(self) -> list[str]:
        return list(self.gds_allow_empty)

    def get_threads(self) -> int | str | None:
        """Validated `threads:` — a positive int, `auto`, or None (#654)."""
        return self.threads

    def get_checkpoints(self) -> tuple[str, ...] | None:
        """The stages to checkpoint, in flow order; ``None`` when off (#653)."""
        return self.checkpoints

    def get_harden(self) -> bool:
        """Whether the run publishes a hard-macro abstract (#95)."""
        return self.harden

    def get_reglvl(self, tool_name: str) -> int:
        match self._reglvl:
            case int() as lvl:
                return lvl
            case dict() if tool_name in self._reglvl:
                return self._reglvl[tool_name]
            case dict() if "default" in self._reglvl:
                return self._reglvl["default"]
            case None:
                return 0
            case _:
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr_config.reglvl_malformed",
                    pnr=self.name,
                    tool=tool_name,
                )
                raise FatalRtlBuddyError(
                    f"Malformed pnr.yaml, specify reglvl for {self.name} with {tool_name} or default"
                )

    def get_tool_overrides(self) -> dict | None:
        return self.tool_overrides

    def resolve_synth_cfg(self):
        """Load the upstream synth.yaml and return the referenced entry."""
        suite = SynthSuiteConfig(self.synth_suite_path)
        return suite.get_syntheses(self.synth_name)[0]

    def __str__(self):
        return pprint.pformat(self)


@serde
class PnrSuiteConfigFile:
    filetype: Literal["pnr_config"] = field(rename="rtl-buddy-filetype")
    runs: list[PnrConfigFile]


class PnrSuiteConfig:
    def __init__(self, path: str):
        self.path = path
        self.runs: dict[str, PnrConfig] = {}
        try:
            with open(path, "r") as f:
                data = from_yaml(PnrSuiteConfigFile, f.read())
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "pnr_suite_config.load_failed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f'failed to load "{path}"') from e

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.runs = {r.name: r.initialise(config_dir) for r in data.runs}
        except FatalRtlBuddyError:
            raise
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "pnr_suite_config.runs_malformed",
                path=path,
                error=e,
            )
            raise FatalRtlBuddyError(f"{path}: runs section malformed") from e

    def get_runs(self, name: str | None = None) -> list[PnrConfig]:
        if name is not None:
            if name not in self.runs:
                log_event(
                    logger,
                    logging.ERROR,
                    "pnr_suite_config.run_missing",
                    path=self.path,
                    run=name,
                )
                raise FatalRtlBuddyError(
                    f"pnr run '{name}' not found in suite {self.path}"
                )
            return [self.runs[name]]
        return list(self.runs.values())

    def get_run_names(self) -> list[str]:
        return list(self.runs.keys())

    def get_path(self) -> str:
        return self.path

    def __str__(self):
        return pprint.pformat(self)
