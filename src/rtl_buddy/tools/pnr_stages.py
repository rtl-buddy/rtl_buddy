"""Stages of the `rb pnr` flow: the flow template split at its stage boundaries, staged-session identity, and the checks a resume must pass.

`flow.tcl.template` carries `{{ @name }}` markers. A stage marker (`floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `finish`) starts that stage's part of the flow. A piece marker (`load`, `session`, `design`, `clocks`, `layers`) starts text that a staged session's prelude repeats, and `{{ @/name }}` ends a piece inside a stage. A one-session run renders every marker as nothing, so its script is the flow as written; a staged session is a prelude rb generates from those pieces, followed by its stage's part of the flow.
"""

import hashlib
import os
import re
from dataclasses import dataclass

from ..config.pnr import CHECKPOINT_STAGES, PNR_STAGES
from . import pnr_checkpoints
from .artifact_paths import project_root_or_none

_MARKER_RE = re.compile(r"\{\{ @(/?\w+) \}\}")

#: Template pieces a staged prelude repeats.
PIECES = ("load", "session", "design", "clocks", "layers")


@dataclass(frozen=True)
class Segment:
    """Template text after one marker; `marker` is "" for the text before the first."""

    marker: str
    text: str


class FlowTemplate:
    """The flow template, split at its markers."""

    def __init__(self, template: str):
        parts = _MARKER_RE.split(template)
        self.segments = [Segment("", parts[0])] + [
            Segment(marker, text) for marker, text in zip(parts[1::2], parts[2::2])
        ]
        stages = [s.marker for s in self.segments if s.marker in PNR_STAGES]
        if stages != list(PNR_STAGES):
            raise ValueError(f"flow template stage markers {stages} != {PNR_STAGES}")
        names = [s.marker for s in self.segments]
        missing = [p for p in PIECES if names.count(p) != 1]
        if missing:
            raise ValueError(
                f"flow template needs exactly one of each piece: {missing}"
            )

    def head(self) -> str:
        """Everything before the netlist is read: variables, threads, checkpoints, platform Tcl, hook procs, Liberty and LEF."""
        return self.segments[0].text

    def piece(self, name: str) -> str:
        """The text of piece `name`, up to the next marker."""
        return next(s.text for s in self.segments if s.marker == name)

    def body(self, stage: str) -> str:
        """The flow from `stage`'s marker to the next stage's, including pieces inside it."""
        out = []
        inside = False
        for segment in self.segments:
            if segment.marker in PNR_STAGES:
                if inside:
                    break
                inside = segment.marker == stage
            if inside:
                out.append(segment.text)
        return "".join(out)

    def whole(self, marker_text=lambda _marker: "") -> str:
        """The one-session flow, with `marker_text(marker)` in place of each marker."""
        return "".join(
            (marker_text(s.marker) if s.marker else "") + s.text for s in self.segments
        )

    def through(self, stage: str, marker_text=lambda _marker: "") -> str:
        """The one-session flow up to the end of `stage`, as `whole` renders it."""
        stop = PNR_STAGES[PNR_STAGES.index(stage) + 1]
        out = []
        for s in self.segments:
            if s.marker == stop:
                break
            out.append((marker_text(s.marker) if s.marker else "") + s.text)
        return "".join(out)


def option_error(
    stop_after: str | None, resume: str | None, resume_from: str | None
) -> str | None:
    """Return why `--stop-after`, `--resume` and `--from` cannot be combined as given, or None."""
    if stop_after is not None and stop_after not in CHECKPOINT_STAGES:
        return (
            f"--stop-after {stop_after}: name a stage with a checkpoint "
            f"({', '.join(CHECKPOINT_STAGES)})"
        )
    if (resume is None) != (resume_from is None):
        return "--resume and --from go together"
    if resume_from is not None and resume_from not in PNR_STAGES[1:]:
        return (
            f"--from {resume_from}: name a stage after the first "
            f"({', '.join(PNR_STAGES[1:])})"
        )
    if (
        stop_after is not None
        and resume_from is not None
        and after(resume_from, stop_after)
    ):
        return f"--stop-after {stop_after} comes before --from {resume_from}"
    return None


def predecessor(stage: str) -> str | None:
    """The stage before `stage`, or None for the first."""
    index = PNR_STAGES.index(stage)
    return PNR_STAGES[index - 1] if index else None


def after(stage: str, other: str) -> bool:
    """Whether `stage` comes after `other` in the flow."""
    return PNR_STAGES.index(stage) > PNR_STAGES.index(other)


#: Printed by each staged session but `finish` once its checkpoint is written.
STAGE_DONE_MARKER = ">>> End of stage "

#: Printed by the flow's last command, which `finish` and a one-session run end on.
FLOW_DONE_MARKER = ">>> DONE"


