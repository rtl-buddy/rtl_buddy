import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from .artifact_paths import clear_stale_artefacts
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
)
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

# Default ABC script for liberty without timing constraint
_ABC_SCRIPT_NO_TIMING = (
    "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
    "&get -n; &dch -f; &nf {D}; &put"
)
# Same but with stime -p appended to report critical-path delay
_ABC_SCRIPT_WITH_TIMING = _ABC_SCRIPT_NO_TIMING + "; stime -p"

# A machine-log line carries at most this many findings; the full count and the
# number dropped travel alongside, so nothing is silently lost.
MAX_EVENT_FINDINGS = 25


# Yosys `check` (run inside `synth`) reports a shared net taking incompatible
# drivers as `Warning: multiple conflicting drivers for <mod>.<sig> [n]:`, from
# log_warning(), so the "Warning: " prefix is always present and always starts
# the line. Anchoring on it keeps the `check -h` help text ("two or more
# conflicting drivers for one wire") and any echo of the phrase in a script or
# command line from being counted.
#
# The sibling `Drivers conflicting with a constant <s> driver:` message from
# the same pass is deliberately NOT matched: it fires when something drives a
# constant bit, which is a different condition from the shared-formal
# corruption this gate exists for, and neither repro shape in #472 produces it.
_CONFLICTING_DRIVERS_RE = re.compile(
    r"^(?:\S+:\d+:\s*)?Warning:\s*multiple conflicting drivers\b"
)

# Indented driver lines follow each warning, one per driver, in the three forms
# check.cc emits: `port <P>[n] of cell <name> (<type>)`, `module input <w>[n]`,
# and `action <lhs> <= <rhs> (... rule) in process <p>`.
_TRISTATE_DRIVER_RE = re.compile(
    r"^\s+port \S+ of cell \S+ \((?:\$tribuf|\$_TBUF_)\)\s*$"
)
_PORT_DRIVER_RE = re.compile(r"^\s+module (?:input|output|inout) \S+\s*$")


def _is_tristate_bus(driver_lines: list[str]) -> bool:
    """Whether a warning's drivers are a legitimate tristate bus.

    Two `assign bus = en ? d : 8'bz;` on an `inout wire` elaborate to a pair of
    `$tribuf` cells plus the module port, and yosys `check` reports every bit
    of the bus as conflicting. That is a working multi-driver design, not the
    silent corruption this gate is for, so a warning whose drivers are all
    tristate buffers and module ports is skipped. One driver of any other kind
    (a `$dff`, a process action) means the warning is counted.
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
    """Yosys "multiple conflicting drivers" warnings that are real conflicts.

    Returns the header line of each warning, with legitimate tristate buses
    filtered out; see :func:`_is_tristate_bus`.
    """
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


def filelist_scan_context(
    fl_path: str,
) -> tuple[list[str], dict[str, str | None]]:
    """`+incdir+` directories and `+define+` macros from a synthesis filelist.

    `_source_files_from_filelist` drops both because Yosys is handed sources
    only. The incdirs are what the lifetime scan resolves `` `include ``
    through (the read commands get theirs from
    :func:`vlog_filelist.incdirs_from_filelist`). The defines feed the Yosys
    read commands (see :func:`elaboration_defines`) and the lifetime scan's
    macro table. Paths resolve relative to the filelist, matching the source
    entries.

    A macro's value is ``None`` when the entry carried no ``=``: a bare
    ``+define+X`` and ``+define+X=`` are different things -- the former is
    passed to Yosys as a valueless ``-D X``, the latter as ``-D X=``.

    A value containing whitespace is fatal: a Yosys script line is tokenised
    on whitespace with no quoting that survives (see
    :func:`fpv_coi.render_slang_read`), so no ``-D`` can carry it and the
    read command would otherwise fail on a mangled source path.
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


# Macros each frontend defines for itself, so every synthesis elaboration sees
# them whether or not the project asks for them. The scan has to agree, or a
# guarded region the compiler never reads -- `\`ifndef SYNTHESIS` around a
# simulation-only helper is a very common idiom -- becomes a finding.
#
# Verified by source and then confirmed with a deliberate syntax error inside
# the guarded region, per frontend:
#
#   read_verilog  SYNTHESIS=1 (verilog_frontend.cc:494, unless -formal, which
#                 rtl_buddy never passes) and YOSYS=1, added by the
#                 define_map_t constructor in preproc.cc:335.
#   read_slang    SYNTHESIS=1, pushed by yosys-slang (slang_frontend.cc:3299)
#                 unless --no-synthesis-define, which rtl_buddy never passes,
#                 plus slang's own built-ins. YOSYS is NOT defined here.
_VERILOG_IMPLICIT_DEFINES: dict[str, str] = {"SYNTHESIS": "1", "YOSYS": "1"}

