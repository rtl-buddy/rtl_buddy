import json
import logging
import math
import os
import pprint
import re
from dataclasses import dataclass, field as dc_field, make_dataclass, replace
from enum import StrEnum
from typing import Any, Literal, NoReturn

import yaml
from serde import field, serde
from serde.compat import SerdeError
from .yaml_loader import config_from_data, load_yaml

from ..errors import FatalRtlBuddyError
from ..runner.xfail import compile_xfail_match
from ..logging_utils import log_event
from .blocks import BlockRef, BlockRefFile, load_block_refs
from .dispatch import (
    DispatchResourcesFile,
    JobResources,
    apply_resource_layers,
    validate_resources_block,
)
from .openroad_threads import validate_threads
from .pdk import PLACEMENT_SPEC, PlacementFile, validate_placement
from .pnr_platform import PnrRoutingLayersFile, PnrRunOverrides, read_run_overrides
from .pnr_signoff import SIGNOFF_SPEC
from .pnr_repair import (
    GLOBAL_ROUTE_REPAIR_SPEC,
    POST_ROUTE_REPAIR_SPEC,
    UNSET,
    PnrMode,
    read_global_route_repair,
    read_hold_repair_shorthand,
    read_mode,
    read_post_route_repair,
)
from .synth import SynthSuiteConfig
from .toolpath import resolve_tool_path
from .unknown_keys import KeySpec, find_unknown_entry_keys, reject_unknown_keys

logger = logging.getLogger(__name__)


