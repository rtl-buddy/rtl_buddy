import contextlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many times `_snapshot_netlist` re-copies a netlist that changed
#: underneath it before giving up. Three is enough to ride out a single
#: upstream rewrite landing at an unlucky moment and short enough that a
#: writer rewriting the file in a loop fails the run rather than pinning
#: it (#560).
_SNAPSHOT_ATTEMPTS = 3

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
from . import openroad_corners
from .artifact_paths import clear_stale_artefacts
from .pnr_openroad import PNR_SCRIPT_NAME, ROUTED_SPEF_SUFFIX
from .power_base import BasePower
from .synth_yosys import library_fingerprint


def _within(root: str, path: str) -> bool:
    """Is ``path`` inside ``root``, by either spelling of the pair?

    Logical first, for the reason
    :func:`~rtl_buddy.phys.manifest.project_relative` gives: a suite
    whose ``artefacts/`` is a link to scratch storage is an ordinary
    layout, and its run directories are inside the project *as the
    project is read* even though they resolve out of it. The resolved
    comparison is the second chance, for the reverse arrangement — a
    project reached through a link the candidate path is not.
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
    """Why the routed SPEF beside a P&R result may not be read, or `None`.

    `rb pnr` clears `<top>.routed.spef` before every run and on every
    failure, so a SPEF on disk is normally the one the run that wrote the
    ODB extracted (#101). "Normally" is not enough to time a design on: an
    rtl_buddy that predates the SPEF does not know to clear it, so a rerun
    under one leaves a fresh ODB beside the previous run's SPEF, and a
    copied or restored artefact directory can pair any two files.

    So the SPEF has to be vouched for by the P&R run's own flow script,
    which every rtl_buddy writes afresh at the start of every run: the
    script must contain a `write_spef` command — the run that produced the
    ODB was configured to extract — and the SPEF must be no older than the
    script, i.e. written by that run rather than an earlier one. mtime,
    not content, because the script is the run's first write and the SPEF
    one of its last, minutes apart on any real design.
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
    """The paths in first-named order, one entry per file, empties dropped.

    De-duplication is on the resolved path, as the P&R backend's own
    stream-out inputs de-duplicate: a macro Liberty that a `power.yaml`
    repeats after inheriting it from the run it reads is one library, and
    `read_liberty` on the same file twice makes OpenSTA re-register every
    cell in it and warn about each one.

    Order is stable and deterministic because `read_liberty` is: two
    libraries that define a cell of the same name resolve to whichever
    was read first, so a set here would make the analysis depend on hash
    ordering (#627).
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


#: A Liberty ``cell (NAME) {`` declaration, and the two-line spelling of
#: it that generated libraries use. Scanned line by line rather than
#: parsed, for the reason
#: :meth:`~rtl_buddy.tools.synth_openroad.OpenRoadSynth._masters_from_lef_and_liberty`
#: gives: a standard-cell Liberty runs to tens of megabytes and only the
#: declaration lines matter here.
_LIBERTY_CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?([^"\s()]+)"?\s*\)')
_LIBERTY_CELL_OPEN_RE = re.compile(r"^\s*cell\s*$")
_LIBERTY_CELL_NAME_RE = re.compile(r'^\s*\(\s*"?([^"\s()]+)"?\s*\)')


def _liberty_cell_names(paths) -> set[str]:
    """Every cell name the given Liberty files declare.

    The synthesis backend's equivalent scans LEF as well, because what it
    asks is "does OpenROAD have a *master* for this name". This one asks
    the narrower question the power flow cares about — is there a library
    cell with power data behind this master — and a `MACRO` in a LEF is
    exactly the case that answers no (#627).

    A file that cannot be read contributes nothing. That is the safe
    direction: a name this fails to find is reported as unpowered, which
    is a warning naming a real instance, where a name it wrongly found
    would restore the silence the issue is about.
    """
    names: set[str] = set()
    for path in paths:
        try:
            with open(path) as f:
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
        except OSError:
            continue
    return names


class OpenRoadPower(BasePower):
    """OpenROAD-driven power-analysis backend.

    Reads the upstream `rb synth` artefact (tech-mapped netlist) together
    with the platform Liberty + tech/macro LEFs + SDC, applies a
    switching-activity model (synthetic global activity, SAIF file, or
    VCD file), and parses OpenROAD's `report_power` output for
    total/internal/switching/leakage.

    LEF is required even though `report_power` itself only needs Liberty
    — OpenROAD's gate-level `read_verilog` builds an in-memory database
    that requires a technology view (`[ERROR ORD-2010] no technology has
    been read.` otherwise).
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
        # Where `phys-model.json` and its manifest go. Every other file
        # this flow writes — the script, the log, the two reports, the
        # netlist copy — stays in `artefact_dir`, because they are this
        # run's raw output and the model is the one document two runs
        # share. Rebound by `_bind_phys_dir` when `phys-run:` names a
        # synthesis to publish beside (#589); until then, and for a
        # config that says nothing, the two are the same directory.
        self.phys_dir = self.artefact_dir
        # The upstream netlist this run measures, and the hash of the
        # private copy OpenROAD is actually given; see
        # `_snapshot_netlist`. Both `None` until the run resolves them,
        # and for a `netlist-source: pnr` run that reads a routed
        # database and never a netlist at all.
        self._netlist_source_path: str | None = None
        self._netlist_sha256: str | None = None
        # The activity trace's identity, taken as OpenROAD is launched
        # and confirmed when it returns; see `_hash_trace`. `None` both
        # before the run and for a static run that reads no trace.
        self._trace_sha256: str | None = None
        # The SDC's identity, on the same schedule as the trace's and for
        # the same reason; see `_hash_constraints`. `None` before the run
        # and for a run whose SDC could not be read.
        self._constraints_sha256: str | None = None
        # The technology files `_write_script` named, in the order it
        # named them: `read_liberty` then `read_lef`. Captured there
        # because the fingerprint has to be of what the script read, and
        # `None` until it runs (#570).
        self._script_technology: dict | None = None
        # The PDK cells that are in the layout but not in the netlist —
        # `filler_placement`'s fill cells. They have no Liberty and no
        # power, by construction, and must not be read as macros the
        # analysis could say nothing about; see `_unpowered_instances`.
        # Captured by `_write_script` with the rest of the platform.
        self._physical_only_cells: list[str] = []
        # What a `netlist-source: pnr` run timed the routed design on —
        # `PARASITICS_SPEF` or `PARASITICS_ESTIMATED` — decided by
        # `_write_script` (#101). `None` for a synth-source run, which
        # has no routing to take parasitics from.
        self._parasitics: str | None = None
        # What `_resolve_inputs()` said when the script was generated —
        # the top `link_design` names, the SDC `read_sdc` reads. `None`
        # until `_write_script` runs; see `_publish_phys_model` for why
        # publication reads this rather than resolving a second time.
        self._script_inputs: dict | None = None
        # The OpenROAD thread plan `_write_script` resolved; `None` until
        # it runs (#654).
        self._thread_plan: ThreadPlan | None = None
        # The corners `_write_script` analysed, primary first, under a
        # multi-corner platform (#104, #105); empty for a single corner.
        self._script_corners: list[str] = []

    # ------------------------------------------------------------------
    # Artefact paths
    # ------------------------------------------------------------------

    def _script_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.tcl")

    def _log_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.log")

    def _report_path(self) -> str:
        return os.path.join(self.artefact_dir, "power.rpt")

    def _corner_report_path(self, corner: str) -> str:
        """One corner's `report_power` under multi-corner (#104, #105)."""
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
        """`report_power -instances`' output: one line per leaf cell (#558)."""
        return os.path.join(self.artefact_dir, "power_instances.rpt")

    def _instances_cells_path(self) -> str:
        """The `<instance path> <liberty cell>` sidecar (#558).

        `report_power` prints the path and the four powers, never the master
        the instance is an instance *of* — so the hierarchy walk that feeds
        it writes the mapping out alongside. Without this the model's power
        rows have no module column and cannot be joined to the synth half.
        """
        return os.path.join(self.artefact_dir, "power_instances.cells")

    @staticmethod
    def _staging_path(published: str) -> str:
        """Where the per-instance block writes ``published`` before it is
        published (#560).

        Tcl's ``>`` redirection creates the file before the command it
        redirects runs, and ``report_power -instances`` streams a row per
        cell into it. A call that emits two thirds of the design and *then*
        raises therefore leaves a nonempty report at the published path —
        and the ``catch`` around the block swallows the error by design,
        because the detail is a by-product that may not fail a run. The
        publish that follows reads that prefix as a whole breakdown: real
        watts, for a third of the instances, presented as the design's.
        Only an entirely empty parse is caught today, and a failure part-way
        through is never empty. The ``foreach`` writing the cells sidecar
        has exactly the same shape.

        So the block writes here and renames onto the published names as
        its last act, which Tcl reaches only when every command before it
        returned — an atomic publish on success. A failure leaves the
        staging file and no published one, which is the state the publish
        already reads as "this run produced no breakdown"; the trailing
        cleanup, and the next run's stale-clear, remove it.
        """
        return published + ".tmp"

    def _netlist_snapshot_path(self) -> str:
        """This run's own copy of the netlist it hands OpenROAD (#560).

        The analysis reads a netlist another command wrote, in another
        artefact directory, and records its sha256 as the evidence that
        these watts and the module rows beside them describe one design.
        Hashing the upstream path leaves a window however tightly it is
        drawn: `rb synth` rewriting that file between the hash and
        OpenROAD's `read_verilog` would have the model name bytes the
        analysis never measured, and the provenance gate would then read
        a real mismatch as a match.

        So the netlist is *snapshotted* instead: copied here, hashed
        here, and read from here. The hash and the bytes OpenROAD parses
        are then the same file, which no concurrent writer can reach —
        the upstream directory is not this run's, and this one is.

        The cost is one copy of a netlist that may be megabytes, per
        power run, in the directory that already holds the run's log and
        reports; the stale-clear removes it exactly as it removes them.
        """
        return os.path.join(self.artefact_dir, "power_netlist.v")

    def _bind_phys_dir(self) -> None:
        """Point `phys_dir` at the run `phys-run:` names, or leave it (#589).

        Without the field a merged model is an accident of naming: the
        two halves meet because a power run happens to be called after
        the synthesis it reads *and* configured in the same directory, so
        both write `artefacts/<one name>/`. Rename either side — or split
        a project's suites into `synth/` and `power/` — and each half
        lands in its own directory, each model half-filled, with nothing
        saying why. `phys-run:` states the pairing instead.

        The directory is *derived*, never taken: the run names an entry
        in the same `synth.yaml` this analysis already reaches through
        `synth-path:`, and its half is published into that suite's
        `artefacts/<run>/` — the path a synthesis of that name writes its
        own half into. So the knob survives the suite moving, and the
        config layer having refused any separator in the value leaves
        exactly one component to join: it cannot name a directory outside
        that artefacts tree.

        The named run's directory need not exist yet. A power analysis
        may legitimately land first and the synthesis fill the other half
        later, which is the whole point of publishing into a directory
        chosen rather than inherited.

        Called from `_write_script`, which is where this flow validates
        its configuration (see `run`): a `phys-run:` no entry in the
        referenced suite carries is broken on every machine and is
        reported as a failed run rather than as a by-product warning
        minutes later, and a resolution that failed leaves `phys_dir` at
        this run's own directory — the right target for the withdrawal
        `run()` then makes, since nothing was ever published to the
        directory that could not be resolved.
        """
        run = self.power_cfg.get_phys_run()
        if not run:
            return
        # The config layer requires `synth`/`synth-path` of every
        # `netlist-source: synth` entry, and refuses `phys-run` on any
        # other kind.
        suite_path = self.power_cfg.get_synth_suite_path()
        assert suite_path is not None
        phys_dir = os.path.normpath(
            os.path.join(os.path.dirname(suite_path), "artefacts", run)
        )
        # Where it lands is asked before whether the run exists: a
        # `synth-path:` pointing out of the project is wrong about the
        # directory whatever the suite turns out to contain, and asking in
        # this order keeps a suite that cannot be loaded at all from
        # answering with a parse error instead.
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
        """`(size, mtime_ns)` of the upstream netlist, as a change witness.

        The pair a writer cannot plausibly leave untouched: truncating and
        rewriting a netlist changes its length, and `write`/`rename` both
        stamp the mtime. It is a witness, not a lock — two writes inside one
        mtime tick that land on the same length would present the same pair,
        which POSIX gives no way to rule out short of holding the file open
        against a writer that does not want it held. See `_snapshot_netlist`
        for what that residue costs.
        """
        st = os.stat(source)
        return (st.st_size, st.st_mtime_ns)

    def _snapshot_netlist(self) -> str | None:
        """Copy the upstream netlist in, hash the copy, or say why not.

        Called between the stale-clear (which removes the previous run's
        copy) and OpenROAD, so the file the script names is written once
        and read once, by this run. Copy-then-rename via a `.tmp`
        sibling: a crash mid-copy leaves the staging file, never a short
        `power_netlist.v` that the next reader would take for a netlist.

        The copy is private and therefore immutable, but that alone does
        not make it *coherent*. When `power.yaml` reads a synthesis in
        another suite the two commands hold different artefact-tree
        locks, so a concurrent `rb synth` can truncate and rewrite
        `synth_netlist.v` under `copyfile`'s read — and the snapshot
        would then be a torn prefix of two netlists that the recorded
        sha256 authenticates perfectly. So the source is stat'd either
        side of each copy and the copy is retried while those stats
        differ: bounded at `_SNAPSHOT_ATTEMPTS`, because a writer looping
        over the netlist would otherwise loop this with it.

        Exhausting the attempts fails the run. A power figure over bytes
        that were never one netlist is worse than a refusal — the refusal
        is re-runnable, the figure is not detectably wrong.

        A `netlist-source: pnr` run resolves no netlist at all — it reads
        a routed database — so there is nothing to snapshot and nothing
        to hash, which is what it recorded before this existed.

        :returns: ``None`` on success (or when there is nothing to do),
            else a description of the failure. A netlist that cannot be
            copied into the artefact directory is not a by-product
            failure to warn about and continue past: the generated
            script names the copy, so there would be nothing for
            `read_verilog` to read.
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
                    # Somebody rewrote the netlist mid-copy; whatever is in
                    # the staging file spans the two versions. Drop it and
                    # read the source again from the top.
                    continue
                os.replace(staging, snapshot)
                # Of the copy, not of the source: these are the bytes
                # OpenROAD is about to read, and nothing else writes this
                # path.
                self._netlist_sha256 = sha256_of(snapshot)
                return None
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
        """The activity trace this run hands OpenROAD, or ``None``.

        ``None`` for a static run, which reads no trace at all:
        :func:`~rtl_buddy.phys.provenance.activity_block` drops a
        retained trace from such a run's block anyway, and a VCD is the
        largest file in an artefact tree — a whole pass over one to
        identify a file the Tcl never opens is a whole pass for nothing.
        """
        if self.power_cfg.get_activity_source() not in TRACE_SOURCES:
            return None
        activity = self.power_cfg.get_activity()
        return activity.saif or activity.vcd

    def _hash_trace(self) -> None:
        """Identify the trace by its bytes, as OpenROAD is launched (#570).

        **Not snapshotted, unlike the netlist.** The netlist is copied
        into this run's own directory precisely so the hash and the bytes
        the tool parses are one file no concurrent writer can reach, and
        that is the stronger guarantee. It is not available here: a SAIF
        is megabytes and a VCD of a long test is gigabytes, so a copy per
        power run would multiply the largest artefact in the tree by the
        number of corners analysed, on a filesystem that is holding the
        original for the same reason. The netlist is worth the copy
        because it is small; the trace is not.

        So the trace is hashed in place, immediately before the
        subprocess starts, and the residual race is the interval between
        this read and OpenROAD's own — milliseconds, against the minutes
        the analysis itself takes, and against the whole analysis that
        the old placement left exposed. `_confirm_trace_unchanged` closes
        the report on the other end.
        """
        self._trace_sha256 = sha256_of(self._trace_path())

    def _constraints_path(self) -> str | None:
        """The SDC this run hands OpenROAD, as the script named it (#570).

        `_write_script`'s own resolution, not a fresh one: on a
        `netlist-source: pnr` run with no explicit `constraints:` the SDC
        is `<pnr artefact>/<top>.routed.sdc`, which re-resolving could
        answer differently, and the point of the digest is to identify
        the file the generated Tcl reads.
        """
        return (self._script_inputs or {}).get("sdc")

    def _hash_constraints(self) -> None:
        """Identify the SDC by its bytes, as OpenROAD is launched (#570).

        Taken here rather than at publication for the reason the trace's
        is. A `netlist-source: pnr` run reads `<top>.routed.sdc` out of
        another command's artefact directory, where a concurrent `rb pnr`
        rewrites it in place; a synthesis SDC is a source file a person
        edits. Either way a digest computed after an analysis that runs
        for minutes identifies the replacement and records it as the
        constraints these watts were measured under — the exact
        substitution the digest exists to catch, one file over.

        Hashed in place rather than snapshotted: an SDC is a page of
        text, so the read is free, but it is also small enough that
        `_confirm_constraints_unchanged` can simply read it again on the
        way out and close the window from both ends.
        """
        self._constraints_sha256 = sha256_of(self._constraints_path())

    def _confirm_constraints_unchanged(self) -> None:
        """Withdraw the SDC hash if the file moved under the run (#570).

        The trace's rule, applied to the constraints:
        :func:`~rtl_buddy.phys.publish.confirm_digest` says whether the
        bytes hashed at launch are still there, and a mismatch records
        ``null`` rather than a digest nothing can vouch for. The warning
        is what keeps that null from reading as "this run had no
        constraints", which is the opposite of what happened.
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
        """Withdraw the trace hash if the file moved under the run (#570).

        `dump.saif` is rewritten in place by the next run of the test
        behind it, and a power analysis is long enough for that to happen
        while it is reading. Re-hashing at the end and comparing is what
        turns "the trace probably did not change" into a statement the
        document can make: equal, and the recorded hash identifies bytes
        that were on disk for the whole of the run.

        Unequal, and the honest record is that the identity is *unknown*.
        Neither hash is the answer — the first names bytes OpenROAD may
        not have finished reading, the second names bytes it certainly
        did not start with — and a hash nothing can vouch for is worse
        than no hash, because the provenance gate reads a recorded hash
        as evidence. ``None`` is the model's own word for unknown, which
        is what a static run and an unreadable file already record, so
        the withdrawal needs no new vocabulary. The warning is what makes
        it findable: a null here otherwise reads as "this run measured no
        trace", which is the opposite of what happened.
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

    # ------------------------------------------------------------------
    # Inputs resolution
    # ------------------------------------------------------------------

    def _resolve_inputs(self) -> dict:
        """Resolve netlist / ODB / SDC paths per `netlist-source`.

        Dispatches on `power_cfg.get_netlist_source()`:

        - "synth" (default): post-synth tech-mapped netlist (Liberty +
          LEF supply the technology view; switching power is
          under-estimated because there are no real parasitics and no
          CTS-buffered clock tree).
        - "pnr": post-PnR OpenROAD binary DB (`<top>.routed.odb`) +
          post-CTS SDC. The .odb encapsulates placement + routing so
          rerunning `estimate_parasitics -global_routing` reflects the
          CTS-buffered clock tree and routed wire capacitance. When the
          P&R run's PDK declared `rcx-rules`, the run also wrote an
          OpenRCX-extracted `<top>.routed.spef`, and `_write_script`
          reads that instead of estimating (#101); see
          `routed_spef_rejection` for when it is trusted.

        Returns a dict with keys: netlist (None for pnr), odb (None for
        synth), spef and pnr_script (None for synth), sdc, top,
        macro_libs, macro_lefs.

        **The macro libraries are resolved here** rather than in
        `_write_script`, because this is the one place that already holds
        the upstream entry they are inherited from (#627). A hard macro's
        Liberty reaches `rb pnr` or `rb synth` through *that* run's
        `lib-paths`; the power analysis reads only the platform corner,
        which characterises standard cells, so every macro instance was in
        the design and in the instance report contributing exactly zero.
        The libraries the upstream run declares are what this run has to
        read to say anything about those instances, and the `power.yaml`'s
        own `lib-paths` are appended after them.

        ``macro_lefs`` is empty on the `pnr` path and the synthesis run's
        `lef-paths` on the `synth` one, because that is where the LEF is
        load-bearing: `read_verilog` + `link_design` builds the database
        out of LEF masters and cannot place an instance of a master it has
        never seen, while `read_db` restores a database in which every
        master the router placed is already present. Reading the macro LEF
        again before a `read_db` would not even survive it — the database
        read replaces the technology the LEF built.
        """
        # Appended after whatever the upstream run declares, so the
        # inherited list stays the base and a `power.yaml` adds to it.
        own_libs = self.power_cfg.get_lib_paths()
        if self.power_cfg.get_netlist_source() == "pnr":
            pnr_cfg = self.power_cfg.resolve_pnr_cfg()
            top = pnr_cfg.resolve_synth_cfg().get_top()
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
                "macro_libs": _dedup_paths([*pnr_cfg.get_lib_paths(), *own_libs]),
                "macro_lefs": [],
            }

        synth_cfg = self.power_cfg.resolve_synth_cfg()
        top = synth_cfg.get_top()
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
            "macro_libs": _dedup_paths([*synth_cfg.get_lib_paths(), *own_libs]),
            "macro_lefs": _dedup_paths(synth_cfg.get_lef_paths()),
        }

    def _upstream_identity(self) -> dict:
        """Which upstream run this analysis actually read, for the digest.

        `netlist_source` names the *kind* of upstream — "synth" or "pnr" —
        and nothing more. Two power entries pointing at two different
        synth entries, or at two suites through `synth-path`, resolve
        different netlists under one spelling of it; `_resolve_inputs`
        hands OpenROAD that difference and the config fingerprint did not
        record it, so two runs measuring two designs fingerprinted
        identically and a run listing showed them as one experiment
        (#570).

        Digest what the run consumed. A `netlist-source: synth` run
        already holds the strongest statement available — the sha256 of
        the netlist copy it measured — and it is better than a path here:
        two entries that resolve byte-identical netlists *are* one
        experiment, which is the comparison this block exists to make. A
        `netlist-source: pnr` run reads a routed database that nothing
        hashes (an .odb is large, and is read once), so the ODB's path
        stands in for its contents; it names the pnr run's own artefact
        directory, which is exactly what two pnr entries differ in.

        Project-relative, because a digest that moved with the checkout
        would tell one run apart from itself. This is the one path the
        publish cannot relativise on our behalf: `_publish` rewrites the
        paths *inside* the config block, and by the time it runs the
        options mapping has already been digested.

        Unknown stays ``null`` rather than becoming a placeholder — the
        strict-or-absent rule :func:`options_digest` keeps.
        """
        if self.power_cfg.get_netlist_source() != "pnr":
            return {"netlist_sha256": self._netlist_sha256, "input_path": None}
        # The capture `_write_script` took, not a fresh resolution: this
        # names the database OpenROAD was given (#560).
        odb = (self._script_inputs or {}).get("odb")
        identity = {
            "netlist_sha256": None,
            "input_path": (
                project_relative(odb, project_root_for_dir(self.artefact_dir))
                if odb
                else None
            ),
        }
        # One ODB timed on its extracted SPEF and on the global-route
        # estimate is two measurements, and has to digest as two (#101).
        # Only the SPEF case adds the key, so an estimate-path model keeps
        # the digest it had before extraction existed.
        if self._parasitics == "spef":
            identity["parasitics"] = self._parasitics
        return identity

    def _resolve_platform(self):
        """Resolve to a PnrPlatformConfig (provides Liberty path)."""
        return self.root_cfg.get_pnr_platform_cfg(self.power_cfg.get_platform())

    # ------------------------------------------------------------------
    # Tcl script generation
    # ------------------------------------------------------------------

    def _emit_activity_cmds(self) -> list[str]:
        """Translate the resolved activity source into OpenROAD Tcl.

        The *decision* of which source to use lives on PowerConfig
        (`get_activity_source()`); this backend just emits the
        corresponding `read_saif` / `read_power_activities` /
        `set_power_activity` command.
        """
        source = self.power_cfg.get_activity_source()
        activity = self.power_cfg.get_activity()
        if source == "saif":
            scope_arg = f" -scope {activity.scope}" if activity.scope else ""
            return [f"read_saif{scope_arg} {activity.saif}"]
        if source == "vcd":
            scope_arg = f" -scope {activity.scope}" if activity.scope else ""
            return [f"read_power_activities{scope_arg} -vcd {activity.vcd}"]
        if source == "synthetic":
            return [
                f"set_power_activity -global "
                f"-activity {activity.default_toggle_rate} "
                f"-duty {activity.default_static_prob}"
            ]
        return []  # "default" → static, no activity commands

    # Printed by the generated script between the design-total `report_power`
    # and the per-instance block below it. It splits `power.log` into the half
    # that decides the run and the half that only decides the by-product: see
    # `_fatal_log_region` (#558).
    _DETAIL_MARKER = "RB_PHYS_DETAIL_BEGIN"

    def _emit_per_instance_cmds(self, multi_corner: bool = False) -> list[str]:
        """Tcl that attributes the run's power to individual leaf instances.

        This is rtl-buddy/rtl_buddy#114 delivered where the OpenROAD session
        already lives, rather than as the stand-alone `emit_phys.tcl` that
        issue predates `rb power` by (#558).

        Three properties the shape is chosen for:

        **One analysis, not one per instance.** `report_power` takes a *list*
        of instances and prints one line each, so the whole design costs a
        single extra call on top of the design-total report above it. The
        obvious `foreach ... {report_power -instances $inst}` spelling reruns
        the propagation per cell and turns a minute into an afternoon on
        anything real.

        **Leaf cells only.** `get_cells -hierarchical *` returns the leaves —
        the instances that have a Liberty cell and therefore a power number.
        Roll-up to the enclosing modules is the model consumer's job.

        **It cannot fail the run.** Everything here is inside a `catch`: the
        design totals have already been written by the time this executes, so
        a `get_cells` that finds nothing, or an OpenSTA without the
        `-instances` form, must cost the run its per-instance detail and
        nothing else. A Tcl error escaping to the top level would abort the
        script and take the exit code with it.

        The `catch` alone is not enough, though: an OpenSTA that rejects
        `-instances` prints an `[ERROR ...]` diagnostic *before* raising, and
        the post-run log gate fails any run whose log carries one. So the
        block opens with a marker line naming where the by-product begins —
        `_fatal_log_region` scans only what precedes it, and this contract
        holds without the gate having to guess which diagnostics are benign.

        **And a swallowed failure must publish nothing**, which is why the
        two files are written under staging names and renamed onto the
        published ones as the block's last two commands. Failing part-way
        leaves rows on disk, `catch` hides that it failed, and a partial
        report at the published path parses as a complete design. See
        `_staging_path`; the trailing deletes clear the staging files a
        failed block leaves, and are no-ops after a successful rename.

        **Under multi-corner it is the worst corner's breakdown** (#104,
        #105): `rb_power_corner`, set by the per-corner block before it,
        is the corner whose design totals the run reports, and the rows
        have to add up to those totals.
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
            # Reached only if everything above returned: this is the
            # publication, and it is one rename per file.
            f"    file rename -force {cells_tmp} {cells}",
            f"    file rename -force {instances_tmp} {instances}",
            "  }",
            "}",
            f"catch {{file delete -force {cells_tmp}}}",
            f"catch {{file delete -force {instances_tmp}}}",
        ]

    def _write_script(self) -> str:
        # Before anything else this generates: a `phys-run:` that cannot
        # be resolved is a configuration error, and the withdrawal the
        # caller makes on the way out needs to know which directory this
        # run publishes into (#589).
        self._bind_phys_dir()
        platform = self._resolve_platform()
        pdk = platform.get_pdk()
        liberty = platform.get_sta_lib_path()
        # Multi-corner (#104, #105): every corner of the platform in this one
        # session. Empty for a single-corner platform, whose script is then
        # the one this flow has always emitted, line for line.
        corner_libs = (
            platform.get_sta_corner_lib_paths() if platform.is_multi_corner() else {}
        )
        self._script_corners = list(corner_libs)
        tech_lef = pdk.get_tech_lef()
        macro_lef = pdk.get_macro_lef()
        inputs = self._resolve_inputs()
        # The script is generated from these, so these are what the run
        # measured — `_publish_phys_model` reads the capture rather than
        # resolving again (#560).
        self._script_inputs = inputs
        macro_libs = list(inputs.get("macro_libs") or [])
        macro_lefs = list(inputs.get("macro_lefs") or [])
        # Not part of the script — the fill cells are already in the
        # database this reads — but resolved here with the rest of the
        # platform, so the detection below judges the PDK the run was
        # prepared against (#627).
        self._physical_only_cells = list(pdk.get_fill_cells() or [])
        # And the technology the `read_liberty` / `read_lef` lines below
        # name, in the order they name it, for `_phys_technology` (#570).
        self._script_technology = {
            "liberty": liberty,
            "corner_libs": list(corner_libs.values()),
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
        # Every macro input the configuration named, before OpenROAD is
        # launched and at ERROR, the way a stream-out judges its own
        # (`pnr.gds_missing_inputs`). A `read_liberty` of a path that is not
        # there is a diagnostic in a log nobody reads and an analysis that
        # carries on to report the macro at zero watts — which is the exact
        # silence this key exists to end, restored by a typo (#627).
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
            # The script reads this run's own copy, not the upstream
            # path: `_snapshot_netlist` writes it after the stale-clear
            # below and hashes what it wrote, so the bytes the model
            # names and the bytes OpenROAD parses are one file that no
            # concurrent `rb synth` can reach (#560). Recorded here so
            # that step knows what to copy.
            self._netlist_source_path = netlist

        # Ahead of the first `read_liberty`, and absent when `threads:` is
        # unset, so such a script is the one this flow has always emitted
        # (#654).
        self._thread_plan = plan_threads(
            self.power_cfg.get_threads(), flow="power", run=self.power_cfg.get_name()
        )
        threads_tcl = self._thread_plan.tcl()
        lines = ["# Generated by rtl_buddy power flow"]
        if threads_tcl:
            lines.append(threads_tcl)
        if corner_libs:
            # Each macro library read into every corner, after the standard
            # cells; see `openroad_corners.liberty_tcl`.
            lines.extend(openroad_corners.liberty_tcl(corner_libs, macro_libs))
        else:
            lines.append(f"read_liberty {liberty}")
            # After the platform corner, so a macro library never shadows a
            # standard cell, and in the order resolved. Both lists are empty
            # for a design with no macros, and the script is then the one
            # this flow has always emitted, line for line (#627).
            lines.extend(f"read_liberty {lib}" for lib in macro_libs)
        lines.append(f"read_lef {tech_lef}")
        if macro_lef:
            lines.append(f"read_lef {macro_lef}")
        lines.extend(f"read_lef {lef}" for lef in macro_lefs)
        if source == "pnr":
            # ODB encapsulates placement + routing. Reading it
            # repopulates OpenROAD's DB at the post-route state. The
            # wire parasitics then come from the P&R run's extracted SPEF
            # when it wrote one this run can trust (#101); otherwise
            # estimate_parasitics derives them from the global routes, so
            # the CTS-buffered clock tree still contributes realistically
            # to switching power.
            spef = self._choose_parasitics(inputs)
            lines.append(f"read_db {odb}")
            lines.append(f"read_sdc {sdc}")
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
            # One report per corner, then `power.rpt` at the worst of them —
            # the corner that drives a budget, chosen in the session so the
            # per-instance rows below are of the same corner.
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
        """The routed SPEF to read, or `None` to estimate; logs which (#101).

        Sets `_parasitics` for the results and the model's provenance. A
        SPEF that exists but is refused is a WARNING, naming why: the user
        configured extraction and is about to get the estimate instead.
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

    # ------------------------------------------------------------------
    # Report parsing
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def _clear_stale_report(self) -> str | None:
        """Remove the previous run's `power.rpt` and its per-instance half.

        The per-instance report and its cell sidecar are read back inside
        this same `run()` to build the phys model, so they take the same
        treatment as the report they accompany: an OpenROAD that exits 0
        without reaching the `catch` block must not have the last run's
        per-instance watts published as this one's (#469, #558).

        The netlist snapshot goes with them (#560). It is the largest
        thing this flow writes, nothing reads it after OpenROAD has, and
        a failed run that left it behind would leave a copy of a netlist
        no artefact here still describes. `run()` therefore clears
        *before* `_snapshot_netlist` takes this run's copy, never after.

        The model and its manifest stay -- a synthesis may have merged its
        own half into them -- but this flow's half is nulled out, because
        publication happens only on a pass and a failed rerun would otherwise
        leave the previous run's per-instance watts discoverable with the
        report behind them already deleted (#558). They are withdrawn from
        `phys_dir`, which is where this run published them and need not be
        the directory the reports above are cleared from (#589).

        :returns: ``None``, or the reason the withdrawal did not happen —
            which every caller turns into a failed run, because the reports
            behind the still-published half have just been deleted here. See
            :func:`~rtl_buddy.phys.publish.withdrawal_failure_desc`.
        """
        stale = clear_stale_artefacts(
            [
                self._report_path(),
                self._instances_report_path(),
                self._instances_cells_path(),
                # The names the per-instance block writes under before it
                # renames (#560). An OpenROAD killed inside that block never
                # reaches its trailing deletes, and a staging file left in
                # the artefact directory would be the next run's to publish.
                self._staging_path(self._instances_report_path()),
                self._staging_path(self._instances_cells_path()),
                self._netlist_snapshot_path(),
                # Every per-corner report a multi-corner run wrote (#104,
                # #105), by pattern rather than by the configured corners:
                # the platform may have lost a corner since, and its report
                # must not outlive the run that produced it.
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
        """Null this flow's half of the model + manifest it publishes (#558).

        The counterpart of `_publish_phys_model`, called from the clear so a
        run that never reaches publication withdraws the previous one's
        per-instance rows rather than leaving them over a deleted report. The
        synthesis half is untouched -- which matters more under `phys-run:`,
        where the directory being written is a synthesis run's own (#589).

        A withdrawal that *succeeded* is bookkeeping and logs at DEBUG. One
        that failed is not: the clear that called this has already deleted
        the reports the published half was read from, so the rows are now
        standing over nothing, and carrying on would let a run that dies
        before publishing leave them there. It warns and hands the reason
        back for the caller to fail on (#560).

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
        """Instances the analysis could say nothing about, and their cells.

        A hard macro whose Liberty never reached the run is *in* the
        design — placed, routed, and one line of `power_instances.rpt` —
        and every one of its four columns is `0.00e+00`. Nothing in the
        report distinguishes that from a cell that genuinely burns
        nothing, so the design total, the `Macro` group row and the
        model's per-instance half all read as a measurement when they are
        an omission (#627).

        Read off the two reports this run already produced, rather than
        asked of OpenSTA. Emitting the list from Tcl would put a new
        `get_property` in the hierarchy walk — a property an older
        OpenSTA may not answer, inside the `catch` that must not cost the
        run anything — and would change the generated script for every
        design, including the ones with no macros at all. The reports are
        already parsed by the publish a few lines further on, and the
        Liberty files the script named are on disk.

        All three conditions, not any one:

        - the instance's master is not declared in any Liberty the script
          read (:func:`_liberty_cell_names`), which is the statement
          being made;
        - its total power is exactly zero, which is what makes the
          statement worth a warning; and
        - its master is not one of the PDK's fill cells.

        The second keeps a Liberty spelling this scanner misses from
        turning a whole standard-cell library into a warning: a cell that
        reports watts has power data whatever the scan concluded. The
        first keeps an unclocked flop that really does sit at zero out of
        it. The third is not a refinement but a correction: `rb pnr` ends
        with `filler_placement`, so a routed database holds tens of
        thousands of fill instances that are in the layout and not in the
        netlist. They have no Liberty and no power *by construction*, and
        reporting them would bury the one macro this exists to find under
        24 000 lines of noise. They are named by the PDK, which is the
        same list the flow filled with.

        :returns: ``{"cells": [...], "instances": int}`` — the master
            names, sorted, and how many instances of them there are. The
            paths themselves are not carried: a design can hold thousands
            and the per-instance report and `phys-model.json` both list
            them with the cell beside each. Empty when there is nothing
            to say, including when either report is absent — the
            by-product failure `power.phys_model_incomplete` reports
            that.
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
            # The common case, and the expensive check skipped: no
            # zero-power instance means no Liberty needs scanning.
            return empty
        technology = self._script_technology or {}
        known = _liberty_cell_names(
            [
                path
                for path in [
                    technology.get("liberty"),
                    *(technology.get("macro_libs") or []),
                ]
                if path
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
        """The part of `power.log` whose `[ERROR ...]` lines fail the run.

        Everything the generated script prints after `_DETAIL_MARKER` belongs
        to the per-instance block, which is a by-product: it is `catch`ed, and
        by the time it runs the design totals are already in `power.rpt`. An
        `[ERROR ...]` down there (an OpenSTA without `report_power
        -instances`, say) must cost the model its `instances` half and nothing
        more — failing the run on it would delete totals this wrapper had
        already parsed.

        A log with no marker is scanned whole: older scripts predate it, and
        so does a run that died before reaching the totals.
        """
        marker_at = log_text.find(self._DETAIL_MARKER)
        return log_text if marker_at < 0 else log_text[:marker_at]

    def _fail_after_openroad(self, desc: str) -> PowerFailResults:
        """Fail a run that has already invoked OpenROAD, publishing no report.

        `report_power`'s output file is written before the script ends, so
        OpenROAD can exit non-zero — or log an `[ERROR ...]`, or leave a
        report this wrapper cannot read or parse — with `power.rpt` on disk
        at the fixed path the next run would otherwise quote (#469). Every
        post-OpenROAD failure return goes through here.

        The clear withdraws this flow's published half as it goes, and a
        withdrawal it could not make is said out loud in the description
        this run already fails with: the run was over either way, but the
        user has to know the artefact directory still publishes rows over
        the reports just deleted (#560).
        """
        stale_error = self._clear_stale_report()
        if stale_error is not None:
            desc = f"{desc}; {withdrawal_failure_desc(stale_error)}"
        return self._with_threads(
            PowerFailResults(name=self.name + "/results", desc=desc)
        )

    def _with_threads(self, res: PowerResults) -> PowerResults:
        """Record the run's OpenROAD thread provenance on ``res`` (#654).

        Only once OpenROAD has been launched on a script that carried the
        plan; the count OpenROAD itself logged wins over the one asked for.
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

        # Ahead of the "openroad not found" return below: `_write_script`
        # is where this flow validates its configuration — the SDC, the
        # platform's tech-lef, the upstream netlist or routed ODB. An
        # analysis pointing at a missing input is broken on every machine,
        # and reporting it as "openroad not found" on a box that merely
        # lacks the tool sends the user after the wrong problem. A config
        # error is a failed run, so it clears on the way out (#469).
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

        # Everything past the "openroad not found" return above is a run of
        # this entry, however it ends — including the script-generation
        # failure just below. `report_power`'s output file is read back off a
        # fixed path and OpenROAD exiting 0 with no [ERROR] does not prove it
        # rewrote it, so clear here and the "power report not produced" path
        # stays reachable instead of quoting a previous run's watts (#469).
        #
        # The clear also withdraws whatever this flow published here last
        # time, and OpenROAD does not start if it could not: the reports
        # behind those rows have just been deleted, so a run that went ahead
        # and then failed would leave a breakdown of a design this directory
        # no longer holds discoverable as a current one (#560).
        stale_error = self._clear_stale_report()
        if stale_error is not None:
            return PowerFailResults(
                name=self.name + "/results",
                desc=withdrawal_failure_desc(stale_error),
                fail_stage="setup",
            )

        # After the clear, before OpenROAD: the script names this run's
        # own copy of the netlist, and the hash recorded beside the watts
        # is of that copy (#560).
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
        # OpenROAD's `-log` truncates only once it is running; a launch that
        # dies earlier would leave the previous run's log, and its ORD-0030
        # thread count, to be read as this run's (#654).
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

        # Last thing before the subprocess: the trace's and the SDC's
        # identities are of the bytes on disk as OpenROAD starts, and the
        # narrower that window is the less there is to confirm
        # afterwards (#570).
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

        # Before the pass is announced, so the warning reads above the
        # numbers it qualifies rather than under them. It never changes
        # the verdict: the watts reported are a real measurement of
        # everything that had a library, and refusing to report them
        # would cost a user the standard-cell figure they can act on to
        # make a point about the macro they already know is a macro.
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
        return self._with_threads(passed)

    def _parse_corner_reports(self, log_text: str) -> tuple[dict, str | None]:
        """Per-corner totals and the worst corner of a multi-corner run.

        Returns ``(fields, error)``. `power.rpt` — already parsed for the
        scalar fields — is the report at the corner the script chose as
        worst and printed after `POWER_CORNER_MARKER`; each corner's own
        report is `power.<corner>.rpt`. A corner whose report is missing or
        unparsable fails the run like a missing `power.rpt` does: the run
        was asked for every corner, and reporting fewer would read as a
        signoff over corners it never saw (#104, #105).
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
        """The Liberty + LEF the generated script reads, as identity (#570).

        `_write_script` emits `read_liberty <liberty>` and one or two
        `read_lef` lines from the resolved platform, and the options
        mapping recorded only the platform *name*. A `cfg-pnr-platforms`
        entry repointed at another corner — a different Liberty, a
        different tech LEF — is the same name, so two analyses of two
        technologies fingerprinted identically and a run listing showed
        them as one experiment. The synthesis fingerprints already close
        this on their own library lists; this closes it on the power
        flow's, through the same
        :func:`~rtl_buddy.tools.synth_yosys.library_fingerprint` — paths
        rather than contents (a Liberty is tens of megabytes), spelled
        project-relative, and in script order, because `read_liberty` and
        `read_lef` are order-sensitive.

        Read from the capture `_write_script` took rather than resolved
        again: the mapping has to describe the technology the run
        consumed, not whatever `cfg-pnr-platforms` says now.

        The macro libraries and LEFs sit where the script reads them —
        the inherited and configured Liberty after the platform corner,
        the inherited LEFs after the PDK's (#627). They belong in the
        fingerprint for the reason the platform's do, and more sharply: a
        run that reads a macro's Liberty and one that does not measure
        the same netlist and report different watts, so digesting them
        identically would present the before and after of this very fix
        as one experiment.
        """
        resolved = self._script_technology or {}
        named = [
            resolved.get("liberty"),
            # Every corner's Liberty under multi-corner (#104, #105), the
            # primary's again among them: a run over three corners and a run
            # over the primary alone are two experiments. Empty, and so
            # absent from the digest, for a single-corner run.
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
        """Write `phys-model.json` + its manifest for a run that passed (#558).

        Into `phys_dir`, which is this run's own artefact directory unless
        `phys-run:` named a synthesis to publish beside (#589). The report
        paths the manifest records still point into the run's own
        directory, and they are project-relative there as everywhere, so
        a reader reaches them from either place.

        Never fails the power analysis. The design totals are already parsed
        and already reported by the time this runs; the per-instance rows are
        the by-product, and an OpenSTA that skipped or garbled them costs the
        model its `instances` half and earns a warning.

        The top comes from the resolution the *script* was generated from
        rather than the run name, because the model is keyed on the
        *design*: it is what decides whether a synthesis' module rows
        already in this directory describe the same thing and may be merged
        forward. Resolving a second time here would re-read the synth or pnr
        YAML this analysis references, minutes after OpenROAD was launched
        against the first answer — a `top:` edited in between, or a
        referenced entry renamed away, would then have these watts attributed
        to a design they do not describe and merged against a co-named
        publication of another one. So `_write_script` captures what it
        resolved and this reads the capture, the same capture-at-preparation
        rule the netlist snapshot follows (#560).

        The netlist this run read is identified by the hash
        `_snapshot_netlist` took of the private copy it gave OpenROAD, not
        by re-reading the upstream path now: it is what a later synthesis
        into this directory tests its own output against before carrying
        these per-instance rows forward, and what this publish tests before
        carrying any module rows already here forward (#558), so it has to
        be of the bytes the analysis actually measured -- which is why the
        analysis reads a copy nothing else can rewrite (#560). A
        `netlist-source: pnr` run resolves no netlist at all -- it reads the
        routed ODB -- so it records none, and nothing is inherited in either
        direction. The manifest records where that copy is as well as
        what it hashed to, so a result read back from an archive can reach
        the netlist and re-check the hash rather than take it on faith
        (#560).

        The mode and the activity go in beside them (#568). Without them
        the model records a µW figure with no statement of what it is a
        figure OF: static leakage-plus-internal and a SAIF-driven dynamic
        total print in the same column, and two runs of one design that
        differ only in their stimulus are one document read twice. Both
        are the resolved values this run actually dispatched on -- the
        same `get_mode()` / `get_activity_source()` pair
        `_emit_activity_cmds` branches on -- so the record cannot claim a
        source the Tcl did not use. The trace is identified by its
        SHA-256 as well as its path: `dump.saif` is rewritten in place by
        the next run of the test behind it, so the path alone cannot tell
        a re-captured trace from the one this run measured. That hash is
        `_hash_trace`'s, taken as OpenROAD was launched and confirmed
        when it returned, not re-read here -- a hash taken at this point
        would identify a replacement written while the analysis ran, and
        `_confirm_trace_unchanged` records `null` rather than a hash
        nothing can vouch for. Two reads of one file, and only on a run
        that read it at all.

        The constraints recorded are the RESOLVED SDC, the `sdc` of the
        resolution `_write_script` generated from, and not the config's
        `constraints:` field. On a
        `netlist-source: pnr` run they are not the same thing: with no
        explicit `constraints:` the analysis reads `<pnr
        artefact>/<top>.routed.sdc`, the post-CTS constraints the router
        wrote, and the field is empty -- so the config block recorded
        `null` and its hash with it, and two runs against two different
        routed SDCs fingerprinted identically while measuring different
        timing. The rest of the block is what the run dispatched on; this
        one field was what it was configured with, which is the same
        value only when the reader spelt it out. Its hash is
        `_hash_constraints`' -- taken as OpenROAD was launched and
        confirmed when it returned, for the reason the trace's is: a
        routed SDC is rewritten in place by a concurrent `rb pnr`, and a
        digest computed here would name the replacement (#570).
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
            # The snapshot, not the upstream path: the manifest names the
            # bytes the hash beside it identifies, and only the copy is
            # still guaranteed to be those bytes. Null exactly when the
            # hash is -- a `netlist-source: pnr` run snapshots nothing.
            netlist_path=(
                self._netlist_snapshot_path()
                if self._netlist_sha256 is not None
                else None
            ),
            mode=self.power_cfg.get_mode(),
            activity=activity_block(
                source=source,
                trace=trace,
                # Taken as OpenROAD was launched and confirmed when it
                # returned (`_hash_trace`), not re-read now: the analysis
                # is long, `dump.saif` is rewritten in place by the next
                # run of the test behind it, and a hash taken afterwards
                # would identify the replacement rather than the bytes
                # this run measured. `None` where the run read no trace,
                # or where the trace changed underneath it and the
                # identity is therefore unknown.
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
                # The Liberty and LEF the script reads: the platform name
                # alone does not determine them, so two corners behind one
                # name digested identically (#570).
                "technology": self._phys_technology(),
                # Which upstream run, not merely which kind of one; see
                # `_upstream_identity` (#570).
                **self._upstream_identity(),
                "mode": self.power_cfg.get_mode(),
                "activity_source": source,
                # Under a multi-corner platform the watts and per-instance
                # rows are the worst corner's; say which, so the model is
                # not read as the primary corner's. Absent for one corner,
                # keeping those digests as they were (#104).
                **({"corner": worst_corner} if worst_corner else {}),
                "reglvl": self.power_cfg.get_reglvl(self.power_cfg.get_tool_name()),
                # `tool_overrides` is deliberately absent. Nothing in this
                # backend reads it -- `PowerConfig.get_tool_overrides()` has
                # no caller at all, so a `power.yaml` that carries the block
                # runs exactly as one that does not -- and a fingerprint over
                # a field that shapes nothing tells two identical analyses
                # apart, which is the one thing the digest exists not to do.
                # Recording it would also be a quiet claim that it was
                # applied. If the field is ever wired in, it belongs back
                # here in the same change.
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
            # Only under `phys-run:`. The pairing was asked for by name,
            # and the netlist hashes say the module rows in that
            # directory were counted off a netlist this analysis did not
            # read -- so the gate dropped them and the model written
            # there is this half alone. Said out loud, because a config
            # that names the run it wants to pair with and then quietly
            # produces a half-filled model is the same silence the field
            # exists to end (#589). Not a failure: the watts are sound
            # and re-running the synthesis pairs them.
            log_event(
                logger,
                logging.WARNING,
                "power.phys_pair_mismatch",
                power=self.power_cfg.get_name(),
                phys_run=self.power_cfg.get_phys_run(),
                phys_dir=self.phys_dir,
            )
        return published["model"]
