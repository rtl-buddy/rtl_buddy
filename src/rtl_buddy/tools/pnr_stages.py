"""Stages of the `rb pnr` flow: the flow template split at its stage boundaries, staged-session identity, and the checks a resume must pass.

`flow.tcl.template` carries `{{ @name }}` markers. A stage marker (`floorplan`, `place`, `cts`, `global_route`, `detailed_route`, `finish`) starts that stage's part of the flow. `post_route_repair` has no marker: rb runs its rounds as sessions of their own. A piece marker (`load`, `session`, `design`, `clocks`, `layers`) starts text that a staged session's prelude repeats, and `{{ @/name }}` ends a piece inside a stage. A one-session run renders every marker as nothing, so its script is the flow as written; a staged session is a prelude rb generates from those pieces, followed by its stage's part of the flow.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field

from ..config.pnr import CHECKPOINT_STAGES, PNR_STAGES, POST_ROUTE_REPAIR_STAGE
from ..config.pnr_repair import PnrMode
from . import pnr_checkpoints
from .artifact_paths import project_root_or_none

_MARKER_RE = re.compile(r"\{\{ @(/?\w+) \}\}")

#: Template pieces a staged prelude repeats.
PIECES = ("load", "session", "design", "clocks", "layers")
#: The last line of the head's flow variables.
_VARIABLES_END = "file mkdir $OUT_DIR\n"

#: The stages the flow template marks: every stage but `post_route_repair`.
TEMPLATE_STAGES = tuple(s for s in PNR_STAGES if s != POST_ROUTE_REPAIR_STAGE)


def run_stages(post_route_repair: bool) -> tuple[str, ...]:
    """A run's stages in order: `post_route_repair` only when the run repairs after detailed route."""
    return PNR_STAGES if post_route_repair else TEMPLATE_STAGES


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
        stages = [s.marker for s in self.segments if s.marker in TEMPLATE_STAGES]
        if stages != list(TEMPLATE_STAGES):
            raise ValueError(
                f"flow template stage markers {stages} != {TEMPLATE_STAGES}"
            )
        names = [s.marker for s in self.segments]
        missing = [p for p in PIECES if names.count(p) != 1]
        if missing:
            raise ValueError(
                f"flow template needs exactly one of each piece: {missing}"
            )

    def head(self) -> str:
        """Everything before the netlist is read: variables, threads, checkpoints, platform Tcl, hook procs, Liberty and LEF."""
        return self.segments[0].text

    def variables(self) -> str:
        """The head's flow variables, through the line that creates `$OUT_DIR`: what a `session` hook can read."""
        head = self.head()
        end = head.index(_VARIABLES_END) + len(_VARIABLES_END)
        return head[:end]

    def piece(self, name: str) -> str:
        """The text of piece `name`, up to the next marker."""
        return next(s.text for s in self.segments if s.marker == name)

    def body(self, stage: str) -> str:
        """The flow from `stage`'s marker to the next stage's, including pieces inside it."""
        out = []
        inside = False
        for segment in self.segments:
            if segment.marker in TEMPLATE_STAGES:
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
        stop = TEMPLATE_STAGES[TEMPLATE_STAGES.index(stage) + 1]
        out = []
        for s in self.segments:
            if s.marker == stop:
                break
            out.append((marker_text(s.marker) if s.marker else "") + s.text)
        return "".join(out)


def option_error(
    stop_after: str | None,
    resume: str | None,
    resume_from: str | None,
    accept_changes: tuple[str, ...] = (),
) -> str | None:
    """Return why `--stop-after`, `--resume`, `--from` and `--accept-change` cannot be combined as given, or None."""
    if stop_after is not None and stop_after not in CHECKPOINT_STAGES:
        return (
            f"--stop-after {stop_after}: name a stage with a checkpoint "
            f"({', '.join(CHECKPOINT_STAGES)})"
        )
    if (resume is None) != (resume_from is None):
        return "--resume and --from go together"
    if accept_changes and resume is None:
        return "--accept-change needs --resume"
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


