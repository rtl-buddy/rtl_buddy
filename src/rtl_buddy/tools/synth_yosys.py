import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

from . import block_params, icg_split
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
    resolve_clock_gating_max_flops,
    resolve_clock_gating_min_flops,
    resolve_conflicting_drivers_mode,
    resolve_static_functions_mode,
    resolve_unresolved_interfaces_mode,
)
from ..constraints.tcl_reader import read_constraints
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event, task_status
from ..phys.manifest import project_relative
from ..phys.reports import stat_json_span
from ..phys.publish import (
    confirm_digest,
    invalidate_half,
    publish_synth,
    sha256_of,
    withdrawal_failure_desc,
)
from ..process_utils import run_managed_process
from ..runner.synth_results import SynthFailResults, SynthPassResults, SynthResults

# Yosys' default Liberty script without `dc2`, which rebuilds log-depth carry networks as ripple
# chains, and without `&fraig -x`, whose SAT sweep (1M conflicts per node) does not finish on deep arithmetic.
DEFAULT_MAPPED_ABC_SCRIPT = (
    "strash; scorr; dretime; strash; &get -n; &dch -f; &nf {D}; &put"
)
# The default without `&dch -f`. `&nf` area recovery over `&dch` choices, under the module's one global
# required time, rebuilds prefix adders off that critical path as low-area ripple chains (#724).
DELAY_MAPPED_ABC_SCRIPT = "strash; scorr; dretime; strash; &get -n; &nf {D}; &put"
#: Names `abc-script` accepts in place of a script.
MAPPED_ABC_PRESETS = {
    "default": DEFAULT_MAPPED_ABC_SCRIPT,
    "delay": DELAY_MAPPED_ABC_SCRIPT,
}
# `synth -extra-map +/choices/<map>.v` requests a prefix-adder carry network (kogge-stone, sklansky, ...).
_CHOICES_MAP_RE = re.compile(r"(?:^|\s)-extra-map\s+\+/choices/\S+")
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


YOSYS_TMP_DIRNAME = "yosys-tmp"
#: Screen width `yosys-abc`'s readline assumes. Without `TERM` it assumes 80 columns and echoes a
#: long `source <TMPDIR>/.../abc.script` line scrolled sideways, so Yosys never sees the prompt it
#: waits for and both processes block (#797).
YOSYS_ABC_COLUMNS = "100000"


def yosys_process_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return `env` (default: this process's environment) with ``COLUMNS`` set for every Yosys launch (#797)."""
    return {**(os.environ if env is None else env), "COLUMNS": YOSYS_ABC_COLUMNS}


def yosys_env(artefact_dir: str) -> dict[str, str]:
    """Return the environment for a Yosys run, with ``TMPDIR`` set to the run's own directory.

    Yosys keeps temp files and its merged Liberty SCL cache under ``TMPDIR``; the default
    cache directory under ``/tmp`` belongs to whichever user created it first. ``COLUMNS``
    is set as :func:`yosys_process_env` sets it.
    """
    tmp_dir = os.path.join(os.path.abspath(artefact_dir), YOSYS_TMP_DIRNAME)
    os.makedirs(tmp_dir, exist_ok=True)
    return {**yosys_process_env(), "TMPDIR": tmp_dir}


#: Name of the link to `yosys-abc` that `abc-persistent: false` points ``abc.exe`` at (#826).
ABC_BATCH_LINK_NAME = "abc-batch"


def resolve_yosys_abc(yosys_executable: str) -> str:
    """Return the absolute path of the `yosys-abc` that `yosys_executable` runs.

    Yosys looks for `yosys-abc` in the directory of its own resolved binary, so that directory is
    tried first, then the directory `yosys_executable` names or is found in, then ``PATH``.
    Raises :class:`FatalRtlBuddyError` when none has an executable `yosys-abc`.
    """
    found = shutil.which(yosys_executable)
    dirs: list[str] = []
    if found:
        dirs.append(os.path.dirname(os.path.realpath(found)))
        dirs.append(os.path.dirname(os.path.abspath(found)))
    candidates = [os.path.join(d, "yosys-abc") for d in dict.fromkeys(dirs)]
    on_path = shutil.which("yosys-abc")
    if on_path:
        candidates.append(os.path.abspath(on_path))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    log_event(
        logger,
        logging.ERROR,
        "synth.yosys_abc_missing",
        yosys=yosys_executable,
        tried=candidates,
    )
    raise FatalRtlBuddyError(
        f"abc-persistent: false needs the yosys-abc that {yosys_executable!r} runs, "
        "but none was found next to it or on PATH "
        f"(tried: {', '.join(candidates) or 'nothing'}); install yosys-abc or set "
        "abc-persistent back to true"
    )


def abc_batch_cmds(
    opts: SynthToolOpts, yosys_executable: str, artefact_dir: str
) -> list[str]:
    """Return the Yosys script lines that make ABC run in batch mode, or ``[]`` with ``abc-persistent`` on.

    Yosys 0.64 reuses one `yosys-abc` process over a pipe only when ``abc.exe`` is its own
    `yosys-abc` path (``is_yosys_abc()``). Pointing ``abc.exe`` at a symlink to the same binary
    starts a fresh `yosys-abc -s -f <script>` for every ABC call, with the same mapping (#826).
    The link is ``<artefact_dir>/yosys-tmp/abc-batch``; with ``abc-persistent`` on, a stale one is removed.
    """
    tmp_dir = os.path.join(os.path.abspath(artefact_dir), YOSYS_TMP_DIRNAME)
    link = os.path.join(tmp_dir, ABC_BATCH_LINK_NAME)
    if opts.abc_persistent:
        if os.path.islink(link):
            os.remove(link)
        return []
    target = resolve_yosys_abc(yosys_executable)
    os.makedirs(tmp_dir, exist_ok=True)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)
    return [f"scratchpad -set abc.exe {link}"]


