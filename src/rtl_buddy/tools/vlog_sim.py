# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""
vlog_sim module handles verilog simulations for rtl-buddy

"""

import hashlib
import json
import os
import random
import re
import signal
import logging

logger = logging.getLogger(__name__)
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


def force_symlink(target, link_name):
    if os.path.lexists(link_name):
        os.remove(link_name)

    os.symlink(target, link_name)


# Stamp written into a shared build dir after a successful compile; records
# the exact compile inputs the simv was built from so reuse can be validated.
SHARED_BUILD_STAMP_NAME = "rb-compile-stamp.json"

# Matches the option prefixes VlogFilelist emits into run.f (see
# VlogFilelist._extract): `+incdir+`, `+libext+`, `-v `, `-y `, `-F `.
_FILELIST_OPTION_RE = re.compile(r"^(?:\+(?:incdir|libext)\+|-[vyF]\s+)?(.*)$")


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
    ):
        """
        compile and execute sim for given test
        """
        self.name = name
        self.root_cfg = root_cfg
        self.rtl_builder_cfg = root_cfg.get_rtl_builder_cfg()
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
        """
        rtl_builder_exe = self.rtl_builder_cfg.get_exe()
        if os.path.basename(rtl_builder_exe).startswith("verilator"):
            if self._shared_build_dir is not None:
                return str(Path(self._shared_build_dir) / "simv")
            return str(
                Path(self._get_compile_work_dir()) / self._get_build_dir() / "simv"
            )
        simv_path = self.rtl_builder_cfg.get_simv()
        if os.path.isabs(simv_path):
            return simv_path
        return str(Path(self._get_compile_work_dir()) / simv_path)

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
        }

    @staticmethod
    def _compile_config_key(fingerprint):
        """Short stable hash naming the shared build dir.

        Excludes source size/mtime so editing RTL rebuilds in place in the
        same dir (the stamp comparison catches the staleness) instead of
        accumulating a new obj_dir per edit.
        """
        config = {
            "cmd": fingerprint["cmd"],
            "env": fingerprint["env"],
            "filelist": [entry[0] for entry in fingerprint["sources"]],
        }
        digest = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return digest[:16]

    @staticmethod
    def _shared_build_is_valid(build_dir, fingerprint):
        simv_path = Path(build_dir) / "simv"
        stamp_path = Path(build_dir) / SHARED_BUILD_STAMP_NAME
        if not simv_path.is_file() or not stamp_path.is_file():
            return False
        try:
            stored = json.loads(stamp_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return stored == fingerprint

    def pre(self):
        script_path = self.test_cfg.get_preproc_path()
        if script_path is None:
            log_event(logger, logging.DEBUG, "preproc.skipped", test=self.test_name)
            return None

        with open(script_path, "r") as file:
            code = file.read()

        # Pass self.test_cfg to the preproc script as root_cfg
        # preproc script can mutate self.test_cfg, which is used for compile and sim
        ns = {
            "logger": logger,
            "test_cfg": self.test_cfg,
            "root_cfg": self.root_cfg,
            "suite_dir": self.suite_work_dir,
            "artifact_dir": self._get_artifact_dir(),
            "__file__": os.path.abspath(script_path),
        }
        try:
            exec(code, ns)
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

        log_event(
            logger,
            logging.INFO,
            "preproc.completed",
            test=self.test_name,
            script=script_path,
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
            if is_verilator:
                key_cmd = (
                    [rtl_builder_cfg.get_exe()]
                    + builder_opts
                    + extra_compile_flags
                    + assertion_flags
                    + plusdefines
                )
                fingerprint = self._compile_fingerprint(key_cmd, filelist_path)
                shared_dir = shared_build_dir(
                    self.suite_work_dir, self._compile_config_key(fingerprint)
                )
                self._shared_build_dir = str(shared_dir)
                build_dir = str(shared_dir)
                if self._shared_build_is_valid(shared_dir, fingerprint):
                    log_event(
                        logger,
                        logging.INFO,
                        "compile.build_reused",
                        test=self.test_name,
                        build_dir=build_dir,
                    )
                    return 0
                shared_dir.mkdir(parents=True, exist_ok=True)
                # A crashed/killed compile must never leave a stamp that
                # validates a broken simv.
                (shared_dir / SHARED_BUILD_STAMP_NAME).unlink(missing_ok=True)
            else:
                log_event(
                    logger,
                    logging.WARNING,
                    "compile.share_build_unsupported",
                    test=self.test_name,
                    simulator=self._get_simulator_family(),
                )

        run_cmd = [rtl_builder_cfg.get_exe()]
        run_cmd += builder_opts

        if is_verilator:
            run_cmd += ["--Mdir", build_dir]

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
        if result.returncode != 0:
            transcript_path = self._get_compile_transcript_path()
            with open(transcript_path, "w") as transcript_fp:
                transcript_fp.write(f"Command: {run_str}\n\n")
                transcript_fp.write("=== stderr ===\n")
                transcript_fp.write(result.stderr or "")
                transcript_fp.write("\n=== stdout ===\n")
                transcript_fp.write(result.stdout or "")
            log_event(
                logger,
                logging.ERROR,
                "compile.failed",
                test=self.test_name,
                returncode=result.returncode,
                duration_sec=round(e_time - s_time, 2),
                transcript=transcript_path,
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "compile.completed",
                test=self.test_name,
                duration_sec=round(e_time - s_time, 2),
            )
            if result.stdout:
                logger.debug("compile stdout\n%s", result.stdout)
            if fingerprint is not None:
                stamp_path = Path(build_dir) / SHARED_BUILD_STAMP_NAME
                stamp_path.write_text(json.dumps(fingerprint, sort_keys=True))
                log_event(
                    logger,
                    logging.DEBUG,
                    "compile.build_stamp_written",
                    test=self.test_name,
                    stamp=str(stamp_path),
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
                    )
                    returncode = result.returncode

                    t_time = time.time() - s_time
                    if result.timed_out:
                        log_event(
                            logger,
                            logging.ERROR,
                            "sim.timeout",
                            test=self.test_name,
                            run_id=run_id,
                            timeout_sec=timeout,
                            **artifact_paths,
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
