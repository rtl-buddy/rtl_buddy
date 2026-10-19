"""Stage checkpoints and progress events for `rb pnr` runs with `checkpoints:` set.

The generated flow writes a stage-named database at each stage boundary and appends a JSON-lines event as each flow step starts and ends, so a killed run leaves its last stage and physical state behind.

Layout under the run's artefact directory::

    checkpoints/
      latest -> 20260925T101500-4242      # the run that started last
      20260925T101500-4242/
        manifest.json                     # inputs + tool identity (Python)
        progress.jsonl                    # step / stage / checkpoint events
        01_floorplan.{odb,def,sdc}
        02_place.{odb,def,sdc}
        03_cts.{odb,def,sdc}
        04_global_route.{odb,def,sdc,guide,segments}
        05_detailed_route.{odb,def,sdc}
        06_post_route_repair.{odb,def,sdc}   # with post-route-repair set
        export/03_cts/...                 # `rb pnr-export --checkpoint`

A staged run (`stages: true`) adds each stage's session script and log, `NN_<stage>.{tcl,log}`, and a `NN_<stage>.state.tcl` per checkpoint with the flow globals the next session needs.

- Checkpoints are never final outputs. They live below `checkpoints/`, out of reach of the up-front clear and of `rb power`'s `<top>.routed.odb` path.
- Each run writes its own run-id directory, which later runs never overwrite or delete. Every `rb pnr` run removes `latest` first; a checkpointed run re-points it when it launches OpenROAD.
- Python writes the manifest (input hashes, script hashes, OpenROAD version, requested stages) before OpenROAD starts and completes it after exit. Tcl only appends events, closing the file after each, so `progress.jsonl` is complete up to a kill.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

from ..config.pnr import CHECKPOINT_STAGES, PNR_STAGES
from ..logging_utils import log_event
from .artifact_paths import project_relative, project_root_or_none

logger = logging.getLogger(__name__)

CHECKPOINTS_DIRNAME = "checkpoints"
LATEST_NAME = "latest"
MANIFEST_NAME = "manifest.json"
PROGRESS_NAME = "progress.jsonl"
#: Exports go inside the run directory so a pre-route layout never lands at a routed output's path.
EXPORT_DIRNAME = "export"

#: Bumped on an incompatible manifest or event change.
CHECKPOINT_SCHEMA = 1

_TCL_FILE = "checkpoints.tcl"

#: Each stage's file index. Indexes are fixed per stage, so a file name names its stage whatever stages a run has.
STAGE_INDEX = {
    "floorplan": "01",
    "place": "02",
    "cts": "03",
    "global_route": "04",
    "detailed_route": "05",
    "post_route_repair": "06",
    "finish": "07",
}

#: How a one-session run ends each stage: on entering the next stage's first command, on leaving the detailed router, or (None) where the flow calls `rb::ckpt::stage_end` after any global-route repair. The database at that point is exactly what the stage left.
STAGE_ANCHORS = {
    "floorplan": ("global_placement", "enter"),
    "place": ("clock_tree_synthesis", "enter"),
    "cts": ("global_route", "enter"),
    "global_route": None,
    "detailed_route": ("detailed_route", "leave"),
}

#: Checkpoints whose database holds a global route.
GLOBAL_ROUTED = ("global_route", "detailed_route", "post_route_repair")

#: Checkpoints whose database holds a detailed route.
DETAIL_ROUTED = ("detailed_route", "post_route_repair")

#: Checkpoints that also carry the route guides and global-route segments; the detailed-route session reads the segments. A routed database's pin access points no longer match the segments, which OpenROAD then refuses (GRT-0263), so a later session takes the global route from the guides the database keeps.
ROUTE_FILES = ("global_route",)

#: Flow globals a staged checkpoint saves for the next session: per-macro cell halos from `floorplan.macros`, which the rotated-macro blockages read in the place session.
SESSION_STATE = ("MACRO_CELL_HALOS",)

#: Flow commands whose start and end are progress events.
PROGRESS_STEPS = (
    "link_design",
    "read_sdc",
    "initialize_floorplan",
    "place_pins",
    "insert_tiecells",
    "pdngen",
    "global_placement",
    "repair_tie_fanout",
    "repair_design",
    "detailed_placement",
    "clock_tree_synthesis",
    "repair_timing",
    "check_placement",
    "global_route",
    "detailed_route",
    "filler_placement",
)

#: Why a checkpoint has no congestion grid; readers must see "unavailable", not an empty grid that reads as zero congestion.
_CONGESTION_PRE_ROUTE = "no global route at this stage"
_CONGESTION_GR = (
    "the checkpoint carries no congestion grid; the run's congestion.rpt lists "
    "overflowing tiles only, and the route guides and segments are the "
    "retained routing state"
)

#: Run id: timestamp and pid, optionally de-duplicated; one safe path segment.
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}-\d+(?:-\d+)?$")


def checkpoints_root(artefact_dir: str) -> str:
    return os.path.join(artefact_dir, CHECKPOINTS_DIRNAME)


def latest_pointer(artefact_dir: str) -> str:
    return os.path.join(checkpoints_root(artefact_dir), LATEST_NAME)


def allocate_run_dir(artefact_dir: str, *, now: datetime | None = None) -> str:
    """Create and return a fresh run directory, suffixing the id if it already exists."""
    root = checkpoints_root(artefact_dir)
    os.makedirs(root, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    base = f"{stamp}-{os.getpid()}"
    for attempt in range(1000):
        run_id = base if attempt == 0 else f"{base}-{attempt}"
        path = os.path.join(root, run_id)
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return path
    raise RuntimeError(f"cannot allocate a checkpoint directory under {root}")


def _tcl_quote(value: str) -> str:
    """Return a Tcl double-quoted word with substitutions suppressed, also safe inside a braced body."""
    escaped = value.replace("\\", "\\\\")
    for char in ("$", "[", "]", '"', "{", "}"):
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def _arm_config(
    *, anchors: dict, wanted, order, account: bool, strict: bool, state=()
) -> str:
    """Return the braced Tcl dict `rb::ckpt::arm` takes; every name in it is a plain identifier. `order` is the run's stages."""
    return (
        "{"
        + " ".join(
            [
                "anchors {"
                + " ".join(
                    f"{stage} {{{cmd} {edge}}}"
                    for stage, (cmd, edge) in anchors.items()
                )
                + "}",
                f"wanted {{{' '.join(wanted)}}}",
                "order {"
                + " ".join(f"{stage} {STAGE_INDEX[stage]}" for stage in order)
                + "}",
                f"routed {{{' '.join(ROUTE_FILES)}}}",
                f"state {{{' '.join(state)}}}",
                f"account {int(account)}",
                f"strict {int(strict)}",
            ]
        )
        + "}"
    )