def sdc_clock_period_ps(
    sdc_path: str, time_unit_ps: Callable[[], float], synth_name: str
) -> int | None:
    """Return the minimum ``create_clock`` period in the SDC, in picoseconds, or None (:func:`sdc_clock_periods`)."""
    return sdc_clock_periods(sdc_path, time_unit_ps, synth_name)[0]


def sdc_clock_periods(
    sdc_path: str, time_unit_ps: Callable[[], float], synth_name: str
) -> tuple[int | None, int]:
    """Return ``(period_ps, unresolved)``: the minimum ``create_clock`` period in the SDC, in picoseconds, or None, and how many ``create_clock`` commands gave no period.

    Shared by both synthesis backends. Periods are in the Liberty `time_unit`, which
    ``time_unit_ps`` returns in picoseconds. ABC ``-D`` takes one timing window, so
    multi-clock designs use the minimum, which over-constrains slower domains. Periods
    are read via the constraint reader, so ``\\``-continued commands and ``{10.0}``
    work, ``source`` is followed relative to the sourcing file, and a file the interp
    refuses is retried with object queries answering as name lists and the file's own
    error checks skipped, so a proc or loop over ``[get_ports ...]`` is evaluated
    (#788). A period that is not a number (``$p`` under the tokenizer backend,
    or ``[get_property ...]``) is skipped with ``synth.sdc_period_unevaluated``, and
    a ``create_clock`` the tokenizer found inside a loop, ``proc`` or ``if`` body with
    ``synth.sdc_clock_unread``; both count as unresolved.
    """
    try:
        with open(sdc_path) as f:
            text = f.read()
    except OSError:
        return None, 0

    result = read_constraints(
        text,
        interest=frozenset({"create_clock"}),
        source=sdc_path,
        script_path=sdc_path,
        lenient_queries=True,
    )
    unresolved = 0
    for unread in result.unread:
        unresolved += 1
        log_event(
            logger,
            logging.WARNING,
            "synth.sdc_clock_unread",
            synth=synth_name,
            sdc=unread.file or sdc_path,
            line=unread.line,
            enclosing=unread.enclosing,
        )
    periods: list[float] = []
    for cmd in result.commands:
        value = _flag_value(cmd.words, "-period")
        if value is None:
            continue
        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1].strip()
        if value.startswith("$") or value.startswith("["):
            unresolved += 1
            log_event(
                logger,
                logging.WARNING,
                "synth.sdc_period_unevaluated",
                synth=synth_name,
                sdc=cmd.file or sdc_path,
                line=cmd.line,
                value=value,
            )
            continue
        try:
            periods.append(float(value))
        except ValueError:
            continue
    if not periods:
        return None, unresolved
    ps_per_unit = time_unit_ps()
    if len(periods) > 1:
        ns_per_unit = ps_per_unit / 1000.0
        log_event(
            logger,
            logging.WARNING,
            "synth.sdc_multi_clock",
            synth=synth_name,
            clocks=len(periods),
            periods_ns=[p * ns_per_unit for p in periods],
            used_ns=min(periods) * ns_per_unit,
            sdc=sdc_path,
        )
    return int(min(periods) * ps_per_unit), unresolved


