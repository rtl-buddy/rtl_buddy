"""Inputs of a `rb pnr --run-tag` call that live in trees the call does not own.

A tagged call holds the lock of its own `artefacts/.runs/<tag>/` tree only. Block
abstracts and the upstream synthesis netlist live in other trees, which another call
may rewrite at any time. `TagInputs` copies each one, under the lock of the tree it is
in, into the call's tree, and the call reads only the copies. Copies are keyed by the
digest of what they copy and never overwritten, so a saved result keeps the inputs it
was made from until the tag's tree is removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..artifact_lock import tree_lock
from ..logging_utils import log_event
from . import pnr_abstract
from .artifact_paths import run_artifact_root

logger = logging.getLogger(__name__)

#: Under a tag's tree: one copy of each block abstract its runs consumed, `<run>-<suite>-<digest>/`.
BLOCK_COPIES_DIRNAME = ".block-abstracts"
#: Under a tag's tree: one copy of each synthesis netlist and filelist its runs read, `<synth>-<suite>-<digest>/`.
SYNTH_COPIES_DIRNAME = ".synth"
#: In a tagged P&R run's directory: the block abstracts the run consumed, which `rb pnr-export --run-tag` reads again.
CONSUMED_BLOCKS_NAME = "blocks.consumed.json"
#: In a tagged P&R run's directory: written when a P&R attempt starts, so a directory `rb pnr-export` alone created is not taken for a run.
ATTEMPT_MARKER_NAME = ".rb-pnr-attempted"

_SYNTH_NETLIST = "synth_netlist.v"
_SYNTH_FILELIST = "synth.f"
_FILELIST_PATH_OPTIONS = ("-v ", "-y ", "-f ", "-F ")


def _short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_filelist(text: str, base: str) -> str:
    """Return a filelist with every path made absolute against `base`, so it reads the same from another directory."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith("//")
            or line.startswith(("+define+", "+libext+"))
        ):
            lines.append(raw)
        elif line.startswith("+incdir+"):
            dirs = [d for d in line[len("+incdir+") :].split("+") if d]
            lines.append(
                "+incdir+"
                + "+".join(os.path.normpath(os.path.join(base, d)) for d in dirs)
            )
        elif line.startswith(_FILELIST_PATH_OPTIONS):
            option, path = line[:3], line[3:].strip()
            lines.append(option + os.path.normpath(os.path.join(base, path)))
        else:
            lines.append(os.path.normpath(os.path.join(base, line)))
    return "\n".join(lines) + "\n"


def mark_attempt(artefact_dir: str) -> None:
    """Record that a P&R attempt of this tagged run started; see `TagInputs`."""
    Path(artefact_dir, ATTEMPT_MARKER_NAME).touch()


def read_consumed_blocks(artefact_dir: str) -> dict | None:
    """Return the block copies a saved tagged run consumed, by (pnr.yaml real path, run), or None without a record."""
    try:
        data = json.loads(Path(artefact_dir, CONSUMED_BLOCKS_NAME).read_text())
    except (OSError, ValueError):
        return None
    return {
        (os.path.realpath(b["pnr_path"]), b["pnr"]): b
        for b in data.get("blocks") or []
        if isinstance(b, dict)
    }


def write_consumed_blocks(artefact_dir: str, run_tag: str, blocks) -> None:
    """Record `blocks` (`ResolvedBlock`s) in the run directory for a later `rb pnr-export --run-tag`."""
    Path(artefact_dir, CONSUMED_BLOCKS_NAME).write_text(
        json.dumps(
            {
                "run_tag": run_tag,
                "blocks": [
                    {
                        **b.manifest_entry(),
                        "pnr_path": b.ref.pnr_suite_path,
                        "abstract_dir": b.abstract_dir,
                    }
                    for b in blocks
                ],
            },
            indent=2,
        )
        + "\n"
    )