def run_option_error(
    stages: tuple[str, ...],
    mode: PnrMode,
    stop_after: str | None,
    resume_from: str | None,
) -> str | None:
    """Return why `--stop-after` or `--from` names a stage this run does not reach, or None.

    `stages` are the run's stages (see `run_stages`); a probe ends after `global_route`.
    """
    for flag, stage in (("--stop-after", stop_after), ("--from", resume_from)):
        if stage is not None and mode is PnrMode.PROBE and after(stage, "global_route"):
            return f"{flag} {stage}: a probe (mode: probe) ends after global_route"
        if stage is not None and stage not in stages:
            return (
                f"{flag} {stage}: this run has no {stage} stage; it runs only "
                "with post-route-repair set"
            )
    return None


def predecessor(stage: str, stages: tuple[str, ...]) -> str | None:
    """The stage before `stage` among a run's `stages`, or None for the first."""
    index = stages.index(stage)
    return stages[index - 1] if index else None


def after(stage: str, other: str) -> bool:
    """Whether `stage` comes after `other` in the flow."""
    return PNR_STAGES.index(stage) > PNR_STAGES.index(other)


#: Printed by each staged session but `finish` once its checkpoint is written.
STAGE_DONE_MARKER = ">>> End of stage "

#: Printed by the flow's last command, which `finish` and a one-session run end on.
FLOW_DONE_MARKER = ">>> DONE"


#: The `threads:` block of a stage script; the thread count is not part of its identity.
_THREADS_RE = re.compile(r'\nputs ">>> Threads"\nset_thread_count [^\n]*\n')


#: A whole-line Tcl comment.
_COMMENT_LINE_RE = re.compile(r"^[ \t]*#[^\n]*\n?", re.MULTILINE)

#: A line of the shared header at the top of every stage script: `set NAME value`.
_HEADER_SET_RE = re.compile(r"^set (\w+)\s")


def _reads(name: str, text: str) -> bool:
    """Whether Tcl `text` reads global variable `name` as `$name`, `${name}`, `$::name` or `${::name}`."""
    name = re.escape(name)
    return re.search(rf"\$(?:\{{(?:::)?{name}\}}|(?:::)?{name}\b)", text) is not None


def _header_and_rest(text: str) -> tuple[list[tuple[str, str]], str]:
    """Split a stage script into its header's `set` lines, as `(name, line)`, and the text after the header.

    The header is the leading run of `set NAME value` lines, comments and blank lines.
    """
    lines = text.splitlines(keepends=True)
    header = []
    end = len(lines)
    for index, line in enumerate(lines):
        match = _HEADER_SET_RE.match(line)
        if match:
            header.append((match.group(1), line))
        elif line.strip() and not line.lstrip().startswith("#"):
            end = index
            break
    return header, "".join(lines[end:])


def session_body(text: str) -> str:
    """A session script without its shared header's `set` lines and its threads block: what a record of one stage's several sessions repeats of each after the first."""
    return _THREADS_RE.sub("", _header_and_rest(text)[1], count=1)


def _identity_text(text: str) -> str:
    """A stage script as far as its checkpoint depends on it.

    That is the script up to its end-of-stage line, without whole-line comments, with the run bindings blanked, the thread count left out, and of the shared header only the `set` lines whose variable the rest of the script reads (see `_reads`), directly or through another such line. What follows the end-of-stage line, `exit` or a `--stop-after` report, does not change the checkpoint, so a stopped run's stage counts as the same stage. A stage may run at another thread count than the stages before it.
    """
    done = text.find(f'puts "{STAGE_DONE_MARKER}')
    if done >= 0:
        end = text.find("\n", done)
        text = text[: end + 1] if end >= 0 else text
    text = _COMMENT_LINE_RE.sub("", text)
    text = _THREADS_RE.sub("", text, count=1)
    text = pnr_checkpoints.RUN_BINDING_RE.sub(r"set RB_\1_DIR <run>", text)
    header, rest = _header_and_rest(text)
    values = dict(header)
    read = {name for name in values if _reads(name, rest)}
    pending = list(read)
    while pending:
        value = values[pending.pop()]
        for name in values:
            if name not in read and _reads(name, value):
                read.add(name)
                pending.append(name)
    return "".join(line for name, line in header if name in read) + rest


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
    return "no line differs as compared now, so another rtl_buddy version recorded it"