def resolve_abc_period_ps(
    constraints: str | None,
    abc_script: str,
    time_unit_ps: Callable[[], float],
    synth_name: str,
) -> int | None:
    """Return the ABC ``-D`` delay target, in picoseconds, for a mapped run, or None.

    Shared by both synthesis backends. None without an SDC. An SDC that yields no
    period warns ``synth.sdc_no_clock``; its ``abc_delay_target`` field says whether
    ``abc_script`` writes ``{D}``, which then maps with no delay target (#788), and
    its ``unresolved`` field counts the ``create_clock`` commands the reader found
    but could not resolve.
    """
    if not constraints:
        return None
    period_ps, unresolved = sdc_clock_periods(constraints, time_unit_ps, synth_name)
    if period_ps is not None:
        log_event(
            logger,
            logging.DEBUG,
            "synth.sdc_period",
            synth=synth_name,
            period_ps=period_ps,
            sdc=constraints,
        )
    else:
        log_event(
            logger,
            logging.WARNING,
            "synth.sdc_no_clock",
            synth=synth_name,
            sdc=constraints,
            abc_delay_target="{D}" in abc_script,
            unresolved=unresolved,
        )
    return period_ps


def abc_period_arg(period_ps: int | None) -> str:
    """Return the `` -D <ps>`` argument of a mapped ``abc`` command, or ``""`` without a period."""
    return "" if period_ps is None else f" -D {period_ps}"


#: The ABC constraints file a mapped run writes when its platform sets a driving cell or output load.
ABC_CONSTR_FILENAME = "abc.constr"


def resolve_abc_constr(synth_cfg, root_cfg) -> dict | None:
    """Return the platform's ``{"driving_cell", "output_load"}`` for ``abc -constr``, or None when it sets neither (#787)."""
    platform = synth_cfg.get_platform()
    if not platform or root_cfg is None:
        return None
    platform_cfg = root_cfg.get_synth_platform_cfg(platform)
    get_cell = getattr(platform_cfg, "get_abc_driving_cell", None)
    get_load = getattr(platform_cfg, "get_abc_output_load", None)
    cell = get_cell() if get_cell is not None else ""
    load = get_load() if get_load is not None else None
    if not cell and load is None:
        return None
    return {"driving_cell": cell or None, "output_load": load}


def abc_constr_arg(constr: dict | None, artefact_dir: str) -> str:
    """Write ``abc.constr`` for `constr` and return the `` -constr <file>`` argument, or ``""`` for None.

    Yosys runs ``read_constr`` on the file ahead of the ABC script, so the driving cell and output
    load reach ``buffer``, ``upsize``, ``dnsize`` and ``stime`` in any ``abc-script``.
    """
    path = os.path.join(os.path.abspath(artefact_dir), ABC_CONSTR_FILENAME)
    if constr is None:
        # A stale file would mislead someone reading the artefacts.
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return ""
    lines = []
    if constr["driving_cell"]:
        lines.append(f"set_driving_cell {constr['driving_cell']}")
    if constr["output_load"] is not None:
        lines.append(f"set_load {constr['output_load']:g}")
    Path(path).write_text("\n".join(lines) + "\n")
    return f" -constr {path}"


_ABC_DRIVING_CELL_MISSING_RE = re.compile(
    r"Cannot find the default PI driving cell \((?P<cell>[^)]*)\)"
)


def warn_abc_driving_cell_missing(
    log_text: str, constr: dict | None, synth_name: str, log_path: str
) -> None:
    """Warn when ABC did not find the platform's ``abc-driving-cell`` in the mapped library; ABC only logs it and carries on."""
    if not constr or not constr.get("driving_cell"):
        return
    m = _ABC_DRIVING_CELL_MISSING_RE.search(log_text)
    if m:
        log_event(
            logger,
            logging.WARNING,
            "synth.abc_driving_cell_missing",
            synth=synth_name,
            cell=m.group("cell"),
            log=log_path,
        )


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