_SLANG_IMPLICIT_DEFINES: dict[str, str] = {
    "SYNTHESIS": "1",
    # slang built-ins, re-added by Preprocessor::undefineAll().
    "__slang__": "1",
    "__slang_major__": "1",
    "__slang_minor__": "1",
    "__FILE__": "",
    "__LINE__": "",
    # LRM coverage constants slang predefines; guarding on one is unusual but
    # legal, and a spurious finding is worse than a redundant entry.
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
    """Macros `frontend` defines for itself, before any `-D` is applied."""
    if frontend == "slang":
        return dict(_SLANG_IMPLICIT_DEFINES)
    return dict(_VERILOG_IMPLICIT_DEFINES)


# What a *bare* `+define+X` (no `=`) gives the macro, measured by expanding it
# in an expression rather than only testing `\`ifdef`, with `-E` output as the
# witness:
#
#   Verilator  +define+X  -> empty   `assign y = 8'd0 + \`X;` becomes `+ ;`
#   Icarus     -DX        -> 1       ...becomes `+ 1;`
#   read_verilog -DX      -> empty   (verilog_frontend.cc leaves value "")
#   read_slang   -DX      -> 1       (slang appends " 1" to a valueless predefine)
#
# The consumers disagree with each other, and rtl_buddy's simulation flow hands
# the filelist to whichever builder the suite selects. So a bare entry is passed
# to Yosys valueless (the frontend decides), and a run `defines:` value paired
# with it is always reported as an override rather than compared.
BARE_DEFINE_MEANINGS = (
    "empty under Verilator and read_verilog, 1 under Icarus and slang"
)


def bare_define_value(frontend: str) -> str:
    """What a valueless ``-D X`` expands to under `frontend`; see
    BARE_DEFINE_MEANINGS."""
    return "1" if frontend == "slang" else ""


def merge_defines(
    filelist_defines: dict[str, str | None], run_defines: dict | None
) -> tuple[dict[str, str | None], list[str]]:
    """Macros Yosys is given: the filelist's ``+define+`` entries, then the
    run's ``defines:`` on top, so the synth.yaml entry wins on conflict.

    Returns the merged table (filelist order first, run additions after) and
    the filelist entries the run overrode with a different value. A bare
    filelist entry paired with any run value counts as overridden: the run
    spells a value the filelist did not, and the tools disagree about what a
    valueless macro expands to (BARE_DEFINE_MEANINGS), so the pair cannot be
    called equal.
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
    """The ``-D`` table for the Yosys read commands: filelist ``+define+``
    entries with the run's ``defines:`` layered on top. ``None`` marks a bare
    entry, emitted without ``=``. Quiet -- the override warning is logged once
    per run by :func:`lifetime_scan_inputs`."""
    _incdirs, filelist_defines = filelist_scan_context(fl_path)
    merged, _overridden = merge_defines(filelist_defines, run_defines)
    return merged


def lifetime_scan_inputs(
    fl_path: str, synth_name: str, run_defines: dict | None, frontend: str
) -> tuple[list[str], dict[str, str]]:
    """Include dirs and macros for the lifetime scan, matching what Yosys sees.

    The scan must model the *elaboration the flow actually performs*, not an
    idealised one. `_write_script()` passes the filelist's ``+define+``
    entries and then the run's ``defines:`` to ``read_verilog -D`` /
    ``read_slang -D`` (:func:`elaboration_defines`), so the macro table is
    the frontend's own predefines (:func:`implicit_defines`) with that same
    merged table on top. A bare filelist entry takes the value the selected
    frontend gives a valueless ``-D`` (:func:`bare_define_value`).

    A filelist entry the run overrides with a different value is reported
    once per run: `+define+WIDTH=8` in the filelist with `defines: {WIDTH:
    16}` on the run means simulation builds an 8-bit design and synthesis a
    16-bit one, and nothing else says so.
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


# Machine-level fallback for the yosys-slang plugin location, so toolchain
# env scripts can provide it once per machine instead of every project
# hard-coding an absolute path. Explicit config always wins.
SLANG_PLUGIN_ENV = "RTL_BUDDY_SLANG_PLUGIN"


def resolve_plugin_path(plugin_path: str | None, root_cfg) -> str | None:
    """Resolve a Yosys plugin path. Absolute paths pass through; relative
    paths are taken relative to the project root. When no path is
    configured, fall back to ``RTL_BUDDY_SLANG_PLUGIN`` from the
    environment, which must be absolute after ``~`` expansion — a
    machine-level variable has no project anchor, and a relative value
    would otherwise resolve against the tool subprocess CWD (failing
    only as a silent COI-coverage warning on the FPV side). Returns
    ``None`` when neither channel is set."""
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
    """Check the frontend selection, returning the resolved plugin path.

    Raises :class:`FatalRtlBuddyError` for an unknown ``frontend`` and for
    ``frontend: slang`` with no plugin to load. These are configuration
    errors, so they must exit 2 rather than become a per-run ``FAIL`` -- and
    the correctness gates return before ``_write_script()`` ever calls
    :func:`emit_frontend_read_cmds`, so ``run()`` calls this up front instead
    of relying on elaboration to reach the same checks.

    Returns the absolute plugin path for slang, and None for the verilog
    frontend, which needs no plugin.
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
    """Emit the Yosys commands that load + elaborate the design, based on
    the selected frontend (``verilog`` | ``slang``).

    ``incdirs`` are the filelist's ``+incdir+`` directories, passed as ``-I``
    to both frontends so a `` `include `` resolves the same way it does under
    the simulation flow (#519).

    - verilog (default): per-file ``read_verilog -sv -defer`` matching the
      legacy behavior. Elaboration is lazy; ``synth -top`` resolves later
      and any top-level parameter overrides come from a subsequent
      ``chparam`` line.
    - slang: load the yosys-slang plugin and elaborate fully with
      ``read_slang``. Slang requires ``--top`` and accepts ``-D NAME=VAL``
      for macros and ``-G NAME=VAL`` for top-level parameter overrides
      (the latter folded in here since slang elaborates eagerly — a later
      ``chparam`` would arrive too late). ``opts.single_unit`` adds
      ``--single-unit``, parsing every source as one compilation unit so
      preprocessor definitions stay visible across file boundaries.
    """
    # Shell-quote everything that comes from filesystem paths or
    # user-supplied dict values — Yosys parses each script line with
    # shell-style tokenisation, so an unquoted space in a macOS Library
    # path or a project name with a space corrupts the command. This is
    # critical on the slang path (one read_slang line covers all
    # sources, so one bad path breaks elaboration entirely) but applied
    # uniformly so both frontends behave the same.
    cmds: list[str] = []

    def _d(k, v, sep: str) -> str:
        # A None value is a bare filelist `+define+X`: passed valueless, so
        # each frontend gives it its own meaning (BARE_DEFINE_MEANINGS).
        if v is None:
            return f"-D{sep}{k}"
        v = str(v)
        # Yosys splits a script line on whitespace and passes quote characters
        # through verbatim (see fpv_coi.render_slang_read), so a Verilog literal
        # such as 8'hff or "ok" must reach the frontend untouched: shlex.quote
        # would wrap it in quotes the frontend then reads as part of the value.
        if any(c.isspace() for c in v):
            v = shlex.quote(v)
        return f"-D{sep}{k}={v}"

    inc_flags = [f"-I {shlex.quote(inc)}" for inc in (incdirs or [])]

    define_flags_v = "".join(f" {f}" for f in inc_flags)
    if defines:
        define_flags_v += " " + " ".join(_d(k, v, " ") for k, v in defines.items())

    if opts.frontend == "verilog":
        # Not fatal — the legacy frontend simply has no equivalent knob —
        # but never silently accepted either: a project that asked for one
        # compilation unit and did not get it fails later, inscrutably.
        if opts.single_unit:
            log_event(
                logger,
                logging.WARNING,
                "synth.single_unit_ignored",
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
        sources_joined = " ".join(shlex.quote(s) for s in source_files)
        cmds.append(
            f"read_slang --std 1800-2017 --top {top}"
            f"{single_unit_flag}{flags_str} {sources_joined}"
        )
        return cmds

    validate_frontend(opts, root_cfg)
    raise AssertionError("unreachable: validate_frontend rejects other frontends")


def library_fingerprint(paths, root_cfg) -> list[str]:
    """The technology libraries a generated script reads, as identity (#570).

    Both backends resolve Liberty (and, for OpenROAD, LEF) from the
    platform's PDK corner with the config's own ``lib-paths`` /
    ``lef-paths`` appended, and both then name the resolved files in the
    script. The config fingerprint recorded only *that* the run was
    mapped, so the classic experiment — one design, one effort, two
    corners named explicitly rather than through a platform — produced
    two netlists with two areas under one ``options_sha256``, and a
    reader comparing runs was told they were the same experiment.

    **Paths, not contents.** A Liberty file is tens of megabytes and the
    fingerprint is taken once per run; hashing the library set would put
    a pass over the PDK into every synthesis to tell apart experiments
    that the *names* already tell apart. A corner that is edited in place
    under one path is the case a path cannot catch, and it is not the
    case the reviewer describes or that a synthesis flow creates.

    **In script order, not sorted.** ``read_liberty`` is order-sensitive
    — a cell defined twice resolves to the file that supplied it last —
    so two runs naming one set of libraries in two orders are two
    experiments, and a sort would digest them as one.

    Spelled project-relative where they sit inside the project, as every
    other path in these documents is
    (:func:`rtl_buddy.phys.manifest.project_relative`), so a checkout
    moved between machines is not read as a different library set. A PDK
    outside the project keeps its absolute path, which is the only
    identity it has.

    A ``root_cfg`` that cannot name a project root — absent, or a stand-in
    that answers only the queries its caller needs — falls back to the
    paths as resolved. This feeds `_publish_phys_model`, which never fails
    a synthesis whose netlist is already on disk and already judged, and a
    fingerprint helper is the last place worth raising from.
    """
    get_root = getattr(root_cfg, "get_project_rootdir", None)
    root = get_root() if get_root is not None else None
    if not root:
        return [str(path) for path in paths]
    return [project_relative(path, root) for path in paths]


def elaboration_fingerprint(opts: SynthToolOpts, root_cfg=None) -> dict:
    """The elaboration settings a generated Yosys script actually reads.

    Both synthesis backends elaborate through :func:`emit_frontend_read_cmds`
    and gate the run with the same two mode resolvers, so both fingerprint
    the same subset of :class:`~rtl_buddy.config.synth.SynthToolOpts` — and
    fingerprinting it in one place is what keeps the two from drifting apart
    about what an experiment varied (#568).

    A *subset*, because a digest of the whole dataclass reports differences
    the netlist cannot have. ``synth_args`` and ``abc_args`` are left to the
    caller: which of them a script reads, and whether it reads the tool
    option or the effort's, differs per backend and per path. ``strategy``
    never appears here at all — it is a stage-2 OpenROAD knob and no Yosys
    script line consumes it.

    ``plugin_path`` and ``single_unit`` are recorded only under
    ``frontend: slang``. The verilog branch of the read emitter loads no
    plugin and warns that it cannot honour one compilation unit, so two
    verilog runs differing in either produce the same script and are the
    same experiment.

    The plugin is fingerprinted **resolved**, as
    :func:`resolve_plugin_path` returns it and the script's ``plugin -i``
    line spells it (#570). The raw field was wrong in both directions: a
    plugin selected through the ``RTL_BUDDY_SLANG_PLUGIN`` fallback
    leaves it empty, so two runs against two different yosys-slang builds
    digested identically, while a relative path and the absolute path it
    resolves to are one plugin digesting as two. A path and not its
    contents, consistently with :func:`library_fingerprint`: a rebuilt
    ``slang.so`` at one path is the case a path cannot catch, and hashing
    a shared object on every synthesis is not the price for it.

    ``root_cfg`` is what a relative path resolves against. Resolution is
    allowed to fail back to the raw spelling: it raises only for a
    non-absolute environment variable, which :func:`validate_frontend`
    has already refused by the time any run publishes, and a fingerprint
    is the last place worth raising from.

    The two gates are recorded *resolved* rather than as configured: the
    default of ``static_functions`` depends on the frontend
    (:func:`resolve_static_functions_mode`), so an empty setting under
    slang and an explicit ``error`` are one behaviour and must digest as
    one.
    """
    fed = {
        "frontend": opts.frontend,
        "static_functions": resolve_static_functions_mode(opts),
        "conflicting_drivers": resolve_conflicting_drivers_mode(opts),
    }
    if opts.frontend == "slang":
        try:
            plugin = resolve_plugin_path(opts.plugin_path, root_cfg)
        except FatalRtlBuddyError:
            plugin = opts.plugin_path
        fed["plugin_path"] = plugin
        fed["single_unit"] = opts.single_unit
    return fed


def slang_handles_params(opts: SynthToolOpts) -> bool:
    """Slang elaborates eagerly so top-level params are folded into
    read_slang; a subsequent chparam would be too late."""
    return opts.frontend == "slang"


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
        # The `-D` table `_write_script` actually fed the frontend:
        # filelist `+define+` entries with the run's `defines:` on top.
        # Recorded by the writer and read by `_phys_options`, the way
        # `_period_ps` is, so the digest is of what the script consumed
        # rather than of a re-derivation of it (#570).
        self._script_defines: dict[str, str | None] | None = None
        # The SDC's identity, taken as the script is generated and
        # confirmed when Yosys returns; see `_hash_constraints`. `None`
        # before the run, and for a run configured with no SDC (#570).
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
        """Yosys' machine-readable per-module `stat -json` dump (#558)."""
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

    def _parse_clock_period_ps(self, sdc_path: str) -> int | None:
        """Extract create_clock periods from SDC and return the minimum in picoseconds.

        ABC -D takes a single timing window; for multi-clock designs this is a
        workaround — the minimum period is used, which over-constrains slower domains.
        """
        periods = []
        try:
            with open(sdc_path) as f:
                for line in f:
                    m = re.search(r"create_clock\s+.*-period\s+([\d.]+)", line)
                    if m:
                        periods.append(float(m.group(1)))
        except OSError:
            return None
        if not periods:
            return None
        if len(periods) > 1:
            log_event(
                logger,
                logging.WARNING,
                "synth.sdc_multi_clock",
                synth=self.synth_cfg.get_name(),
                clocks=len(periods),
                periods_ns=periods,
                used_ns=min(periods),
                sdc=sdc_path,
            )
        return int(min(periods) * 1000)

    def _resolve_lib_paths(self) -> list[str]:
        extras = list(self.synth_cfg.get_lib_paths())
        platform = self.synth_cfg.get_platform()
        if not platform or self.root_cfg is None:
            return extras
        return [self.root_cfg.get_synth_platform_cfg(platform).get_path()] + extras

    def _parse_area_um2(self, log_text: str) -> float | None:
        m = re.search(r"Chip area for module[^:]*:\s*([\d.]+)", log_text)
        return float(m.group(1)) if m else None

    def _parse_gate_count(self, log_text: str) -> int | None:
        matches = re.findall(
            r"^\s+(\d+)\s+(?:[\d.]+(?:[Ee][+-]?\d+)?\s+)?cells$", log_text, re.MULTILINE
        )
        return int(matches[-1]) if matches else None

    def _parse_critical_path_ps(self, log_text: str) -> float | None:
        m = re.search(r"Delay\s*=\s*([\d.]+)\s*ps", log_text)
        return float(m.group(1)) if m else None

    def _resolve_opts(self) -> SynthToolOpts:
        """Tool options with per-synthesis overrides and the effort applied.

        Effort-level knobs take precedence over tool-level defaults but are
        outranked by per-synthesis ``tool_overrides``. Memoised because
        resolving the overrides emits validation warnings, and both the gates
        and the script writer need the same answer.
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
        self._opts = opts
        return opts

    def _scan_static_lifetimes(
        self, fl_path: str, opts: SynthToolOpts
    ) -> list[LifetimeFinding]:
        """Findings for the filelist's sources, or [] when the gate is off.

        The bare and ``-v`` entries of ``synth.f`` are the scan roots; headers
        they `` `include `` are followed through the filelist's ``+incdir+``
        entries. A ``-y`` library directory contributes files the filelist
        never names, so its contents stay outside the scan. Macros are the
        filelist's ``+define+`` entries plus the run's ``defines:``, matching
        the Yosys invocation exactly — see :func:`lifetime_scan_inputs`.
        """
        # Resolved before the mode check so the overridden-defines warning is
        # reported even when the gate itself is switched off.
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
            # Only slang honours --single-unit; with the verilog frontend the
            # flag is ignored (with a warning), so each file is its own
            # compilation unit either way.
            single_unit=opts.single_unit and opts.frontend == "slang",
            # slang's `undefineall` re-applies the -D macros; Yosys's own
            # read_verilog drops them along with everything else.
            undefineall_keeps_predefines=opts.frontend == "slang",
        )

    def _stat_json_cmd(self, liberty: str | None) -> str:
        """The `stat -json` line that feeds the phys model's module rows (#558).

        `stat -json` prints to the console rather than to a file, so it is
        wrapped in `tee -o` — the pattern Yosys' own help points at. `-q`
        keeps the JSON out of `synth.log`, which is scraped line by line for
        the design totals and for `ERROR:` lines; a JSON document in there
        would be noise at best.

        The `-liberty` copy of the human-readable `stat` above it is passed
        again here because that is what puts an `area` field on each module.
        Without a Liberty the dump still carries `num_cells`, so an unmapped
        run gets module rows with a null area rather than no rows at all.
        """
        liberty_arg = f" -liberty {liberty}" if liberty else ""
        return f"tee -q -o {self._stats_path()} stat -json{liberty_arg}"

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

        # Top-level params: chparam works for the legacy verilog frontend
        # (lazy elaboration). For slang they're already folded into
        # read_slang via -G, so skip the redundant pass.
        if params and not slang_handles_params(opts):
            for key, value in params.items():
                lines.append(f"chparam -set {key} {value} {top}")

        synth_cmd = f"synth -top {top}"
        if opts.synth_args:
            synth_cmd += f" {opts.synth_args}"
        lines.append(synth_cmd)
        # Unguarded immediate assertions survive synth as $assert/$assume/
        # $cover cells and get emitted into the netlist, which structural
        # Verilog readers (OpenROAD/OpenSTA `read_verilog` for pnr/power)
        # reject with a syntax error. Formal cells are not gates — strip them
        # all here; a no-op when the design carries none.
        lines.append("chformal -remove")

        if mapped:
            for lib in lib_paths:
                lines.append(f"dfflibmap -liberty {lib}")

            abc_cmd = f"abc -liberty {lib_paths[0]}"
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

            abc_script = (
                _ABC_SCRIPT_WITH_TIMING
                if period_ps is not None
                else _ABC_SCRIPT_NO_TIMING
            )
            abc_cmd += f' -script "+{abc_script}"'
            lines.append(abc_cmd)
            lines.append(f"write_verilog {self._netlist_path(mapped=True)}")
            lines.append(f"stat -liberty {lib_paths[0]}")
            lines.append(self._stat_json_cmd(lib_paths[0]))
        else:
            if opts.abc_args:
                lines.append(f"abc {opts.abc_args}")
            lines.append(f"write_rtlil {self._netlist_path()}")
            lines.append(self._stat_json_cmd(None))

        script = "\n".join(lines) + "\n"
        script_path = self._script_path()
        with open(script_path, "w") as f:
            f.write(script)
        return script_path

    def _hash_constraints(self) -> None:
        """Identify the SDC by its bytes, as the script is written (#570).

        The digest used to be computed inside the publish, minutes after
        Yosys had finished — so an SDC edited while the run worked, or
        replaced by a `rb pnr` writing over the same path, was hashed as
        the constraints this netlist was built under. That is the exact
        substitution the digest exists to catch, recorded as a match.

        Taken here instead, where the script is generated and where the
        ABC delay target beside it is parsed out of the same file, so the
        recorded identity is of the bytes the run actually read.
        `_confirm_constraints_unchanged` closes the other end: an SDC is
        a page of text, so reading it twice costs nothing and the window
        shuts from both sides.
        """
        self._constraints_sha256 = sha256_of(self.synth_cfg.get_constraints())

    def _confirm_constraints_unchanged(self) -> None:
        """Withdraw the SDC hash if the file moved under the run (#570).

        The other end of `_hash_constraints`. A digest computed here,
        after a synthesis that runs for minutes, identifies whatever is
        at the path now — an SDC a person edited while the run worked, or
        one a `rb pnr` rewrote — and records it as the constraints this
        netlist was built under, which is the substitution the digest
        exists to catch.
        :func:`~rtl_buddy.phys.publish.confirm_digest` says whether the
        bytes hashed at script generation are still there; a mismatch
        records ``null`` rather than a digest nothing can vouch for, and
        the warning keeps that null from reading as "this run had no
        constraints".
        """
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
        """Remove the previous run's netlists, before anything can return.

        `synth_netlist.v` / `synth.rtlil` are this flow's real product and the
        fixed-path *inputs* of `rb pnr` and `rb power`, which guard them with
        `isfile` only. A failed rerun would otherwise leave the last
        successful run's netlist in place, byte-identical, and the downstream
        commands would place, route and power-analyse it as though it were
        current (#469). Both spellings are cleared because which one the
        script writes depends on whether a Liberty resolved, and that can
        change between runs.

        This is the first action of `run()` so that *every* early return --
        a filelist error, and the static-lifetime and conflicting-driver
        gates, which fail before or without reading the netlist -- leaves no
        stale product behind.

        `synth_stat.json` goes with them for the same reason one step in: it
        is read back inside this same `run()` to build the phys model, and a
        Yosys that exits 0 without reaching its trailing `tee ... stat -json`
        would otherwise have the previous run's per-module areas published as
        this one's (#558).

        The model and its manifest are not *deleted* -- a run of the other
        flow may have merged its own half into them, and that half's
        artefacts are still on disk. They are edited instead: this flow's
        half is nulled out, because publication happens only on a pass and a
        rerun that fails here would otherwise leave the previous run's
        per-module rows discoverable with the `synth_stat.json` behind them
        already gone (#558).

        :returns: ``None``, or the reason the withdrawal did not happen —
            which every caller turns into a failed run, because the
            `synth_stat.json` behind the still-published half has just been
            deleted here. See
            :func:`~rtl_buddy.phys.publish.withdrawal_failure_desc`.
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
        """Null this flow's half of any model + manifest already here (#558).

        The counterpart of `_publish_phys_model`, called from the clear above
        so every path that ends without publishing -- a failure, a crash, a
        gate that returns early -- leaves no module rows standing over
        artefacts that have just been deleted. The power half is untouched.

        A withdrawal that *succeeded* is bookkeeping and logs at DEBUG. One
        that failed is not: the clear that called this has already deleted
        the `synth_stat.json` the published rows were read from, so they are
        now standing over nothing, and carrying on would let a run that dies
        before publishing leave them there. It warns and hands the reason
        back for the caller to fail on (#560).

        :returns: ``None`` on success, else `invalidate_half`'s ``error``.
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
        """Fail a run that has already invoked Yosys, publishing no netlist.

        Yosys writes `synth_netlist.v` / `synth.rtlil` partway through its
        script and only then runs the trailing `stat`, so it can crash — or
        log an `ERROR:` line — with the netlist already on disk. Returning a
        FAIL and leaving it there hands `rb pnr` and `rb power` a design at
        exactly the fixed path they resolve, from a synthesis that failed
        (#469). Every post-Yosys failure return goes through here so the two
        halves cannot drift apart, and so a new failure gate added to this
        method inherits the cleanup by using it.

        The clear withdraws this flow's published half as it goes, and a
        withdrawal it could not make is said out loud in the description this
        run already fails with: the run was over either way, but the user has
        to know the artefact directory still publishes module rows over the
        `synth_stat.json` just deleted (#560).
        """
        stale_error = self._clear_stale_netlists()
        if stale_error is not None:
            desc = f"{desc}; {withdrawal_failure_desc(stale_error)}"
        return SynthFailResults(name=self.name + "/results", desc=desc)

    def run(self) -> SynthResults:
        # The clear withdraws whatever this flow published here last time,
        # and nothing else starts if it could not: the `synth_stat.json`
        # behind those rows has just been deleted, so a run that went ahead
        # and then failed would leave a breakdown of a design this directory
        # no longer holds discoverable as a current one (#560).
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

        # Both gate modes are resolved up front, ahead of the filelist write,
        # so a misspelled value is the fatal config error it is on every run --
        # not something a FilelistError can mask into an ordinary FAIL. Matches
        # OpenRoadSynth.run().
        opts = self._resolve_opts()
        static_mode = resolve_static_functions_mode(opts)
        conflicting_mode = resolve_conflicting_drivers_mode(opts)
        # Same reason: an unknown frontend or a missing slang plugin is a
        # config error, and the gates below return before `_write_script()`
        # would have reached the same check inside emit_frontend_read_cmds().
        validate_frontend(opts, self.root_cfg)

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
            # Yosys already ran write_verilog/write_rtlil, so the netlist at
            # the fixed path is this failed run's own product — the
            # start-of-run cleanup only removed the previous one. Drop it, or
            # `rb pnr` / `rb power` would consume a netlist whose shared net
            # folded to x.
            return self._fail_after_yosys(
                f"{len(conflicting)} 'multiple conflicting drivers' "
                f"warning(s) in {log_path}"
            )

        area_um2 = self._parse_area_um2(log_text)
        gate_count = self._parse_gate_count(log_text)
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
            phys_model=phys_model,
        )

    def _phys_options(self, *, mapped: bool) -> dict:
        """The effective options the config fingerprint is digested over.

        What `_write_script` actually reads, not the whole resolved
        `SynthToolOpts`. A digest over the dataclass tells two runs apart
        by a field the generated script never looks at, which reports a
        difference the netlist cannot have -- the same rule that keeps the
        power flow's `tool_overrides` out of its own mapping (#568).

        Read off this backend's script writer, line by line:

        - `elaborate`: the frontend subset both backends share
          (:func:`elaboration_fingerprint`).
        - `synth_args`: the resolved value, which is the effort's
          `yosys.synth-args` unless a `tool_overrides.<tool>.synth_args`
          outranks it -- `_resolve_opts` has already folded that in.
        - `params` and `defines`: the elaboration values, which shape the
          netlist exactly as an ABC script does and are the knob a
          parameter sweep turns. `defines` is the **merged** table
          `_write_script` hands the frontend -- the filelist's `+define+`
          entries with the run's `defines:` layered on top
          (:func:`elaboration_defines`) -- and not `synth.yaml`'s field
          alone. The field alone digested a `+define+WIDTH=8` change in
          the generated filelist as no change at all, which is a
          different design under one fingerprint (#570); it also read two
          runs apart when one spelt a value the filelist already gave it.
        - `mapped`: which branch the script took, since the two consume
          different things below.

        The two branches differ in what they consume below. **Unmapped**
        emits `abc {opts.abc_args}`, so `abc_args` is fed. **Mapped**
        does not: it hard-codes `_ABC_SCRIPT_NO_TIMING` /
        `_ABC_SCRIPT_WITH_TIMING` and passes ABC the delay target parsed
        out of the SDC, so `abc_args` is dropped -- a mapped run that
        sets it runs identically to one that does not -- and the target
        (`_period_ps`, `null` when the SDC named no clock, which also
        selects the untimed script) is fed in its place. Mapped also feeds
        `libs`, the resolved Liberty set the script reads
        (:func:`library_fingerprint`): `mapped: true` alone said the run
        was mapped without saying what against, so two corners named
        through `lib-paths` digested identically (#570). The unmapped
        branch has none by definition -- that is what makes it unmapped.

        `strategy` is in neither: this backend has no OpenROAD stage and
        no script line reads it.

        Values only: the digest is of what the run resolved to, not of
        the files it resolved from, so two configs that spell one setting
        differently and come out the same are one experiment.
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
            fed["abc_period_ps"] = self._period_ps
            fed["libs"] = library_fingerprint(self._resolve_lib_paths(), self.root_cfg)
        else:
            fed["abc_args"] = opts.abc_args
        return fed

    def _digested_defines(self) -> dict:
        """The macro table the generated script fed the frontend (#570).

        Recorded by `_write_script`, not re-derived here: it is the table
        that was passed, and re-reading `synth.f` to rebuild it would
        digest a filelist that may since have been rewritten. The
        fallback is `synth.yaml`'s own field, for a caller that reaches
        this without a script having been written -- there is no
        published run in that state, so it is a floor and not a path.
        """
        if self._script_defines is not None:
            return dict(self._script_defines)
        return self.synth_cfg.get_defines()

    def _publish_phys_model(
        self, *, area_um2: float | None, gate_count: int | None, mapped: bool
    ) -> str | None:
        """Write `phys-model.json` + its manifest for a run that passed (#558).

        Never fails the synthesis: the per-module breakdown is a by-product
        of a run whose product -- the netlist -- is already on disk and
        already judged. A Yosys that skipped its `stat -json` line, or wrote
        something this cannot read, costs the model its `modules` rows and
        earns a warning; the totals scraped from the log are written either
        way, so the document still says what the design came to.

        The identity fields (#568) are the ones that shaped THIS netlist:
        the platform whose Liberty it mapped against, the effort actually
        applied, the SDC read for the ABC delay target, and the options
        the generated script actually consumed on the branch it took --
        everything a second experiment of the same design would differ
        in. See `_phys_options` for the branch-by-branch mapping.
        `_resolve_opts` is memoised, so asking it here costs nothing and
        cannot re-emit the override warnings it logs on first use.

        The SDC's hash is `_hash_constraints`', taken as the script was
        written and confirmed here, not computed here: a file edited or
        replaced while Yosys ran would otherwise be recorded as what this
        netlist was built under (#570).
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