def _identity_text(text: str) -> str:
    """A stage script as far as its checkpoint depends on it: up to its end-of-stage line, with the run bindings blanked.

    What follows that line, `exit` or a `--stop-after` report, does not change the checkpoint, so a stopped run's stage counts as the same stage.
    """
    done = text.find(f'puts "{STAGE_DONE_MARKER}')
    if done >= 0:
        end = text.find("\n", done)
        text = text[: end + 1] if end >= 0 else text
    return pnr_checkpoints.RUN_BINDING_RE.sub(r"set RB_\1_DIR <run>", text)


def script_identity(text: str) -> str:
    """The SHA-256 of a stage script's checkpoint-relevant text (see `_identity_text`)."""
    return hashlib.sha256(_identity_text(text).encode()).hexdigest()


def first_difference(old: str, new: str) -> str:
    """Name the first line where two stage scripts differ, as `script_identity` compares them."""
    old_lines = _identity_text(old).splitlines()
    new_lines = _identity_text(new).splitlines()
    for number, (a, b) in enumerate(zip(old_lines, new_lines), start=1):
        if a != b:
            return f"line {number}: was {a.strip()!r}, now {b.strip()!r}"
    if len(old_lines) != len(new_lines):
        return f"it was {len(old_lines)} lines, now {len(new_lines)}"
    return "they differ only in whitespace at line ends"


#: The stage that reads each run hook point.
HOOK_STAGE = {
    "after-floorplan": "floorplan",
    "macro-placement": "floorplan",
    "after-pdn": "floorplan",
    "after-pins": "floorplan",
    "after-global-placement": "place",
    "after-repair-design": "place",
    "after-cts": "cts",
    "after-global-route": "global_route",
    "before-detailed-route": "detailed_route",
    "before-reports": "finish",
}

#: Manifest input roles that only the floorplan session reads.
_FLOORPLAN_INPUTS = (
    "netlist",
    "synth_netlist",
    "sdc",
    "pin_constraints",
    "macros_file",
    "pins_file",
    "pdn_config",
    "tracks_tcl",
    "tapcell_tcl",
)


def input_read_before(role: str, stage: str) -> bool:
    """Whether a session before `stage` reads the manifest input `role`.

    Every session reads the Liberty, LEF, `platform-tcl`, `layer-rc-tcl` and `session` hooks, and so does any role not named here.
    """
    if role == "script":
        return False
    if role in _FLOORPLAN_INPUTS:
        first = "floorplan"
    elif role.startswith("hook_") and role[5:].replace("_", "-") in HOOK_STAGE:
        first = HOOK_STAGE[role[5:].replace("_", "-")]
    else:
        first = "floorplan"
    return PNR_STAGES.index(first) < PNR_STAGES.index(stage)


@dataclass
class ResumeSource:
    """A checked staged run to resume, and what the new run inherits from it."""

    run_dir: str
    run_id: str
    from_stage: str
    #: Where the predecessor's checkpoint files are.
    pred_dir: str
    #: Chain entries of the inherited stages, paths absolute.
    chain: list[dict]
    #: Checkpoints of the inherited stages, paths absolute, each naming the run that wrote it.
    checkpoints: dict[str, dict]


def _absolute(path, root: str | None) -> str | None:
    if not path:
        return None
    if os.path.isabs(path) or root is None:
        return path
    return os.path.join(root, path)


def _absolutise(value, root):
    if isinstance(value, dict):
        return {
            k: (_absolute(v, root) if k == "path" else _absolutise(v, root))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_absolutise(v, root) for v in value]
    return value


def locate_run(artefact_dir: str, spec: str) -> str | None:
    """Return the run directory `--resume` names: a directory, `latest`, or a run id under this run's checkpoints."""
    if os.path.isdir(spec) and not os.path.isabs(spec):
        return os.path.abspath(spec)
    if os.path.isabs(spec):
        return spec if os.path.isdir(spec) else None
    root = pnr_checkpoints.checkpoints_root(artefact_dir)
    if spec == pnr_checkpoints.LATEST_NAME:
        pointer = pnr_checkpoints.latest_pointer(artefact_dir)
        return (
            os.path.join(root, os.readlink(pointer))
            if os.path.islink(pointer)
            else None
        )
    if pnr_checkpoints._RUN_ID_RE.match(spec):
        path = os.path.join(root, spec)
        return path if os.path.isdir(path) else None
    return None


def _stage_records(run_dir: str, manifest: dict) -> tuple[dict, dict]:
    """Return `(ended, checkpoints)` for a staged run: each stage's `stage_end` event, and each checkpoint with absolute file paths.

    The progress file is read first, so a run rb did not see finish (killed by a wall limit) still counts; the manifest adds the checkpoints it inherited.
    """
    root = project_root_or_none(run_dir)
    run_id = os.path.basename(run_dir)
    ended: dict[str, dict] = {}
    checkpoints: dict[str, dict] = {}
    for event in pnr_checkpoints.read_progress(run_dir):
        if event.get("event") == "stage_end" and event.get("measured_by") == "process":
            ended[str(event.get("stage"))] = event
            if isinstance(event.get("checkpoint"), dict):
                checkpoints[str(event.get("stage"))] = {
                    "files": _absolutise(event["checkpoint"], root),
                    "inherited_from": run_id,
                }
    for stage, entry in (manifest.get("checkpoints") or {}).items():
        if isinstance(entry, dict) and entry.get("inherited_from"):
            checkpoints[stage] = _absolutise(entry, root)
    return ended, checkpoints


