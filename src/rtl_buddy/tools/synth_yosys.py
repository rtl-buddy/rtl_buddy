import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .artifact_paths import clear_stale_artefacts
from .liberty_units import LibertyTimeUnitError, liberty_time_unit_ps
from .vlog_filelist import VlogFilelist, incdirs_from_filelist
from .sv_lifetime_scan import LifetimeFinding, describe_findings, scan_files
from ..config.synth import (
    SynthConfig,
    SynthToolConfig,
    SynthToolOpts,
    SynthEffortConfig,
    default_effort_config,
    resolve_conflicting_drivers_mode,
    resolve_static_functions_mode,
    resolve_unresolved_interfaces_mode,
)
from ..constraints.tcl_reader import read_commands
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event, task_status
from ..phys.manifest import project_relative
from ..phys.publish import (
    confirm_digest,
    invalidate_half,
    publish_synth,
    sha256_of,
    withdrawal_failure_desc,
)
from ..process_utils import run_managed_process
from ..runner.synth_results import SynthFailResults, SynthPassResults, SynthResults

# Yosys' default Liberty script without `dc2`, which rebuilds log-depth carry networks as ripple chains.
DEFAULT_MAPPED_ABC_SCRIPT = (
    "strash; &get -n; &fraig -x; &put; scorr; dretime; strash; "
    "&get -n; &dch -f; &nf {D}; &put"
)
# Appended when the SDC names a clock; `_parse_critical_path_ps` reads its report.
_ABC_STIME = "; stime -p"


def _flag_value(words: list[str], flag: str) -> str | None:
    """Return the word after ``flag`` in a tokenized command, or None."""
    for i, word in enumerate(words):
        if word == flag and i + 1 < len(words):
            return words[i + 1]
    return None


# Maximum findings per machine-log line; the total and dropped counts are logged alongside.
MAX_EVENT_FINDINGS = 25


# Yosys `check` reports `Warning: multiple conflicting drivers for <mod>.<sig> [n]:`.
# The "Warning: " anchor avoids matching help text or echoed commands. The sibling
# `Drivers conflicting with a constant` message is a different condition and is not matched.
_CONFLICTING_DRIVERS_RE = re.compile(
    r"^(?:\S+:\d+:\s*)?Warning:\s*multiple conflicting drivers\b"
)

# Indented driver lines follow each warning: `port <P>[n] of cell <name> (<type>)`,
# `module input <w>[n]`, or `action <lhs> <= <rhs> (... rule) in process <p>`.
_TRISTATE_DRIVER_RE = re.compile(
    r"^\s+port \S+ of cell \S+ \((?:\$tribuf|\$_TBUF_)\)\s*$"
)
_PORT_DRIVER_RE = re.compile(r"^\s+module (?:input|output|inout) \S+\s*$")


def _is_tristate_bus(driver_lines: list[str]) -> bool:
    """Return whether a warning's drivers are only tristate buffers and module ports.

    A tristate bus is a working multi-driver design, so it is not counted. Any
    other driver kind (a `$dff`, a process action) makes the warning count.
    """
    if not driver_lines:
        return False
    saw_tristate = False
    for line in driver_lines:
        if _TRISTATE_DRIVER_RE.match(line):
            saw_tristate = True
        elif not _PORT_DRIVER_RE.match(line):
            return False
    return saw_tristate


def find_conflicting_driver_warnings(log_text: str) -> list[str]:
    """Return the header line of each "multiple conflicting drivers" warning, excluding tristate buses."""
    lines = log_text.splitlines()
    hits: list[str] = []
    i = 0
    while i < len(lines):
        if not _CONFLICTING_DRIVERS_RE.match(lines[i]):
            i += 1
            continue
        header = lines[i]
        i += 1
        drivers: list[str] = []
        while i < len(lines) and lines[i][:1].isspace() and lines[i].strip():
            drivers.append(lines[i])
            i += 1
        if not _is_tristate_bus(drivers):
            hits.append(header)
    return hits


# `hierarchy` warns "Could not find interface instance for `<inst>' in `<module>'".
# It runs several times inside `synth`, so the same instance repeats.
_UNRESOLVED_INTERFACE_RE = re.compile(
    r"^(?:\S+:\d+:\s*)?Warning:\s*Could not find interface instance "
    r"for [`'](?P<inst>[^'`]+)' in [`'](?P<module>[^'`]+)'"
)


def find_unresolved_interface_warnings(log_text: str) -> list[tuple[str, str]]:
    """Return unbound interface instances as ``(instance, module)``, de-duplicated in first-seen order.

    See :func:`config.synth.resolve_unresolved_interfaces_mode` for why this is a hazard.
    """
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in log_text.splitlines():
        m = _UNRESOLVED_INTERFACE_RE.match(line)
        if not m:
            continue
        key = (m.group("inst"), m.group("module"))
        if key in seen:
            continue
        seen.add(key)
        hits.append(key)
    return hits


def describe_unresolved_interfaces(hits: list[tuple[str, str]]) -> str:
    """Format unbound instances as comma-separated ``<module>.<inst>``."""
    return ", ".join(f"{module}.{inst}" for inst, module in hits)


def filelist_scan_context(
    fl_path: str,
) -> tuple[list[str], dict[str, str | None]]:
    """Return `+incdir+` directories and `+define+` macros from a synthesis filelist.

    Paths resolve relative to the filelist. A macro's value is ``None`` for a
    bare ``+define+X``, which differs from ``+define+X=``. Raises
    :class:`FatalRtlBuddyError` if a value contains whitespace, which a Yosys
    script line cannot carry.
    """
    fl_dir = os.path.dirname(os.path.abspath(fl_path))
    incdirs: list[str] = []
    defines: dict[str, str | None] = {}
    try:
        with open(fl_path) as f:
            lines = f.readlines()
    except OSError:
        return incdirs, defines
    for line in lines:
        line = line.strip()
        if line.startswith("+incdir+"):
            for entry in line[len("+incdir+") :].split("+"):
                if entry:
                    incdirs.append(os.path.normpath(os.path.join(fl_dir, entry)))
        elif line.startswith("+define+"):
            for entry in line[len("+define+") :].split("+"):
                if not entry:
                    continue
                name, sep, value = entry.partition("=")
                if not name:
                    continue
                if any(c.isspace() for c in value):
                    raise FatalRtlBuddyError(
                        f"{fl_path}: `+define+{entry}` has a value containing "
                        "whitespace, which a Yosys read command cannot "
                        "express; drop the whitespace or move the macro out "
                        "of the filelist"
                    )
                defines[name] = value if sep else None
    return incdirs, defines


