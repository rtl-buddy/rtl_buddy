# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
vlog_sim module handles verilog simulations for rtl-buddy

"""

import contextlib
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

logger = logging.getLogger(__name__)
from ..hooks import exec_hook_script
from ..seed_mode import SeedMode

from .vlog_filelist import VlogFilelist
from .vlog_post import VlogPost
from .vlog_post import UvmVlogPost
from .vlog_cov import VlogCov
from .artifact_paths import shared_build_dir, test_artifact_dir, test_build_dir_name

import time
import pprint
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event, task_status
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
SHARED_BUILD_STAMP_NAME = "rb-compile-stamp.json"

# Simulator families whose compile output rtl_buddy can redirect wholesale
# into a shared build dir, and whose simv still runs from there once other
# tests point at it. Everything else compiles inside each test's own
# artefact dir (correct, just unshared).
SHARE_BUILD_FAMILIES = frozenset({"verilator", "vcs", "icarus"})

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


def _log_stale_stamp_toolchain(stored_inputs, fingerprint, *, test_name=None):
    """Say so when a rebuild is the toolchain's doing, not the RTL's.

    A recompile after a source edit explains itself. A recompile because
    the simulator moved underneath a build that was being reused does not,
    and reading it off a diff of two JSON stamps is not a thing anyone
    should have to do — this is the case that used to be missed entirely.
    Silent on a stamp predating the toolchain entry: that is an rtl_buddy
    upgrade, not a toolchain change, and it happens exactly once.
    """
    if "toolchain" not in stored_inputs:
        return
    was = stored_inputs.get("toolchain")
    # A caller may hand in no fingerprint at all to assert a stamp is stale;
    # that is not a toolchain change either.
    now = (fingerprint or {}).get("toolchain") or {}
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
# line — enough for the fingerprint to notice when the defines change.
_FILELIST_OPTION_RE = re.compile(r"^(?:\+(?:incdir|libext|define)\+|-[vyF]\s+)?(.*)$")

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


def _hash_file_content(path: str, size: int, mtime_ns: int) -> str | None:
    """``sha256`` hexdigest[:16] of ``path``'s bytes, or None if unreadable.

    Memoised on ``(path, size, mtime_ns)``: within one process a file whose
    stats have not moved has not been rewritten, and re-reading it per
    validated stamp is the cost this whole check has to stay under.
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


def _content_sha(path: str, stat: os.stat_result, project_root: str | None):
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

    **Policy: nothing outside the project root is hashed** (brief invariant
    4). Verilator's dependency file names the toolchain's own std includes
    and, for some installs, ``verilator_bin`` itself; hashing tens of
    megabytes of unchanging install per validation is not a trade worth
    making, and the toolchain fingerprint's version probe already catches an
    install swapped underneath a build. Those entries stay stat-only.
    """
    if not project_root:
        return None
    resolved = os.path.realpath(path)
    try:
        if os.path.commonpath([resolved, project_root]) != project_root:
            return None
    except ValueError:
        # Different drives on Windows: not under the root by definition.
        return None
    # Hash under the realpath so two spellings of one file (the filelist
    # normpaths, the dependency file realpaths) share a memo entry.
    return _hash_file_content(resolved, stat.st_size, stat.st_mtime_ns)


def _hashed_stat_entry(path: str, *, project_root: str | None) -> list:
    """``[path, size, mtime_ns, sha]`` for a tracked *input*.

    ``sha`` is :func:`_content_sha` — a short content hash for files under
    the project root, None for anything else (and for an existing file that
    cannot be read, which :func:`_entry_matches` then treats as changed). A
    vanished file records as ``[path, None, None, None]`` rather than being
    dropped, so its later reappearance still invalidates the stamp.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [path, None, None, None]
    return [
        path,
        stat.st_size,
        stat.st_mtime_ns,
        _content_sha(path, stat, project_root),
    ]