class GdsMode(StrEnum):
    """How complete a KLayout stream-out must be.

    ``PREVIEW`` (default) keeps a layout with unresolved cells and reports them; ``STRICT`` refuses to publish it.
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
        # Anchor for relative `tool:` candidates: the root_config.yaml directory, not the cwd.
        self._base_dir = base_dir

    def get_name(self) -> str:
        return self._cfg.name

    def get_executable(self) -> str:
        """Return the effective tool executable (see :mod:`rtl_buddy.config.toolpath`)."""
        return resolve_tool_path(
            self._cfg.tool,
            base_dir=self._base_dir,
            block="cfg-pnr-tools",
            name=self._cfg.name,
            field="tool",
        )


class MacroAnchor(StrEnum):
    """The core corner the macro packer starts from; the opposite edges stay clear of macros."""

    LOWER_LEFT = "lower-left"
    LOWER_RIGHT = "lower-right"
    UPPER_LEFT = "upper-left"
    UPPER_RIGHT = "upper-right"


class MacroPlacement(StrEnum):
    """Who places the hard macros.

    ``PACK`` is rtl_buddy's shelf packer, steered by `macro-anchor` and hard blockages. ``RTL_MP`` uses OpenROAD's `rtl_macro_placer`.
    """

    PACK = "pack"
    RTL_MP = "rtl-mp"


class BlockageType(StrEnum):
    """Placement blockage kinds.

    ``HARD`` keeps every standard cell out and is also a macro-packer keep-out. ``SOFT`` excludes cells from global placement only. ``PARTIAL`` caps density at ``max-density``.
    """

    HARD = "hard"
    SOFT = "soft"
    PARTIAL = "partial"


class BlockageLifetime(StrEnum):
    """How long a placement blockage exists.

    ``FLOW`` blockages are created after the floorplan and stay. ``GLOBAL_PLACEMENT`` ones are created immediately before `global_placement` and destroyed immediately after it.
    """

    FLOW = "flow"
    GLOBAL_PLACEMENT = "global-placement"


@serde
class PnrBlockageFile:
    rect: list[float]
    type: str = BlockageType.HARD.value
    max_density: float | None = field(rename="max-density", default=None)
    lifetime: str = BlockageLifetime.FLOW.value


@dataclass(frozen=True)
class PnrBlockage:
    """One placement blockage: a die-coordinate rectangle in microns."""

    rect: tuple[float, float, float, float]
    type: BlockageType
    max_density: float | None = None
    lifetime: BlockageLifetime = BlockageLifetime.FLOW


class PinSide(StrEnum):
    """A die edge a group of IO pins is constrained to."""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


@serde
class PnrPinFile:
    # Port names or globs; one string or a list.
    names: str | list[str]
    side: str | None = None
    start: float | int | None = None
    end: float | int | None = None
    group: bool = False
    order: bool = False
    location: list[float | int] | None = None
    layer: str | None = None
    size: list[float | int] | None = None


@dataclass(frozen=True)
class PnrPin:
    """One `floorplan.pins` entry: a side/range/group constraint, or one pin at an exact location.

    `start` / `end` are die coordinates in microns along the edge (x for top and bottom, y for left and right). `location` is the pin centre in die microns; `layer` and `size` apply to it only.
    """

    names: tuple[str, ...]
    side: PinSide | None = None
    start: float | None = None
    end: float | None = None
    group: bool = False
    order: bool = False
    location: tuple[float, float] | None = None
    layer: str | None = None
    size: tuple[float, float] | None = None
    # Where the entry came from in `pnr.tcl` messages; `None` for an inline `floorplan.pins` entry.
    label: str | None = None
    # Coordinates are passed to OpenROAD at full precision instead of to the nanometre.
    exact: bool = False


#: OpenDB orientations that turn a macro by 90 degrees, swapping its width and height.
ROTATED_ORIENTATIONS = ("R90", "R270", "MXR90", "MYR90")
#: Orientations `floorplan.macros` accepts: all eight OpenDB orientations.
MACRO_ORIENTATIONS = ("R0", "R180", "MX", "MY", *ROTATED_ORIENTATIONS)


@serde
class PnrMacroFile:
    # An instance name, or a glob over instance names.
    instance: str
    location: list[float | int] | None = None
    orientation: str | None = None
    halo: list[float | int] | None = None


@dataclass(frozen=True)
class PnrMacro:
    """One `floorplan.macros` directive.

    `location` is the lower-left corner of the macro in die microns; it fixes the macro before the packer or RTL-MP runs. `halo` is the standard-cell keep-out ring in microns, overriding `placement.macro-cell-halo` for the macros `instance` matches.
    """

    instance: str
    location: tuple[float, float] | None = None
    orientation: str | None = None
    halo: tuple[float, float] | None = None
    # Where the entry came from in `pnr.tcl` messages; `None` for an inline `floorplan.macros` entry.
    label: str | None = None
    # Coordinates are passed to OpenROAD at full precision instead of to the nanometre.
    exact: bool = False


#: `floorplan` sizing defaults when neither `die-area` nor `core-area` is set.
DEFAULT_UTILIZATION = 0.55
DEFAULT_ASPECT = 1.0
DEFAULT_CORE_MARGIN = 2.0


@serde
class PnrFloorplanFile:
    # Unset means the default below; set together with `die-area` is an error.
    utilization: float | int | None = None
    aspect: float | int | None = None
    core_margin: float | int | None = field(rename="core-margin", default=None)
    die_area: list[float | int] | None = field(rename="die-area", default=None)
    core_area: list[float | int] | None = field(rename="core-area", default=None)
    core_cutouts: list[list[float | int]] = field(
        rename="core-cutouts", default_factory=list
    )
    macro_anchor: str = field(
        rename="macro-anchor", default=MacroAnchor.LOWER_LEFT.value
    )
    macro_placement: str = field(
        rename="macro-placement", default=MacroPlacement.PACK.value
    )
    blockages: list[PnrBlockageFile] = field(default_factory=list)
    pins: list[PnrPinFile] = field(default_factory=list)
    macros: list[PnrMacroFile] = field(default_factory=list)
    # Planner files, relative to pnr.yaml; read at run setup, not at load.
    macros_file: str | None = field(rename="macros-file", default=None)
    macro_snap: bool = field(rename="macro-snap", default=True)
    pins_file: str | None = field(rename="pins-file", default=None)
    pin_snap: bool = field(rename="pin-snap", default=True)
    pins_complete: bool = field(rename="pins-complete", default=False)


@dataclass
class PnrFloorplan:
    utilization: float
    aspect: float
    core_margin: float
    macro_anchor: MacroAnchor = MacroAnchor.LOWER_LEFT
    blockages: list[PnrBlockage] = dc_field(default_factory=list)
    macro_placement: MacroPlacement = MacroPlacement.PACK
    pins: list[PnrPin] = dc_field(default_factory=list)
    macros: list[PnrMacro] = dc_field(default_factory=list)
    # Explicit die and core rectangles in microns; when set, `utilization`, `aspect` and `core_margin` are unused.
    die_area: tuple[float, float, float, float] | None = None
    core_area: tuple[float, float, float, float] | None = None
    # Rectangles carved out of the core: hard blockages with their rows cut.
    core_cutouts: list[tuple[float, float, float, float]] = dc_field(
        default_factory=list
    )
    # Absolute planner-file paths; their entries join `macros` and `pins` at run setup (`with_planner_files`).
    macros_file: str | None = None
    pins_file: str | None = None
    # False places located macros at the exact DBU given, off the site and row grid.
    macro_snap: bool = True
    # False places exact pins at their exact centre, without `-force_to_die_boundary`.
    pin_snap: bool = True
    # Every signal port must be covered by exactly one exact entry or region rule.
    pins_complete: bool = False

    def flow_blockages(self) -> list["PnrBlockage"]:
        """The blockages that live for the whole flow."""
        return [b for b in self.blockages if b.lifetime is BlockageLifetime.FLOW]

    def global_placement_blockages(self) -> list["PnrBlockage"]:
        """The blockages that exist only during `global_placement`."""
        return [
            b for b in self.blockages if b.lifetime is BlockageLifetime.GLOBAL_PLACEMENT
        ]

    def ring_margin(self) -> float | None:
        """The narrowest core-to-die gap the floorplan asks for, in microns; None when only OpenROAD knows it."""
        if self.die_area is None or self.core_area is None:
            return self.core_margin
        dx0, dy0, dx1, dy1 = self.die_area
        cx0, cy0, cx1, cy1 = self.core_area
        return min(cx0 - dx0, cy0 - dy0, dx1 - cx1, dy1 - cy1)


_MIN_BLOCKAGE_SPAN = 0.001 - 1e-9


def _load_blockage(run: str, index: int, entry: PnrBlockageFile) -> PnrBlockage:
    """Validate one `floorplan.blockages` entry at load time."""
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
    # Coordinates are written to the nanometre; a narrower rect would have no area.
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
    try:
        lifetime = BlockageLifetime(entry.lifetime)
    except ValueError:
        raise FatalRtlBuddyError(
            f"{where}: unknown lifetime {entry.lifetime!r} "
            f"(expected one of {', '.join(t.value for t in BlockageLifetime)})"
        ) from None
    return PnrBlockage(
        rect=(x0, y0, x1, y1), type=kind, max_density=max_density, lifetime=lifetime
    )


def _finite_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _number_pair(where: str, key: str, value, what: str) -> tuple[float, float]:
    """Validate a two-number list such as `[x, y]`; `what` names its elements in the error."""
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(_finite_number(v) for v in value)
    ):
        raise FatalRtlBuddyError(
            f"{where}: {key} must be [{what}] in microns, got {value!r}"
        )
    return float(value[0]), float(value[1])


# What would end a braced Tcl word or list element in the generated script: whitespace, a brace, a quote, `$`, `;`, or a trailing backslash, which would escape the closing brace.
_TCL_UNSAFE = re.compile(r"[\s{}\"$;]|\\$")


def _tcl_safe_names(where: str, key: str, value) -> tuple[str, ...]:
    """Return a non-empty tuple of names from one string or a list, refusing Tcl-unsafe ones."""
    names = [value] if isinstance(value, str) else list(value or [])
    if not names:
        raise FatalRtlBuddyError(f"{where}: {key} must name at least one {key[:-1]}")
    for name in names:
        if not isinstance(name, str) or not name or _TCL_UNSAFE.search(name):
            raise FatalRtlBuddyError(
                f"{where}: {key} entry {name!r} must be a non-empty name or glob "
                "without whitespace, braces, quotes, '$', ';' or a trailing backslash"
            )
    return tuple(names)


def _load_pin(where: str, entry: PnrPinFile) -> PnrPin:
    """Validate one `floorplan.pins` or pins-file entry; `where` names it in errors."""
    names = _tcl_safe_names(where, "names", entry.names)
    if entry.location is not None:
        extra = [
            key
            for key, set_ in (
                ("side", entry.side is not None),
                ("start", entry.start is not None),
                ("end", entry.end is not None),
                ("group", entry.group),
                ("order", entry.order),
            )
            if set_
        ]
        if extra:
            raise FatalRtlBuddyError(
                f"{where}: location places one pin exactly and cannot be combined "
                f"with {', '.join(extra)}; use a separate entry"
            )
        if len(names) != 1:
            raise FatalRtlBuddyError(
                f"{where}: location places exactly one pin, but names lists "
                f"{len(names)}"
            )
        x, y = _number_pair(where, "location", entry.location, "x, y")
        if x < 0.0 or y < 0.0:
            raise FatalRtlBuddyError(
                f"{where}: location is in die coordinates, which start at 0, "
                f"got {entry.location!r}"
            )
        layer = entry.layer
        if layer is not None and (not layer or _TCL_UNSAFE.search(layer)):
            raise FatalRtlBuddyError(f"{where}: layer {layer!r} is not a layer name")
        size = None
        if entry.size is not None:
            size = _number_pair(where, "size", entry.size, "width, height")
            if size[0] <= 0.0 or size[1] <= 0.0:
                raise FatalRtlBuddyError(
                    f"{where}: size must be positive, got {entry.size!r}"
                )
        return PnrPin(names=names, location=(x, y), layer=layer, size=size)

    for key, value in (("layer", entry.layer), ("size", entry.size)):
        if value is not None:
            raise FatalRtlBuddyError(
                f"{where}: {key} applies to a pin placed at a location only"
            )
    side = None
    if entry.side is not None:
        try:
            side = PinSide(entry.side)
        except ValueError:
            raise FatalRtlBuddyError(
                f"{where}: unknown side {entry.side!r} "
                f"(expected one of {', '.join(s.value for s in PinSide)})"
            ) from None
    bounds = {}
    for key, value in (("start", entry.start), ("end", entry.end)):
        if value is None:
            continue
        if side is None:
            raise FatalRtlBuddyError(f"{where}: {key} needs a side")
        if not _finite_number(value) or value < 0:
            raise FatalRtlBuddyError(
                f"{where}: {key} must be a non-negative number of microns along "
                f"the edge, got {value!r}"
            )
        bounds[key] = float(value)
    if "start" in bounds and "end" in bounds and bounds["start"] >= bounds["end"]:
        raise FatalRtlBuddyError(
            f"{where}: start must be below end, got start {entry.start} and "
            f"end {entry.end}"
        )
    if entry.order and not entry.group:
        raise FatalRtlBuddyError(f"{where}: order needs group: true")
    if side is None and not entry.group:
        raise FatalRtlBuddyError(
            f"{where}: give a side, group: true, or a location — the entry "
            "constrains nothing"
        )
    return PnrPin(
        names=names,
        side=side,
        start=bounds.get("start"),
        end=bounds.get("end"),
        group=bool(entry.group),
        order=bool(entry.order),
    )


def _load_macro(
    where: str, entry: PnrMacroFile, placement: "MacroPlacement"
) -> PnrMacro:
    """Validate one `floorplan.macros` or macros-file entry; `where` names it in errors."""
    (instance,) = _tcl_safe_names(where, "instances", entry.instance)
    location = None
    if entry.location is not None:
        location = _number_pair(where, "location", entry.location, "x, y")
        if location[0] < 0.0 or location[1] < 0.0:
            raise FatalRtlBuddyError(
                f"{where}: location is in die coordinates, which start at 0, "
                f"got {entry.location!r}"
            )
    orientation = entry.orientation
    if orientation is not None:
        if orientation not in MACRO_ORIENTATIONS:
            raise FatalRtlBuddyError(
                f"{where}: unknown orientation {orientation!r} "
                f"(expected one of {', '.join(MACRO_ORIENTATIONS)})"
            )
        if location is None and placement is MacroPlacement.RTL_MP:
            raise FatalRtlBuddyError(
                f"{where}: 'macro-placement: rtl-mp' chooses the orientation of "
                "every macro it places; give this macro a location too, or use "
                "the packer"
            )
    halo = None
    if entry.halo is not None:
        halo = _number_pair(where, "halo", entry.halo, "x, y")
        if halo[0] < 0.0 or halo[1] < 0.0:
            raise FatalRtlBuddyError(
                f"{where}: halo must be non-negative, got {entry.halo!r}"
            )
    if location is None and orientation is None and halo is None:
        raise FatalRtlBuddyError(
            f"{where}: give a location, an orientation or a halo — the entry "
            "changes nothing"
        )
    return PnrMacro(
        instance=instance, location=location, orientation=orientation, halo=halo
    )


_MACRO_FILE_KEYS = ("instance", "location", "orientation", "halo")
_PIN_FILE_KEYS = (
    "name",
    "names",
    "side",
    "start",
    "end",
    "group",
    "order",
    "location",
    "layer",
    "size",
)


def _reject_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    duplicated = sorted({k for k in keys if keys.count(k) > 1})
    if duplicated:
        raise ValueError(f"duplicate key {duplicated[0]!r}")
    return dict(pairs)


def _read_planner_file(where: str, path: str) -> list:
    """Return the entry list a planner file holds; JSON is read directly, anything else as YAML."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError as e:
        raise FatalRtlBuddyError(f"{where}: cannot read {path}: {e.strerror or e}")
    data = None
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except ValueError:
            data = None
    if data is None:
        try:
            data = load_yaml(text, path, fast=True)
        except FatalRtlBuddyError as e:
            raise FatalRtlBuddyError(f"{where}: {e}") from None
        except yaml.YAMLError as e:
            raise FatalRtlBuddyError(
                f"{where}: {path} is not valid YAML: {e}"
            ) from None
    if data is None:
        return []
    if not isinstance(data, list):
        raise FatalRtlBuddyError(
            f"{where}: {path} must hold a list of entries, got {type(data).__name__}"
        )
    return data