#: The first stage that reads each run hook point; `after-extraction` is read first where `rcx-rules` is (see `input_read_before`).
HOOK_STAGE = {
    "after-floorplan": "floorplan",
    "macro-placement": "floorplan",
    "after-pdn": "floorplan",
    "after-pins": "floorplan",
    "after-global-placement": "place",
    "after-repair-design": "place",
    "before-cts": "cts",
    "before-repair-clock-nets": "cts",
    "after-cts": "cts",
    "after-timing-repair": "cts",
    "after-cts-repair": "cts",
    "after-global-route": "global_route",
    "after-global-route-repair": "global_route",
    "before-detailed-route": "detailed_route",
    "before-reports": "finish",
    "after-reports": "finish",
    "stage-end": "floorplan",
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
    "extra_inputs",
)


#: The stages that read the PDK's `rcx-rules`, in flow order.
_RCX_RULES_READERS = ("post_route_repair", "finish")


#: Run hook points that run in more than one stage, with those stages; every other point runs in its `HOOK_STAGE` only.
_HOOK_STAGES = {
    "session": PNR_STAGES,
    "stage-end": PNR_STAGES,
    "after-timing-repair": ("cts", "global_route", "post_route_repair"),
    "after-extraction": _RCX_RULES_READERS,
}


def input_readers(role: str, stages: tuple[str, ...]) -> tuple[str, ...]:
    """The stages of a run of `stages` whose sessions read the manifest input `role`, in flow order.

    The floorplan inputs are read by the floorplan session only; later sessions read its checkpoint. Every session reads the Liberty, LEF, the PDK Tcl hooks other than `tracks-tcl` and `tapcell-tcl`, and the files under `inputs:`, which rb cannot place in a stage.
    """
    point = role[5:].replace("_", "-") if role.startswith("hook_") else None
    if role == "rcx_rules":
        readers = _RCX_RULES_READERS
    elif role in _FLOORPLAN_INPUTS and role != "extra_inputs":
        readers = ("floorplan",)
    elif point in _HOOK_STAGES:
        readers = _HOOK_STAGES[point]
    elif point in HOOK_STAGE:
        readers = (HOOK_STAGE[point],)
    else:
        readers = PNR_STAGES
    return tuple(s for s in stages if s in readers)


def input_read_before(
    role: str, stage: str, stages: tuple[str, ...] = PNR_STAGES
) -> bool:
    """Whether a session before `stage` of a run of `stages` reads the manifest input `role`.

    Every session reads the Liberty, LEF, `platform-tcl`, `layer-rc-tcl` and `session` hooks, and so does any role not named here. `rcx_rules` and the `after-extraction` hooks are read first by the run's post-route repair, else by its finish.
    """
    if role == "script":
        return False
    if role in ("rcx_rules", "hook_after_extraction"):
        first = next((s for s in _RCX_RULES_READERS if s in stages), "finish")
    elif role in _FLOORPLAN_INPUTS:
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
    #: The differences from a fresh run that `--accept-change` accepted, paths absolute; one an earlier resume accepted names that run in `inherited_from`.
    accepted: list[dict] = field(default_factory=list)


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


