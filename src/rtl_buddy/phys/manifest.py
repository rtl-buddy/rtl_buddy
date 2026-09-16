# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``phys-manifest.json`` — the physical artefact discovery contract (#558).

Every run that produces physical metrics writes one manifest beside its
artefacts. It answers the only two questions a later consumer has: *what
did this run produce*, and *where is it*. Without it, finding last
night's synthesis meant knowing the suite, the run name, and which of
the two synth backends had written the log.

Rules the file keeps, unchanged from the coverage manifest they are
copied from:

* **Stable keys.** The blocks (``synth``, ``power``) are always present,
  and so is every key inside them; a value is ``null`` when that
  artefact was not produced. Absent never means "not produced" — ``null``
  does. ``backend`` is the one to read first: it is ``null`` exactly when
  that half did not run here.
* **Project-relative paths.** Every path is POSIX and relative to the
  project root, so a manifest survives being read from somewhere else,
  archived, or attached to a CI artefact.
* **One per artefact directory**, rewritten whole on each run.

The one place it diverges from ``cov_dir/manifest.json`` is *where* it
lives, and the divergence follows from the flows. Coverage has a
``cov_dir`` — one directory per run, named, that discovery can walk for.
Synthesis and power do not: each writes into the run's own
``artefacts/<name>/``, which it shares with every other command that
happens to carry that name. So the manifest is named
``phys-manifest.json`` rather than ``manifest.json`` (discovery is a
filename match, and it must not collide with a coverage manifest a user
has pointed at the same directory), and there is no ``phys_dir``
convention to key on.

That layout also means a synth run and a power run *can* land in one
directory when they share a name, so the manifest merges the same way
the model does: a run rewrites its own block and carries the other's
forward when the model kept the other half (:func:`merge_manifest`). A
rerun that never gets as far as publishing withdraws its own block the
same way (:func:`blank_block`), so the manifest never points at reports
a stale-clear has since deleted.

Every write carries the ``publication`` token of the model it was
written with — see :func:`rtl_buddy.phys.model.new_publication`.

Schema (``schema_version`` 1)::

    {
      "schema_version": 1,
      "publication": "9f2c…",           # shared with the model of this write
      "generator": "rtl-buddy 6.44.0",
      "generated_at": "2026-09-12T11:04:12+08:00",
      "command": "synth",               # or "power" — this write's producer
      "run": "demo_synth",              # the run name the artefact dir is keyed on
      "top": "demo_top",
      "phys_dir": "verif/demo/artefacts/demo_synth",
      "model": "verif/demo/artefacts/demo_synth/phys-model.json",
      "totals": {"area_um2": .., "cell_count": .., "internal_uw": ..,
                 "switching_uw": .., "leakage_uw": .., "total_uw": ..},
      "synth": {"backend": "yosys"|"openroad"|null, "run": .., "stats": ..,
                "netlist": .., "log": .., "config": {..}|null},
      "power": {"backend": "openroad"|null, "run": .., "netlist_source": ..,
                "report": .., "instances": .., "cells": .., "log": ..,
                "mode": "static"|"dynamic"|null, "activity": {..}|null,
                "config": {..}|null}
    }