def _file_entry_object(where: str, cls, entry: dict):
    """Build `cls` from one planner-file entry, reporting a wrongly typed value against `where`."""
    try:
        return cls(**entry)
    except SerdeError as e:
        match = re.search(r"parameter (\w+)=", str(e))
        key = match.group(1) if match else "an entry value"
        raise FatalRtlBuddyError(
            f"{where}: {key} has the wrong type, got {entry.get(key)!r}"
        ) from None


def _file_entry(where: str, entry, keys: tuple[str, ...]) -> dict:
    if not isinstance(entry, dict):
        raise FatalRtlBuddyError(f"{where}: an entry must be a mapping, got {entry!r}")
    unknown = [k for k in entry if k not in keys]
    if unknown:
        raise FatalRtlBuddyError(
            f"{where}: unknown key {unknown[0]!r} (expected {', '.join(keys)})"
        )
    return entry


def _load_macros_file(
    run: str, path: str, placement: "MacroPlacement"
) -> list[PnrMacro]:
    """Read and validate a `floorplan.macros-file`: entries with the `floorplan.macros` keys."""
    entries = _read_planner_file(f"pnr run '{run}': floorplan.macros-file", path)
    macros = []
    for i, raw in enumerate(entries):
        where = f"pnr run '{run}': floorplan.macros-file {path} [{i}]"
        entry = _file_entry(where, raw, _MACRO_FILE_KEYS)
        if "instance" not in entry:
            raise FatalRtlBuddyError(f"{where}: instance is required")
        macro = _load_macro(
            where, _file_entry_object(where, PnrMacroFile, entry), placement
        )
        macros.append(replace(macro, label=f"floorplan.macros-file[{i}]", exact=True))
    return macros


def _load_pins_file(run: str, path: str) -> list[PnrPin]:
    """Read and validate a `floorplan.pins-file`: exact `{name, location, layer, size}` pins and `floorplan.pins` entries."""
    entries = _read_planner_file(f"pnr run '{run}': floorplan.pins-file", path)
    pins = []
    for i, raw in enumerate(entries):
        where = f"pnr run '{run}': floorplan.pins-file {path} [{i}]"
        entry = dict(_file_entry(where, raw, _PIN_FILE_KEYS))
        if "name" in entry:
            if "names" in entry:
                raise FatalRtlBuddyError(f"{where}: give name or names, not both")
            if entry.get("location") is None:
                raise FatalRtlBuddyError(
                    f"{where}: name places one pin exactly and needs a location; "
                    "use names for a side or group rule"
                )
            entry["names"] = entry.pop("name")
        if "names" not in entry:
            raise FatalRtlBuddyError(f"{where}: name or names is required")
        pin = _load_pin(where, _file_entry_object(where, PnrPinFile, entry))
        pins.append(replace(pin, label=f"floorplan.pins-file[{i}]", exact=True))
    return pins


def with_planner_files(run: str, fp: PnrFloorplan) -> PnrFloorplan:
    """Return `fp` with its `macros-file` and `pins-file` entries after the inline ones.

    Read at run setup, not when `pnr.yaml` loads. Raises FatalRtlBuddyError naming the file and entry index.
    """
    macros = list(fp.macros)
    pins = list(fp.pins)
    if fp.macros_file is not None:
        macros += _load_macros_file(run, fp.macros_file, fp.macro_placement)
    if fp.pins_file is not None:
        pins += _load_pins_file(run, fp.pins_file)
    return replace(fp, macros=macros, pins=pins)


#: The flow's stages in order. A staged run (`stages: true`) runs each in its own OpenROAD session; `--stop-after`, `--resume` and `--from` name them. `post_route_repair` runs only when `post-route-repair` is set.
PNR_STAGES = (
    "floorplan",
    "place",
    "cts",
    "global_route",
    "detailed_route",
    "post_route_repair",
    "finish",
)

#: The stage a run has only when `post-route-repair` is set.
POST_ROUTE_REPAIR_STAGE = "post_route_repair"

#: Stages `checkpoints:` can name, in flow order: every stage but `finish`. Each is written when its stage finishes.
CHECKPOINT_STAGES = PNR_STAGES[:-1]


def _normalise_checkpoints(run: str, value) -> tuple[str, ...] | None:
    """Return the `checkpoints:` stages in flow order; ``None`` when off.

    ``true`` is every stage; ``false`` is off (no progress file). A name or list picks stages; an empty list keeps the progress file and manifest but writes no database.
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


def _load_constraints(run: str, value, config_dir: str) -> str | dict[str, str] | None:
    """Return `constraints:` with each path resolved against `config_dir`: one SDC path, or a mapping of corner name to SDC path in config order. Whether the mapping names the platform's corners is checked when the run starts (see `PnrConfig.constraints_problem`)."""
    if value is None:
        return None
    if isinstance(value, str):
        return os.path.normpath(os.path.join(config_dir, value))
    if not value:
        raise FatalRtlBuddyError(
            f"pnr run '{run}': constraints: a per-corner mapping must name "
            "each corner of the platform"
        )
    out: dict[str, str] = {}
    for corner, path in value.items():
        if not isinstance(corner, str) or not isinstance(path, str) or not path:
            raise FatalRtlBuddyError(
                f"pnr run '{run}': constraints: corner {corner!r} must map to "
                f"an SDC path, not {path!r}"
            )
        out[corner] = os.path.normpath(os.path.join(config_dir, path))
    return out


