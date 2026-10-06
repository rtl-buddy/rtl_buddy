# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Simulation compile, run and grading for Verilog testbenches."""

import contextlib
import fnmatch
import hashlib
import json
import os
import platform
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import logging
import threading
import types
from dataclasses import dataclass, field
from stat import S_ISREG

logger = logging.getLogger(__name__)
from ..hooks import exec_hook_script
from ..seed_mode import SeedMode
from ..config.rtl import expand_compile_opts

from .vlog_filelist import VlogFilelist
from .vlog_post import VlogPost
from .vlog_post import UvmVlogPost
from .vlog_post import grade_unknown_sim_exit
from .vlog_cov import VlogCov
from .artifact_paths import (
    ARTIFACT_DIRNAME,
    BUILD_DIR_PREFIX,
    DISPATCH_OUTPUT_PATTERNS,
    RESULT_JSON_NAME,
    SHARED_BUILDS_DIRNAME,
    atomic_tmp_name,
    atomic_tmp_patterns,
    run_artifact_root,
    shared_build_dir,
    shared_build_namespace,
    test_artifact_dir,
    test_build_dir_name,
)

import time
import pprint
from pathlib import Path

from ..artifact_lock import build_dir_lock
from ..dispatch.base import (
    BUILD_PHASE_BUILD,
    BUILD_PHASE_FULL,
    BUILD_PHASE_VERILATE,
)
from ..errors import FatalRtlBuddyError
from ..logging_utils import (
    DEFAULT_FILE_LOG,
    log_console_event,
    log_event,
    task_status,
)
from ..runner.result_io import build_compile_fail_desc, load_build_result_json
from ..process_utils import run_managed_process
from .vcs_license import VcsLicenseQueueMonitor, has_license_queue_marker


def force_symlink(target, link_name):
    """Atomically repoint ``link_name`` at ``target``.

    Concurrent writers (the elements of a dispatched array share suite-level links)
    must not race, so this renames a uniquely named temporary link over the target
    instead of remove-then-create.
    """
    tmp = atomic_tmp_name(link_name)
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link_name)
    except OSError:
        # Don't leak the temp link into the suite dir if the rename fails.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Sentinel for "argument not given", where None is a meaningful value (see VlogSim.pre).
_UNSET = object()

# Stamp written into a shared build dir after a successful compile. It records the
# compile inputs the simv was built from.
# Defined in `artifact_paths` and re-exported here.
from .artifact_paths import SHARED_BUILD_STAMP_NAME as SHARED_BUILD_STAMP_NAME

# First line of the ``compile.log`` that a reuse leaves. It tells a reuse breadcrumb
# from a real compile transcript.
_REUSE_TRANSCRIPT_MARKER = "Compile skipped: reused the build already in "

# Separates a reuse breadcrumb from the compile transcript it preserves below itself.
_CARRIED_TRANSCRIPT_HEADER = (
    "\n=== transcript of the compile that last wrote this file ===\n"
)

# The `.retry.` transcript is written by a dispatched sim job that recompiles after
# finding its build's stamp invalid.
# It must not replace the build job's `compile.log`.
COMPILE_TRANSCRIPT_NAME = "compile.log"
COMPILE_RETRY_TRANSCRIPT_NAME = "compile.retry.log"

# Names of a test's other artefact outputs. The shared-build stamp uses them to exclude
# rtl_buddy's own outputs from a directory listing.
FILELIST_NAME = "run.f"
TEST_LOG_NAME = "test.log"
TEST_ERR_NAME = "test.err"
TEST_RANDSEED_NAME = "test.randseed"
COVERAGE_DAT_NAME = "coverage.dat"
SIMV_NAME = "simv"
ICARUS_SNAPSHOT_NAME = "simv.vvp"

# Per-run outputs a run removes before PRE (see `VlogSim.clear_run_outputs`).
_RUN_OUTPUT_NAMES = (
    TEST_LOG_NAME,
    TEST_ERR_NAME,
    COVERAGE_DAT_NAME,
    COMPILE_RETRY_TRANSCRIPT_NAME,
    RESULT_JSON_NAME,
)

# Simulator families whose compile output can be redirected into a shared build dir.
# Other families compile inside each test's own artefact dir.
SHARE_BUILD_FAMILIES = frozenset({"verilator", "vcs", "icarus"})


@dataclass(frozen=True)
class _TopFlagSpec:
    """How one simulator family spells "elaborate from this module".

    ``emit`` is the spelling rtl_buddy writes. ``aliases`` is every spelling that
    counts as a top already pinned by the user, so rtl_buddy does not append a
    second, winning one.
    ``glued`` lists prefixes whose value may be attached to the flag (``iverilog
    -stb``).
    """

    emit: str
    aliases: tuple[str, ...]
    glued: tuple[str, ...] = ()


# Top-selection flag per simulator family, so a testbench's `toplevel:` decides the
# elaboration root.
# A family absent here keeps its default top election.
TOP_MODULE_FLAGS = {
    "verilator": _TopFlagSpec(
        emit="--top-module",
        aliases=("--top-module", "-top-module", "--top", "-top"),
    ),
    "vcs": _TopFlagSpec(emit="-top", aliases=("-top",)),
    "icarus": _TopFlagSpec(emit="-s", aliases=("-s",), glued=("-s",)),
}


def _find_configured_top(spec, opts):
    """The top pinned by the configured opts, or ``None``.

    Returns ``(flag_as_written, module_or_None)`` for the LAST occurrence, which the
    simulator honours. The module is ``None`` for a bare flag.
    """
    found = None
    for index, token in enumerate(opts):
        if token in spec.aliases:
            nxt = opts[index + 1] if index + 1 < len(opts) else None
            # A module name never starts with `-`; anything that does is the next
            # option.
            value = nxt if (nxt and not nxt.startswith("-")) else None
            found = (token, value)
            continue
        for prefix in spec.glued:
            if token.startswith(prefix) and len(token) > len(prefix):
                found = (prefix, token[len(prefix) :])
                break
    return found


# Conflicts already warned about, keyed by (family, configured top, declared toplevel).
# A suite of N tests over one builder warns once.
_TOPLEVEL_CONFLICTS_LOCK = threading.Lock()
_TOPLEVEL_CONFLICTS: set[tuple] = set()


def _claim_toplevel_conflict(key: tuple) -> bool:
    """Return True the first time this process sees ``key``, and claim it."""
    with _TOPLEVEL_CONFLICTS_LOCK:
        if key in _TOPLEVEL_CONFLICTS:
            return False
        _TOPLEVEL_CONFLICTS.add(key)
        return True


def _reset_toplevel_conflicts() -> None:
    """Forget every claim. Tests only."""
    with _TOPLEVEL_CONFLICTS_LOCK:
        _TOPLEVEL_CONFLICTS.clear()


# Argv suffix that prints a simulator's version, for the toolchain half of the
# shared-build stamp.
# VCS is absent because `vcs -ID` checks out a licence.
_TOOLCHAIN_VERSION_ARGS = {
    "verilator": ("--version",),
    "icarus": ("-V",),
}

# (resolved path, mtime_ns) -> version line; one probe per binary per process.
_TOOLCHAIN_VERSION_CACHE: dict[tuple[str, int], str | None] = {}