# Macros each frontend predefines. The lifetime scan must match them, or code guarded by
# `ifndef SYNTHESIS` (a common idiom) is scanned as if it were compiled.
#   read_verilog: SYNTHESIS=1 and YOSYS=1.
#   read_slang:   SYNTHESIS=1 plus slang built-ins; YOSYS is not defined.
_VERILOG_IMPLICIT_DEFINES: dict[str, str] = {"SYNTHESIS": "1", "YOSYS": "1"}

_SLANG_IMPLICIT_DEFINES: dict[str, str] = {
    "SYNTHESIS": "1",
    # slang built-ins.
    "__slang__": "1",
    "__slang_major__": "1",
    "__slang_minor__": "1",
    "__FILE__": "",
    "__LINE__": "",
    # LRM coverage constants slang predefines.
    "SV_COV_START": "0",
    "SV_COV_STOP": "1",
    "SV_COV_RESET": "2",
    "SV_COV_CHECK": "3",
    "SV_COV_MODULE": "10",
    "SV_COV_HIER": "11",
    "SV_COV_ASSERTION": "20",
    "SV_COV_FSM_STATE": "21",
    "SV_COV_STATEMENT": "22",
    "SV_COV_TOGGLE": "23",
    "SV_COV_OVERFLOW": "-2",
    "SV_COV_ERROR": "-1",
    "SV_COV_NOCOV": "0",
    "SV_COV_OK": "1",
    "SV_COV_PARTIAL": "2",
}


def implicit_defines(frontend: str) -> dict[str, str]:
    """Return the macros `frontend` predefines, before any `-D`."""
    if frontend == "slang":
        return dict(_SLANG_IMPLICIT_DEFINES)
    return dict(_VERILOG_IMPLICIT_DEFINES)


# A bare `+define+X` expands differently per tool, so it is passed to Yosys valueless
# and a run `defines:` value paired with it is always reported as an override.
#   Verilator, read_verilog: empty    Icarus, read_slang: 1
BARE_DEFINE_MEANINGS = (
    "empty under Verilator and read_verilog, 1 under Icarus and slang"
)


def bare_define_value(frontend: str) -> str:
    """Return what a valueless ``-D X`` expands to under `frontend`."""
    return "1" if frontend == "slang" else ""


def merge_defines(
    filelist_defines: dict[str, str | None], run_defines: dict | None
) -> tuple[dict[str, str | None], list[str]]:
    """Merge the filelist's ``+define+`` entries with the run's ``defines:``; the run wins.

    Returns the merged table and the filelist entries the run overrode with a
    different value. A bare filelist entry paired with any run value counts as
    overridden (see BARE_DEFINE_MEANINGS).
    """
    merged: dict[str, str | None] = dict(filelist_defines)
    overridden: list[str] = []
    for k, v in (run_defines or {}).items():
        name, value = str(k), str(v)
        if name in filelist_defines:
            fl_value = filelist_defines[name]
            if fl_value is None:
                overridden.append(f"{name} (filelist=bare, synth={value!r})")
            elif fl_value != value:
                overridden.append(f"{name} (filelist={fl_value!r}, synth={value!r})")
        merged[name] = value
    return merged, overridden


def elaboration_defines(
    fl_path: str, run_defines: dict | None
) -> dict[str, str | None]:
    """Return the ``-D`` table for the Yosys read commands; ``None`` marks a bare entry.

    Does not log; :func:`lifetime_scan_inputs` logs overrides once per run.
    """
    _incdirs, filelist_defines = filelist_scan_context(fl_path)
    merged, _overridden = merge_defines(filelist_defines, run_defines)
    return merged


def lifetime_scan_inputs(
    fl_path: str, synth_name: str, run_defines: dict | None, frontend: str
) -> tuple[list[str], dict[str, str]]:
    """Return include dirs and macros for the lifetime scan, matching the elaboration Yosys performs.

    Macros are the frontend's predefines (:func:`implicit_defines`) plus the
    merged filelist and run defines. A bare entry takes the frontend's
    valueless-``-D`` value. Logs one warning when the run overrides a
    filelist define, e.g. `+define+WIDTH=8` versus `defines: {WIDTH: 16}`.
    """
    incdirs, filelist_defines = filelist_scan_context(fl_path)
    merged, overridden = merge_defines(filelist_defines, run_defines)
    defines = implicit_defines(frontend)
    for name, value in merged.items():
        defines[name] = bare_define_value(frontend) if value is None else value
    if overridden:
        log_event(
            logger,
            logging.WARNING,
            "synth.filelist_defines_overridden",
            synth=synth_name,
            overridden=overridden,
            count=len(overridden),
            filelist=fl_path,
        )
    return incdirs, defines


# Machine-level fallback for the yosys-slang plugin location; explicit config wins.
SLANG_PLUGIN_ENV = "RTL_BUDDY_SLANG_PLUGIN"