@serde
class PnrStageResourcesFile:
    """One stage job's reservation, `resources.stages.<stage>`: the `cfg-dispatch.resources` fields without `modes`, as a P&R stage has no builder mode."""

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None


@serde
class PnrResourcesFile:
    """A run's `resources:`: the reservation of each stage job under `--dispatch slurm`, with per-stage overrides under `stages`."""

    cpus: int | None = None
    mem: str | int | None = None
    time: str | int | None = None
    # Stage name -> a `PnrStageResourcesFile` block; typed loosely so `initialise` names a bad entry.
    stages: dict | None = None


@dataclass(frozen=True)
class PnrResources:
    """A run's validated `resources:` block: the run-wide reservation and each stage's override."""

    base: DispatchResourcesFile | None = None
    stages: dict[str, DispatchResourcesFile] = dc_field(default_factory=dict)


def _validated_block(block, where: str) -> DispatchResourcesFile:
    """One reservation block through the dispatch validators, with `cpus` a positive integer; errors name `where`."""
    try:
        valid = validate_resources_block(block, where=where)
    except FatalRtlBuddyError as e:
        if str(e).startswith(where):
            raise
        raise FatalRtlBuddyError(f"{where[:-1]}: {e}") from None
    cpus = valid.cpus
    if cpus is not None and (
        not isinstance(cpus, int) or isinstance(cpus, bool) or cpus < 1
    ):
        raise FatalRtlBuddyError(
            f"{where}cpus must be a positive integer, got {cpus!r}"
        )
    return valid


def _load_resources(run: str, entry: PnrResourcesFile | None) -> PnrResources | None:
    """Validate a run's `resources:` block at load time."""
    if entry is None:
        return None
    where = f"pnr run '{run}': resources."
    base = _validated_block(entry, where)
    if entry.stages is not None and not isinstance(entry.stages, dict):
        raise FatalRtlBuddyError(
            f"{where}stages must map stage names ({', '.join(PNR_STAGES)}) to "
            f"cpus/mem/time blocks, got {entry.stages!r}"
        )
    stages = {}
    for stage, raw in (entry.stages or {}).items():
        here = f"{where}stages.{stage}."
        if stage not in PNR_STAGES:
            raise FatalRtlBuddyError(
                f"{here[:-1]}: unknown stage (expected any of {', '.join(PNR_STAGES)})"
            )
        if not isinstance(raw, dict):
            raise FatalRtlBuddyError(
                f"{here[:-1]} must be a block of cpus, mem and time, got {raw!r}"
            )
        stages[stage] = _validated_block(
            config_from_data(PnrStageResourcesFile, raw), here
        )
    has_base = any(v is not None for v in (base.cpus, base.mem, base.time))
    return PnrResources(base=base if has_base else None, stages=stages)


#: The key check's view of `resources.stages`: one key per stage, each a reservation block.
_STAGE_RESOURCES_SPEC = KeySpec(
    make_dataclass(
        "PnrStageResourcesKeys", [(stage, Any, None) for stage in PNR_STAGES]
    ),
    {stage: KeySpec(PnrStageResourcesFile) for stage in PNR_STAGES},
)


#: Flow points `hooks:` can name, in flow order.
HOOK_POINTS = (
    "session",
    "after-floorplan",
    "macro-placement",
    "after-pdn",
    "after-pins",
    "after-global-placement",
    "after-repair-design",
    "after-cts",
    "after-global-route",
    "before-detailed-route",
    "before-reports",
)


def _normalise_hooks(run: str, value, config_dir: str) -> dict[str, tuple[str, ...]]:
    """Return the `hooks:` files by point, in flow order with absolute paths; a point with no file is dropped.

    A point takes one path or a list of paths, relative to `pnr.yaml`; null or an empty list is unset.
    """
    where = f"pnr run '{run}': hooks"
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FatalRtlBuddyError(
            f"{where} must map a flow point to a Tcl file or a list of Tcl files "
            f"(points: {', '.join(HOOK_POINTS)}), got {value!r}"
        )
    unknown = [k for k in value if k not in HOOK_POINTS]
    if unknown:
        raise FatalRtlBuddyError(
            f"{where} has unknown point(s) {', '.join(repr(k) for k in unknown)} "
            f"(valid points: {', '.join(HOOK_POINTS)})"
        )
    hooks: dict[str, tuple[str, ...]] = {}
    for point in HOOK_POINTS:
        if point not in value:
            continue
        entry = value[point]
        if entry is None:
            continue
        names = [entry] if isinstance(entry, str) else entry
        if not (
            isinstance(names, list) and all(isinstance(n, str) and n for n in names)
        ):
            raise FatalRtlBuddyError(
                f"{where}.{point} must be a Tcl file path or a list of non-empty "
                f"paths, got {entry!r}"
            )
        if names:
            hooks[point] = tuple(
                os.path.abspath(os.path.join(config_dir, n)) for n in names
            )
    return hooks


#: `detailed_route -verbose` level when a run sets none: iteration and violation counts, no per-net detail.
DEFAULT_DETAILED_ROUTE_VERBOSE = 1


def _validate_detailed_route_verbose(run: str, value) -> int:
    """Return the `detailed-route-verbose` level, default 1, or raise FatalRtlBuddyError.

    ``bool`` is refused because it is an ``int`` subclass.
    """
    if value is None:
        return DEFAULT_DETAILED_ROUTE_VERBOSE
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise FatalRtlBuddyError(
        f"pnr run '{run}': 'detailed-route-verbose' must be a non-negative "
        f"integer (the detailed_route -verbose level), got {value!r}"
    )


@serde
class PnrPdnRingFile:
    # [horizontal, vertical] routing layers.
    layers: list[str]
    width: float | int
    spacing: float | int
    offset: float | int


@serde
class PnrPdnStripeFile:
    layer: str
    width: float | int
    pitch: float | int | None = None
    offset: float | int | None = None
    spacing: float | int | None = None
    followpins: bool = False


@serde
class PnrPdnFile:
    ring: PnrPdnRingFile | None = None
    stripes: list[PnrPdnStripeFile] = field(default_factory=list)
    # Layer pairs to connect with vias; default: each stripe layer to the next.
    connect: list[list[str]] | None = None


@dataclass(frozen=True)
class PnrPdnRing:
    layers: tuple[str, str]
    width: float
    spacing: float
    offset: float


@dataclass(frozen=True)
class PnrPdnStripe:
    layer: str
    width: float
    pitch: float | None = None
    offset: float | None = None
    spacing: float | None = None
    followpins: bool = False


@dataclass(frozen=True)
class PnrPdn:
    """A run's declarative core power grid (`pdn:`).

    It replaces the core grid of the PDN Tcl (the PDK's `pdn-config` or the run's), whose global connections, voltage domains and macro grids still apply. `connect` is resolved: the default chain is already filled in.
    """

    stripes: tuple[PnrPdnStripe, ...]
    connect: tuple[tuple[str, str], ...]
    ring: PnrPdnRing | None = None


def _layer_name(where: str, value) -> str:
    if not isinstance(value, str) or not value or _TCL_UNSAFE.search(value):
        raise FatalRtlBuddyError(f"{where}: {value!r} is not a layer name")
    return value


