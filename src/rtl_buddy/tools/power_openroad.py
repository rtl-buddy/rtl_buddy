import contextlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many times `_snapshot_netlist` re-copies a netlist that changed underneath it before failing the run.
_SNAPSHOT_ATTEMPTS = 3

from .liberty_units import open_liberty
from ..config.openroad_threads import ThreadPlan, parse_reported_threads, plan_threads
from ..config.power import PowerConfig
from ..logging_utils import log_event, task_status
from ..phys import reports
from ..phys.manifest import (
    project_relative,
    project_root_for_dir,
    project_root_or_none,
)
from ..phys.provenance import TRACE_SOURCES, activity_block
from ..phys.publish import (
    confirm_digest,
    invalidate_half,
    publish_power,
    sha256_of,
    withdrawal_failure_desc,
)
from ..runner.power_results import PowerFailResults, PowerPassResults, PowerResults
from . import block_params, openroad_corners, pnr_abstract
from .artifact_paths import clear_stale_artefacts
from .pnr_openroad import PNR_SCRIPT_NAME, ROUTED_SPEF_SUFFIX, tcl_source
from .power_base import BasePower
from .synth_yosys import library_fingerprint


def _within(root: str, path: str) -> bool:
    """Return whether ``path`` is inside ``root``, comparing logical paths first, then resolved ones.

    Logical first because a suite whose ``artefacts/`` links to scratch storage is inside the
    project as the project is read (see :func:`~rtl_buddy.phys.manifest.project_relative`), though
    it resolves outside it. The resolved comparison covers the reverse arrangement.
    """
    for base, candidate in (
        (os.path.abspath(root), os.path.abspath(path)),
        (os.path.realpath(root), os.path.realpath(path)),
    ):
        if candidate == base or candidate.startswith(base + os.sep):
            return True
    return False


#: `parasitics` values: what the analysis timed the routed design on.
PARASITICS_SPEF = "spef"
PARASITICS_ESTIMATED = "estimated"

_WRITE_SPEF_RE = re.compile(r"^\s*write_spef\s", re.MULTILINE)


def routed_spef_rejection(spef: str, script: str) -> str | None:
    """Return why the routed SPEF beside a P&R result may not be read, or `None`.

    `rb pnr` clears `<top>.routed.spef` before every run, but an older rtl_buddy or a copied
    artefact directory can leave a SPEF that does not belong to the ODB. The SPEF is accepted only
    if the P&R run's flow script contains a `write_spef` command and the SPEF is no older than that
    script (mtime, since the script is the run's first write and the SPEF one of its last).
    """
    if not os.path.isfile(spef):
        return "no routed SPEF"
    try:
        script_text = Path(script).read_text()
        script_mtime = os.stat(script).st_mtime_ns
    except OSError:
        return f"no P&R flow script at {script} to vouch for it"
    if not _WRITE_SPEF_RE.search(script_text):
        return "the P&R run that wrote the ODB did not extract parasitics"
    try:
        spef_mtime = os.stat(spef).st_mtime_ns
    except OSError as e:
        return f"cannot stat it: {e}"
    if spef_mtime < script_mtime:
        return "it is older than the P&R run that wrote the ODB"
    return None


def _dedup_paths(paths) -> list[str]:
    """Return the paths in first-named order, one entry per resolved file, empties dropped.

    `read_liberty` on the same file twice re-registers every cell and warns about each. Order is
    deterministic because the first library to define a cell name wins.
    """
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


