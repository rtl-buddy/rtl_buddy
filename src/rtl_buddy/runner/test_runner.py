# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import logging
from enum import Enum

logger = logging.getLogger(__name__)

from ..tools.vlog_sim import VlogSim
from ..tools.vlog_post import describe_sim_exit
from ..tools.cocotb_sim import CocotbSim
from ..tools.systemc_sim import SystemCSim
from ..seed_mode import SeedMode
from .test_results import *
from ..errors import FilelistError
from ..logging_utils import log_event


class RunDepth(Enum):
    PRE = "pre"
    COMP = "comp"
    SIM = "sim"
    POST = "post"


# Separates "use pre()'s default run_id" from an explicit run_id=None, which run_multiple() needs.
_PRE_RUN_ID_DEFAULT = object()


class TestRunner:
    def __init__(
        self,
        name,
        root_cfg,
        test_cfg,
        rtl_builder_mode,
        test_runner_mode,
        run_id=None,
        seed_mode: SeedMode = SeedMode.DEFAULT,
        replay_run_id=None,
        run_depth=None,
        suite_dir=None,
        share_build=False,
        shared_build_root=None,
        expect_prebuilt=False,
        rebuild=False,
        build_result_json=None,
        build_phase=None,
        run_tag=None,
    ):
        """Run one test from its config, including Verilog compilation."""
        log_event(
            logger,
            logging.DEBUG,
            "test_runner.init",
            name=name,
            test=test_cfg.get_name(),
            run_id=run_id,
        )
        self.name = name
        self.root_cfg = root_cfg
        self.test_cfg = test_cfg
        self.run_id = run_id
        self.seed_mode = seed_mode
        self.replay_run_id = replay_run_id
        self.run_depth = run_depth
        self.rtl_builder_mode = rtl_builder_mode
        self.test_runner_mode = test_runner_mode
        self.suite_dir = suite_dir
        self.share_build = share_build
        # Passed in, not re-resolved: the head resolved the precedence once, and a second resolution could disagree with the build job.
        self.shared_build_root = shared_build_root
        self.expect_prebuilt = expect_prebuilt
        # `--rebuild`: compile even when the stamp says the build is warm.
        self.rebuild = rebuild
        self.build_result_json = build_result_json
        # Which half of a split compile COMPILE performs; ``None`` means all of it.
        self.build_phase = build_phase
        # The `--run-tag` artefact namespace, decided by the head.
        self.run_tag = run_tag
        # Set by prepare(). A preproc hook may mutate test_cfg, so the compile key exists only on the sim that ran the hook.
        self._vlog_sim = None

    def _create_vlog_sim(self):
        sim_mode = {"sim_to_stdout": True}
        if "sim_to_stdout" in self.test_runner_mode:
            sim_mode["sim_to_stdout"] = self.test_runner_mode["sim_to_stdout"]

        tb = self.test_cfg.get_testbench()
        if tb.is_cocotb():
            sim_class = CocotbSim
        elif tb.is_systemc():
            sim_class = SystemCSim
        else:
            sim_class = VlogSim
        return sim_class(
            name=self.name + "/vlog_sim",
            root_cfg=self.root_cfg,
            test_cfg=self.test_cfg,
            rtl_builder_mode=self.rtl_builder_mode,
            sim_mode=sim_mode,
            run_id=self.run_id,
            replay_run_id=self.replay_run_id,
            suite_dir=self.suite_dir,
            share_build=self.share_build,
            shared_build_root=self.shared_build_root,
            expect_prebuilt=self.expect_prebuilt,
            rebuild=self.rebuild,
            build_result_json=self.build_result_json,
            run_tag=self.run_tag,
            **(
                {"build_phase": self.build_phase}
                if self.build_phase is not None
                else {}
            ),
        )

    def _run_pre(self, *, pre_run_id=_PRE_RUN_ID_DEFAULT):
        """Create the sim instance and run PRE; return an error string or None."""
        self._vlog_sim = self._create_vlog_sim()
        if pre_run_id is _PRE_RUN_ID_DEFAULT:
            return self._vlog_sim.pre()
        return self._vlog_sim.pre(run_id=pre_run_id)

    def prepare(self, *, pre_run_id=_PRE_RUN_ID_DEFAULT):
        """Create the sim instance and run PRE; return ``SetupFailResults`` or ``None``.

        A dispatched build job calls this directly so PRE hooks stay serial while compiles run concurrently.
        """
        # Logged here, not in run(): the build job drives the phases directly and needs a per-test log line.
        log_event(
            logger,
            logging.DEBUG,
            "test_runner.start",
            runner=self.name,
            test=self.test_cfg.get_name(),
            run_id=self.run_id,
        )
        pre_error = self._run_pre(pre_run_id=pre_run_id)
        if pre_error is not None:
            return SetupFailResults(name=self.name + "/results", desc=pre_error)
        return None

    @property
    def last_compile(self):
        """Return the sim's ``{duration_sec, builder, reused}`` compile record, or ``None`` before :meth:`prepare` built the sim."""
        return None if self._vlog_sim is None else self._vlog_sim.last_compile

    @property
    def last_compile_failure(self):
        """Return the sim's ``{returncode, transcript}`` failed-compile record, or ``None``.

        The build job records it in its envelope. Telemetry: it never raises.
        """
        try:
            return getattr(self._vlog_sim, "last_compile_failure", None)
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return None

    @property
    def last_build_stamp(self):
        """Return the sim's ``{build_dir, fingerprint_sha, simv}`` build-stamp identity, or ``None``.

        The head uses it at collect time to check that one compile key produced one binary. Telemetry: it never raises.
        """
        try:
            return getattr(self._vlog_sim, "last_build_stamp", None)
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return None

    @property
    def stamp_write_failed(self):
        """Return whether the compile succeeded but left no stamp.

        The build job records it as ``stamp_written: false`` so a gated sim job does not recompile. Telemetry: it never raises.
        """
        try:
            return bool(getattr(self._vlog_sim, "stamp_write_failed", False))
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return False

    def refresh_build_stamp(self):
        """Re-read the stamp behind :attr:`last_build_stamp`.

        The build job calls this after every group member is done, because a sibling's adoption rewrites the stamp. Telemetry: it never raises.
        """
        try:
            self._vlog_sim.refresh_build_stamp()
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return

    def adopt_group_build(self):
        """Adopt a same-key sibling's build on the prepared sim.

        Returns ``("adopted", None)``, ``("drift", <path>)`` or ``(None, <reason>)``; see :meth:`VlogSim.adopt_group_build`.
        Only the dispatched build job calls it, after the group leader compiled.
        """
        return self._vlog_sim.adopt_group_build()

    @property
    def builder_name(self):
        """Return the resolved builder's name, or ``None`` before :meth:`prepare` built the sim.

        Unlike :attr:`last_compile` it is known even when PRE failed. Telemetry: it never raises.
        """
        if self._vlog_sim is None:
            return None
        try:
            return self._vlog_sim.rtl_builder_cfg.get_name()
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return None

    def compile_group_dir(self):
        """Return ``(group_dir, None)`` or ``(None, Results)`` for the prepared sim.

        The build job uses it to group configs; a filelist error maps to a failure result as in compile.
        """
        try:
            return self._vlog_sim.compile_group_dir(), None
        except FilelistError as e:
            return None, FilelistFailResults(name=self.name + "/results", desc=str(e))

    def _compile_outcome(self, run_ids=None):
        """Run COMPILE on the prepared sim and return a results factory, or ``None`` to go on to simulation.

        It returns a factory because :meth:`run_multiple` needs a separate results object per run_id.
        """
        try:
            compile_returncode = self._vlog_sim.compile()
        except FilelistError as e:
            desc = str(e)
            return lambda: FilelistFailResults(name=self.name + "/results", desc=desc)
        if compile_returncode != 0:
            # getattr: only some sim classes offer a specific desc.
            desc = getattr(self._vlog_sim, "compile_fail_desc", None)
            return lambda: CompileFailResults(name=self.name + "/results", desc=desc)

        if self.run_depth == RunDepth.COMP:
            if run_ids is None:
                log_event(
                    logger,
                    logging.INFO,
                    "run.early_stop",
                    test=self.test_cfg.get_name(),
                    run_id=self.run_id,
                    stage="compile",
                )
            else:
                log_event(
                    logger,
                    logging.INFO,
                    "run.early_stop",
                    test=self.test_cfg.get_name(),
                    stage="compile",
                    run_ids=run_ids,
                )
            return lambda: EarlyStopResults(
                name=self.name + "/results", desc="Stopped early at compile"
            )
        return None

    def compile_prepared(self, run_ids=None):
        """Run COMPILE on the prepared sim.

        Returns ``FilelistFailResults``, ``CompileFailResults`` or, at ``-E comp``, ``EarlyStopResults``; ``None`` means go on to simulation.
        ``run_ids`` only shapes the early-stop log record.
        """
        make_results = self._compile_outcome(run_ids=run_ids)
        return None if make_results is None else make_results()

    def _sim_stage_failure(self, execute_returncode, run_id):
        """Return the ``-E sim`` result for a simulation that exited nonzero.

        The desc names the exit status and says the transcript was not post-processed; it does not claim a verdict is missing, because the transcript may hold one.
        """
        log_event(
            logger,
            logging.ERROR,
            "sim.stage_failed",
            test=self.test_cfg.get_name(),
            run_id=run_id,
            stage="sim",
            returncode=execute_returncode,
        )
        return SimStageFailResults(
            name=self.name + "/results",
            desc=(
                f"Sim {describe_sim_exit(execute_returncode)} before the "
                "-E sim stop; transcript not post-processed"
            ),
        )

    def run(self):
        setup_failure = self.prepare()
        if setup_failure is not None:
            return setup_failure
        vlog_sim = self._vlog_sim

        if self.run_depth == RunDepth.PRE:
            log_event(
                logger,
                logging.INFO,
                "run.early_stop",
                test=self.test_cfg.get_name(),
                run_id=self.run_id,
                stage="preproc",
            )
            return EarlyStopResults(
                name=self.name + "/results", desc="Stopped early at preproc"
            )

        compile_results = self.compile_prepared()
        if compile_results is not None:
            return compile_results

        execute_returncode = vlog_sim.execute(
            run_id=self.run_id,
            seed_mode=self.seed_mode,
            replay_run_id=self.replay_run_id,
        )
        if execute_returncode == 4444:
            return SimTimeoutResults(name=self.name + "/results")

        if self.run_depth == RunDepth.SIM:
            # A simulator crash under -E sim is a failed stage, not an early stop.
            if execute_returncode != 0:
                return self._sim_stage_failure(execute_returncode, self.run_id)
            log_event(
                logger,
                logging.INFO,
                "run.early_stop",
                test=self.test_cfg.get_name(),
                run_id=self.run_id,
                stage="sim",
            )
            return EarlyStopResults(
                name=self.name + "/results", desc="Stopped early at sim"
            )

        # post() needs the exit status: an aborted run has no verdict in its transcript and must become a FAIL, not an NA.
        return vlog_sim.post(run_id=self.run_id, sim_returncode=execute_returncode)

    def run_multiple(self, run_ids):
        """Run one pre/compile flow, then one simulation per run_id.

        run_id names each simulation's outputs; seed_mode selects a default, fresh, replayed or pre-resolved master seed.
        """
        log_event(
            logger,
            logging.DEBUG,
            "test_runner.start_multiple",
            runner=self.name,
            test=self.test_cfg.get_name(),
            run_ids=run_ids,
        )
        # pre_run_id=None: the hook serves every run_id, not just run_ids[0].
        pre_error = self._run_pre(pre_run_id=None)
        vlog_sim = self._vlog_sim
        # Must precede the SetupFail return; the sim's own cleanup reaches only run_ids[0].
        vlog_sim.clear_retry_transcripts(run_ids)
        if pre_error is not None:
            return [
                SetupFailResults(name=self.name + "/results", desc=pre_error)
                for _ in run_ids
            ]

        if self.run_depth == RunDepth.PRE:
            log_event(
                logger,
                logging.INFO,
                "run.early_stop",
                test=self.test_cfg.get_name(),
                stage="preproc",
                run_ids=run_ids,
            )
            return [
                EarlyStopResults(
                    name=self.name + "/results", desc="Stopped early at preproc"
                )
                for _ in run_ids
            ]

        make_results = self._compile_outcome(run_ids=run_ids)
        if make_results is not None:
            return [make_results() for _ in run_ids]

        repeated_results = []
        for run_id in run_ids:
            replay_run_id = self.replay_run_id
            if self.seed_mode == SeedMode.REPLAY and replay_run_id is None:
                replay_run_id = run_id
            execute_returncode = vlog_sim.execute(
                run_id=run_id, seed_mode=self.seed_mode, replay_run_id=replay_run_id
            )
            if execute_returncode == 4444:
                result = SimTimeoutResults(name=self.name + "/results")
            elif self.run_depth == RunDepth.SIM and execute_returncode != 0:
                result = self._sim_stage_failure(execute_returncode, run_id)
            elif self.run_depth == RunDepth.SIM:
                log_event(
                    logger,
                    logging.INFO,
                    "run.early_stop",
                    test=self.test_cfg.get_name(),
                    run_id=run_id,
                    stage="sim",
                )
                result = EarlyStopResults(
                    name=self.name + "/results", desc="Stopped early at sim"
                )
            else:
                result = vlog_sim.post(run_id=run_id, sim_returncode=execute_returncode)
            # Read now: the next run's execute() overwrites it, and a shared binary may change between seeds.
            stamp = self.last_build_stamp
            if stamp is not None:
                result.results["build_stamp"] = dict(stamp)
            repeated_results.append(result)

        return repeated_results