def _positive(where: str, key: str, value, *, zero: bool = False) -> float:
    if not _finite_number(value) or value < 0 or (value == 0 and not zero):
        bound = "non-negative" if zero else "positive"
        raise FatalRtlBuddyError(
            f"{where}: {key} must be a {bound} number of microns, got {value!r}"
        )
    return float(value)


def _load_pdn(run: str, entry: PnrPdnFile, ring_margin: float | None) -> PnrPdn:
    """Validate a run's `pdn:` block at load time."""
    where = f"pnr run '{run}': pdn"
    ring = None
    if entry.ring is not None:
        here = f"{where}.ring"
        if not isinstance(entry.ring.layers, list) or len(entry.ring.layers) != 2:
            raise FatalRtlBuddyError(
                f"{here}: layers must be [horizontal, vertical], got "
                f"{entry.ring.layers!r}"
            )
        layers = tuple(_layer_name(here, v) for v in entry.ring.layers)
        ring = PnrPdnRing(
            layers=layers,
            width=_positive(here, "width", entry.ring.width),
            spacing=_positive(here, "spacing", entry.ring.spacing),
            offset=_positive(here, "offset", entry.ring.offset, zero=True),
        )
        extent = ring.offset + 2 * ring.width + ring.spacing
        if ring_margin is not None and extent > ring_margin + 1e-9:
            raise FatalRtlBuddyError(
                f"{here}: the ring needs {extent:g} um outside the core "
                "(offset + 2 x width + spacing), but the floorplan leaves "
                f"{ring_margin:g} um between core and die; widen core-margin "
                "or the die"
            )
    if not entry.stripes:
        raise FatalRtlBuddyError(
            f"{where}: stripes must list the core grid's stripes, including the "
            "followpins rails; the block replaces the pdn-config's core grid"
        )
    stripes = []
    for i, stripe in enumerate(entry.stripes):
        here = f"{where}.stripes[{i}]"
        layer = _layer_name(here, stripe.layer)
        width = _positive(here, "width", stripe.width)
        if stripe.followpins:
            extra = [
                key
                for key in ("pitch", "offset", "spacing")
                if getattr(stripe, key) is not None
            ]
            if extra:
                raise FatalRtlBuddyError(
                    f"{here}: followpins rails follow the rows, so "
                    f"{', '.join(extra)} does not apply"
                )
            stripes.append(PnrPdnStripe(layer=layer, width=width, followpins=True))
            continue
        if stripe.pitch is None:
            raise FatalRtlBuddyError(f"{here}: a stripe needs a pitch")
        pitch = _positive(here, "pitch", stripe.pitch)
        offset = (
            _positive(here, "offset", stripe.offset, zero=True)
            if stripe.offset is not None
            else None
        )
        spacing = (
            _positive(here, "spacing", stripe.spacing)
            if stripe.spacing is not None
            else None
        )
        if 2 * width + (spacing or 0.0) > pitch:
            raise FatalRtlBuddyError(
                f"{here}: a VDD and a VSS stripe ({2 * width + (spacing or 0.0):g} "
                f"um with spacing) do not fit in the {pitch:g} um pitch"
            )
        stripes.append(
            PnrPdnStripe(
                layer=layer, width=width, pitch=pitch, offset=offset, spacing=spacing
            )
        )
    if entry.connect is None:
        layers = list(dict.fromkeys(s.layer for s in stripes))
        connect = tuple(zip(layers, layers[1:]))
    else:
        connect = []
        for i, pair in enumerate(entry.connect):
            here = f"{where}.connect[{i}]"
            if not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]:
                raise FatalRtlBuddyError(
                    f"{here}: must be two different layers [lower, upper], got {pair!r}"
                )
            connect.append(tuple(_layer_name(here, v) for v in pair))
        connect = tuple(connect)
    return PnrPdn(stripes=tuple(stripes), connect=connect, ring=ring)


def _load_rect(where: str, value) -> tuple[float, float, float, float]:
    """Validate an `[x0, y0, x1, y1]` die-coordinate rectangle in microns."""
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(_finite_number(v) for v in value)
    ):
        raise FatalRtlBuddyError(
            f"{where} must be [x0, y0, x1, y1] in microns, got {value!r}"
        )
    x0, y0, x1, y1 = (float(v) for v in value)
    if round(x1, 3) - round(x0, 3) < _MIN_BLOCKAGE_SPAN or (
        round(y1, 3) - round(y0, 3) < _MIN_BLOCKAGE_SPAN
    ):
        raise FatalRtlBuddyError(
            f"{where} must have x0 < x1 and y0 < y1, at least 0.001 um apart, "
            f"got {value!r}"
        )
    if x0 < 0.0 or y0 < 0.0:
        raise FatalRtlBuddyError(
            f"{where} is in die coordinates, which start at 0, got {value!r}"
        )
    return x0, y0, x1, y1


def _inside(inner, outer) -> bool:
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _load_floorplan_shape(run: str, fp: PnrFloorplanFile) -> dict:
    """Validate the floorplan's size keys and core cut-outs; return the PnrFloorplan fields."""
    where = f"pnr run '{run}': floorplan"
    die = core = None
    if fp.die_area is not None or fp.core_area is not None:
        if fp.die_area is None or fp.core_area is None:
            raise FatalRtlBuddyError(
                f"{where}: die-area and core-area go together; set both"
            )
        sizing = [
            key
            for key, value in (
                ("utilization", fp.utilization),
                ("aspect", fp.aspect),
                ("core-margin", fp.core_margin),
            )
            if value is not None
        ]
        if sizing:
            raise FatalRtlBuddyError(
                f"{where}: die-area and core-area size the floorplan, so "
                f"{', '.join(sizing)} cannot be set with them"
            )
        die = _load_rect(f"{where}.die-area", fp.die_area)
        core = _load_rect(f"{where}.core-area", fp.core_area)
        if not _inside(core, die):
            raise FatalRtlBuddyError(
                f"{where}.core-area {list(core)} must lie inside die-area {list(die)}"
            )
    cutouts = []
    for i, rect in enumerate(fp.core_cutouts):
        cutout = _load_rect(f"{where}.core-cutouts[{i}]", rect)
        if core is not None and not _inside(cutout, core):
            raise FatalRtlBuddyError(
                f"{where}.core-cutouts[{i}] {list(cutout)} must lie inside "
                f"core-area {list(core)}"
            )
        cutouts.append(cutout)

    def _or_default(key, value, default):
        if value is None:
            return default
        if not _finite_number(value):
            raise FatalRtlBuddyError(f"{where}.{key} must be a number, got {value!r}")
        return float(value)

    return {
        "utilization": _or_default("utilization", fp.utilization, DEFAULT_UTILIZATION),
        "aspect": _or_default("aspect", fp.aspect, DEFAULT_ASPECT),
        "core_margin": _or_default("core-margin", fp.core_margin, DEFAULT_CORE_MARGIN),
        "die_area": die,
        "core_area": core,
        "core_cutouts": cutouts,
    }


#: `abstract.bloat` value for `write_abstract_lef -bloat_occupied_layers`, the default.
ABSTRACT_BLOAT_OCCUPIED = "occupied"


@serde
class PnrAbstractFile:
    """`abstract:` of a `harden: true` run: the obstruction style of the abstract LEF (#768)."""

    # `str` first so `occupied` is not read as a number.
    bloat: str | float | int | None = None
    # `str` first so a single name is not read as characters; it is then rejected.
    feedthrough_layers: str | list[str] | None = field(
        rename="feedthrough-layers", default=None
    )


@dataclass(frozen=True)
class PnrAbstract:
    """A hardened run's abstract LEF obstruction style.

    `bloat` is `occupied` (every layer the block uses is blocked over its whole footprint) or OpenROAD's `-bloat_factor`. `feedthrough_layers` are written with the block's real shapes as obstructions instead, so a parent can route between them.
    """

    bloat: str | float = ABSTRACT_BLOAT_OCCUPIED
    feedthrough_layers: tuple[str, ...] = ()

    def is_default(self) -> bool:
        return self.bloat == ABSTRACT_BLOAT_OCCUPIED and not self.feedthrough_layers