def resolve_plugin_path(plugin_path: str | None, root_cfg) -> str | None:
    """Resolve a Yosys plugin path.

    Absolute paths pass through and relative ones resolve against the project
    root. With no configured path, ``RTL_BUDDY_SLANG_PLUGIN`` is used and must
    be absolute after ``~`` expansion. Returns ``None`` when neither is set.
    """
    if plugin_path is None or not plugin_path.strip():
        env = os.environ.get(SLANG_PLUGIN_ENV, "").strip()
        if not env:
            return None
        p = Path(env).expanduser()
        if not p.is_absolute():
            raise FatalRtlBuddyError(
                f"{SLANG_PLUGIN_ENV} must be an absolute path to "
                f"yosys-slang's slang.so, got {env!r}"
            )
        return str(p)
    p = Path(plugin_path)
    if p.is_absolute():
        return str(p)
    if root_cfg is None:
        return str(p.resolve())
    return str((Path(root_cfg.get_project_rootdir()) / p).resolve())


def validate_frontend(opts: SynthToolOpts, root_cfg) -> str | None:
    """Validate the frontend selection and return the plugin path (None for the verilog frontend).

    Raises :class:`FatalRtlBuddyError` (exit 2) for an unknown ``frontend`` or
    for ``frontend: slang`` with no plugin. ``run()`` calls this up front
    because the correctness gates return before script writing.
    """
    if opts.frontend == "verilog":
        return None
    if opts.frontend == "slang":
        plugin_abs = resolve_plugin_path(opts.plugin_path, root_cfg)
        if not plugin_abs:
            raise FatalRtlBuddyError(
                "frontend: slang requires opts.plugin-path to be set "
                "(path to yosys-slang's slang.so), or the "
                f"{SLANG_PLUGIN_ENV} environment variable to point at it"
            )
        return plugin_abs
    raise FatalRtlBuddyError(
        f"unknown synth frontend {opts.frontend!r}; expected 'verilog' or 'slang'"
    )


def emit_frontend_read_cmds(
    opts: SynthToolOpts,
    source_files: list[str],
    top: str,
    defines: dict | None,
    params: dict | None,
    root_cfg,
    incdirs: list[str] | None = None,
) -> list[str]:
    """Return the Yosys commands that load and elaborate the design for the selected frontend.

    ``incdirs`` are passed as ``-I`` to both frontends.

    - verilog: one ``read_verilog -sv -defer`` per file; top-level parameter
      overrides come from a later ``chparam``.
    - slang: ``plugin -i`` plus one ``read_slang`` with ``--top``, ``-D`` macros
      and ``-G`` parameter overrides (folded in because slang elaborates
      eagerly). ``opts.single_unit`` adds ``--single-unit`` and
      ``opts.best_effort_hierarchy`` adds ``--best-effort-hierarchy``.
    """
    # Yosys tokenises script lines shell-style, so quote paths and user-supplied values.
    cmds: list[str] = []

    def _d(k, v, sep: str) -> str:
        # None is a bare `+define+X`, passed valueless (BARE_DEFINE_MEANINGS).
        if v is None:
            return f"-D{sep}{k}"
        v = str(v)
        # Yosys passes quote characters through verbatim, so only quote when whitespace forces it.
        if any(c.isspace() for c in v):
            v = shlex.quote(v)
        return f"-D{sep}{k}={v}"

    inc_flags = [f"-I {shlex.quote(inc)}" for inc in (incdirs or [])]

    define_flags_v = "".join(f" {f}" for f in inc_flags)
    if defines:
        define_flags_v += " " + " ".join(_d(k, v, " ") for k, v in defines.items())

    if opts.frontend == "verilog":
        # Warn rather than ignore silently: the verilog frontend has no equivalent knob.
        if opts.single_unit:
            log_event(
                logger,
                logging.WARNING,
                "synth.single_unit_ignored",
                frontend=opts.frontend,
                top=top,
            )
        if opts.best_effort_hierarchy:
            log_event(
                logger,
                logging.WARNING,
                "synth.best_effort_hierarchy_ignored",
                frontend=opts.frontend,
                top=top,
            )
        for src in source_files:
            cmds.append(f"read_verilog -sv -defer{define_flags_v} {shlex.quote(src)}")
        return cmds

    if opts.frontend == "slang":
        plugin_abs = validate_frontend(opts, root_cfg)
        cmds.append(f"plugin -i {shlex.quote(plugin_abs)}")
        flags: list[str] = list(inc_flags)
        if defines:
            flags.extend(_d(k, v, "") for k, v in defines.items())
        if params:
            flags.extend(f"-G{k}={shlex.quote(str(v))}" for k, v in params.items())
        flags_str = (" " + " ".join(flags)) if flags else ""
        single_unit_flag = " --single-unit" if opts.single_unit else ""
        hierarchy_flag = (
            " --best-effort-hierarchy" if opts.best_effort_hierarchy else ""
        )
        sources_joined = " ".join(shlex.quote(s) for s in source_files)
        cmds.append(
            f"read_slang --std 1800-2017 --top {top}"
            f"{single_unit_flag}{hierarchy_flag}{flags_str} {sources_joined}"
        )
        return cmds

    validate_frontend(opts, root_cfg)
    raise AssertionError("unreachable: validate_frontend rejects other frontends")


def liberty_args(paths: list[str]) -> str:
    """Return one `` -liberty <file>`` per path, the form `dfflibmap`, `abc` and `stat` take for split cell libraries."""
    return "".join(f" -liberty {path}" for path in paths)


def library_fingerprint(paths, root_cfg) -> list[str]:
    """Return the technology library paths a generated script reads, as run identity.

    Paths, not contents, in script order (``read_liberty`` is order-sensitive),
    project-relative where inside the project. Falls back to the paths as
    given when ``root_cfg`` cannot name a project root.
    """
    get_root = getattr(root_cfg, "get_project_rootdir", None)
    root = get_root() if get_root is not None else None
    if not root:
        return [str(path) for path in paths]
    return [project_relative(path, root) for path in paths]