def check_resume(
    run_dir: str,
    from_stage: str,
    *,
    openroad_version: str | None,
    scripts: dict[str, str],
    inputs: dict,
    fingerprint,
) -> ResumeSource | str:
    """Return the run to resume from `from_stage`, or why it cannot be.

    The run must be staged and its stage before `from_stage` must have finished with its checkpoint written and unchanged since. Every stage before `from_stage` must regenerate the same script today (`scripts`, stage -> text) and read the same input files (`inputs`, the manifest roles, project-relative), and OpenROAD must be the same version. `fingerprint` hashes a file the way the manifest does.
    """
    run_id = os.path.basename(run_dir)
    where = f"run {run_id} ({run_dir})"
    manifest = pnr_checkpoints.read_manifest(run_dir)
    if manifest is None:
        return f"{where} has no readable {pnr_checkpoints.MANIFEST_NAME}"
    if not manifest.get("staged"):
        return (
            f"{where} was not a staged run; only a run with stages: true can be resumed"
        )
    pred = predecessor(from_stage)
    if pred is None:
        return "--from floorplan starts from the netlist; run without --resume"
    root = project_root_or_none(run_dir)
    chain = {
        entry.get("stage"): _absolutise(entry, root)
        for entry in manifest.get("chain") or []
        if isinstance(entry, dict)
    }
    ended, checkpoints = _stage_records(run_dir, manifest)
    record = checkpoints.get(pred)
    if pred in ended and ended[pred].get("status") != "ok":
        return (
            f"stage {pred} of {where} did not finish cleanly "
            f"(exit code {ended[pred].get('returncode')})"
        )
    if record is None:
        return f"stage {pred} of {where} did not finish with its checkpoint written"

    old_version = (manifest.get("tool") or {}).get("version")
    if old_version != openroad_version:
        return (
            f"OpenROAD is {openroad_version or 'unknown'}, but {where} ran "
            f"{old_version or 'an unknown version'}"
        )

    inherited = PNR_STAGES[: PNR_STAGES.index(from_stage)]
    for stage in inherited:
        entry = chain.get(stage)
        if entry is None:
            return f"{where} records no script for stage {stage}"
        recorded = (entry.get("script") or {}).get("identity")
        if recorded != script_identity(scripts[stage]):
            name = f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}.tcl"
            path = (entry.get("script") or {}).get("path")
            detail = ""
            if path and os.path.isfile(path):
                with open(path) as f:
                    detail = f": {first_difference(f.read(), scripts[stage])}"
            return (
                f"the {name} script generated now differs from the one {where} "
                f"ran{detail}"
            )

    old_inputs = _absolutise(manifest.get("inputs") or {}, root)
    for role, value in inputs.items():
        if not input_read_before(role, from_stage):
            continue
        if _content(old_inputs.get(role)) != _content(value):
            return (
                f"input {role} differs from what {where} read: "
                f"{_describe(old_inputs.get(role))} then, {_describe(value)} now"
            )
    for role in old_inputs:
        if role not in inputs and input_read_before(role, from_stage):
            return f"input {role} that {where} read is no longer configured"

    for kind, file in (record.get("files") or {}).items():
        path = (file or {}).get("path")
        if not path or not os.path.isfile(path):
            return f"checkpoint file {kind} of stage {pred} is missing: {path}"
        now = fingerprint(path) or {}
        if file.get("sha256") and now.get("sha256") != file.get("sha256"):
            return f"checkpoint file {path} changed since {where} wrote it"

    pred_dir = os.path.dirname(next(iter(record["files"].values()))["path"])
    return ResumeSource(
        run_dir=run_dir,
        run_id=run_id,
        from_stage=from_stage,
        pred_dir=pred_dir,
        chain=[{**chain[stage], "inherited": True} for stage in inherited],
        checkpoints={
            stage: {**checkpoints[stage], **pnr_checkpoints.checkpoint_label(stage)}
            for stage in inherited
            if stage in checkpoints and stage in CHECKPOINT_STAGES
        },
    )


def _content(value):
    """A manifest input as comparable content: each file's digest. Where a file is read from is in the scripts, which are compared apart."""
    if isinstance(value, list):
        return [_content(v) for v in value]
    if isinstance(value, dict):
        return value.get("sha256")
    return value


def _describe(value) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, list):
        return "[" + ", ".join(_describe(v) for v in value) + "]"
    if isinstance(value, dict):
        digest = (value.get("sha256") or "")[:12]
        return f"{value.get('path')} ({digest or 'missing'})"
    return str(value)
