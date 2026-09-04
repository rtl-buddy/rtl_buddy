# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
vlog_sim module handles verilog simulations for rtl-buddy

"""

import contextlib
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import logging
import threading
import types
import uuid
from dataclasses import dataclass, field
from stat import S_ISREG

logger = logging.getLogger(__name__)
from ..hooks import exec_hook_script
from ..seed_mode import SeedMode

from .vlog_filelist import VlogFilelist
from .vlog_post import VlogPost
from .vlog_post import UvmVlogPost
from .vlog_cov import VlogCov
from .artifact_paths import (
    ARTIFACT_DIRNAME,
    BUILD_DIR_PREFIX,
    DISPATCH_OUTPUT_PATTERNS,
    RESULT_JSON_NAME,
    SHARED_BUILDS_DIRNAME,
    shared_build_dir,
    test_artifact_dir,
    test_build_dir_name,
)

import time
import pprint
from pathlib import Path

from ..artifact_lock import build_dir_lock
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

    Create-a-temp-then-``os.replace`` instead of remove-then-create:
    ``os.replace`` over a symlink is a single rename, so it cannot lose a
    race. The check-then-act form (``lexists`` then ``remove`` then
    ``symlink``) races when concurrent writers share a link — under
    ``--dispatch`` every element of a suite's Slurm array runs at once
    against the same suite-level ``test.log``/``test.err``/``test.randseed``,
    and the interleaving killed passing tests with ``FileNotFoundError`` /
    ``FileExistsError`` (#363). The temp name carries the pid and a random
    suffix so no two writers — separate processes (real array elements) or
    threads — ever collide on the intermediate link.
    """
    tmp = f"{link_name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link_name)
    except OSError:
        # Don't leak the temp link into the suite dir if the rename fails.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Sentinel for "argument not given", where None is a meaningful value the
# caller may need to pass explicitly (see VlogSim.pre).
_UNSET = object()

# Stamp written into a shared build dir after a successful compile; records
# the exact compile inputs the simv was built from so reuse can be validated.
# Defined in `artifact_paths` so the artefact-clearing helpers can protect it
# from a co-named run's suffix clear (#469); re-exported here because this is
# where every consumer already looks for it.
from .artifact_paths import SHARED_BUILD_STAMP_NAME as SHARED_BUILD_STAMP_NAME

# First line of the ``compile.log`` a *reuse* leaves (#494). Doubles as the
# marker that tells a breadcrumb from a real compile transcript, so a reuse
# can replace the one and preserve the other.
_REUSE_TRANSCRIPT_MARKER = "Compile skipped: reused the build already in "

# Separates a reuse breadcrumb from the compile transcript it preserves
# below itself, and lets the next reuse carry that transcript forward
# instead of nesting breadcrumb inside breadcrumb.
_CARRIED_TRANSCRIPT_HEADER = (
    "\n=== transcript of the compile that last wrote this file ===\n"
)

# Where a compile's command + captured output is kept, in the test's compile
# work dir. The `.retry.` variant is used by exactly one caller: a
# dispatched sim job that was gated on a build job and found that build's
# stamp invalid. Its recompile runs under the SIM reservation, so its
# failure mode is not the build job's — writing it to `compile.log` would
# replace the build's real compile error with the retry's, which is how a
# one-line lint error became three rounds of "raise compile memory" (#498).
COMPILE_TRANSCRIPT_NAME = "compile.log"
COMPILE_RETRY_TRANSCRIPT_NAME = "compile.retry.log"

# The rest of what a test writes into its artefact directory, named rather
# than spelled inline at the one `_get_*_path` that builds each: the
# shared-build stamp has to recognise rtl_buddy's own outputs to keep them
# out of a directory listing (#478), and a list of names guessed
# separately from the code that writes them is a list that goes stale.
FILELIST_NAME = "run.f"
TEST_LOG_NAME = "test.log"
TEST_ERR_NAME = "test.err"
TEST_RANDSEED_NAME = "test.randseed"
COVERAGE_DAT_NAME = "coverage.dat"
SIMV_NAME = "simv"
ICARUS_SNAPSHOT_NAME = "simv.vvp"

# Simulator families whose compile output rtl_buddy can redirect wholesale
# into a shared build dir, and whose simv still runs from there once other
# tests point at it. Everything else compiles inside each test's own
# artefact dir (correct, just unshared).
SHARE_BUILD_FAMILIES = frozenset({"verilator", "vcs", "icarus"})


@dataclass(frozen=True)
class _TopFlagSpec:
    """How one simulator family spells "elaborate from this module".

    ``emit`` is the single spelling rtl_buddy writes. ``aliases`` is every
    spelling that counts as *the user already pinned a top*, which is a
    strictly larger set: Verilator documents ``--top-module`` and ``--top``
    and accepts each with one or two leading dashes, so a project that
    worked around #508 by putting ``--top spare_top`` in ``compile-time``
    must be recognised — appending our own ``--top-module`` there would
    silently win (Verilator takes the LAST top on the command line), which
    is the exact inverse of the "configured flag wins" contract. Verified
    against Verilator 5.050: all four spellings are accepted, ``--top-module=x``
    is rejected outright, so no ``=``-glued form needs handling.

    ``glued`` lists the prefixes whose value may be attached to the flag
    (``iverilog -stb``); Verilator and VCS always take the module as a
    separate token.
    """

    emit: str
    aliases: tuple[str, ...]
    glued: tuple[str, ...] = ()


# The top-selection flag per simulator family, so a testbench's declared
# `toplevel:` decides the elaboration root instead of whichever source the
# composed filelist happens to name first (#506, #508). A family absent here
# has no such flag rtl_buddy knows of, and its builds keep electing a top the
# way they always did.
TOP_MODULE_FLAGS = {
    "verilator": _TopFlagSpec(
        emit="--top-module",
        aliases=("--top-module", "-top-module", "--top", "-top"),
    ),
    "vcs": _TopFlagSpec(emit="-top", aliases=("-top",)),
    "icarus": _TopFlagSpec(emit="-s", aliases=("-s",), glued=("-s",)),
}


def _find_configured_top(spec, opts):
    """The top the configured opts already pin, or ``None``.

    Returns ``(flag_as_written, module_or_None)`` for the LAST occurrence,
    because that is the one the simulator honours — Verilator's duplicate
    options are last-wins, and scanning from the front would compare
    ``toplevel:`` against a spelling the build overrides anyway.

    The module is ``None`` for a flag with nothing usable after it: a
    trailing bare ``--top``, or one followed by another option. That still
    counts as pinned (rtl_buddy must not append a second top next to a
    malformed one), there is simply no value to compare or to print.
    """
    found = None
    for index, token in enumerate(opts):
        if token in spec.aliases:
            nxt = opts[index + 1] if index + 1 < len(opts) else None
            # A module name never starts with `-`; anything that does is the
            # next option, so the flag is bare.
            value = nxt if (nxt and not nxt.startswith("-")) else None
            found = (token, value)
            continue
        for prefix in spec.glued:
            if token.startswith(prefix) and len(token) > len(prefix):
                found = (prefix, token[len(prefix) :])
                break
    return found


# Conflicts claimed by (family, configured top, declared toplevel) so a suite
# of N tests over one misconfigured builder warns once rather than N times —
# the same "one console line per distinct fact per process" discipline as
# `_claim_rebuild` and `_first_reuse_announcement`. Keyed on the fact and not
# on the test, because the fact is a property of the builder config every one
# of those tests shares.
_TOPLEVEL_CONFLICTS_LOCK = threading.Lock()
_TOPLEVEL_CONFLICTS: set[tuple] = set()


def _claim_toplevel_conflict(key: tuple) -> bool:
    """Is this process's first warning about this conflict? Claims it."""
    with _TOPLEVEL_CONFLICTS_LOCK:
        if key in _TOPLEVEL_CONFLICTS:
            return False
        _TOPLEVEL_CONFLICTS.add(key)
        return True


def _reset_toplevel_conflicts() -> None:
    """Forget every claim. Tests only — one pytest process is many runs."""
    with _TOPLEVEL_CONFLICTS_LOCK:
        _TOPLEVEL_CONFLICTS.clear()


# The argv suffix that makes a simulator print its version cheaply, per
# family, for the toolchain half of the shared-build stamp. A family absent
# here keeps the path + size + mtime half and no version string. VCS is
# deliberately absent: `vcs -ID` checks out a licence, and queueing for one
# before every compile would cost far more than the check is worth — a VCS
# install is versioned by its path, which the resolved executable already
# carries.
_TOOLCHAIN_VERSION_ARGS = {
    "verilator": ("--version",),
    "icarus": ("-V",),
}

# (resolved path, mtime_ns) -> version line. One fork per distinct binary per
# process: a regression compiles many suites through the same toolchain, and
# a dispatched fan-out re-probes once per job.
_TOOLCHAIN_VERSION_CACHE: dict[tuple[str, int], str | None] = {}