def resolve_dont_use_cells(synth_cfg, root_cfg) -> list[str]:
    """Return the platform's `dont-use-cells` patterns, or `[]` for an unmapped run.

    The same `cfg-pdks.dont-use-cells` list serves P&R (`set_dont_use`) and
    synthesis; a synth platform's own list is added to the PDK's.
    """
    platform = synth_cfg.get_platform()
    if not platform or root_cfg is None:
        return []
    return root_cfg.get_synth_platform_cfg(platform).get_dont_use_cells()


def dont_use_args(cells: list[str]) -> str:
    """Return one ` -dont_use <pattern>` argument per cell for a Yosys `dfflibmap` / `abc` command."""
    return "".join(f" -dont_use {cell}" for cell in cells)


def mapped_abc_script(opts: SynthToolOpts) -> str:
    """Return the ABC script a Liberty-mapped run passes to ``abc -script "+..."``.

    The resolved ``abc_script``, or :data:`DEFAULT_MAPPED_ABC_SCRIPT` when it is empty.
    """
    script = (opts.abc_script or "").strip()
    if not script:
        return DEFAULT_MAPPED_ABC_SCRIPT
    if '"' in script or "\n" in script:
        raise FatalRtlBuddyError(
            f"synth option abc-script must be one line without double quotes, "
            f"got {script!r}; separate ABC commands with ';'"
        )
    return script


def warn_mapped_abc_args(opts: SynthToolOpts, synth_name: str) -> None:
    """Warn that a Liberty-mapped run drops the resolved ``abc_args``."""
    if opts.abc_args:
        log_event(
            logger,
            logging.WARNING,
            "synth.abc_args_ignored",
            synth=synth_name,
            abc_args=opts.abc_args,
        )


def elaboration_fingerprint(opts: SynthToolOpts, root_cfg=None) -> dict:
    """Return the elaboration settings a generated Yosys script reads, for the options fingerprint.

    Shared by both synthesis backends. ``synth_args`` and ``abc_args`` are left
    to the caller and ``strategy`` never applies. ``plugin_path``,
    ``single_unit`` and ``best_effort_hierarchy`` are recorded only under
    ``frontend: slang``. The plugin is recorded resolved, as a path rather than
    its contents. The gate modes are recorded resolved, so an empty
    ``static_functions`` and the explicit value it defaults to digest alike.
    """
    fed = {
        "frontend": opts.frontend,
        "static_functions": resolve_static_functions_mode(opts),
        "conflicting_drivers": resolve_conflicting_drivers_mode(opts),
        "unresolved_interfaces": resolve_unresolved_interfaces_mode(opts),
    }
    if opts.frontend == "slang":
        try:
            plugin = resolve_plugin_path(opts.plugin_path, root_cfg)
        except FatalRtlBuddyError:
            plugin = opts.plugin_path
        fed["plugin_path"] = plugin
        fed["single_unit"] = opts.single_unit
        fed["best_effort_hierarchy"] = opts.best_effort_hierarchy
    return fed


def slang_handles_params(opts: SynthToolOpts) -> bool:
    """Return whether top-level parameters are folded into ``read_slang`` (slang elaborates eagerly)."""
    return opts.frontend == "slang"


# `stat -liberty` prints one `Chip area for module '\<name>':` line per module and, on a
# hierarchical design, a final `Chip area for top module` line whose area includes submodules.
# Parsing anchors on the top and falls back to the last line, not the first.
_AREA_LINE_RE = re.compile(r"Chip area for (?:top )?module[^:]*:\s*([\d.]+)")

# The `cells` line has no module name; the `=== <module> ===` header anchors it.
_CELLS_LINE_RE = re.compile(
    r"^\s+(\d+)\s+(?:[\d.]+(?:[Ee][+-]?\d+)?\s+)?cells$", re.MULTILINE
)
_STAT_SECTION_RE = re.compile(r"^=== (.+) ===\s*$", re.MULTILINE)
_HIERARCHY_SECTION = "design hierarchy"


def parse_area_um2(log_text: str, top: str | None = None) -> float | None:
    """Return the design's chip area from a Yosys log, or None.

    Prefers the last area line naming ``top`` (the `top module` roll-up under
    `-liberty`), else the last area line of any module.
    """
    if top:
        anchored = re.findall(
            r"Chip area for (?:top )?module '\\?" + re.escape(top) + r"':\s*([\d.]+)",
            log_text,
        )
        if anchored:
            return float(anchored[-1])
    matches = _AREA_LINE_RE.findall(log_text)
    return float(matches[-1]) if matches else None


def _whole_design_stat_section(log_text: str, top: str | None) -> str | None:
    """Return the body of the last `stat` section covering the whole design, or None.

    That is the `=== design hierarchy ===` roll-up, or the top module's section
    for a flat design. Searching backwards skips earlier hierarchical stats.
    """
    headers = list(_STAT_SECTION_RE.finditer(log_text))
    for i in range(len(headers) - 1, -1, -1):
        name = headers[i].group(1).strip()
        if name == _HIERARCHY_SECTION or (top and name == top):
            end = headers[i + 1].start() if i + 1 < len(headers) else len(log_text)
            return log_text[headers[i].end() : end]
    return None


def parse_gate_count(log_text: str, top: str | None = None) -> int | None:
    """Return the design's cell count from a Yosys log, or None.

    Reads the whole-design `stat` section (see :func:`_whole_design_stat_section`),
    else the last `cells` line of the log.
    """
    section = _whole_design_stat_section(log_text, top)
    if section is not None:
        counts = _CELLS_LINE_RE.findall(section)
        if counts:
            return int(counts[-1])
    counts = _CELLS_LINE_RE.findall(log_text)
    return int(counts[-1]) if counts else None


