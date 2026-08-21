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
import types
import uuid

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
    if family not in ("verilator", "icarus") and os.path.isabs(builder_cfg.get_simv()):
        return (
            f"builder-simv is an absolute path "
            f"({builder_cfg.get_simv()}), which pins the "
            "executable outside the shared build dir"
        )
    return None


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
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [path, None, None]
    return [path, stat.st_size, stat.st_mtime_ns]


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

    def _fingerprint_filelist_sources(self, filelist_path):
        """Per-entry (line, size, mtime_ns) stamps for the generated run.f.

        Entries that don't resolve to a plain file (+incdir+/-y directories,
        +libext+ suffixes) keep only their raw line; changes inside include
        directories are not tracked.
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
                resolved = os.path.normpath(os.path.join(base, entry_path))
                if os.path.isfile(resolved):
                    stat = os.stat(resolved)
                    stamps.append([line, stat.st_size, stat.st_mtime_ns])
                else:
                    stamps.append([line, None, None])
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

        Excludes source size/mtime — and the toolchain's size/mtime/version
        — so editing RTL or upgrading a simulator in place rebuilds in the
        same dir (the stamp comparison catches the staleness) instead of
        accumulating a new obj_dir per edit.
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
        return [_stat_entry(path) for path in sorted(seen)]

    @staticmethod
    def _deps_unchanged(test_name, deps):
        """Have any of the stamp's recorded inputs changed on disk?"""
        for entry in deps:
            if not isinstance(entry, list) or len(entry) != 3:
                return False  # not a stamp this version wrote
            if entry != _stat_entry(entry[0]):
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

    @classmethod
    def _shared_build_is_valid(cls, build_dir, fingerprint, *, test_name=None):
        return cls._build_stamp_is_valid(
            build_dir, Path(build_dir) / "simv", fingerprint, test_name=test_name
        )

    @classmethod
    def _build_stamp_is_valid(
        cls, stamp_dir, simv_path, fingerprint, *, test_name=None
    ):
        """Does the stamp in ``stamp_dir`` still describe ``simv_path``?

        ``stamp_dir`` and the executable are separate arguments because an
        unshared build does not put the executable inside a directory
        rtl_buddy chose: the stamp goes in the test's compile work dir while
        ``builder-simv:`` decides where the binary lands (#369).
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
        stored_inputs = {
            key: value for key, value in stored.items() if key not in ("deps", "simv")
        }
        if stored_inputs != fingerprint:
            _log_stale_stamp_toolchain(stored_inputs, fingerprint, test_name=test_name)
            return False
        deps = stored["deps"]
        if deps is None:
            # The builder emitted no dependency file, so include-dir
            # contents stay untracked for it (docs/known-issues.md).
            return True
        return cls._deps_unchanged(test_name, deps)

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

    def compile(self):
        rtl_builder_cfg = self.rtl_builder_cfg
        log_event(
            logger,
            logging.DEBUG,
            "compile.config",
            test=self.test_name,
            config=pprint.pformat(rtl_builder_cfg),
        )
        compile_work_dir = self._ensure_artifact_dir()

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

        build_dir = self._get_build_dir()
        fingerprint = None
        if self.share_build:
            unsupported = self._share_build_unsupported_reason()
            if unsupported is None:
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
                    # how vvp is invoked for both.
                    key_cmd = key_cmd + self._icarus_vvp_extra_args()
                # Keyed on the configured compile line, NOT on the output
                # flags this method appends below: those are derived from the
                # resulting key, so including them would be circular.
                fingerprint = self._compile_fingerprint(key_cmd, filelist_path)
                shared_dir = shared_build_dir(
                    self.suite_work_dir, self._compile_config_key(fingerprint)
                )
                self._shared_build_dir = str(shared_dir)
                build_dir = str(shared_dir)
                if self._shared_build_is_valid(
                    shared_dir, fingerprint, test_name=self.test_name
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
                    return 0
                shared_dir.mkdir(parents=True, exist_ok=True)
                # A crashed/killed compile must never leave a stamp that
                # validates a broken simv.
                (shared_dir / SHARED_BUILD_STAMP_NAME).unlink(missing_ok=True)
                # A shared build owns the output location, so a *relative*
                # builder-simv: is discarded rather than honoured (the absolute
                # case declines sharing outright, above). Say which value went
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
                log_event(
                    logger,
                    logging.WARNING,
                    "compile.share_build_unsupported",
                    test=self.test_name,
                    simulator=self._get_simulator_family(),
                    reason=unsupported,
                )
                # The build cannot be *shared*, but it can still be *reused*
                # by the next process to ask for this test — which is what
                # lets a dispatched fan-out compile once in the build job and
                # have its elements short-circuit instead of racing each
                # other into one directory (#369). Same fingerprint, same
                # stamp file; only the scope differs, so the stamp lives in
                # the test's own compile work dir.
                key_cmd = (
                    [rtl_builder_cfg.get_exe()]
                    + builder_opts
                    + extra_compile_flags
                    + assertion_flags
                    + plusdefines
                )
                fingerprint = self._compile_fingerprint(key_cmd, filelist_path)
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