def _load_abstract(
    run: str, entry: PnrAbstractFile | None, harden: bool
) -> PnrAbstract:
    """Validate a run's `abstract:` block at load time."""
    if entry is None:
        return PnrAbstract()
    where = f"pnr run '{run}': abstract"
    if not harden:
        raise FatalRtlBuddyError(
            f"{where} needs harden: true; only a hardened run writes an abstract"
        )
    bloat = entry.bloat
    if bloat is None or bloat == ABSTRACT_BLOAT_OCCUPIED:
        bloat = ABSTRACT_BLOAT_OCCUPIED
    elif isinstance(bloat, bool) or not _finite_number(bloat) or bloat < 0:
        raise FatalRtlBuddyError(
            f"{where}.bloat must be {ABSTRACT_BLOAT_OCCUPIED!r} or a non-negative "
            f"number (OpenROAD's -bloat_factor), got {entry.bloat!r}"
        )
    else:
        bloat = float(bloat)
    layers = entry.feedthrough_layers
    if layers is None:
        layers = []
    if not isinstance(layers, list):
        raise FatalRtlBuddyError(
            f"{where}.feedthrough-layers must be a list of layer names, "
            f"got {layers!r}; write [{layers}]"
        )
    names = tuple(_layer_name(f"{where}.feedthrough-layers", v) for v in layers)
    if len(set(names)) != len(names):
        raise FatalRtlBuddyError(f"{where}.feedthrough-layers repeats a layer")
    return PnrAbstract(bloat=bloat, feedthrough_layers=names)