def yosys_read_lib_paths(
    lib_paths: list[str], blocks, source_files: list[str], synth_name: str
) -> list[str]:
    """Return the Liberty files the Yosys script reads, without the abstracts of `blocks:` the sources stub with parameters.

    A block's abstract Liberty defines its module with no parameters. Read ahead of the sources, it replaces a parameterised `(* blackbox *)` stub, and Yosys then rejects an instance's override (`does not have a parameter named`). Such a stub is the blackbox Yosys needs, so the abstract is left out. Any other definition of the module, such as a simulation model under `` `ifndef SYNTHESIS ``, does not count: the abstract stays, as it does for a stub without parameters.
    """
    if not blocks:
        return list(lib_paths)
    stubbed_names: set[str] = set()
    for src in source_files:
        try:
            with open(src, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "blackbox" not in text:
            continue
        stubbed_names |= {
            bb.name for bb in block_params.blackbox_modules(text) if bb.parameterised
        }
    stubbed = [b for b in blocks if b.ref.name in stubbed_names]
    if not stubbed:
        return list(lib_paths)
    log_event(
        logger,
        logging.DEBUG,
        "synth.block_liberty_skipped",
        synth=synth_name,
        blocks=[b.ref.name for b in stubbed],
    )
    skip = {b.lib for b in stubbed}
    return [p for p in lib_paths if p not in skip]


def clean_block_netlist(netlist_path: str, blocks, synth_name: str) -> str | None:
    """Strip the parameter overrides from `blocks:` instances in the mapped netlist, in place.

    Returns a failure description when an override differs from the block's synthesis or the netlist cannot be rewritten, else None. See :mod:`.block_params`.
    """
    if not blocks or not os.path.isfile(netlist_path):
        return None
    try:
        _, instances = block_params.clean_netlist(netlist_path, netlist_path, blocks)
    except block_params.BlockParamError as e:
        log_event(
            logger,
            logging.ERROR,
            "synth.block_params_mismatch",
            synth=synth_name,
            error=str(e),
        )
        return str(e)
    except OSError as e:
        return f"could not strip block parameter overrides from {netlist_path}: {e}"
    if instances:
        log_event(
            logger,
            logging.INFO,
            "synth.block_params_stripped",
            synth=synth_name,
            instances=[f"{i.module}:{i.instance}" for i in instances],
        )
    return None


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

    The resolved ``abc_script``, expanded when it names a :data:`MAPPED_ABC_PRESETS` entry. When it
    is empty: the ``delay`` preset if the resolved ``synth_args`` (the arguments the run gives Yosys
    ``synth``) request a ``+/choices/`` carry map, else the ``default`` preset.
    """
    script = (opts.abc_script or "").strip()
    if not script:
        if _CHOICES_MAP_RE.search(opts.synth_args or ""):
            return DELAY_MAPPED_ABC_SCRIPT
        return DEFAULT_MAPPED_ABC_SCRIPT
    if script in MAPPED_ABC_PRESETS:
        return MAPPED_ABC_PRESETS[script]
    if '"' in script or "\n" in script:
        raise FatalRtlBuddyError(
            f"synth option abc-script must be one line without double quotes, "
            f"got {script!r}; separate ABC commands with ';'"
        )
    return script


def resolve_clock_gating(
    opts: SynthToolOpts, synth_cfg, root_cfg, *, warn: bool = False
) -> dict | None:
    """Return ``{"min_flops", "cell"}`` for a run that gates clocks, or None when ``clock-gating-min-flops`` is 0 (#786).

    ``cell`` is the platform's ``clock-gating-cell`` or None, in which case ``clockgate -liberty`` picks the ICG.
    ``max_flops`` is present only when ``clock-gating-max-flops`` is set (#854). With ``warn``, a
    ``clock-gating-max-flops`` without ``clock-gating-min-flops`` warns ``synth.clock_gating_max_ignored``.
    """
    min_flops = resolve_clock_gating_min_flops(opts)
    max_flops = resolve_clock_gating_max_flops(opts)
    if not min_flops:
        if max_flops and warn:
            log_event(
                logger,
                logging.WARNING,
                "synth.clock_gating_max_ignored",
                synth=synth_cfg.get_name(),
                max_flops=max_flops,
            )
        return None
    cell = ""
    platform = synth_cfg.get_platform()
    if platform and root_cfg is not None:
        get_cell = getattr(
            root_cfg.get_synth_platform_cfg(platform), "get_clock_gating_cell", None
        )
        cell = get_cell() if get_cell is not None else ""
    gating = {"min_flops": min_flops, "cell": cell or None}
    if max_flops:
        gating["max_flops"] = max_flops
    return gating


#: The Yosys lines that make each flop whose synchronous reset has priority over its enable (`$_SDFFE_*`) one whose enable has priority (`$_SDFFCE_*`), enabled by its enable or its reset, so `clockgate` keeps the reset when it gates the enable (#938). `opt_merge` and `opt_clean` merge the enable gates, so flops that shared an enable and a reset share the new enable.
SDFFE_AS_SDFFCE_CMDS = (
    "dfflegalize -cell $_SDFFCE_????_ 01 t:$_SDFFE_*",
    "opt_merge",
    "opt_clean",
)

#: Where `clock_gating_cmds` round-trips the design through `icg_split`, below the artefact directory.
ICG_SPLIT_RTLIL = ("clockgate.il", "clockgate_split.il")


def icg_split_cmds(max_flops: int, artefact_dir: str) -> list[str]:
    """Return the Yosys lines that split each ICG driving more than ``max_flops`` flops (#854).

    The design goes out as RTLIL, :mod:`rtl_buddy.tools.icg_split` rewrites it, and it comes back
    in place of the old one, so `dfflibmap`, `abc`, `stat` and the netlist all see the copies.
    RTLIL, unlike JSON, keeps every parameter's type, so cells that are not split come back unchanged.
    """
    tmp_dir = os.path.join(os.path.abspath(artefact_dir), YOSYS_TMP_DIRNAME)
    os.makedirs(tmp_dir, exist_ok=True)
    source, target = (os.path.join(tmp_dir, name) for name in ICG_SPLIT_RTLIL)
    helper = os.path.abspath(icg_split.__file__)
    command = " ".join(
        _exec_quote(word)
        for word in (sys.executable, "-I", helper, source, target, str(max_flops))
    )
    return [
        f"write_rtlil {_ys_quote(source)}",
        f"exec -expect-return 0 -- {command}",
        "design -reset",
        f"read_rtlil {_ys_quote(target)}",
    ]


def _ys_quote(path: str) -> str:
    """Double-quote a path for a Yosys command, so whitespace stays one argument."""
    return f'"{path}"'


def _exec_quote(word: str) -> str:
    """Quote one word of a Yosys ``exec`` command for its shell.

    Yosys keeps a double-quoted word whole, with its quotes and backslashes, and joins the words
    with single spaces, so a word is double-quoted with the shell's own escapes.
    """
    return '"' + re.sub(r'([\\"$`])', r"\\\1", word) + '"'


#: A line `icg_split` prints for an over-full ICG it left alone; Yosys logs it.
_ICG_SPLIT_WARNING_RE = re.compile(
    "^" + re.escape(icg_split.WARNING_PREFIX) + r"(?P<detail>.*)$", re.MULTILINE
)


def warn_icg_split_skipped(log_text: str, synth_name: str, log_path: str) -> None:
    """Warn ``synth.icg_not_split`` for each over-full ICG `icg_split` could not split."""
    for m in _ICG_SPLIT_WARNING_RE.finditer(log_text):
        log_event(
            logger,
            logging.WARNING,
            "synth.icg_not_split",
            synth=synth_name,
            detail=m.group("detail").rstrip(),
            log=log_path,
        )


def clock_gating_cmds(
    gating: dict | None,
    cell_libs: str,
    dont_use: str,
    synth_name: str,
    mapped: bool,
    artefact_dir: str,
) -> list[str]:
    """Return the ``clockgate`` lines a run adds between ``chformal -remove`` and ``dfflibmap``, or ``[]``.

    The ICG comes from the platform's ``clock-gating-cell`` (``-pos``), else ``-liberty`` picks one from
    the cell libraries, skipping ``dont-use-cells``. Only rising-edge flops are gated. :data:`SDFFE_AS_SDFFCE_CMDS`
    runs first, so a flop whose synchronous reset has priority over its enable is gated on its enable or its
    reset. With ``max_flops``, :func:`icg_split_cmds` follows. An unmapped run has no ICG to map to, so it warns
    ``synth.clock_gating_ignored`` and gates nothing.
    """
    if gating is None:
        return []
    if not mapped:
        log_event(
            logger,
            logging.WARNING,
            "synth.clock_gating_ignored",
            synth=synth_name,
            min_flops=gating["min_flops"],
        )
        return []
    lines = list(SDFFE_AS_SDFFCE_CMDS)
    if gating["cell"]:
        lines.append(
            f"clockgate -pos {gating['cell']} -min_net_size {gating['min_flops']}"
        )
    else:
        lines.append(
            f"clockgate{cell_libs}{dont_use} -min_net_size {gating['min_flops']}"
        )
    if gating.get("max_flops"):
        lines.extend(icg_split_cmds(gating["max_flops"], artefact_dir))
    return lines


def apply_effort(
    opts: SynthToolOpts, overrides: dict | None, effort_cfg: SynthEffortConfig
) -> SynthToolOpts:
    """Set the effort's ``synth_args``, ``abc_args``, ``abc_script`` and clock-gating limits on `opts` and return it.

    Shared by both synthesis backends. An effort value replaces the tool config's but not a key
    the run's `overrides` set, so precedence is run override, then effort, then tool config.
    """
    for key, value in (
        ("synth_args", effort_cfg.get_yosys_synth_args()),
        ("abc_args", effort_cfg.get_yosys_abc_args()),
        ("abc_script", effort_cfg.get_yosys_abc_script()),
        ("clock_gating_min_flops", effort_cfg.get_yosys_clock_gating_min_flops()),
        ("clock_gating_max_flops", effort_cfg.get_yosys_clock_gating_max_flops()),
    ):
        if value and (not overrides or key not in overrides):
            setattr(opts, key, value)
    return opts


def clean_stat_json(path: str) -> None:
    """Rewrite a ``tee -q -o <path> stat -json`` capture to the JSON object alone.

    Shared by both synthesis backends. ``tee`` writes everything ``stat`` logs, so a gzipped
    Liberty's ``Found gzip magic ...`` notice lands ahead of the JSON and the file does not parse
    (#710). A file that is already only the object, or holds none, is left as it is.
    """
    try:
        text = Path(path).read_text()
    except OSError:
        return
    span = stat_json_span(text)
    if span is None:
        return
    start, end = span
    if not text[:start].strip() and not text[end:].strip():
        return
    Path(path).write_text(text[start:end] + "\n")


def warn_mapped_abc_args(opts: SynthToolOpts, synth_name: str) -> None:
    """Warn that a Liberty-mapped run drops the resolved ``abc_args``, and note an automatic ``delay`` preset."""
    if not (opts.abc_script or "").strip() and _CHOICES_MAP_RE.search(
        opts.synth_args or ""
    ):
        # An upgrade changes the script under an unchanged config (#724), so say so in the run output.
        log_event(logger, logging.INFO, "synth.abc_delay_preset", synth=synth_name)
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
        # The platform's `abc -constr` settings `_write_script` used; None without them.
        self._abc_constr: dict | None = None
        self._ps_per_unit: float | None = None
        # The `-D` table `_write_script` fed the frontend, read by `_phys_options`.
        self._script_defines: dict[str, str | None] | None = None
        # SDC identity; see `_hash_constraints`. None before the run or with no SDC.
        self._constraints_sha256: str | None = None
        self._opts: SynthToolOpts | None = None
        # Resolved `blocks:` abstracts, set by the runner.
        self.blocks: list = []

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
        """Return the SDC's minimum ``create_clock`` period in picoseconds, or None (:func:`sdc_clock_period_ps`)."""
        return sdc_clock_period_ps(
            sdc_path, self._time_unit_ps, self.synth_cfg.get_name()
        )

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
        # The run's `tool:`, not this tool config's name: a `tool: openroad` run that falls back here
        # with the `yosys` entry still reads `tool_overrides.openroad`.
        overrides = self.synth_cfg.get_yosys_stage_overrides(
            self.synth_cfg.get_tool_name()
        )
        opts = apply_effort(
            self.tool_cfg.get_opts(overrides), overrides, self.effort_cfg
        )
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

        source_files = self._source_files_from_filelist(fl_path)
        lines = abc_batch_cmds(opts, self.tool_cfg.get_executable(), self.artefact_dir)
        for lib in yosys_read_lib_paths(
            lib_paths, self.blocks, source_files, self.synth_cfg.get_name()
        ):
            lines.append(f"read_liberty -lib {lib}")

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

        dont_use = dont_use_args(resolve_dont_use_cells(self.synth_cfg, self.root_cfg))
        cell_libs = liberty_args(self._resolve_cell_lib_paths())
        lines.extend(
            clock_gating_cmds(
                resolve_clock_gating(opts, self.synth_cfg, self.root_cfg, warn=True),
                cell_libs,
                dont_use,
                self.synth_cfg.get_name(),
                mapped,
                self.artefact_dir,
            )
        )

        if mapped:
            lines.append(f"dfflibmap{dont_use}{cell_libs}")

            abc_script = mapped_abc_script(opts)
            period_ps = resolve_abc_period_ps(
                self.synth_cfg.get_constraints(),
                abc_script,
                self._time_unit_ps,
                self.synth_cfg.get_name(),
            )
            self._abc_constr = resolve_abc_constr(self.synth_cfg, self.root_cfg)
            abc_cmd = (
                f"abc{cell_libs}{dont_use}"
                f"{abc_constr_arg(self._abc_constr, self.artefact_dir)}"
                f"{abc_period_arg(period_ps)}"
            )
            self._period_ps = period_ps

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
                    env=yosys_env(self.artefact_dir),
                )
        clean_stat_json(self._stats_path())

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

        warn_abc_driving_cell_missing(
            log_text, self._abc_constr, self.synth_cfg.get_name(), log_path
        )
        warn_icg_split_skipped(log_text, self.synth_cfg.get_name(), log_path)

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

        # OpenROAD rejects the `#(...)` Yosys leaves on a parameterised block's instances.
        block_error = clean_block_netlist(
            self._netlist_path(mapped=True), self.blocks, self.synth_cfg.get_name()
        )
        if block_error is not None:
            return self._fail_after_yosys(block_error)

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
          `libs` (:func:`library_fingerprint`), `dont_use`, `abc_constr`
          (:func:`resolve_abc_constr`) when the platform sets one, and
          `clock_gating` (:func:`resolve_clock_gating`) when it is on.
        - unmapped branch: `abc_args`. Mapped runs ignore `abc_args`.
        - `abc_persistent: false` when batch ABC is on (:func:`abc_batch_cmds`); absent otherwise.

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
            # Absent without a driving cell or load, so such a run keeps its digest.
            constr = resolve_abc_constr(self.synth_cfg, self.root_cfg)
            if constr is not None:
                fed["abc_constr"] = constr
            gating = resolve_clock_gating(opts, self.synth_cfg, self.root_cfg)
            if gating is not None:
                fed["clock_gating"] = gating
        else:
            fed["abc_args"] = opts.abc_args
        # Absent at the default, so runs that keep persistent ABC keep their digest.
        if not opts.abc_persistent:
            fed["abc_persistent"] = False
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


#: The wrapper top the parameter probe elaborates around a block's top.
_PROBE_TOP = "__rb_param_probe"


def _probe_opts(synth_cfg, root_cfg) -> tuple[SynthToolOpts, str]:
    """Return the Yosys frontend options a synthesis elaborates with, as its backend resolves them, and the Yosys executable."""
    tool_name = synth_cfg.get_tool_name()
    try:
        tool_cfg = root_cfg.get_synth_tool_cfg("yosys")
    except FatalRtlBuddyError:
        tool_cfg = root_cfg.get_synth_tool_cfg(tool_name)
    overrides = synth_cfg.get_yosys_stage_overrides(tool_name)
    return tool_cfg.get_opts(overrides), tool_cfg.get_executable()


def _probe_value(value) -> str:
    """Return a `params:` value as the probe wrapper's override expression: numbers as written, other strings quoted."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        len(text) >= 2 and text[0] == '"' and text[-1] == '"'
    ) or block_params.is_number(text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _probe_lib_paths(synth_cfg, root_cfg, sources: list[str]) -> list[str]:
    """Return the Liberty files the block's synthesis reads, so the probe knows the cells and sub-blocks its top instances."""
    libs = list(getattr(synth_cfg, "get_lib_paths", lambda: [])())
    platform = getattr(synth_cfg, "get_platform", lambda: None)()
    if platform:
        try:
            libs = root_cfg.get_synth_platform_cfg(platform).get_paths() + libs
        except Exception:  # the probe is best-effort; the synthesis already ran
            pass
    nested = []
    refs = getattr(synth_cfg, "get_blocks", lambda: [])()
    if refs:
        from .pnr_abstract import BlockResolutionError, resolve_blocks

        try:
            nested = resolve_blocks(refs)
        except BlockResolutionError:
            nested = []
        libs += [b.lib for b in nested]
    return yosys_read_lib_paths(libs, nested, sources, synth_cfg.get_top())


def probe_block_parameters(synth_cfg, root_cfg, synth_dir: str, out_dir: str):
    """Elaborate a block's top in Yosys and return its parameter values as a `block_params.ParamRecord`.

    Run when the block is hardened, so a parent's instance overrides can be compared with every value the block was built with. The top is wrapped in an instance carrying the synthesis's `params:`, in the synthesis's frontend, reading the sources and defines of its `synth.f`:

    - slang: the top is a blackbox (`--blackboxed-module`), so only its header is elaborated, and the wrapper instance carries every parameter, localparams included, as a parent's would.
    - verilog: the wrapper's hierarchy is elaborated and the derived module's parameters are read, plus any `real` override from the wrapper's cell, which the derived module leaves out. Localparams and `real` defaults are not available, so the record is marked incomplete.

    The synthesis's Liberty files are read first, so the top's cells and sub-blocks are known; with slang, unknown modules are also ignored, since only the header matters. String `params:` are quoted. The parameter order for positional overrides is read from the top's declaration in the sources, and so are the names the top declares as parameters and localparams in its own scope: only those are recorded, never a generate loop's genvar or a localparam of a generate or procedural block. Raises RuntimeError when the probe cannot run or its output is unusable.
    """
    top = synth_cfg.get_top()
    out_dir = os.path.abspath(out_dir)
    fl_path = os.path.join(synth_dir, "synth.f")
    if not os.path.isfile(fl_path):
        raise RuntimeError(f"no synthesis filelist at {fl_path}; run rb synth first")
    opts, executable = _probe_opts(synth_cfg, root_cfg)
    from .pnr_abstract import filelist_sources

    sources = filelist_sources(fl_path)
    os.makedirs(out_dir, exist_ok=True)
    wrapper = os.path.join(out_dir, "param_probe.sv")
    params = synth_cfg.get_params() or {}
    overrides = ", ".join(f".{k}({_probe_value(v)})" for k, v in params.items())
    Path(wrapper).write_text(
        f"module {_PROBE_TOP};\n  {top} {'#(' + overrides + ') ' if overrides else ''}u_probe ();\nendmodule\n"
    )
    json_path = os.path.join(out_dir, "param_probe.json")
    overrides_json = os.path.join(out_dir, "param_probe_overrides.json")
    cmds = [
        f"read_liberty -lib {shlex.quote(lib)}"
        for lib in _probe_lib_paths(synth_cfg, root_cfg, sources)
    ]
    cmds += emit_frontend_read_cmds(
        opts=opts,
        source_files=[*sources, wrapper],
        top=_PROBE_TOP,
        defines=elaboration_defines(fl_path, synth_cfg.get_defines()),
        params=None,
        root_cfg=root_cfg,
        incdirs=incdirs_from_filelist(fl_path),
    )
    if opts.frontend == "slang":
        cmds[-1] = cmds[-1].replace(
            f"--top {_PROBE_TOP}",
            # Only the top's header matters: a module its body uses but no source or Liberty defines must not stop the probe.
            f"--top {_PROBE_TOP} --blackboxed-module {shlex.quote(top)}"
            " --ignore-unknown-modules",
            1,
        )
    else:
        # The wrapper (read last) is elaborated on read, so its cell still carries the override values before `hierarchy` derives the top: Yosys leaves `real` parameters out of the derived module's defaults.
        cmds[-1] = cmds[-1].replace("read_verilog -sv -defer", "read_verilog -sv", 1)
        cmds += [
            f"select {_PROBE_TOP}",
            f"write_json -selected {shlex.quote(overrides_json)}",
            "select -clear",
            f"hierarchy -top {_PROBE_TOP}",
            "proc",
        ]
    cmds.append(f"write_json {shlex.quote(json_path)}")
    script = os.path.join(out_dir, "param_probe.ys")
    Path(script).write_text("\n".join(cmds) + "\n")
    log_path = os.path.join(out_dir, "param_probe.log")
    with open(log_path, "w") as log_f:
        result = subprocess.run(
            [executable, "-q", "-s", script],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=out_dir,
            env=yosys_env(out_dir),
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"Yosys exited with code {result.returncode}; see {log_path}"
        )
    try:
        design = json.loads(Path(json_path).read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError(f"unreadable probe output {json_path}: {e}") from None
    modules = design.get("modules") or {}
    if opts.frontend == "slang":
        cell = (modules.get(_PROBE_TOP, {}).get("cells") or {}).get("u_probe")
        if not cell:
            raise RuntimeError(f"the probe found no instance of {top!r}")
        values = cell.get("parameters") or {}
        complete = True
    else:
        # The wrapper's cell names the derived module, `$paramod\\blk\\W=...` or `$paramod$<sha1>\\blk`.
        cell = (modules.get(_PROBE_TOP, {}).get("cells") or {}).get("u_probe") or {}
        derived = modules.get(cell.get("type", ""))
        if derived is None:
            pattern = re.compile(rf"^\$paramod(\$[0-9a-f]+)?\\{re.escape(top)}(\\|$)")
            derived = next(
                (
                    m
                    for name, m in modules.items()
                    if name == top or pattern.match(name)
                ),
                None,
            )
        if derived is None:
            raise RuntimeError(f"the probe did not elaborate {top!r}")
        values = derived.get("parameter_default_values") or {}
        # A `real` parameter is only in the wrapper cell's overrides, as Yosys's string ("2.500000"); one left at its default is not recorded.
        try:
            before = json.loads(Path(overrides_json).read_text())
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"unreadable probe output {overrides_json}: {e}"
            ) from None
        wrapper_cell = (
            ((before.get("modules") or {}).get(_PROBE_TOP) or {}).get("cells") or {}
        ).get("u_probe") or {}
        values = {**(wrapper_cell.get("parameters") or {}), **values}
        complete = False
    order = None
    declared = None
    for src in sources:
        try:
            text = Path(src).read_text(errors="replace")
        except OSError:
            continue
        if top in text:
            order = block_params.parameter_port_names(text, top)
            if order is not None:
                declared = block_params.module_scope_parameter_names(text, top)
                break
    if declared is not None:
        # yosys-slang's blackbox cell carries every parameter of the top's body, nested scopes included: each generate loop iteration's genvar and each generate or procedural block's localparams. Keep the top's own.
        values = {k: v for k, v in values.items() if k in declared}
    return block_params.ParamRecord(
        module=top,
        frontend=opts.frontend,
        complete=complete,
        parameters={
            k: block_params.yosys_json_literal(str(v))
            for k, v in sorted(values.items())
        },
        order=order or [],
    )