def is_run_name(spec: str) -> bool:
    """Whether a `--resume` value names a run under the run's checkpoints (`latest` or a run id) rather than a directory."""
    return spec == pnr_checkpoints.LATEST_NAME or bool(
        pnr_checkpoints._RUN_ID_RE.match(spec)
    )


def locate_run(artefact_dir: str, spec: str) -> str | None:
    """Return the run directory `--resume` names: `latest` or a run id under this run's checkpoints, else an absolute directory."""
    root = pnr_checkpoints.checkpoints_root(artefact_dir)
    if spec == pnr_checkpoints.LATEST_NAME:
        pointer = pnr_checkpoints.latest_pointer(artefact_dir)
        return (
            os.path.join(root, os.readlink(pointer))
            if os.path.islink(pointer)
            else None
        )
    if is_run_name(spec):
        path = os.path.join(root, spec)
        return path if os.path.isdir(path) else None
    if os.path.isabs(spec):
        return spec if os.path.isdir(spec) else None
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
    stages: tuple[str, ...],
    mode: PnrMode,
    openroad_version: str | None,
    scripts: dict[str, str],
    inputs: dict,
    fingerprint,
    accept: tuple[str, ...] = (),
    base_dir: str = ".",
) -> ResumeSource | str:
    """Return the run to resume from `from_stage`, or why it cannot be.

    `stages` are the resuming run's stages and `mode` its mode. The run must be staged, must not be a probe when the resuming run is a closure, and its stage before `from_stage` must have finished with its checkpoint written and unchanged since. Every stage before `from_stage` must regenerate the same script today (`scripts`, stage -> text) and read the same input files (`inputs`, the manifest roles, project-relative), and OpenROAD must be the same version. `fingerprint` hashes a file the way the manifest does.

    A stage script or input file that differs is refused unless an `accept` value names it: `script` or `script:<stage>` a script, a path relative to `base_dir` or absolute an input file. A difference an earlier resume of the run accepted needs no value while the script or file is as it accepted. The refusal lists every difference with the value that accepts it; a value that names none refuses too.
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
    if manifest.get("mode") == PnrMode.PROBE and mode is PnrMode.CLOSURE:
        return (
            f"{where} is a probe (mode: probe), which skips timing repair; a probe "
            "cannot be resumed into a closure run. Run the closure from the start"
        )
    pred = predecessor(from_stage, stages)
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

    inherited = stages[: stages.index(from_stage)]
    old_accepted = _absolutise(manifest.get("accepted_changes") or [], root)
    found: list[_Difference] = []
    carried: list[_Difference] = []
    for stage in inherited:
        entry = chain.get(stage)
        if entry is None:
            found.append(
                _Difference(f"it records no script for stage {stage}", stage=stage)
            )
            continue
        recorded = (entry.get("script") or {}).get("identity")
        path = (entry.get("script") or {}).get("path")
        ran = None
        if path and os.path.isfile(path):
            with open(path) as f:
                ran = f.read()
        now = script_identity(scripts[stage])
        # The recorded script is compared again, so a run recorded by another rtl_buddy that compares less of a script still resumes.
        if now in (recorded, ran is not None and script_identity(ran)):
            continue
        earlier = next(
            (
                r
                for r in old_accepted
                if r.get("stage") == stage and r.get("new_identity") == now
            ),
            None,
        )
        if earlier is not None:
            carried.append(_Difference.carried(earlier, run_id))
            continue
        name = f"{pnr_checkpoints.STAGE_INDEX[stage]}_{stage}.tcl"
        first = first_difference(ran, scripts[stage]) if ran else None
        change = script_change(stage)
        found.append(
            _Difference(
                f"the {name} script generated now differs from the one it ran"
                + (f": {first}" if first else ""),
                change,
                {
                    "change": change,
                    "stage": stage,
                    "script": name,
                    "old_identity": recorded,
                    "new_identity": now,
                    "first_difference": first,
                },
                stage=stage,
            )
        )

    old_inputs = _absolutise(manifest.get("inputs") or {}, root)
    for role in [*inputs, *(r for r in old_inputs if r not in inputs)]:
        if not input_read_before(role, from_stage, stages):
            continue
        old, new = old_inputs.get(role), inputs.get(role)
        with_path = role in _PATH_COMPARED_INPUTS
        if _content(old, with_path) == _content(new, with_path):
            continue
        files = _changed_files(old, new, with_path)
        if files is None:
            if new is None:
                reason = f"input {role} is no longer configured"
            elif old is None:
                reason = f"input {role} is newly configured: {_describe(new)}"
            else:
                reason = (
                    f"input {role} lists other files: "
                    f"{_describe(old)} then, {_describe(new)} now"
                )
            found.append(_Difference(reason, paths=_resolved(old) | _resolved(new)))
            continue
        readers = input_readers(role, stages)
        for was, now_file in files:
            what = f"input {role} differs: {_describe(was)} then, {_describe(now_file)} now"
            paths = _resolved(was) | _resolved(now_file)
            if was.get("sha256") is None or now_file.get("sha256") is None:
                state = (
                    "was unreadable then"
                    if now_file.get("sha256")
                    else "is missing or unreadable"
                )
                found.append(
                    _Difference(
                        f"input {role} {now_file.get('path')} {state}", paths=paths
                    )
                )
            elif all(after(from_stage, s) for s in readers):
                found.append(
                    _Difference(
                        f"{what}, but only the stages before --from {from_stage} "
                        f"read it; {_rerun_from(readers[0], stages)}",
                        paths=paths,
                    )
                )
            else:
                change = input_change(now_file["path"], base_dir)
                found.append(
                    _Difference(
                        what,
                        change,
                        {
                            "change": change,
                            "role": role,
                            "old": _digest(was),
                            "new": _digest(now_file),
                        },
                        paths=paths,
                    )
                )
    now_digests = {
        path: record.get("sha256")
        for value in inputs.values()
        for record in _file_records(value)
        for path in _resolved(record)
    }
    carried += [
        _Difference.carried(r, run_id)
        for r in old_accepted
        if r.get("role")
        and input_read_before(r["role"], from_stage, stages)
        and now_digests.get(next(iter(_resolved(r.get("new"))), None))
        == (r.get("new") or {}).get("sha256")
    ]

    for kind, file in (record.get("files") or {}).items():
        path = (file or {}).get("path")
        if not path or not os.path.isfile(path):
            found.append(
                _Difference(
                    f"checkpoint file {kind} of stage {pred} is missing: {path}"
                )
            )
            continue
        now = fingerprint(path) or {}
        if file.get("sha256") and now.get("sha256") != file.get("sha256"):
            found.append(
                _Difference(f"checkpoint file {path} changed since the run wrote it")
            )

    named = {v: [d for d in found + carried if d.named_by(v, base_dir)] for v in accept}
    accepted = {d for ds in named.values() for d in ds if d.change}
    problems = [
        f"{ACCEPT_FLAG} {v} "
        + ("names a difference no value accepts" if ds else "names no difference")
        for v, ds in named.items()
        if not any(d.change for d in ds)
    ]
    if problems:
        listed = [d.described() for d in found] or [
            "no inherited script or input differs"
        ]
        return f"{where}: " + "; ".join(problems + listed)
    refused = [d for d in found if d not in accepted]
    if refused:
        hint = (
            [f"{ACCEPT_FLAG} {ACCEPT_SCRIPTS} accepts every stage script"]
            if sum(1 for d in refused if d.change and d.stage) > 1
            else []
        )
        return f"{where}: " + "; ".join([d.described() for d in refused] + hint)

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
        accepted=[d.record for d in carried + found if d.record],
    )


#: The `--resume` option that accepts a named difference.
ACCEPT_FLAG = "--accept-change"
#: The `--accept-change` value that accepts every inherited stage script that differs; `script:<stage>` accepts one stage's.
ACCEPT_SCRIPTS = "script"


def script_change(stage: str) -> str:
    """The `--accept-change` value that accepts a difference in `stage`'s script."""
    return f"{ACCEPT_SCRIPTS}:{stage}"