class YosysSynth:
    def __init__(
        self,
        name: str,
        synth_cfg: SynthConfig,
        tool_cfg: SynthToolConfig,
        suite_dir: str,
        root_cfg=None,
        effort_cfg: SynthEffortConfig | None = None,
    ):
        self.name = name
        self.synth_cfg = synth_cfg
        self.tool_cfg = tool_cfg
        self.root_cfg = root_cfg
        self.effort_cfg = effort_cfg or default_effort_config()

        artefact_root = Path(suite_dir) / "artefacts" / synth_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)
        self._period_ps: int | None = None
        self._ps_per_unit: float | None = None
        # The `-D` table `_write_script` fed the frontend, read by `_phys_options`.
        self._script_defines: dict[str, str | None] | None = None
        # SDC identity; see `_hash_constraints`. None before the run or with no SDC.
        self._constraints_sha256: str | None = None
        self._opts: SynthToolOpts | None = None

    def _filelist_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.f")

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.ys")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "synth.log")

    def _netlist_path(self, mapped: bool = False) -> str:
        if mapped:
            return os.path.join(self.artefact_dir, "synth_netlist.v")
        return os.path.join(self.artefact_dir, "synth.rtlil")

    def _stats_path(self) -> str:
        """Path of Yosys' per-module `stat -json` dump."""
        return os.path.join(self.artefact_dir, "synth_stat.json")

    def _write_filelist(self) -> str:
        fl_path = self._filelist_path()
        vlog_fl = VlogFilelist(
            name=self.name + "/filelist",
            model_cfg=self.synth_cfg.get_model(),
            output_path=fl_path,
        )
        vlog_fl.write_output(
            output_filepath=fl_path, unroll=True, strip=False, deduplicate=True
        )
        return fl_path

    def _source_files_from_filelist(self, fl_path: str) -> list[str]:
        """Return absolute source file paths from a (possibly stripped) filelist."""
        fl_dir = os.path.dirname(os.path.abspath(fl_path))
        _SKIP = ("+incdir+", "+libext+", "+define+", "-y ", "-F ", "-f ")
        _SOURCE_PREFIX = "-v "
        paths = []
        with open(fl_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                if any(line.startswith(opt) for opt in _SKIP):
                    continue
                if line.startswith(_SOURCE_PREFIX):
                    line = line[len(_SOURCE_PREFIX) :]
                paths.append(os.path.normpath(os.path.join(fl_dir, line)))
        return paths

    def _time_unit_ps(self) -> float:
        """Return the Liberty `time_unit` in picoseconds, the unit of the SDC's periods.

        Raises :class:`LibertyTimeUnitError` when the libraries disagree.
        """
        if self._ps_per_unit is None:
            self._ps_per_unit = liberty_time_unit_ps(self._resolve_lib_paths())
        return self._ps_per_unit

    def _parse_clock_period_ps(self, sdc_path: str) -> int | None:
        """Return the minimum ``create_clock`` period in the SDC, in picoseconds, or None.

        Periods are in the Liberty `time_unit` (:meth:`_time_unit_ps`). ABC ``-D`` takes one timing window, so multi-clock designs use the
        minimum, which over-constrains slower domains. Periods are read via the
        constraint reader, so ``\\``-continued commands and ``{10.0}`` work. A
        period that is not a number (``$p`` under the tokenizer backend, or
        ``[get_property ...]``) is skipped with ``synth.sdc_period_unevaluated``.
        """
        try:
            with open(sdc_path) as f:
                text = f.read()
        except OSError:
            return None

        commands, _backend = read_commands(
            text, interest=frozenset({"create_clock"}), source=sdc_path
        )
        periods: list[float] = []
        for cmd in commands:
            value = _flag_value(cmd.words, "-period")
            if value is None:
                continue
            if value.startswith("{") and value.endswith("}"):
                value = value[1:-1].strip()
            if value.startswith("$") or value.startswith("["):
                log_event(
                    logger,
                    logging.WARNING,
                    "synth.sdc_period_unevaluated",
                    synth=self.synth_cfg.get_name(),
                    sdc=sdc_path,
                    line=cmd.line,
                    value=value,
                )
                continue
            try:
                periods.append(float(value))
            except ValueError:
                continue
        if not periods:
            return None
        ps_per_unit = self._time_unit_ps()
        if len(periods) > 1:
            ns_per_unit = ps_per_unit / 1000.0
            log_event(
                logger,
                logging.WARNING,
                "synth.sdc_multi_clock",
                synth=self.synth_cfg.get_name(),
                clocks=len(periods),
                periods_ns=[p * ns_per_unit for p in periods],
                used_ns=min(periods) * ns_per_unit,
                sdc=sdc_path,
            )
        return int(min(periods) * ps_per_unit)

    def _resolve_lib_paths(self) -> list[str]:
        extras = list(self.synth_cfg.get_lib_paths())
        platform = self.synth_cfg.get_platform()
        if not platform or self.root_cfg is None:
            return extras
        return self.root_cfg.get_synth_platform_cfg(platform).get_paths() + extras

    def _resolve_cell_lib_paths(self) -> list[str]:
        """The standard-cell Liberty files mapping and `stat` use: the platform corner's, or `lib-paths` without a platform."""
        platform = self.synth_cfg.get_platform()
        if not platform or self.root_cfg is None:
            return list(self.synth_cfg.get_lib_paths())
        return self.root_cfg.get_synth_platform_cfg(platform).get_paths()

    def _parse_area_um2(self, log_text: str, top: str | None = None) -> float | None:
        return parse_area_um2(log_text, top)

    def _parse_gate_count(self, log_text: str, top: str | None = None) -> int | None:
        return parse_gate_count(log_text, top)

    def _parse_critical_path_ps(self, log_text: str) -> float | None:
        m = re.search(r"Delay\s*=\s*([\d.]+)\s*ps", log_text)
        return float(m.group(1)) if m else None

    def _resolve_opts(self) -> SynthToolOpts:
        """Return tool options with the effort applied, then per-synthesis ``tool_overrides`` on top.

        Memoised so override validation warnings are emitted once.
        """
        if self._opts is not None:
            return self._opts
        overrides = self.synth_cfg.get_tool_overrides_for(self.tool_cfg.get_name())
        opts = self.tool_cfg.get_opts(overrides)
        if not overrides or "synth_args" not in overrides:
            eff_synth = self.effort_cfg.get_yosys_synth_args()
            if eff_synth:
                opts.synth_args = eff_synth
        if not overrides or "abc_args" not in overrides:
            eff_abc = self.effort_cfg.get_yosys_abc_args()
            if eff_abc:
                opts.abc_args = eff_abc
        if not overrides or "abc_script" not in overrides:
            eff_script = self.effort_cfg.get_yosys_abc_script()
            if eff_script:
                opts.abc_script = eff_script
        self._opts = opts
        return opts

    def _scan_static_lifetimes(
        self, fl_path: str, opts: SynthToolOpts
    ) -> list[LifetimeFinding]:
        """Return static-lifetime findings for the filelist's sources, or [] when the gate is off.

        Scan roots are the bare and ``-v`` entries; headers are followed through
        ``+incdir+``, and ``-y`` directories are not scanned. Macros match the
        Yosys invocation (:func:`lifetime_scan_inputs`).
        """
        # Resolved first so the overridden-defines warning appears even when the gate is off.
        incdirs, defines = lifetime_scan_inputs(
            fl_path,
            self.synth_cfg.get_name(),
            self.synth_cfg.get_defines(),
            opts.frontend,
        )
        if resolve_static_functions_mode(opts) == "allow":
            return []
        return scan_files(
            self._source_files_from_filelist(fl_path),
            incdirs=incdirs,
            defines=defines,
            # Only slang honours --single-unit.
            single_unit=opts.single_unit and opts.frontend == "slang",
            # slang's `undefineall` keeps the -D macros; read_verilog drops them.
            undefineall_keeps_predefines=opts.frontend == "slang",
        )

    def _stat_json_cmd(self, liberty_args: str) -> str:
        """Return the ``stat -json`` script line that feeds the phys model's module rows.

        Written with ``tee -q -o`` to keep JSON out of ``synth.log``. Without a
        Liberty the modules get a null area.
        """
        return f"tee -q -o {self._stats_path()} stat -json{liberty_args}"

    def _write_script(self, fl_path: str) -> str:
        top = self.synth_cfg.get_top()
        opts = self._resolve_opts()
        params = self.synth_cfg.get_params()
        lib_paths = self._resolve_lib_paths()
        mapped = bool(lib_paths)

        defines = elaboration_defines(fl_path, self.synth_cfg.get_defines())
        self._script_defines = defines
        self._hash_constraints()
        incdirs = incdirs_from_filelist(fl_path)

        lines = []
        for lib in lib_paths:
            lines.append(f"read_liberty -lib {lib}")

        source_files = self._source_files_from_filelist(fl_path)
        lines.extend(
            emit_frontend_read_cmds(
                opts=opts,
                source_files=source_files,
                top=top,
                defines=defines,
                params=params,
                root_cfg=self.root_cfg,
                incdirs=incdirs,
            )
        )

        # slang already took params via -G.
        if params and not slang_handles_params(opts):
            for key, value in params.items():
                lines.append(f"chparam -set {key} {value} {top}")

        synth_cmd = f"synth -top {top}"
        if opts.synth_args:
            synth_cmd += f" {opts.synth_args}"
        lines.append(synth_cmd)
        # Formal cells in the netlist are rejected by OpenROAD/OpenSTA `read_verilog`.
        lines.append("chformal -remove")

        if mapped:
            dont_use = dont_use_args(
                resolve_dont_use_cells(self.synth_cfg, self.root_cfg)
            )
            cell_libs = liberty_args(self._resolve_cell_lib_paths())
            lines.append(f"dfflibmap{dont_use}{cell_libs}")

            abc_cmd = f"abc{cell_libs}{dont_use}"
            constraints = self.synth_cfg.get_constraints()
            period_ps = None
            if constraints:
                period_ps = self._parse_clock_period_ps(constraints)
                if period_ps is not None:
                    abc_cmd += f" -D {period_ps}"
                    log_event(
                        logger,
                        logging.DEBUG,
                        "synth.sdc_period",
                        synth=self.synth_cfg.get_name(),
                        period_ps=period_ps,
                        sdc=constraints,
                    )
                else:
                    log_event(
                        logger,
                        logging.WARNING,
                        "synth.sdc_no_clock",
                        synth=self.synth_cfg.get_name(),
                        sdc=constraints,
                    )
            self._period_ps = period_ps

            abc_script = mapped_abc_script(opts)
            if period_ps is not None:
                abc_script += _ABC_STIME
            abc_cmd += f' -script "+{abc_script}"'
            warn_mapped_abc_args(opts, self.synth_cfg.get_name())
            lines.append(abc_cmd)
            lines.append(f"write_verilog {self._netlist_path(mapped=True)}")
            lines.append(f"stat{cell_libs}")
            lines.append(self._stat_json_cmd(cell_libs))
        else:
            if opts.abc_args:
                lines.append(f"abc {opts.abc_args}")
            lines.append(f"write_rtlil {self._netlist_path()}")
            lines.append(self._stat_json_cmd(""))

        script = "\n".join(lines) + "\n"
        script_path = self._script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def _hash_constraints(self) -> None:
        """Record the SDC digest as the script is written, so it identifies the bytes the run read."""
        self._constraints_sha256 = sha256_of(self.synth_cfg.get_constraints())

    def _confirm_constraints_unchanged(self) -> None:
        """Null the SDC digest, with a warning, if the SDC changed since :meth:`_hash_constraints`."""
        self._constraints_sha256, changed = confirm_digest(
            self.synth_cfg.get_constraints(), self._constraints_sha256
        )
        if changed:
            log_event(
                logger,
                logging.WARNING,
                "synth.constraints_changed_during_run",
                synth=self.synth_cfg.get_name(),
                constraints=self.synth_cfg.get_constraints(),
            )

    def _clear_stale_netlists(self) -> str | None:
        """Remove the previous run's netlists, stats and published module rows.

        `rb pnr` and `rb power` read the netlists at fixed paths, so every early
        return of `run()` must leave none stale. Both netlist spellings and
        ``synth_stat.json`` are deleted, and this flow's half of the phys model
        is nulled (the other flow's half is kept).

        Returns None, or the reason the withdrawal failed, which callers turn
        into a failed run (see
        :func:`~rtl_buddy.phys.publish.withdrawal_failure_desc`).
        """
        stale = clear_stale_artefacts(
            [
                self._netlist_path(mapped=True),
                self._netlist_path(),
                self._stats_path(),
            ],
            owner=self.synth_cfg.get_name(),
        )
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "synth.stale_artefacts_removed",
                synth=self.synth_cfg.get_name(),
                paths=stale,
            )
        return self._invalidate_phys_half()

    def _invalidate_phys_half(self) -> str | None:
        """Null this flow's half of any phys model and manifest in the artefact directory.

        The power half is untouched. Success logs at DEBUG. A failure warns and
        returns the error, because the rows now stand over a deleted
        ``synth_stat.json``.
        """
        result = invalidate_half(self.artefact_dir, "modules")
        if result["error"]:
            log_event(
                logger,
                logging.WARNING,
                "synth.phys_half_stale",
                synth=self.synth_cfg.get_name(),
                error=result["error"],
            )
            return result["error"]
        if result["model"] or result["manifest"]:
            log_event(
                logger,
                logging.DEBUG,
                "synth.phys_half_invalidated",
                synth=self.synth_cfg.get_name(),
                model=result["model"],
                manifest=result["manifest"],
            )
        return None

    def _fail_after_yosys(self, desc: str) -> SynthFailResults:
        """Return a FAIL for a run that already invoked Yosys, removing its netlist.

        Yosys may have written the netlist before failing, and `rb pnr` and
        `rb power` would consume it. If the withdrawal fails, the reason is
        appended to ``desc``. Every post-Yosys failure return goes through here.
        """
        stale_error = self._clear_stale_netlists()
        if stale_error is not None:
            desc = f"{desc}; {withdrawal_failure_desc(stale_error)}"
        return SynthFailResults(name=self.name + "/results", desc=desc)

    def run(self) -> SynthResults:
        # Do not proceed if the withdrawal failed: the published rows would outlive their stats file.
        stale_error = self._clear_stale_netlists()
        if stale_error is not None:
            return SynthFailResults(
                name=self.name + "/results",
                desc=withdrawal_failure_desc(stale_error),
                fail_stage="setup",
            )
        log_event(
            logger,
            logging.INFO,
            "synth.start",
            synth=self.synth_cfg.get_name(),
            tool=self.tool_cfg.get_executable(),
            top=self.synth_cfg.get_top(),
        )

        # Resolve gate modes before the filelist write so a misspelled value is
        # a fatal config error, not a FilelistError-masked FAIL (as in OpenRoadSynth.run()).
        opts = self._resolve_opts()
        static_mode = resolve_static_functions_mode(opts)
        conflicting_mode = resolve_conflicting_drivers_mode(opts)
        interfaces_mode = resolve_unresolved_interfaces_mode(opts)
        # Same for frontend errors: the gates below return before `_write_script()` checks.
        validate_frontend(opts, self.root_cfg)

        try:
            self._time_unit_ps()
        except LibertyTimeUnitError as e:
            log_event(
                logger,
                logging.ERROR,
                "synth.liberty_time_unit_error",
                synth=self.synth_cfg.get_name(),
                error=str(e),
                units=e.units,
            )
            return SynthFailResults(
                name=self.name + "/results", desc=str(e), fail_stage="setup"
            )

        try:
            fl_path = self._write_filelist()
        except FilelistError as e:
            log_event(
                logger,
                logging.ERROR,
                "synth.filelist_failed",
                synth=self.synth_cfg.get_name(),
                error=str(e),
            )
            return SynthFailResults(
                name=self.name + "/results",
                desc=f"Filelist error: {e}",
                fail_stage="setup",
            )

        findings = self._scan_static_lifetimes(fl_path, opts)
        if findings:
            detail = describe_findings(findings)
            if static_mode == "error":
                log_event(
                    logger,
                    logging.ERROR,
                    "synth.static_functions",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    count=len(findings),
                    findings=[f.describe() for f in findings[:MAX_EVENT_FINDINGS]],
                    truncated=max(0, len(findings) - MAX_EVENT_FINDINGS),
                )
                return SynthFailResults(
                    name=self.name + "/results",
                    desc=(
                        f"{len(findings)} subroutine(s) declared without an "
                        f"explicit automatic lifetime: {detail}"
                    ),
                )
            for finding in findings:
                log_event(
                    logger,
                    logging.WARNING,
                    "synth.static_function",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    path=finding.path,
                    line=finding.line,
                    kind=finding.kind,
                    subroutine=finding.name,
                )

        script_path = self._write_script(fl_path)
        log_path = self._log_path()

        cmd = [self.tool_cfg.get_executable(), "-s", script_path]
        log_event(
            logger,
            logging.DEBUG,
            "synth.run_cmd",
            synth=self.synth_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        with task_status(f"synth {self.synth_cfg.get_name()}"):
            with open(log_path, "w") as log_f:
                result = run_managed_process(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=self.artefact_dir,
                )

        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "synth.failed",
                synth=self.synth_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            return self._fail_after_yosys(f"Tool exited with code {result.returncode}")

        try:
            with open(log_path, "r") as f:
                log_text = f.read()
        except OSError:
            log_text = ""

        error_lines = [ln for ln in log_text.splitlines() if ln.startswith("ERROR:")]
        if error_lines:
            log_event(
                logger,
                logging.WARNING,
                "synth.errors_in_log",
                synth=self.synth_cfg.get_name(),
                count=len(error_lines),
                log=log_path,
            )
            return self._fail_after_yosys(
                f"{len(error_lines)} ERROR(s) in synthesis log"
            )

        conflicting = find_conflicting_driver_warnings(log_text)
        if conflicting and conflicting_mode == "error":
            log_event(
                logger,
                logging.ERROR,
                "synth.conflicting_drivers",
                synth=self.synth_cfg.get_name(),
                count=len(conflicting),
                log=log_path,
            )
            # The netlist at the fixed path is this run's own product; drop it.
            return self._fail_after_yosys(
                f"{len(conflicting)} 'multiple conflicting drivers' "
                f"warning(s) in {log_path}"
            )

        unbound: list[tuple[str, str]] = []
        if interfaces_mode != "allow":
            unbound = find_unresolved_interface_warnings(log_text)
            if unbound and interfaces_mode == "error":
                log_event(
                    logger,
                    logging.ERROR,
                    "synth.unresolved_interfaces",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    count=len(unbound),
                    instances=[
                        f"{module}.{inst}"
                        for inst, module in unbound[:MAX_EVENT_FINDINGS]
                    ],
                    truncated=max(0, len(unbound) - MAX_EVENT_FINDINGS),
                    log=log_path,
                )
                # Drop the netlist, as the conflicting-driver gate does.
                return self._fail_after_yosys(
                    f"{len(unbound)} unbound interface instance(s): "
                    f"{describe_unresolved_interfaces(unbound)}"
                )
            for inst, module in unbound:
                log_event(
                    logger,
                    logging.WARNING,
                    "synth.unresolved_interface",
                    synth=self.synth_cfg.get_name(),
                    frontend=opts.frontend,
                    instance=inst,
                    module=module,
                    log=log_path,
                )

        top = self.synth_cfg.get_top()
        area_um2 = self._parse_area_um2(log_text, top)
        gate_count = self._parse_gate_count(log_text, top)
        crit_path_ps = self._parse_critical_path_ps(log_text)
        wns_ps = (
            self._period_ps - crit_path_ps
            if self._period_ps is not None and crit_path_ps is not None
            else None
        )

        log_event(
            logger,
            logging.INFO,
            "synth.passed",
            synth=self.synth_cfg.get_name(),
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
            log=log_path,
        )
        phys_model = self._publish_phys_model(
            area_um2=area_um2,
            gate_count=gate_count,
            mapped=bool(self._resolve_lib_paths()),
        )
        return SynthPassResults(
            name=self.name + "/results",
            area_um2=area_um2,
            gate_count=gate_count,
            wns_ps=wns_ps,
            static_function_findings=len(findings) or None,
            unresolved_interfaces=len(unbound) or None,
            phys_model=phys_model,
        )

    def _phys_options(self, *, mapped: bool) -> dict:
        """Return the effective options the config fingerprint is digested over.

        Only what `_write_script` reads, not the whole resolved `SynthToolOpts`.
        Keys:

        - `elaborate`: the shared frontend subset (:func:`elaboration_fingerprint`).
        - `synth_args`: the resolved value, including effort and overrides.
        - `params` and `defines`: `defines` is the merged table the script
          received (:func:`elaboration_defines`), not `synth.yaml`'s field alone.
        - `mapped` branch: `abc_script` (:func:`mapped_abc_script`),
          `abc_period_ps` (null when the SDC names no clock),
          `libs` (:func:`library_fingerprint`) and `dont_use`.
        - unmapped branch: `abc_args`. Mapped runs ignore `abc_args`.

        `strategy` is never included. Values only, not the files they came from.
        """
        opts = self._resolve_opts()
        fed = {
            "tool": self.tool_cfg.get_name(),
            "mapped": mapped,
            "elaborate": elaboration_fingerprint(opts, self.root_cfg),
            "synth_args": opts.synth_args,
            "params": self.synth_cfg.get_params(),
            "defines": self._digested_defines(),
        }
        if mapped:
            fed["abc_script"] = mapped_abc_script(opts)
            fed["abc_period_ps"] = self._period_ps
            fed["libs"] = library_fingerprint(self._resolve_lib_paths(), self.root_cfg)
            fed["dont_use"] = resolve_dont_use_cells(self.synth_cfg, self.root_cfg)
        else:
            fed["abc_args"] = opts.abc_args
        return fed

    def _digested_defines(self) -> dict:
        """Return the macro table the script fed the frontend, as recorded by `_write_script`.

        Falls back to `synth.yaml`'s ``defines`` when no script was written.
        """
        if self._script_defines is not None:
            return dict(self._script_defines)
        return self.synth_cfg.get_defines()

    def _publish_phys_model(
        self, *, area_um2: float | None, gate_count: int | None, mapped: bool
    ) -> str | None:
        """Write `phys-model.json` and its manifest for a passed run; return the model path.

        Never fails the synthesis. If the ``stat -json`` output is missing or
        unreadable, the model lacks its `modules` rows, a warning is logged and
        the log totals are still written. The identity fields come from
        `_phys_options`, the platform, effort and SDC. The SDC digest is the one
        recorded at script generation and confirmed here.
        """
        self._confirm_constraints_unchanged()
        published = publish_synth(
            artefact_dir=self.artefact_dir,
            top=self.synth_cfg.get_top(),
            backend="yosys",
            run=self.synth_cfg.get_name(),
            stats_path=self._stats_path(),
            netlist_path=self._netlist_path(mapped=mapped),
            log_path=self._log_path(),
            area_um2=area_um2,
            gate_count=gate_count,
            platform=self.synth_cfg.get_platform(),
            effort=self.effort_cfg.get_name(),
            constraints=self.synth_cfg.get_constraints(),
            constraints_sha256=self._constraints_sha256,
            options=self._phys_options(mapped=mapped),
        )
        if published["error"] is not None or published["rows"] is None:
            log_event(
                logger,
                logging.WARNING,
                "synth.phys_model_incomplete",
                synth=self.synth_cfg.get_name(),
                stats=self._stats_path(),
                error=published["error"],
            )
        return published["model"]
