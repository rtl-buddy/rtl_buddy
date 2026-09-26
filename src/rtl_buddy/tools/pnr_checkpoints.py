"""Stage checkpoints and progress for `rb pnr` runs (#653).

A routing run that hits a scheduler wall limit used to leave nothing but its
log: the flow writes its only DEF / ODB after detailed routing. With
`checkpoints:` set in pnr.yaml the generated flow also writes a stage-named
database at each stage boundary and appends a JSON-lines event as every flow
step starts and ends, so the stage a killed run was in and the physical
state it had reached both survive it.

Layout, under the run's artefact directory::

    checkpoints/
      latest -> 20260925T101500-4242      # the run that started last
      20260925T101500-4242/
        manifest.json                     # inputs + tool identity (Python)
        progress.jsonl                    # step / checkpoint events (Tcl)
        01_floorplan.{odb,def,sdc}
        02_place.{odb,def,sdc}
        03_cts.{odb,def,sdc}
        04_global_route.{odb,def,sdc,guide,segments}
        export/03_cts/...                 # `rb pnr-export --checkpoint`

Three rules hold it together.

- **A checkpoint is never a final output.** The files carry the stage in
  their name and live below ``checkpoints/``, which the up-front clear and
  `rb power`'s fixed ``<top>.routed.odb`` path never reach. The routed
  outputs keep their own lifecycle — cleared up front, cleared again on any
  failure (#469) — untouched by this.
- **One directory per run, never reused.** Checkpoints exist to survive a
  failure, so a run cannot clear them the way it clears its routed outputs;
  instead each run writes into its own run-id directory, and a later run
  neither overwrites nor deletes an earlier one's. The ``latest`` pointer is
  what says which run is current: it is removed first thing by every
  `rb pnr` run (checkpointed or not) and re-pointed by a checkpointed run
  only as it launches OpenROAD, so an older run's files can never read as the
  current run's.
- **Python records identity, Tcl records events.** The manifest — hashes of
  every input and of the generated `pnr.tcl`, the OpenROAD version, the
  requested stages — is written once before OpenROAD starts and completed
  after it exits. The Tcl side only appends events, closing the file after
  each, so the progress file is complete up to the moment of a kill.

Resume is deliberately not implemented; the manifest is shaped for it. A
resume from ``04_global_route`` needs the segments as well as the guides:
`read_guides` restores the guides but explicitly not the parasitics a
global-route estimate is made from, which only `read_global_route_segments`
brings back.
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

from ..logging_utils import log_event
from .artifact_paths import project_relative, project_root_or_none

logger = logging.getLogger(__name__)

CHECKPOINTS_DIRNAME = "checkpoints"
LATEST_NAME = "latest"
MANIFEST_NAME = "manifest.json"
PROGRESS_NAME = "progress.jsonl"
#: Where `rb pnr-export --checkpoint` puts what it exports, inside the
#: checkpoint's run directory — never beside the routed outputs, where a
#: pre-route layout would sit at the path a routed one is read from.
EXPORT_DIRNAME = "export"

#: Bumped when the manifest or the progress events change incompatibly.
CHECKPOINT_SCHEMA = 1

_TCL_FILE = "checkpoints.tcl"

#: stage -> (file index, flow command, trace edge). A stage's checkpoint is
#: written on the way into the first command of the *next* stage, or — for
#: global routing — on a clean exit from the router, so the database is
#: exactly the one the stage left. Keyed on commands rather than on lines
#: of the template so the flow's stage sequence stays the template's alone.
STAGE_ANCHORS = {
    "floorplan": ("01", "global_placement", "enter"),
    "place": ("02", "clock_tree_synthesis", "enter"),
    "cts": ("03", "global_route", "enter"),
    "global_route": ("04", "global_route", "leave"),
}

#: The flow commands whose start and end are progress events. A command the
#: flow does not call just produces no event; one OpenROAD does not have is
#: skipped when the traces are armed.
PROGRESS_STEPS = (
    "link_design",
    "read_sdc",
    "initialize_floorplan",
    "place_pins",
    "insert_tiecells",
    "pdngen",
    "global_placement",
    "repair_design",
    "detailed_placement",
    "clock_tree_synthesis",
    "repair_timing",
    "check_placement",
    "global_route",
    "detailed_route",
    "filler_placement",
)

#: Why no checkpoint carries a congestion grid. A reader must see
#: "unavailable", never an empty grid that reads as zero congestion.
_CONGESTION_PRE_ROUTE = "no global route at this stage"
_CONGESTION_GR = (
    "the flow's global_route writes no congestion report; the route guides "
    "and segments are the retained routing state"
)

#: A run id is a timestamp and a pid, optionally de-duplicated: one safe
#: path segment, sortable by start time.
_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}-\d+(?:-\d+)?$")


def checkpoints_root(artefact_dir: str) -> str:
    return os.path.join(artefact_dir, CHECKPOINTS_DIRNAME)


def latest_pointer(artefact_dir: str) -> str:
    return os.path.join(checkpoints_root(artefact_dir), LATEST_NAME)


def allocate_run_dir(artefact_dir: str, *, now: datetime | None = None) -> str:
    """Create and return a fresh run directory; never an existing one.

    `os.mkdir` failing on an existing path is the no-overwrite guarantee:
    a second run that lands on the same id (same second, same pid) takes a
    suffixed one rather than writing into the first run's checkpoints.
    """
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
    """A Tcl double-quoted word with every substitution suppressed."""
    escaped = value.replace("\\", "\\\\")
    for char in ("$", "[", "]", '"'):
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def render_tcl_block(run_dir: str, stages: tuple[str, ...]) -> str:
    """The Tcl the flow template splices in when checkpoints are on.

    Carries its own leading newline, like the other optional blocks, so the
    substitution point is an existing blank line of the template and a run
    without checkpoints renders byte-identically.
    """
    anchors = " ".join(
        "{" + f"{stage} {{{' '.join(STAGE_ANCHORS[stage])}}}" + "}" for stage in stages
    )
    procs = files("rtl_buddy.pnr").joinpath(_TCL_FILE).read_text()
    return (
        f'\nputs ">>> Stage checkpoints (#653)"\n{procs}\n'
        f"rb::ckpt::arm {_tcl_quote(run_dir)} "
        f"{_tcl_quote(os.path.join(run_dir, PROGRESS_NAME))} "
        f"{{{' '.join(PROGRESS_STEPS)}}} "
        f"[concat {anchors}]\n"
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_event(run_dir: str, event: str, **fields) -> None:
    record = {"event": event, "t": _now(), **fields}
    with open(os.path.join(run_dir, PROGRESS_NAME), "a") as f:
        f.write(json.dumps(record) + "\n")


def read_progress(run_dir: str) -> list[dict]:
    """Every complete event in the run's progress file, in order.

    A line that does not parse — the tail of a write a kill interrupted —
    is dropped rather than failing the reader: the events before it are
    exactly what the file exists to keep.
    """
    try:
        # Tcl writes the file in the system encoding; a byte that is not
        # UTF-8 must cost one event's text, not the reader (#653).
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
    """stage -> its `checkpoint` event, for checkpoints that completed."""
    return {
        str(e.get("stage")): e
        for e in events
        if e.get("event") == "checkpoint" and e.get("status") == "ok"
    }


def last_step(events: list[dict]) -> dict | None:
    """The step a run was in when its events stop, and how it stood.

    ``{"step": ..., "status": "running" | "ok" | "error"}``: a begin with no
    end is a step the run never left — the wall-limit case.
    """
    open_steps: list[str] = []
    last: dict | None = None
    for e in events:
        if e.get("event") == "step_begin":
            open_steps.append(str(e.get("step")))
            last = {"step": e.get("step"), "status": "running"}
        elif e.get("event") == "step_end":
            if str(e.get("step")) in open_steps:
                open_steps.remove(str(e.get("step")))
            last = {"step": e.get("step"), "status": e.get("status")}
            if e.get("error"):
                last["error"] = e.get("error")
    if open_steps:
        return {"step": open_steps[-1], "status": "running"}
    return last


def checkpoint_label(stage: str) -> dict:
    """What a checkpoint is and is not, spelled out for every reader."""
    global_routed = stage == "global_route"
    return {
        "final": False,
        "detail_routed": False,
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
) -> str:
    """Write the manifest and the first event, then point ``latest`` here.

    Called once the flow script exists and just before OpenROAD starts, so
    a manifest always describes a run that really launched, and its
    ``inputs.script`` hash is of the exact `pnr.tcl` OpenROAD was given.
    """
    root = project_root_or_none(artefact_dir)
    document = {
        "schema_version": CHECKPOINT_SCHEMA,
        "generator": f"rtl-buddy {version('rtl-buddy')}",
        "run_id": os.path.basename(run_dir),
        "run": run,
        "design": design,
        "started_at": _now(),
        "pid": os.getpid(),
        "tool": openroad,
        "stages": {
            stage: {
                "index": STAGE_ANCHORS[stage][0],
                "name": f"{STAGE_ANCHORS[stage][0]}_{stage}",
                **checkpoint_label(stage),
            }
            for stage in stages
        },
        "inputs": _relativise(inputs, root),
        # Filled in by `finish_run`; absent when the rb process itself was
        # killed, in which case `progress.jsonl` is the whole account.
        "outcome": None,
        "checkpoints": {},
    }
    _write_manifest(run_dir, document)
    _append_event(
        run_dir, "run_start", run_id=document["run_id"], run=run, design=design
    )
    try:
        _point_latest(artefact_dir, run_dir)
    except OSError as e:
        # A filesystem without symlinks (some SMB/NFS exports, exFAT) costs
        # the convenience pointer, not the run: the checkpoints are still
        # written, and `<run-id>/<stage>` still names them (#653).
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
    """Re-point ``latest`` at this run, atomically (relative link)."""
    pointer = latest_pointer(artefact_dir)
    tmp = f"{pointer}.tmp-{os.getpid()}"
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.remove(tmp)
    os.symlink(os.path.basename(run_dir), tmp)
    os.replace(tmp, pointer)


def finish_run(
    run_dir: str, *, returncode: int | None, result: str, desc: str, fingerprint
) -> dict:
    """Complete the manifest from the progress file; return a summary.

    Every checkpoint the Tcl side reported written is listed with a
    fingerprint of each file, so a later reader (or a resume) can tell the
    database is the one this run wrote. The summary is what the result row
    and the failure log carry: the stages written and the step the run was
    in when it stopped.
    """
    events = read_progress(run_dir)
    written = _written_checkpoints(events)
    step = last_step(events)
    manifest = read_manifest(run_dir) or {}
    root = project_root_or_none(run_dir)
    checkpoints = {}
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
    manifest["checkpoints"] = checkpoints
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
    """`cts`, `03_cts` -> `cts`; anything else -> None."""
    if token in STAGE_ANCHORS:
        return token
    for stage, (index, _cmd, _edge) in STAGE_ANCHORS.items():
        if token == f"{index}_{stage}":
            return stage
    return None


def resolve_checkpoint(artefact_dir: str, spec: str) -> CheckpointRef | str:
    """Resolve a `--checkpoint` value, or return why it cannot be used.

    Accepted spellings: a stage (``cts`` or ``03_cts``) of the ``latest``
    run; ``<run-id>/<stage>`` for an older run; or a path to one of a
    checkpoint's files. Whichever it is, the checkpoint must have a
    completed `checkpoint` event in its run's progress file — a database a
    kill interrupted mid-write is refused rather than exported.
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
                f"({', '.join(STAGE_ANCHORS)}), <run-id>/<stage>, or a "
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
            # Joined, not resolved: an `artefacts/` symlinked to scratch
            # storage keeps reading through the project's own path.
            run_dir = os.path.join(checkpoints_root(artefact_dir), os.readlink(pointer))
    if not os.path.isdir(run_dir):
        return f"no checkpoint run directory at {run_dir}"
    written = _written_checkpoints(read_progress(run_dir))
    event = written.get(stage)
    index = STAGE_ANCHORS[stage][0]
    if event is None:
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