def input_change(path: str, base_dir: str) -> str:
    """The `--accept-change` value that names input file `path`: relative to `base_dir`, the invocation directory, when under it, else absolute."""
    path = os.path.abspath(path)
    relative = os.path.relpath(path, base_dir)
    return path if relative.split(os.sep)[0] == os.pardir else relative


@dataclass(eq=False)
class _Difference:
    """A difference from a fresh run: why it refuses the resume, and the `--accept-change` value that accepts it with its manifest record, both None when no value does.

    `stage` is the stage of a script difference; `paths` are the resolved paths of an input difference's files, by which a value names it.
    """

    reason: str
    change: str | None = None
    record: dict | None = None
    stage: str | None = None
    paths: frozenset[str] = frozenset()

    @classmethod
    def carried(cls, record: dict, run_id: str) -> "_Difference":
        """A difference an earlier resume accepted that still holds, recorded by run `run_id` or the run it names."""
        record = {**record, "inherited_from": record.get("inherited_from") or run_id}
        return cls(
            "",
            record.get("change"),
            record,
            stage=record.get("stage"),
            paths=_resolved(record.get("new")),
        )

    def described(self) -> str:
        return self.reason + (f" ({ACCEPT_FLAG} {self.change})" if self.change else "")

    def named_by(self, value: str, base_dir: str) -> bool:
        if value == ACCEPT_SCRIPTS or value.startswith(f"{ACCEPT_SCRIPTS}:"):
            return self.stage is not None and value in (
                ACCEPT_SCRIPTS,
                script_change(self.stage),
            )
        return os.path.realpath(os.path.join(base_dir, value)) in self.paths