The ``config``, ``mode`` and ``activity`` entries are the run's
*identity* (#568), written here as well as into the model's provenance
so a listing of every run in a project — `rb phys runs`, the pane's run
selector — can tell partitions, power modes and optimisation
experiments apart from the manifests alone, without opening a model
per run. Their shapes are :mod:`rtl_buddy.phys.provenance`'s.
Documents written before them carry neither key; every reader here
normalises an absent block to ``null``, which is what the stable-keys
rule promises anyway. The paths *inside* those two blocks — a
constraints file, an activity trace — are project-relative like every
other path in the document, but they are made so one step earlier, by
:func:`rtl_buddy.phys.publish._publish`: the same blocks go into the
model, and relativising each document separately is how the two would
come to spell one path two ways.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: Bumped when the manifest's shape changes incompatibly.
MANIFEST_SCHEMA_VERSION = 1

#: Filename inside the producing run's artefact directory.
# Defined in `tools.artifact_paths` — the bottom of the import graph, and
# where the artefact-clearing helpers protect it from a co-named run's
# suffix clear (#469). Re-exported here, where consumers already look.
from ..tools.artifact_paths import (  # noqa: E402
    ARTIFACT_DIRNAME,
    PHYS_MANIFEST_NAME as MANIFEST_FILENAME,
)

# The model's pairing of each half with the totals it owns. Imported so
# the two merges cannot drift apart on which numbers travel with which
# block; see :func:`merge_manifest`.
from .model import _POWER_TOTALS, _SYNTH_TOTALS  # noqa: E402

#: Keys of the ``synth`` block, so a power-only run still writes them all.
SYNTH_KEYS = ("backend", "run", "stats", "netlist", "log", "config")

#: Keys of the ``power`` block, likewise.
POWER_KEYS = (
    "backend",
    "run",
    "netlist_source",
    "report",
    "instances",
    "cells",
    "log",
    "mode",
    "activity",
    "config",
)

#: Which block keys hold a path and so need making project-relative. The
#: rest are plain strings a `rel()` would mangle into a filename.
PATH_KEYS = frozenset({"stats", "netlist", "log", "report", "instances", "cells"})

#: Each producer block, its keys, and the totals it owns — the manifest
#: side of the model's :data:`~rtl_buddy.phys.model._HALVES`. Named once
#: because :func:`merge_manifest` and :func:`blank_block` both walk it,
#: and a second spelling is how the two would come to disagree about
#: which numbers belong to which block.
_BLOCKS = (
    ("synth", SYNTH_KEYS, _SYNTH_TOTALS),
    ("power", POWER_KEYS, _POWER_TOTALS),
)


def _generator() -> str:
    try:
        return f"rtl-buddy {version('rtl-buddy')}"
    except PackageNotFoundError:  # pragma: no cover - source checkout only
        return "rtl-buddy"


def project_relative(path, project_root) -> str | None:
    """POSIX path relative to the project root, or the path unchanged.

    A path outside the project (an artefact directory on a scratch
    filesystem, say) is kept verbatim rather than turned into a ``../..``
    chain nothing can join on.

    The comparison is made on the *logical* paths first — absolute-ised
    but with no symlink resolved — and only falls back to the resolved
    pair. A suite whose ``artefacts/`` is a link to scratch storage is an
    ordinary setup, the same one :func:`discover_manifests` follows and
    the filelist writer is pinned against, and resolving both operands
    would put the scratch path on both sides of the ``relative_to``,
    match nothing, and write the manifest full of absolute host paths.
    The project-relative rule is about the tree the project is read
    through, not about where the bytes live. Resolving is still worth a
    second try, for the reverse arrangement: a path handed in through a
    link that the project root is *not* reached through.
    """
    if path is None:
        return None
    logical = Path(os.path.abspath(path))
    logical_root = Path(os.path.abspath(project_root))
    try:
        return logical.relative_to(logical_root).as_posix()
    except ValueError:
        pass
    try:
        return Path(path).resolve().relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return str(path)


#: What marks a project root, walking up from an artefact directory. Same
#: two markers :func:`rtl_buddy.config.root.discover_project_root` uses,
#: in the same order.
_ROOT_MARKERS = ("root_config.yaml", ".git")


def project_root_for_dir(artefact_dir) -> str:
    """The project root an artefact directory's paths should hang off.

    Resolved by walking up from the artefact directory rather than taken
    from the root config, because the backends that call this hold a
    suite directory and, in the OpenROAD synthesis case, a ``root_cfg``
    that may legitimately be absent.

    Deliberately not :func:`rtl_buddy.config.root.discover_project_root`,
    close as the walk is: that one logs at ERROR before falling back, and
    a synthesis run outside a project is not an error *here* — it is a
    manifest whose paths are bare filenames, which is still joinable and
    still worth writing.

    Walked on the logical path first, for the reason
    :func:`project_relative` gives and to the same end: an ``artefacts/``
    symlinked to scratch resolves out of the project entirely, and a walk
    that started there would find no root, hand every path back absolute,
    and break the project-relative contract for exactly the layout the
    rest of the module supports. The resolved walk is the fallback, so a
    directory reached through a link from outside the project still finds
    the root it really sits under.
    """
    logical = Path(os.path.abspath(artefact_dir))
    for start in (logical, Path(artefact_dir).resolve()):
        for candidate in (start, *start.parents):
            if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
                return str(candidate)
    return str(logical)


def build_manifest(
    *,
    project_root,
    phys_dir,
    command: str,
    run: str | None = None,
    top: str | None = None,
    model_path=None,
    totals: dict | None = None,
    synth: dict | None = None,
    power: dict | None = None,
) -> dict:
    """Assemble a manifest document with every path project-relative.

    ``synth`` and ``power`` are the two producer blocks; pass only the
    one this run wrote. The other is still emitted, with every key
    ``null`` — that is the stable-keys rule, and it is what lets a
    consumer read ``manifest["power"]["report"]`` without first asking
    whether a power run ever happened here.
    """

    def rel(path):
        return project_relative(path, project_root)

    def block(values, keys):
        values = values or {}
        return {
            key: rel(values.get(key)) if key in PATH_KEYS else values.get(key)
            for key in keys
        }

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        # Stamped by `phys.publish._publish` with the token it also puts
        # on the model this manifest names, so a reader can tell the two
        # were written together; see `phys.model.new_publication`.
        "publication": None,
        "generator": _generator(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": command,
        "run": run,
        "top": top,
        "phys_dir": rel(phys_dir),
        "model": rel(model_path),
        "totals": totals,
        "synth": block(synth, SYNTH_KEYS),
        "power": block(power, POWER_KEYS),
    }


def merge_manifest(existing: dict | None, new: dict, *, own_block: str | None) -> dict:
    """Carry an ``existing`` manifest's other-half block onto ``new``.

    The mirror of :func:`rtl_buddy.phys.model.merge_model`, and it must
    stay in step with it: the model's merge is what makes both halves
    describable from one directory, and a manifest that then pointed at
    only the last run's reports would leave half the document
    unattributable to the tool output behind it.

    ``own_block`` names the block this run produced (``"synth"`` or
    ``"power"``) and, exactly as in the model's merge, that block is
    never inherited. In practice a producer always sets its own
    ``backend``, so the ``backend is not None`` test below already
    excludes it — ``own_block`` is what makes that a rule rather than a
    coincidence of the callers, and keeps a run whose backend name went
    missing from silently republishing the previous run's report paths.

    Otherwise a block is inherited only when the new run left it empty
    (``backend`` null) and the existing manifest describes the same top,
    for the same reason the model's merge checks the top: a
    same-directory artefact from a different design is not evidence
    about this one. Totals travel with their half: a block this run
    re-produced keeps the totals it wrote, nulls included — a scrape
    that failed this time must not republish last run's number.

    The merged document keeps ``new``'s ``publication`` token, for the
    reason :func:`rtl_buddy.phys.model.merge_model` gives: the token
    names the write in progress, not the run whose block was inherited.
    """
    if not isinstance(existing, dict):
        return new
    if existing.get("schema_version") != new.get("schema_version"):
        return new
    if existing.get("top") != new.get("top"):
        return new
    merged = dict(new)
    merged["totals"] = dict(new.get("totals") or {})
    for half, keys, totals_keys in _BLOCKS:
        if half == own_block:
            continue
        block = merged.get(half) or {}
        if block.get("backend") is not None:
            continue
        inherited = existing.get(half)
        if not isinstance(inherited, dict) or inherited.get("backend") is None:
            continue
        merged[half] = {key: inherited.get(key) for key in keys}
        for key in totals_keys:
            value = (existing.get("totals") or {}).get(key)
            if value is not None and merged["totals"].get(key) is None:
                merged["totals"][key] = value
    return merged


def blank_block(manifest: dict, own_block: str) -> dict:
    """``manifest`` with ``own_block``'s keys and totals nulled out.

    The manifest side of :func:`rtl_buddy.phys.model.blank_half`, walking
    the same :data:`_BLOCKS` pairing :func:`merge_manifest` does. Every
    key of the block goes ``null`` — ``backend`` included, which is the
    one a consumer reads first to tell a half that ran here from one that
    did not, and the one :func:`merge_manifest` keys inheritance on.

    The document's own header (``command``, ``run``, ``generated_at``)
    is left as the last publication wrote it: this is not a new
    measurement, it is the withdrawal of one.
    """
    blanked = dict(manifest)
    blanked["totals"] = dict(manifest.get("totals") or {})
    for block, keys, totals_keys in _BLOCKS:
        if block != own_block:
            continue
        blanked[block] = {key: None for key in keys}
        for key in totals_keys:
            blanked["totals"][key] = None
    return blanked


def write_manifest(manifest: dict, phys_dir) -> str:
    """Write ``phys-manifest.json`` into ``phys_dir`` and return its path.

    Temp-then-:func:`os.replace`, for the reason
    :func:`rtl_buddy.phys.model.write_model` gives: discovery walks for
    this file by name, so a reader can arrive mid-rewrite.
    """
    phys_dir = Path(phys_dir)
    phys_dir.mkdir(parents=True, exist_ok=True)
    path = phys_dir / MANIFEST_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def load_manifest(path) -> dict:
    """Read a manifest document back."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_manifest_or_none(phys_dir) -> dict | None:
    """The manifest already in ``phys_dir``, or ``None``.

    The merge's read side; see
    :func:`rtl_buddy.phys.model.load_model_or_none` for why a bad read is
    "nothing to merge" rather than an error.
    """
    try:
        manifest = load_manifest(Path(phys_dir) / MANIFEST_FILENAME)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def resolve(manifest_path, relative_path) -> str | None:
    """Turn a manifest-relative path into an absolute one.

    Paths are relative to the *project root*, not to the manifest, so
    resolution walks up from ``phys_dir`` using the manifest's own
    ``phys_dir`` value. That keeps a manifest joinable after the tree has
    been moved, which a project-root field baked in at write time would
    not.
    """
    if relative_path is None:
        return None
    if os.path.isabs(relative_path):
        return relative_path
    root = project_root_for(manifest_path)
    if root is None:
        return relative_path
    return str(Path(root) / relative_path)


def _joins_back(root, phys_dir: str, phys_dir_abs) -> bool:
    """Whether ``root / phys_dir`` is the directory the manifest is in.

    The check that turns :func:`project_root_for`'s component count into
    an answer that can be wrong out loud rather than quietly. Compared on
    the *resolved* paths, because the whole point is that the two
    spellings may differ by a link.
    """
    joined = os.path.join(str(root), phys_dir)
    return os.path.realpath(joined) == os.path.realpath(phys_dir_abs)


def project_root_for(manifest_path) -> str | None:
    """Infer the project root a manifest's relative paths hang off.

    Counted back up the *logical* path, not the resolved one: the
    ``phys_dir`` the walk consumes is relative to the project root as the
    writer saw it, and an ``artefacts/`` symlinked to scratch resolves to
    a path with none of those components above it (see
    :func:`project_relative`). Walking a resolved path up by
    ``len(phys_dir.parts)`` would then climb out of scratch entirely and
    return a root no manifest path joins onto.

    The count alone is only right when the manifest is *read* through the
    same route it was written through, and one ordinary layout breaks
    that: an ``artefacts/`` link whose target is itself inside the
    project. :func:`discover_manifests` admits each directory once by its
    real path, so whichever of the two routes ``os.walk`` reaches first
    wins — and when that is the target (``scratch_artefacts/<run>/``
    rather than ``verif/demo/artefacts/<run>/``) the count climbs off the
    wrong stem and every path the manifest names resolves to nothing.
    Which route wins is directory-order luck, so the same tree answers
    differently on two machines.

    So the count is *checked*: the root it proposes has to join back onto
    the directory the manifest actually sits in. When it does not, the
    marker walk (:func:`project_root_for_dir`) gets the second try, and
    it is taken only if it joins back too. Failing both, the counted root
    stands — it is no worse than before, and a manifest read from outside
    any project has no better answer available.
    """
    manifest_path = Path(os.path.abspath(manifest_path))
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError):
        return None
    phys_dir = manifest.get("phys_dir")
    if not phys_dir or os.path.isabs(phys_dir):
        return str(manifest_path.parent)
    counted = manifest_path.parent
    for _ in Path(phys_dir).parts:
        counted = counted.parent
    if _joins_back(counted, phys_dir, manifest_path.parent):
        return str(counted)
    walked = project_root_for_dir(manifest_path.parent)
    if _joins_back(walked, phys_dir, manifest_path.parent):
        return walked
    return str(counted)


def may_follow_link(link: str, rel_parts: tuple[str, ...], root_real: str) -> bool:
    """Whether a symlinked directory is part of the artefact layout.

    The boundary :func:`discover_manifests` documents, factored out
    because it is the whole of the rule and reads better named.

    ``link`` is the link itself, ``rel_parts`` the components of its path
    below the project root (its own basename last), ``root_real`` the
    resolved project root.

    Public because the hub's ``?dir=`` route decides the same question
    about the same tree (:func:`rtl_buddy.hub.phys_page
    .contained_phys_dir`). A run the walk refused to enter is a run the
    route must refuse to read: two spellings of one boundary would
    eventually disagree, and the disagreement anyone finds first is the
    one where the route is the looser of the two.
    """
    if ARTIFACT_DIRNAME not in rel_parts:
        return False
    link_real = os.path.realpath(link)
    return not (root_real == link_real or root_real.startswith(link_real + os.sep))


def discover_manifests(project_root) -> list[str]:
    """Every ``phys-manifest.json`` under a project, newest first.

    A bounded walk rather than one fixed path, and — unlike the coverage
    equivalent — it cannot shortcut on the directory name: a physical
    manifest lives in whatever ``artefacts/<run>/`` the run was named
    into, so the filename is the only marker. Version-control and build
    directories are skipped; ties break on the path so the order is
    deterministic on a tree with identical timestamps.

    **Symlinked directories are followed only inside the artefact
    layout.** A suite whose ``artefacts/`` is a link to scratch storage
    is an ordinary, documented setup — the same one the filelist writer
    is pinned against — and a walk that did not follow it would report a
    project with no physical data at all. Following *every* link is the
    other error: a ``vendor/`` link, or a link to ``$HOME``, drags an
    unrelated tree into the project's walk, which is slow and — worse —
    reports someone else's ``phys-manifest.json`` as this project's own
    run. So a link is descended into only when
    :data:`~rtl_buddy.tools.artifact_paths.ARTIFACT_DIRNAME` is already a
    component of its path below the project root — its own basename
    (``<suite>/artefacts -> scratch``, the supported case) or an
    ``artefacts`` above it (a run directory inside an ``artefacts/``
    subtree linked out individually). Real directories are walked as
    before; the boundary is only about links.

    Two guards sit under that rule. A link whose realpath is the project
    root or an ancestor of it is refused outright, so no admitted link
    can circle back over the whole tree (or over ``/``). And each
    directory is admitted once by its real path, so a run reachable by
    two paths is listed once and any remaining cycle terminates.
    """
    root = Path(project_root)
    root_real = os.path.realpath(root)
    found: list[tuple[float, str]] = []
    seen: set[str] = set()
    skip = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        kept: list[str] = []
        for d in dirnames:
            if d in skip or d.startswith("obj_dir"):
                continue
            child = os.path.join(dirpath, d)
            if os.path.islink(child) and not may_follow_link(
                child, Path(os.path.relpath(child, root)).parts, root_real
            ):
                continue
            kept.append(d)
        dirnames[:] = kept
        if MANIFEST_FILENAME not in filenames:
            continue
        path = os.path.join(dirpath, MANIFEST_FILENAME)
        try:
            mtime = os.path.getmtime(path)
        except OSError:  # pragma: no cover - raced deletion
            continue
        found.append((mtime, path))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in found]