#: A Liberty ``cell (NAME) {`` declaration, and the two-line spelling generated libraries use.
#: Scanned line by line, not parsed, because a Liberty can be tens of MB.
_LIBERTY_CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?([^"\s()]+)"?\s*\)')
_LIBERTY_CELL_OPEN_RE = re.compile(r"^\s*cell\s*$")
_LIBERTY_CELL_NAME_RE = re.compile(r'^\s*\(\s*"?([^"\s()]+)"?\s*\)')


def _liberty_cell_names(paths) -> set[str]:
    """Return every cell name the given Liberty files declare.

    Unlike the synthesis backend's scan this ignores LEF, since a `MACRO` has no power data. An
    unreadable file contributes nothing, so its cells are reported as unpowered.
    """
    names: set[str] = set()
    for path in paths:
        try:
            with open_liberty(path) as f:
                pending = False
                for line in f:
                    if pending:
                        pending = False
                        m = _LIBERTY_CELL_NAME_RE.match(line)
                        if m:
                            names.add(m.group(1))
                            continue
                    m = _LIBERTY_CELL_RE.match(line)
                    if m:
                        names.add(m.group(1))
                    elif _LIBERTY_CELL_OPEN_RE.match(line):
                        pending = True
        except (OSError, EOFError):
            continue
    return names


class OpenRoadPower(BasePower):
    """OpenROAD power-analysis backend.

    Reads the upstream `rb synth` netlist (or `rb pnr` routed ODB) with the platform Liberty, tech
    and macro LEFs and the SDC, applies an activity model (synthetic global activity, SAIF or VCD),
    and parses `report_power` for total/internal/switching/leakage. LEF is required because
    OpenROAD's gate-level `read_verilog` needs a technology view (`[ERROR ORD-2010] no technology
    has been read.`).
    """

    def __init__(
        self,
        name: str,
        power_cfg: PowerConfig,
        suite_dir: str,
        root_cfg,
        executable: str = "openroad",
    ):
        super().__init__(
            name=name,
            power_cfg=power_cfg,
            suite_dir=suite_dir,
            root_cfg=root_cfg,
            executable=executable,
        )
        artefact_root = Path(suite_dir) / "artefacts" / power_cfg.get_name()
        artefact_root.mkdir(parents=True, exist_ok=True)
        self.artefact_dir = str(artefact_root)
        # Where `phys-model.json` and its manifest go; every other file stays in `artefact_dir`. Rebound by `_bind_phys_dir` when `phys-run:` is set.
        self.phys_dir = self.artefact_dir
        # The upstream netlist this run measures and the hash of the private copy OpenROAD reads (`_snapshot_netlist`). None until resolved and for `netlist-source: pnr`.
        self._netlist_source_path: str | None = None
        self._netlist_sha256: str | None = None
        # Activity trace identity, taken at OpenROAD launch and confirmed on return (`_hash_trace`). None before the run and for static runs.
        self._trace_sha256: str | None = None
        # SDC identity, on the same schedule (`_hash_constraints`). None before the run or if the SDC is unreadable.
        self._constraints_sha256: str | None = None
        # Technology files `_write_script` named (`read_liberty` then `read_lef`), for the fingerprint. None until it runs.
        self._script_technology: dict | None = None
        # PDK fill cells (`filler_placement`): in the layout but not the netlist, with no power by construction. See `_unpowered_instances`.
        self._physical_only_cells: list[str] = []
        # `PARASITICS_SPEF` or `PARASITICS_ESTIMATED`, set by `_write_script` for a `netlist-source: pnr` run; None for synth.
        self._parasitics: str | None = None
        # sha256 of the PDK `layer-rc-tcl` an estimating `netlist-source: pnr` run sourced, for the config digest; None otherwise.
        self._layer_rc_sha256: str | None = None
        # What `_resolve_inputs()` returned when the script was generated (top, SDC); `_publish_phys_model` reads it instead of resolving again.
        self._script_inputs: dict | None = None
        # Hardened blocks the upstream run consumed through `blocks:`, resolved by `_resolve_inputs`; empty if none.
        self._blocks: list[pnr_abstract.ResolvedBlock] = []
        # Thread plan resolved by `_write_script`; None until it runs.
        self._thread_plan: ThreadPlan | None = None
        # Corners `_write_script` analysed, primary first, under a multi-corner platform; empty for one corner.
        self._script_corners: list[str] = []

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.tcl")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.log")

    def _report_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.rpt")

    def _corner_report_path(self, corner: str) -> str:
        """One corner's `report_power` under multi-corner."""
        return os.path.join(self.artefact_dir, f"power.{corner}.rpt")

    def _corner_report_paths_on_disk(self) -> list[str]:
        """Every `power.<corner>.rpt` currently in the artefact directory."""
        try:
            names = os.listdir(self.artefact_dir)
        except OSError:
            return []
        return [
            os.path.join(self.artefact_dir, name)
            for name in sorted(names)
            if name.startswith("power.")
            and name.endswith(".rpt")
            and name != "power.rpt"
        ]

    def _instances_report_path(self) -> str:
        """`report_power -instances` output: one line per leaf cell."""
        return os.path.join(self.artefact_dir, "power_instances.rpt")

    def _instances_cells_path(self) -> str:
        """The `<instance path> <liberty cell>` sidecar.

        `report_power` does not print each instance's master, so the hierarchy walk writes the
        mapping here; without it the model's power rows cannot be joined to the synth half.
        """
        return os.path.join(self.artefact_dir, "power_instances.cells")

    @staticmethod
    def _staging_path(published: str) -> str:
        """Return the staging name the per-instance block writes ``published`` under.

        Tcl creates a redirect target before the command runs, and the `catch` around the block
        swallows failures, so a failure part-way would leave a partial report at the published path
        that parses as complete. The block renames staging onto the published names as its last
        act; a failure leaves only the staging file, which the trailing cleanup and the next run's
        stale-clear remove.
        """
        return published + ".tmp"

    def _netlist_snapshot_path(self) -> str:
        """Return this run's private copy of the netlist it hands OpenROAD.

        The copy is hashed and read from here, so the recorded sha256 and the bytes OpenROAD parses
        are one file that no concurrent `rb synth` can rewrite. The stale-clear removes it.
        """
        return os.path.join(self.artefact_dir, "power_netlist.v")

    def _bind_phys_dir(self) -> None:
        """Point `phys_dir` at the run `phys-run:` names, or leave it at this run's directory.

        The named entry is looked up in the `synth.yaml` reached through `synth-path:`, and this
        half is published into that suite's `artefacts/<run>/`. The directory need not exist yet.
        Called from `_write_script`, so an unknown `phys-run:` fails the run; `phys_dir` then stays
        at this run's directory, the right target for the withdrawal `run()` makes.
        """
        run = self.power_cfg.get_phys_run()
        if not run:
            return
        # The config layer requires `synth`/`synth-path` for `netlist-source: synth` and refuses `phys-run` on other kinds.
        suite_path = self.power_cfg.get_synth_suite_path()
        assert suite_path is not None
        phys_dir = os.path.normpath(
            os.path.join(os.path.dirname(suite_path), "artefacts", run)
        )
        # Ask where it lands before whether the run exists, so an unloadable suite does not mask a bad `synth-path:`.
        root = project_root_or_none(self.artefact_dir)
        if root is not None and not _within(root, phys_dir):
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': phys-run "
                f"'{run}' resolves to {phys_dir}, outside the project at "
                f"{root} — `synth-path:` reaches into another checkout, so "
                "the merged model would be written where this project's "
                "`rb phys` never looks"
            )
        # Raises when the suite has no such entry, which is the check.
        self.power_cfg.resolve_phys_run_cfg()
        self.phys_dir = phys_dir

    def _source_identity(self, source: str) -> tuple[int, int]:
        """Return `(size, mtime_ns)` of the upstream netlist as a change witness.

        Not a lock: two writes in one mtime tick with the same length look identical.
        """
        st = os.stat(source)
        return (st.st_size, st.st_mtime_ns)

    def _snapshot_netlist(self) -> str | None:
        """Copy the upstream netlist into this run's directory and hash the copy, or return why not.

        Called between the stale-clear and OpenROAD. The copy goes via a `.tmp` sibling so a crash
        never leaves a short `power_netlist.v`. The source is stat'd on either side of each copy
        and the copy is retried while the stats differ, at most `_SNAPSHOT_ATTEMPTS` times, because
        a concurrent `rb synth` can rewrite it mid-copy. Exhausting the attempts fails the run. A
        `netlist-source: pnr` run has no netlist, so nothing is copied or hashed.

        :returns: ``None`` on success or when there is nothing to do, else a description of the
            failure. A failed copy fails the run because the script reads the copy.
        """
        source = self._netlist_source_path
        self._netlist_sha256 = None
        if source is None:
            return None
        snapshot = Path(self._netlist_snapshot_path())
        staging = snapshot.with_name(snapshot.name + ".tmp")
        try:
            for _attempt in range(_SNAPSHOT_ATTEMPTS):
                before = self._source_identity(source)
                shutil.copyfile(source, staging)
                if self._source_identity(source) != before:
                    # The source was rewritten mid-copy; drop the staging file and copy again.
                    continue
                # OpenROAD rejects the `#(...)` on a parameterised block's instances.
                block_params.clean_netlist(
                    str(staging), str(staging), self._blocks, where=source
                )
                os.replace(staging, snapshot)
                # Hash the copy, not the source: it is the file OpenROAD reads.
                self._netlist_sha256 = sha256_of(snapshot)
                return None
        except block_params.BlockParamError as e:
            with contextlib.suppress(OSError):
                staging.unlink()
            return str(e)
        except OSError as e:
            with contextlib.suppress(OSError):
                staging.unlink()
            return f"could not copy {source} to {snapshot}: {e}"
        with contextlib.suppress(OSError):
            staging.unlink()
        return (
            f"{source} changed underneath every one of {_SNAPSHOT_ATTEMPTS} "
            "copies, so no snapshot of it is known to be a single netlist "
            "— something is writing it concurrently (a `rb synth` of the "
            "upstream entry); re-run this power analysis once that has "
            "finished"
        )

    def _trace_path(self) -> str | None:
        """Return the activity trace this run hands OpenROAD, or ``None`` for a static run.

        A static run reads no trace, so hashing a large VCD would be wasted work.
        """
        if self.power_cfg.get_activity_source() not in TRACE_SOURCES:
            return None
        activity = self.power_cfg.get_activity()
        return activity.saif or activity.vcd

    def _hash_trace(self) -> None:
        """Hash the trace bytes as OpenROAD is launched.

        Unlike the netlist the trace is not copied, since a SAIF is megabytes and a VCD can be
        gigabytes. The hash is taken in place just before the subprocess starts, and
        `_confirm_trace_unchanged` checks it again on return.
        """
        self._trace_sha256 = sha256_of(self._trace_path())

    def _constraints_path(self) -> str | None:
        """Return the SDC this run hands OpenROAD, as `_write_script` resolved it.

        Not re-resolved: a `netlist-source: pnr` run without `constraints:` uses
        `<pnr artefact>/<top>.routed.sdc`, which a second resolution could answer differently.
        """
        return (self._script_inputs or {}).get("sdc")

    def _hash_constraints(self) -> None:
        """Hash the SDC bytes as OpenROAD is launched.

        Hashing at publication would record a replacement written during the analysis (a
        concurrent `rb pnr` rewrites `<top>.routed.sdc`; a synthesis SDC is edited by hand). The
        hash is taken in place, and `_confirm_constraints_unchanged` reads the file again on return.
        """
        self._constraints_sha256 = sha256_of(self._constraints_path())

    def _confirm_constraints_unchanged(self) -> None:
        """Withdraw the SDC hash if the file changed during the run.

        Uses :func:`~rtl_buddy.phys.publish.confirm_digest`. A mismatch records ``null`` and warns,
        so the null does not read as "this run had no constraints".
        """
        self._constraints_sha256, changed = confirm_digest(
            self._constraints_path(), self._constraints_sha256
        )
        if changed:
            log_event(
                logger,
                logging.WARNING,
                "power.constraints_changed_during_run",
                power=self.power_cfg.get_name(),
                constraints=self._constraints_path(),
            )

    def _confirm_trace_unchanged(self) -> None:
        """Withdraw the trace hash if the file changed during the run.

        `dump.saif` is rewritten in place by the next run of its test. If the re-hash differs the
        identity is unknown and is recorded as ``None``, with a warning so the null does not read as
        "no trace measured".
        """
        if self._trace_sha256 is None:
            return
        if sha256_of(self._trace_path()) == self._trace_sha256:
            return
        log_event(
            logger,
            logging.WARNING,
            "power.trace_changed_during_run",
            power=self.power_cfg.get_name(),
            trace=self._trace_path(),
        )
        self._trace_sha256 = None

    def _resolve_inputs(self) -> dict:
        """Resolve netlist / ODB / SDC paths per `netlist-source`.

        - "synth" (default): the post-synth tech-mapped netlist, with Liberty and LEF supplying the
          technology view. Switching power is under-estimated because there are no real
          parasitics or CTS clock tree.
        - "pnr": the post-P&R database (`<top>.routed.odb`) and post-CTS SDC, so
          `estimate_parasitics -global_routing` reflects the CTS clock tree and routed wires. If
          the P&R run's PDK declared `rcx-rules`, `<top>.routed.spef` is read instead of
          estimating; see `routed_spef_rejection`.

        Returns a dict with keys netlist (None for pnr), odb (None for synth), spef and pnr_script
        (None for synth), sdc, top, macro_libs, macro_lefs.

        Macro libraries are resolved here, where the upstream entry they are inherited from is at
        hand. A hard macro's Liberty reaches the upstream run through its `lib-paths`; without it
        the macro reads zero power. The `power.yaml`'s own `lib-paths` are appended after them.

        `macro_lefs` is the synthesis run's `lef-paths` on the synth path, where `link_design`
        cannot place an instance of an unseen master. It is empty on the pnr path, where `read_db`
        already holds every master and would discard a LEF read before it.

        A `blocks:` entry on the upstream run is resolved for its LEF (synth path) and for the
        result's `blocks` rows, without the staleness gate `rb pnr` and `rb synth` apply.

        A block's abstract Liberty is not read. It is `write_timing_model` output with no power
        tables and no output `function`, so OpenSTA's activity propagation cannot reach the block's
        outputs and the parent logic they drive would read as static. Without it the block is a
        Liberty-less master whose outputs take the trace's or the default activity.
        """
        # power.yaml libraries go after the inherited ones.
        own_libs = self.power_cfg.get_lib_paths()
        if self.power_cfg.get_netlist_source() == "pnr":
            pnr_cfg = self.power_cfg.resolve_pnr_cfg()
            top = pnr_cfg.resolve_synth_cfg().get_top()
            self._blocks = self._resolve_upstream_blocks(pnr_cfg.get_blocks())
            pnr_suite_path = self.power_cfg.get_pnr_suite_path()
            assert pnr_suite_path is not None
            pnr_suite_dir = os.path.dirname(pnr_suite_path)
            pnr_artefact = os.path.join(pnr_suite_dir, "artefacts", pnr_cfg.get_name())
            odb = os.path.join(pnr_artefact, f"{top}.routed.odb")
            sdc = self.power_cfg.get_constraints() or os.path.join(
                pnr_artefact, f"{top}.routed.sdc"
            )
            return {
                "netlist": None,
                "odb": odb,
                "spef": os.path.join(pnr_artefact, f"{top}{ROUTED_SPEF_SUFFIX}"),
                "pnr_script": os.path.join(pnr_artefact, PNR_SCRIPT_NAME),
                "sdc": sdc,
                "top": top,
                "macro_libs": _dedup_paths(
                    [
                        *pnr_cfg.get_lib_paths(),
                        *own_libs,
                    ]
                ),
                "macro_lefs": [],
            }

        synth_cfg = self.power_cfg.resolve_synth_cfg()
        top = synth_cfg.get_top()
        self._blocks = self._resolve_upstream_blocks(synth_cfg.get_blocks())
        synth_suite_path = self.power_cfg.get_synth_suite_path()
        assert synth_suite_path is not None
        synth_suite_dir = os.path.dirname(synth_suite_path)
        netlist = os.path.join(
            synth_suite_dir, "artefacts", synth_cfg.get_name(), "synth_netlist.v"
        )
        return {
            "netlist": netlist,
            "odb": None,
            "spef": None,
            "pnr_script": None,
            "sdc": self.power_cfg.get_constraints(),
            "top": top,
            "macro_libs": _dedup_paths(
                [
                    *synth_cfg.get_lib_paths(),
                    *own_libs,
                ]
            ),
            "macro_lefs": _dedup_paths(
                [*synth_cfg.get_lef_paths(), *(b.lef for b in self._blocks)]
            ),
        }

    def _resolve_upstream_blocks(self, refs) -> list[pnr_abstract.ResolvedBlock]:
        """Return the upstream run's `blocks:`, each resolved to its abstract.

        A block whose abstract is missing fails the run at setup, naming the block, like a missing
        macro Liberty (`power.missing_macro_inputs`).
        """
        if not refs:
            return []
        try:
            return pnr_abstract.resolve_blocks(refs)
        except pnr_abstract.BlockResolutionError as e:
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': upstream {e}"
            ) from None

    def _blocks_fields(self) -> dict:
        """Return the `blocks` result field: the abstracts this run read, without the staleness that `rb pnr` and `rb synth` report."""
        rows = []
        for block in self._blocks:
            row = block.result_row()
            row.pop("stale", None)
            row.pop("changes", None)
            rows.append(row)
        return {"blocks": rows} if rows else {}

    def _upstream_identity(self) -> dict:
        """Return which upstream run this analysis read, for the config digest.

        `netlist_source` names only the kind ("synth" or "pnr"). A synth run uses the sha256 of the
        netlist copy it measured, so entries that resolve byte-identical netlists count as one
        experiment. A pnr run hashes nothing large, so the ODB's path stands in for its contents.
        Paths are project-relative so the digest does not change with the checkout; `_publish`
        cannot relativise this one because the options are digested first. Unknown values stay
        ``null`` (the rule :func:`options_digest` keeps).
        """
        if self.power_cfg.get_netlist_source() != "pnr":
            return {"netlist_sha256": self._netlist_sha256, "input_path": None}
        # Use the capture `_write_script` took: it names the database OpenROAD was given.
        odb = (self._script_inputs or {}).get("odb")
        identity = {
            "netlist_sha256": None,
            "input_path": (
                project_relative(odb, project_root_for_dir(self.artefact_dir))
                if odb
                else None
            ),
        }
        # One ODB timed on its extracted SPEF and on the global-route estimate is two measurements.
        # Only the SPEF case adds the key.
        if self._parasitics == "spef":
            identity["parasitics"] = self._parasitics
        # The estimate depends on the layer RC script's contents; only a run that sourced one adds the key.
        if self._layer_rc_sha256 is not None:
            identity["layer_rc_tcl_sha256"] = self._layer_rc_sha256
        return identity

    def _resolve_platform(self):
        """Resolve to a PnrPlatformConfig (provides Liberty path)."""
        return self.root_cfg.get_pnr_platform_cfg(self.power_cfg.get_platform())

    def _emit_activity_cmds(self) -> list[str]:
        """Translate the resolved activity source (`PowerConfig.get_activity_source()`) into `read_saif`, `read_power_activities` or `set_power_activity` Tcl."""
        source = self.power_cfg.get_activity_source()
        activity = self.power_cfg.get_activity()
        if source == "saif":
            scope_arg = f" -scope {activity.scope}" if activity.scope else ""
            return [f"read_saif{scope_arg} {activity.saif}"]
        if source == "vcd":
            scope_arg = f" -scope {activity.scope}" if activity.scope else ""
            return [f"read_power_activities{scope_arg} -vcd {activity.vcd}"]
        if source == "synthetic":
            # `-global` alone leaves primary inputs with no activity. OpenSTA then reports inf/NaN
            # power downstream of any input it does not time (a false-pathed one, say), so the
            # inputs take the same rate. The options digest marks it (`input_activity`), so rows
            # from before this change are not taken for the same experiment.
            rate = f"-activity {activity.default_toggle_rate} -duty {activity.default_static_prob}"
            return [
                f"set_power_activity -global {rate}",
                f"set_power_activity -input {rate}",
            ]
        return []  # "default" → static, no activity commands

    # Printed between the design-total `report_power` and the per-instance block; `_fatal_log_region` fails the run only on errors before it.
    _DETAIL_MARKER = "RB_PHYS_DETAIL_BEGIN"

    def _emit_per_instance_cmds(self, multi_corner: bool = False) -> list[str]:
        """Return Tcl that attributes the run's power to individual leaf instances.

        - One `report_power -instances` call takes the list of instances, so the whole design costs
          one extra analysis; a `foreach` over instances would rerun propagation per cell.
        - `get_cells -hierarchical *` returns leaf cells only; roll-up to modules is the model
          consumer's job.
        - The block cannot fail the run: it is inside a `catch` because the design totals are
          already written. It opens with `_DETAIL_MARKER` because an OpenSTA that rejects
          `-instances` logs an `[ERROR ...]` before raising, and `_fatal_log_region` scans only
          what precedes the marker.
        - Both files are written under staging names (`_staging_path`) and renamed onto the
          published names as the last commands, so a swallowed failure publishes nothing. Trailing
          deletes clear leftover staging files.
        - Under multi-corner the rows are the worst corner's (`rb_power_corner`), matching the
          reported totals.
        """
        corner_arg = " -corner $rb_power_corner" if multi_corner else ""
        instances = self._instances_report_path()
        cells = self._instances_cells_path()
        instances_tmp = self._staging_path(instances)
        cells_tmp = self._staging_path(cells)
        return [
            f'puts "{self._DETAIL_MARKER}"',
            "catch {",
            "  set rb_insts [get_cells -hierarchical *]",
            "  if {[llength $rb_insts] > 0} {",
            f"    set rb_fh [open {cells_tmp} w]",
            "    foreach rb_inst $rb_insts {",
            '      puts $rb_fh "[get_full_name $rb_inst] '
            '[get_property $rb_inst ref_name]"',
            "    }",
            "    close $rb_fh",
            f"    report_power -instances $rb_insts{corner_arg} > {instances_tmp}",
            # Reached only if everything above succeeded: publish by rename.
            f"    file rename -force {cells_tmp} {cells}",
            f"    file rename -force {instances_tmp} {instances}",
            "  }",
            "}",
            f"catch {{file delete -force {cells_tmp}}}",
            f"catch {{file delete -force {instances_tmp}}}",
        ]

    def _write_script(self) -> str:
        # First: an unresolvable `phys-run:` is a config error, and the caller's withdrawal needs the publish directory.
        self._bind_phys_dir()
        platform = self._resolve_platform()
        pdk = platform.get_pdk()
        liberties = platform.get_sta_lib_paths()
        # Every corner of a multi-corner platform in this one session; empty for a single corner.
        corner_libs = (
            platform.get_sta_corner_lib_paths() if platform.is_multi_corner() else {}
        )
        self._script_corners = list(corner_libs)
        tech_lef = pdk.get_tech_lef()
        macro_lef = pdk.get_macro_lef()
        inputs = self._resolve_inputs()
        # The script is generated from these; `_publish_phys_model` reads this capture instead of resolving again.
        self._script_inputs = inputs
        macro_libs = list(inputs.get("macro_libs") or [])
        macro_lefs = list(inputs.get("macro_lefs") or [])
        # Not in the script: resolved with the platform so the fill-cell detection judges the PDK the run was prepared against.
        self._physical_only_cells = list(pdk.get_fill_cells() or [])
        # The technology the `read_liberty` / `read_lef` lines name, in order, for `_phys_technology`.
        self._script_technology = {
            "liberty": liberties,
            "corner_libs": [lib for libs in corner_libs.values() for lib in libs],
            "macro_libs": macro_libs,
            "tech_lef": tech_lef,
            "macro_lef": macro_lef,
            "macro_lefs": macro_lefs,
        }
        netlist = inputs["netlist"]
        sdc = inputs["sdc"]
        odb = inputs["odb"]
        top = inputs["top"]
        source = self.power_cfg.get_netlist_source()

        if not sdc:
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': "
                "constraints (SDC path) is required"
            )
        if not tech_lef:
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': "
                f"pdk '{pdk.get_name()}' has no tech-lef configured"
            )
        # Check every configured macro input before launching OpenROAD, at ERROR: a missing `read_liberty` path otherwise gives a log nobody reads and a macro at zero watts.
        missing = [path for path in macro_libs + macro_lefs if not os.path.isfile(path)]
        if missing:
            log_event(
                logger,
                logging.ERROR,
                "power.missing_macro_inputs",
                power=self.power_cfg.get_name(),
                count=len(missing),
                missing=missing,
            )
            raise RuntimeError(
                f"power run '{self.power_cfg.get_name()}': "
                f"{len(missing)} configured macro input(s) not on disk: "
                + ", ".join(missing)
            )
        if source == "pnr":
            if not os.path.isfile(odb):
                raise RuntimeError(
                    f"power run '{self.power_cfg.get_name()}': "
                    f"routed ODB not found at {odb} — re-run `rb pnr` "
                    "(older runs predate the .routed.odb artefact)"
                )
        else:
            if not os.path.isfile(netlist):
                raise RuntimeError(
                    f"power run '{self.power_cfg.get_name()}': "
                    f"upstream netlist not found at {netlist} — run `rb synth` first"
                )
            # The script reads this run's own copy, written by `_snapshot_netlist` after the stale-clear. Recorded here so that step knows what to copy.
            self._netlist_source_path = netlist

        # PDK Tcl hooks this session sources; check them now so a missing one fails at setup, not mid-run.
        platform_tcl = pdk.get_platform_tcl()
        layer_rc_tcl = pdk.get_layer_rc_tcl() if source == "pnr" else ""
        for key, path in (
            ("platform-tcl", platform_tcl),
            ("layer-rc-tcl", layer_rc_tcl),
        ):
            if path and not os.path.isfile(path):
                log_event(
                    logger,
                    logging.ERROR,
                    "power.tcl_hook_missing",
                    power=self.power_cfg.get_name(),
                    key=key,
                    path=path,
                )
                raise RuntimeError(f"{key} not found: {path}")

        # Ahead of the first `read_liberty`; absent when `threads:` is unset.
        self._thread_plan = plan_threads(
            self.power_cfg.get_threads(), flow="power", run=self.power_cfg.get_name()
        )
        threads_tcl = self._thread_plan.tcl()
        lines = ["# Generated by rtl_buddy power flow"]
        if threads_tcl:
            lines.append(threads_tcl)
        # Before the Liberty reads, as in `rb pnr`, so its `suppress_message` covers them.
        if platform_tcl:
            lines.append(tcl_source(platform_tcl))
        if corner_libs:
            # Each macro library is read into every corner, after the standard cells; see `openroad_corners.liberty_tcl`.
            lines.extend(openroad_corners.liberty_tcl(corner_libs, macro_libs))
        else:
            lines.extend(f"read_liberty {lib}" for lib in liberties)
            # After the platform corner so a macro library never shadows a standard cell, in resolved order.
            lines.extend(f"read_liberty {lib}" for lib in macro_libs)
        lines.append(f"read_lef {tech_lef}")
        if macro_lef:
            lines.append(f"read_lef {macro_lef}")
        lines.extend(f"read_lef {lef}" for lef in macro_lefs)
        if source == "pnr":
            # The ODB restores the post-route state. Wire parasitics come from the P&R run's extracted SPEF
            # when it is trusted, otherwise `estimate_parasitics` uses the global routes.
            spef = self._choose_parasitics(inputs)
            lines.append(f"read_db {odb}")
            lines.append(f"read_sdc {sdc}")
            # Layer RC is session state, not stored in the ODB; the estimate below needs it as `rb pnr` had it.
            self._layer_rc_sha256 = None
            if layer_rc_tcl:
                lines.append(tcl_source(layer_rc_tcl))
                if spef is None:
                    self._layer_rc_sha256 = sha256_of(layer_rc_tcl)
            if spef is not None:
                lines.append(f"read_spef {spef}")
            else:
                lines.append("estimate_parasitics -global_routing")
        else:
            lines.extend(
                [
                    f"read_verilog {self._netlist_snapshot_path()}",
                    f"link_design {top}",
                    f"read_sdc {sdc}",
                ]
            )
        lines.extend(self._emit_activity_cmds())
        if corner_libs:
            # One report per corner, then `power.rpt` at the worst, so the per-instance rows are of the same corner.
            lines.extend(
                openroad_corners.power_report_tcl(
                    list(corner_libs), self._corner_report_path, self._report_path()
                )
            )
        else:
            lines.append(f"report_power > {self._report_path()}")
        lines.extend(self._emit_per_instance_cmds(multi_corner=bool(corner_libs)))
        lines.append("exit")
        lines.append("")

        script_path = self._script_path()
        Path(script_path).write_text("\n".join(lines))
        return script_path

    def _choose_parasitics(self, inputs: dict) -> str | None:
        """Return the routed SPEF to read, or `None` to estimate, and log which.

        Sets `_parasitics`. A SPEF that exists but is refused logs a WARNING naming why, since the
        user configured extraction and gets the estimate.
        """
        spef = inputs.get("spef")
        script = inputs.get("pnr_script")
        reason = (
            routed_spef_rejection(spef, script) if spef and script else "no routed SPEF"
        )
        if reason is None:
            self._parasitics = PARASITICS_SPEF
        else:
            self._parasitics = PARASITICS_ESTIMATED
            if spef and os.path.isfile(spef):
                log_event(
                    logger,
                    logging.WARNING,
                    "power.spef_rejected",
                    power=self.power_cfg.get_name(),
                    spef=spef,
                    reason=reason,
                )
        log_event(
            logger,
            logging.INFO,
            "power.parasitics",
            power=self.power_cfg.get_name(),
            parasitics=self._parasitics,
            spef=spef if reason is None else None,
        )
        return spef if reason is None else None

    # report_power output for the Total line looks like:
    #   Total                 1.50e-04   2.30e-05   8.00e-06   1.81e-04
    _TOTAL_LINE_RE = re.compile(
        r"^\s*Total\s+"
        r"([-\d.eE+]+)\s+"  # internal
        r"([-\d.eE+]+)\s+"  # switching
        r"([-\d.eE+]+)\s+"  # leakage
        r"([-\d.eE+]+)",  # total
        re.MULTILINE,
    )

    def _parse_report(self, report_text: str) -> dict | None:
        m = self._TOTAL_LINE_RE.search(report_text)
        if not m:
            return None
        try:
            return {
                "internal_w": float(m.group(1)),
                "switching_w": float(m.group(2)),
                "leakage_w": float(m.group(3)),
                "total_w": float(m.group(4)),
            }
        except ValueError:
            return None

    def _clear_stale_report(self) -> str | None:
        """Remove the previous run's `power.rpt`, per-instance reports and netlist snapshot.

        An OpenROAD that exits 0 without reaching the `catch` block must not leave the last run's
        watts to be published as this one's. Call before `_snapshot_netlist`, never after. This
        flow's half of the phys model is nulled out in `phys_dir` (which may differ from the
        directory cleared here); the model and manifest stay because a synthesis may have merged
        its half into them.

        :returns: ``None``, or the reason the withdrawal failed; every caller turns that into a
            failed run. See :func:`~rtl_buddy.phys.publish.withdrawal_failure_desc`.
        """
        stale = clear_stale_artefacts(
            [
                self._report_path(),
                self._instances_report_path(),
                self._instances_cells_path(),
                # Staging names the per-instance block writes under; an OpenROAD killed inside it leaves them behind.
                self._staging_path(self._instances_report_path()),
                self._staging_path(self._instances_cells_path()),
                self._netlist_snapshot_path(),
                # Every per-corner report, by pattern rather than configured corners: the platform may have lost a corner.
                *self._corner_report_paths_on_disk(),
            ],
            owner=self.power_cfg.get_name(),
        )
        if stale:
            log_event(
                logger,
                logging.DEBUG,
                "power.stale_artefacts_removed",
                power=self.power_cfg.get_name(),
                paths=stale,
            )
        return self._invalidate_phys_half()

    def _invalidate_phys_half(self) -> str | None:
        """Null this flow's half of the model and manifest it publishes; the synthesis half is untouched.

        Called from the clear so a run that never publishes withdraws the previous run's rows. A
        successful withdrawal logs at DEBUG; a failed one warns and is returned for the caller to
        fail on.

        :returns: ``None`` on success, else `invalidate_half`'s ``error``.
        """
        result = invalidate_half(self.phys_dir, "instances")
        if result["error"]:
            log_event(
                logger,
                logging.WARNING,
                "power.phys_half_stale",
                power=self.power_cfg.get_name(),
                error=result["error"],
            )
            return result["error"]
        if result["model"] or result["manifest"]:
            log_event(
                logger,
                logging.DEBUG,
                "power.phys_half_invalidated",
                power=self.power_cfg.get_name(),
                model=result["model"],
                manifest=result["manifest"],
            )
        return None

    def _read_if_present(self, path: str) -> str | None:
        try:
            return Path(path).read_text()
        except OSError:
            return None

    def _unpowered_instances(self) -> dict:
        """Return the instances the analysis could say nothing about, and their cells.

        A hard macro whose Liberty never reached the run reports 0.00e+00 in every column, which
        looks like a measurement. Detection reads the two reports this run produced instead of
        asking OpenSTA, so the generated script is unchanged. An instance is reported only if all
        hold:

        - its master is not declared in any Liberty the script read (:func:`_liberty_cell_names`),
          which keeps a genuinely zero-power cell such as an unclocked flop out;
        - its total power is exactly zero, so a Liberty spelling the scan misses cannot flag a whole
          library;
        - its master is not one of the PDK's fill cells, which `filler_placement` adds to the routed
          database and which have no Liberty or power by construction.

        :returns: ``{"cells": [...], "instances": int}``: sorted master names and the instance
            count. Empty when there is nothing to report, including when either report is absent
            (`power.phys_model_incomplete` reports that).
        """
        empty: dict = {"cells": [], "instances": 0}
        instances_text = self._read_if_present(self._instances_report_path())
        cells_text = self._read_if_present(self._instances_cells_path())
        if not instances_text or not cells_text:
            return empty
        cells = reports.parse_instance_cells(cells_text)
        physical_only = set(self._physical_only_cells)
        rows = [
            row
            for row in reports.parse_instance_power(instances_text, cells)
            if row.get("module")
            and row.get("total_uw") == 0.0
            and str(row["module"]) not in physical_only
        ]
        if not rows:
            # Skip the Liberty scan when no instance has zero power.
            return empty
        technology = self._script_technology or {}
        # A block's abstract Liberty is not read, but a `power.yaml` `lib-paths` can name one. It has no
        # power tables, so it cannot vouch for a zero-watt instance; exclude it so the block is still flagged.
        block_libs = {os.path.abspath(b.lib) for b in self._blocks}
        known = _liberty_cell_names(
            [
                path
                for path in [
                    *(technology.get("liberty") or []),
                    *(technology.get("macro_libs") or []),
                ]
                if path and os.path.abspath(path) not in block_libs
            ]
        )
        unpowered = [row for row in rows if str(row["module"]) not in known]
        if not unpowered:
            return empty
        return {
            "cells": sorted({str(row["module"]) for row in unpowered}),
            "instances": len(unpowered),
        }

    def _fatal_log_region(self, log_text: str) -> str:
        """Return the part of `power.log` whose `[ERROR ...]` lines fail the run.

        Output after `_DETAIL_MARKER` belongs to the per-instance by-product, so an error there
        (e.g. an OpenSTA without `report_power -instances`) costs the model its `instances` half
        but not the parsed totals. A log without the marker is scanned whole.
        """
        marker_at = log_text.find(self._DETAIL_MARKER)
        return log_text if marker_at < 0 else log_text[:marker_at]

    def _fail_after_openroad(self, desc: str) -> PowerFailResults:
        """Fail a run that has already invoked OpenROAD, leaving no report published.

        `power.rpt` can be on disk even when OpenROAD exits non-zero or logs an `[ERROR ...]`, so
        every post-OpenROAD failure return goes through here to clear it. A withdrawal that could
        not be made is added to the failure description.
        """
        stale_error = self._clear_stale_report()
        if stale_error is not None:
            desc = f"{desc}; {withdrawal_failure_desc(stale_error)}"
        return self._with_threads(
            PowerFailResults(name=self.name + "/results", desc=desc)
        )

    def _with_threads(self, res: PowerResults) -> PowerResults:
        """Record OpenROAD thread provenance on ``res``, once OpenROAD ran on a script carrying the plan.

        The thread count OpenROAD logged wins over the one requested.
        """
        if self._thread_plan is not None:
            try:
                reported = parse_reported_threads(Path(self._log_path()).read_text())
            except OSError:
                reported = None
            res.results["openroad_threads"] = self._thread_plan.fields(reported)
        return res

    def run(self) -> PowerResults:
        log_event(
            logger,
            logging.INFO,
            "power.start",
            power=self.power_cfg.get_name(),
            tool=self.executable,
            mode=self.power_cfg.get_mode(),
            netlist_source=self.power_cfg.get_netlist_source(),
        )

        # Before the "openroad not found" return: `_write_script` validates the configuration (SDC,
        # tech-lef, upstream netlist or ODB), and a bad config must not be reported as a missing tool.
        # A config error is a failed run and clears on the way out.
        try:
            script_path = self._write_script()
        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "power.script_failed",
                power=self.power_cfg.get_name(),
                error=str(e),
            )
            stale_error = self._clear_stale_report()
            desc = f"script generation error: {e}"
            if stale_error is not None:
                desc = f"{desc}; {withdrawal_failure_desc(stale_error)}"
            return PowerFailResults(
                name=self.name + "/results", desc=desc, fail_stage="setup"
            )

        if not shutil.which(self.executable):
            log_event(
                logger,
                logging.WARNING,
                "power.no_openroad",
                power=self.power_cfg.get_name(),
                exe=self.executable,
            )
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"{self.executable!r} not found",
                fail_stage="setup",
            )

        # Everything past the "openroad not found" return is a run of this entry, however it ends. The
        # report is read from a fixed path, so clear it here or a run that does not rewrite it would
        # quote the previous watts. If the withdrawal fails, OpenROAD does not start.
        stale_error = self._clear_stale_report()
        if stale_error is not None:
            return PowerFailResults(
                name=self.name + "/results",
                desc=withdrawal_failure_desc(stale_error),
                fail_stage="setup",
            )

        # After the clear, before OpenROAD: the script names this run's copy of the netlist, and the recorded hash is of that copy.
        snapshot_error = self._snapshot_netlist()
        if snapshot_error is not None:
            log_event(
                logger,
                logging.WARNING,
                "power.netlist_snapshot_failed",
                power=self.power_cfg.get_name(),
                error=snapshot_error,
            )
            return PowerFailResults(
                name=self.name + "/results",
                desc=f"could not stage the netlist for OpenROAD: {snapshot_error}",
                fail_stage="setup",
            )

        log_path = self._log_path()
        # OpenROAD's `-log` truncates only once running; remove the old log so an earlier launch failure does not leave the previous ORD-0030 thread count.
        try:
            os.unlink(log_path)
        except FileNotFoundError:
            pass
        env = os.environ.copy()
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

        cmd = [
            self.executable,
            "-no_init",
            "-exit",
            "-log",
            log_path,
            script_path,
        ]
        log_event(
            logger,
            logging.DEBUG,
            "power.run_cmd",
            power=self.power_cfg.get_name(),
            cmd=" ".join(cmd),
        )

        # Last step before the subprocess, to narrow the window between the hashes and OpenROAD's reads.
        self._hash_trace()
        self._hash_constraints()

        with task_status(f"power {self.power_cfg.get_name()} [openroad]"):
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
            )

        if result.returncode != 0:
            log_event(
                logger,
                logging.WARNING,
                "power.failed",
                power=self.power_cfg.get_name(),
                returncode=result.returncode,
                log=log_path,
            )
            return self._fail_after_openroad(
                f"OpenROAD exited with code {result.returncode}"
            )

        try:
            log_text = Path(log_path).read_text()
        except OSError:
            log_text = ""

        error_lines = [
            ln
            for ln in self._fatal_log_region(log_text).splitlines()
            if ln.startswith("[ERROR ")
        ]
        if error_lines:
            return self._fail_after_openroad(
                f"{len(error_lines)} ERROR(s) in OpenROAD log"
            )

        report_path = self._report_path()
        if not os.path.isfile(report_path):
            return self._fail_after_openroad(
                f"power report not produced at {report_path}"
            )

        try:
            report_text = Path(report_path).read_text()
        except OSError as e:
            return self._fail_after_openroad(f"failed to read power report: {e}")

        parsed = self._parse_report(report_text)
        if parsed is None:
            return self._fail_after_openroad(
                "could not parse Total line from report_power output"
            )

        corner_fields: dict = {}
        if self._script_corners:
            corner_fields, corner_error = self._parse_corner_reports(log_text)
            if corner_error is not None:
                return self._fail_after_openroad(corner_error)

        # Before the pass is announced so the warning reads above the numbers. It never changes the verdict: the reported watts are real for everything that had a library.
        unpowered = self._unpowered_instances()
        if unpowered["cells"]:
            log_event(
                logger,
                logging.WARNING,
                "power.unpowered_instances",
                power=self.power_cfg.get_name(),
                count=unpowered["instances"],
                cells=unpowered["cells"],
            )

        activity_source = self.power_cfg.get_activity_source()
        log_event(
            logger,
            logging.INFO,
            "power.passed",
            power=self.power_cfg.get_name(),
            mode=self.power_cfg.get_mode(),
            activity_source=activity_source,
            parasitics=self._parasitics,
            total_w=parsed["total_w"],
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            worst_corner=corner_fields.get("worst_corner"),
            log=log_path,
            report=report_path,
        )
        phys_model = self._publish_phys_model(
            parsed, worst_corner=corner_fields.get("worst_corner")
        )
        passed = PowerPassResults(
            name=self.name + "/results",
            mode=self.power_cfg.get_mode(),
            netlist_source=self.power_cfg.get_netlist_source(),
            total_w=parsed["total_w"],
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            activity_source=activity_source,
            parasitics=self._parasitics,
            phys_model=phys_model,
            unpowered_cells=unpowered["cells"],
            unpowered_instance_count=unpowered["instances"],
            worst_corner=corner_fields.get("worst_corner"),
            corners=corner_fields.get("corners"),
        )
        passed.results.update(self._blocks_fields())
        return self._with_threads(passed)

    def _parse_corner_reports(self, log_text: str) -> tuple[dict, str | None]:
        """Return per-corner totals and the worst corner of a multi-corner run as ``(fields, error)``.

        `power.rpt` is the worst corner's report, printed after `POWER_CORNER_MARKER`; each corner's
        own report is `power.<corner>.rpt`. A missing or unparsable corner report fails the run,
        since fewer corners would read as a signoff over corners never seen.
        """
        worst = openroad_corners.parse_power_corner(self._fatal_log_region(log_text))
        if worst not in self._script_corners:
            return {}, "could not determine the worst power corner from the log"
        corners: dict[str, dict] = {}
        for corner in self._script_corners:
            path = self._corner_report_path(corner)
            try:
                parsed = self._parse_report(Path(path).read_text())
            except OSError:
                return {}, f"power report for corner '{corner}' not produced at {path}"
            if parsed is None:
                return {}, (
                    f"could not parse Total line from corner '{corner}' "
                    "report_power output"
                )
            corners[corner] = parsed
        return {"worst_corner": worst, "corners": corners}, None

    def _phys_technology(self) -> list[str]:
        """Return the Liberty and LEF the generated script reads, as identity for the fingerprint.

        The platform name alone does not determine them. Paths, not contents, are digested (a
        Liberty is tens of MB), project-relative and in script order because `read_liberty` and
        `read_lef` are order-sensitive. Read from the capture `_write_script` took, not resolved
        again. Macro libraries and LEFs are included where the script reads them, since a run with a
        macro Liberty and one without report different watts.
        """
        resolved = self._script_technology or {}
        named = [
            *(resolved.get("liberty") or []),
            # Every corner's Liberty under multi-corner, the primary's included; empty (so absent from the digest) for one corner.
            *(resolved.get("corner_libs") or []),
            *(resolved.get("macro_libs") or []),
            resolved.get("tech_lef"),
            resolved.get("macro_lef"),
            *(resolved.get("macro_lefs") or []),
        ]
        return library_fingerprint([path for path in named if path], self.root_cfg)

    def _publish_phys_model(
        self, parsed: dict, *, worst_corner: str | None = None
    ) -> str | None:
        """Write `phys-model.json` and its manifest for a passing run.

        Written into `phys_dir`, this run's directory unless `phys-run:` names a synthesis. The
        manifest's report paths still point into this run's directory, project-relative.

        Never fails the power analysis: a missing or garbled per-instance report costs the model its
        `instances` half and a warning.

        Identity recorded:

        - The top and SDC come from the resolution `_write_script` generated from, not from a second
          resolution or the config's `constraints:` field. A `netlist-source: pnr` run with no
          explicit `constraints:` uses the router's `<top>.routed.sdc`.
        - The netlist is identified by the hash `_snapshot_netlist` took of the private copy, which
          gates merging module rows already in the directory. The manifest also records the copy's
          location. A pnr run records none and inherits nothing.
        - The mode and activity source are the resolved values the Tcl dispatched on. The trace and
          SDC hashes are those taken at launch (`_hash_trace`, `_hash_constraints`) and confirmed on
          return, not re-read here.
        """
        self._confirm_trace_unchanged()
        self._confirm_constraints_unchanged()
        inputs = self._script_inputs or {}
        activity = self.power_cfg.get_activity()
        source = self.power_cfg.get_activity_source()
        trace = activity.saif or activity.vcd
        published = publish_power(
            artefact_dir=self.phys_dir,
            top=inputs.get("top"),
            backend="openroad",
            run=self.power_cfg.get_name(),
            netlist_source=self.power_cfg.get_netlist_source(),
            netlist_sha256=self._netlist_sha256,
            # The snapshot, not the upstream path: only the copy is guaranteed to be the hashed bytes. Null exactly when the hash is (`netlist-source: pnr`).
            netlist_path=(
                self._netlist_snapshot_path()
                if self._netlist_sha256 is not None
                else None
            ),
            mode=self.power_cfg.get_mode(),
            activity=activity_block(
                source=source,
                trace=trace,
                # `_hash_trace`'s value, not re-read now. None where no trace was read or it changed underneath the run.
                trace_sha256=self._trace_sha256,
                scope=activity.scope,
                toggle_rate=activity.default_toggle_rate,
                duty=activity.default_static_prob,
            ),
            platform=self.power_cfg.get_platform(),
            constraints=inputs.get("sdc"),
            constraints_sha256=self._constraints_sha256,
            options={
                "tool": self.power_cfg.get_tool_name(),
                "netlist_source": self.power_cfg.get_netlist_source(),
                # The platform name alone does not determine the Liberty and LEF.
                "technology": self._phys_technology(),
                # Which upstream run, not just its kind; see `_upstream_identity`.
                **self._upstream_identity(),
                "mode": self.power_cfg.get_mode(),
                "activity_source": source,
                # Synthetic activity also drives the primary inputs (#714); earlier runs left them idle.
                **({"input_activity": True} if source == "synthetic" else {}),
                # Under a multi-corner platform the watts and rows are the worst corner's; say which. Absent for one corner.
                **({"corner": worst_corner} if worst_corner else {}),
                "reglvl": self.power_cfg.get_reglvl(self.power_cfg.get_tool_name()),
                # `tool_overrides` is deliberately absent: nothing in this backend reads it, so digesting it would
                # tell identical analyses apart and imply it was applied. Add it if it is ever wired in.
            },
            report_path=self._report_path(),
            instances_path=self._instances_report_path(),
            cells_path=self._instances_cells_path(),
            log_path=self._log_path(),
            internal_w=parsed["internal_w"],
            switching_w=parsed["switching_w"],
            leakage_w=parsed["leakage_w"],
            total_w=parsed["total_w"],
        )
        if published["error"] is not None or published["rows"] is None:
            log_event(
                logger,
                logging.WARNING,
                "power.phys_model_incomplete",
                power=self.power_cfg.get_name(),
                instances=self._instances_report_path(),
                error=published["error"],
            )
        if self.power_cfg.get_phys_run() and published["paired"] is False:
            # Only under `phys-run:`. The netlist hashes say the module rows already there were counted off
            # a different netlist, so the gate dropped them and the model is this half alone. Not a
            # failure: the watts are sound and re-running the synthesis pairs them.
            log_event(
                logger,
                logging.WARNING,
                "power.phys_pair_mismatch",
                power=self.power_cfg.get_name(),
                phys_run=self.power_cfg.get_phys_run(),
                phys_dir=self.phys_dir,
            )
        return published["model"]