def _procs() -> str:
    return files("rtl_buddy.pnr").joinpath(_TCL_FILE).read_text()


def render_tcl_block(
    run_dir: str,
    stages: tuple[str, ...],
    *,
    order: tuple[str, ...],
    stop_after: str | None = None,
    state: tuple[str, ...] = (),
) -> str:
    """Return the Tcl block the flow template splices in when checkpoints are on, for a one-session run.

    `order` is the run's stages; `state` names the flow globals each checkpoint saves for a later session. Every stage boundary is armed, so each stage's resources are recorded; only `stages`, and the `stop_after` stage, write a checkpoint. The `stop_after` stage is ended by the flow's explicit stop, not by its anchor, so its checkpoint is strict and no later stage starts. The block carries its own leading newline so a run without checkpoints renders the template byte-identically.
    """
    wanted = [s for s in CHECKPOINT_STAGES if s in stages or s == stop_after]
    anchors = {
        stage: a
        for stage, a in STAGE_ANCHORS.items()
        if a is not None and stage != stop_after
    }
    config = _arm_config(
        anchors=anchors,
        wanted=wanted,
        order=order,
        account=True,
        strict=False,
        state=state,
    )
    return (
        f'\nputs ">>> Stage checkpoints (#653)"\n{_procs()}\n'
        f"rb::ckpt::arm {_tcl_quote(run_dir)} "
        f"{_tcl_quote(os.path.join(run_dir, PROGRESS_NAME))} "
        f"{{{' '.join(PROGRESS_STEPS)}}} "
        f"{config}\n"
    )


