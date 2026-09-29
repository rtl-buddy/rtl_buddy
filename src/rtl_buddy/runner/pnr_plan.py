"""Block-before-top ordering for `rb pnr` over a whole suite.

A run that names hardened blocks under `blocks:` consumes the abstracts their `harden: true` runs publish, so those run first.
The plan is a topological sort over the `blocks:` edges that keeps file order where the edges leave a choice.
Blocks defined in another `pnr.yaml` are pulled in ahead of their consumers, recursively.
A named run is not expanded: it consumes whatever abstract is published and fails fast when there is none.
"""

import os
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from ..config.pnr import PnrConfig, PnrSuiteConfig
from ..errors import FatalRtlBuddyError

RunKey = tuple[str, str]


def run_key(suite_path: str, run_name: str) -> RunKey:
    """A run's identity across files: its `pnr.yaml`, resolved, and its name."""
    return (os.path.realpath(suite_path), run_name)


@dataclass(frozen=True)
class BlockDep:
    """One `blocks:` entry of a planned run, as an edge of the plan."""

    block: str
    key: RunKey


@dataclass(frozen=True)
class PlannedRun:
    suite_path: str
    cfg: PnrConfig
    deps: tuple[BlockDep, ...] = ()
    # True for a run from another pnr.yaml, included only because a requested run or one of its blocks consumes it.
    pulled_in: bool = False

    @property
    def name(self) -> str:
        return self.cfg.get_name()

    @property
    def key(self) -> RunKey:
        return run_key(self.suite_path, self.name)


def _label(key: RunKey, root: str) -> str:
    suite, name = key
    return name if suite == root else f"{name} ({suite})"


def plan_pnr_runs(
    suite_cfg: PnrSuiteConfig, *, load_suite=PnrSuiteConfig, synth_blocks=None
):
    """Return every run of ``suite_cfg`` plus the blocks it is built from, in run order.

    ``synth_blocks`` (`rb pnr --synth`) is a predicate over a run's config. Where it holds, the run also depends on the blocks named by its upstream synthesis.
    It is false for a run `-l` deselects.

    Raises :class:`FatalRtlBuddyError`, before anything runs, for a block naming a run no `pnr.yaml` defines and for a cycle.
    """
    root = os.path.realpath(suite_cfg.get_path())
    suites: dict[str, PnrSuiteConfig] = {root: suite_cfg}
    found: dict[RunKey, tuple[str, PnrConfig, bool]] = {}
    for cfg in suite_cfg.get_runs():
        found[run_key(root, cfg.get_name())] = (root, cfg, False)

    deps: dict[RunKey, list[BlockDep]] = {}
    pending = list(found)
    while pending:
        key = pending.pop(0)
        cfg = found[key][1]
        edges = deps.setdefault(key, [])
        refs = list(cfg.get_blocks())
        if synth_blocks is not None and synth_blocks(cfg):
            refs += cfg.resolve_synth_cfg().get_blocks()
        for ref in refs:
            dep = run_key(ref.pnr_suite_path, ref.pnr_run)
            if any(edge.key == dep for edge in edges):
                continue
            edges.append(BlockDep(block=ref.name, key=dep))
            if dep in found:
                continue
            dep_suite = dep[0]
            if dep_suite not in suites:
                if not os.path.isfile(dep_suite):
                    raise FatalRtlBuddyError(
                        f"pnr run '{_label(key, root)}': block '{ref.name}' "
                        f"names pnr-path {ref.pnr_suite_path}, which does not exist"
                    )
                suites[dep_suite] = load_suite(dep_suite)
            runs = suites[dep_suite].runs
            if ref.pnr_run not in runs:
                raise FatalRtlBuddyError(
                    f"pnr run '{_label(key, root)}': block '{ref.name}' names "
                    f"pnr run '{ref.pnr_run}', which {dep_suite} does not define"
                )
            found[dep] = (dep_suite, runs[ref.pnr_run], True)
            pending.append(dep)

    # Two pnr.yaml files in one directory share its artefacts/, so a run name in both would write one directory twice.
    outputs: dict[tuple[str, str], RunKey] = {}
    for key in found:
        out = (os.path.dirname(key[0]), key[1])
        if out in outputs:
            raise FatalRtlBuddyError(
                f"pnr runs '{_label(outputs[out], root)}' and "
                f"'{_label(key, root)}' would both write "
                f"{os.path.join(out[0], 'artefacts', out[1])}: rename one"
            )
        outputs[out] = key

    order = list(found)
    index = {key: i for i, key in enumerate(order)}
    done: set[RunKey] = set()
    planned: list[PlannedRun] = []
    while len(planned) < len(order):
        ready = [
            key
            for key in order
            if key not in done and all(d.key in done for d in deps[key])
        ]
        if not ready:
            cycle = _find_cycle([k for k in order if k not in done], deps)
            raise FatalRtlBuddyError(
                "pnr blocks: cycle: "
                + " -> ".join(_label(key, root) for key in cycle)
                + " — a block cannot be built from a run that consumes it"
            )
        key = min(ready, key=index.__getitem__)
        suite, cfg, pulled = found[key]
        planned.append(
            PlannedRun(
                suite_path=suite, cfg=cfg, deps=tuple(deps[key]), pulled_in=pulled
            )
        )
        done.add(key)
    return planned


def _find_cycle(keys: list[RunKey], deps: dict[RunKey, list[BlockDep]]):
    """Return one cycle among ``keys`` as a path that ends where it starts."""
    remaining = set(keys)
    path: list[RunKey] = []
    on_path: dict[RunKey, int] = {}
    key = keys[0]
    # Every remaining run has a remaining block, so the walk must revisit a run.
    while key not in on_path:
        on_path[key] = len(path)
        path.append(key)
        key = next(d.key for d in deps[key] if d.key in remaining)
    return [*path[on_path[key] :], key]


class OnceMap:
    """Compute a value once per key; concurrent callers for the same key wait for the first result."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict = {}
        self._values: dict = {}

    def get(self, key, compute):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key not in self._values:
                self._values[key] = compute()
            return self._values[key]


def run_plan(plan: list[PlannedRun], step, *, jobs: int = 1, on_error=None) -> dict:
    """Call ``step(planned, outcomes)`` for every run of ``plan`` and return ``{key: step's return}``.

    ``outcomes`` maps each finished run's key to its `results` object. With ``jobs`` 1 steps run in plan order.
    Otherwise up to ``jobs`` steps run at once, each starting once every block it names that is in the plan has finished.

    ``on_error(planned, exc)``, when given, turns an exception from a step into that run's row instead of aborting the plan.
    """

    def _call(planned, outcomes):
        if on_error is None:
            return step(planned, outcomes)
        try:
            return step(planned, outcomes)
        except Exception as exc:  # noqa: BLE001 — reported as the run's row
            return on_error(planned, exc)

    in_plan = {p.key for p in plan}
    rows: dict[RunKey, dict] = {}
    outcomes: dict[RunKey, object] = {}
    if jobs <= 1:
        for planned in plan:
            rows[planned.key] = _call(planned, outcomes)
            outcomes[planned.key] = rows[planned.key]["results"]
        return rows

    pending = list(plan)
    running: dict = {}
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="rb-pnr") as pool:
        while pending or running:
            for planned in list(pending):
                if len(running) >= jobs:
                    break
                if all(d.key in outcomes or d.key not in in_plan for d in planned.deps):
                    pending.remove(planned)
                    running[pool.submit(_call, planned, dict(outcomes))] = planned
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                planned = running.pop(future)
                rows[planned.key] = future.result()
                outcomes[planned.key] = rows[planned.key]["results"]
    return rows