@serde
class PnrConfigFile:
    name: str
    desc: str
    tool: str = "openroad"
    synth: str = ""
    synth_path: str = field(rename="synth-path", default="")
    # One SDC path, or a mapping of corner name to SDC path; typed loosely so `initialise` names a bad entry.
    constraints: str | dict | None = None
    pin_constraints: str | None = field(rename="pin-constraints", default=None)
    # Replaces the PDK's `pdn-config` for this run; relative to pnr.yaml.
    pdn_config: str | None = field(rename="pdn-config", default=None)
    # Declarative core power grid, replacing the pdn-config's core grid.
    pdn: PnrPdnFile | None = None
    platform: str = ""
    floorplan: PnrFloorplanFile = field(default_factory=PnrFloorplanFile)
    lef_paths: list[str] = field(rename="lef-paths", default_factory=list)
    lib_paths: list[str] = field(rename="lib-paths", default_factory=list)
    # Layout for the `lef-paths` macros; used by KLayout stream-out only.
    gds_paths: list[str] = field(rename="gds-paths", default_factory=list)
    # `gds-allow-empty`: cells allowed to have no layout; names or case-sensitive fnmatch globs.
    gds_mode: str = field(rename="gds-mode", default=GdsMode.PREVIEW.value)
    gds_allow_empty: list[str] = field(rename="gds-allow-empty", default_factory=list)
    # `str` precedes the list so a single stage name is not read as characters.
    checkpoints: bool | str | list[str] = False
    # One OpenROAD session per stage, chained through the stage checkpoints.
    stages: bool = False
    # Tcl files sourced at fixed flow points: point -> a path or a list of paths. Typed loosely so `initialise` names a bad value.
    hooks: Any = None
    # Publish a hard-macro abstract (LEF, Liberty, GDS, manifest) under `abstract/`; forces a strict GDS export.
    harden: bool = False
    # Obstruction style of a hardened run's abstract LEF; unset keeps `-bloat_occupied_layers`.
    abstract: PnrAbstractFile | None = None
    # `buffer_ports -inputs -outputs` before global placement; unset follows `harden`.
    buffer_ports: bool | None = field(rename="buffer-ports", default=None)
    # Fail a routed run that has max-slew, max-capacitance or max-fanout violators.
    fail_on_electrical: bool = field(rename="fail-on-electrical", default=False)
    # Hardened blocks instanced as hard macros, each a `harden: true` run's abstract.
    blocks: list[BlockRefFile] = field(default_factory=list)
    # Overrides the platform's `placement:` keys field by field.
    placement: PlacementFile | None = None
    # Override the platform's repair blocks field by field; `null` turns one off. Read by `pnr_repair`.
    global_route_repair: Any = field(rename="global-route-repair", default=UNSET)
    post_route_repair: Any = field(rename="post-route-repair", default=UNSET)
    # Override the platform's keys of the same name; unset keeps the platform's. `routing-layers` goes field by field. Typed loosely where the validators name a bad value.
    routing_layers: PnrRoutingLayersFile | None = field(
        rename="routing-layers", default=None
    )
    routing_layer_adjustment: float | None = field(
        rename="routing-layer-adjustment", default=None
    )
    cts_buffer: str | list | None = field(rename="cts-buffer", default=None)
    cts_latency_balancing: bool | None = field(
        rename="cts-latency-balancing", default=None
    )
    cts_repair_clock_nets: bool | None = field(
        rename="cts-repair-clock-nets", default=None
    )
    post_cts_setup_repair: bool | None = field(
        rename="post-cts-setup-repair", default=None
    )
    global_route_hold_repair: bool | None = field(
        rename="global-route-hold-repair", default=None
    )
    port_buffer: str | list | None = field(rename="port-buffer", default=None)
    tapcell_tcl: str | None = field(rename="tapcell-tcl", default=None)
    # `clock_tree_synthesis -clk_nets`: the clock nets CTS builds trees for; run only.
    cts_clock_nets: str | list | None = field(rename="cts-clock-nets", default=None)
    # `closure` (default) or `probe`: no timing repair, stop after global route.
    mode: str | None = None
    # Overrides the platform's `signoff:` field by field; `null` turns it off. Read by `pnr_signoff`.
    signoff: Any = UNSET
    reglvl: int | dict | None = field(rename="reglvl", default=None)
    tool_overrides: dict | None = None
    # OpenROAD threads: a positive integer or `auto`; unset means single-threaded.
    threads: int | str | None = None
    # Each stage job's reservation under `--dispatch slurm`, over `cfg-dispatch.resources` field by field.
    resources: PnrResourcesFile | None = None
    # `detailed_route -verbose` level; unset means 1. Typed loosely so `initialise` names a bad value.
    detailed_route_verbose: int | float | str | None = field(
        rename="detailed-route-verbose", default=None
    )
    # Either flag marks the run expected-to-fail; `xfail_strict` fails on an unexpected pass. See docs/concepts/expected-failures.md.
    xfail: bool = False
    xfail_strict: bool = field(rename="xfail_strict", default=False)
    # Regex the failure must match to count as expected; needs `xfail`/`xfail_strict`. See docs/concepts/expected-failures.md.
    xfail_match: str | None = None

    def initialise(self, config_dir: str, suite_path: str | None = None) -> "PnrConfig":
        compile_xfail_match(
            self.xfail_match,
            xfail=self.xfail or self.xfail_strict,
            where=f"pnr {self.name!r}",
        )
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
        resources = _load_resources(self.name, self.resources)
        detailed_route_verbose = _validate_detailed_route_verbose(
            self.name, self.detailed_route_verbose
        )

        try:
            macro_anchor = MacroAnchor(self.floorplan.macro_anchor)
        except ValueError:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': unknown 'floorplan.macro-anchor' "
                f"{self.floorplan.macro_anchor!r} "
                f"(expected one of {', '.join(a.value for a in MacroAnchor)})"
            ) from None
        try:
            macro_placement = MacroPlacement(self.floorplan.macro_placement)
        except ValueError:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': unknown 'floorplan.macro-placement' "
                f"{self.floorplan.macro_placement!r} "
                f"(expected one of {', '.join(m.value for m in MacroPlacement)})"
            ) from None
        if (
            macro_placement is MacroPlacement.RTL_MP
            and macro_anchor is not MacroAnchor.LOWER_LEFT
        ):
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': 'floorplan.macro-anchor' steers the "
                "packer and has no effect with 'macro-placement: rtl-mp' — "
                "remove one of them"
            )
        blockages = [
            _load_blockage(self.name, i, entry)
            for i, entry in enumerate(self.floorplan.blockages)
        ]
        pins = [
            _load_pin(f"pnr run '{self.name}': floorplan.pins[{i}]", entry)
            for i, entry in enumerate(self.floorplan.pins)
        ]
        macros = [
            _load_macro(
                f"pnr run '{self.name}': floorplan.macros[{i}]", entry, macro_placement
            )
            for i, entry in enumerate(self.floorplan.macros)
        ]
        planner_files = {}
        for key, value in (
            ("macros-file", self.floorplan.macros_file),
            ("pins-file", self.floorplan.pins_file),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise FatalRtlBuddyError(
                    f"pnr run '{self.name}': floorplan.{key} must be a path to a "
                    f"YAML or JSON file, got {value!r}"
                )
            planner_files[key] = os.path.abspath(os.path.join(config_dir, value))
        placement = (
            validate_placement(self.placement, f"pnr run '{self.name}'")
            if self.placement is not None
            else None
        )
        global_route_repair = read_hold_repair_shorthand(
            f"pnr run '{self.name}'",
            self.global_route_hold_repair,
            read_global_route_repair(
                f"pnr run '{self.name}'", self.global_route_repair
            ),
        )
        overrides = read_run_overrides(
            f"pnr run '{self.name}'",
            routing_layers=self.routing_layers,
            cts_buffer=self.cts_buffer,
            cts_clock_nets=self.cts_clock_nets,
            cts_repair_clock_nets=self.cts_repair_clock_nets,
            cts_latency_balancing=self.cts_latency_balancing,
            post_cts_setup_repair=self.post_cts_setup_repair,
            routing_layer_adjustment=self.routing_layer_adjustment,
            port_buffer=self.port_buffer,
            tapcell_tcl=self.tapcell_tcl,
            config_dir=config_dir,
            signoff=self.signoff,
        )
        post_route_repair = read_post_route_repair(
            f"pnr run '{self.name}'", self.post_route_repair
        )
        mode = read_mode(f"pnr run '{self.name}'", self.mode)
        if mode is PnrMode.PROBE and self.harden:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': mode: probe stops after global route and "
                "publishes no abstract, so it cannot be harden: true"
            )
        shape = _load_floorplan_shape(self.name, self.floorplan)
        if self.pdn_config is not None and not self.pdn_config:
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': pdn-config must be a path to a Tcl file"
            )
        pdn = (
            _load_pdn(
                self.name,
                self.pdn,
                PnrFloorplan(**shape).ring_margin(),
            )
            if self.pdn is not None
            else None
        )
        checkpoints = _normalise_checkpoints(self.name, self.checkpoints)
        # Every stage but `post_route_repair`, which a run without that knob lacks, also counts as all.
        if (
            self.stages
            and checkpoints is not None
            and set(checkpoints) | {POST_ROUTE_REPAIR_STAGE} != set(CHECKPOINT_STAGES)
        ):
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': 'stages: true' writes every stage "
                "checkpoint; remove 'checkpoints' or set it to true"
            )
        hooks = _normalise_hooks(self.name, self.hooks, config_dir)
        if "macro-placement" in hooks:
            for key, is_default in (
                ("floorplan.macro-placement", macro_placement is MacroPlacement.PACK),
                ("floorplan.macro-anchor", macro_anchor is MacroAnchor.LOWER_LEFT),
            ):
                if not is_default:
                    raise FatalRtlBuddyError(
                        f"pnr run '{self.name}': '{key}' steers rb's macro placement "
                        "and has no effect with a 'macro-placement' hook — remove "
                        "one of them"
                    )

        blocks = load_block_refs(
            f"pnr run '{self.name}'",
            self.blocks,
            config_dir,
            default_pnr_path=(
                os.path.abspath(suite_path) if suite_path is not None else None
            ),
        )
        if suite_path is not None and any(
            b.pnr_run == self.name
            and os.path.abspath(b.pnr_suite_path) == os.path.abspath(suite_path)
            for b in blocks
        ):
            raise FatalRtlBuddyError(
                f"pnr run '{self.name}': lists itself under blocks"
            )

        synth_path_abs = os.path.normpath(os.path.join(config_dir, self.synth_path))
        constraints = _load_constraints(self.name, self.constraints, config_dir)
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
            pdn_config=(
                os.path.abspath(os.path.join(config_dir, self.pdn_config))
                if self.pdn_config is not None
                else None
            ),
            pdn=pdn,
            floorplan=PnrFloorplan(
                **shape,
                macro_anchor=macro_anchor,
                blockages=blockages,
                macro_placement=macro_placement,
                pins=pins,
                macros=macros,
                macros_file=planner_files.get("macros-file"),
                pins_file=planner_files.get("pins-file"),
                macro_snap=self.floorplan.macro_snap,
                pin_snap=self.floorplan.pin_snap,
                pins_complete=self.floorplan.pins_complete,
            ),
            placement=placement,
            global_route_repair=global_route_repair,
            post_route_repair=post_route_repair,
            overrides=overrides,
            mode=mode,
            lef_paths=lef_paths,
            lib_paths=lib_paths,
            gds_paths=gds_paths,
            gds_mode=gds_mode,
            gds_allow_empty=list(self.gds_allow_empty),
            checkpoints=CHECKPOINT_STAGES if self.stages else checkpoints,
            stages=bool(self.stages),
            hooks=hooks,
            harden=bool(self.harden),
            abstract=_load_abstract(self.name, self.abstract, bool(self.harden)),
            buffer_ports=(
                bool(self.buffer_ports) if self.buffer_ports is not None else None
            ),
            fail_on_electrical=bool(self.fail_on_electrical),
            blocks=blocks,
            _reglvl=self.reglvl,
            tool_overrides=self.tool_overrides,
            threads=threads,
            resources=resources,
            detailed_route_verbose=detailed_route_verbose,
            xfail=self.xfail,
            xfail_strict=self.xfail_strict,
            xfail_match=self.xfail_match,
        )


#: The keys of one `pnr.yaml` run and its nested blocks, for the unknown-key check. `hooks` names its own points in `_normalise_hooks`.
PNR_RUN_SPEC = KeySpec(
    PnrConfigFile,
    {
        "floorplan": KeySpec(
            PnrFloorplanFile,
            {
                "blockages": KeySpec(PnrBlockageFile, many=True),
                "pins": KeySpec(PnrPinFile, many=True),
                "macros": KeySpec(PnrMacroFile, many=True),
            },
        ),
        "pdn": KeySpec(
            PnrPdnFile,
            {
                "ring": KeySpec(PnrPdnRingFile),
                "stripes": KeySpec(PnrPdnStripeFile, many=True),
            },
        ),
        "abstract": KeySpec(PnrAbstractFile),
        "placement": PLACEMENT_SPEC,
        "global-route-repair": GLOBAL_ROUTE_REPAIR_SPEC,
        "post-route-repair": POST_ROUTE_REPAIR_SPEC,
        "routing-layers": KeySpec(PnrRoutingLayersFile),
        "blocks": KeySpec(BlockRefFile, many=True),
        "resources": KeySpec(PnrResourcesFile, {"stages": _STAGE_RESOURCES_SPEC}),
        "signoff": SIGNOFF_SPEC,
    },
)