#: The lines of a stage script that bind it to its run; script identity ignores them.
RUN_BINDING_RE = re.compile(r"^set RB_(RUN|PRED)_DIR .*$", re.MULTILINE)


def render_staged_block(
    stage: str,
    run_dir: str,
    pred_dir: str | None,
    *,
    order: tuple[str, ...],
    session: str = "staged run",
) -> str:
    """Return the checkpoint block of one staged session; `order` is the run's stages and `session` names the session in its first log line.

    `RB_RUN_DIR` is where the session writes its checkpoint and events; `RB_PRED_DIR` holds the checkpoint it starts from. They are the only run-specific lines, so two runs' scripts of a stage compare equal apart from them (see `RUN_BINDING_RE`). A checkpoint that cannot be written fails the session.
    """
    index = STAGE_INDEX[stage]
    lines = [
        "",
        f'puts ">>> Stage {index}_{stage} ({session})"',
        f"set RB_RUN_DIR {_tcl_quote(run_dir)}",
    ]
    if pred_dir is not None:
        lines.append(f"set RB_PRED_DIR {_tcl_quote(pred_dir)}")
    config = _arm_config(
        anchors={},
        wanted=[stage] if stage in CHECKPOINT_STAGES else [],
        order=order,
        account=False,
        strict=True,
        state=SESSION_STATE,
    )
    lines += [
        _procs().rstrip("\n"),
        f"rb::ckpt::arm $RB_RUN_DIR [file join $RB_RUN_DIR {PROGRESS_NAME}] "
        f"{{{' '.join(PROGRESS_STEPS)}}} {config}",
        "",
    ]
    return "\n".join(lines)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_event(run_dir: str, event: str, **fields) -> None:
    record = {"event": event, "t": _now(), **fields}
    with open(os.path.join(run_dir, PROGRESS_NAME), "a") as f:
        f.write(json.dumps(record) + "\n")


def stage_begin(run_dir: str, stage: str) -> None:
    """Record that rb launched a staged session."""
    _append_event(run_dir, "stage_begin", stage=stage, index=STAGE_INDEX[stage])


def stage_end(
    run_dir: str,
    stage: str,
    *,
    returncode: int,
    resources: dict,
    status: str,
    checkpoint: dict | None = None,
) -> None:
    """Record a staged session's exit, the resources rb measured for it and, when it wrote one, its checkpoint's files with their fingerprints.

    The fingerprints are recorded here, not only in the manifest, so a run rb did not see finish can still be resumed.
    """
    root = project_root_or_none(run_dir)
    _append_event(
        run_dir,
        "stage_end",
        stage=stage,
        index=STAGE_INDEX[stage],
        status=status,
        returncode=returncode,
        **resources,
        **({"checkpoint": _relativise(checkpoint, root)} if checkpoint else {}),
    )


def record_event(run_dir: str, event: str, **fields) -> None:
    """Append an event rb itself observed, such as a post-route repair round's timing."""
    _append_event(run_dir, event, **fields)


def record_checkpoint(
    run_dir: str, stage: str, files: dict, *, design: str, source: str
) -> None:
    """Record `stage`'s checkpoint as complete when rb made it from the files of checkpoint `source` (`{kind: file name}` in `run_dir`), as a session records one it wrote."""
    _append_event(
        run_dir,
        "checkpoint",
        stage=stage,
        index=STAGE_INDEX[stage],
        status="ok",
        design=design,
        files=files,
        source=source,
    )


def written_checkpoint(run_dir: str, stage: str) -> dict | None:
    """Return `{kind: file name}` of `stage`'s checkpoint if this run's progress file records it complete."""
    event = _written_checkpoints(read_progress(run_dir)).get(stage)
    return dict(event.get("files") or {}) if event else None