def _entry_matches(stored, current: list) -> bool:
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

    Anything that is not an entry of this version's shape — a 3-element
    entry from a stamp written before #494, say — is "we do not know", and
    the only honest reading of that is one rebuild.
    """
    if not isinstance(stored, list) or len(stored) != len(current):
        return False
    if stored[0] != current[0]:
        return False
    stored_sha, current_sha = stored[-1], current[-1]
    if stored_sha is not None and current_sha is not None:
        # A size mismatch under equal content hashes cannot happen for a
        # real file, so there is nothing else worth asking.
        return stored_sha == current_sha
    return stored == current


def _entry_lists_match(stored, current) -> bool:
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
        _entry_matches(stored_entry, current_entry)
        for stored_entry, current_entry in zip(stored, current)
    )


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
        # Opt-in: key the build dir on a hash of the compile inputs so tests
        # with identical inputs share one simv (#293). The resolved shared
        # dir is only known once compile() has written the filelist.
        self.share_build = share_build
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
        self._project_root = os.path.realpath(
            project_root
            if isinstance(project_root, str) and project_root
            else self.suite_work_dir
        )

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
            return str(Path(self._shared_build_dir) / "simv")
        rtl_builder_exe = self.rtl_builder_cfg.get_exe()
        if os.path.basename(rtl_builder_exe).startswith("verilator"):
            return str(
                Path(self._get_compile_work_dir()) / self._get_build_dir() / "simv"
            )
        if self._get_simulator_family() == "icarus":
            return str(Path(self._get_compile_work_dir()) / "simv")
        simv_path = self.rtl_builder_cfg.get_simv()
        if os.path.isabs(simv_path):
            return simv_path
        return str(Path(self._get_compile_work_dir()) / simv_path)

    def _get_icarus_snapshot_path(self):
        """Path to the .vvp snapshot produced by iverilog."""
        if self._shared_build_dir is not None:
            return str(Path(self._shared_build_dir) / "simv.vvp")
        return str(
            Path(self._get_compile_work_dir()) / self._get_build_dir() / "simv.vvp"
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
        return str(Path(self._get_compile_work_dir()) / "compile.log")

    def _get_filelist_path(self):
        return str(Path(self._get_compile_work_dir()) / "run.f")

    def _get_log_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / "test.log")

    def _get_err_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / "test.err")

    def _get_randseed_path(self, run_id=None):
        return str(Path(self._get_artifact_dir(run_id=run_id)) / "test.randseed")

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
        return str(Path(self._get_artifact_dir(run_id=run_id)) / "coverage.dat")

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

    def _hashed_stat_entry(self, path):
        """:func:`_hashed_stat_entry` under this instance's hashing policy."""
        return _hashed_stat_entry(path, project_root=self._project_root)

    def _fingerprint_filelist_sources(self, filelist_path):
        """Per-entry (line, size, mtime_ns, sha) stamps for the generated run.f.

        The content hash is what makes an edit invalidate the stamp on a
        cluster, where a cached NFS ``stat`` can still describe the file as
        it was before the edit (#494) — see :func:`_content_sha`. It goes in
        the *fingerprint*, never in the key: :meth:`_compile_config_key`
        reads ``entry[0]`` only, so an edit still rebuilds in place instead
        of stranding a new obj_dir per edit.

        Entries that don't resolve to a plain file (+incdir+/-y directories,
        +libext+ suffixes) keep only their raw line; changes inside include
        directories are not tracked.

        Quoted entries (emitted for paths containing whitespace) are unquoted
        here with ``shlex`` before stat'ing. This unquoting is independent of
        the builder's own ``-f`` parser: Verilator's quote handling was
        validated, other builders' were not — but a bare path with whitespace
        was already broken for every builder, so quoting only appears where
        nothing worked before. The raw (quoted) line is what goes into the
        stamp, matching what ``run.f`` actually contains.
        """
        base = os.path.dirname(os.path.abspath(filelist_path))
        stamps = []
        with open(filelist_path) as filelist_fp:
            for raw_line in filelist_fp:
                line = raw_line.strip()
                if not line or line.startswith("//"):
                    continue
                option_match = _FILELIST_OPTION_RE.match(line)
                entry_path = option_match.group(1) if option_match else line
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
                if os.path.isfile(resolved):
                    # The raw line, not the resolved path, stays entry[0]:
                    # it is what run.f contains and what the compile key
                    # hashes.
                    stamps.append([line] + self._hashed_stat_entry(resolved)[1:])
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
        """Persist the compile command and its captured output; return the path."""
        transcript_path = self._get_compile_transcript_path()
        with open(transcript_path, "w") as transcript_fp:
            transcript_fp.write(f"Command: {run_str}\n\n")
            transcript_fp.write("=== stderr ===\n")
            transcript_fp.write(result.stderr or "")
            transcript_fp.write("\n=== stdout ===\n")
            transcript_fp.write(result.stdout or "")
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
        validates the stamp from its own directory. ``realpath``, not
        ``normpath`` as in :meth:`_fingerprint_filelist_sources`, because
        resolving symlinks on *both* sides is what makes the ``run.f``
        exclusion below actually match; the two lists therefore canonicalise
        differently and are not comparable to each other. The compile's own
        ``run.f`` is excluded — it is regenerated on every compile, so its
        mtime would invalidate the stamp for the very test that built it,
        and its *contents* are already fingerprinted entry by entry.
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
                resolved = os.path.realpath(os.path.join(compile_cwd, prerequisite))
                if resolved != filelist_path:
                    seen.setdefault(resolved, None)
        return [self._hashed_stat_entry(path) for path in sorted(seen)]

    def _deps_unchanged(self, test_name, deps):
        """Have any of the stamp's recorded inputs changed on disk?

        Entry-wise through :func:`_entry_matches`, so a dependency inside
        the project root is decided by its content and one outside it (a
        toolchain header) by its stats — and a stamp written before #494,
        whose entries are 3 elements long, fails closed into one rebuild.
        """
        for entry in deps:
            if not isinstance(entry, list) or len(entry) != 4:
                return False  # not a stamp this version wrote
            if not isinstance(entry[0], str):
                # `os.stat` takes a file *descriptor* for an int, so a
                # corrupt stamp must never reach it.
                return False
            if not _entry_matches(entry, self._hashed_stat_entry(entry[0])):
                # The one question worth answering when a warm run
                # unexpectedly recompiles.
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.build_dep_changed",
                    test=test_name,
                    dependency=entry[0],
                )
                return False
        return True

    def _shared_build_is_valid(self, build_dir, fingerprint, *, test_name=None):
        return self._build_stamp_is_valid(
            build_dir, Path(build_dir) / "simv", fingerprint, test_name=test_name
        )

    def _build_stamp_is_valid(
        self, stamp_dir, simv_path, fingerprint, *, test_name=None
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
        """
        simv_path = Path(simv_path)
        stamp_path = Path(stamp_dir) / SHARED_BUILD_STAMP_NAME
        if not simv_path.is_file() or not stamp_path.is_file():
            return False
        try:
            stored = json.loads(stamp_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(stored, dict) or "deps" not in stored:
            # Written before dependency tracking existed. Its silence about
            # headers is indistinguishable from having had none, so the only
            # honest reading is one rebuild — after which the stamp says
            # which it is.
            return False
        # The executable is an *output*, so the input fingerprint says
        # nothing about it. That was harmless while the output always lived
        # in a directory named after those inputs, and stops being harmless
        # here: an absolute `builder-simv:` is one path shared by every test
        # using that builder, while the stamp is per test. Without this,
        # test_a's stamp keeps validating after test_b overwrote the binary
        # they both point at, and test_a silently simulates test_b's build
        # (#369).
        if stored.get("simv") != _stat_entry(str(simv_path)):
            log_event(
                logger,
                logging.DEBUG,
                "compile.build_dep_changed",
                test=test_name,
                dependency=str(simv_path),
            )
            return False
        if not isinstance(fingerprint, dict):
            # A caller asserting a stamp is stale hands in no fingerprint;
            # nothing can match one.
            return False
        stored_inputs = {
            key: value for key, value in stored.items() if key not in ("deps", "simv")
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
            _log_stale_stamp_toolchain(stored_inputs, fingerprint, test_name=test_name)
            return False
        if not _entry_lists_match(stored_sources, current_sources):
            return False
        deps = stored["deps"]
        if deps is None:
            # The builder emitted no dependency file, so include-dir
            # contents stay untracked for it (docs/known-issues.md).
            return True
        return self._deps_unchanged(test_name, deps)

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

    def compile(self):
        rtl_builder_cfg = self.rtl_builder_cfg
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

        compile_work_dir = plan.compile_work_dir
        # Copied: the VCS strip below rewrites these, and the plan is the
        # record of what was decided, not a scratch buffer.
        builder_opts = list(plan.builder_opts)
        extra_compile_flags = list(plan.extra_compile_flags)
        assertion_flags = plan.assertion_flags
        plusdefines = plan.plusdefines
        is_verilator = plan.is_verilator
        filelist_path = plan.filelist_path
        build_dir = plan.build_dir
        fingerprint = plan.fingerprint

        if self.share_build:
            if plan.unsupported_reason is None:
                if self._shared_build_is_valid(
                    plan.shared_dir, fingerprint, test_name=self.test_name
                ):
                    log_event(
                        logger,
                        logging.INFO,
                        "compile.build_reused",
                        test=self.test_name,
                        build_dir=build_dir,
                        toolchain=fingerprint["toolchain"]["version"]
                        or fingerprint["toolchain"]["exe"],
                    )
                    # 0.0, not the stamp-check time: the number is read as
                    # "what this build cost", and a reuse cost no build.
                    # The stat cost is real but sub-millisecond and would
                    # only invite someone to sum it against a compile.
                    self._record_compile(duration_sec=0.0, reused=True)
                    return 0
                plan.shared_dir.mkdir(parents=True, exist_ok=True)
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
                if self._build_stamp_is_valid(
                    compile_work_dir,
                    self._get_simv_path(),
                    fingerprint,
                    test_name=self.test_name,
                ):
                    log_event(
                        logger,
                        logging.INFO,
                        "compile.build_reused",
                        test=self.test_name,
                        build_dir=compile_work_dir,
                        shared=False,
                        toolchain=fingerprint["toolchain"]["version"]
                        or fingerprint["toolchain"]["exe"],
                    )
                    self._record_compile(duration_sec=0.0, reused=True)
                    return 0
                (Path(compile_work_dir) / SHARED_BUILD_STAMP_NAME).unlink(
                    missing_ok=True
                )

        shared = self._shared_build_dir is not None
        run_cmd = [rtl_builder_cfg.get_exe()]
        if shared and self._get_simulator_family() == "vcs":
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
            if dropped_opts:
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.share_build_opts_overridden",
                    test=self.test_name,
                    dropped=dropped_opts,
                    build_dir=build_dir,
                )
        run_cmd += builder_opts

        if is_verilator:
            run_cmd += ["--Mdir", build_dir]
        elif self._get_simulator_family() == "icarus":
            # Icarus has no -Mdir equivalent; output a single .vvp snapshot
            # into the build dir (shared or per-test) and let our execute()
            # path wrap it.
            Path(self._get_icarus_snapshot_path()).parent.mkdir(
                parents=True, exist_ok=True
            )
            run_cmd += ["-o", self._get_icarus_snapshot_path()]
        elif shared and self._get_simulator_family() == "vcs":
            run_cmd += self._vcs_shared_output_argv(build_dir)

        run_cmd += extra_compile_flags

        if assertion_flags:
            run_cmd += assertion_flags
            log_event(
                logger,
                logging.INFO,
                "compile.assertions_enabled",
                test=self.test_name,
                flags=assertion_flags,
            )

        # add test plus-defines
        run_cmd += plusdefines

        run_cmd += ["-f", filelist_path]
        run_str = " ".join(run_cmd)
        if self.expect_prebuilt:
            # This job was ordered after a build job precisely so it would not
            # have to compile. Reaching here means that build's stamp did not
            # validate, and the dependency only *orders* the elements — it does
            # not exclude them — so every sibling element is about to do the
            # same thing into the same directory. That is #369 resurrected, and
            # this is the one line that says so; a `Compile failed` further
            # down otherwise looks like a design error.
            log_event(
                logger,
                logging.WARNING,
                "compile.prebuilt_stamp_invalid",
                test=self.test_name,
                run_id=self.run_id,
                build_dir=build_dir,
            )
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
        if result.returncode != 0:
            transcript_path = self._write_compile_transcript(run_str, result)
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
                    transcript=self._write_compile_transcript(run_str, result),
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