def _probe_toolchain_version(exe_path, simulator_family, mtime_ns):
    """First line of the simulator's version banner, or ``None``.

    Every failure returns ``None``; an unreadable version must not fail a compile.
    """
    args = _TOOLCHAIN_VERSION_ARGS.get(simulator_family)
    if args is None:
        return None
    key = (exe_path, mtime_ns)
    if key in _TOOLCHAIN_VERSION_CACHE:
        return _TOOLCHAIN_VERSION_CACHE[key]
    version = None
    try:
        proc = subprocess.run(
            [exe_path, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        lines = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        version = lines[0].strip() if lines else None
    _TOOLCHAIN_VERSION_CACHE[key] = version
    return version


# Records which Verilator last compiled into a build directory, so the make can be
# scrubbed when the toolchain moves.
# Unlike the compile stamp it exists for builds without `--share-build`.
BUILD_TOOLCHAIN_MARKER_NAME = "rb-toolchain.json"

# Make outputs that embed the compiling toolchain's paths: objects, archives and `-MMD`
# dependency files.
# Everything else in a build dir is rewritten by the next compile or owned by rtl_buddy.
_STALE_BUILD_OUTPUT_GLOBS = ("*.d", "*.o", "*.a")


def _toolchain_marker_identity(toolchain) -> dict:
    """The part of a toolchain fingerprint that decides the make is stale.

    The resolved executable, version and host platform. Size and mtime are excluded,
    so touching the binary does not force a rebuild.
    """
    exe = toolchain.get("exe")
    return {
        "exe": os.path.realpath(exe) if exe else exe,
        "platform": f"{sys.platform}-{platform.machine()}",
        "version": toolchain.get("version"),
    }


def _describe_marker_toolchain(identity):
    """``<version> (<exe>, <platform>)`` for a marker identity, or ``None`` for no
    record.
    """
    if not identity:
        return None
    return (
        f"{identity.get('version') or 'unknown version'} "
        f"({identity.get('exe')}, {identity.get('platform') or 'unknown platform'})"
    )


def _scrub_stale_build_outputs(build_dir) -> int:
    """Delete the make outputs a new toolchain must not inherit; return how many.

    Top level only. Never raises; a file that will not go is left for make.
    """
    removed = 0
    root = Path(build_dir)
    for pattern in _STALE_BUILD_OUTPUT_GLOBS:
        for path in root.glob(pattern):
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def _log_stale_stamp_toolchain(stored_inputs, current_inputs, *, test_name=None):
    """Warn that a rebuild is caused by a toolchain change rather than an RTL change.

    Silent when the stamp has no toolchain entry (an rtl_buddy upgrade). Both
    arguments are the caller's comparison operands.
    """
    if "toolchain" not in stored_inputs:
        return
    was = stored_inputs.get("toolchain")
    # A caller may pass no fingerprint to assert a stamp is stale; that is not a
    # toolchain change.
    now = (current_inputs or {}).get("toolchain") or {}
    if not isinstance(was, dict) or was == now:
        return
    log_event(
        logger,
        logging.WARNING,
        "compile.build_toolchain_changed",
        test=test_name,
        was=was.get("version") or was.get("exe"),
        now=now.get("version") or now.get("exe"),
    )


def share_build_supported(simulator_family) -> bool:
    """Can this simulator family reuse one compiled simv across tests?

    Shared by :meth:`VlogSim.compile` and the dispatch head, which sizes a sim job's
    reservation on whether the job also compiles.
    """
    return simulator_family in SHARE_BUILD_FAMILIES


def share_build_unsupported_reason(builder_cfg):
    """Why this builder cannot use a shared build, or ``None`` if it can.

    Unsupported families and an absolute ``builder-simv:`` both decline. Module-level
    so the dispatch head can ask before any VlogSim exists.
    """
    family = builder_cfg.get_simulator_family()
    if not share_build_supported(family):
        return f"simulator family {family!r} has no shared-build support"
    pinned = pinned_simv_path(builder_cfg)
    if pinned is not None:
        return (
            f"builder-simv is an absolute path "
            f"({pinned}), which pins the "
            "executable outside the shared build dir"
        )
    return None


def pinned_simv_path(builder_cfg):
    """The absolute executable this builder pins every test to, or ``None``.

    Verilator and Icarus derive their output from the build dir and ignore
    ``builder-simv:``. This is a sharing predicate only; compile-pool grouping uses
    the resolved path from ``_get_simv_path()``.
    """
    if builder_cfg.get_simulator_family() in ("verilator", "icarus"):
        return None
    simv = builder_cfg.get_simv()
    return simv if os.path.isabs(simv) else None


# Option prefixes VlogFilelist emits into run.f. A `+define+` entry stamps as a raw
# line.
# `+incdir+` and `-y ` name directories and are stamped by listing them.
_FILELIST_OPTION_RE = re.compile(r"^(\+(?:incdir|libext|define)\+|-[vyF]\s+)?(.*)$")

_INCDIR_OPTION = "+incdir+"
_LIBRARY_DIR_OPTION = "-y"

# Compile-line options that name an input path, and what it is: ``("dir", recursive)``,
# ``"file"``, or a filelist.
# Output options (`-o`, `--Mdir`) must not appear, or a warm rebuild would hash its own
# binary.
# A relative path in a `-f` list resolves against the builder's cwd; in a `-F` list,
# against the list's directory. A nested list resets the rule.
_CMD_PATH_OPTIONS = {
    _LIBRARY_DIR_OPTION: ("dir", False),
    "-v": "file",
    "-f": "filelist-cwd",
    "-F": "filelist-rel",
}

# Maximum depth of nested `-f`/`-F` chains followed when keying a persistent cache, and
# the regex for a list line.
# It also accepts a lowercase `-f`, which a generated `run.f` never contains but a
# hand-written list may.
_NESTED_FILELIST_MAX_DEPTH = 8

# Key entry for a filelist chain beyond the depth bound. It carries the absolute path,
# so the cache serves only that checkout.
_DEPTH_BOUND_MARKER = "<unread-filelist> "
_NESTED_FILELIST_OPTION_RE = re.compile(
    r"^(\+(?:incdir|libext|define)\+|-[vyfF]\s+)?(.*)$"
)

# Compile-line options whose argument is an output location and must never be read as an
# input.
# See :func:`_is_build_tree_name` for the general guard.
_CMD_OUTPUT_OPTIONS = frozenset({"-o", "--Mdir", "-Mdir", "--exe-name"})

# Path-valued options that can sit inside a larger compile-line token, such as
# `-CFLAGS=-I../../inc`.
# Anchored to a boundary so `--Include` is not read as `-I`. `+libext+` is absent
# because its argument is a suffix list.
_EMBEDDED_PATH_OPTION_RE = re.compile(
    r"""(?:^|[\s=,:'"])          # a boundary, so `--Include` is not an `-I`
        (\+incdir\+|-I|-y)        # the option
        \s*                       # `-I../inc` and `-I ../inc` alike
        ([^\s:;,'"=]+)            # its payload
    """,
    re.VERBOSE,
)

# Prefixes whose argument is a value compiled into the model, not a relocatable path.
# Shared by the compile line and the generated ``run.f``.
_MACRO_VALUE_PREFIXES = (
    "+define+",
    "+libext+",
    "+parameter+",
    "-D",
    "-G",
    "-pvalue+",
)


def _is_macro_shaped(token: str) -> bool:
    """Is ``token`` a define/parameter assignment rather than a path?

    Define values are compiled into the model, so they are left as written and never
    relativised or content-hashed. ``+libext+`` takes suffixes, not a path. A bare
    ``NAME=value`` also counts.
    """
    return token.startswith(_MACRO_VALUE_PREFIXES) or (
        not token.startswith(("-", "+")) and "=" in token
    )


# Directory names an `+incdir+` walk must not descend into.
# rtl_buddy's own artefact trees are written after the fingerprint that would list them,
# so walking them makes every later process see a different listing and recompile.
# `__pycache__` is included because a `preproc` hook can write bytecode during the
# fingerprint phase.
# Dot-directories hold no compile input.
_PRUNED_WALK_DIRNAMES = frozenset(
    {ARTIFACT_DIRNAME, SHARED_BUILDS_DIRNAME, "__pycache__"}
)
_PRUNED_WALK_DIR_PREFIXES = (BUILD_DIR_PREFIX,)

# Editor and VCS bookkeeping files (fnmatch patterns) that no simulator reads.
# A name-based denylist, not "every dot-file": a dot-file can be a real include.
_BOOKKEEPING_FILE_PATTERNS = (
    ".DS_Store",
    ".gitignore",
    ".gitattributes",
    ".gitkeep",
    "*.swp",  # vim swap
    "*.swo",
    "*~",  # emacs/gedit backup
    ".#*",  # emacs lock
    "#*#",  # emacs autosave
)

# Marker the verilate half of a split compile leaves in the build dir for the build
# half: the plan fingerprint, whether verilation succeeded, and the failure transcript.
# A dotfile, so no suffix clear matches it.
VERILATE_MARKER_NAME = ".rb-verilate.json"

# rtl_buddy's own outputs, by name.
# An include root can itself be a managed tree (a `preproc` hook generates headers into
# its `artifact_dir`), so pruning `artefacts` is not enough. Generated inputs there must
# stay tracked, so the tree is walked and these outputs are removed by name.
# Each entry comes from the constant its writer uses.
_MANAGED_OUTPUT_FILE_PATTERNS = (
    FILELIST_NAME,
    COMPILE_TRANSCRIPT_NAME,
    COMPILE_RETRY_TRANSCRIPT_NAME,
    TEST_LOG_NAME,
    TEST_ERR_NAME,
    TEST_RANDSEED_NAME,
    COVERAGE_DAT_NAME,
    SIMV_NAME,
    ICARUS_SNAPSHOT_NAME,
    SHARED_BUILD_STAMP_NAME,
    VERILATE_MARKER_NAME,
    RESULT_JSON_NAME,
) + DISPATCH_OUTPUT_PATTERNS
# The head's `rtl_buddy.log` is excluded by path in `_directory_listing` (see
# `_is_suite_log`), since a file of that name elsewhere is an ordinary input.

# The same outputs caught mid-write.
# Managed outputs are written via a sibling temp file renamed into place, so an include
# directory (`+incdir+.` on a suite dir) can list a `test.log.<pid>.<uuid>.tmp` that is
# not a compile input.
# Derived from the patterns above via the writers' own helper. Anchored to a managed
# name, not a blanket `*.tmp`, because a project may include `defs.tmp`.
_MANAGED_OUTPUT_TMP_PATTERNS = tuple(
    tmp_pattern
    for pattern in _MANAGED_OUTPUT_FILE_PATTERNS
    for tmp_pattern in atomic_tmp_patterns(pattern)
)

_NON_INPUT_FILE_PATTERNS = (
    _BOOKKEEPING_FILE_PATTERNS
    + _MANAGED_OUTPUT_FILE_PATTERNS
    + _MANAGED_OUTPUT_TMP_PATTERNS
)

# The stamp's own keys, as opposed to the compile fingerprint it wraps.
# Removing them leaves the dict `_compile_fingerprint` returned. `root` is metadata and
# never an input: it differs between checkouts that must validate each other's stamp.
_STAMP_META = frozenset({"deps", "deps_format", "simv", "root"})

# Flags that make a Verilator run build as well as verilate. A compile line without one
# cannot be split.
_BUILD_STEP_FLAGS = ("--binary", "--build")

# Whether a Verilator supports ``--no-verilate``, keyed on the resolved executable.
# Answered once per process.
_NO_VERILATE_SUPPORT: dict[str, bool] = {}

# Environment override for the persistent shared-build cache root.
# Below ``--shared-build-root`` and above the root config's
# ``cfg-rtl-reg.shared-build-root``.
SHARED_BUILD_ROOT_ENV = "RTL_BUDDY_SHARED_BUILD_ROOT"

# A directory source entry is `[line, None, None, None, listing]`: the four-element file
# shape, empty, plus the listing.
# The extra element makes an older four-element stamp fail closed into one rebuild.
_DIRECTORY_ENTRY_LEN = 5

# How a stamp's `deps` entries name files. 2 is the declared path the build used
# (`normpath`). A stamp whose `deps` is a list and whose `deps_format` differs fails
# closed into one rebuild.
_DEPS_FORMAT = 2

# Verilator's make-style dependency file, listing every input the verilation consumed.
# It is named after `--prefix`, so it is found by glob. Other builders emit nothing
# comparable.
_VERILATOR_DEPEND_GLOB = "*__ver.d"

# One token of a make dependency line; a backslash escapes the next character.
_DEPEND_TOKEN_RE = re.compile(r"(?:[^\s\\]|\\.)+")


def parse_depend_prerequisites(text: str) -> list[str]:
    """Prerequisite paths from a make-style dependency file.

    Parsed rule by rule: targets before the ``:`` are dropped, and the bare phony
    rules that ``--MP`` appends are ignored. Order and duplicates are kept.
    """
    prerequisites = []
    for line in text.replace("\\\n", " ").splitlines():
        tokens = _DEPEND_TOKEN_RE.findall(line)
        for index, token in enumerate(tokens):
            if token == ":" or token.endswith(":"):
                # A rule with no prerequisites is a phony target.
                prerequisites += tokens[index + 1 :]
                break
        # A line with no separator is not a rule; ignore it.
    return [re.sub(r"\\(.)", r"\1", token) for token in prerequisites]


def _stat_entry(path: str) -> list:
    """``[path, size, mtime_ns]`` for a tracked input, or nulls if absent.

    A vanished file records as ``[path, None, None]`` so its reappearance invalidates
    the stamp. Stat-only: used for the ``simv`` output, where hashing a large binary
    buys nothing.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [path, None, None]
    return [path, stat.st_size, stat.st_mtime_ns]


def resolve_shared_build_root(raw, project_root) -> str | None:
    """The persistent shared-build cache root in force, absolute, or None.

    ``raw`` comes from ``--shared-build-root``, :data:`SHARED_BUILD_ROOT_ENV` or the
    root config, in that precedence. Blank or ``None`` means no cache. A relative
    root anchors to the project root, not the cwd; ``~`` and ``$VAR`` are expanded.
    """
    if raw is None:
        return None
    text = os.path.expandvars(os.path.expanduser(str(raw))).strip()
    if not text:
        return None
    if not os.path.isabs(text):
        text = os.path.join(str(project_root), text)
    return os.path.normpath(text)


def _relativise_paths(text: str, root: str) -> str:
    """``text`` with every mention of ``root`` stripped to a relative path.

    Applied to whole ``run.f`` lines and command tokens by substring replacement, so
    it needs no per-option table. Paths outside ``root`` stay absolute. Exact-prefix
    only: a different symlink spelling gives a different, still correct, key.
    """
    if not isinstance(text, str) or not root or root == os.sep:
        return text
    if text == root:
        return os.curdir
    prefix = root if root.endswith(os.sep) else root + os.sep
    return text.replace(prefix, "")


# One content hash per (path, size, mtime_ns) per process. Lock-guarded because build
# jobs validate from worker threads.
_CONTENT_HASH_LOCK = threading.Lock()
_CONTENT_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_CONTENT_HASH_CHUNK = 1 << 20

# Above this size an input keeps the size+mtime comparison instead of being read.
# Hashing is by location, not kind, so a large memory-init `.hex` or generated database
# would otherwise be read on every validation. `compile.hash_skipped_large` reports the
# skip.
_CONTENT_HASH_MAX_BYTES = 64 << 20

# Paths already reported as too large to hash, so the debug line appears once per file.
# Guarded by _CONTENT_HASH_LOCK.
_HASH_SKIPPED_LARGE: set[str] = set()


def _log_hash_skipped_large(path: str, size: int) -> None:
    """Log once per path that a tracked input is over the hash size cap and stays
    stat-only.
    """
    with _CONTENT_HASH_LOCK:
        if path in _HASH_SKIPPED_LARGE:
            return
        _HASH_SKIPPED_LARGE.add(path)
    log_event(
        logger,
        logging.DEBUG,
        "compile.hash_skipped_large",
        path=path,
        size=size,
        limit=_CONTENT_HASH_MAX_BYTES,
    )


def _hash_file_content(path: str, size: int, mtime_ns: int) -> str | None:
    """``sha256`` hexdigest[:16] of ``path``'s bytes, or None if unreadable.

    Memoised on ``(path, size, mtime_ns)`` to bound the cost within one process.
    """
    key = (path, size, mtime_ns)
    with _CONTENT_HASH_LOCK:
        cached = _CONTENT_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as content_fp:
            while True:
                chunk = content_fp.read(_CONTENT_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        # An unreadable file records None so the comparison fails closed against a stamp
        # that has a hash.
        return None
    value = digest.hexdigest()[:16]
    with _CONTENT_HASH_LOCK:
        _CONTENT_HASH_CACHE[key] = value
    return value


# Build directories ``--rebuild`` has already forced in this process.
# The flag means "do not trust the stamp on disk", not "compile once per test": the
# first test to meet a shared directory rebuilds and claims it, the rest validate the
# fresh stamp. Lock-guarded because build jobs compile from worker threads.
_REBUILT_DIRS_LOCK = threading.Lock()
_REBUILT_DIRS: set[str] = set()

# Build dirs whose reuse this process has already printed on the console, so N tests
# reusing one build print one line.
# The file log keeps every `compile.build_reused`.
_REUSE_ANNOUNCED_LOCK = threading.Lock()
_REUSE_ANNOUNCED: set[str] = set()


def _first_reuse_announcement(build_dir: str) -> bool:
    """Is this the process's first console-worthy reuse of ``build_dir``?"""
    key = os.path.realpath(build_dir)
    with _REUSE_ANNOUNCED_LOCK:
        if key in _REUSE_ANNOUNCED:
            return False
        _REUSE_ANNOUNCED.add(key)
        return True


def _reset_reuse_announcements() -> None:
    """Test hook: forget which build dirs already hit the console."""
    with _REUSE_ANNOUNCED_LOCK:
        _REUSE_ANNOUNCED.clear()


def _claim_rebuild(build_dir: str) -> bool:
    """Is this process's first ``--rebuild`` of ``build_dir``? Claims it.

    Keyed on ``realpath`` so two spellings of one directory are one build.
    """
    key = os.path.realpath(build_dir)
    with _REBUILT_DIRS_LOCK:
        if key in _REBUILT_DIRS:
            return False
        _REBUILT_DIRS.add(key)
        return True


def _reset_rebuilt_dirs() -> None:
    """Forget every claim. Tests only."""
    with _REBUILT_DIRS_LOCK:
        _REBUILT_DIRS.clear()


def _build_dir_fields(build_dir, *, shared: bool) -> dict:
    """The directory fields shared by ``compile.build_reused`` and
    ``compile.rebuild_forced``.

    ``build_dir`` is the basename and ``build_path`` the full path. ``shared``
    selects how :func:`logging_utils._build_location` shows it.
    """
    fields = {
        "build_dir": os.path.basename(str(build_dir).rstrip(os.sep)),
        "build_path": str(build_dir),
    }
    if not shared:
        fields["shared"] = False
    return fields


def _path_is_under(path: str, root: str) -> bool:
    """Is ``path`` inside ``root``? Both must already be canonical."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Different drives on Windows.
        return False


def _content_sha(
    path: str,
    stat: os.stat_result,
    project_root: str | None,
    *,
    resolved: bool = False,
    toolchain_prefix: str | None = None,
) -> str | None:
    """Hash ``path``'s content when policy allows, else None.

    Content, not stats, decides edits: on a cluster an NFS node can serve cached
    attributes for a file edited seconds ago, while ``open()`` revalidates content.

    Only the project's own regular files are hashed: the path must be under
    ``project_root`` (judged on the declared name as well as the realpath, so a
    symlinked-in IP tree counts), outside ``toolchain_prefix`` (tested on the
    realpath), and no larger than ``_CONTENT_HASH_MAX_BYTES``. A FIFO would block in
    ``open()``. ``resolved`` says ``path`` is already a realpath.
    """
    if not project_root:
        return None
    if not S_ISREG(stat.st_mode):
        return None
    # Hash under the realpath so two spellings of one file share a memo entry.
    real_path = path if resolved else os.path.realpath(path)
    under_root = _path_is_under(real_path, project_root) or (
        not resolved and _path_is_under(os.path.abspath(path), project_root)
    )
    if not under_root:
        return None
    if toolchain_prefix and _path_is_under(real_path, toolchain_prefix):
        return None
    if stat.st_size > _CONTENT_HASH_MAX_BYTES:
        _log_hash_skipped_large(real_path, stat.st_size)
        return None
    return _hash_file_content(real_path, stat.st_size, stat.st_mtime_ns)


def _hashed_stat_entry(
    path: str,
    *,
    project_root: str | None,
    resolved: bool = False,
    toolchain_prefix: str | None = None,
) -> list:
    """``[path, size, mtime_ns, sha]`` for a tracked input.

    ``sha`` is :func:`_content_sha`; None when policy excludes the file or it is
    unreadable, which :func:`_entry_matches` treats as changed. A vanished file
    records as ``[path, None, None, None]`` so its reappearance invalidates the
    stamp.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [path, None, None, None]
    return [
        path,
        stat.st_size,
        stat.st_mtime_ns,
        _content_sha(
            path,
            stat,
            project_root,
            resolved=resolved,
            toolchain_prefix=toolchain_prefix,
        ),
    ]


def _is_pruned_walk_dir(name: str) -> bool:
    """Should an ``+incdir+`` walk refuse to descend into ``name``?

    True for rtl_buddy's own artefact trees and for dot-directories. See
    :data:`_PRUNED_WALK_DIRNAMES`.
    """
    return (
        name.startswith(".")
        or name in _PRUNED_WALK_DIRNAMES
        or name.startswith(_PRUNED_WALK_DIR_PREFIXES)
    )


def _is_build_tree_name(name: str) -> bool:
    """Is ``name`` a directory a builder writes into, ``.shared-builds`` or an
    ``obj_dir*``?

    Nothing in there is an input, and keying on one would move the key on every
    build.
    This is narrower than :func:`_is_pruned_walk_dir`: pruning every dot-directory
    would misread an input under a path such as ``/home/ci/.worktrees/pr`` as an
    output. Ask it only of components below the project root (see
    :meth:`VlogSim._key_input_path`).
    ``artefacts`` is not included because generated headers can live there;
    :func:`_is_non_input_file` excludes outputs by name.
    """
    return name == SHARED_BUILDS_DIRNAME or name.startswith(BUILD_DIR_PREFIX)


def _key_spelling_is_relocated(spelling: str) -> bool:
    """Does ``spelling`` name a path relative to a project root?

    In cache mode that means inside the project root, where every checkout has its
    own copy. When such a path cannot be hashed the key needs a fallback. An absolute
    path is the same bytes for every checkout, so its stats do not belong in the key.
    """
    match = _FILELIST_OPTION_RE.match(spelling)
    path = match.group(2) if match else spelling
    return bool(path) and not os.path.isabs(path)


def _key_content_identity(spelling, entry, *, relocated: bool):
    """What one tracked input contributes to a content-addressed key.

    ``entry`` is a ``[path, size, mtime_ns, sha]`` stamp; ``spelling`` is the name
    the key records. With a hash, the contribution is ``[spelling, sha]``. Without
    one, an in-root path falls back to ``[spelling, size, mtime_ns]`` so checkouts
    with different large inputs do not share a build, at the cost of not sharing
    across checkouts. The two shapes differ in length, so they never compare equal.
    """
    if not isinstance(entry, list) or len(entry) != 4:
        return entry
    if entry[-1] is not None:
        return [spelling, entry[-1]]
    if relocated:
        return [spelling, entry[1], entry[2]]
    return [spelling, None]


def _is_non_input_file(name: str) -> bool:
    """Is ``name`` bookkeeping or rtl_buddy's own output, not a compile input?

    Matched by name against :data:`_NON_INPUT_FILE_PATTERNS` anywhere in a listing,
    because an include root can itself be an artefact directory. Other files,
    including dot-files, are inputs.
    """
    return any(
        fnmatch.fnmatchcase(name, pattern) for pattern in _NON_INPUT_FILE_PATTERNS
    )


def _is_directory_entry(entry) -> bool:
    """Is ``entry`` a ``+incdir+``/``-y`` entry carrying a directory listing?

    The listing is the last element: ordinary entries keyed by path relative to the
    directory, which ``entry[0]`` already fixes.
    """
    return (
        isinstance(entry, list)
        and len(entry) == _DIRECTORY_ENTRY_LEN
        and isinstance(entry[-1], list)
    )


def _listing_names(entries) -> list | None:
    """The names a directory listing carries, or None if it is malformed.

    This is the part of a listing that survives ``listing_names_only``: which files
    exist, not their content.
    """
    if not isinstance(entries, list):
        return None
    names = []
    for entry in entries:
        if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
            return None  # not a listing this version wrote
        names.append(entry[0])
    return names


def _entry_matches(stored, current: list, *, listing_names_only: bool = False) -> bool:
    """Does a stored stamp entry still describe what ``current`` describes?

    The one comparison behind the filelist ``sources`` and the build ``deps``. When
    both sides have a content hash, the hash decides; otherwise ``[path, size,
    mtime_ns]`` must match exactly. A directory entry compares by its listing.
    ``listing_names_only`` compares listings by file names alone; the caller sets it
    when the stamp's ``deps`` already decide the content of consumed files. Any other
    shape means "unknown" and forces one rebuild.
    """
    if not isinstance(stored, list) or len(stored) != len(current):
        return False
    if not stored or not current:
        # Unreachable while `current` comes from _hashed_stat_entry; this is the general
        # comparator and fails closed.
        return False
    if stored[0] != current[0]:
        return False
    if len(stored) == _DIRECTORY_ENTRY_LEN:
        # A directory entry's last element is a listing, not a hash, so recurse rather
        # than compare it by equality. A five-element entry that is not a listing fails
        # closed.
        if not (_is_directory_entry(stored) and _is_directory_entry(current)):
            return False
        if listing_names_only:
            stored_names = _listing_names(stored[-1])
            return stored_names is not None and stored_names == _listing_names(
                current[-1]
            )
        return _entry_lists_match(stored[-1], current[-1])
    stored_sha, current_sha = stored[-1], current[-1]
    if stored_sha is not None and current_sha is not None:
        # Equal content hashes with different sizes cannot happen for a real file.
        return stored_sha == current_sha
    return stored == current


def _entry_lists_match(stored, current, *, listing_names_only: bool = False) -> bool:
    """:func:`_entry_matches` over two whole lists, order-sensitive.

    Both lists are built deterministically, so a reordering is a real difference. A
    non-list fails closed.
    """
    if not isinstance(stored, list) or not isinstance(current, list):
        return False
    if len(stored) != len(current):
        return False
    return all(
        _entry_matches(
            stored_entry, current_entry, listing_names_only=listing_names_only
        )
        for stored_entry, current_entry in zip(stored, current)
    )


def _first_listing_mismatch(stored, current, *, names_only: bool = False):
    """What made two directory listings disagree, for a diagnostic.

    Diffed by name, not position: a name only one side has is reported as
    ``+added.svh`` or ``-removed.svh``, and a name both have that no longer matches
    is reported as itself. The decision stays with :func:`_entry_lists_match`.
    """
    if not isinstance(stored, list) or not isinstance(current, list):
        return "(listing is not a list)"

    def _by_name(entries):
        return {
            entry[0]: entry
            for entry in entries
            if isinstance(entry, list) and entry and isinstance(entry[0], str)
        }

    stored_by_name, current_by_name = _by_name(stored), _by_name(current)
    added = sorted(set(current_by_name) - set(stored_by_name))
    removed = sorted(set(stored_by_name) - set(current_by_name))
    if added:
        return f"+{added[0]}"
    if removed:
        return f"-{removed[0]}"
    if not names_only:
        for name in sorted(set(stored_by_name) & set(current_by_name)):
            if not _entry_matches(stored_by_name[name], current_by_name[name]):
                return name
    # Nothing named differs, so the disagreement is in a dropped shape or in ordering.
    return _first_entry_mismatch(stored, current, listing_names_only=names_only)


def _first_resolution_change(stored_sources, sources, deps):
    """A file newly added to a stamped directory that can change what the compile
    resolves, or None.

    Returned as ``"<line> :: +<name>"``. :meth:`VlogSim.adopt_group_build` ignores a
    member's own ``preproc`` output appearing under ``+incdir+.``, but not:
    - any file appearing in a ``-y`` directory (resolution is by module name on
      demand);
    - a file appearing in an ``+incdir+`` under the same relative name as an include
      the leader consumed.

    Removals are covered by the ``deps`` comparison. Lists that do not line up as
    stamps of one ``run.f`` count as a change.
    """
    if not isinstance(stored_sources, list) or not isinstance(sources, list):
        return "(stamp sources are not a list)"
    if len(stored_sources) != len(sources):
        return f"(entry count {len(stored_sources)} -> {len(sources)})"
    consumed = tuple(
        os.path.normpath(entry[0])
        for entry in deps
        if isinstance(entry, list) and entry and isinstance(entry[0], str)
    )
    for stored_entry, entry in zip(stored_sources, sources):
        if not _is_directory_entry(entry):
            continue
        if not isinstance(stored_entry, list) or not stored_entry:
            return f"{entry[0]} :: (stamp entry is malformed)"
        if stored_entry[0] != entry[0]:
            return str(entry[0])
        names = _listing_names(entry[-1])
        if _is_directory_entry(stored_entry):
            stored_names = _listing_names(stored_entry[-1])
        else:
            stored_names = []
        if stored_names is None or names is None:
            return f"{entry[0]} :: (listing is not a list)"
        added = sorted(set(names) - set(stored_names))
        if not added:
            continue
        option_match = _FILELIST_OPTION_RE.match(entry[0])
        option = (option_match.group(1) or "").strip() if option_match else ""
        if option == _LIBRARY_DIR_OPTION:
            return f"{entry[0]} :: +{added[0]}"
        for name in added:
            suffix = os.sep + name
            if any(path.endswith(suffix) for path in consumed):
                return f"{entry[0]} :: +{name}"
    return None


def _first_entry_mismatch(stored, current, *, listing_names_only: bool = False):
    """What made :func:`_entry_lists_match` say no, for a diagnostic.

    Returns the first mismatching entry's path or line, or a shape note. It is told
    ``listing_names_only`` so it explains the same decision the matchers made.
    """
    if not isinstance(stored, list) or not isinstance(current, list):
        return "(stamp sources are not a list)"
    if len(stored) != len(current):
        return f"(entry count {len(stored)} -> {len(current)})"
    for stored_entry, current_entry in zip(stored, current):
        if not _entry_matches(
            stored_entry, current_entry, listing_names_only=listing_names_only
        ):
            if _is_directory_entry(stored_entry) and _is_directory_entry(current_entry):
                # Name the file inside a stamped directory, not just the directory.
                inner = _first_listing_mismatch(
                    stored_entry[-1],
                    current_entry[-1],
                    names_only=listing_names_only,
                )
                return f"{current_entry[0]} :: {inner}"
            if isinstance(current_entry, list) and current_entry:
                return current_entry[0]
            if isinstance(stored_entry, list) and stored_entry:
                return stored_entry[0]
            return "(malformed entry)"
    return "(no mismatch)"


def _entry_identity(entry):
    """The part of a tracked-input entry that decides :func:`_entry_matches`.

    An entry with a hash collapses to ``[path, sha]``; one without keeps its full
    shape. This lets a hash of a fingerprint mean what the entry-wise comparison
    means: a ``touch`` must not change it. The two shapes never compare equal.
    Unrecognised shapes pass through unchanged.
    """
    if _is_directory_entry(entry):
        # Same reduction one level down, so a `touch` inside an include directory does
        # not move the sha.
        return [entry[0], [_entry_identity(inner) for inner in entry[-1]]]
    if isinstance(entry, list) and len(entry) == 4 and entry[-1] is not None:
        return [entry[0], entry[-1]]
    return entry


def _fingerprint_sha(fingerprint):
    """Compact identity (sha256) of one compile's inputs, or None for None.

    Hashes the canonical JSON of the stamp's fingerprint dict with each ``sources``
    entry reduced by :func:`_entry_identity`, so equal shas mean the stamp would
    match. A build job records it beside a failed compile's return code, and the
    gated sim job recomputes it: equal means the sim job would repeat the same failed
    compile, different means the inputs moved and a retry is earned.
    """
    if fingerprint is None:
        return None
    canonical = dict(fingerprint)
    sources = canonical.get("sources")
    if isinstance(sources, list):
        canonical["sources"] = [_entry_identity(entry) for entry in sources]
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass
class _CompilePlan:
    """Everything about a compile that is decided before the builder runs.

    Split out of :meth:`VlogSim.compile` so a build job can ask for the compile key
    without compiling and group configs by :attr:`group_dir`. The grouping value is
    derived here only, because a second derivation would drift and put two builders
    in one directory.
    """

    compile_work_dir: str
    filelist_path: str
    build_dir: str
    builder_opts: list = field(default_factory=list)
    extra_compile_flags: list = field(default_factory=list)
    assertion_flags: list = field(default_factory=list)
    # The family's top-selection flag for the testbench's `toplevel:`. Empty when none
    # is declared, the family has none, or the configured opts already pin one.
    top_flags: list = field(default_factory=list)
    plusdefines: list = field(default_factory=list)
    is_verilator: bool = False
    # None unless share_build is on and the family supports sharing.
    key_cmd: list | None = None
    fingerprint: dict | None = None
    shared_dir: Path | None = None
    # Why sharing was declined, or None. Set only when share_build is on.
    unsupported_reason: str | None = None
    # What this compile writes over: the shared build dir when shareable, the absolute
    # `builder-simv:` when one pins the executable, else the test's compile work dir.
    # Two configs with the same value must not compile concurrently. It is the
    # directory, not (compile_work_dir, shared_dir), so different tests with one key
    # land in one group.
    group_dir: str = ""


class VlogSim:
    """Compiles and runs one Verilog test under a simulator."""

    # TODO: Replace suite_cfg, test_name with test_info and testbench
    def __init__(
        self,
        name,
        root_cfg,
        test_cfg,
        rtl_builder_mode,
        sim_mode,
        run_id=None,
        replay_run_id=None,
        suite_dir=None,
        share_build=False,
        shared_build_root=None,
        expect_prebuilt=False,
        rebuild=False,
        build_result_json=None,
        build_phase=BUILD_PHASE_FULL,
        run_tag=None,
    ):
        """Set up the sim for one test.

        ``suite_dir`` defaults to the cwd, for direct construction in tests.
        """
        self.name = name
        self.root_cfg = root_cfg
        self.rtl_builder_cfg = root_cfg.resolve_rtl_builder_cfg(
            test_cfg.get_builder_name()
        )
        self.rtl_builder_mode = rtl_builder_mode
        self.sim_mode = sim_mode
        self.test_cfg = test_cfg
        get_resolved_seed = getattr(test_cfg, "get_resolved_seed", None)
        self._resolved_runtime_seed = (
            get_resolved_seed() if callable(get_resolved_seed) else None
        )
        self._resolved_runtime_seed_source = getattr(test_cfg, "seed_source", None)
        self._resolved_runtime_seed_identity = getattr(test_cfg, "seed_identity", None)
        self._resolved_runtime_seed_plusarg = getattr(
            test_cfg, "sim_rand_seed_plusarg", None
        )
        self.test_name = self.test_cfg.get_name()
        self.run_id = run_id
        self.replay_run_id = replay_run_id
        self.testbench = self.test_cfg.get_testbench()
        self.vlog_post = None
        # Why the last stamp check said no, in one phrase, or None. Read by the
        # gated-retry warning.
        self.stamp_mismatch_reason = None
        # The stamp's ``{fingerprint_sha, simv}`` for the build this run ended up
        # simulating. It rides the result envelope so the head can check that every run
        # of one compile key named the same binary.
        self.last_build_stamp = None
        # True when the last compile succeeded but failed to record its stamp. The build
        # job reports it as ``stamp_written: false`` so a gated sim job does not
        # recompile.
        self.stamp_write_failed = False
        # Opt-in: key the build dir on a hash of the compile inputs so tests with
        # identical inputs share one simv.
        # The shared dir is known only after compile() writes the filelist.
        self.share_build = share_build
        # Root of the persistent shared-build cache, or None for the in-tree
        # `<suite>/artefacts/.shared-builds/` layout. Resolved below, once
        # `_project_root` exists.
        self._configured_shared_build_root = shared_build_root
        # `--rebuild`: distrust the stamp and compile anyway. Honoured once per build
        # dir per process (see :func:`_claim_rebuild`).
        self.rebuild = rebuild
        self._shared_build_dir = None
        # Directory the builder will run in, once a plan settles it. Relative entries in
        # a `-f` filelist resolve against it. None until a plan exists.
        self._compile_cwd = None
        # Logged once per instance so a chain past the depth bound does not log at every
        # level.
        self._depth_bound_logged = False
        # Filled by _compile_plan() and consumed by compile(), so a probe and the
        # following compile share one derivation, while a second compile() re-stats its
        # sources.
        self._compile_plan_cache = None
        # Cost of the last compile this instance performed: {duration_sec, builder,
        # reused}, or None.
        # Never a stamp key: the stamp's key set is the fingerprint comparison, so an
        # extra key would invalidate every stamp.
        self.last_compile = None
        # Set by a dispatched sim job gated on a build job. Compiling here then means
        # the build's stamp did not validate, which warrants a WARNING.
        self.expect_prebuilt = expect_prebuilt
        # Which half of the compile this instance runs. ``full`` is `verilator
        # --binary`; ``verilate`` emits the sources and `V<top>.mk`; ``build`` runs make
        # over that.
        self.build_phase = build_phase
        # Set by the build half: whether the marker cleared this key to skip the front
        # end. False means a full compile.
        self._skip_verilate = False
        # Time the verilate half spent on this key, from its marker. The build half
        # reports the sum.
        self._verilate_sec = None
        # The build job's envelope, when the head knew one. It separates a build whose
        # compile failed for this test (retrying only burns the sim reservation) from a
        # stamp that is absent or stale (retry). None for local runs.
        self.build_result_json = build_result_json
        # {returncode, transcript} of the last failed compile, read by a dispatched
        # build job for its envelope. None until a compile fails; reset by each
        # compile().
        self.last_compile_failure = None
        # One-line desc replacing the generic "Compile failed" on the gated-build-failed
        # path.
        self.compile_fail_desc = None
        # Full-path override for the next compile transcript; None means the test-scoped
        # `compile.log`.
        # A gated sim job's retry must not overwrite the build job's `compile.log`. The
        # path is run-scoped because sibling runs share the test artefact dir.
        self._compile_transcript_override = None
        # CLI commands pass suite_dir resolved from the test config. The cwd fallback is
        # for tests that construct VlogSim directly; new code paths must pass suite_dir.
        self.suite_work_dir = (
            os.path.abspath(suite_dir)
            if suite_dir is not None
            else os.path.abspath(os.getcwd())
        )
        # The `--run-tag` namespace this run's artefacts hang under, passed from the
        # head so a dispatched job writes into the head's tree. None is the flat layout.
        # The shared build directory stays shared across tags.
        self.run_tag = run_tag
        # Where the head writes its own log, the one path a directory listing skips by
        # location rather than name.
        # Under a `--run-tag` the log sits in `artefacts/`, which listings prune, so
        # only the flat path needs naming.
        self._suite_log_path = os.path.realpath(
            os.path.join(self.suite_work_dir, DEFAULT_FILE_LOG)
        )

        # Files this instance may content-hash for build stamps. It is the project root,
        # not the suite dir, because RTL and headers often live outside the suite.
        # Realpath'd for containment tests.
        get_project_rootdir = getattr(self.root_cfg, "get_project_rootdir", None)
        try:
            project_root = (
                get_project_rootdir() if get_project_rootdir is not None else None
            )
        except Exception:
            # Choosing what to hash must not stop a build; an unusable root narrows the
            # policy to the suite dir.
            project_root = None
        # Same cwd fallback as suite_work_dir, for directly constructed callers.
        derived = isinstance(project_root, str) and bool(project_root)
        self._project_root = os.path.realpath(
            project_root if derived else self.suite_work_dir
        )
        # Falling back to the suite dir turns hashing off for out-of-suite RTL, so log
        # the root in force and where it came from.
        log_event(
            logger,
            logging.DEBUG,
            "compile.hash_root",
            test=self.test_name,
            project_root=self._project_root,
            derived=derived,
        )
        # Resolved lazily and once: an install prefix under the project root is excluded
        # from hashing, and finding it costs a PATH walk.
        self._toolchain_prefix = _UNSET

        # Cache mode needs `_project_root` to anchor a relative root. It applies to the
        # shared build only; without `--share-build` there is nothing another checkout
        # could reuse.
        self.shared_build_root = (
            resolve_shared_build_root(
                self._configured_shared_build_root, self._project_root
            )
            if share_build
            else None
        )
        if self.shared_build_root is not None:
            log_event(
                logger,
                logging.DEBUG,
                "compile.shared_build_root",
                test=self.test_name,
                root=self.shared_build_root,
                namespace=shared_build_namespace(
                    self.suite_work_dir, self._project_root
                ),
            )

        output_dir = run_artifact_root(self.suite_work_dir, self.run_tag)
        output_dir.mkdir(parents=True, exist_ok=True)

        self.output_dir = str(output_dir)

    def _get_build_tag(self):
        """Return a filesystem-safe tag derived from the test name."""
        return test_artifact_dir(self.suite_work_dir, self.test_name).name

    def _get_build_dir(self):
        """Return the simulator build directory name for this test."""
        return test_build_dir_name(self.test_name)

    def _get_compile_work_dir(self):
        return self._get_artifact_dir()

    def _get_simv_path(self):
        """Return the simulator executable path for this test/build.

        - Verilator: `<artefact>/<build>/simv`.
        - Icarus: `<artefact>/simv`, a shell wrapper around `vvp <build>/simv.vvp`.
        - Other backends: `builder-simv:` from the builder config.

        Under an active shared build it is always `simv` inside the shared dir, the
        path other tests with the same key look for and `_shared_build_is_valid`
        checks.
        """
        if self._shared_build_dir is not None:
            return str(Path(self._shared_build_dir) / SIMV_NAME)
        rtl_builder_exe = self.rtl_builder_cfg.get_exe()
        if os.path.basename(rtl_builder_exe).startswith("verilator"):
            return str(
                Path(self._get_compile_work_dir()) / self._get_build_dir() / SIMV_NAME
            )
        if self._get_simulator_family() == "icarus":
            return str(Path(self._get_compile_work_dir()) / SIMV_NAME)
        simv_path = self.rtl_builder_cfg.get_simv()
        if os.path.isabs(simv_path):
            return simv_path
        return str(Path(self._get_compile_work_dir()) / simv_path)

    def _get_icarus_snapshot_path(self):
        """Path to the .vvp snapshot produced by iverilog."""
        if self._shared_build_dir is not None:
            return str(Path(self._shared_build_dir) / ICARUS_SNAPSHOT_NAME)
        return str(
            Path(self._get_compile_work_dir())
            / self._get_build_dir()
            / ICARUS_SNAPSHOT_NAME
        )

    def _icarus_vvp_extra_args(self) -> list:
        """Extra `vvp` arguments placed ahead of the snapshot in the wrapper.

        None in the base class. CocotbSim loads the cocotb VPI module here, since
        Icarus binds VPI at `vvp` invocation.
        """
        return []

    def _write_icarus_simv_wrapper(self):
        """Write a shell wrapper that execs `vvp [extra] <snapshot> "$@"`.

        It lets execute() invoke Icarus as a single executable. Extra args go before
        the snapshot, where `vvp` requires `-M`/`-m`.
        """
        wrapper_path = self._get_simv_path()
        snapshot = self._get_icarus_snapshot_path()
        argv = ["exec", "vvp", *self._icarus_vvp_extra_args(), snapshot]
        cmd = " ".join(shlex.quote(part) for part in argv)
        Path(wrapper_path).write_text(f'#!/bin/sh\n{cmd} "$@"\n')
        os.chmod(wrapper_path, 0o755)

    def _get_artifact_dir(self, run_id=None):
        return str(
            test_artifact_dir(
                self.suite_work_dir,
                self.test_name,
                run_id=run_id,
                run_tag=self.run_tag,
            )
        )

    def _ensure_artifact_dir(self, run_id=None):
        artifact_dir = Path(self._get_artifact_dir(run_id=run_id))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return str(artifact_dir)

    def _get_compile_transcript_path(self):
        if self._compile_transcript_override is not None:
            return self._compile_transcript_override
        return str(Path(self._get_compile_work_dir()) / COMPILE_TRANSCRIPT_NAME)

    def _get_retry_transcript_path(self):
        """Where this run's gated retry writes its transcript.

        It sits in the run's artifact directory, because sibling runs of a fanned-out
        test share the test dir and would overwrite each other's retry log.
        """
        return str(
            Path(self._get_artifact_dir(run_id=self.run_id))
            / COMPILE_RETRY_TRANSCRIPT_NAME
        )

    def _run_output_paths(self, run_id):
        """The files one run writes into its artifact directory and a later run replaces.

        Each holds a verdict or the evidence for one. ``test.randseed`` is not listed: a
        ``--replay`` run reads the previous run's seed from it.
        """
        artifact_dir = Path(self._get_artifact_dir(run_id=run_id))
        return [artifact_dir / name for name in _RUN_OUTPUT_NAMES]

    def clear_run_outputs(self, run_ids):
        """Unlink the previous run's outputs from the artifact directory of every run in
        ``run_ids``.

        Called before PRE, so a run that stops at setup or compile leaves no file from an
        earlier run that names an earlier verdict. Best-effort: a file that cannot be
        removed is logged and the run goes on.
        """
        for run_id in run_ids:
            for path in self._run_output_paths(run_id):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "test.stale_output_unremovable",
                        test=self.test_name,
                        run_id=run_id,
                        path=str(path),
                        error=str(exc),
                    )

    def _get_build_compile_transcript_path(self):
        """Where the build job wrote this test's transcript.

        Always ``compile.log``, so a gated sim job that declines to retry never names
        its own retry log.
        """
        return str(Path(self._get_compile_work_dir()) / COMPILE_TRANSCRIPT_NAME)

    def _get_filelist_path(self):
        return str(Path(self._get_compile_work_dir()) / FILELIST_NAME)

    def _get_log_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / TEST_LOG_NAME)

    def _get_err_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / TEST_ERR_NAME)

    def _get_randseed_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / TEST_RANDSEED_NAME)

    def _coverage_enabled(self):
        compile_opts = self._get_builder_compile_opts()
        if any(opt.startswith("--coverage") for opt in compile_opts):
            return True
        # Verilator `--coverage-user` from assertions=true is enough to produce a
        # coverage.dat.
        if self._assertions_enabled() and self._get_simulator_family() == "verilator":
            return True
        return False

    def _assertions_enabled(self):
        """SVA assertions requested for this test (Verilator-only)."""
        return bool(getattr(self.test_cfg, "assertions", False))

    def _get_verilator_assertion_flags(self, builder_opts: list[str]) -> list[str]:
        """Return Verilator flags needed to compile SVA and cover hits.

        Skips flags already present in the builder's configured opts.
        """
        if not self._assertions_enabled():
            return []
        if self._get_simulator_family() != "verilator":
            log_event(
                logger,
                logging.WARNING,
                "compile.assertions_not_verilator",
                test=self.test_name,
                simulator=self._get_simulator_family(),
            )
            return []

        existing = set(builder_opts)
        extras: list[str] = []
        if "--assert" not in existing:
            extras.append("--assert")
        if not any(opt == "--coverage-user" for opt in existing):
            extras.append("--coverage-user")
        return extras

    def _user_configured_top(self):
        """The top the builder's ``compile-time`` opts pin, or ``None``.

        Returns ``(flag_as_written, module_or_None)``. User opts only, filtered as
        :meth:`_build_compile_plan` filters them. A subclass calls it before
        generating its own top, because a generated flag placed after the user's
        would win on last-wins precedence and hide the conflict warning.
        """
        spec = TOP_MODULE_FLAGS.get(self._get_simulator_family())
        if spec is None:
            return None
        return _find_configured_top(
            spec,
            self._filter_builder_opts(self._get_builder_compile_opts()),
        )

    def _get_top_module_flags(
        self, builder_opts: list, extra_compile_flags: list
    ) -> list:
        """Root the compile at the testbench's declared ``toplevel:``.

        Nothing is added when no ``toplevel:`` is declared. ``builder_opts`` (the
        user's ``compile-time``) is checked for a pinned top in every spelling the
        family accepts (see :class:`_TopFlagSpec`); a pinned top wins, and one that
        disagrees with ``toplevel:`` gives one WARNING per (family, configured top,
        declared top) per process. ``extra_compile_flags`` (generated by the
        SystemC/cocotb subclass) is checked only to avoid a duplicate, never for
        conflicts: the generated flag comes later and would otherwise shadow the
        user's.
        """
        toplevel = getattr(self.testbench, "toplevel", None)
        if not toplevel:
            return []
        family = self._get_simulator_family()
        spec = TOP_MODULE_FLAGS.get(family)
        if spec is None:
            log_event(
                logger,
                logging.DEBUG,
                "compile.toplevel_family_unsupported",
                test=self.test_name,
                simulator=family,
                toplevel=toplevel,
            )
            return []

        pinned = _find_configured_top(spec, builder_opts)
        if pinned is not None:
            written, existing = pinned
            if existing == toplevel:
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.toplevel_already_pinned",
                    test=self.test_name,
                    simulator=family,
                    flag=written,
                    toplevel=toplevel,
                    source="builder-opts",
                )
            elif _claim_toplevel_conflict((family, written, existing, toplevel)):
                log_event(
                    logger,
                    logging.WARNING,
                    "compile.toplevel_conflict",
                    test=self.test_name,
                    simulator=family,
                    flag=written,
                    toplevel=toplevel,
                    # Omitted, not None, for a bare flag, so the message says "with no
                    # value".
                    configured=existing,
                )
            return []

        generated = _find_configured_top(spec, extra_compile_flags)
        if generated is not None:
            # Ours, not the user's: a duplicate to avoid, never a conflict.
            log_event(
                logger,
                logging.DEBUG,
                "compile.toplevel_already_pinned",
                test=self.test_name,
                simulator=family,
                flag=generated[0],
                toplevel=toplevel,
                source="backend",
            )
            return []

        log_event(
            logger,
            logging.DEBUG,
            "compile.toplevel",
            test=self.test_name,
            simulator=family,
            flag=spec.emit,
            toplevel=toplevel,
        )
        return [spec.emit, toplevel]

    def _get_simulator_family(self):
        """Return the canonical simulator family for backend-specific handling."""
        return self.rtl_builder_cfg.get_simulator_family()

    def _get_builder_compile_opts(self) -> list:
        """The builder mode's ``compile-time`` opts, variables expanded.

        ``${RTL_BUDDY_PROJECT_ROOT}`` names the project root; see
        :func:`expand_compile_opts`.
        """
        return expand_compile_opts(
            self.rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode),
            getattr(self, "_project_root", None),
        )

    def _filter_builder_opts(self, opts: list) -> list:
        return opts

    def _get_extra_compile_flags(self) -> list:
        return []

    def _get_extra_compile_env(self) -> dict:
        """Hook for subclasses to inject env vars into the compile subprocess.

        SystemCSim overrides it to pin CXX and export SYSTEMC_HOME, SYSTEMC_INCLUDE
        and SYSTEMC_LIBDIR for Verilator's --build step.
        """
        return {}

    def _get_extra_sim_env(self, run_id=None) -> dict:
        return {}

    def _get_cov_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / COVERAGE_DAT_NAME)

    def _get_cov_abspath(self, run_id=None):
        return str(Path(self._get_cov_path(run_id=run_id)).resolve())

    def _get_suite_symlink_path(self, name):
        return str(Path(self.suite_work_dir) / name)

    def _append_hier_instance_seed(
        self, randseed_fp, *, artifact_dir, run_cmd, test, run_id
    ):
        if "hier_inst_seed" not in run_cmd:
            return

        hier_seed_path = Path(artifact_dir) / "HierInstanceSeed.txt"
        if not hier_seed_path.exists():
            log_event(
                logger,
                logging.WARNING,
                "sim.hier_seed_missing",
                test=test,
                run_id=run_id,
                seed_path=hier_seed_path,
            )
            return

        with open(hier_seed_path, "r") as instance_seeds:
            for line in instance_seeds:
                randseed_fp.write(line)

    def _write_filelist(self, output_path):
        """Generate run.f for the sim."""
        self.vlog_fl = VlogFilelist(
            name=self.name + "/vlog_filelist",
            model_cfg=self.test_cfg.get_model(),
            output_path=output_path,
        )
        self.vlog_fl.write_output(
            unroll=True,
            flatten=False,
            strip=False,
            deduplicate=True,
            absolute_sources=True,
            test_filelist=self.testbench.get_filelist(),
            suite_dir=self.suite_work_dir,
        )

    def _get_plusargs(self):
        pa_list = []
        if self.test_cfg.get_plusargs() is not None:
            plusargs = self.test_cfg.get_plusargs()
            log_event(
                logger,
                logging.DEBUG,
                "sim.plusargs",
                test=self.test_name,
                plusargs=plusargs,
            )
            for plusarg in plusargs:
                if plusargs[plusarg] is not None:
                    pa_list += [f"+{plusarg}={plusargs[plusarg]}"]
                else:
                    pa_list += [f"+{plusarg}"]
        return pa_list

    def _get_plusdefines(self):
        pd_list = []
        if self.test_cfg.pd is not None:
            plusdefines = self.test_cfg.get_plusdefines()
            log_event(
                logger,
                logging.DEBUG,
                "compile.plusdefines",
                test=self.test_name,
                plusdefines=plusdefines,
            )
            for plusdefine in plusdefines:
                if plusdefines[plusdefine] is not None:
                    pd_list += [f"+define+{plusdefine}={plusdefines[plusdefine]}"]
                else:
                    pd_list += [f"+define+{plusdefine}"]
        return pd_list

    def _get_toolchain_prefix(self):
        """Install tree of the resolved simulator exe, if it is worth excluding from
        hashing.

        A vendored or in-repo install would otherwise be content-hashed. The prefix
        is the exe's directory, or its parent when that is ``bin``. It is used only
        when it is a proper subdirectory of the project root, so a simulator in
        ``<root>/bin`` does not exclude everything.
        """
        if self._toolchain_prefix is not _UNSET:
            return self._toolchain_prefix
        self._toolchain_prefix = None
        try:
            resolved = shutil.which(self.rtl_builder_cfg.get_exe())
            if resolved is not None:
                exe_dir = os.path.dirname(os.path.realpath(resolved))
                prefix = (
                    os.path.dirname(exe_dir)
                    if os.path.basename(exe_dir) == "bin"
                    else exe_dir
                )
                if prefix != self._project_root and _path_is_under(
                    prefix, self._project_root
                ):
                    self._toolchain_prefix = prefix
        except Exception:
            # Never fails a build: an underivable prefix means nothing is excluded.
            self._toolchain_prefix = None
        return self._toolchain_prefix

    def _tracked_entry(self, path, *, resolved=False):
        """:func:`_hashed_stat_entry` under this instance's hashing policy.

        ``resolved`` says ``path`` is already a realpath.
        """
        return _hashed_stat_entry(
            path,
            project_root=self._project_root,
            resolved=resolved,
            toolchain_prefix=self._get_toolchain_prefix(),
        )

    def _stamp_relpath(self, path):
        """How this instance spells ``path`` in a build stamp and compile key.

        Unchanged by default. In cache mode paths under the project root are relative
        and anything outside keeps its absolute spelling, so a reader can re-anchor
        every entry against its own root.
        """
        if self.shared_build_root is None:
            return path
        return _relativise_paths(path, self._project_root)

    def _stamp_abspath(self, path):
        """The inverse of :meth:`_stamp_relpath` for a plain path entry.

        Re-anchors a stamp's relative ``deps``/``simv`` spelling against this
        checkout's project root. ``sources`` entries carry option prefixes and are
        only compared, never re-opened.
        """
        if self.shared_build_root is None or not isinstance(path, str):
            return path
        if os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(self._project_root, path))

    def _stamp_tracked_entry(self, stored_path):
        """:meth:`_tracked_entry` for a path spelled the way a stamp spells it.

        Stats this checkout's file and reports it under the stored spelling, so
        :func:`_entry_matches` compares content and stats, not prefixes.
        """
        return [stored_path] + self._tracked_entry(self._stamp_abspath(stored_path))[1:]

    def _stamp_simv_entry(self, simv_path):
        """The stamp's ``simv`` entry for ``simv_path``, in this mode's spelling."""
        path = str(simv_path)
        return [self._stamp_relpath(path)] + _stat_entry(path)[1:]

    def _is_suite_log(self, path) -> bool:
        """Is ``path`` the head's own ``rtl_buddy.log`` in the suite directory?

        Checked by name first so ``realpath`` is only paid for candidates.
        """
        return (
            os.path.basename(path) == DEFAULT_FILE_LOG
            and os.path.realpath(path) == self._suite_log_path
        )

    def _directory_listing(self, dir_path, *, recursive):
        """A listing of the regular files under ``dir_path``, or ``None`` if unreadable.

        Each file has the ``[name, size, mtime_ns, sha]`` shape of
        :meth:`_tracked_entry`, named relative to ``dir_path`` with ``/`` separators.
        ``recursive`` follows the option: ``+incdir+`` is walked (symlinked
        subdirectories are not followed), ``-y`` is flat. Nothing is filtered by
        suffix, because ``-y`` suffixes may come only from a builder-line
        ``+libext+`` and over-approximating is safe.

        Skipped: dot-directories and managed artefact trees
        (:func:`_is_pruned_walk_dir`), files matching
        :data:`_NON_INPUT_FILE_PATTERNS`, and the suite's own ``rtl_buddy.log`` (by
        path). rtl_buddy writes those after the fingerprint that would list them, so
        listing them would make every later validation fail. Dot-files otherwise
        count as inputs.

        ``None`` leaves the entry untracked; an empty listing would claim the
        directory is empty and validate a reuse.
        """

        def _reraise(error):
            # os.walk swallows unopenable directories by default, which would yield an
            # empty listing. Re-raise so the handler returns None.
            raise error

        entries = []
        try:
            if recursive:
                for walk_root, dir_names, file_names in os.walk(
                    dir_path, onerror=_reraise
                ):
                    # In place, because os.walk reads the list back; pruned names are
                    # never walked.
                    dir_names[:] = sorted(
                        name for name in dir_names if not _is_pruned_walk_dir(name)
                    )
                    for name in sorted(file_names):
                        if _is_non_input_file(name):
                            continue
                        path = os.path.join(walk_root, name)
                        if not os.path.isfile(path) or self._is_suite_log(path):
                            # A dangling symlink is not an input; a FIFO must not reach
                            # the hasher's `open()`.
                            continue
                        entries.append((os.path.relpath(path, dir_path), path))
            else:
                with os.scandir(dir_path) as scan:
                    # `is_file` follows symlinks and usually answers from the dirent.
                    names = sorted(
                        item.name
                        for item in scan
                        if item.is_file()
                        and not _is_non_input_file(item.name)
                        and not self._is_suite_log(item.path)
                    )
                entries = [(name, os.path.join(dir_path, name)) for name in names]
        except OSError as e:
            log_event(
                logger,
                logging.DEBUG,
                "compile.build_dir_unreadable",
                test=self.test_name,
                directory=str(dir_path),
                error=str(e),
            )
            return None
        return [
            [name.replace(os.sep, "/")] + self._tracked_entry(path)[1:]
            for name, path in sorted(entries)
        ]

    def _fingerprint_filelist_sources(self, filelist_path):
        """Per-entry ``(line, size, mtime_ns, sha)`` stamps for the generated run.f.

        The content hash goes in the fingerprint, never the key, so an edit rebuilds
        in place. An entry that resolves to a directory (``+incdir+``, ``-y``) gains
        a fifth element, a listing from :meth:`_directory_listing`, so a header edit
        reachable only through an include path or a file appearing in a ``-y``
        directory invalidates the stamp for every builder. That relies on ``run.f``
        carrying absolute paths. The listing over-approximates on purpose:
        over-invalidating costs a recompile, under-invalidating reports a stale
        binary as green.

        Entries that are neither file nor directory (``+define+``, ``+libext+``, a
        missing path) keep only their raw line. Quoted entries are unquoted with
        ``shlex`` before stat; the raw line goes into the stamp.
        """
        base = os.path.dirname(os.path.abspath(filelist_path))
        with open(filelist_path) as filelist_fp:
            lines = [
                stripped
                for stripped in (raw_line.strip() for raw_line in filelist_fp)
                if stripped and not stripped.startswith("//")
            ]
        stamps = []
        for line in lines:
            option_match = _FILELIST_OPTION_RE.match(line)
            option = (option_match.group(1) or "").strip() if option_match else ""
            entry_path = option_match.group(2) if option_match else line
            if entry_path.startswith('"') and entry_path.endswith('"'):
                try:
                    parsed = shlex.split(entry_path)
                except ValueError:
                    # An unbalanced quote degrades to [line, None, None, None] instead
                    # of aborting the compile.
                    parsed = []
                if len(parsed) == 1:
                    entry_path = parsed[0]
            resolved = os.path.normpath(os.path.join(base, entry_path))
            listing = None
            if option in (_INCDIR_OPTION, _LIBRARY_DIR_OPTION) and os.path.isdir(
                resolved
            ):
                listing = self._directory_listing(
                    resolved, recursive=option == _INCDIR_OPTION
                )
            # In cache mode the line is re-spelled relative to the project root so
            # checkouts of the same content produce the same entry; otherwise it is the
            # raw line.
            # A `+define+` is the exception, as on the compile line: an absolute in-root
            # path in its value is compiled into the model, so relativising would merge
            # checkouts that bake in different paths.
            stamp_line = line if _is_macro_shaped(line) else self._stamp_relpath(line)
            if listing is not None:
                # The line stays entry[0], so a changed listing moves the stamp and,
                # outside cache mode, never the compile key.
                stamps.append([stamp_line, None, None, None, listing])
            elif os.path.isfile(resolved):
                # The line, not the resolved path, stays entry[0]: it is what run.f
                # contains and the compile key hashes.
                stamps.append([stamp_line] + self._tracked_entry(resolved)[1:])
            else:
                stamps.append([stamp_line, None, None, None])
        return stamps

    def _key_input_path(self, resolved, *, directory: bool = False):
        """``resolved`` if it is an in-root input worth reading, else None.

        One gate for compile-line tokens and nested filelist entries. ``directory``
        marks an ``+incdir+``/``-y`` search path: it is listed with
        :meth:`_directory_listing`'s name exclusions, so generated headers under an
        artefact tree stay tracked. A file named directly is refused when its name is
        one of rtl_buddy's outputs.
        """
        root = self._project_root
        if not (resolved == root or resolved.startswith(root + os.sep)):
            # Outside the project root, two checkouts name the same bytes, so the
            # absolute text is enough.
            return None
        cache_root = self.shared_build_root
        if cache_root is not None and (
            resolved == cache_root or resolved.startswith(cache_root + os.sep)
        ):
            # The build's own directory; reading it would make the key a function of its
            # output.
            return None
        # Only components below the project root are rtl_buddy's to name; the checkout
        # may sit under `.worktrees/`.
        if any(
            _is_build_tree_name(part)
            for part in Path(os.path.relpath(resolved, root)).parts
        ):
            # A `.shared-builds/` or `obj_dir*` holds a builder's output.
            return None
        if not directory and _is_non_input_file(os.path.basename(resolved)):
            # rtl_buddy's own outputs are written after the fingerprint that would key
            # them.
            return None
        return resolved

    def _embedded_in_root_paths(self, token):
        """Paths written inside a larger option token, absolute or relative, such as
        ``-CFLAGS=-I/checkout/inc`` or ``-CFLAGS=-I../../inc``.

        The token is not a path itself, but the build reads the path, so its content
        must reach the key. Two passes:
        - Absolute: any path under the project root, matched on the same prefix
          :func:`_relativise_paths` rewrites.
        - Relative: matched by the introducing option
          (:data:`_EMBEDDED_PATH_OPTION_RE`). The caller resolves it against the
          builder's cwd; one that resolves to nothing under the root contributes
          nothing.

        Payloads stop at whitespace and packing separators, which under-approximates
        a path containing one. Each distinct spelling is yielded once.
        """
        root_prefix = self._project_root + os.sep
        emitted = set()

        def _fresh(raw):
            if not raw or raw in emitted:
                return False
            emitted.add(raw)
            return True

        for match in re.finditer(re.escape(root_prefix) + r"[^\s:;,'\"]*", token):
            if _fresh(match.group(0)):
                yield match.group(0)
        if self._compile_cwd is None:
            # No cwd to anchor a relative payload to; guessing would key a file the
            # build never opens.
            return
        for match in _EMBEDDED_PATH_OPTION_RE.finditer(token):
            option, payload = match.group(1), match.group(2)
            # `+incdir+a+b` is two directories by filelist convention.
            parts = payload.split("+") if option == _INCDIR_OPTION else [payload]
            for part in parts:
                if os.path.isabs(part):
                    # An in-root one was yielded above; one outside the root stays text.
                    continue
                if _fresh(part):
                    yield part

    def _cmd_token_roles(self, key_cmd):
        """Which compile-line tokens name a path, and what kind.

        Yields ``(index, prefix, raw, kind)``: token position, the ``run.f``-style
        prefix the key records it under, the path as written, and what to read
        (``("dir", recursive)``, ``"file"``, ``"filelist-cwd"``, ``"filelist-rel"``,
        ``"embedded"`` or ``"output"``).

        A token not yielded is not a path and stays verbatim. In particular a
        define's value (``+define+DATA="/checkout/data.hex"``, ``-D``, ``-G``,
        ``-pvalue+``, any ``key=value``) is compiled into the model and must not be
        relocated. ``"output"`` is yielded so its text relativises, but
        :meth:`_cmd_path_tokens` drops it before anything is read.
        """
        awaiting = None
        for index, token in enumerate(key_cmd):
            if not isinstance(token, str):
                awaiting = None
                continue
            if awaiting is not None:
                prefix, kind = awaiting
                awaiting = None
                yield (index, prefix, token, kind)
                continue
            if token.startswith(_INCDIR_OPTION):
                # `+incdir+a+b` names two directories, as every filelist parser reads
                # it.
                for part in token[len(_INCDIR_OPTION) :].split("+"):
                    yield (index, _INCDIR_OPTION, part, ("dir", True))
                continue
            if token in _CMD_PATH_OPTIONS:
                awaiting = (f"{token} ", _CMD_PATH_OPTIONS[token])
                continue
            if token in _CMD_OUTPUT_OPTIONS:
                awaiting = (f"{token} ", "output")
                continue
            if token.startswith(_LIBRARY_DIR_OPTION) and len(token) > len(
                _LIBRARY_DIR_OPTION
            ):
                yield (
                    index,
                    f"{_LIBRARY_DIR_OPTION} ",
                    token[len(_LIBRARY_DIR_OPTION) :],
                    ("dir", False),
                )
                continue
            if _is_macro_shaped(token):
                # A define or parameter assignment; its value is not a path (see
                # :func:`_is_macro_shaped`).
                continue
            if token.startswith(("-", "+")):
                # Another option: its own text stays as written, but a path it embeds
                # (`-CFLAGS=-I/checkout/inc`) still reaches the key. Its argument, if
                # any, is judged on the next pass, since a boolean flag is often
                # followed by a bare source.
                for embedded in self._embedded_in_root_paths(token):
                    yield (index, "", embedded, "embedded")
                continue
            yield (index, "", token, "file")

    def _relativise_cmd(self, key_cmd):
        """The compile line as the fingerprint records it.

        Verbatim outside cache mode. In cache mode only the tokens
        :meth:`_cmd_token_roles` recognises as paths are relativised against the
        project root.
        """
        if self.shared_build_root is None:
            return list(key_cmd)
        relocatable = {index for index, _, _, _ in self._cmd_token_roles(key_cmd)}
        return [
            self._stamp_relpath(token) if index in relocatable else token
            for index, token in enumerate(key_cmd)
        ]

    def _nested_filelist_tokens(self, filelist_path, *, seen, depth, base):
        """Everything a nested ``-f``/``-F`` filelist names, recursively, as
        ``(spelling, resolved, kind)``.

        A filelist on the compile line is keyed by the sources, include directories
        and lists it names, not by its own bytes. ``run.f`` needs no walk because
        :meth:`_write_filelist` unrolls it.

        ``base`` is the directory relative entries resolve against: the builder's cwd
        for a ``-f`` list, the list's own directory for a ``-F`` list (see
        :data:`_CMD_PATH_OPTIONS`); a nested option resets it. ``None`` means the cwd
        is unknown, so relative entries stay text. The walk is cycle-safe on
        ``(realpath, base)`` and bounded by :data:`_NESTED_FILELIST_MAX_DEPTH`; past
        the bound the key gets :data:`_DEPTH_BOUND_MARKER` plus the list's absolute
        path, making it checkout-specific.

        Never raises: unreadable lists, malformed lines and missing entries are not
        keyed, and the compile reports the real problem. Lines are read directly
        because :class:`~rtl_buddy.tools.vlog_filelist.VlogFilelist` validates and
        refuses ``-f``.
        """
        # Identity is the file and the base its entries resolve against: a list reached
        # through both `-f` and `-F` reads as two sets of inputs.
        # The same list under the same base is entered once, and a file has at most two
        # bases, so the walk ends.
        identity = (os.path.realpath(filelist_path), base)
        if depth > _NESTED_FILELIST_MAX_DEPTH:
            # Fail closed: returning quietly would let checkouts that differ only below
            # the bound share a build.
            if not self._depth_bound_logged:
                self._depth_bound_logged = True
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.cache_key_depth_bound",
                    test=self.test_name,
                    filelist=str(filelist_path),
                    depth=_NESTED_FILELIST_MAX_DEPTH,
                )
            yield (
                f"{_DEPTH_BOUND_MARKER}{os.path.abspath(filelist_path)}",
                filelist_path,
                "opaque",
            )
            return
        if identity in seen:
            return
        seen.add(identity)
        try:
            with open(filelist_path) as filelist_fp:
                lines = [
                    stripped
                    for stripped in (raw_line.strip() for raw_line in filelist_fp)
                    if stripped and not stripped.startswith("//")
                ]
        except OSError:
            return
        for line in lines:
            match = _NESTED_FILELIST_OPTION_RE.match(line)
            option = (match.group(1) or "").strip() if match else ""
            entry_path = match.group(2) if match else line
            if option in ("+define+", "+libext+"):
                # Not paths. A define's value is already covered by the list's own
                # content hash.
                continue
            if entry_path.startswith('"') and entry_path.endswith('"'):
                try:
                    parsed = shlex.split(entry_path)
                except ValueError:
                    parsed = []
                if len(parsed) == 1:
                    entry_path = parsed[0]
            parts = entry_path.split("+") if option == _INCDIR_OPTION else [entry_path]
            for part in parts:
                if not part:
                    continue
                if os.path.isabs(part):
                    candidate = os.path.normpath(part)
                elif base is None:
                    # Relative with nothing to anchor it to; guessing would key a file
                    # the build never opens.
                    continue
                else:
                    candidate = os.path.normpath(os.path.join(base, part))
                resolved = self._key_input_path(candidate)
                if resolved is None:
                    continue
                spelled = self._stamp_relpath(resolved)
                if option in ("-f", "-F"):
                    # The nested option resets the rule: `-f` hands its contents the
                    # builder's cwd, `-F` their own directory.
                    child_base = (
                        self._compile_cwd
                        if option == "-f"
                        else os.path.dirname(resolved)
                    )
                    if (os.path.realpath(resolved), child_base) in seen:
                        # Already read under this base.
                        continue
                    yield (f"{option} {spelled}", resolved, "file")
                    yield from self._nested_filelist_tokens(
                        resolved,
                        seen=seen,
                        depth=depth + 1,
                        base=child_base,
                    )
                elif option == _INCDIR_OPTION:
                    yield (f"{_INCDIR_OPTION}{spelled}", resolved, ("dir", True))
                elif option == _LIBRARY_DIR_OPTION:
                    yield (
                        f"{_LIBRARY_DIR_OPTION} {spelled}",
                        resolved,
                        ("dir", False),
                    )
                else:
                    yield (spelled, resolved, "file")

    def _cmd_path_tokens(self, key_cmd):
        """Compile-line inputs inside the project root, as ``(spelling, resolved,
        kind)``.

        ``spelling`` is the ``run.f``-style key spelling (``+incdir+rel``, ``-y rel``
        or ``rel``). A relative path resolves against :attr:`_compile_cwd`; with no
        plan settled it is left as text. An input is recorded under what it resolves
        to, relative to the project root, because ``+incdir+inc`` on the compile line
        and in ``run.f`` name different directories.

        Only ``+incdir+`` and ``-y`` are listed as directories.
        """
        embedded_seen: set[str] = set()
        for _, prefix, raw, kind in self._cmd_token_roles(key_cmd):
            if kind == "output":
                continue
            if os.path.isabs(raw):
                candidate = os.path.normpath(raw)
            elif self._compile_cwd is not None:
                # The builder resolves a relative compile-line path against its own cwd,
                # so read the same file.
                candidate = os.path.normpath(os.path.join(self._compile_cwd, raw))
            else:
                # No plan yet, so nothing to anchor it to.
                continue
            directory = kind == "embedded" or (
                isinstance(kind, tuple) and kind[0] == "dir"
            )
            resolved = self._key_input_path(candidate, directory=directory)
            if resolved is None:
                continue
            # Spelled by what it resolves to, not the text that named it: one spelling
            # for two directories would let the `covered` check drop a real input.
            spelling = f"{prefix}{self._stamp_relpath(resolved)}"
            if kind == "embedded":
                # One directory named twice (an absolute `-I` and a relative one) is one
                # input.
                if resolved in embedded_seen:
                    continue
                embedded_seen.add(resolved)
                # Classified by what it is: a directory is keyed by its listing under
                # the `+incdir+` spelling (which dedupes against `run.f`), a file by its
                # sha.
                if os.path.isdir(resolved):
                    yield (
                        f"{_INCDIR_OPTION}{self._stamp_relpath(resolved)}",
                        resolved,
                        ("dir", True),
                    )
                elif self._key_input_path(resolved) is not None:
                    yield (spelling, resolved, "file")
                continue
            if kind in ("filelist-cwd", "filelist-rel"):
                # The list's own bytes, then everything it names, read against the base
                # its option implies.
                yield (spelling, resolved, "file")
                yield from self._nested_filelist_tokens(
                    resolved,
                    seen=set(),
                    depth=1,
                    base=(
                        self._compile_cwd
                        if kind == "filelist-cwd"
                        else os.path.dirname(resolved)
                    ),
                )
                continue
            yield (spelling, resolved, kind)

    def _fingerprint_cmd_inputs(self, key_cmd, sources):
        """Content identity for the inputs the compile line names.

        ``sources`` covers ``run.f``. Inputs reaching the builder through
        ``builder-opts.compile-time`` or a subclass's flags would otherwise enter the
        key as text only, so two checkouts with different header content could share
        one persistent ``obj_dir``. Each such path contributes what its ``run.f``
        equivalent would: a file its sha, a directory the ``[name, sha]`` pairs of
        its listing. A path ``run.f`` already names under the same spelling is
        skipped.

        Cache mode only, and never part of the fingerprint: a listing there would
        reintroduce mtime sensitivity and invalidate every existing stamp.
        """
        covered = {entry[0] for entry in sources if isinstance(entry, list) and entry}
        entries = []
        for spelling, resolved, kind in self._cmd_path_tokens(key_cmd):
            if spelling in covered:
                continue
            if kind == "opaque":
                # A filelist that was not read. Its spelling carries the absolute path
                # that makes the key checkout-specific.
                entries.append([spelling, None])
                continue
            # In-root by construction, so an unhashable input falls back to its stats
            # (see :func:`_key_content_identity`).
            if kind == "file":
                if not os.path.isfile(resolved):
                    continue
                entry = _key_content_identity(
                    spelling, self._tracked_entry(resolved), relocated=True
                )
            else:
                if not os.path.isdir(resolved):
                    continue
                listing = self._directory_listing(resolved, recursive=kind[1])
                if listing is None:
                    continue
                entry = [
                    spelling,
                    [
                        _key_content_identity(inner[0], inner, relocated=True)
                        if isinstance(inner, list) and inner
                        else inner
                        for inner in listing
                    ],
                ]
            entries.append(entry)
        return entries

    def _fingerprint_toolchain(self, exe):
        """Which simulator install this build would come out of.

        ``cmd`` records the configured executable name, the same string whichever
        install ``PATH`` resolves. Without this entry, switching simulators would
        leave every shared stamp validating and the run would report PASS on the old
        toolchain's binary.

        ``exe`` goes in the key, so two installs get two build dirs. Size, mtime and
        version go in the stamp, so an in-place upgrade rebuilds in place.
        """
        resolved = shutil.which(exe)
        entry = {
            "exe": resolved or exe,
            "size": None,
            "mtime_ns": None,
            "version": None,
        }
        if resolved is None:
            # Nothing to stat; the compile is about to fail with a better message.
            return entry
        try:
            stat = os.stat(resolved)
        except OSError:
            return entry
        entry["size"] = stat.st_size
        entry["mtime_ns"] = stat.st_mtime_ns
        # A wrapper script (verilator's `bin/verilator`) can keep its size and mtime
        # across an upgrade, so the version banner catches that.
        entry["version"] = _probe_toolchain_version(
            resolved, self._get_simulator_family(), stat.st_mtime_ns
        )
        return entry

    def _compile_fingerprint(self, key_cmd, filelist_path):
        """Everything that determines the compiled binary.

        Runtime-only inputs (seed, plusargs, run-time opts, timeout, coverage path)
        are excluded. Must stay JSON-native: the stamp check compares this dict with
        a ``json.loads()`` round-trip, so a tuple would silently disable reuse.
        """
        return {
            # Relativised in cache mode, but only tokens that are paths. A
            # `+define+DATA="/checkout/data.hex"` keeps its value, which the model bakes
            # in. See :meth:`_cmd_token_roles`.
            "cmd": self._relativise_cmd(key_cmd),
            "env": dict(sorted(self._get_extra_compile_env().items())),
            "sources": self._fingerprint_filelist_sources(filelist_path),
            "toolchain": self._fingerprint_toolchain(key_cmd[0]),
        }

    @staticmethod
    def _key_source_entry(entry, *, content: bool):
        """One ``sources`` entry as the compile key reads it.

        Path-only by default (``entry[0]``, the run.f line), so an edit rebuilds in
        place. With ``content`` (cache mode) the content digest joins it: a file's
        sha, or a directory listing's ``[name, sha]`` pairs. Size and mtime are
        excluded, as in :func:`_entry_identity`. An unrecognised shape passes through
        unchanged.
        """
        if not content or not isinstance(entry, list) or not entry:
            return entry[0] if isinstance(entry, list) and entry else entry
        relocated = isinstance(entry[0], str) and _key_spelling_is_relocated(entry[0])
        if _is_directory_entry(entry):
            # The directory decides, not each file: listing names are relative to it by
            # construction.
            return [
                entry[0],
                [
                    _key_content_identity(inner[0], inner, relocated=relocated)
                    if isinstance(inner, list) and inner
                    else inner
                    for inner in entry[-1]
                ],
            ]
        if len(entry) == 4:
            return _key_content_identity(entry[0], entry, relocated=relocated)
        return entry

    @staticmethod
    def _compile_config_key(fingerprint, *, content: bool = False, cmd_inputs=None):
        """Short stable hash naming the shared build dir.

        By default it excludes source size, mtime and content hash and the
        toolchain's size, mtime and version, so an RTL edit or in-place simulator
        upgrade rebuilds in the same dir and the stamp comparison catches the
        staleness.

        ``content`` makes the directory content-addressed, for the persistent cache
        shared by every checkout, where rebuilding in place would let two worktrees
        overwrite one obj_dir. The caller pairs it with a relativised fingerprint.
        ``cmd_inputs`` (see :meth:`_fingerprint_cmd_inputs`) joins the key only under
        ``content``.
        """
        config = {
            "cmd": fingerprint["cmd"],
            "env": fingerprint["env"],
            "filelist": [
                VlogSim._key_source_entry(entry, content=content)
                for entry in fingerprint["sources"]
            ],
            # The install, not its version: an in-place upgrade reuses the dir, a
            # different install gets its own so an A/B keeps both builds.
            "toolchain": fingerprint["toolchain"]["exe"],
        }
        if content:
            config["cmd_inputs"] = cmd_inputs or []
        digest = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return digest[:16]

    def _write_compile_transcript(self, run_str, result):
        """Persist the compile command and its captured output; return the path, or
        None.

        Written on every compile that ran, pass or fail, so its presence never means
        "nothing compiled" (a reuse writes a breadcrumb). Best-effort: a builder that
        exited 0 must not fail because the transcript could not be written.
        """
        transcript_path = self._get_compile_transcript_path()
        try:
            self._replace_text(
                transcript_path,
                f"Command: {run_str}\n\n"
                "=== stderr ===\n"
                f"{result.stderr or ''}"
                "\n=== stdout ===\n"
                f"{result.stdout or ''}",
            )
        except OSError as e:
            log_event(
                logger,
                logging.DEBUG,
                "compile.transcript_unwritable",
                test=self.test_name,
                error=str(e),
            )
            return None
        return transcript_path

    def _compile_queued_for_license(self, result):
        """Did a ``vcs`` elaboration wait in the ``-licqueue`` queue?

        Waiting for a seat is not compile work, and under dispatch it decides whether
        a timed-out build job needs a bigger reservation or a freer license server.
        Non-VCS families never queue.
        """
        if self._get_simulator_family() != "vcs":
            return False
        return has_license_queue_marker((result.stdout or "") + (result.stderr or ""))

    def _share_build_unsupported_reason(self):
        return share_build_unsupported_reason(self.rtl_builder_cfg)

    def _vcs_shared_output_argv(self, build_dir):
        """VCS flags that put the whole build inside ``build_dir``.

        ``-o`` sets the executable (``simv.daidir`` goes beside it) and ``-Mdir`` the
        intermediate C tree. Both point into the shared dir so a later rebuild from
        another test reuses the incremental tree.
        """
        return [
            "-o",
            str(Path(build_dir) / "simv"),
            f"-Mdir={Path(build_dir) / 'csrc'}",
        ]

    @staticmethod
    def _strip_vcs_output_opts(opts):
        """Split configured VCS opts into (kept, dropped ``-o``/``-Mdir``).

        A shared build owns the output location, so a ``builder-opts`` entry that
        sets one is dropped. Handles ``-Mdir=dir`` and ``-Mdir dir``.
        """
        kept, dropped = [], []
        skip_next = False
        for opt in opts:
            if skip_next:
                skip_next = False
                dropped.append(opt)
                continue
            if opt in ("-o", "-Mdir"):
                skip_next = True
                dropped.append(opt)
            elif opt.startswith("-Mdir="):
                dropped.append(opt)
            else:
                kept.append(opt)
        return kept, dropped

    def _collect_build_deps(self, build_dir, compile_cwd):
        """Stamps for every input the verilation consumed, or ``None``.

        Reads Verilator's dependency file, which closes the gap the filelist
        fingerprint leaves for headers reached only through ``+incdir+``/``-y``.
        ``None`` means no dependency information exists (a non-Verilator family, or
        no ``.d`` emitted); it is stored as such because "we do not know" must not
        validate a reuse.

        Paths are resolved against ``compile_cwd`` and stored absolute, by the name
        the build used (``normpath``, not ``realpath``), so retargeting a symlink
        invalidates the stamp. The compile's own ``run.f`` is excluded, matched by
        realpath: it is regenerated on every compile and its contents are already
        fingerprinted.
        """
        # `build_dir` is an absolute shared dir on one path and a bare name on the
        # other; resolve against the compile cwd.
        depend_files = sorted(
            (Path(compile_cwd) / build_dir).glob(_VERILATOR_DEPEND_GLOB)
        )
        if not depend_files:
            return None
        filelist_path = os.path.realpath(self._get_filelist_path())
        seen: dict[str, None] = {}
        for depend_file in depend_files:
            try:
                text = depend_file.read_text()
            except OSError as e:
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.build_deps_unreadable",
                    test=self.test_name,
                    depend_file=str(depend_file),
                    error=str(e),
                )
                return None
            for prerequisite in parse_depend_prerequisites(text):
                declared = os.path.normpath(os.path.join(compile_cwd, prerequisite))
                if os.path.realpath(declared) != filelist_path:
                    seen.setdefault(declared, None)
        # Sorted by the stored spelling, not the absolute path: in cache mode project
        # paths sort relative and toolchain headers absolute, and the list is compared
        # by position. Identical to `sorted(seen)` in the default mode.
        spelled = sorted((self._stamp_relpath(path), path) for path in seen)
        return [[stored] + self._tracked_entry(path)[1:] for stored, path in spelled]

    def _deps_unchanged(self, test_name, deps, *, quiet=False):
        """Have any of the stamp's recorded inputs changed on disk?

        Entry-wise through :func:`_entry_matches`: inputs inside the project root are
        decided by content, others by stats. Any unrecognised shape, up to ``deps``
        not being a list, answers False (a rebuild) rather than raising, since a
        mixed-version cluster can hand a node a stamp it does not understand.
        """
        if not isinstance(deps, list):
            return self._note_stamp_mismatch("the stamp's dependency list is corrupt")
        for entry in deps:
            if not isinstance(entry, list) or len(entry) != 4:
                return self._note_stamp_mismatch(
                    "the stamp's dependency list is corrupt"
                )
            if not isinstance(entry[0], str):
                # `os.stat` takes a file descriptor for an int, so a corrupt stamp must
                # not reach it.
                return self._note_stamp_mismatch(
                    "the stamp's dependency list is corrupt"
                )
            if not _entry_matches(entry, self._stamp_tracked_entry(entry[0])):
                # The one question worth answering when a warm run unexpectedly
                # recompiles.
                if not quiet:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "compile.build_dep_changed",
                        test=test_name,
                        dependency=entry[0],
                    )
                return self._note_stamp_mismatch(
                    f"a consumed input changed: {entry[0]}"
                )
        return True

    def _note_stamp_mismatch(self, reason: str) -> bool:
        """Record why the stamp lost and answer False, for the caller's ``return``."""
        self.stamp_mismatch_reason = reason
        return False

    def _shared_build_is_valid(
        self, build_dir, fingerprint, *, test_name=None, quiet=False
    ):
        return self._build_stamp_is_valid(
            build_dir,
            Path(build_dir) / "simv",
            fingerprint,
            test_name=test_name,
            quiet=quiet,
        )

    def _build_stamp_is_valid(
        self, stamp_dir, simv_path, fingerprint, *, test_name=None, quiet=False
    ):
        """Does the stamp in ``stamp_dir`` still describe ``simv_path``?

        The two are separate because an unshared build keeps the stamp in the test's
        compile work dir while ``builder-simv:`` decides where the binary lands.
        Everything but the tracked inputs compares by equality; ``sources`` and
        ``deps`` go entry-wise through :func:`_entry_matches`.

        ``quiet`` suppresses the diagnostics for :meth:`compile`'s unlocked
        pre-check, whose in-lock repeat owns them. Every False verdict records its
        reason in :attr:`stamp_mismatch_reason`, which a gated sim job reports (its
        INFO-level log lacks the DEBUG lines).
        """
        self.stamp_mismatch_reason = None
        simv_path = Path(simv_path)
        stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
        if not simv_path.is_file() or not stamp_path.is_file():
            return self._note_stamp_mismatch("no stamp or no simv in the build dir")
        try:
            stored = json.loads(stamp_path.read_text())
        except (OSError, json.JSONDecodeError):
            return self._note_stamp_mismatch("the stamp is unreadable")
        if not isinstance(stored, dict) or "deps" not in stored:
            # Written before dependency tracking existed; its silence about headers
            # means one rebuild.
            return self._note_stamp_mismatch("the stamp predates dependency tracking")
        if (stored.get("root") is not None) != (self.shared_build_root is not None):
            # Stamp inputs are spelled relative to the project root in cache mode and
            # absolute otherwise, so a stamp from the other mode means one rebuild.
            # Enabling or disabling the cache root recompiles once for this reason.
            return self._note_stamp_mismatch(
                "the stamp was written in the other shared-build mode"
            )
        # The executable is an output, so the input fingerprint does not cover it. An
        # absolute `builder-simv:` is one path shared by every test using that builder
        # while the stamp is per test; without this check test_a's stamp keeps
        # validating after test_b overwrote the binary.
        if stored.get("simv") != self._stamp_simv_entry(simv_path):
            if not quiet:
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.build_dep_changed",
                    test=test_name,
                    dependency=str(simv_path),
                )
            return self._note_stamp_mismatch(f"the simv changed: {simv_path}")
        if not isinstance(fingerprint, dict):
            # A caller asserting a stamp is stale passes no fingerprint.
            return self._note_stamp_mismatch("no fingerprint to compare against")
        stored_inputs = {
            key: value for key, value in stored.items() if key not in _STAMP_META
        }
        # `sources` is compared entry-wise by _entry_matches; cmd/env/toolchain stay
        # exact. Popping from copies keeps both sides symmetrical, and a stamp with no
        # `sources` still fails because None is not a list.
        stored_sources = stored_inputs.pop("sources", None)
        current_inputs = dict(fingerprint)
        current_sources = current_inputs.pop("sources", None)
        if stored_inputs != current_inputs:
            if not quiet:
                _log_stale_stamp_toolchain(
                    stored_inputs, current_inputs, test_name=test_name
                )
            return self._note_stamp_mismatch("the compile line or toolchain changed")
        # A stamp that recorded the builder's dependency list decides file content
        # there, so directory listings compare by name alone.
        deps = stored["deps"]
        if not _entry_lists_match(
            stored_sources, current_sources, listing_names_only=deps is not None
        ):
            # Name what changed, as the deps path does.
            entry = _first_entry_mismatch(
                stored_sources, current_sources, listing_names_only=deps is not None
            )
            if not quiet:
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.build_source_changed",
                    test=test_name,
                    entry=entry,
                )
            return self._note_stamp_mismatch(f"a compile input changed: {entry}")
        if deps is None:
            # The builder emitted no dependency file. Reuse is sound because `sources`
            # carries a listing of every `+incdir+`/`-y` directory; the remaining
            # unknown is what the filelist never named (see docs/known-issues.md).
            return True
        if stored.get("deps_format") != _DEPS_FORMAT:
            return self._note_stamp_mismatch(
                "the stamp's dependency list predates declared-path tracking"
            )
        return self._deps_unchanged(test_name, deps, quiet=quiet)

    def pre(self, run_id=_UNSET):
        """Run the test's ``preproc`` hook; return a setup-failure string or None.

        ``run_id`` defaults to ``self.run_id``, right when the hook runs once per
        run. A caller that runs the hook once for several runs
        (:meth:`TestRunner.run_multiple`) must pass ``None`` explicitly.
        """
        script_path = self.test_cfg.get_preproc_path()
        if script_path is None:
            log_event(logger, logging.DEBUG, "preproc.skipped", test=self.test_name)
            return None
        if run_id is _UNSET:
            run_id = self.run_id

        # Remove this run's stale retry transcript before the hook: a run dir whose PRE
        # fails never reaches compile().
        try:
            Path(self._get_retry_transcript_path()).unlink(missing_ok=True)
        except OSError:
            pass

        with open(script_path, "r") as file:
            code = file.read()

        # `artifact_dir` stays test-keyed and `run_artifact_dir` is where a
        # run-dependent generator writes. They are the same directory when one hook run
        # serves the whole invocation. Both are created here.
        artifact_dir = self._ensure_artifact_dir()
        run_artifact_dir = self._ensure_artifact_dir(run_id=run_id)

        # The preproc script receives self.test_cfg as root_cfg and may mutate it;
        # compile and sim use the result.
        try:
            ns = exec_hook_script(
                script_path,
                code,
                stage="preproc",
                logger=logger,
                test_cfg=self.test_cfg,
                root_cfg=self.root_cfg,
                suite_dir=self.suite_work_dir,
                artifact_dir=artifact_dir,
                run_id=run_id,
                run_artifact_dir=run_artifact_dir,
            )
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "preproc.failed",
                test=self.test_name,
                script=script_path,
                error=e,
            )
            logger.debug("preproc traceback", exc_info=True)
            return f"Setup failed in preproc: {e}"

        import_error = self._check_preproc_imports(ns, script_path)
        if import_error is not None:
            log_event(
                logger,
                logging.ERROR,
                "preproc.import_collision",
                test=self.test_name,
                script=script_path,
                error=import_error,
            )
            return f"Setup failed in preproc: {import_error}"

        log_event(
            logger,
            logging.INFO,
            "preproc.completed",
            test=self.test_name,
            script=script_path,
        )
        return None

    def _find_suite_dir(self, start_dir: str, project_root: str) -> str | None:
        """Walk up from start_dir to project_root, returning the first dir with
        tests.yaml.
        """
        start_dir = os.path.abspath(start_dir)
        project_root = os.path.abspath(project_root)
        current = start_dir
        while True:
            if os.path.isfile(os.path.join(current, "tests.yaml")):
                return current
            if current == project_root:
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None

    def _check_preproc_imports(self, ns, script_path):
        """Fail loudly if the preproc imported a module from a different suite
        directory.
        """
        script_dir = os.path.dirname(os.path.abspath(script_path))
        get_root = getattr(self.root_cfg, "get_project_rootdir", None)
        if get_root is not None:
            project_root = os.path.abspath(get_root())
        else:
            project_root = script_dir
        script_suite = self._find_suite_dir(script_dir, project_root)
        for value in ns.values():
            if not isinstance(value, types.ModuleType):
                continue
            mod_file = getattr(value, "__file__", None)
            if mod_file is None:
                continue
            mod_file = os.path.abspath(mod_file)
            try:
                if os.path.commonpath([mod_file, project_root]) != project_root:
                    continue
            except ValueError:
                continue
            mod_dir = os.path.dirname(mod_file)
            mod_suite = self._find_suite_dir(mod_dir, project_root)
            if mod_suite is not None and mod_suite != script_suite:
                return (
                    f"preproc imported module '{value.__name__}' from a different "
                    f"suite directory ({mod_suite}); use a unique module name or "
                    "isolate the helper to avoid sys.modules caching collisions"
                )
        return None

    def _record_compile(self, *, duration_sec, reused, verilate_sec=None):
        """Set :attr:`last_compile` to this instance's compile outcome.

        The build job folds it into the build envelope and the in-process path into
        the run's result envelope. Best-effort telemetry. ``verilate_sec`` (from a
        preceding verilate job) makes ``duration_sec`` the whole compile and is
        reported beside ``build_sec``.
        """
        self.last_compile = {
            "duration_sec": duration_sec,
            "builder": self.rtl_builder_cfg.get_name(),
            "reused": reused,
        }
        if verilate_sec is not None:
            self.last_compile["duration_sec"] = round(
                verilate_sec + (duration_sec or 0), 2
            )
            self.last_compile["verilate_sec"] = verilate_sec
            self.last_compile["build_sec"] = duration_sec

    def _build_compile_plan(self):
        """Derive this test's :class:`_CompilePlan`, the pre-builder half.

        Writes ``run.f`` as a side effect (the fingerprint stats what it names) and
        sets ``self._shared_build_dir``, which ``_get_simv_path()`` branches on. It
        does not touch stamps or create the shared dir, since a probe must not
        destroy the reuse it asks about.
        """
        rtl_builder_cfg = self.rtl_builder_cfg
        compile_work_dir = self._ensure_artifact_dir()
        # The cwd `run_managed_process` will use, so a `-f` filelist's relative entries
        # resolve as the builder resolves them.
        self._compile_cwd = compile_work_dir
        # A probe settles the builder for this config. Recording it lets a config that
        # never reaches a builder (filelist failure, killed job) still name it, with
        # `reused` left unknown.
        self._record_compile(duration_sec=None, reused=None)

        builder_opts = self._filter_builder_opts(self._get_builder_compile_opts())
        extra_compile_flags = self._get_extra_compile_flags()
        assertion_flags = self._get_verilator_assertion_flags(builder_opts)
        # After the extra flags: the subclass that emits its own top flag emits it there
        # and this must see it.
        top_flags = self._get_top_module_flags(builder_opts, extra_compile_flags)
        plusdefines = self._get_plusdefines()
        is_verilator = os.path.basename(rtl_builder_cfg.get_exe()).startswith(
            "verilator"
        )

        # Compile outputs stay in the suite work dir; explicit paths let the sim cwd
        # vary.
        filelist_path = self._get_filelist_path()
        self._write_filelist(
            filelist_path
        )  # raises FilelistError on a bad path; caught by TestRunner

        plan = _CompilePlan(
            compile_work_dir=compile_work_dir,
            filelist_path=filelist_path,
            build_dir=self._get_build_dir(),
            builder_opts=builder_opts,
            extra_compile_flags=extra_compile_flags,
            assertion_flags=assertion_flags,
            top_flags=top_flags,
            plusdefines=plusdefines,
            is_verilator=is_verilator,
            # The group is the resolved output path the compile writes: the
            # single-writer resource the pool must not hand to two workers.
            # For verilator/icarus the output is under the per-test compile dir, so
            # every test is its own group. For a family that honours `builder-simv:`,
            # two configs can name one executable (an absolute pin, or a relative path
            # whose `..` escapes the workspace), and grouping on compile dirs would run
            # two builders onto one binary.
            # `realpath` because two spellings can meet through a symlinked parent; a
            # nonexistent tail is normalised textually.
            # Resolved here, before `_shared_build_dir` is set, so it is the unshared
            # output; the share-build branch overrides it.
            group_dir=os.path.realpath(self._get_simv_path()),
        )
        if not self.share_build:
            return plan

        plan.unsupported_reason = self._share_build_unsupported_reason()
        # One key_cmd for both branches, so they cannot stop agreeing.
        key_cmd = (
            [rtl_builder_cfg.get_exe()]
            + builder_opts
            + extra_compile_flags
            + assertion_flags
            # The top flag changes which modules are elaborated and the model name, so
            # testbenches differing only in `toplevel:` must not share a build dir.
            # Empty when no `toplevel:` is declared, leaving existing keys unchanged.
            + top_flags
            + plusdefines
        )
        if self._get_simulator_family() == "icarus":
            # The Icarus `simv` wrapper lives in the shared dir and bakes in these args
            # (CocotbSim adds the VPI module), so tests differing only there need
            # different keys.
            key_cmd = key_cmd + self._icarus_vvp_extra_args()
        plan.key_cmd = key_cmd
        # Keyed on the configured compile line, not the output flags compile() appends
        # later; those derive from the key.
        plan.fingerprint = self._compile_fingerprint(key_cmd, filelist_path)

        if plan.unsupported_reason is None:
            cache_root = self.shared_build_root
            shared_dir = shared_build_dir(
                self.suite_work_dir,
                # Content-addressed where the directory is persistent and shared between
                # checkouts.
                self._compile_config_key(
                    plan.fingerprint,
                    content=cache_root is not None,
                    # Compile-line inputs that `run.f` does not name, so an `+incdir+`
                    # from `builder-opts.compile-time` is content-addressed too.
                    cmd_inputs=(
                        self._fingerprint_cmd_inputs(
                            key_cmd, plan.fingerprint["sources"]
                        )
                        if cache_root is not None
                        else None
                    ),
                ),
                cache_root=cache_root,
                project_root=self._project_root,
            )
            plan.shared_dir = shared_dir
            self._shared_build_dir = str(shared_dir)
            plan.build_dir = str(shared_dir)
            plan.group_dir = str(shared_dir)
        else:
            # Emitted from the plan, so it lands at probe time, before compile.config
            # for the same test.
            log_event(
                logger,
                logging.WARNING,
                "compile.share_build_unsupported",
                test=self.test_name,
                simulator=self._get_simulator_family(),
                reason=plan.unsupported_reason,
            )
            # The build cannot be shared but can still be reused by the next process
            # asking for this test, so a dispatched fan-out compiles once in the build
            # job. Same fingerprint and stamp file, kept in the test's own compile work
            # dir. `group_dir` stays the resolved output path.
        return plan

    def _gated_build_verdict(self, fingerprint=None):
        """What the build envelope says about this test, as ``(kind, record)``.

        Called by a gated job whose build stamp failed to validate.
        - ``("failed", record)``: the builder ran for this config and exited
          non-zero. The record must carry a ``returncode``, since the envelope's
          ``failed`` list also holds PRE/setup failures that a retry could fix. If
          the record's ``fingerprint_sha`` differs from ``fingerprint`` (this job's
          own), the inputs moved and the result is ``(None, None)``.
        - ``("built", record)``: the build job built this config, so the caller
          declines to compile under the simulation reservation. ``record`` is ``{}``
          for an older build job.
        - ``(None, None)``: nothing decisive (no build job, unreadable or stale
          envelope, config never reached, or inputs moved). The retry runs and writes
          ``compile.retry.log``.

        Best-effort: an unreadable envelope falls back to the retry.
        """
        if not self.expect_prebuilt or self.build_result_json is None:
            return None, None
        try:
            envelope = load_build_result_json(self.build_result_json)
        except Exception:  # noqa: BLE001 - advisory; never costs a run
            return None, None
        if not envelope:
            return None, None
        record = next(
            (
                entry
                for entry in envelope.get("builds") or ()
                if entry.get("test") == self.test_name
            ),
            None,
        )
        if self.test_name not in set(envelope.get("failed") or ()):
            if self.test_name in set(envelope.get("built") or ()):
                return "built", (record or {})
            return None, None
        # Compiler evidence or nothing: no record, or one without a returncode,
        # describes a failure that never reached a builder.
        if record is None or record.get("returncode") is None:
            return None, None
        recorded_sha = record.get("fingerprint_sha")
        if recorded_sha is not None:
            own_sha = _fingerprint_sha(fingerprint)
            if own_sha is not None and recorded_sha != own_sha:
                # The build failed a different compile than this job would run, so the
                # retry is earned.
                log_event(
                    logger,
                    logging.INFO,
                    "compile.build_failure_inputs_changed",
                    test=self.test_name,
                    run_id=self.run_id,
                    recorded_sha=recorded_sha,
                    own_sha=own_sha,
                )
                return None, None
        return "failed", record

    def _decline_gated_recompile(self, record, fingerprint, build_dir):
        """Fail a gated job whose build job built this test.

        The binary exists in ``build_dir`` but this job's stamp check disagreed.
        Recompiling would run under the simulation reservation, which the scheduler
        kills for memory, would block every sibling element on the directory, and
        would replace the real drift with `signal 9`. The test fails with the reason
        instead.

        ``stamp_written: false`` in the record means the build succeeded and only the
        stamp write failed. Otherwise equal or unknown fingerprints mean the
        disagreement is in the stamp itself; different ones mean this node's inputs
        differ from the build job's (a ``preproc`` that generates something
        different, or a mid-run edit).
        """
        recorded_sha = (record or {}).get("fingerprint_sha")
        own_sha = _fingerprint_sha(fingerprint)
        reason = self.stamp_mismatch_reason or "the build's stamp did not validate"
        inputs_differ = (
            recorded_sha is not None and own_sha is not None and recorded_sha != own_sha
        )
        # The build job knows its stamp write failed; name that instead of "no stamp or
        # no simv".
        stamp_unwritten = (record or {}).get("stamp_written") is False
        if stamp_unwritten:
            reason = "the build job could not write the build stamp"
        what = (
            "but could not write its build stamp"
            if stamp_unwritten
            else (
                "from different compile inputs than this job derived"
                if inputs_differ
                else "and its stamp still does not validate here"
            )
        )
        transcript = self._get_build_compile_transcript_path()
        log_event(
            logger,
            logging.ERROR,
            "compile.build_stamp_rejected",
            test=self.test_name,
            run_id=self.run_id,
            build_dir=build_dir,
            reason=reason,
            inputs_differ=inputs_differ,
            stamp_unwritten=stamp_unwritten,
            recorded_sha=recorded_sha,
            own_sha=own_sha,
            build_result=str(self.build_result_json),
        )
        # One line: `render_summary` puts it in a table cell.
        self.compile_fail_desc = (
            f"build job built this test {what} ({reason}); not recompiling "
            f"under the simulation reservation (see {transcript})"
        )
        self.last_compile_failure = {
            "returncode": 1,
            "transcript": transcript,
        }
        return 1

    def _compile_plan(self):
        """The cached :class:`_CompilePlan`, deriving it on first ask."""
        if self._compile_plan_cache is None:
            self._compile_plan_cache = self._build_compile_plan()
        return self._compile_plan_cache

    def compile_group_dir(self):
        """The directory this test's compile will write into.

        A dispatched build job groups on it: configs sharing a value compile
        serially. Raises :class:`FilelistError` as :meth:`compile` does.
        """
        return self._compile_plan().group_dir

    def _compile_argv_base(self, plan, *, quiet=False):
        """The builder command line ``plan`` would run, before any phase.

        Derived here only, since the reuse breadcrumb
        (:meth:`_write_reuse_transcript`) records this command and a second assembly
        would drift. ``quiet`` drops side effects of a real compile: the VCS strip's
        DEBUG record, the assertions line and the Icarus snapshot directory.
        """
        # Copied: the VCS strip rewrites these, and the plan records what was decided.
        builder_opts = list(plan.builder_opts)
        extra_compile_flags = list(plan.extra_compile_flags)
        build_dir = plan.build_dir
        family = self._get_simulator_family()
        shared = self._shared_build_dir is not None

        run_cmd = [self.rtl_builder_cfg.get_exe()]
        if shared and family == "vcs":
            # Strip both sources of compile flags: a `-o` from
            # _get_extra_compile_flags() would come after _vcs_shared_output_argv() and
            # win on VCS's duplicate-option precedence, so the simv would land outside
            # the shared dir and every job would recompile.
            builder_opts, dropped_opts = self._strip_vcs_output_opts(builder_opts)
            extra_compile_flags, dropped_extra = self._strip_vcs_output_opts(
                extra_compile_flags
            )
            dropped_opts += dropped_extra
            if dropped_opts and not quiet:
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.share_build_opts_overridden",
                    test=self.test_name,
                    dropped=dropped_opts,
                    build_dir=build_dir,
                )
        run_cmd += builder_opts

        if plan.is_verilator:
            run_cmd += ["--Mdir", build_dir]
        elif family == "icarus":
            # Icarus has no -Mdir equivalent; write one .vvp snapshot into the build dir
            # and let execute() wrap it.
            if not quiet:
                Path(self._get_icarus_snapshot_path()).parent.mkdir(
                    parents=True, exist_ok=True
                )
            run_cmd += ["-o", self._get_icarus_snapshot_path()]
        elif shared and family == "vcs":
            run_cmd += self._vcs_shared_output_argv(build_dir)

        run_cmd += extra_compile_flags

        if plan.assertion_flags:
            run_cmd += plan.assertion_flags
            if not quiet:
                log_event(
                    logger,
                    logging.INFO,
                    "compile.assertions_enabled",
                    test=self.test_name,
                    flags=plan.assertion_flags,
                )

        # Pin the elaboration root in the same position as in `key_cmd`, so the reuse
        # breadcrumb and real compile agree.
        run_cmd += plan.top_flags

        run_cmd += plan.plusdefines

        run_cmd += ["-f", plan.filelist_path]
        return run_cmd

    def _compile_argv(self, plan, *, quiet=False):
        """:meth:`_compile_argv_base`, rewritten for this job's phase.

        What callers ask for as "the command this compile runs".
        :meth:`_compile_with_plan` assembles the halves itself because it inspects
        the base line first.
        """
        return self._apply_build_phase(self._compile_argv_base(plan, quiet=quiet))

    @staticmethod
    def _splittable(run_cmd):
        """Is there a build step in ``run_cmd`` for a verilate job to omit?

        A line with neither ``--binary`` nor ``--build`` already stops after the
        front end, so the verilate job runs it whole and the build job reuses that
        build.
        """
        return any(arg in _BUILD_STEP_FLAGS for arg in run_cmd)

    def _apply_build_phase(self, run_cmd):
        """Rewrite ``run_cmd`` for the half of the compile this job runs.

        Applied at emission, never to ``plan.builder_opts``, so both halves share one
        key. ``verilate`` replaces ``--binary`` with ``--exe --main --timing`` and
        drops ``--build``, so Verilator emits C++ and ``V<top>.mk`` under ``--Mdir``
        and stops. ``build`` adds ``--no-verilate`` so the make runs over what the
        first half left.
        """
        if self.build_phase == BUILD_PHASE_FULL:
            return run_cmd
        if self.build_phase == BUILD_PHASE_BUILD:
            # Only where the marker cleared this key; every fallback runs the whole
            # compile.
            return run_cmd + ["--no-verilate"] if self._skip_verilate else run_cmd
        if not self._splittable(run_cmd):
            return run_cmd
        rewritten = []
        for arg in run_cmd:
            if arg == "--binary":
                rewritten += ["--exe", "--main", "--timing"]
            elif arg == "--build":
                # A line spelling the parts out (`--cc --exe --main --build`) needs only
                # this half of the rewrite.
                continue
            else:
                rewritten.append(arg)
        return rewritten

    def _verilator_supports_no_verilate(self):
        """Will this Verilator run the make step alone?

        Probed with ``--help`` rather than version-gated, once per executable per
        process. Any failure reads as "no", which falls back to a full compile.
        """
        exe = self.rtl_builder_cfg.get_exe()
        cached = _NO_VERILATE_SUPPORT.get(exe)
        if cached is not None:
            return cached
        try:
            probe = subprocess.run(
                [exe, "--help"], capture_output=True, text=True, timeout=60
            )
            supported = "--no-verilate" in f"{probe.stdout}{probe.stderr}"
        except (OSError, subprocess.SubprocessError):
            supported = False
        _NO_VERILATE_SUPPORT[exe] = supported
        return supported

    @staticmethod
    def _verilate_marker_path(stamp_dir):
        """Where the verilate half records what it did, beside the stamp."""
        return Path(stamp_dir) / VERILATE_MARKER_NAME

    def _write_verilate_marker(
        self, stamp_dir, *, fingerprint, status, transcript, duration_sec
    ):
        """Record this key's verilation for the build half. Never raises.

        Best-effort: a missing marker makes the build half do the whole compile.
        ``duration_sec`` lets the build half report the whole compile's cost.
        """
        path = self._verilate_marker_path(stamp_dir)
        try:
            self._replace_text(
                path,
                json.dumps(
                    {
                        "fingerprint_sha": _fingerprint_sha(fingerprint),
                        "status": status,
                        "transcript": transcript,
                        "duration_sec": duration_sec,
                        "timestamp": time.time(),
                    },
                    sort_keys=True,
                ),
            )
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "compile.verilate_marker_write_failed",
                test=self.test_name,
                marker=str(path),
                error=str(exc),
            )

    def _read_verilate_marker(self, stamp_dir):
        """The marker in ``stamp_dir`` as a dict, or ``None``. Never raises."""
        try:
            stored = json.loads(self._verilate_marker_path(stamp_dir).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return stored if isinstance(stored, dict) else None

    @staticmethod
    def _verilate_marker_is_ok(marker, fingerprint):
        """Does ``marker`` vouch for a successful verilation of these inputs?"""
        if not isinstance(marker, dict) or marker.get("status") != "ok":
            return False
        recorded = marker.get("fingerprint_sha")
        own = _fingerprint_sha(fingerprint)
        return recorded is not None and own is not None and recorded == own

    def _settle_verilate_phase(self, stamp_dir, fingerprint, *, forced):
        """Has this key already been verilated? ``0`` = done, ``None`` = do it.

        The marker is to this phase what the stamp is to a whole compile: the second
        config on one compile key must not pay a second front end.
        """
        if forced:
            return None
        if not self._verilate_marker_is_ok(
            self._read_verilate_marker(stamp_dir), fingerprint
        ):
            return None
        log_event(
            logger,
            logging.INFO,
            "compile.verilate_reused",
            test=self.test_name,
            **_build_dir_fields(stamp_dir, shared=self._shared_build_dir is not None),
        )
        self._record_compile(duration_sec=0.0, reused=True)
        return 0

    def _settle_split_phase(self, stamp_dir, fingerprint):
        """What the build half of a split compile does with this key.

        Returns a compile status to return outright, or ``None`` to go on and
        compile, with :attr:`_skip_verilate` saying whether the front end may be
        skipped.
        - Marker ``ok`` for these inputs and a Verilator with ``--no-verilate``: run
          the make alone (the fast path).
        - Marker ``failed`` for these inputs: fail here carrying its transcript,
          since re-running would fail again and overwrite the errors.
        - No marker, or one from other inputs: verilate and build here.
        - A Verilator without ``--no-verilate``: the same full compile, reported
          differently.
        """
        marker = self._read_verilate_marker(stamp_dir)
        if marker is None:
            self._report_build_phase_fallback("marker-missing")
            return None
        recorded = marker.get("fingerprint_sha")
        own = _fingerprint_sha(fingerprint)
        if recorded is None or own is None or recorded != own:
            self._report_build_phase_fallback("marker-stale")
            return None
        if marker.get("status") != "ok":
            return self._decline_failed_verilation(marker)
        if not self._verilator_supports_no_verilate():
            self._report_build_phase_fallback("no-verilate-unsupported")
            return None
        self._skip_verilate = True
        self._verilate_sec = marker.get("duration_sec")
        return None

    def _report_build_phase_fallback(self, reason):
        """Warn that this key's build job had to verilate for itself.

        A WARNING because the reservation is wrong: the suite paid for a verilate job
        and a build job, and one did the whole compile under the other's resources.
        """
        log_event(
            logger,
            logging.WARNING,
            "compile.build_phase_fallback",
            test=self.test_name,
            reason=reason,
        )

    def _decline_failed_verilation(self, marker):
        """The verilate job failed this key; report it without re-running it.

        Inputs are unchanged, so a second attempt fails identically and only
        overwrites the first one's transcript.
        """
        transcript = (
            marker.get("transcript") or self._get_build_compile_transcript_path()
        )
        log_event(
            logger,
            logging.ERROR,
            "compile.verilate_failed",
            test=self.test_name,
            transcript=transcript,
        )
        self.compile_fail_desc = (
            "the verilate job failed to verilate this test; not re-running "
            f"it under the build reservation (see {transcript})"
        )
        self.last_compile_failure = {"returncode": 1, "transcript": transcript}
        return 1

    def _prepare_build_dir_toolchain(self, plan, *, forced):
        """Keep a Verilator build dir's make from outliving its toolchain.

        Runs only where the front end is about to. Scrubs objects and dependency
        files (:func:`_scrub_stale_build_outputs`) when ``--rebuild`` forced this
        compile, or the directory was last compiled by another Verilator or by one
        that recorded nothing. An ordinary source edit keeps its objects. Then
        records this toolchain before the compile, since whatever the builder leaves
        behind, even from a failed or killed compile, was made by it.
        """
        if not plan.is_verilator or self._skip_verilate:
            return
        # `--Mdir` is handed to a builder running in the compile work dir.
        mdir = Path(plan.compile_work_dir) / plan.build_dir
        marker = mdir / BUILD_TOOLCHAIN_MARKER_NAME
        toolchain = (plan.fingerprint or {}).get("toolchain") or (
            self._fingerprint_toolchain(self.rtl_builder_cfg.get_exe())
        )
        current = _toolchain_marker_identity(toolchain)
        try:
            recorded = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            recorded = None
        if not isinstance(recorded, dict):
            recorded = None
        if forced:
            reason = "rebuild"
        elif recorded == current:
            reason = None
        elif recorded is None:
            reason = "toolchain-unrecorded"
        else:
            reason = "toolchain-changed"
        if reason is not None:
            removed = _scrub_stale_build_outputs(mdir)
            if removed:
                # Console: the C++ build that follows is a full one and this says why.
                log_console_event(
                    logger,
                    logging.INFO,
                    "compile.build_dir_scrubbed",
                    test=self.test_name,
                    build_path=str(mdir),
                    removed=removed,
                    reason=reason,
                    was=_describe_marker_toolchain(recorded),
                    now=_describe_marker_toolchain(current),
                )
        if recorded != current:
            try:
                mdir.mkdir(parents=True, exist_ok=True)
                self._replace_text(marker, json.dumps(current, sort_keys=True))
            except OSError:
                # Best-effort: without it the next compile scrubs again, which costs a
                # full C++ build and is never wrong.
                pass

    def _rebuild_forced(self, build_dir, *, shared=True):
        """Does ``--rebuild`` override the stamp on ``build_dir`` right now?

        True at most once per directory per process, and only when the run asked for
        it. Logs ``compile.rebuild_forced`` with the same directory fields as
        ``compile.build_reused``.
        """
        if not self.rebuild:
            return False
        if not _claim_rebuild(str(build_dir)):
            return False
        # Console, like the reuse line: together they answer "what produced the binary
        # this run simulated?". Once per build dir per process.
        log_console_event(
            logger,
            logging.INFO,
            "compile.rebuild_forced",
            test=self.test_name,
            **_build_dir_fields(build_dir, shared=shared),
        )
        return True

    def _read_build_stamp(self, stamp_dir):
        """The stamp in ``stamp_dir`` as a dict, or ``None``. Never raises."""
        try:
            stored = json.loads((Path(stamp_dir) / SHARED_BUILD_STAMP_NAME).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return stored if isinstance(stored, dict) else None

    def _record_build_stamp(self, stamp_dir):
        """Record which binary this run's stamp vouched for.

        ``build_dir`` is the resolved stamp directory (for a shared build, the
        ``obj_dir_<key>`` directory), ``fingerprint_sha`` the digest of the stamp's
        input half, and ``simv`` the entry it vouched for. The head groups runs by
        ``build_dir`` at collect and flags a run naming a different binary or digest.
        """
        stored = self._read_build_stamp(stamp_dir)
        if stored is None:
            self.last_build_stamp = None
            return
        inputs = {key: value for key, value in stored.items() if key not in _STAMP_META}
        self.last_build_stamp = {
            "build_dir": os.path.realpath(str(stamp_dir)),
            "fingerprint_sha": _fingerprint_sha(inputs),
            "simv": stored.get("simv"),
        }

    def refresh_build_stamp(self):
        """Re-read the stamp ``last_build_stamp`` was taken from.

        A sibling's adoption rewrites the shared stamp after the leader recorded its
        digest, so a record taken when the group is done must name the stamp the
        gated jobs will validate. Never raises.
        """
        stamp = self.last_build_stamp
        if stamp is None or self._read_build_stamp(stamp["build_dir"]) is None:
            return
        self._record_build_stamp(stamp["build_dir"])

    def _record_launched_simv(self, simv_path):
        """Stamp the executable this run launches, not the one it validated.

        Another process may rebuild the shared directory between the check and the
        launch, and the head's binary audit needs the binary actually run.
        """
        if self.last_build_stamp is not None:
            # Spelled as the stamp spells it, so the head's audit compares two stats,
            # never two prefixes.
            self.last_build_stamp["simv"] = self._stamp_simv_entry(simv_path)

    def adopt_group_build(self):
        """Take the build a same-key sibling just made, or say why not.

        A group's members share a compile key, so the only difference between the
        leader's stamp and this member's fingerprint is a file that moved during the
        job, typically this member's own ``preproc`` output under a listed
        ``+incdir+``. The question is therefore narrower than the stamp's: did any
        input the leader's build consumed (the stamp's ``deps``) change? Listings
        still count when a new file can change what the compile resolves
        (:func:`_first_resolution_change`).

        Returns ``("adopted", None)``, ``("drift", <path>)`` or ``(None, <reason>)``.
        The last means not decidable here (no shared build or stamp, a differing
        compile line, a resolution change, or a builder with no dependency file such
        as VCS or Icarus); the caller compiles, and a valid stamp still
        short-circuits that. The caller logs the second element.

        Adoption rewrites the stamp's ``sources`` to this member's listing, because
        gated sim jobs validate against it after every ``preproc`` has run.
        Validation and rewrite happen under the build directory lock.
        """
        plan = self._compile_plan()
        fingerprint = plan.fingerprint
        if plan.shared_dir is None:
            return None, "no shared build directory"
        if not isinstance(fingerprint, dict):
            return None, "no compile fingerprint"
        with build_dir_lock(plan.shared_dir, test=self.test_name):
            return self._adopt_group_build_locked(plan, fingerprint)

    def _adopt_group_build_locked(self, plan, fingerprint):
        stored = self._read_build_stamp(plan.shared_dir)
        if stored is None:
            return None, "no stamp"
        deps = stored.get("deps")
        if not isinstance(deps, list):
            return None, "no dependency list (builder reports none)"
        stored_format = stored.get("deps_format")
        if stored_format != _DEPS_FORMAT:
            return None, f"stamp dependency format {stored_format} != {_DEPS_FORMAT}"
        if (stored.get("root") is not None) != (self.shared_build_root is not None):
            # Same fail-closed reading as `_build_stamp_is_valid`; the exact comparison
            # below skips `root`.
            return None, "stamp written in the other shared-build mode"
        simv_path = self._get_simv_path()
        if not Path(simv_path).is_file() or stored.get(
            "simv"
        ) != self._stamp_simv_entry(simv_path):
            return None, "simv changed"
        # Everything but the tracked inputs, compared exactly. The key fixes the command
        # and toolchain identity, so this catches only a toolchain replaced under a
        # running job.
        skipped = _STAMP_META | {"sources"}
        if {key: value for key, value in stored.items() if key not in skipped} != {
            key: value for key, value in fingerprint.items() if key != "sources"
        }:
            return None, "stamp inputs differ"
        for entry in deps:
            if not isinstance(entry, list) or len(entry) != 4:
                return None, "unreadable dependency entry"
            if not isinstance(entry[0], str):
                # `os.stat` takes a file descriptor for an int.
                return None, "unreadable dependency entry"
            if not _entry_matches(entry, self._stamp_tracked_entry(entry[0])):
                return "drift", self._note_group_input_drift(entry[0])
        # Listed now, under the lock: the plan's listing predates the wait, and a file
        # added meanwhile must be seen and reach the refreshed stamp.
        sources = self._fingerprint_filelist_sources(plan.filelist_path)
        appeared = _first_resolution_change(stored.get("sources"), sources, deps)
        if appeared is not None:
            log_event(
                logger,
                logging.DEBUG,
                "compile.group_resolution_changed",
                test=self.test_name,
                appeared=appeared,
            )
            return None, f"resolution changed: {appeared}"
        if not self._refresh_stamp_sources(plan.shared_dir, sources):
            return None, "stamp refresh failed"
        # Consumed like a compile: this instance has had its one build.
        self._compile_plan_cache = None
        self._report_build_reused(plan, stamp_dir=plan.shared_dir)
        self._record_compile(duration_sec=0.0, reused=True)
        return "adopted", None

    def _refresh_stamp_sources(self, stamp_dir, sources):
        """Rewrite the stamp in ``stamp_dir`` with ``sources`` as its listing.

        Everything else is kept. Call with the build directory lock held. Written as
        one atomic replacement.

        Returns whether the stamp now carries ``sources``. ``False`` means the caller
        must not report an adoption, since gated sim jobs would fail rather than
        recompile.
        """
        stored = self._read_build_stamp(stamp_dir)
        if stored is None:
            return False
        if stored.get("sources") == sources:
            return True
        stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
        try:
            self._replace_text(
                stamp_path, json.dumps({**stored, "sources": sources}, sort_keys=True)
            )
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "compile.build_stamp_refresh_failed",
                test=self.test_name,
                stamp=str(stamp_path),
                error=str(exc),
            )
            return False
        log_event(
            logger,
            logging.DEBUG,
            "compile.build_stamp_sources_refreshed",
            test=self.test_name,
            stamp=str(stamp_path),
        )
        return True

    def _note_group_input_drift(self, dependency):
        """Record the drift verdict as a compile failure; return ``dependency``.

        The ``returncode`` stops the gated sim job from recompiling into the shared
        directory. There is no transcript because no builder ran; the one line
        travels in the envelope's ``error_tail``.
        """
        line = (
            f"same compile key, different compiled input: {dependency}; give "
            "this test its own compile key, or fix the preproc that rewrites "
            "that input per test"
        )
        self.compile_fail_desc = line
        self.last_compile_failure = {"returncode": 1, "error_tail": [line]}
        return dependency

    def _report_build_reused(self, plan, *, stamp_dir, shared=True):
        """Report on the console and in the test's ``compile.log`` that this compile was
        skipped.

        Both records name the directory and the age of its stamp, so a run that
        reuses a build made before an edit says so where the reader looks.
        """
        fingerprint = plan.fingerprint
        toolchain = (
            fingerprint["toolchain"]["version"] or fingerprint["toolchain"]["exe"]
        )
        stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
        try:
            stamp_mtime = stamp_path.stat().st_mtime
        except OSError:
            # The stamp validated a moment ago, so this is a vanishing race; report the
            # reuse without an age.
            stamp_mtime = None
        age_sec = (
            None if stamp_mtime is None else max(0, round(time.time() - stamp_mtime))
        )
        fields = {
            "test": self.test_name,
            **_build_dir_fields(stamp_dir, shared=shared),
            "stamp_age_sec": age_sec,
            "toolchain": toolchain,
        }
        # Console as well as the log file, since the console handler sits at WARNING and
        # a dispatched run's reuse would otherwise be invisible.
        # Once per build dir per process on the console; every reuse lands in the file
        # log.
        if _first_reuse_announcement(stamp_dir):
            log_console_event(logger, logging.INFO, "compile.build_reused", **fields)
        else:
            log_event(logger, logging.INFO, "compile.build_reused", **fields)
        self._write_reuse_transcript(
            plan, stamp_dir=stamp_dir, stamp_mtime=stamp_mtime, toolchain=toolchain
        )
        self._record_build_stamp(stamp_dir)

    def _write_reuse_transcript(self, plan, *, stamp_dir, stamp_mtime, toolchain):
        """Leave a ``compile.log`` for a compile that did not run.

        It records the build's age, toolchain and the command a rebuild would have
        run. Best-effort.

        A transcript a compile left at this path is kept below the breadcrumb (it may
        be the only file-level record of a VCS ``-licqueue`` wait). Exactly one
        transcript is carried, so the file does not grow. Written via temp file and
        :func:`os.replace`, since fan-out elements share the path.
        """
        when = (
            "unknown"
            if stamp_mtime is None
            else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp_mtime))
        )
        try:
            run_str = " ".join(self._compile_argv(plan, quiet=True))
        except Exception:  # noqa: BLE001 - a breadcrumb never fails a build
            run_str = "(unavailable)"
        text = (
            f"{_REUSE_TRANSCRIPT_MARKER}{stamp_dir}\n"
            f"Stamp written: {when}\n"
            f"Toolchain: {toolchain}\n"
            "Nothing was compiled for this run. The command a rebuild "
            "would have run:\n\n"
            f"Command: {run_str}\n\n"
            "Use --rebuild to compile it again, or delete the directory "
            "above.\n"
        )
        path = Path(self._get_compile_transcript_path())
        previous = self._previous_compile_transcript(path)
        if previous:
            text += f"{_CARRIED_TRANSCRIPT_HEADER}{previous}"
        try:
            self._replace_text(path, text)
        except OSError as e:
            log_event(
                logger,
                logging.DEBUG,
                "compile.reuse_transcript_unwritable",
                test=self.test_name,
                error=str(e),
            )

    @staticmethod
    def _previous_compile_transcript(path):
        """The compile output already at ``path``, or ``""``.

        Over a breadcrumb, returns the transcript it carried, so N reuses preserve
        one transcript. Decodes with ``errors="replace"`` and catches ``ValueError``
        as well as ``OSError``; it must not raise.
        """
        try:
            existing = Path(path).read_text(errors="replace")
        except (OSError, ValueError):
            return ""
        if existing.startswith(_REUSE_TRANSCRIPT_MARKER):
            _, separator, carried = existing.partition(_CARRIED_TRANSCRIPT_HEADER)
            return carried if separator else ""
        return existing

    def _replace_text(self, path, text):
        """Write ``text`` to ``path`` as one atomic replacement."""
        path = Path(path)
        tmp = path.with_name(atomic_tmp_name(path.name))
        try:
            tmp.write_text(text)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def compile(self):
        rtl_builder_cfg = self.rtl_builder_cfg
        # One compile, one verdict: do not inherit the previous compile's failure
        # record, desc or transcript name.
        self.last_compile_failure = None
        self.compile_fail_desc = None
        self.stamp_write_failed = False
        self._compile_transcript_override = None
        # Remove this run's stale retry transcript up front, so it exists only when this
        # compile's own gated retry writes it. Run-scoped, never a sibling's.
        # Best-effort.
        try:
            Path(self._get_retry_transcript_path()).unlink(missing_ok=True)
        except OSError:
            pass
        log_event(
            logger,
            logging.DEBUG,
            "compile.config",
            test=self.test_name,
            config=pprint.pformat(rtl_builder_cfg),
        )
        plan = self._compile_plan()
        # One plan serves one compile; consume the cache so a second compile() re-stats
        # its inputs.
        self._compile_plan_cache = None

        if plan.shared_dir is None:
            # Unshared builds stay unlocked: each per-test build directory has one
            # writer.
            return self._compile_with_plan(plan)
        # The reuse fast path takes no lock. Gated sim elements all call compile()
        # against one valid shared build, and serialising their stamp validations on a
        # cross-node flock would put them on the critical path and hold reusers behind
        # any compile (a VCS licence queue, say).
        # `--rebuild` is decided inside the lock: `_rebuild_forced` claims the rebuild
        # next to the compile it forces.
        if not self.rebuild and self._reuse_shared_build(plan, quiet=True):
            return 0
        # Before the lock, because the lock file lives inside the directory it guards.
        plan.shared_dir.mkdir(parents=True, exist_ok=True)
        # Cross-process single writer: several `rb` processes against a cold shared tree
        # would otherwise compile into it at once. The stamp check is inside the lock so
        # a waiter reuses the build it waited for. Lock ordering is in build_dir_lock.
        with build_dir_lock(plan.shared_dir, test=self.test_name):
            return self._compile_with_plan(plan)

    def _reuse_shared_build(self, plan, *, quiet=False):
        """Reuse the stamped build in ``plan.shared_dir`` if it validates.

        Asked twice: unlocked in :meth:`compile` (the fast path) and again inside the
        build lock. ``quiet`` is for the first ask; the in-lock repeat owns the
        diagnostics.
        """
        if not self._shared_build_is_valid(
            plan.shared_dir, plan.fingerprint, test_name=self.test_name, quiet=quiet
        ):
            return False
        self._report_build_reused(plan, stamp_dir=plan.shared_dir)
        # 0.0, not the stamp-check time: a reuse cost no build.
        self._record_compile(duration_sec=0.0, reused=True)
        return True

    def _compile_with_plan(self, plan):
        """Check the stamp, compile if it does not validate, stamp the result.

        Split from :meth:`compile` so the shared-build case can hold
        :func:`build_dir_lock` across the whole sequence. The stamp is validated
        against ``plan.fingerprint``, computed before any wait on the lock, so a
        source edited while queued is caught on the next run.
        """
        rtl_builder_cfg = self.rtl_builder_cfg
        compile_work_dir = plan.compile_work_dir
        build_dir = plan.build_dir
        fingerprint = plan.fingerprint
        # The stale stamp is removed only once a builder is certain to run. A gated job
        # that declines returns without touching it, so one job's drift does not fail
        # every sibling with "no stamp or no simv".
        stale_stamp = None
        # Did `--rebuild` claim this directory? A split phase's reuse check must respect
        # it.
        forced = False

        if self.share_build:
            if plan.unsupported_reason is None:
                # Claimed before the check so `--rebuild` decides, and once per
                # directory so the next test with this key reuses the build.
                forced = self._rebuild_forced(plan.shared_dir)
                if not forced and self._reuse_shared_build(plan):
                    return 0
                # The directory was created by compile(), which needed it for the build
                # lock.
                # A crashed or killed compile must not leave a stamp that validates a
                # broken simv, so it is removed below, once the gated verdict has had
                # its say.
                stale_stamp = plan.shared_dir / SHARED_BUILD_STAMP_NAME
                # A shared build owns the output location, so a relative builder-simv:
                # is discarded (an absolute one declines sharing). Log which value went
                # unused.
                configured_simv = self.rtl_builder_cfg.get_simv()
                if (
                    self._get_simulator_family() not in ("verilator", "icarus")
                    and configured_simv != "simv"
                ):
                    log_event(
                        logger,
                        logging.DEBUG,
                        "compile.share_build_simv_overridden",
                        test=self.test_name,
                        configured=configured_simv,
                        used=self._get_simv_path(),
                    )
            else:
                forced = self._rebuild_forced(compile_work_dir, shared=False)
                if not forced and self._build_stamp_is_valid(
                    compile_work_dir,
                    self._get_simv_path(),
                    fingerprint,
                    test_name=self.test_name,
                ):
                    self._report_build_reused(
                        plan, stamp_dir=compile_work_dir, shared=False
                    )
                    self._record_compile(duration_sec=0.0, reused=True)
                    return 0
                stale_stamp = Path(compile_work_dir) / SHARED_BUILD_STAMP_NAME
        elif plan.is_verilator:
            # No stamp to distrust without --share-build; every run compiles.
            # `--rebuild` still owes a non-incremental compile, which only a Verilator
            # build dir can distinguish (see _prepare_build_dir_toolchain).
            forced = self._rebuild_forced(compile_work_dir, shared=False)

        # Where this key's stamp and verilate marker live: the shared build dir, else
        # beside the test's compile outputs.
        stamp_dir = self._shared_build_dir or compile_work_dir
        base_argv = self._compile_argv_base(plan)
        # Is this invocation the verilation alone? Only where a build step exists to
        # hold back; otherwise the line runs whole and the build half reuses it.
        verilate_only = self.build_phase == BUILD_PHASE_VERILATE and self._splittable(
            base_argv
        )
        if self.build_phase == BUILD_PHASE_BUILD:
            settled = self._settle_split_phase(stamp_dir, fingerprint)
            if settled is not None:
                return settled
        elif verilate_only:
            settled = self._settle_verilate_phase(stamp_dir, fingerprint, forced=forced)
            if settled is not None:
                return settled

        run_cmd = self._apply_build_phase(base_argv)
        run_str = " ".join(run_cmd)
        if self.expect_prebuilt:
            # This job was ordered after a build job so it would not compile. Reaching
            # here means that build's stamp did not validate; only the build envelope
            # says whether compiling now is a recovery or a catastrophe.
            verdict, build_record = self._gated_build_verdict(fingerprint)
            if verdict == "built":
                return self._decline_gated_recompile(
                    build_record, fingerprint, build_dir
                )
            if verdict == "failed":
                build_failure = build_record
                # The build job's compile for this test exited non-zero. Same inputs
                # fail again, now under the sim reservation, where a memory kill would
                # overwrite the build's `compile.log`. Fail immediately with the build's
                # verdict and leave that transcript alone.
                returncode = build_failure.get("returncode")
                build_transcript = self._get_build_compile_transcript_path()
                log_event(
                    logger,
                    logging.ERROR,
                    "compile.build_job_failed",
                    test=self.test_name,
                    run_id=self.run_id,
                    returncode=returncode,
                    transcript=build_transcript,
                    build_result=str(self.build_result_json),
                )
                self.compile_fail_desc = build_compile_fail_desc(
                    returncode=returncode,
                    error_tail=build_failure.get("error_tail"),
                    logs=build_transcript,
                )
                self.last_compile_failure = {
                    "returncode": returncode,
                    "transcript": build_transcript,
                }
                # A non-zero status is the contract with _compile_outcome, so use the
                # build's. _gated_build_verdict guarantees a returncode; the guard keeps
                # a malformed record (0, or a non-int) from becoming a success.
                return returncode if isinstance(returncode, int) and returncode else 1
            # The stamp is merely absent or stale. The retry is right, but the
            # dependency only orders the elements, so every sibling is about to compile
            # into the same directory. This warning says so.
            log_event(
                logger,
                logging.WARNING,
                "compile.prebuilt_stamp_invalid",
                test=self.test_name,
                run_id=self.run_id,
                build_dir=build_dir,
                # What drifted, since the check's diagnostics are DEBUG and a dispatched
                # job logs at INFO.
                reason=self.stamp_mismatch_reason,
            )
            # Write beside the build's transcript, never over it: this retry is the sim
            # job's story under the sim reservation. It goes in the run's own directory,
            # which must exist first.
            self._ensure_artifact_dir(run_id=self.run_id)
            self._compile_transcript_override = self._get_retry_transcript_path()
        # Now that every path returning without a compile has returned: a builder is
        # about to write this directory, so the stamp describing what was there stops
        # being true.
        if stale_stamp is not None:
            stale_stamp.unlink(missing_ok=True)
        self._prepare_build_dir_toolchain(plan, forced=forced)
        log_event(
            logger,
            logging.INFO,
            "compile.start",
            test=self.test_name,
            command=run_str,
            builder=rtl_builder_cfg.get_name(),
        )
        s_time = time.time()
        extra_compile_env = self._get_extra_compile_env()
        compile_env = {**os.environ, **extra_compile_env} if extra_compile_env else None
        with task_status(f"Compiling {self.test_name}", spinner="dots12"):
            try:
                result = run_managed_process(
                    run_cmd,
                    capture_output=True,
                    text=True,
                    cwd=compile_work_dir,
                    env=compile_env,
                )
            except FileNotFoundError:
                log_event(
                    logger,
                    logging.ERROR,
                    "compile.builder_missing",
                    test=self.test_name,
                    executable=run_cmd[0],
                )
                raise FatalRtlBuddyError(f"Builder not found. Run exe: {run_cmd[0]}")

        e_time = time.time()
        # Recorded before the pass/fail branch: a slow failed compile is the number a
        # build-job reservation is sized against.
        self._record_compile(
            duration_sec=round(e_time - s_time, 2),
            reused=False,
            # Set only where the build half consumed a marker.
            verilate_sec=self._verilate_sec,
        )
        license_queued = self._compile_queued_for_license(result)
        # Written unconditionally (see _write_compile_transcript), so a compile that ran
        # always leaves a file, whichever of `compile.log` or `compile.retry.log`
        # applies. Consumers read the path off the events below, not a name of their
        # own.
        transcript_path = self._write_compile_transcript(run_str, result)
        if verilate_only:
            # What the build half reads instead of a stamp. No stamp is written, since
            # nothing runnable exists yet and a gated sim would reuse a directory with
            # no executable.
            self._write_verilate_marker(
                stamp_dir,
                fingerprint=fingerprint,
                status="ok" if result.returncode == 0 else "failed",
                transcript=transcript_path,
                duration_sec=round(e_time - s_time, 2),
            )
        if result.returncode != 0:
            log_event(
                logger,
                logging.ERROR,
                "compile.failed",
                test=self.test_name,
                returncode=result.returncode,
                duration_sec=round(e_time - s_time, 2),
                transcript=transcript_path,
                license_queued=license_queued,
            )
            # What a dispatched build job records in its envelope for this config: the
            # status and the transcript that says why.
            self.last_compile_failure = {
                "returncode": result.returncode,
                "transcript": transcript_path,
            }
            failed_sha = _fingerprint_sha(fingerprint)
            if failed_sha is not None:
                # Which compile failed: a gated sim job honours the no-retry verdict
                # only when its own fingerprint sha matches.
                self.last_compile_failure["fingerprint_sha"] = failed_sha
        else:
            log_event(
                logger,
                logging.INFO,
                "compile.completed",
                test=self.test_name,
                duration_sec=round(e_time - s_time, 2),
            )
            if license_queued:
                # Keep the evidence: on a dispatched build this is the only record that
                # the wall-clock went to the license server.
                log_event(
                    logger,
                    logging.WARNING,
                    "compile.license_queued",
                    test=self.test_name,
                    duration_sec=round(e_time - s_time, 2),
                    transcript=transcript_path,
                )
            if result.stdout:
                logger.debug("compile stdout\n%s", result.stdout)
            if self._get_simulator_family() == "icarus":
                self._write_icarus_simv_wrapper()
            if fingerprint is not None and not verilate_only:
                # A shared build stamps its own directory; an unshared build's stamp
                # goes beside the test's compile outputs, since `build_dir` is a bare
                # relative name.
                # Recorded from the finished build, because the builder knows which
                # headers it opened. Read from the build output dir, which is the stamp
                # dir only in the shared case.
                deps = self._collect_build_deps(build_dir, compile_work_dir)
                stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
                try:
                    # Atomic: the reuse fast path reads the stamp with no lock held, and
                    # a truncated read would recompile a good build.
                    self._replace_text(
                        stamp_path,
                        json.dumps(
                            # The executable is stamped too, so a reuse check can tell
                            # "these inputs" from "this binary".
                            {
                                **fingerprint,
                                "deps": deps,
                                "deps_format": _DEPS_FORMAT,
                                "simv": self._stamp_simv_entry(self._get_simv_path()),
                                # In cache mode, the root the relative spellings are
                                # anchored to. Never compared (see `_STAMP_META`); it
                                # records which checkout last wrote the stamp.
                                **(
                                    {"root": self._project_root}
                                    if self.shared_build_root is not None
                                    else {}
                                ),
                            },
                            sort_keys=True,
                        ),
                    )
                except OSError as exc:
                    # The compile succeeded and only the stamp write failed. Raising
                    # would report the build as failed and cancel the afterok fan-out
                    # behind a binary that exists. Keep the builder's status, log it,
                    # and let the build job record it so gated jobs decline rather than
                    # recompile.
                    self.stamp_write_failed = True
                    log_event(
                        logger,
                        logging.ERROR,
                        "compile.stamp_write_failed",
                        test=self.test_name,
                        build_dir=str(stamp_dir),
                        stamp=str(stamp_path),
                        error=str(exc),
                    )
                else:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "compile.build_stamp_written",
                        test=self.test_name,
                        stamp=str(stamp_path),
                        # None, not "none": machine mode emits JSON Lines and the
                        # field's type must not vary.
                        tracked_deps=None if deps is None else len(deps),
                    )
                    self._record_build_stamp(stamp_dir)
            if self.build_phase == BUILD_PHASE_BUILD:
                # A consumed marker must not outlive the build it cleared. Also removed
                # after a fallback build.
                with contextlib.suppress(OSError):
                    self._verilate_marker_path(stamp_dir).unlink(missing_ok=True)
        return result.returncode

    def execute(
        self, run_id=None, seed_mode: SeedMode = SeedMode.DEFAULT, replay_run_id=None
    ):
        """Run the simulation executable; return its exit status.

        ``run_id`` selects run-indexed output names. ``seed_mode`` selects the seed:
        - "default": the builder-config seed
        - "new": a fresh random seed
        - "replay": the seed from a previous run's .randseed file
        - "master": the seed resolved before preprocessing

        A fixed per-test seed resolved before preprocessing takes precedence over
        ``seed_mode``.
        """
        run_id = self.run_id if run_id is None else run_id
        replay_run_id = self.replay_run_id if replay_run_id is None else replay_run_id
        artifact_dir = self._ensure_artifact_dir(run_id=run_id)
        log_path = self._get_log_path(run_id=run_id)
        err_path = self._get_err_path(run_id=run_id)
        randseed_path = self._get_randseed_path(run_id=run_id)

        run_cmd = [self._get_simv_path()]

        resolved_seed = self._resolved_runtime_seed
        if resolved_seed is not None:
            seed = resolved_seed
            self.test_cfg.resolved_seed = resolved_seed
            self.test_cfg.seed_source = self._resolved_runtime_seed_source
            self.test_cfg.seed_identity = self._resolved_runtime_seed_identity
            self.test_cfg.sim_rand_seed_plusarg = self._resolved_runtime_seed_plusarg
            ensure_seed_plusarg = getattr(
                self.test_cfg, "ensure_resolved_seed_plusarg", None
            )
            if callable(ensure_seed_plusarg):
                ensure_seed_plusarg()
            log_event(
                logger,
                logging.INFO,
                "sim.seed_selected",
                test=self.test_name,
                run_id=run_id,
                seed=seed,
                source=self._resolved_runtime_seed_source,
                identity=self._resolved_runtime_seed_identity,
            )

        elif seed_mode == SeedMode.MASTER:
            raise FatalRtlBuddyError(
                f"test {self.test_name!r}: master seed was not resolved before preproc"
            )

        elif seed_mode == SeedMode.REPLAY:
            seed_source_run_id = replay_run_id if replay_run_id is not None else run_id
            seed_source_path = self._get_randseed_path(run_id=seed_source_run_id)
            try:
                seed = int(open(seed_source_path).readline().strip())
            except (FileNotFoundError, ValueError):
                err_msg = f"Replay seed missing or invalid at {seed_source_path}"
                log_event(
                    logger,
                    logging.ERROR,
                    "sim.replay_seed_missing",
                    test=self.test_name,
                    seed_path=seed_source_path,
                )
                with open(log_path, "w+") as test_out_fp:
                    test_out_fp.write("FAIL replay seed missing\n")
                    test_out_fp.write(f"ERR: {err_msg}\n")
                with open(err_path, "w+") as test_err_fp:
                    test_err_fp.write(err_msg + "\n")
                # Convenience latest-run links: never fail a test over one.
                with contextlib.suppress(OSError):
                    force_symlink(err_path, self._get_suite_symlink_path("test.err"))
                    force_symlink(log_path, self._get_suite_symlink_path("test.log"))
                return 1

        elif seed_mode == SeedMode.NEW:
            seed = random.randrange(1000000)
            log_event(
                logger,
                logging.INFO,
                "sim.seed_generated",
                test=self.test_name,
                run_id=run_id,
                seed=seed,
            )

        else:
            seed = self.rtl_builder_cfg.get_seed()

        run_cmd += self.rtl_builder_cfg.get_run_time_opts(
            self.rtl_builder_mode, seed=seed
        )

        run_cmd += self._get_plusdefines()

        run_cmd += self._get_plusargs()

        if self._coverage_enabled() and self._get_simulator_family() == "verilator":
            run_cmd += [
                f"+verilator+coverage+file+{self._get_cov_abspath(run_id=run_id)}"
            ]

        self._record_launched_simv(run_cmd[0])
        run_str = " ".join(run_cmd)
        log_event(
            logger,
            logging.INFO,
            "sim.start",
            test=self.test_name,
            run_id=run_id,
            seed=seed,
            command=run_str,
        )

        timeout, is_custom = self.test_cfg.get_timeout()
        if is_custom:
            log_event(
                logger,
                logging.INFO,
                "sim.timeout_override",
                test=self.test_name,
                run_id=run_id,
                timeout_sec=timeout,
            )
        # Added, not substituted, so per-test sim_timeout values keep their meaning. The
        # `is not None` guard keeps an allowance from creating a timeout for a caller
        # that had none.
        extra_timeout = self.root_cfg.resolve_extra_sim_timeout(self.rtl_builder_cfg)
        if extra_timeout and timeout is not None:
            timeout += extra_timeout
            log_event(
                logger,
                logging.INFO,
                "sim.timeout_extended",
                test=self.test_name,
                run_id=run_id,
                timeout_sec=timeout,
                extra_sec=extra_timeout,
                builder=self.rtl_builder_cfg.get_name(),
            )
        artifact_paths = {
            "log": log_path,
            "err": err_path,
            "randseed": randseed_path,
        }
        log_event(
            logger,
            logging.DEBUG,
            "sim.output_paths",
            test=self.test_name,
            run_id=run_id,
            **artifact_paths,
        )
        s_time = time.time()
        t_time = 0

        license_monitor = None
        timeout_pauser = None
        if self._get_simulator_family() == "vcs":
            license_monitor = VcsLicenseQueueMonitor(
                log_path,
                err_path,
                on_enter_queue=lambda: log_event(
                    logger,
                    logging.WARNING,
                    "sim.license_queue",
                    test=self.test_name,
                    run_id=run_id,
                ),
                # WARNING so the pause/resume pair is visible at default console
                # verbosity.
                on_exit_queue=lambda queued_sec: log_event(
                    logger,
                    logging.WARNING,
                    "sim.license_granted",
                    test=self.test_name,
                    run_id=run_id,
                    queued_sec=round(queued_sec, 2),
                ),
            )
            timeout_pauser = license_monitor.is_waiting

        # stderr goes to test.err, stdout to test.log.
        with task_status(
            f"Running simulation {self.test_name}{'' if run_id is None else f' #{run_id:04d}'}",
            spinner="dots12",
        ):
            extra_env = self._get_extra_sim_env(run_id=run_id)
            sim_env = {**os.environ, **extra_env} if extra_env else None
            with open(err_path, "w+") as test_err_fp:
                with open(log_path, "w+") as test_out_fp:
                    result = run_managed_process(
                        run_cmd,
                        stdout=test_out_fp,
                        stderr=test_err_fp,
                        cwd=artifact_dir,
                        env=sim_env,
                        timeout=timeout,
                        timeout_returncode=4444,
                        terminate_signal=signal.SIGQUIT,
                        timeout_pauser=timeout_pauser,
                    )
                    returncode = result.returncode

                    t_time = time.time() - s_time
                    if result.timed_out:
                        timeout_fields = dict(
                            test=self.test_name,
                            run_id=run_id,
                            timeout_sec=timeout,
                            **artifact_paths,
                        )
                        if license_monitor is not None and license_monitor.cap_exceeded:
                            timeout_fields["license_queue_sec"] = round(
                                license_monitor.queue_wait_sec, 2
                            )
                        log_event(
                            logger,
                            logging.ERROR,
                            "sim.timeout",
                            **timeout_fields,
                        )

        with open(randseed_path, "w") as f:
            f.write(str(seed) + "\n")
            self._append_hier_instance_seed(
                f,
                artifact_dir=artifact_dir,
                run_cmd=run_cmd,
                test=self.test_name,
                run_id=run_id,
            )

        # Latest-run links: a passing test must not fail over one (removed suite dir,
        # ENOSPC, read-only or EXDEV mount).
        with contextlib.suppress(OSError):
            force_symlink(err_path, self._get_suite_symlink_path("test.err"))
            force_symlink(log_path, self._get_suite_symlink_path("test.log"))
            force_symlink(randseed_path, self._get_suite_symlink_path("test.randseed"))

        if returncode != 0:
            log_event(
                logger,
                logging.ERROR,
                "sim.failed",
                test=self.test_name,
                run_id=run_id,
                returncode=returncode,
                duration_sec=round(t_time, 2),
                **artifact_paths,
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "sim.completed",
                test=self.test_name,
                run_id=run_id,
                duration_sec=round(t_time, 2),
            )

        return returncode

    def post(self, run_id=None, sim_returncode=None):
        """Post-process the test output into a TestResult.

        ``sim_returncode`` is the simulation's exit status. When given, an unknown
        verdict is graded against it before ``postproc.completed`` is logged.
        ``None`` grades nothing.
        """

        run_id = self.run_id if run_id is None else run_id
        log_path = self._get_log_path(run_id=run_id)
        err_path = self._get_err_path(run_id=run_id)
        assertions_enabled = self._assertions_enabled()

        if self.test_cfg.uvm:
            self.vlog_post = UvmVlogPost(
                name=self.test_name,
                path=log_path,
                max_warns=self.test_cfg.uvm.max_warns,
                max_errors=self.test_cfg.uvm.max_errors,
                err_path=err_path,
                assertions_enabled=assertions_enabled,
            )

        # Default post-processing (VlogPost).
        else:
            self.vlog_post = VlogPost(
                name=self.test_name,
                path=log_path,
                err_path=err_path,
                assertions_enabled=assertions_enabled,
            )
        results = self.vlog_post.get_results()
        # Before the coverage overlay and postproc.completed: that event's result/desc
        # are the authoritative record for JSONL consumers, so the verdict must be
        # final.
        grade_unknown_sim_exit(
            results.results, sim_returncode, test=self.test_name, run_id=run_id
        )
        if self._coverage_enabled():
            cov = VlogCov(
                simulator_name=self._get_simulator_family(),
                use_lcov=self.root_cfg.get_use_lcov(self._get_simulator_family()),
                root_cfg=self.root_cfg,
            )
            cov_results = cov.collect(
                self._get_cov_abspath(run_id=run_id),
                source_roots=[self.suite_work_dir],
            )
            if cov_results is not None:
                results.results["coverage"] = cov_results.to_dict()
        log_event(
            logger,
            logging.INFO,
            "postproc.completed",
            test=self.test_name,
            run_id=run_id,
            result=results.results["result"],
            desc=results.results["desc"],
        )
        return results