@dataclass
class TagInputs:
    """Where one `--run-tag` call finds its block abstracts and synthesis netlists.

    A block run the tag has attempted (its run directory holds `ATTEMPT_MARKER_NAME`, checked
    under the tag's tree lock) is copied from the tag's tree; one it has never attempted is
    copied from the untagged tree, with a `pnr.block_untagged_abstract` warning. Either copy
    is taken while holding that tree's lock, and no other tree's lock is waited for meanwhile;
    the copy is checked against the abstract's manifest. `pinned` maps (pnr.yaml real path,
    run) to the record of a saved run, whose copy is read again instead of taking a new one.
    Each block is resolved once per call, and an error is kept like a copy, so synthesis and
    P&R in one call read the same copy.
    """

    run_tag: str
    root: str
    pinned: dict | None = None
    _copies: dict = field(default_factory=dict, repr=False)
    _key_locks: dict = field(default_factory=dict, repr=False)
    _mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _once(self, key, compute, keep):
        """Return `compute()` for `key`, computed once while `keep` holds for its result.

        Only computing one key waits for that key; a thread reading another key, or a kept one, never waits for it.
        """
        with self._mutex:
            if key in self._copies:
                return self._copies[key]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._mutex:
                if key in self._copies:
                    return self._copies[key]
            value = compute()
            if keep(value):
                with self._mutex:
                    self._copies[key] = value
            return value

    def abstract_dir(self, ref, where: str) -> tuple[str, str | None]:
        """Return the copy of `ref`'s abstract to read and the tag it was published under."""
        key = (os.path.realpath(ref.pnr_suite_path), ref.pnr_run)
        if self.pinned is not None:
            return self._pinned(ref, key, where)

        def _compute():
            try:
                return self._copy_abstract(ref, where)
            except pnr_abstract.BlockResolutionError as e:
                return e

        result = self._once(("abstract", *key), _compute, lambda _v: True)
        if isinstance(result, pnr_abstract.BlockResolutionError):
            raise result
        return result

    def pinned_abstract(self, ref, where: str) -> tuple[str, str | None] | None:
        """For a saved run's export, the copy it consumed and the tag it was published under; None when this call takes new copies."""
        if self.pinned is None:
            return None
        return self._pinned(
            ref, (os.path.realpath(ref.pnr_suite_path), ref.pnr_run), where
        )

    def prefetch(self, refs) -> None:
        """Resolve `refs` now, so a later resolution under another tree's lock waits for no block tree."""
        for ref in refs:
            try:
                pnr_abstract.resolve_block(ref, source=self)
            except pnr_abstract.BlockResolutionError:
                pass

    def _pinned(self, ref, key, where: str) -> tuple[str, str | None]:
        record = self.pinned.get(key)
        if record is None:
            raise pnr_abstract.BlockResolutionError(
                f"{where}: the saved run under --run-tag {self.run_tag} records no "
                f"abstract for it — re-run the P&R run under --run-tag {self.run_tag}"
            )
        directory = record.get("abstract_dir") or ""
        manifest = Path(directory, pnr_abstract.ABSTRACT_MANIFEST_NAME)
        if not manifest.is_file() or _sha256(manifest) != record.get("manifest_sha256"):
            raise pnr_abstract.BlockResolutionError(
                f"{where}: the abstract copy the saved run consumed is gone from "
                f"{directory} — re-run the P&R run under --run-tag {self.run_tag}"
            )
        return directory, record.get("run_tag")

    def _copy_abstract(self, ref, where: str) -> tuple[str, str | None]:
        suite_dir = os.path.dirname(ref.pnr_suite_path)
        tagged_root = run_artifact_root(suite_dir, self.run_tag)
        if tagged_root.is_dir():
            # A same-tag block run holding its tree has marked it or ended by the time the lock is ours.
            with tree_lock(tagged_root, command="pnr"):
                if (tagged_root / ref.pnr_run / ATTEMPT_MARKER_NAME).is_file():
                    return self._copy_from(ref, where, tagged_root, self.run_tag)
        return self._copy_from(ref, where, run_artifact_root(suite_dir), None)

    def _copy_from(
        self, ref, where: str, source_root: Path, source_tag: str | None
    ) -> tuple[str, str | None]:
        source = pnr_abstract.abstract_dir(str(source_root / ref.pnr_run))
        with tree_lock(source_root, command="pnr"):
            manifest_file = Path(source, pnr_abstract.ABSTRACT_MANIFEST_NAME)
            if pnr_abstract.read_manifest(source) is None:
                if source_tag is not None:
                    raise pnr_abstract.BlockResolutionError(
                        f"{where}: no abstract at {source} — --run-tag {source_tag} "
                        "ran the block without publishing one; run "
                        f"{pnr_abstract.rerun_command(ref, source_tag)} first"
                    )
                raise pnr_abstract.BlockResolutionError(
                    f"{where}: no abstract at {source}, and --run-tag "
                    f"{self.run_tag} has not run the block — run "
                    f"{pnr_abstract.rerun_command(ref)} or "
                    f"{pnr_abstract.rerun_command(ref, self.run_tag)} first"
                )
            manifest_bytes = manifest_file.read_bytes()
            digest = hashlib.sha256(manifest_bytes).hexdigest()
            dest = Path(
                self.root,
                BLOCK_COPIES_DIRNAME,
                f"{ref.pnr_run}-{_short(os.path.realpath(ref.pnr_suite_path))}-{digest[:16]}",
            )
            if not dest.is_dir():
                partial = dest.with_name(f"{dest.name}.partial")
                shutil.rmtree(partial, ignore_errors=True)
                partial.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, partial)
                self._check_copy(partial, manifest_bytes, source, where)
                Path(partial, pnr_abstract.COPY_SOURCE_NAME).write_text(
                    json.dumps({"abstract_dir": source}) + "\n"
                )
                os.replace(partial, dest)
        if source_tag is None:
            log_event(
                logger,
                logging.WARNING,
                "pnr.block_untagged_abstract",
                block=ref.name,
                pnr_run=ref.pnr_run,
                run_tag=self.run_tag,
                source=source,
                copy=str(dest),
            )
        return str(dest), source_tag

    @staticmethod
    def _check_copy(copy: Path, manifest_bytes: bytes, source: str, where: str) -> None:
        if (
            Path(copy, pnr_abstract.ABSTRACT_MANIFEST_NAME).read_bytes()
            != manifest_bytes
        ):
            shutil.rmtree(copy, ignore_errors=True)
            raise pnr_abstract.BlockResolutionError(
                f"{where}: the copy of {source} does not match it"
            )
        manifest = json.loads(manifest_bytes)
        design = manifest.get("block")
        for view, record in (manifest.get("outputs") or {}).items():
            if not record:
                continue
            path = pnr_abstract.view_path(str(copy), design, view)
            if not os.path.isfile(path) or _sha256(path) != record.get("sha256"):
                shutil.rmtree(copy, ignore_errors=True)
                raise pnr_abstract.BlockResolutionError(
                    f"{where}: the copy of {source} does not match its manifest "
                    f"({view})"
                )

    def synth_dir(self, synth_suite_path: str, synth_name: str) -> str | None:
        """Return the copy of a synthesis's netlist and filelist to read, or None when it has no netlist.

        Taken under the synthesis tree's lock. The copied filelist names absolute paths. A missing netlist is looked for again on the next call, so a synthesis this call runs later is still copied.
        """
        return self._once(
            ("synth", os.path.realpath(synth_suite_path), synth_name),
            lambda: self._copy_synth(synth_suite_path, synth_name),
            lambda copy: copy is not None,
        )

    def _copy_synth(self, synth_suite_path: str, synth_name: str) -> str | None:
        source_root = run_artifact_root(os.path.dirname(synth_suite_path))
        source = source_root / synth_name
        with tree_lock(source_root, command="pnr"):
            netlist = source / _SYNTH_NETLIST
            if not netlist.is_file():
                return None
            data = netlist.read_bytes()
            try:
                filelist = (source / _SYNTH_FILELIST).read_text()
            except OSError:
                filelist = None
        digest = hashlib.sha256(data + (filelist or "").encode()).hexdigest()
        dest = Path(
            self.root,
            SYNTH_COPIES_DIRNAME,
            f"{synth_name}-{_short(os.path.realpath(synth_suite_path))}-{digest[:16]}",
        )
        if not dest.is_dir():
            partial = dest.with_name(f"{dest.name}.partial")
            shutil.rmtree(partial, ignore_errors=True)
            partial.mkdir(parents=True)
            (partial / _SYNTH_NETLIST).write_bytes(data)
            if filelist is not None:
                (partial / _SYNTH_FILELIST).write_text(
                    _absolute_filelist(filelist, str(source))
                )
            os.replace(partial, dest)
        return str(dest)