#: Fields of a `stage_end` event that describe what the stage used.
_RESOURCE_FIELDS = ("status", "wall_s", "cpu_s", "peak_rss_kb", "measured_by")


def stage_resources(events: list[dict]) -> dict[str, dict]:
    """Map each stage that ended to its status, wall time, CPU time, peak RSS and how they were measured, in flow order."""
    ended = {
        str(e.get("stage")): {k: e.get(k) for k in _RESOURCE_FIELDS}
        for e in events
        if e.get("event") == "stage_end"
    }
    return {stage: ended[stage] for stage in PNR_STAGES if stage in ended}


def read_progress(run_dir: str) -> list[dict]:
    """Return the complete events in the run's progress file, in order.

    A line that does not parse, such as a write a kill interrupted, is dropped.
    """
    try:
        # Tcl writes the system encoding; errors="replace" keeps a non-UTF-8 byte from failing the read.
        text = Path(run_dir, PROGRESS_NAME).read_text(errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def _written_checkpoints(events: list[dict]) -> dict[str, dict]:
    """Map stage to its `checkpoint` event for checkpoints that completed."""
    return {
        str(e.get("stage")): e
        for e in events
        if e.get("event") == "checkpoint" and e.get("status") == "ok"
    }


def last_step(events: list[dict]) -> dict | None:
    """Return `{"step", "status"}` for the step a run was in when its events stop.

    `status` is `running`, `ok` or `error`; a begin with no end is `running`. A run hook counts as the step `hook <point>`, and a run stopped inside one names the hook, not the command it was running.
    """
    open_steps: list[str] = []
    last: dict | None = None
    for e in events:
        kind = e.get("event")
        step = (
            f"hook {e.get('point')}"
            if kind in ("hook_begin", "hook_end")
            else e.get("step")
        )
        if kind in ("step_begin", "hook_begin"):
            open_steps.append(str(step))
            last = {"step": step, "status": "running"}
        elif kind in ("step_end", "hook_end"):
            if str(step) in open_steps:
                open_steps.remove(str(step))
            last = {"step": step, "status": e.get("status")}
            if e.get("error"):
                last["error"] = e.get("error")
    if open_steps:
        # A command a hook runs belongs to the hook.
        hooks = [s for s in open_steps if s.startswith("hook ")]
        return {"step": (hooks or open_steps)[-1], "status": "running"}
    return last


def checkpoint_label(stage: str) -> dict:
    """Return the flags stating a checkpoint is not final and whether it is global- and detail-routed."""
    global_routed = stage in GLOBAL_ROUTED
    return {
        "final": False,
        "detail_routed": stage in DETAIL_ROUTED,
        "global_routed": global_routed,
        "congestion": {
            "available": False,
            "reason": _CONGESTION_GR if global_routed else _CONGESTION_PRE_ROUTE,
        },
    }


def _rel(path: str | None, root: str | None) -> str | None:
    return project_relative(path, root) if root and path else path


def begin_run(
    run_dir: str,
    *,
    artefact_dir: str,
    run: str,
    design: str,
    stages: tuple[str, ...],
    openroad: dict,
    inputs: dict,
    staged: dict | None = None,
    mode: str = "closure",
) -> str:
    """Write the manifest and first event, point `latest` here, and return the manifest path.

    Call after the flow scripts exist and just before OpenROAD starts, so `inputs.script` hashes the exact `pnr.tcl` OpenROAD runs. A staged run passes `staged`: its `chain` of stage sessions (script and log per stage, including stages a resume inherits), `resumed_from`, and the checkpoints it inherits, which `finish_run` keeps beside the ones it writes. `stop_after` is recorded for either kind of run, and `chain` for the sessions after detailed route of a one-session run with post-route repair. `mode` is the run's mode.
    """
    root = project_root_or_none(artefact_dir)
    staged = staged or {}
    document = {
        "schema_version": CHECKPOINT_SCHEMA,
        "generator": f"rtl-buddy {version('rtl-buddy')}",
        "run_id": os.path.basename(run_dir),
        "run": run,
        "design": design,
        "mode": mode,
        "started_at": _now(),
        "pid": os.getpid(),
        "tool": openroad,
        "stages": {
            stage: {
                "index": STAGE_INDEX[stage],
                "name": f"{STAGE_INDEX[stage]}_{stage}",
                **checkpoint_label(stage),
            }
            for stage in stages
        },
        "inputs": _relativise(inputs, root),
        **{
            key: _relativise(staged[key], root)
            for key in ("staged", "stop_after", "resumed_from", "chain")
            if staged.get(key) is not None
        },
        # Set by `finish_run`; stays None if rb itself is killed.
        "outcome": None,
        "checkpoints": _relativise(staged.get("inherited_checkpoints") or {}, root),
        "resources": {},
    }
    _write_manifest(run_dir, document)
    _append_event(
        run_dir, "run_start", run_id=document["run_id"], run=run, design=design
    )
    try:
        _point_latest(artefact_dir, run_dir)
    except OSError as e:
        # Filesystems without symlinks lose only the pointer, not the run.
        log_event(
            logger,
            logging.WARNING,
            "pnr.checkpoint_latest_failed",
            run=run,
            dir=run_dir,
            error=str(e),
        )
    return os.path.join(run_dir, MANIFEST_NAME)


def _relativise(value, root):
    if isinstance(value, dict):
        return {
            k: (_rel(v, root) if k == "path" else _relativise(v, root))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_relativise(v, root) for v in value]
    return value


def _write_manifest(run_dir: str, document: dict) -> None:
    path = os.path.join(run_dir, MANIFEST_NAME)
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(document, indent=2) + "\n")
    os.replace(tmp, path)