def _probe_toolchain_version(exe_path, simulator_family, mtime_ns):
    """First line of the simulator's own version banner, or ``None``.

    Plain ``subprocess.run`` rather than ``run_managed_process``: this is a
    sub-second probe with no output to stream and nothing to clean up on a
    signal, and it is memoised per (path, mtime) so a whole regression pays
    for it once. The one thing that memo cannot see is an upgrade that
    changes neither size nor mtime *while a run is in flight*; the next
    process — the next ``rb``, or any dispatched job — probes afresh.

    Every failure mode degrades to ``None``: a version we could not read
    must never fail a compile, it only costs the stamp the ability to
    notice an in-place upgrade.
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


def _log_stale_stamp_toolchain(stored_inputs, current_inputs, *, test_name=None):
    """Say so when a rebuild is the toolchain's doing, not the RTL's.

    A recompile after a source edit explains itself. A recompile because
    the simulator moved underneath a build that was being reused does not,
    and reading it off a diff of two JSON stamps is not a thing anyone
    should have to do — this is the case that used to be missed entirely.
    Silent on a stamp predating the toolchain entry: that is an rtl_buddy
    upgrade, not a toolchain change, and it happens exactly once.

    Both sides are the *same* dict shape — the caller's comparison operands,
    not one of them and the raw fingerprint — so that this stays right if it
    ever diffs more than ``toolchain``.
    """
    if "toolchain" not in stored_inputs:
        return
    was = stored_inputs.get("toolchain")
    # A caller may hand in no fingerprint at all to assert a stamp is stale;
    # that is not a toolchain change either.
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
    """Can this simulator family reuse one compiled simv across tests (#358)?

    The single source of truth for the capability, because two places must
    agree on it: :meth:`VlogSim.compile` decides whether to build into the
    shared dir, and the dispatch head sizes a sim job's reservation on
    whether that job will also compile (an unsupported family recompiles
    inside every job, under the *sim* reservation — see
    ``config.dispatch.combine_for_in_job_compile``).
    """
    return simulator_family in SHARE_BUILD_FAMILIES


def share_build_unsupported_reason(builder_cfg):
    """Why this builder cannot use a shared build, or ``None`` if it can.

    Two ways out: a family rtl_buddy cannot redirect into the shared dir at
    all, and an absolute ``builder-simv:`` — that pins the executable to one
    exact path the user chose, which a per-compile-key shared dir cannot
    honour without silently ignoring the config.

    Module-level and taking the builder config rather than a live
    :class:`VlogSim`, because the dispatch head must ask the *same* question
    before any VlogSim exists: it sizes a sim job's reservation on whether
    that job will also compile, and gates the fan-out on whether anything
    needs serializing. A head that consulted only the family would plan a
    VCS builder with an absolute ``builder-simv:`` as shareable and give it
    a sim-sized reservation, while the job itself took the unshared path
    (#369).
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

    Verilator and Icarus derive their output from the build dir, so
    ``builder-simv:`` cannot move it; every other family honours an absolute
    one verbatim (see :meth:`VlogSim._get_simv_path`). An absolute pin is
    why such a builder cannot share a build (above): a per-compile-key
    shared dir cannot honour one exact user-chosen path. Note this is a
    *sharing* predicate only — the #495 compile-pool grouping key is the
    resolved output path from ``_get_simv_path()`` itself, because a
    RELATIVE ``builder-simv:`` whose ``..`` escapes the per-test workspace
    collides on one file just as an absolute one does (#496 review), while
    never being a reason to decline sharing.
    """
    if builder_cfg.get_simulator_family() in ("verilator", "icarus"):
        return None
    simv = builder_cfg.get_simv()
    return simv if os.path.isabs(simv) else None


# Matches the option prefixes VlogFilelist emits into run.f (see
# VlogFilelist._extract): `+incdir+`, `+libext+`, `+define+`, `-v `, `-y `,
# `-F `. A `+define+` entry never resolves to a file, so it stamps as a raw
# line — enough for the fingerprint to notice when the defines change. The
# option is captured as well as the path: `+incdir+` and `-y ` are the two
# that name a *directory*, and they are stamped by listing it (#478).
_FILELIST_OPTION_RE = re.compile(r"^(\+(?:incdir|libext|define)\+|-[vyF]\s+)?(.*)$")

_INCDIR_OPTION = "+incdir+"
_LIBRARY_DIR_OPTION = "-y"

# Directory names an `+incdir+` walk must not descend into (#478 review).
#
# rtl_buddy's own artefact trees are the load-bearing half: a `+incdir+.`
# declared in a tests.yaml, or a `+incdir+..` from a design directory that
# contains verif suites, makes the walk reach `artefacts/` — and the files
# under it (run.f, compile.log, the obj_dir, the stamp itself) are written
# AFTER the fingerprint that lists them. Every later process would then see
# a different listing and recompile, which under `--dispatch` is every
# gated simulation job. Derived from the constants the writers use, so
# renaming a managed directory cannot leave this behind.
#
# Dot-directories are the other half: `.git`, `.svn`, `.hg` and friends
# hold no compile input and can be enormous.
#
# `__pycache__` is rtl_buddy's own side effect too, one level removed: a
# `preproc` hook importing a helper module out of the suite directory makes
# CPython write bytecode beside it, during the very phase that computes the
# fingerprint (#537).
_PRUNED_WALK_DIRNAMES = frozenset(
    {ARTIFACT_DIRNAME, SHARED_BUILDS_DIRNAME, "__pycache__"}
)
_PRUNED_WALK_DIR_PREFIXES = (BUILD_DIR_PREFIX,)

# Files that are metadata rather than compile input, as fnmatch patterns.
#
# A *name-based* denylist, not "everything starting with a dot": a dot-file
# can be perfectly ordinary input — `` `include ".config.svh" `` resolves
# and compiles — so skipping every dot name would reopen the gap this
# stamp exists to close. What is listed here is editor and VCS bookkeeping
# no simulator ever reads, and `.DS_Store` in particular, which browsing an
# include directory in Finder writes and which used to force a full
# recompile.
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

# rtl_buddy's OWN outputs, by name (#478 review).
#
# Pruning the `artefacts` directory is not enough on its own, because an
# include root can *be* one: a `preproc` hook is documented to generate
# headers into its `artifact_dir`, and the filelist then carries
# `+incdir+artefacts/<test>` or a subdirectory of it. The walk starts
# inside the managed tree, so no `artefacts` component is ever seen — and
# every one of these files is written AFTER the fingerprint that would list
# it, so the generated header the project actually wanted tracked came with
# run.f, compile.log, test.log, the result envelope and the stamp itself
# attached, and no run ever validated the stamp again.
#
# Generated inputs under `artefacts/` MUST stay tracked, so the tree is
# walked and the outputs are removed by name instead. Every entry is taken
# from the constant the writer uses, not restated here.
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
    RESULT_JSON_NAME,
) + DISPATCH_OUTPUT_PATTERNS
# The head's own `rtl_buddy.log` is deliberately not here: it is excluded
# by PATH in `_directory_listing` (see `_is_suite_log`), because a file of
# that name in any other include directory is an ordinary input.

_NON_INPUT_FILE_PATTERNS = _BOOKKEEPING_FILE_PATTERNS + _MANAGED_OUTPUT_FILE_PATTERNS

# The stamp's own keys, as opposed to the compile fingerprint it wraps: the
# builder's reported dependencies and the executable it produced. Removing
# them leaves exactly the dict `_compile_fingerprint` returned, which is
# what both the stamp comparison and `_fingerprint_sha` work on.
_STAMP_META = frozenset({"deps", "deps_format", "simv"})

# A directory-valued source entry is `[line, None, None, None, listing]`:
# four elements of the ordinary `[path, size, mtime_ns, sha]` shape, all
# empty because a directory has no content of its own, plus the listing of
# the regular files inside it. The extra element is deliberate — a stamp
# written before #478 has four, so it cannot compare equal and fails closed
# into exactly one rebuild (the #494 precedent).
_DIRECTORY_ENTRY_LEN = 5

# How a stamp's `deps` entries name their files. 2 is the declared path the
# build used (`normpath`); the unversioned entries before it were `realpath`s,
# which validate a retargeted symlink's *old* target and so cannot be told
# apart from a current entry by shape. A stamp whose `deps` is a list and
# whose `deps_format` is not this fails closed into one rebuild.
_DEPS_FORMAT = 2

# Verilator writes a make-style dependency file naming every input the
# verilation consumed — sources, headers reached through `+incdir+`/`-y`,
# its own std includes, and the `verilator_bin` binary itself. It is named
# after `--prefix`, which rtl_buddy never sets, so it is found by glob
# rather than construction. Other builders emit nothing comparable (#303).
_VERILATOR_DEPEND_GLOB = "*__ver.d"

# One token of a make dependency line: a run of non-whitespace, where a
# backslash escapes the character after it (`\ ` inside a path).
_DEPEND_TOKEN_RE = re.compile(r"(?:[^\s\\]|\\.)+")


def parse_depend_prerequisites(text: str) -> list[str]:
    """Prerequisite paths from a make-style dependency file.

    Parsed rule by rule rather than "everything after the first colon":
    with ``--MP`` (a ``builder-opts`` a project may set) Verilator appends a
    ``gcc -MP``-style tail of phony rules — one bare ``<prerequisite>:`` per
    line, so ``make`` does not fail on a deleted include. Those are targets,
    and collecting them as prerequisites would stamp a shadow entry per real
    dependency, each with a trailing colon and so resolving to a path that
    never exists.

    Within a rule, everything up to the ``:`` is the target list and is
    dropped — those are generated files, not inputs. Line continuations are
    joined and ``\\ `` escapes are unescaped; order is preserved and
    duplicates are kept for the caller to collapse, since a prerequisite
    listed twice is not an error.
    """
    prerequisites = []
    for line in text.replace("\\\n", " ").splitlines():
        tokens = _DEPEND_TOKEN_RE.findall(line)
        for index, token in enumerate(tokens):
            if token == ":" or token.endswith(":"):
                # A rule with no prerequisites is a phony target: nothing to
                # collect, and the next line starts a new rule either way.
                prerequisites += tokens[index + 1 :]
                break
        # A line with no separator is not a rule rtl_buddy understands (a
        # comment, or a stray target list); saying nothing beats treating
        # every token on it as an input.
    return [re.sub(r"\\(.)", r"\1", token) for token in prerequisites]


def _stat_entry(path: str) -> list:
    """``[path, size, mtime_ns]`` for a tracked input, or nulls if absent.

    A vanished file records as ``[path, None, None]`` rather than being
    dropped, so its later reappearance still invalidates the stamp.

    Still stat-only, and deliberately: its one remaining caller stamps the
    build's *output* (``simv``), which is a freshness check on a binary
    rtl_buddy just wrote, not edit detection on an input. Hashing a
    hundred-megabyte executable on every validation would buy nothing —
    see :func:`_hashed_stat_entry` for the inputs, where content decides.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [path, None, None]
    return [path, stat.st_size, stat.st_mtime_ns]


# One content hash per (path, size, mtime_ns) per process. A suite
# validating N stamps over one source set otherwise re-reads every file N
# times, and the #495 build job validates from worker threads, so the memo
# is guarded rather than thread-local: two threads asking for the same file
# should read it once between them.
_CONTENT_HASH_LOCK = threading.Lock()
_CONTENT_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_CONTENT_HASH_CHUNK = 1 << 20

# Above this, an input keeps the old size+mtime comparison instead of being
# read. The hashing policy is locational, not by kind, so a memory-init
# `.hex`, a vendored blob or a generated database named in run.f or in the
# dependency file qualifies exactly as a `.sv` does — and would then be read
# in full on every stamp validation, on every node. The cap is the same
# trade as excluding the toolchain (brief invariant 4), drawn well above any
# hand-written source so that what #494 is actually about is never affected;
# what it does cost is that an edit to a file this large is only noticed by
# its stats again. `compile.hash_skipped_large` says when that happens.
_CONTENT_HASH_MAX_BYTES = 64 << 20

# Paths this process has already reported as too large to hash. The DEBUG
# line is worth having once per file; once per file per validated stamp is
# noise. Guarded by _CONTENT_HASH_LOCK, like the memo beside it.
_HASH_SKIPPED_LARGE: set[str] = set()


def _log_hash_skipped_large(path: str, size: int) -> None:
    """Name a tracked input the size cap left on the stat-only path.

    The cap is invisible otherwise — the entry looks like any other
    stat-only one — and "why did this file not get content-hashed" is the
    question a second #494 would start from. Once per path per process.
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

    Memoised on ``(path, size, mtime_ns)``. The memo is a cost bound, not a
    correctness claim about stats: it stops one process re-reading a file
    once per validated stamp, which is the cost this whole check has to stay
    under. The value it returns came from a real ``open()``, so it is
    close-to-open-fresh as of when it was taken — and the boundary that
    matters for #494, a *later* run on another node, is a different process
    with an empty memo, where the re-read is guaranteed.
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
        # A file that exists but cannot be read: record None and let the
        # comparison fail closed against a stamp that has a hash.
        return None
    value = digest.hexdigest()[:16]
    with _CONTENT_HASH_LOCK:
        _CONTENT_HASH_CACHE[key] = value
    return value


# Build directories ``--rebuild`` has already forced in THIS process (#494).
# The flag means "do not trust the stamp on disk", not "compile once per
# test": a suite whose tests share one compile key meets the same directory
# N times, and rebuilding it N times would both waste the run and put N
# builders into one directory (#369). The first meeting rebuilds and claims
# the directory; the rest validate the stamp that rebuild just wrote and
# reuse it. Lock-guarded because the #495 build job compiles from worker
# threads.
_REBUILT_DIRS_LOCK = threading.Lock()
_REBUILT_DIRS: set[str] = set()

# Build dirs whose reuse this process has already put on the CONSOLE. The
# file log keeps every `compile.build_reused`; the console line is the
# once-per-build signal — a 50-test local regression reusing one build must
# not print 50 identical lines (#494 review), while under dispatch each job
# is its own process and still prints its one line.
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

    ``realpath``'d, for the reason the compile grouping is: two spellings
    of one directory (a symlinked parent, a ``..`` that escapes the test's
    workspace) are one build, and a textual key would let each spelling
    rebuild it.
    """
    key = os.path.realpath(build_dir)
    with _REBUILT_DIRS_LOCK:
        if key in _REBUILT_DIRS:
            return False
        _REBUILT_DIRS.add(key)
        return True


def _reset_rebuilt_dirs() -> None:
    """Forget every claim. Tests only — one pytest process is many runs."""
    with _REBUILT_DIRS_LOCK:
        _REBUILT_DIRS.clear()


def _build_dir_fields(build_dir, *, shared: bool) -> dict:
    """The directory fields ``compile.build_reused`` and
    ``compile.rebuild_forced`` both carry (#494).

    One schema for the pair: ``build_dir`` is always the basename and
    ``build_path`` always the full path, so a consumer keying on either
    across the two events gets the same kind of thing. Which one the human
    line shows is :func:`logging_utils._build_location`'s decision, and it
    needs ``shared`` to make it — a shared directory is identified by its
    ``obj_dir_<key>`` basename, an unshared one only by its path.
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
        # Different drives on Windows: not under the root by definition.
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

    **Why content and not just stats (#494).** ``size``/``mtime_ns`` answer
    "has this file changed?" correctly on one machine. Across a cluster they
    do not: the edit happens on the submit host and the validation on a
    compute node, and NFS serves that node a *cached* attribute answer for
    up to ``acregmax`` — so a file edited seconds ago still stats as it did
    before the edit and the stamp validates against a stale answer. Reading
    the bytes is what closes it, because NFS close-to-open consistency
    revalidates on ``open()``: the content of an edited file is visible even
    while its cached stats are not. That is the difference between a rebuild
    and a false PASS on a design that was never simulated.

    **Policy: hash the project's own files, never the toolchain's** (brief
    invariant 4). Verilator's dependency file names the toolchain's own std
    includes and, for some installs, ``verilator_bin`` itself; hashing tens
    of megabytes of unchanging install per validation is not a trade worth
    making, and the toolchain fingerprint's version probe already catches an
    install swapped underneath a build. So an entry qualifies only if it is
    under ``project_root`` *and* outside ``toolchain_prefix`` — the install
    tree of the resolved simulator executable, which a vendored
    ``tools/verilator/`` or an in-repo venv puts under the project root.
    Everything else stays stat-only, as is anything over
    ``_CONTENT_HASH_MAX_BYTES``.

    **Containment is decided on the name the build used, not only on where
    that name lands.** Symlinking an IP or RTL tree into the project is a
    common hardware-repo layout, and resolving first would put such a source
    outside the root and leave it stat-only — turning the fix off for
    exactly the files a shared IP mount holds. So the declared path counts
    too, while the *exclusion* still tests the realpath, which is what
    catches a symlink into the toolchain install.

    ``resolved`` says ``path`` is already a ``realpath``, which saves a
    second walk of one ``lstat`` per component on the NFS mount this check
    exists for; a resolved entry has no declared path left to consult.
    Neither the filelist's nor the dependency list's entries are resolved:
    both are stored by the ``normpath`` the build used, so a header reached
    through ``+incdir+`` from a symlinked-in tree is hashed like the tree's
    sources are, and a symlink retargeted between two runs is seen by the
    stat and hash of wherever it points *now*.

    Only regular files are hashed: a directory or a FIFO named among the
    prerequisites would otherwise reach ``open()``, and a FIFO blocks there
    forever rather than failing closed.
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
    """``[path, size, mtime_ns, sha]`` for a tracked *input*.

    ``sha`` is :func:`_content_sha` — a short content hash for the project's
    own files, None for anything the hashing policy excludes (and for an
    existing file that cannot be read, which :func:`_entry_matches` then
    treats as changed). A vanished file records as ``[path, None, None,
    None]`` rather than being dropped, so its later reappearance still
    invalidates the stamp.
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

    See :data:`_PRUNED_WALK_DIRNAMES`: rtl_buddy's own artefact trees,
    whose contents are written *after* the fingerprint that would list
    them, and dot-directories, which hold no compile input.
    """
    return (
        name.startswith(".")
        or name in _PRUNED_WALK_DIRNAMES
        or name.startswith(_PRUNED_WALK_DIR_PREFIXES)
    )


def _is_non_input_file(name: str) -> bool:
    """Is ``name`` bookkeeping or rtl_buddy's own output, not a compile input?

    Matched against :data:`_NON_INPUT_FILE_PATTERNS` by name only, and
    everywhere in a listing rather than only under ``artefacts/``: an
    include root can *be* an artefact directory (a ``preproc`` hook
    generating headers into its ``artifact_dir``), and then no path
    component says so. Every other file is listed, dot-prefixed ones
    included — ``.config.svh`` is a legal include and dropping it would be
    exactly the silent gap #478 is about.
    """
    return any(
        fnmatch.fnmatchcase(name, pattern) for pattern in _NON_INPUT_FILE_PATTERNS
    )


def _is_directory_entry(entry) -> bool:
    """Is ``entry`` a ``+incdir+``/``-y`` entry carrying a directory listing?

    The listing is the last element and is itself a list of ordinary
    tracked-input entries, keyed by the file's path *relative to the
    directory*. The directory's own path is already fixed by ``entry[0]``,
    the ``run.f`` line the compile key hashes, so every test sharing a build
    carries the same one and repeating it per file would only bloat the
    stamp.
    """
    return (
        isinstance(entry, list)
        and len(entry) == _DIRECTORY_ENTRY_LEN
        and isinstance(entry[-1], list)
    )


def _listing_names(entries) -> list | None:
    """The names a directory listing carries, or None if it is malformed.

    The half of a listing that survives ``listing_names_only`` (#536): which
    files exist, not what is in them.
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

    The one comparison both tracked-input lists go through — the filelist
    fingerprint's ``sources`` and the build's ``deps`` — so they cannot
    drift on what "unchanged" means.

    **The hash decides.** When both sides carry a content hash and the
    hashes agree, the entry validates even if ``mtime_ns`` moved: a ``git
    checkout`` that restores byte-identical content, a ``touch``, or a
    rebuilt generated file no longer forces a rebuild. When either side has
    no hash (an entry outside the project root, or an unreadable file), the
    comparison falls back to today's exact ``[path, size, mtime_ns]``
    equality, which is fail-closed in both directions: a stamp that recorded
    a hash for a file we can no longer hash counts as changed.

    **A directory entry compares by its listing.** An entry for a
    ``+incdir+`` or ``-y`` directory (see :func:`_is_directory_entry`)
    carries the files inside it instead of stats of its own, and matches
    only when that whole listing does — so a file added to, removed from, or
    edited inside such a directory is a change for every builder, whether or
    not it emits a dependency file (#478).

    ``listing_names_only`` narrows that to *which files exist*, and the
    caller sets it exactly when the stamp carries the builder's ``deps``
    (#536). A dependency file names every input the build actually opened,
    headers reached through ``+incdir+`` included, so their content is
    already decided there and hashing them a second time out of the listing
    only adds a way to lose: an include directory that is also a working
    directory — a suite dir under ``+incdir+.`` — collects the run's own
    output while the run is still going, and every later fingerprint then
    disagrees with the stamp over a file no compile ever read (#535/#537).
    What the listing still decides is what ``deps`` structurally cannot see:
    a file that *appears* or *vanishes*, which for ``-y`` is tomorrow's
    module resolution (gap 2 of #478). With no dependency file the listing
    is the only record of either, and the full comparison stands.

    Anything that is not an entry of this version's shape — a 3-element
    entry from a stamp written before #494, or a 4-element one where #478
    now records a listing — is "we do not know", and the only honest reading
    of that is one rebuild.
    """
    if not isinstance(stored, list) or len(stored) != len(current):
        return False
    if not stored or not current:
        # Two empty entries are the same length and index into nothing.
        # Unreachable while every `current` comes from _hashed_stat_entry,
        # but this is the general comparator and it fails closed.
        return False
    if stored[0] != current[0]:
        return False
    if len(stored) == _DIRECTORY_ENTRY_LEN:
        # A directory entry's last element is a listing, not a hash, so the
        # generic tail comparison below would compare it by equality and
        # re-introduce exactly the mtime sensitivity #494 removed. Recurse
        # instead: the same content-decides rule, one level down. A
        # five-element entry that is not a listing is a shape this version
        # did not write, and fails closed.
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
        # A size mismatch under equal content hashes cannot happen for a
        # real file, so there is nothing else worth asking.
        return stored_sha == current_sha
    return stored == current


def _entry_lists_match(stored, current, *, listing_names_only: bool = False) -> bool:
    """:func:`_entry_matches` over two whole lists, order-sensitive.

    Order matters because both lists are built deterministically (filelist
    order, sorted dependency paths), so a reordering is a real difference.
    A stored value that is not a list at all fails closed.
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

    Diffed **by name**, not position: an added or removed file shifts every
    entry after it, and answering that with "(entry count 3 -> 4)" names the
    directory but not the file, which is the whole point of the line. So a
    name only one side carries is reported as ``+added.svh`` /
    ``-removed.svh``, and a name both carry that no longer matches is
    reported as itself. Diagnostic only — the decision stays with
    :func:`_entry_lists_match`.
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
    # Nothing named differs, so the disagreement is in a shape the mapping
    # above dropped, or in the order the two lists carry.
    return _first_entry_mismatch(stored, current, listing_names_only=names_only)


def _first_entry_mismatch(stored, current, *, listing_names_only: bool = False):
    """What made :func:`_entry_lists_match` say no, for a diagnostic.

    Returns the first mismatching entry's path/line, or a shape note when
    the lists themselves are not comparable. Diagnostic only — never the
    decision, which stays with the matchers above, so it is told what the
    decision was made on (``listing_names_only``) rather than guessing.
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
                # "+incdir+/p/inc" alone does not answer "why did this
                # recompile" when the directory is what is stamped, so the
                # line names the file inside it as well (#478).
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

    Exists so a *hash* of a fingerprint can mean the same thing the
    entry-wise comparison means (#494 + #498). ``_entry_matches`` does not
    compare entries by equality: when both sides carry a content hash, the
    hash decides and ``size``/``mtime_ns`` are ignored, so a ``touch`` or a
    ``git checkout`` that restores byte-identical content is *not* a change.
    Hashing the raw entry would reintroduce exactly the mtime sensitivity
    #494 removed — and would do it on the one comparison whose "different"
    answer costs a re-run of a compile that already failed deterministically.

    So an entry that carries a hash collapses to ``[path, sha]``, and one
    that does not keeps its full ``[path, size, mtime_ns, None]`` shape —
    which is what ``_entry_matches`` falls back to comparing exactly. The
    two shapes can never compare equal to each other, matching that
    comparator's fail-closed answer when only one side could be hashed.
    Anything that is not an entry of this version's shape is passed through
    untouched: an unrecognised shape is "we do not know" on both sides.
    """
    if _is_directory_entry(entry):
        # Same reduction one level down, so a `touch` inside an include
        # directory does not move the sha either.
        return [entry[0], [_entry_identity(inner) for inner in entry[-1]]]
    if isinstance(entry, list) and len(entry) == 4 and entry[-1] is not None:
        return [entry[0], entry[-1]]
    return entry


def _fingerprint_sha(fingerprint):
    """Compact identity of one compile's inputs (#498 review).

    sha256 over the canonical JSON of the fingerprint dict — the very dict
    the stamp comparison checks (``stored_inputs``: the stamp minus its
    ``deps``/``simv`` keys), so "same sha" means exactly what "stamp would
    match" means: same sources, same flags, same toolchain.

    Canonical, not raw: each ``sources`` entry goes through
    :func:`_entry_identity` first, because the stamp comparison decides
    those entries by content hash where one exists (#494). Without that
    step a benign ``touch`` — or a rebuilt generated file with identical
    bytes — would move the sha, and a gated sim job would "earn" a retry of
    a compile whose inputs never changed, recompiling a deterministic
    failure under the sim reservation, which is the whole thing #498 is
    about. The other direction is safe either way: an edited byte moves the
    content hash and therefore the sha.

    The ONE hashing used by both sides of the no-retry verdict: a build
    job records it beside a failed compile's returncode, and the gated sim
    job recomputes it over its own just-derived fingerprint. Equal says
    the sim job is looking at the same compile the build failed, so
    repeating it is pointless; different says the inputs moved since — an
    edited source, a PRE that regenerated one — and the failure may not
    reproduce, so the retry is earned. Factored here so the two sides
    cannot drift. ``None`` in, ``None`` out.
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
    """Everything about a compile that is decided *before* the builder runs.

    Split out of :meth:`VlogSim.compile` so the compile key can be asked for
    without compiling (#495): a dispatched build job groups its configs by
    :attr:`group_dir` and compiles the groups concurrently, and two configs
    that would write the same directory must land in the same group (#369).

    The grouping value is derived here and nowhere else on purpose. A second
    derivation — a standalone "what would the key be" helper — drifts from
    the real one the first time somebody touches the Icarus wrapper args or
    the VCS output-flag strip, and the symptom of that drift is two builders
    in one directory, which is corruption rather than a wrong number.
    """

    compile_work_dir: str
    filelist_path: str
    build_dir: str
    builder_opts: list = field(default_factory=list)
    extra_compile_flags: list = field(default_factory=list)
    assertion_flags: list = field(default_factory=list)
    # The family's top-selection flag for the testbench's declared
    # `toplevel:` (#506, #508), or empty when none is declared, the family
    # has no such flag, or the configured opts already pin one.
    top_flags: list = field(default_factory=list)
    plusdefines: list = field(default_factory=list)
    is_verilator: bool = False
    # None unless share_build is on AND the family supports sharing.
    key_cmd: list | None = None
    fingerprint: dict | None = None
    shared_dir: Path | None = None
    # Why sharing was declined, or None. Set only when share_build is on.
    unsupported_reason: str | None = None
    # What this compile writes over: the shared build dir when the build is
    # shareable, the absolute `builder-simv:` when one pins the executable,
    # else the test's own compile work dir. Two configs with the same value
    # MUST NOT compile concurrently.
    #
    # It is the build DIRECTORY and not (compile_work_dir, shared_dir):
    # under share_build the whole point is that two DIFFERENT tests with one
    # key write one shared dir, and a key that included the per-test dir
    # would split them into two groups and put two builders in it (#369).
    # The pinned-simv case is the same rule read the other way — there the
    # per-test dirs differ but the output does not, so the output is the key.
    group_dir: str = ""


class VlogSim:
    """
    Verilog Sim Compile and Execution
    """

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
        expect_prebuilt=False,
        rebuild=False,
        build_result_json=None,
    ):
        """
        compile and execute sim for given test
        """
        self.name = name
        self.root_cfg = root_cfg
        self.rtl_builder_cfg = root_cfg.resolve_rtl_builder_cfg(
            test_cfg.get_builder_name()
        )
        self.rtl_builder_mode = rtl_builder_mode
        self.sim_mode = sim_mode
        # assert 'sim_to_stdout' in self.sim_mode NOTE: not used anywhere, may or may not become important in the future
        self.test_cfg = test_cfg
        self.test_name = self.test_cfg.get_name()
        self.run_id = run_id
        self.replay_run_id = replay_run_id
        self.testbench = self.test_cfg.get_testbench()
        self.vlog_post = None
        # Why the last stamp check said no, in one phrase, or None. Read by
        # the gated-retry warning, which is the only place a dispatched
        # job's INFO-level log can carry it (#536).
        self.stamp_mismatch_reason = None
        # Which build this run ended up simulating: the stamp's own
        # ``{fingerprint_sha, simv}``, recorded wherever a build is
        # stamped, reused or adopted. It rides the result envelope so the
        # head can check at collect that every run of one compile key named
        # the same binary (#535).
        self.last_build_stamp = None
        # Opt-in: key the build dir on a hash of the compile inputs so tests
        # with identical inputs share one simv (#293). The resolved shared
        # dir is only known once compile() has written the filelist.
        self.share_build = share_build
        # `--rebuild`: distrust the stamp and compile anyway (#494). The
        # escape hatch for the case no stamp can see — a source restored to
        # byte-identical content by a tool that also changed how it is
        # built, an obj_dir somebody edited by hand — and the answer to the
        # issue's "dropping --share-build does not stop the reuse". It is
        # honoured at most ONCE per build dir per process; see
        # :func:`_claim_rebuild`.
        self.rebuild = rebuild
        self._shared_build_dir = None
        # Filled by _compile_plan() and consumed (and cleared) by compile(),
        # so a probe and the compile that follows it share one derivation
        # while a *second* compile() on this instance still re-stats its
        # sources — a source edited between two compiles has to invalidate
        # the stamp.
        self._compile_plan_cache = None
        # What the last compile *this instance* performed cost, for the
        # build envelope and the results overlay (#495). A dict
        # {duration_sec, builder, reused} — never a stamp key: the stamp's
        # key set IS the fingerprint comparison (_build_stamp_is_valid), so
        # an extra key there would permanently invalidate every stamp ever
        # written. None until something records one.
        self.last_compile = None
        # Set by a dispatched sim job that was gated on a build job, so
        # compiling here means the stamp that build left did not
        # validate — worth a WARNING, because the whole serialization
        # guarantee rests on it (#369).
        self.expect_prebuilt = expect_prebuilt
        # That build job's envelope, when the head knew one (#498). It is
        # what separates the two reasons a stamp fails to validate: the
        # build's compile FAILED for this test (deterministic — retrying it
        # here only burns the sim reservation and overwrites the real
        # error), or the stamp is merely absent/stale (toolchain drift, a
        # clock skew) and a retry is the right answer. None everywhere else,
        # including every local run, where there is no build job at all.
        self.build_result_json = build_result_json
        # What a failed compile *this instance* ran cost the caller in
        # diagnostics: {returncode, transcript}. Read by a dispatched build
        # job to record the failure in its envelope (#498); None until a
        # compile actually fails, and reset by each compile() so a second
        # one on this instance cannot inherit the first's verdict.
        self.last_compile_failure = None
        # A one-line desc that replaces the generic "Compile failed" when
        # this compile failed for a reason the sim itself already knows
        # (#498). Set only on the gated-build-failed path.
        self.compile_fail_desc = None
        # Full-path override for where the next compile transcript is
        # written; None means the test-scoped `compile.log`. The retry a
        # gated sim job runs when the build's stamp did not validate must
        # NOT truncate `compile.log`: that file is the build job's, and
        # overwriting it replaces a real compile error with whatever the
        # retry hit under the sim's (smaller) reservation — an 8G OOM
        # reading as "signal 9" in the ECP report that filed #498. A full
        # path rather than a name, because the retry log is RUN-scoped
        # (#498 review): sibling runs of one fanned-out test share the test
        # artefact dir, and a test-scoped retry log is one run's story
        # advertised — and destroyed — by every sibling.
        self._compile_transcript_override = None
        # CLI commands always pass suite_dir resolved from the test
        # config (see ExecutionContext / rtl_buddy.py). The cwd fallback
        # is tests-only — `tests/test_setup_failures.py`,
        # `tests/test_cocotb_post.py`, etc. construct VlogSim directly
        # with a monkeypatched cwd. New code paths must pass suite_dir.
        self.suite_work_dir = (
            os.path.abspath(suite_dir)
            if suite_dir is not None
            else os.path.abspath(os.getcwd())
        )
        # Where the head writes its own log (ExecutionContext.log_path), the
        # one path a directory listing skips by location rather than name.
        self._suite_log_path = os.path.realpath(
            os.path.join(self.suite_work_dir, DEFAULT_FILE_LOG)
        )

        # Which files this instance is allowed to content-hash for its build
        # stamps (#494). The PROJECT root, not the suite dir: models, RTL and
        # shared headers routinely live outside the suite that compiles them,
        # and those are exactly the files an edit-then-rerun changes.
        # Realpath'd once so containment tests compare canonical paths.
        get_project_rootdir = getattr(self.root_cfg, "get_project_rootdir", None)
        try:
            project_root = (
                get_project_rootdir() if get_project_rootdir is not None else None
            )
        except Exception:
            # Deciding what may be hashed must never be what stops a build
            # (build-job exit-0 contract): an unusable root just narrows the
            # policy to the suite dir below.
            project_root = None
        # The cwd fallback matches suite_work_dir's, for the same
        # directly-constructed callers.
        derived = isinstance(project_root, str) and bool(project_root)
        self._project_root = os.path.realpath(
            project_root if derived else self.suite_work_dir
        )
        # Falling back narrows hashing to the suite dir, which turns the fix
        # off for out-of-suite RTL — the thing #494 is about. Silent is the
        # wrong way for that to happen, so the root actually in force (and
        # where it came from) is readable off a build-job log.
        log_event(
            logger,
            logging.DEBUG,
            "compile.hash_root",
            test=self.test_name,
            project_root=self._project_root,
            derived=derived,
        )
        # Resolved lazily and once: an install prefix under the project root
        # (a vendored toolchain, an in-repo venv) is excluded from hashing,
        # and finding it costs a PATH walk that most instances never need.
        self._toolchain_prefix = _UNSET

        output_dir = Path(self.suite_work_dir) / "artefacts"
        output_dir.mkdir(parents=True, exist_ok=True)

        self.output_dir = str(output_dir)

    def _get_build_tag(self):
        """
        Return a filesystem-safe tag derived from the test name.
        """
        return test_artifact_dir(self.suite_work_dir, self.test_name).name

    def _get_build_dir(self):
        """
        Return the simulator build directory for this test.
        """
        return test_build_dir_name(self.test_name)

    def _get_compile_work_dir(self):
        return self._get_artifact_dir()

    def _get_simv_path(self):
        """
        Return the simulator executable path for this test/build.

        - Verilator: `<artefact>/<build>/simv` (the binary produced by `verilator --binary`).
        - Icarus: `<artefact>/simv` — a tiny shell wrapper around `vvp <build>/simv.vvp`
          so the existing execute() path can invoke it as a single executable.
        - Other backends: honor `builder-simv:` from the builder config.

        Under an active shared build the executable is always `simv`
        directly inside the shared dir, whatever the family: that is the
        one path every other test with the same compile key looks for, and
        what `_shared_build_is_valid` validates against the stamp.
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
        """Extra `vvp` arguments injected ahead of the snapshot in the wrapper.

        Base VlogSim needs none. CocotbSim overrides this to load the cocotb
        VPI module (`-M <libs> -m libcocotbvpi_icarus`) at run time, since
        Icarus binds VPI at `vvp` invocation rather than at compile.
        """
        return []

    def _write_icarus_simv_wrapper(self):
        """Write a shell wrapper that execs `vvp [extra] <snapshot> "$@"`.

        Lets the existing execute() path invoke a single executable regardless
        of backend; Icarus's two-phase compile/run becomes invisible. Any
        `_icarus_vvp_extra_args()` (e.g. cocotb VPI flags) are placed before
        the snapshot, where `vvp` requires its `-M`/`-m` options.
        """
        wrapper_path = self._get_simv_path()
        snapshot = self._get_icarus_snapshot_path()
        argv = ["exec", "vvp", *self._icarus_vvp_extra_args(), snapshot]
        cmd = " ".join(shlex.quote(part) for part in argv)
        Path(wrapper_path).write_text(f'#!/bin/sh\n{cmd} "$@"\n')
        os.chmod(wrapper_path, 0o755)

    def _get_artifact_dir(self, run_id=None):
        return str(
            test_artifact_dir(self.suite_work_dir, self.test_name, run_id=run_id)
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
        """Where THIS run's gated retry writes its transcript (#498 review).

        In the run's artifact directory (``run-NNNN`` under the test dir,
        or the test dir itself for a single run) — the established home of
        run-dependent outputs — because the retry is one run's recompile:
        sibling runs of a fanned-out test share the test dir, and a
        test-scoped ``compile.retry.log`` would be overwritten, unlinked
        and advertised across runs that never retried.
        """
        return str(
            Path(self._get_artifact_dir(run_id=self.run_id))
            / COMPILE_RETRY_TRANSCRIPT_NAME
        )

    def clear_retry_transcripts(self, run_ids):
        """Unlink the stale retry transcript of every run in ``run_ids``.

        For a caller whose one compile serves several runs
        (:meth:`TestRunner.run_multiple`): the per-run cleanup in
        :meth:`pre`/:meth:`compile` reaches only ``self.run_id``, so a
        local rerun after a dispatched fan-out would leave runs 2..N
        advertising the dispatch's retry transcripts beside their fresh
        results (#498 review). Best-effort, like every artefact-dir touch.
        """
        for run_id in run_ids:
            try:
                (
                    Path(self._get_artifact_dir(run_id=run_id))
                    / COMPILE_RETRY_TRANSCRIPT_NAME
                ).unlink(missing_ok=True)
            except OSError:
                pass

    def _get_build_compile_transcript_path(self):
        """Where the *build job* wrote this test's transcript.

        Always ``compile.log``, whatever this instance is about to write:
        the gated sim job names the build's file when it declines to retry,
        and it must not accidentally name its own retry log (#498).
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
        compile_opts = self.rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
        if any(opt.startswith("--coverage") for opt in compile_opts):
            return True
        # Verilator-side `--coverage-user` injected by assertions=true is enough
        # to produce a coverage.dat that the cov pipeline can read.
        if self._assertions_enabled() and self._get_simulator_family() == "verilator":
            return True
        return False

    def _assertions_enabled(self):
        """SVA assertions requested for this test (Verilator-only today)."""
        return bool(getattr(self.test_cfg, "assertions", False))

    def _get_verilator_assertion_flags(self, builder_opts: list[str]) -> list[str]:
        """Return Verilator-specific flags needed to compile in SVA + cover hits.

        Idempotent: skips flags already present in the builder's configured opts.
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
        """The top the builder's own ``compile-time`` opts pin, or ``None``.

        Returns ``(flag_as_written, module_or_None)``. USER opts only, and
        filtered exactly as :meth:`_build_compile_plan` filters them, so a
        subclass can ask "did the user already choose a top?" before
        generating one of its own. A backend that generated unconditionally
        would place its flag *after* the user's on the command line and win
        on Verilator's last-wins precedence, silently overriding the
        configured top and suppressing the conflict warning (#511 review).
        """
        spec = TOP_MODULE_FLAGS.get(self._get_simulator_family())
        if spec is None:
            return None
        return _find_configured_top(
            spec,
            self._filter_builder_opts(
                self.rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
            ),
        )

    def _get_top_module_flags(
        self, builder_opts: list, extra_compile_flags: list
    ) -> list:
        """Root the compile at the testbench's declared ``toplevel:`` (#506, #508).

        Without it, both the elected top and — for Verilator — the model
        name and every emitted C++ file come from filelist order: the first
        *ordinary* (non-``-v``) entry wins, so recomposing a model filelist
        silently renames the model, and an ordinary input carrying a module
        nothing instantiates turns the build into a MULTITOP error. The
        declared ``toplevel:`` is the answer to both, and until now only the
        SystemC and cocotb-on-VCS paths passed it on.

        Nothing is added when no ``toplevel:`` is declared. A testbench
        ``name:`` is a config label, not necessarily a module, so defaulting
        the top to it would turn working builds into "top module not found"
        — ``toplevel:`` stays the explicit knob.

        Idempotent in the same spirit as
        :meth:`_get_verilator_assertion_flags`. "Already pinned" is matched
        across every spelling the family accepts, not just the one rtl_buddy
        emits (see :class:`_TopFlagSpec`): a project that worked around #508
        with ``--top spare_top`` in ``compile-time`` would otherwise get a
        second, later ``--top-module`` that Verilator's last-wins precedence
        hands the win to.

        The two flag sources are consulted for DIFFERENT questions, and
        keeping them apart is the point:

        * ``builder_opts`` — the user's ``compile-time`` — answers "did the
          user pin a top?". A configured top wins, because it is the more
          specific statement about this build, and one that *disagrees* with
          ``toplevel:`` is a WARNING: that combination is how a suite
          silently simulates a different design than its config names. The
          warning is claimed once per (family, configured top, declared top)
          per process — the fact belongs to the builder config every test of
          the suite shares, so warning per test would be N copies of one line.
        * ``extra_compile_flags`` — what the SystemC / cocotb subclass
          generated — answers only "would a second flag be a duplicate?".
          Scanning it for the *conflict* would let our own generated flag
          shadow the user's: it lands later on the command line, so the scan
          would find it, call it agreement, and suppress the warning while
          Verilator's last-wins handed the generated top the victory (#511
          review). Those subclasses now suppress their own generated flag
          when the user pinned one, so this branch only fires when there is
          nothing of the user's to conflict with.
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
                    # Omitted (not None) for a bare flag, so the human
                    # message can say "with no value" instead of "None".
                    configured=existing,
                )
            return []

        generated = _find_configured_top(spec, extra_compile_flags)
        if generated is not None:
            # Ours, not the user's: never a conflict, only a duplicate to
            # avoid. Reached when the backend generated a top and the user
            # pinned none.
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
        """
        Return the canonical simulator family for backend-specific handling.
        """
        return self.rtl_builder_cfg.get_simulator_family()

    def _filter_builder_opts(self, opts: list) -> list:
        return opts

    def _get_extra_compile_flags(self) -> list:
        return []

    def _get_extra_compile_env(self) -> dict:
        """Hook for subclasses to inject env vars into the compile subprocess.

        Base VlogSim has no extra env. SystemCSim overrides to pin CXX and
        export SYSTEMC_HOME / SYSTEMC_INCLUDE / SYSTEMC_LIBDIR so Verilator's
        --build step picks them up when invoking the generated Makefile.
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
        """
        generate run.f for sim
        """
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
        """Install tree of the resolved simulator exe, if it is worth excluding.

        The hashing policy is "the project's files, not the toolchain's"
        (brief invariant 4), and "under the project root" only implements
        that while the toolchain is installed elsewhere. A vendored
        ``tools/verilator/bin/verilator`` or an in-repo venv puts
        ``verilator_bin`` and ``verilated.h`` inside the root, where they
        would be content-hashed — tens of megabytes read once per process
        per node, the exact cost the policy exists to avoid.

        The prefix is the exe's directory, or its parent when that
        directory is ``bin`` (``<prefix>/bin/verilator`` alongside
        ``<prefix>/share/verilator/include``). It is used only when it is a
        *proper* subdirectory of the project root: a project that keeps its
        simulator in ``<root>/bin`` would otherwise derive ``<root>`` and
        silently exclude everything, turning the whole check off.
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
            # Never the thing that fails a build (exit-0 contract): an
            # underivable prefix just means nothing is excluded.
            self._toolchain_prefix = None
        return self._toolchain_prefix

    def _tracked_entry(self, path, *, resolved=False):
        """:func:`_hashed_stat_entry` under this instance's hashing policy.

        ``resolved`` says ``path`` is already a ``realpath`` and the
        containment tests can skip re-walking it.
        """
        return _hashed_stat_entry(
            path,
            project_root=self._project_root,
            resolved=resolved,
            toolchain_prefix=self._get_toolchain_prefix(),
        )

    def _is_suite_log(self, path) -> bool:
        """Is ``path`` the head's own ``rtl_buddy.log`` in the suite directory?

        By name first, so the ``realpath`` is only paid for a candidate.
        """
        return (
            os.path.basename(path) == DEFAULT_FILE_LOG
            and os.path.realpath(path) == self._suite_log_path
        )

    def _directory_listing(self, dir_path, *, recursive):
        """A listing of the regular files under ``dir_path``, or ``None``.

        Each file is stamped with the same ``[name, size, mtime_ns, sha]``
        shape :meth:`_tracked_entry` gives a source, so the whole listing
        goes through :func:`_entry_matches` and is decided by content where
        the hashing policy allows it — the point of #494 applies inside an
        include directory too, and on the NFS mounts that motivated it a
        listing compared by mtime would be no more trustworthy than the
        stats it replaced.

        ``recursive`` follows the search the option performs. An
        ``+incdir+`` is walked, because `` `include "nested/deep.svh" ``
        resolves *beneath* the include directory and a flat listing would
        leave an edit to that header invisible on any builder with no
        dependency file (#478 review). A ``-y`` library directory is not:
        library resolution maps a module name to a file in the directory
        itself, so a subdirectory holds nothing the search can reach. The
        walk does not follow symlinked subdirectories — ``os.walk``'s
        default — which bounds it against a link loop; a symlinked *file*
        is listed like any other.

        **Unfiltered.** Nothing is selected by suffix. For ``+incdir+`` any
        name at all can be `` `include ``d; for ``-y`` the suffixes come
        from ``+libext+``, which can be set on the builder command line
        (``builder-opts.compile-time``) and never appear in ``run.f`` at
        all — a filter derived from ``run.f`` alone silently missed those
        and reused a stale build when a matching library file appeared,
        which is the very failure this stamp exists to stop. Listing
        everything costs a fraction of a second even for a few thousand
        files, and over-approximating is the safe direction.

        Names are relative to ``dir_path``, with ``/`` separators on every
        platform so the stamp does not change spelling between them. The
        directory's own path is already fixed by ``entry[0]`` — the
        ``run.f`` line, which the compile key hashes, so every test sharing
        a build has exactly the same one — and repeating it per file would
        only bloat the stamp.

        Two kinds of name are skipped, and only two. **Directories** that
        are dot-prefixed (``.git``, ``.svn``) or are one of rtl_buddy's own
        managed artefact trees are never descended into — see
        :func:`_is_pruned_walk_dir`. **Files** are skipped when they match
        :data:`_NON_INPUT_FILE_PATTERNS`, which is editor and VCS
        bookkeeping plus rtl_buddy's own per-test outputs. A dot-*file* is
        otherwise listed like any other: `` `include ".config.svh" `` is
        legal and resolves, so a blanket dot-name skip would reopen the gap
        this stamp closes. One more file is skipped by *path*: the suite's
        own ``rtl_buddy.log`` (#537), which the head appends to for the
        whole run. Only that one — a file of the same name anywhere else
        is an input like any other, and stays tracked.

        Both halves exist for one failure. Everything rtl_buddy writes into
        an artefact directory — ``run.f``, the compile transcript, the
        logs, the result envelope, the build output, the stamp itself — is
        written *after* the fingerprint that would list it, so a listing
        that contained any of them could never validate again: every later
        process saw a different one and recompiled, which under
        ``--dispatch`` is every gated simulation job. The directory prune
        covers an ``+incdir+`` that is an *ancestor* of ``artefacts/``
        (``+incdir+.`` in a tests.yaml). The file-name exclusion covers an
        include root that *is* one — a ``preproc`` hook is documented to
        generate headers into its ``artifact_dir``, and then the filelist
        names ``+incdir+artefacts/<test>`` and no path component ever says
        "managed". Those generated headers must stay tracked, so the tree
        is walked and only the outputs are removed.

        ``None`` comes back when the directory cannot be read. That degrades
        to the pre-#478 untracked entry rather than to an empty listing,
        which would claim the directory *is* empty and validate a reuse on
        the strength of it.
        """

        def _reraise(error):
            # os.walk swallows a directory it cannot open by DEFAULT, which
            # here would produce an *empty* listing — the one answer this
            # method must never give, since "the directory is empty"
            # validates a reuse. Re-raise into the handler below instead.
            raise error

        entries = []
        try:
            if recursive:
                for walk_root, dir_names, file_names in os.walk(
                    dir_path, onerror=_reraise
                ):
                    # In-place, because os.walk reads this list back to
                    # decide where to descend: a pruned name is never
                    # walked at all, so a `.git` or an `artefacts/` inside
                    # an include dir costs nothing rather than being walked
                    # and dropped.
                    dir_names[:] = sorted(
                        name for name in dir_names if not _is_pruned_walk_dir(name)
                    )
                    for name in sorted(file_names):
                        if _is_non_input_file(name):
                            continue
                        path = os.path.join(walk_root, name)
                        if not os.path.isfile(path) or self._is_suite_log(path):
                            # A dangling symlink is not an input; a FIFO
                            # must never reach the hasher's `open()`.
                            continue
                        entries.append((os.path.relpath(path, dir_path), path))
            else:
                with os.scandir(dir_path) as scan:
                    # `is_file` follows symlinks (a symlinked-in library
                    # file is a perfectly ordinary input) and answers from
                    # the dirent where the platform supplies one, so this
                    # costs at most the one `stat` per file
                    # `_tracked_entry` needs anyway.
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
        """Per-entry (line, size, mtime_ns, sha) stamps for the generated run.f.

        The content hash is what makes an edit invalidate the stamp on a
        cluster, where a cached NFS ``stat`` can still describe the file as
        it was before the edit (#494) — see :func:`_content_sha`. It goes in
        the *fingerprint*, never in the key: :meth:`_compile_config_key`
        reads ``entry[0]`` only, so an edit still rebuilds in place instead
        of stranding a new obj_dir per edit.

        An entry that resolves to a **directory** — ``+incdir+``, ``-y`` —
        gains a fifth element holding a listing of the files inside it
        (:meth:`_directory_listing`, recursive for ``+incdir+`` and flat for
        ``-y``, following what each option's search can reach), so a header
        edit reachable only through an include path invalidates the stamp
        for *every* builder, with or without a dependency file, and a file
        *appearing* in a library directory does too. The latter is the case
        no depfile can report at all: ``-y`` resolves by module name on
        demand, so a file that changes tomorrow's elaboration was opened by
        nobody today (#478). Where a builder does emit a dependency file it
        stays the more precise record of what was consumed, and both are
        kept.

        This requires the absolute ``+incdir+``/``-y`` spelling #474 gives
        ``run.f``. With the old relative one, a ``tests.yaml`` ``+incdir+.``
        resolved against the *artefact* directory, whose contents change on
        every run — the listing would then never match and the stamp would
        never validate.

        The listing is deliberately an over-approximation: it invalidates on
        an edit to a header nothing includes. The two error directions are
        not symmetric — over-invalidating costs one recompile, while
        under-invalidating reports a stale binary as green — and this is a
        stamp used to gate merges.

        Entries that resolve to neither a file nor a directory (``+define+``,
        ``+libext+`` suffixes, a path that no longer exists) keep only their
        raw line.

        Quoted entries (emitted for paths containing whitespace) are unquoted
        here with ``shlex`` before stat'ing. This unquoting is independent of
        the builder's own ``-f`` parser: Verilator's quote handling was
        validated, other builders' were not — but a bare path with whitespace
        was already broken for every builder, so quoting only appears where
        nothing worked before. The raw (quoted) line is what goes into the
        stamp, matching what ``run.f`` actually contains.
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
                    # An unbalanced quote must degrade to [line, None,
                    # None, None] like every other malformed entry, not
                    # abort the compile from the stamping path.
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
            if listing is not None:
                # The raw line stays entry[0] here too, so a listing that
                # changes moves the stamp and never the compile key.
                stamps.append([line, None, None, None, listing])
            elif os.path.isfile(resolved):
                # The raw line, not the resolved path, stays entry[0]:
                # it is what run.f contains and what the compile key
                # hashes.
                stamps.append([line] + self._tracked_entry(resolved)[1:])
            else:
                stamps.append([line, None, None, None])
        return stamps

    def _fingerprint_toolchain(self, exe):
        """Which simulator install this build would come out of.

        ``cmd`` records the *configured* executable — "verilator", the same
        string whichever install ``PATH`` resolves it to. Without this
        entry, pointing the project at a different simulator left every
        shared build's stamp still validating, so the new toolchain was
        never invoked: the compile short-circuited and the run reported PASS
        on a binary the old one had produced. That is silent by
        construction, and it makes a toolchain A/B report green regardless
        of which side it is on (INF-22). It bit hardest under ``--dispatch``,
        which implies ``--share-build``, so the same regression run locally
        (compiling per test) failed correctly and the dispatched one passed.

        ``exe`` goes in the *key* — two installs get two build dirs, which
        is what an A/B wants — while size, mtime and version go in the
        *stamp*, so upgrading one install in place rebuilds in place
        instead of stranding a directory per version. Same split as
        ``sources``, and for the same reason.
        """
        resolved = shutil.which(exe)
        entry = {
            "exe": resolved or exe,
            "size": None,
            "mtime_ns": None,
            "version": None,
        }
        if resolved is None:
            # Nothing to stat: the compile below is about to fail on this
            # anyway, with a better message than we could give here.
            return entry
        try:
            stat = os.stat(resolved)
        except OSError:
            return entry
        entry["size"] = stat.st_size
        entry["mtime_ns"] = stat.st_mtime_ns
        # A wrapper script (verilator's `bin/verilator` is one) can keep its
        # size and mtime across an upgrade of the binary it dispatches to, so
        # the version banner is the entry that actually catches that case.
        entry["version"] = _probe_toolchain_version(
            resolved, self._get_simulator_family(), stat.st_mtime_ns
        )
        return entry

    def _compile_fingerprint(self, key_cmd, filelist_path):
        """Everything that determines the compiled binary.

        Runtime-only inputs (seed, plusargs, run-time opts, timeout,
        coverage output path) are deliberately excluded — they vary per
        test/run without changing the simv.

        Must stay JSON-native (lists/dicts/str/int/None): the stamp check
        compares this dict against a json.loads() round-trip, so a tuple
        here would silently disable reuse rather than error.
        """
        return {
            "cmd": list(key_cmd),
            "env": dict(sorted(self._get_extra_compile_env().items())),
            "sources": self._fingerprint_filelist_sources(filelist_path),
            "toolchain": self._fingerprint_toolchain(key_cmd[0]),
        }

    @staticmethod
    def _compile_config_key(fingerprint):
        """Short stable hash naming the shared build dir.

        Excludes source size/mtime/content-hash — and the toolchain's
        size/mtime/version — so editing RTL or upgrading a simulator in
        place rebuilds in the same dir (the stamp comparison catches the
        staleness) instead of accumulating a new obj_dir per edit. Only
        ``entry[0]``, the run.f line, is read out of each source entry, so
        adding the content hash to the stamp in #494 left every existing
        key unchanged.
        """
        config = {
            "cmd": fingerprint["cmd"],
            "env": fingerprint["env"],
            "filelist": [entry[0] for entry in fingerprint["sources"]],
            # The install, not its version: a rebuilt-in-place simulator
            # should reuse this dir (the stamp catches the staleness), while
            # a genuinely different install gets its own, so an A/B keeps
            # both builds instead of overwriting one with the other.
            "toolchain": fingerprint["toolchain"]["exe"],
        }
        digest = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return digest[:16]

    def _write_compile_transcript(self, run_str, result):
        """Persist the compile command and its captured output; return the path.

        Written on every compile that ran, pass or fail. Before #494 only a
        failure (or a license-queued VCS build) left one, which was harmless
        while the only other state was no file at all — but a reuse now
        writes a breadcrumb here, and had success stayed silent the file's
        *presence* would have come to mean "nothing compiled", inverting
        what docs/concepts/tests.md says it is.

        Best-effort, like the reuse breadcrumb it is now paired with: this
        runs on the SUCCESS path too since #494, and a builder that exited 0
        must not be turned into a failed compile because its transcript
        could not be written. Returns ``None`` when nothing was written, so
        the events below simply carry no transcript path.
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
        """Did a ``vcs`` elaboration wait in the ``-licqueue`` queue (#358)?

        Elapsed compile time is only a measure of compile *work* when no
        part of it was spent waiting for a seat. Under dispatch that
        distinction decides whether a build job that hit its ``--time``
        needs a bigger reservation or a freer license server, so the answer
        is logged rather than left to be inferred from the wall clock.
        Non-VCS families never queue, and there is nothing to inspect if the
        output was not captured.
        """
        if self._get_simulator_family() != "vcs":
            return False
        return has_license_queue_marker((result.stdout or "") + (result.stderr or ""))

    def _share_build_unsupported_reason(self):
        return share_build_unsupported_reason(self.rtl_builder_cfg)

    def _vcs_shared_output_argv(self, build_dir):
        """VCS flags that put the whole build inside ``build_dir``.

        VCS has no single ``--Mdir``-style knob like Verilator: the
        executable location comes from ``-o`` (and it writes its
        ``simv.daidir`` beside it) while the intermediate C tree comes from
        ``-Mdir``. Both are pointed into the shared dir so the build is
        self-contained — a later stale-stamp rebuild driven from a different
        test's artefact dir then reuses the same incremental tree instead of
        starting from scratch.
        """
        return [
            "-o",
            str(Path(build_dir) / "simv"),
            f"-Mdir={Path(build_dir) / 'csrc'}",
        ]

    @staticmethod
    def _strip_vcs_output_opts(opts):
        """Split configured VCS opts into (kept, dropped ``-o``/``-Mdir``).

        A shared build owns the output location — the simv must land at
        ``<shared>/simv`` or the tests pointed at it look in the wrong
        place — so a ``builder-opts`` entry that also sets one is dropped
        rather than left to fight ours on VCS's duplicate-option
        precedence. Handles both ``-Mdir=dir`` and ``-Mdir dir``.
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

        Closes the gap the filelist fingerprint cannot: an entry resolving
        to a *directory* (``+incdir+``, ``-y``) is recorded as a raw line,
        so a header edit reachable only through one leaves the stamp valid
        and a warm run reuses a simv built from the old header (#303). The
        builder already knows exactly which files it opened, so this reads
        its dependency file instead of re-deriving the include search.

        ``None`` means no dependency information exists for this build —
        every non-Verilator family, or a Verilator invocation that emitted
        no ``.d`` — and is stored as such: it is the difference between
        "nothing else was consumed" and "we do not know", and only the
        first may validate a reuse.

        Paths are resolved against ``compile_cwd`` and stored absolute:
        the file is written relative to whichever test's artefact dir ran
        the compile, and a *different* test with the same compile key
        validates the stamp from its own directory. Stored by the name the
        build used (``normpath``, as in :meth:`_fingerprint_filelist_sources`),
        *not* its ``realpath``: a symlink among the prerequisites — an
        ``+incdir+`` that is a link into a shared IP tree, or a header that
        is itself a link — is re-resolved on every validation, so pointing
        it at a new target invalidates the stamp even though the listing
        the filelist fingerprint keeps still shows the same names. The
        compile's own ``run.f`` is excluded (matched by realpath, so that a
        symlinked suite dir cannot hide it) — it is regenerated on every
        compile, so its mtime would invalidate the stamp for the very test
        that built it, and its *contents* are already fingerprinted entry
        by entry.
        """
        # Resolved against the compile cwd, not used as given: `build_dir` is
        # an absolute shared dir on one path and a bare directory *name* on
        # the other, and globbing the latter would search the process cwd.
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
        return [self._tracked_entry(path) for path in sorted(seen)]

    def _deps_unchanged(self, test_name, deps, *, quiet=False):
        """Have any of the stamp's recorded inputs changed on disk?

        Entry-wise through :func:`_entry_matches`, so a dependency inside
        the project root is decided by its content and one outside it (a
        toolchain header) by its stats — and a stamp written before #494,
        whose entries are 3 elements long, fails closed into one rebuild.

        Every shape this version does not recognise answers False rather
        than raising, all the way up to ``deps`` not being a list at all: a
        mixed-version cluster (submit host upgraded, compute nodes not) can
        hand an older node a container type it was never taught, and the
        answer to that is a rebuild, not an exception out of a build job.
        """
        if not isinstance(deps, list):
            return self._note_stamp_mismatch("the stamp's dependency list is corrupt")
        for entry in deps:
            if not isinstance(entry, list) or len(entry) != 4:
                return self._note_stamp_mismatch(
                    "the stamp's dependency list is corrupt"
                )
            if not isinstance(entry[0], str):
                # `os.stat` takes a file *descriptor* for an int, so a
                # corrupt stamp must never reach it.
                return self._note_stamp_mismatch(
                    "the stamp's dependency list is corrupt"
                )
            if not _entry_matches(entry, self._tracked_entry(entry[0])):
                # The one question worth answering when a warm run
                # unexpectedly recompiles.
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

        ``stamp_dir`` and the executable are separate arguments because an
        unshared build does not put the executable inside a directory
        rtl_buddy chose: the stamp goes in the test's compile work dir while
        ``builder-simv:`` decides where the binary lands (#369).

        Everything but the tracked inputs compares by exact equality; the
        two tracked-input lists (``sources`` and ``deps``) go entry-wise
        through :func:`_entry_matches`, which lets a content hash outvote a
        moved mtime and a moved mtime outvote nothing at all (#494).

        ``quiet`` suppresses the "why this stamp lost" diagnostics for the
        one caller that asks the question twice — :meth:`compile`'s
        unlocked reuse pre-check, whose in-lock repeat is the authority
        and owns those lines. Whether the stamp validates is not affected.

        Every verdict of False also records *why* in
        :attr:`stamp_mismatch_reason`, which is what a gated sim job's
        `compile.prebuilt_stamp_invalid` reports: those runs log at INFO,
        so the DEBUG lines below are the one thing a reader of a dispatched
        job's log cannot get at (#535/#536).
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
            # Written before dependency tracking existed. Its silence about
            # headers is indistinguishable from having had none, so the only
            # honest reading is one rebuild — after which the stamp says
            # which it is.
            return self._note_stamp_mismatch("the stamp predates dependency tracking")
        # The executable is an *output*, so the input fingerprint says
        # nothing about it. That was harmless while the output always lived
        # in a directory named after those inputs, and stops being harmless
        # here: an absolute `builder-simv:` is one path shared by every test
        # using that builder, while the stamp is per test. Without this,
        # test_a's stamp keeps validating after test_b overwrote the binary
        # they both point at, and test_a silently simulates test_b's build
        # (#369).
        if stored.get("simv") != _stat_entry(str(simv_path)):
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
            # A caller asserting a stamp is stale hands in no fingerprint;
            # nothing can match one.
            return self._note_stamp_mismatch("no fingerprint to compare against")
        stored_inputs = {
            key: value for key, value in stored.items() if key not in _STAMP_META
        }
        # `sources` is the one input list whose entries are not compared by
        # equality, so it comes out of the dict comparison and goes through
        # _entry_matches; cmd/env/toolchain stay exact. Popping from copies
        # keeps the two sides symmetrical — a stamp that has no `sources`
        # key at all still fails, because None is not a list.
        stored_sources = stored_inputs.pop("sources", None)
        current_inputs = dict(fingerprint)
        current_sources = current_inputs.pop("sources", None)
        if stored_inputs != current_inputs:
            if not quiet:
                _log_stale_stamp_toolchain(
                    stored_inputs, current_inputs, test_name=test_name
                )
            return self._note_stamp_mismatch("the compile line or toolchain changed")
        # A stamp that recorded the builder's own dependency list decides
        # every tracked file's *content* there, so the directory listings
        # are compared by name alone (#536) — see :func:`_entry_matches`.
        deps = stored["deps"]
        if not _entry_lists_match(
            stored_sources, current_sources, listing_names_only=deps is not None
        ):
            # The deps path names what changed; the sources path answering
            # "why did this rebuild" with silence made the two halves of
            # the same question unequal (#494 review).
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
            # The builder emitted no dependency file. That used to mean the
            # include directories were untracked for it, and reusing on "we
            # do not know" is what #478 reported. It is sound now: `sources`
            # above carries a listing of every `+incdir+`/`-y` directory,
            # so the unknown this branch admits is bounded to what the
            # filelist never named — see docs/known-issues.md.
            return True
        if stored.get("deps_format") != _DEPS_FORMAT:
            return self._note_stamp_mismatch(
                "the stamp's dependency list predates declared-path tracking"
            )
        return self._deps_unchanged(test_name, deps, quiet=quiet)

    def pre(self, run_id=_UNSET):
        """Run the test's ``preproc`` hook; return a setup-failure string or None.

        ``run_id`` is the run this single execution of the hook is preparing.
        It defaults to ``self.run_id``, which is right when the hook runs once
        per run — a plain ``test``, or one dispatched element. A caller that
        runs the hook **once for several runs** must pass ``None`` explicitly:
        :meth:`TestRunner.run_multiple` does, because the runner it builds
        carries ``run_ids[0]`` and the hook it invokes serves all of them.
        Defaulting there would tell the hook it was preparing run 1 and hand
        it run 1's directory, which runs 2..N never read.
        """
        script_path = self.test_cfg.get_preproc_path()
        if script_path is None:
            log_event(logger, logging.DEBUG, "preproc.skipped", test=self.test_name)
            return None
        if run_id is _UNSET:
            run_id = self.run_id

        # This run's stale retry transcript goes before the hook runs, not
        # only at compile() (#498 review): a reused run directory whose PRE
        # fails here never reaches compile(), and the fresh SetupFail
        # envelope would be paired with the previous invocation's retry log.
        try:
            Path(self._get_retry_transcript_path()).unlink(missing_ok=True)
        except OSError:
            pass

        with open(script_path, "r") as file:
            code = file.read()

        # `artifact_dir` stays test-keyed for backward compatibility, and
        # `run_artifact_dir` is where a generator whose output depends on the
        # run must write instead (#415). They are the same directory when one
        # hook run serves the whole invocation, so a hook can always use the
        # latter. Both are created here: a hook is handed directories it may
        # write to, not paths it has to mkdir.
        artifact_dir = self._ensure_artifact_dir()
        run_artifact_dir = self._ensure_artifact_dir(run_id=run_id)

        # Pass self.test_cfg to the preproc script as root_cfg
        # preproc script can mutate self.test_cfg, which is used for compile and sim
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
        """Walk up from start_dir to project_root, returning first dir with tests.yaml."""
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
        """Fail loudly if the preproc imported a module from a different suite directory."""
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

    def _record_compile(self, *, duration_sec, reused):
        """Stamp :attr:`last_compile` with this instance's compile outcome.

        The one writer, so every path records the same three keys. Callers
        read it off the sim after COMPILE: the build job folds it into the
        build envelope's ``builds`` list, and the in-process path folds it
        into the run's own result envelope. Best-effort telemetry — nothing
        downstream may fail because a value here is ``None``.
        """
        self.last_compile = {
            "duration_sec": duration_sec,
            "builder": self.rtl_builder_cfg.get_name(),
            "reused": reused,
        }

    def _build_compile_plan(self):
        """Derive this test's :class:`_CompilePlan` — the pre-builder half.

        Writes ``run.f`` as a side effect (the fingerprint stats what the
        filelist names, so it cannot be computed before the file exists) and
        sets ``self._shared_build_dir``, which ``_get_simv_path()`` branches
        on. Deliberately does *not* touch stamps or create the shared dir:
        those are decisions of an actual compile, and a probe that unlinked
        a stamp would destroy the reuse it was asked about.
        """
        rtl_builder_cfg = self.rtl_builder_cfg
        compile_work_dir = self._ensure_artifact_dir()
        # A probe is not a compile, but it is the point at which the builder
        # for this config is settled (a preproc hook can no longer move it).
        # Recording it here is what lets a config that never reaches a
        # builder — a filelist failure, a killed job — still name the
        # builder it would have used, with `reused` left unknown rather
        # than guessed (#495).
        self._record_compile(duration_sec=None, reused=None)

        builder_opts = self._filter_builder_opts(
            rtl_builder_cfg.get_compile_time_opts(self.rtl_builder_mode)
        )
        extra_compile_flags = self._get_extra_compile_flags()
        assertion_flags = self._get_verilator_assertion_flags(builder_opts)
        # After the extra flags, because the subclass that emits its own top
        # flag emits it there and this must see it (#508).
        top_flags = self._get_top_module_flags(builder_opts, extra_compile_flags)
        plusdefines = self._get_plusdefines()
        is_verilator = os.path.basename(rtl_builder_cfg.get_exe()).startswith(
            "verilator"
        )

        # Keep compile outputs in the suite work dir, but pass explicit paths so sim cwd can vary later.
        filelist_path = self._get_filelist_path()
        self._write_filelist(
            filelist_path
        )  # raises FilelistError on bad path; caught by TestRunner

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
            # The group is the OUTPUT the compile writes, canonicalized —
            # that is the single-writer resource the pool must not hand to
            # two workers at once (#369). For verilator/icarus the output lives
            # under the per-test compile dir, so every test is its own group
            # and they may all compile at once. For a family that honours
            # `builder-simv:`, two configs can name one executable — an
            # absolute pin, or a relative spelling whose `..` escapes the
            # per-test workspace (`../../shared/simv` from two tests meets
            # at one file) — and grouping on the compile DIRS would run two
            # builders onto one binary under `compile.parallel > 1`,
            # attributing a compile failure or a simulation to another
            # config's build (#496 review, twice). Grouping on the resolved
            # path serializes them; it does not make them share (each still
            # stamps and rebuilds in its own dir), and serializing is the
            # whole fix. `realpath`, not `normpath(abspath(...))`: two
            # spellings can also meet at one file through a symlinked
            # parent, which textual normalization cannot see — and it keeps
            # the group consistent with the suite dir the head `resolve()`d
            # (macOS's /tmp is itself a symlink). A nonexistent tail is
            # normalized textually, so the output need not exist yet.
            # Resolved here, where `_shared_build_dir` is still unset, so
            # this is always the UNSHARED output; the share-build branch
            # below overrides the group with the shared dir.
            group_dir=os.path.realpath(self._get_simv_path()),
        )
        if not self.share_build:
            return plan

        plan.unsupported_reason = self._share_build_unsupported_reason()
        # One key_cmd for both branches. They agreed already; spelling it
        # twice was an invitation for them to stop agreeing.
        key_cmd = (
            [rtl_builder_cfg.get_exe()]
            + builder_opts
            + extra_compile_flags
            + assertion_flags
            # The top flag changes which modules are elaborated and what the
            # model is called, so two testbenches over one model that differ
            # only in `toplevel:` must not share a build dir (#508). Empty
            # when no `toplevel:` is declared, which is what keeps every
            # existing key of an untouched project unchanged.
            + top_flags
            + plusdefines
        )
        if self._get_simulator_family() == "icarus":
            # The Icarus `simv` wrapper lives IN the shared dir, and it
            # bakes in these args (CocotbSim adds the VPI module to
            # them while contributing no compile flags of its own). Two
            # tests that differ only there would otherwise share a key
            # and a wrapper, and whichever compiled first would decide
            # how vvp is invoked for both. (Icarus never reaches the
            # unsupported branch below — it is a share-build family and is
            # exempt from the absolute-`builder-simv:` refusal — so adding
            # this before the branch changes no key that exists.)
            key_cmd = key_cmd + self._icarus_vvp_extra_args()
        plan.key_cmd = key_cmd
        # Keyed on the configured compile line, NOT on the output flags
        # compile() appends later: those are derived from the resulting key,
        # so including them would be circular.
        plan.fingerprint = self._compile_fingerprint(key_cmd, filelist_path)

        if plan.unsupported_reason is None:
            shared_dir = shared_build_dir(
                self.suite_work_dir, self._compile_config_key(plan.fingerprint)
            )
            plan.shared_dir = shared_dir
            self._shared_build_dir = str(shared_dir)
            plan.build_dir = str(shared_dir)
            plan.group_dir = str(shared_dir)
        else:
            # Emitted from the plan, so since #495 it lands at probe time
            # rather than compile time: it now precedes compile.config for
            # the same test, and a config the caller probes and then drops
            # warns about a compile that never runs. The count per build
            # job is unchanged — the plan is derived once and cached.
            log_event(
                logger,
                logging.WARNING,
                "compile.share_build_unsupported",
                test=self.test_name,
                simulator=self._get_simulator_family(),
                reason=plan.unsupported_reason,
            )
            # The build cannot be *shared*, but it can still be *reused*
            # by the next process to ask for this test — which is what
            # lets a dispatched fan-out compile once in the build job and
            # have its elements short-circuit instead of racing each
            # other into one directory (#369). Same fingerprint, same
            # stamp file; only the scope differs, so the stamp lives in
            # the test's own compile work dir. `group_dir` also stays as it
            # was built above — the resolved output path, which is per-test
            # unless a `builder-simv:` points two tests at one executable.
        return plan

    def _gated_build_verdict(self, fingerprint=None):
        """What the build envelope says about THIS test, as ``(kind, record)``.

        A gated job that reaches its own compile has already failed to
        validate the build's stamp, and the envelope is the only thing that
        can say whether compiling here is a recovery or a catastrophe
        (#498/#535). Three answers:

        ``("failed", record)`` — the builder ran for this config and exited
        non-zero, on the same inputs. Deterministic; see below.

        ``("built", record)`` — the build job recorded this config as
        BUILT. The binary the whole fan-out was gated on exists, so the
        stamp's disagreement is with a build that is there, and the caller
        declines to compile: a recompile would run under the simulation
        reservation, into the directory every sibling is queued on, and the
        memory kill that follows hides whatever really drifted. ``record``
        is ``{}`` for a build job too old to write per-config records — the
        envelope still positively names this config as built, which is the
        load-bearing half.

        ``(None, None)`` — nothing decisive: no build job, no envelope
        path, an unreadable or stale envelope, a config the build job never
        reached (a crash, a cancellation), a test listed as failed with no
        per-build record or with one carrying no ``returncode``, or a
        recorded failure whose inputs have since moved. Those keep today's
        retry, which writes ``compile.retry.log`` and leaves the build
        job's transcript intact.

        The envelope's ``failed`` list is not compile-only: the build job
        also records PRE/setup failures, filelist-probe errors and worker
        exceptions there, and none of those proves the *builder* would fail
        again here — a sim job re-runs its own preproc, so a transient
        setup failure can succeed on this side, and suppressing its retry
        would turn that run into a false CompileFail (#498 review). The one
        deterministic case a retry cannot fix is a builder that genuinely
        ran and exited non-zero, and the per-build record proves it by
        carrying a ``returncode``. That record is the only decisive answer.

        Deterministic, that is, for the *same inputs* (#498 review). This
        job's PRE has re-run and ``fingerprint`` is its own just-derived
        compile fingerprint; when the record also carries the build's
        ``fingerprint_sha`` and the two hash differently — an edited
        source, a regenerated input, a moved toolchain since the build
        failed — the failure may not reproduce, and suppressing the retry
        would report a CompileFail for a compile nobody has run. The
        verdict then falls through to the retry. A record without the sha
        (an older build job) keeps the verdict, exactly as before.

        Best-effort by construction. A sim job that cannot read the
        envelope falls back to today's retry rather than inventing a
        verdict — deciding on a guess would turn a readable file into a
        lost run in one direction and an OOM in the other.
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
        # Compiler evidence or nothing: no record at all, or one without a
        # returncode, describes a failure that never reached a builder.
        if record is None or record.get("returncode") is None:
            return None, None
        recorded_sha = record.get("fingerprint_sha")
        if recorded_sha is not None:
            own_sha = _fingerprint_sha(fingerprint)
            if own_sha is not None and recorded_sha != own_sha:
                # The build failed a *different* compile than the one this
                # job would run: the inputs moved in between, so the retry
                # is earned rather than a repeat.
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
        """Fail a gated job whose build job built this test (#535).

        The build job compiled this config successfully, so the binary the
        whole fan-out was gated on is in ``build_dir``. This job's stamp
        check disagreed with it anyway, and a recompile is the wrong answer
        to that in every direction: it runs under the SIMULATION
        reservation, which is what the scheduler kills for memory (#536);
        every sibling element queues behind it on the same directory
        (#369/#507); and the memory kill that follows replaces whatever
        really drifted with `signal 9` in the summary (#498). Nothing here
        is recoverable by compiling, so the test fails with the reason
        instead — one row to read rather than N red jobs.

        Which reason depends on the two fingerprints. Equal (or unknown,
        from a build job too old to record one) says the two sides describe
        the same compile and the disagreement is in the stamp itself — a
        dependency the builder no longer reports the same way, a
        replaced ``simv``, a stamp that never landed. Different says this
        node's inputs are not the build job's: a ``preproc`` hook that
        generates something different here, or an edit that landed
        mid-run.
        """
        recorded_sha = (record or {}).get("fingerprint_sha")
        own_sha = _fingerprint_sha(fingerprint)
        reason = self.stamp_mismatch_reason or "the build's stamp did not validate"
        inputs_differ = (
            recorded_sha is not None and own_sha is not None and recorded_sha != own_sha
        )
        what = (
            "from different compile inputs than this job derived"
            if inputs_differ
            else "and its stamp still does not validate here"
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
        """The directory this test's compile will write into (#495).

        The probe a dispatched build job groups on: configs sharing a value
        here must compile serially, configs differing may compile at once.
        Raises :class:`FilelistError` exactly as :meth:`compile` does, and
        callers map it the same way — it is the same ``_write_filelist``.
        """
        return self._compile_plan().group_dir

    def _compile_argv(self, plan, *, quiet=False):
        """The builder command line ``plan`` would run.

        Derived here and nowhere else, for the reason ``group_dir`` is: the
        reuse breadcrumb (:meth:`_write_reuse_transcript`) records the
        command that *would* have run, and a second assembly of it would
        drift from the real one the first time somebody touches the VCS
        output strip — leaving a ``compile.log`` that says a build was made
        from flags no builder ever saw.

        ``quiet`` drops the side effects that belong to a real compile: the
        strip's DEBUG record, the assertions line, and the Icarus snapshot
        directory. A reuse must not create directories or claim to have
        enabled anything.
        """
        # Copied: the VCS strip below rewrites these, and the plan is the
        # record of what was decided, not a scratch buffer.
        builder_opts = list(plan.builder_opts)
        extra_compile_flags = list(plan.extra_compile_flags)
        build_dir = plan.build_dir
        family = self._get_simulator_family()
        shared = self._shared_build_dir is not None

        run_cmd = [self.rtl_builder_cfg.get_exe()]
        if shared and family == "vcs":
            # Strip BOTH sources of compile flags, not just the configured
            # opts: a `-o` reaching run_cmd from _get_extra_compile_flags()
            # would be appended after _vcs_shared_output_argv() and so win on
            # VCS's duplicate-option precedence. The simv would land outside
            # the shared dir, the stamp check would never find it, and every
            # job would recompile — silently, and forever. No subclass emits
            # one today; this keeps that from being load-bearing.
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
            # Icarus has no -Mdir equivalent; output a single .vvp snapshot
            # into the build dir (shared or per-test) and let our execute()
            # path wrap it.
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

        # Pin the elaboration root, in the same position it occupies in
        # `key_cmd` so the reuse breadcrumb and the real compile agree.
        run_cmd += plan.top_flags

        # add test plus-defines
        run_cmd += plan.plusdefines

        run_cmd += ["-f", plan.filelist_path]
        return run_cmd

    def _rebuild_forced(self, build_dir, *, shared=True):
        """Does ``--rebuild`` override the stamp on ``build_dir`` right now?

        True at most once per directory per process (invariant: a shared-key
        suite rebuilds its one directory once, not once per test), and only
        when the run asked for it.

        Same field schema as its counterpart ``compile.build_reused`` —
        basename in ``build_dir``, absolute in ``build_path`` — so a
        consumer keying on either across the pair gets one kind of thing.
        """
        if not self.rebuild:
            return False
        if not _claim_rebuild(str(build_dir)):
            return False
        # Console, like the reuse line: between them the two answer "what
        # produced the binary this run simulated?", and a `--rebuild` that
        # reached a dispatched job silently is as hard to trust as a silent
        # reuse. Fires once per build dir per process, so it cannot become
        # chatter.
        log_console_event(
            logger,
            logging.INFO,
            "compile.rebuild_forced",
            test=self.test_name,
            **_build_dir_fields(build_dir, shared=shared),
        )
        return True

    def _read_build_stamp(self, stamp_dir):
        """The stamp in ``stamp_dir`` as a dict, or ``None``.

        Never raises. Its callers are recording telemetry or deciding to
        fall back to a compile, and neither may be what fails a run.
        """
        try:
            stored = json.loads((Path(stamp_dir) / SHARED_BUILD_STAMP_NAME).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return stored if isinstance(stored, dict) else None

    def _record_build_stamp(self, stamp_dir):
        """Record which binary this run's stamp vouched for (#535).

        ``fingerprint_sha`` over the stamp's input half — the same digest
        :func:`_fingerprint_sha` takes of a live fingerprint, the stamp
        being that dict plus ``deps``/``simv`` — beside the ``simv`` entry
        it validated. The pair says "this key, that binary", which is what
        the head compares across the runs of one key at collect: they all
        validated one stamp, so a run naming a different binary reused
        something nobody else did.
        """
        stored = self._read_build_stamp(stamp_dir)
        if stored is None:
            self.last_build_stamp = None
            return
        inputs = {key: value for key, value in stored.items() if key not in _STAMP_META}
        self.last_build_stamp = {
            "fingerprint_sha": _fingerprint_sha(inputs),
            "simv": stored.get("simv"),
        }

    def adopt_group_build(self):
        """Take the build a same-key sibling just made, or say why not (#535).

        A build job groups its configs by the directory their compile will
        WRITE, so a group's members share a compile key by construction:
        one ``key_cmd``, one set of ``run.f`` lines, one resolved builder.
        The only thing that can differ between the leader's stamp and this
        member's fingerprint is a file that moved *during the job* — and on
        a cold tree the serial PRE phase guarantees one does: this member's
        own ``preproc`` output, created after the leader was fingerprinted,
        under an ``+incdir+`` the stamp lists by name. Recompiling the key
        for that is what #535 reported.

        So the question here is narrower than the stamp's. Did any input
        the leader's build actually CONSUMED change? That is the stamp's
        ``deps``, and a "yes" is not a stale build: it is two tests on one
        compile key compiling different bytes, whose other outcome under
        ``--share-build`` is one test silently simulating the other's
        binary. Nothing is served by recompiling that.

        Returns ``("adopted", None)``, ``("drift", <path>)``, or
        ``(None, None)`` for "not decidable here" — no shared build, no
        stamp, a compile line that somehow differs, or a builder that
        reports no dependencies. VCS and Icarus are that last case: with no
        dependency file nothing separates a consumed input from a
        bystander, so the leader's own full comparison decides it and this
        member takes the pre-#535 path. ``(None, None)`` IS that path — the
        caller compiles, and a valid stamp still short-circuits it.
        """
        plan = self._compile_plan()
        fingerprint = plan.fingerprint
        if plan.shared_dir is None or not isinstance(fingerprint, dict):
            return None, None
        stored = self._read_build_stamp(plan.shared_dir)
        if stored is None or not isinstance(stored.get("deps"), list):
            return None, None
        simv_path = self._get_simv_path()
        if not Path(simv_path).is_file() or stored.get("simv") != _stat_entry(
            simv_path
        ):
            return None, None
        # Everything but the tracked inputs, compared exactly. The compile
        # key already fixes the command and the toolchain's identity, so
        # this only catches a toolchain replaced under a running job —
        # cheap, and the one input a group's members do not share by
        # construction.
        skipped = _STAMP_META | {"sources"}
        if {key: value for key, value in stored.items() if key not in skipped} != {
            key: value for key, value in fingerprint.items() if key != "sources"
        }:
            return None, None
        for entry in stored["deps"]:
            if not isinstance(entry, list) or len(entry) != 4:
                return None, None
            if not isinstance(entry[0], str):
                # `os.stat` takes a file *descriptor* for an int.
                return None, None
            if not _entry_matches(entry, self._tracked_entry(entry[0], resolved=True)):
                return "drift", self._note_group_input_drift(entry[0])
        # Consumed like a compile: this instance has had its one build.
        self._compile_plan_cache = None
        self._report_build_reused(plan, stamp_dir=plan.shared_dir)
        self._record_compile(duration_sec=0.0, reused=True)
        return "adopted", None

    def _note_group_input_drift(self, dependency):
        """Record the drift verdict as a compile failure; return ``dependency``.

        The build job reports this config failed, and the record it writes
        has to be decisive on the sim side too: a ``returncode`` is what
        stops the gated sim job from recompiling the key into the shared
        directory (#498), which is the same clobber read from the other
        end. No transcript, because no builder ran — the one line below is
        the whole story, and it travels in the envelope's ``error_tail``.
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
        """Say — on the console, and in the test's ``compile.log`` — that
        this compile was skipped (#494).

        A stale reuse used to be deducible only from an *absent*
        ``compile.log``, which reads as "nothing to do". Both records name
        the directory and how old its stamp is, so the run that reuses a
        build made before an edit says so where the reader is already
        looking.
        """
        fingerprint = plan.fingerprint
        toolchain = (
            fingerprint["toolchain"]["version"] or fingerprint["toolchain"]["exe"]
        )
        stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
        try:
            stamp_mtime = stamp_path.stat().st_mtime
        except OSError:
            # The stamp validated a moment ago, so this is a vanishing race
            # rather than a state; report the reuse without an age instead
            # of failing the compile over telemetry.
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
        # Console, not just the log file: the console handler sits at
        # WARNING, so a dispatched run's reuse would otherwise be invisible
        # in exactly the transcript that has to show it (#435 pattern).
        # Once per build dir per process on the console — a local regression
        # reusing one build across N tests says so once, not N times; every
        # reuse still lands in the file log (#494 review).
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

        Same path a real transcript takes, and the same per-test,
        per-attempt overwrite semantics: the question it answers is "what
        produced the binary this run simulated", and for a reuse the honest
        answer is a build made elsewhere, at a stated time, from the command
        printed here. Best-effort — a reuse must not fail because its
        breadcrumb could not be written.

        A transcript a *compile* left here is kept below the breadcrumb
        rather than dropped: under dispatch this path is written by the
        build job's compile and then by every gated element's reuse, and
        that first write is the run's only file-level record of, say, a VCS
        ``-licqueue`` wait. Exactly one transcript is carried — a later
        reuse takes over the one the breadcrumb it replaces was holding
        rather than nesting inside it — so the file cannot grow element
        over element.

        Written temp-then-:func:`os.replace`, because a ``run_id`` fan-out
        points N array elements at this one path at once and a truncating
        write would let a reader see a half-file (#363's hazard class).
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

        A breadcrumb is not compile output, so reusing over one keeps what
        that breadcrumb was itself carrying rather than nesting breadcrumbs:
        N reuses of one build preserve exactly one transcript.

        ``errors="replace"``, and ``ValueError`` caught beside ``OSError``:
        a real transcript carries raw simulator output that owes nobody
        valid UTF-8, and a breadcrumb helper must degrade — never raise —
        on the exit-0 path (the same contract as the write side).
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
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(text)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def compile(self):
        rtl_builder_cfg = self.rtl_builder_cfg
        # One compile, one verdict: a second compile() on this instance must
        # not inherit the first's failure record, desc, or transcript name.
        self.last_compile_failure = None
        self.compile_fail_desc = None
        self._compile_transcript_override = None
        # A retry transcript describes exactly one run's retry. Left behind,
        # `rb graph results` would keep advertising it as this run's (#498
        # review) — remove it up front so it exists only when this compile's
        # own gated retry writes it. THIS run's own, never a sibling's: the
        # retry log is run-scoped (#498 review round 6), and unlinking at
        # test scope destroyed the one diagnostic a sibling run's failed
        # retry had left. The same discipline as the stale stamp unlink;
        # best-effort like every artefact-dir touch.
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
        # One plan serves one compile. A second compile() on this instance
        # must re-stat its inputs — that is how an edited source invalidates
        # the stamp — so the cache is consumed here rather than kept.
        self._compile_plan_cache = None

        if plan.shared_dir is None:
            # Unshared builds stay unlocked: within a dispatched run #369
            # already gives each per-test build directory one writer. An
            # absolute `builder-simv:` pinning two *processes* to one
            # executable is pre-existing exposure, out of this scope.
            return self._compile_with_plan(plan)
        # The reuse fast path takes NO lock. A dispatched suite's gated sim
        # elements all call compile() against one already-valid shared
        # build; serialising those on a cross-node flock would put N stamp
        # validations (each content-hashing every tracked input, on a node
        # with a cold page cache) on the critical path one after another,
        # and would leave a reuser hostage to any compile that happened to
        # hold the lock — a VCS licence queue, say. The lock buys the reuse
        # path nothing durable anyway: it is released before execute() runs
        # the simulation, so a reuser is exposed to a later concurrent
        # relink either way.
        #
        # `--rebuild` is not decided here: `_rebuild_forced` CLAIMS the one
        # rebuild this process owes the directory, and that claim belongs
        # next to the compile it forces, inside the lock.
        if not self.rebuild and self._reuse_shared_build(plan, quiet=True):
            return 0
        # Before the lock, because the lock file lives inside the directory
        # it guards. (A reuse would find it there anyway; only a shared dir
        # that never gets compiled into is newly created here.)
        plan.shared_dir.mkdir(parents=True, exist_ok=True)
        # Cross-process single writer (#494): several `rb` processes that
        # start together against a cold shared tree would otherwise all
        # compile into it at once. The stamp check lives INSIDE the lock, so
        # a waiter re-decides after acquiring and reuses the build it waited
        # for rather than repeating it. Lock ordering is in build_dir_lock:
        # one build lock per thread, and never taken around the tree lock.
        with build_dir_lock(plan.shared_dir, test=self.test_name):
            return self._compile_with_plan(plan)

    def _reuse_shared_build(self, plan, *, quiet=False):
        """Reuse the stamped build in ``plan.shared_dir`` if it validates.

        One place, because the question is asked twice: unlocked in
        :meth:`compile` (the fast path — a warm build nobody is compiling
        is the common case) and again inside the build lock, where it is
        the second half of the double check. ``quiet`` is for the first,
        advisory ask: the in-lock repeat owns the "why this stamp lost"
        diagnostics, so a rebuild explains itself once rather than twice.
        """
        if not self._shared_build_is_valid(
            plan.shared_dir, plan.fingerprint, test_name=self.test_name, quiet=quiet
        ):
            return False
        self._report_build_reused(plan, stamp_dir=plan.shared_dir)
        # 0.0, not the stamp-check time: the number is read as "what this
        # build cost", and a reuse cost no build. The stat cost is real
        # but sub-millisecond and would only invite someone to sum it
        # against a compile.
        self._record_compile(duration_sec=0.0, reused=True)
        return True

    def _compile_with_plan(self, plan):
        """Check the stamp, compile if it does not validate, stamp the result.

        Split from :meth:`compile` so the shared-build case can hold
        :func:`build_dir_lock` across the whole sequence — check, compile
        and stamp are one critical section, and a lock released between
        them would let a second process see the invalidated stamp and
        start its own compile into the same directory.

        The stamp is validated against ``plan.fingerprint``, which
        :meth:`_compile_plan` computed BEFORE any wait on the lock. So the
        comparison is "as of when this compile was planned", not as of
        acquisition: a source edited while this process queued behind
        another compile is judged by its pre-wait hash and gets caught on
        the next run instead. The window is the pre-existing one — a
        fingerprint has always been taken before the compile it describes
        — widened from ~0 to the length of somebody else's compile.
        """
        rtl_builder_cfg = self.rtl_builder_cfg
        compile_work_dir = plan.compile_work_dir
        build_dir = plan.build_dir
        fingerprint = plan.fingerprint

        if self.share_build:
            if plan.unsupported_reason is None:
                # Claimed before the check, so `--rebuild` decides it rather
                # than the stamp — and claimed only once per directory, so
                # the next test with this key reuses what this one builds.
                forced = self._rebuild_forced(plan.shared_dir)
                if not forced and self._reuse_shared_build(plan):
                    return 0
                # (The directory itself was created by compile(), which
                # needed it to put the build lock in.)
                # A crashed/killed compile must never leave a stamp that
                # validates a broken simv.
                (plan.shared_dir / SHARED_BUILD_STAMP_NAME).unlink(missing_ok=True)
                # A shared build owns the output location, so a *relative*
                # builder-simv: is discarded rather than honoured (the absolute
                # case declines sharing outright). Say which value went
                # unused instead of leaving it to be inferred from the path.
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
                (Path(compile_work_dir) / SHARED_BUILD_STAMP_NAME).unlink(
                    missing_ok=True
                )

        run_cmd = self._compile_argv(plan)
        run_str = " ".join(run_cmd)
        if self.expect_prebuilt:
            # This job was ordered after a build job precisely so it would not
            # have to compile. Reaching here means that build's stamp did not
            # validate — and only the build envelope can say whether
            # compiling now is a recovery or a catastrophe (#498/#535).
            verdict, build_record = self._gated_build_verdict(fingerprint)
            if verdict == "built":
                return self._decline_gated_recompile(
                    build_record, fingerprint, build_dir
                )
            if verdict == "failed":
                build_failure = build_record
                # The build job's compile for THIS test exited non-zero. That
                # is deterministic: the same sources, the same flags and the
                # same toolchain will fail again here, only now under the sim
                # reservation — which is how a 40-character lint error became
                # `%Error: Verilator threw signal 9` written over the build's
                # own `compile.log`, and three rounds of raising compile
                # memory chasing it. Fail immediately, carrying the build's
                # verdict, and leave that transcript exactly as it is.
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
                # A non-zero status is the contract with _compile_outcome;
                # the build's own is used so the two records agree.
                # _gated_build_verdict guarantees a returncode is present,
                # so the guard only keeps a malformed record (0, or a
                # non-int from a hand-edited envelope) from turning this
                # failure into a success.
                return returncode if isinstance(returncode, int) and returncode else 1
            # The stamp is merely absent or stale — a toolchain moved, a
            # clock skewed, the build job never got to this config. The
            # retry is right, but the dependency only *orders* the elements
            # (it does not exclude them), so every sibling element is about
            # to do the same thing into the same directory. That is #369
            # resurrected, and this is the one line that says so; a
            # `Compile failed` further down otherwise looks like a design
            # error.
            log_event(
                logger,
                logging.WARNING,
                "compile.prebuilt_stamp_invalid",
                test=self.test_name,
                run_id=self.run_id,
                build_dir=build_dir,
                # WHAT drifted, not just that something did. The check's own
                # diagnostics are DEBUG and a dispatched job logs at INFO,
                # so without this the reader of the job that recompiled (or
                # was OOM-killed doing it) has nothing to act on (#536).
                reason=self.stamp_mismatch_reason,
            )
            # Beside the build's transcript, never over it: whatever this
            # retry hits is the *sim job's* story, told under the sim job's
            # reservation, and the build's compile.log is the only record of
            # what the build job saw. In the RUN's own directory (#498
            # review round 6): the retry is one run's recompile, and
            # sibling runs share the test dir. `_ensure_artifact_dir`, not
            # just the path — a failing retry must find the dir to write
            # its transcript into.
            self._ensure_artifact_dir(run_id=self.run_id)
            self._compile_transcript_override = self._get_retry_transcript_path()
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
        # Recorded before the pass/fail branch: a compile that failed after
        # 14 minutes is exactly the number a build-job reservation is sized
        # against, and dropping it would leave the slowest builds invisible.
        self._record_compile(duration_sec=round(e_time - s_time, 2), reused=False)
        license_queued = self._compile_queued_for_license(result)
        # Unconditional since #494 (see _write_compile_transcript): a reuse
        # writes this file, so a compile that ran has to as well, or the
        # file's presence would read as "nothing compiled".
        #
        # Whichever file that is — `compile.log`, or the `compile.retry.log`
        # a gated retry writes so it does not truncate the build job's
        # (#498). Every consumer that points a reader at "the transcript"
        # reads the path off the events below, never off a name of its own.
        transcript_path = self._write_compile_transcript(run_str, result)
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
            # What a dispatched build job records in its envelope for this
            # config (#498): the status its own sim jobs would otherwise
            # have to rediscover, and the file that says why.
            self.last_compile_failure = {
                "returncode": result.returncode,
                "transcript": transcript_path,
            }
            failed_sha = _fingerprint_sha(fingerprint)
            if failed_sha is not None:
                # WHICH compile failed, not just that one did: a gated sim
                # job compares this against its own fingerprint's sha, and
                # honours the no-retry verdict only when they match (#498
                # review). Recorded from the same `fingerprint` the stamp
                # would have been written from, via the same helper the
                # sim side hashes with.
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
                # Keep the evidence: on a dispatched build this is the only
                # record that the wall-clock went to the license server, and
                # the next run may be the one Slurm kills at --time.
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
            if fingerprint is not None:
                # A shared build owns its directory and stamps it; an
                # unshared one has no directory of its own (`build_dir` is a
                # bare relative name the builder interprets), so its stamp
                # goes beside the rest of the test's compile outputs.
                stamp_dir = self._shared_build_dir or compile_work_dir
                # Recorded from the finished build, not predicted from the
                # filelist: the builder is the only thing that knows which
                # headers it actually opened (#303). Read from the *build
                # output* dir, which is the stamp dir only in the shared
                # case — unshared, the builder's outputs are under
                # `compile_work_dir / build_dir` while the stamp sits beside
                # them in `compile_work_dir`.
                deps = self._collect_build_deps(build_dir, compile_work_dir)
                stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
                stamp_path.write_text(
                    json.dumps(
                        # The executable is stamped too, so a reuse check can
                        # tell "these inputs" from "this binary" (#369).
                        {
                            **fingerprint,
                            "deps": deps,
                            "deps_format": _DEPS_FORMAT,
                            "simv": _stat_entry(self._get_simv_path()),
                        },
                        sort_keys=True,
                    )
                )
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.build_stamp_written",
                    test=self.test_name,
                    stamp=str(stamp_path),
                    # None, not "none": machine mode serialises these as JSON
                    # Lines, and a field whose type varies by path forces
                    # every consumer to type-check before comparing. `null`
                    # is also how the stamp itself spells the same thing.
                    tracked_deps=None if deps is None else len(deps),
                )
                self._record_build_stamp(stamp_dir)
        return result.returncode

    def execute(
        self, run_id=None, seed_mode: SeedMode = SeedMode.DEFAULT, replay_run_id=None
    ):
        """
        Run vlog simulation executable.

        run_id controls run-indexed output naming. seed_mode controls how the seed is
        selected:
          - "default": use builder-config seed
          - "new": generate a fresh random seed
          - "replay": read seed from a previous run's .randseed file
        """
        run_id = self.run_id if run_id is None else run_id
        replay_run_id = self.replay_run_id if replay_run_id is None else replay_run_id
        artifact_dir = self._ensure_artifact_dir(run_id=run_id)
        log_path = self._get_log_path(run_id=run_id)
        err_path = self._get_err_path(run_id=run_id)
        randseed_path = self._get_randseed_path(run_id=run_id)

        run_cmd = [self._get_simv_path()]

        if seed_mode == SeedMode.REPLAY:
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

        # add test plus-defines
        run_cmd += self.rtl_builder_cfg.get_run_time_opts(
            self.rtl_builder_mode, seed=seed
        )

        run_cmd += self._get_plusdefines()

        # add test runtime args
        run_cmd += self._get_plusargs()

        if self._coverage_enabled() and self._get_simulator_family() == "verilator":
            run_cmd += [
                f"+verilator+coverage+file+{self._get_cov_abspath(run_id=run_id)}"
            ]

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
        # Added rather than substituted, so per-test sim_timeout values keep
        # their meaning and only the builder-specific allowance moves. The
        # ``is not None`` guard is unreachable while default_timeout is 60, and
        # is kept so an allowance can never manufacture a timeout for a caller
        # that deliberately had none.
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
                # WARNING (not INFO) so the pause/resume pair is visible at
                # default console verbosity.
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

        # subprocess pipe stderr to test.err, stdout to test.log
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

        # Latest-run convenience links: a passing test must never fail over
        # one (a suite dir removed mid-run, ENOSPC, read-only/EXDEV mount).
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

    def post(self, run_id=None):
        """
        post-process vlog test output to determine test results
        return TestResult
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

        # default post-processing (VlogPost)
        else:
            self.vlog_post = VlogPost(
                name=self.test_name,
                path=log_path,
                err_path=err_path,
                assertions_enabled=assertions_enabled,
            )
        results = self.vlog_post.get_results()
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
