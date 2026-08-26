# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import logging
from enum import Enum

logger = logging.getLogger(__name__)

from ..tools.vlog_sim import VlogSim
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


# Distinguishes "call pre() with its own default run_id" from "call it with
# run_id=None on purpose" (#415) — the latter is what run_multiple() needs
# and None is a meaningful value there, so a plain default cannot say it.
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
        expect_prebuilt=False,
    ):
        """
        Run tests based on config
        Handles Verilog compilation
        """
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
        self.expect_prebuilt = expect_prebuilt
        # Set by prepare(); the phases after it all drive this one instance,
        # because a preproc hook may mutate test_cfg and the compile key is
        # only knowable afterwards, on the sim that saw the mutation.
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
            expect_prebuilt=self.expect_prebuilt,
        )

    def _run_pre(self, *, pre_run_id=_PRE_RUN_ID_DEFAULT):
        """Create this runner's sim instance and run PRE; error string or None."""
        self._vlog_sim = self._create_vlog_sim()
        if pre_run_id is _PRE_RUN_ID_DEFAULT:
            return self._vlog_sim.pre()
        return self._vlog_sim.pre(run_id=pre_run_id)

    def prepare(self, *, pre_run_id=_PRE_RUN_ID_DEFAULT):
        """Create the sim instance and run PRE. ``SetupFailResults`` or ``None``.

        Split out of :meth:`run` so a dispatched build job can keep PRE
        serial while compiling concurrently (#495): hook execution is
        process-global-serial by contract (one ``sys.modules`` slot and a
        process-wide ``redirect_stdout``, see ``hooks.py``), and a preproc
        script may mutate ``test_cfg`` — so the compile key exists only
        after this ran, on the instance it ran on.
        """
        # The one per-test marker in a run's DEBUG log, and it belongs to
        # the phase rather than to run(): the build job drives the phases
        # directly, and a build-job log with no per-test line at all is
        # unreadable when one config out of eight misbehaves.
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
        """This runner's sim's compile record, or ``None`` (#495).

        ``{duration_sec, builder, reused}`` — what the COMPILE phase cost,
        for the build envelope and the results overlay. ``None`` before
        :meth:`prepare` has built the sim: a runner whose PRE never got
        that far has nothing to report, and telemetry must never be the
        thing that raises.
        """
        return None if self._vlog_sim is None else self._vlog_sim.last_compile

    def compile_group_dir(self):
        """``(group_dir, None)`` or ``(None, Results)`` for the prepared sim.

        The build job's grouping probe (#495). Same failure mapping the
        compile itself gets, because it is the same ``run.f`` write.
        """
        try:
            return self._vlog_sim.compile_group_dir(), None
        except FilelistError as e:
            return None, FilelistFailResults(name=self.name + "/results", desc=str(e))

    def _compile_outcome(self, run_ids=None):
        """Run COMPILE on the prepared sim; a results *factory*, or ``None``.

        A factory rather than an instance because :meth:`run_multiple` needs
        one outcome as N separate objects (each run_id is recorded on its
        own; a shared instance would make them one row wearing N names)
        while still deriving that outcome here and only here. ``None`` means
        the compile succeeded and the caller should go on to the simulation.
        """
        try:
            compile_returncode = self._vlog_sim.compile()
        except FilelistError as e:
            desc = str(e)
            return lambda: FilelistFailResults(name=self.name + "/results", desc=desc)
        if compile_returncode != 0:
            return lambda: CompileFailResults(name=self.name + "/results")

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

        Returns the terminal ``Results`` — ``FilelistFailResults`` /
        ``CompileFailResults``, or ``EarlyStopResults`` when this runner
        stops at COMP — or ``None`` when the caller should go on to the
        simulation. ``run_ids`` only shapes the early-stop record, so
        :meth:`run_multiple` reports the whole set it was going to run.
        """
        make_results = self._compile_outcome(run_ids=run_ids)
        return None if make_results is None else make_results()

    def run(self):
        # run pre-proc python (which logs test_runner.start)
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

        # compile sim executable
        compile_results = self.compile_prepared()
        if compile_results is not None:
            return compile_results

        # run simulation
        execute_returncode = vlog_sim.execute(
            run_id=self.run_id,
            seed_mode=self.seed_mode,
            replay_run_id=self.replay_run_id,
        )
        if execute_returncode == 4444:
            return SimTimeoutResults(name=self.name + "/results")

        if self.run_depth == RunDepth.SIM:
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

        # run post-proc
        results = vlog_sim.post(run_id=self.run_id)
        return results

    def run_multiple(self, run_ids):
        """
        Execute one pre/compile flow and run multiple simulations over run_ids.

        run_id controls output naming for each simulation. seed_mode controls whether
        each run uses default seed, fresh random seed, or replayed seed.
        """
        log_event(
            logger,
            logging.DEBUG,
            "test_runner.start_multiple",
            runner=self.name,
            test=self.test_cfg.get_name(),
            run_ids=run_ids,
        )
        # One hook execution serves every run_id here, so it is preparing no
        # particular run — explicitly, because the runner was constructed with
        # run_ids[0] and the default would otherwise hand the hook run 1's
        # directory for output that runs 2..N also read (#415).
        pre_error = self._run_pre(pre_run_id=None)
        vlog_sim = self._vlog_sim
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
                repeated_results.append(SimTimeoutResults(name=self.name + "/results"))
            elif self.run_depth == RunDepth.SIM:
                log_event(
                    logger,
                    logging.INFO,
                    "run.early_stop",
                    test=self.test_cfg.get_name(),
                    run_id=run_id,
                    stage="sim",
                )
                repeated_results.append(
                    EarlyStopResults(
                        name=self.name + "/results", desc="Stopped early at sim"
                    )
                )
            else:
                repeated_results.append(vlog_sim.post(run_id=run_id))

        return repeated_results