@dataclass
class PnrConfig:
    name: str
    desc: str
    tool: str
    synth_name: str
    synth_suite_path: str
    # One SDC path, or corner name -> SDC path; absolute.
    constraints: str | dict[str, str] | None
    platform: str
    floorplan: PnrFloorplan
    _reglvl: int | dict | None
    tool_overrides: dict | None
    pin_constraints: str | None = None
    # The run's own `pdn-config`, overriding the PDK's; absolute.
    pdn_config: str | None = None
    pdn: PnrPdn | None = None
    lef_paths: list[str] = dc_field(default_factory=list)
    lib_paths: list[str] = dc_field(default_factory=list)
    gds_paths: list[str] = dc_field(default_factory=list)
    gds_mode: GdsMode = GdsMode.PREVIEW
    gds_allow_empty: list[str] = dc_field(default_factory=list)
    threads: int | str | None = None
    # The validated `resources:` block; None when unset.
    resources: PnrResources | None = None
    detailed_route_verbose: int = DEFAULT_DETAILED_ROUTE_VERBOSE
    checkpoints: tuple[str, ...] | None = None
    stages: bool = False
    # Absolute paths by flow point, in flow order; only points with a file.
    hooks: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    harden: bool = False
    abstract: PnrAbstract = dc_field(default_factory=PnrAbstract)
    # As written in pnr.yaml; `None` follows `harden`. Read it through `get_buffer_ports`.
    buffer_ports: bool | None = None
    fail_on_electrical: bool = False
    blocks: list[BlockRef] = dc_field(default_factory=list)
    # The run's validated `placement:` block, applied over the platform's; None when unset.
    placement: PlacementFile | None = None
    # The run's repair layers (see `pnr_repair`), applied over the platform's: `UNSET` when absent, `None` when off.
    global_route_repair: Any = UNSET
    post_route_repair: Any = UNSET
    # The run's routing, CTS, repair, tap-cell and signoff values; apply them with `PnrPlatformConfig.with_run_overrides`.
    overrides: PnrRunOverrides = PnrRunOverrides()
    mode: PnrMode = PnrMode.CLOSURE
    xfail: bool = False
    xfail_strict: bool = False
    xfail_match: str | None = None

    def is_xfail(self) -> bool:
        """Whether this run is expected to fail."""
        return self.xfail or self.xfail_strict

    def get_xfail_strict(self) -> bool:
        return self.xfail_strict

    def get_xfail_match(self) -> str | None:
        """The `xfail_match` regex a FAIL must match to count as expected, or None."""
        return self.xfail_match

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

    def get_constraints(self) -> str | dict[str, str] | None:
        """The SDC path, or a mapping of corner name to SDC path."""
        return self.constraints

    def constraints_problem(self, corners: list[str]) -> str | None:
        """Why a per-corner `constraints:` mapping does not fit a platform of `corners`, or None: it must name each corner and no other."""
        if not isinstance(self.constraints, dict):
            return None
        missing = [c for c in corners if c not in self.constraints]
        unknown = [c for c in self.constraints if c not in corners]
        problems = []
        if missing:
            problems.append(f"no SDC for corner(s) {', '.join(missing)}")
        if unknown:
            problems.append(
                f"corner(s) {', '.join(unknown)} not among the platform's "
                f"({', '.join(corners)})"
            )
        return "; ".join(problems) or None

    def constraints_by_corner(self, corners: list[str]) -> dict[str, str]:
        """Each of `corners`' SDC path, in `corners` order: the one path for every corner, or the mapping's entry. The mapping must fit (see `constraints_problem`)."""
        if isinstance(self.constraints, dict):
            return {c: self.constraints[c] for c in corners}
        return {c: self.constraints for c in corners}

    def get_platform(self) -> str:
        return self.platform

    def get_floorplan(self) -> PnrFloorplan:
        return self.floorplan

    def get_pdn_config(self) -> str | None:
        """The run's `pdn-config` override, or None to use the PDK's."""
        return self.pdn_config

    def get_pdn(self) -> PnrPdn | None:
        """The run's declarative core power grid, or None."""
        return self.pdn

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
        """Validated `threads:`: a positive int, `auto`, or None."""
        return self.threads

    def get_resources(self) -> PnrResources | None:
        """The run's validated `resources:` block, or None."""
        return self.resources

    def stage_resources(self, dispatch_cfg, stage: str) -> JobResources:
        """The reservation of `stage`'s job: built-in defaults, then `cfg-dispatch.resources`, the run's `resources:` and its `resources.stages.<stage>`, field by field.

        `modes:` blocks do not apply; a P&R stage has no builder mode.
        """
        own = self.resources or PnrResources()
        return apply_resource_layers(
            [
                dispatch_cfg.resources if dispatch_cfg is not None else None,
                own.base,
                own.stages.get(stage),
            ]
        )

    def get_detailed_route_verbose(self) -> int:
        """Validated `detailed-route-verbose:`, the `detailed_route -verbose` level."""
        return self.detailed_route_verbose

    def get_checkpoints(self) -> tuple[str, ...] | None:
        """The stages to checkpoint, in flow order; ``None`` when off."""
        return self.checkpoints

    def get_stages(self) -> bool:
        """Whether the run is staged: one OpenROAD session per stage."""
        return self.stages

    def get_hooks(self) -> dict[str, tuple[str, ...]]:
        """The run's Tcl hook files by flow point, in flow order; empty when none."""
        return {point: tuple(paths) for point, paths in self.hooks.items()}

    def get_harden(self) -> bool:
        """Whether the run publishes a hard-macro abstract."""
        return self.harden

    def get_abstract(self) -> PnrAbstract:
        """The abstract LEF obstruction style of a hardened run (`abstract:`)."""
        return self.abstract

    def get_buffer_ports(self) -> bool:
        """Whether the flow buffers the block's ports: the `buffer-ports:` value, else on exactly when the run hardens.

        A hardened block's pins are what its parent drives and is driven by, so they get buffers by default.
        """
        return self.harden if self.buffer_ports is None else self.buffer_ports

    def get_fail_on_electrical(self) -> bool:
        """Whether max-slew, max-capacitance or max-fanout violators fail the run."""
        return self.fail_on_electrical

    def get_blocks(self) -> list[BlockRef]:
        """The hardened blocks this run instances."""
        return list(self.blocks)

    def get_mode(self) -> PnrMode:
        """`closure`, or `probe`: no timing repair after CTS or global route, and the run stops after global route."""
        return self.mode

    def get_placement(self) -> PlacementFile | None:
        """The run's `placement:` overrides, or None; apply them with `PnrPlatformConfig.with_run_placement`."""
        return self.placement

    def get_overrides(self) -> PnrRunOverrides:
        """The run's own values of the platform's routing, CTS, repair and tap-cell knobs."""
        return self.overrides

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
                raw = load_yaml(f.read(), path)
        except Exception as e:
            self._raise_load_failed(e)
        # Before the typed load, which drops an unknown key and whose own errors a misspelt key can cause.
        reject_unknown_keys(
            find_unknown_entry_keys(
                raw.get("runs") if isinstance(raw, dict) else None,
                PNR_RUN_SPEC,
                kind="pnr run",
            ),
            path=path,
        )
        try:
            data = config_from_data(PnrSuiteConfigFile, raw)
        except Exception as e:
            self._raise_load_failed(e)

        config_dir = os.path.dirname(os.path.abspath(path))
        try:
            self.runs = {r.name: r.initialise(config_dir, path) for r in data.runs}
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

    def _raise_load_failed(self, error: Exception) -> NoReturn:
        log_event(
            logger,
            logging.ERROR,
            "pnr_suite_config.load_failed",
            path=self.path,
            error=error,
        )
        raise FatalRtlBuddyError(f'failed to load "{self.path}"') from error

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