def _file_records(value) -> list[dict]:
    """The file records of a manifest input: one, a list of them, or none."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _resolved(value) -> frozenset[str]:
    """The resolved paths of a manifest input's files."""
    return frozenset(
        os.path.realpath(r["path"]) for r in _file_records(value) if r.get("path")
    )


def _rerun_from(first: str, stages: tuple[str, ...]) -> str:
    if first == stages[0]:
        return "run without --resume"
    return f"resume --from {first} or earlier"


def _changed_files(old, new, with_path: bool) -> list[tuple[dict, dict]] | None:
    """The `(old, new)` file records of a manifest input whose content differs, or None when the input names other files: configured or dropped, another number of files, or for `with_path` another file."""
    if isinstance(old, dict) and isinstance(new, dict):
        olds, news = [old], [new]
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        olds, news = old, new
    else:
        return None
    pairs = list(zip(olds, news))
    if not all(isinstance(a, dict) and isinstance(b, dict) for a, b in pairs):
        return None
    if with_path and any(
        os.path.realpath(a.get("path") or "") != os.path.realpath(b.get("path") or "")
        for a, b in pairs
    ):
        return None
    return [(a, b) for a, b in pairs if a.get("sha256") != b.get("sha256")]


def _digest(record: dict) -> dict:
    return {"path": record.get("path"), "sha256": record.get("sha256")}


#: Manifest input roles whose paths appear in no stage script, so a resume compares each file's resolved path as well as its digest.
_PATH_COMPARED_INPUTS = ("extra_inputs",)


def _content(value, with_path: bool = False):
    """A manifest input as comparable content: each file's digest, with its resolved path when `with_path`. Where the other inputs are read from is in the scripts, which are compared apart."""
    if isinstance(value, list):
        return [_content(v, with_path) for v in value]
    if isinstance(value, dict):
        if not with_path:
            return value.get("sha256")
        path = value.get("path")
        return (path and os.path.realpath(path), value.get("sha256"))
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