def read_manifest(run_dir: str) -> dict | None:
    try:
        data = json.loads(Path(run_dir, MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != CHECKPOINT_SCHEMA:
        return None
    return data


def _point_latest(artefact_dir: str, run_dir: str) -> None:
    """Atomically re-point `latest` at this run with a relative link."""
    pointer = latest_pointer(artefact_dir)
    tmp = f"{pointer}.tmp-{os.getpid()}"
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.remove(tmp)
    os.symlink(os.path.basename(run_dir), tmp)
    os.replace(tmp, pointer)


def finish_run(
    run_dir: str, *, returncode: int | None, result: str, desc: str, fingerprint
) -> dict:
    """Complete the manifest from the progress file and return a summary.

    Each checkpoint the Tcl side reported is listed with a fingerprint of each file. The summary carries `checkpoint_dir`, `checkpoint_stages` and `last_step`, for the result row and failure log.
    """
    events = read_progress(run_dir)
    written = _written_checkpoints(events)
    step = last_step(events)
    manifest = read_manifest(run_dir) or {}
    root = project_root_or_none(run_dir)
    # Inherited checkpoints, from `begin_run`, stay listed beside this run's own.
    checkpoints = {
        stage: entry
        for stage, entry in (manifest.get("checkpoints") or {}).items()
        if isinstance(entry, dict) and entry.get("inherited_from")
    }
    for stage, event in written.items():
        entry_files = {}
        for kind, name in (event.get("files") or {}).items():
            record = fingerprint(os.path.join(run_dir, str(name)))
            if record is not None:
                record["path"] = _rel(record["path"], root)
            entry_files[kind] = record
        checkpoints[stage] = {
            "index": event.get("index"),
            "written_at": event.get("t"),
            "files": entry_files,
            **checkpoint_label(stage),
        }
    manifest["checkpoints"] = {
        stage: checkpoints[stage] for stage in PNR_STAGES if stage in checkpoints
    }
    manifest["resources"] = stage_resources(events)
    manifest["outcome"] = {
        "finished_at": _now(),
        "openroad_returncode": returncode,
        "result": result,
        "desc": desc,
        "last_step": step,
    }
    if manifest.get("schema_version") == CHECKPOINT_SCHEMA:
        _write_manifest(run_dir, manifest)
    _append_event(run_dir, "run_end", result=result, returncode=returncode)
    return {
        "checkpoint_dir": run_dir,
        "checkpoint_stages": list(written),
        "last_step": step,
    }


@dataclass(frozen=True)
class CheckpointRef:
    """One written checkpoint, resolved for `rb pnr-export --checkpoint`."""

    run_dir: str
    run_id: str
    stage: str
    index: str
    def_path: str
    design: str | None

    @property
    def name(self) -> str:
        return f"{self.index}_{self.stage}"

    def export_dir(self) -> str:
        return os.path.join(self.run_dir, EXPORT_DIRNAME, self.name)

    def provenance(self, root: str | None) -> dict:
        return {
            "run_id": self.run_id,
            "stage": self.stage,
            "name": self.name,
            "manifest": _rel(os.path.join(self.run_dir, MANIFEST_NAME), root),
            **checkpoint_label(self.stage),
        }


def _stage_of(token: str) -> str | None:
    """Map `cts` or `03_cts` to `cts`; anything that is not a checkpoint stage to None."""
    if token in CHECKPOINT_STAGES:
        return token
    for stage in CHECKPOINT_STAGES:
        if token == f"{STAGE_INDEX[stage]}_{stage}":
            return stage
    return None


def resolve_checkpoint(artefact_dir: str, spec: str) -> CheckpointRef | str:
    """Resolve a `--checkpoint` value to a `CheckpointRef`, or return the reason it cannot be used.

    Accepted: a stage (`cts` or `03_cts`) of the `latest` run, `<run-id>/<stage>`, or the path of a checkpoint file. The run's progress file must hold a completed `checkpoint` event, so a database cut off mid-write is refused.
    """
    run_dir: str | None = None
    stage: str | None = None
    candidate = os.path.abspath(spec)
    if os.path.isfile(candidate):
        run_dir = os.path.dirname(candidate)
        stage = _stage_of(os.path.splitext(os.path.basename(candidate))[0])
        if stage is None:
            return f"{spec} is not a checkpoint file (<NN>_<stage>.<ext>)"
    else:
        head, _, tail = spec.rpartition("/")
        stage = _stage_of(tail)
        if stage is None:
            return (
                f"unknown checkpoint {spec!r}: name a stage "
                f"({', '.join(CHECKPOINT_STAGES)}), <run-id>/<stage>, or a "
                "checkpoint file"
            )
        if head:
            if not _RUN_ID_RE.match(head):
                return f"{head!r} is not a checkpoint run id"
            run_dir = os.path.join(checkpoints_root(artefact_dir), head)
        else:
            pointer = latest_pointer(artefact_dir)
            if not os.path.isdir(pointer):
                return (
                    f"no current checkpointed run at {pointer} — run rb pnr "
                    "with checkpoints: set, or name <run-id>/<stage>"
                )
            # Joined, not resolved, so a symlinked `artefacts/` keeps its project path.
            run_dir = os.path.join(checkpoints_root(artefact_dir), os.readlink(pointer))
    if not os.path.isdir(run_dir):
        return f"no checkpoint run directory at {run_dir}"
    written = _written_checkpoints(read_progress(run_dir))
    event = written.get(stage)
    index = STAGE_INDEX[stage]
    if event is None:
        manifest = read_manifest(run_dir) or {}
        source = ((manifest.get("checkpoints") or {}).get(stage) or {}).get(
            "inherited_from"
        )
        if source:
            return (
                f"checkpoint {index}_{stage} of resumed run "
                f"{os.path.basename(run_dir)} was written by run {source}; "
                f"name {source}/{stage}"
            )
        return (
            f"checkpoint {index}_{stage} was not written by run "
            f"{os.path.basename(run_dir)} (see {os.path.join(run_dir, PROGRESS_NAME)})"
        )
    def_name = (event.get("files") or {}).get("def")
    if not def_name:
        return f"checkpoint {index}_{stage} records no DEF"
    return CheckpointRef(
        run_dir=run_dir,
        run_id=os.path.basename(run_dir),
        stage=stage,
        index=index,
        def_path=os.path.join(run_dir, str(def_name)),
        design=event.get("design"),
    )
