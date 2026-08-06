# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The one resolver for simulator-recorded source paths (#399).

A coverage database records source files the way the simulator saw them:
often relative to the run directory (``../../tb_top.sv``), sometimes a
bare basename, occasionally absolute. Every consumer — the LCOV ``SF:``
rewriter, the ``--annotate`` scaffolding, the Coverview packer — has to
turn that back into a real file in the project, and each grew its own
copy of the search.

The three copies disagreed, so this module replaces them. Its contract
is the one the call sites already used: **hints are ``[run dir, suite
root]``**, most specific first, and the project root is always the last
resort. Resolution order:

1. direct candidates: ``<base dir>/<path>``, ``<hint>/<path>`` for each
   hint, ``<project root>/<path>``;
2. project-root-anchored suffixes of the path, trimming leading
   segments (``../../design/blk.sv`` -> ``design/blk.sv``);
3. a basename search under the hints and then the project root, taking
   the match only when it is *unambiguous* — a unique hit under a hint
   beats a unique path-suffix match, which beats a unique basename.

Generated trees (``artefacts/``, ``logs/``, annotate output, Verilator
``obj_dir*``) are skipped during the search, judged relative to the
search root: a copy of ``tb_top.sv`` inside an annotate scratch
directory must never win over the real source. When the search root
*is* inside such a directory (a per-run artefact dir passed as a hint)
nothing is skipped, because there the generated tree is the thing being
searched.

Nothing here writes files; callers rewrite their own ``.info``/``.desc``
records.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Directory names whose contents are generated, never sources. Judged
#: relative to the search root so a hint that points *into* one of them
#: still searches normally.
GENERATED_DIRS = frozenset(
    {
        "artefacts",
        "logs",
        "cov_annot",
        "cov_dir",
        "coverage_annotated",
        "coverage_merge.html",
    }
)

#: Verilator build directories are ``obj_dir``, ``obj_dir_<test>`` and
#: ``.shared-builds/obj_dir_<key>``; matched on prefix.
GENERATED_DIR_PREFIXES = ("obj_dir",)


@dataclass(frozen=True)
class Resolution:
    """One resolved source path.

    ``path`` is always usable (the best-effort candidate when nothing
    exists on disk); ``found`` says whether it is real, and
    ``project_relative`` is the POSIX path under the project root, or
    None when the file belongs to another tree and a consumer should
    leave the record alone.
    """

    path: Path
    found: bool
    project_relative: str | None


def _resolved(path) -> Path:
    return Path(path).resolve()


class SourcePathResolver:
    """Resolve simulator-recorded source paths against one project."""

    def __init__(
        self,
        project_root,
        *,
        base_dir=None,
        source_roots=None,
        skip_generated: bool = True,
    ):
        """
        :param project_root: the project root; the last-resort search root.
        :param base_dir: directory the recorded paths are relative to
            (the run directory for a raw database, the ``.info`` file's
            own directory for an LCOV export).
        :param source_roots: the ``[run dir, suite root]`` hints, most
            specific first.
        :param skip_generated: skip :data:`GENERATED_DIRS` during the
            basename search.
        """
        self.project_root = _resolved(project_root)
        self.base_dir = None if base_dir is None else _resolved(base_dir)
        self.source_roots = tuple(
            _resolved(root) for root in (source_roots or ()) if root is not None
        )
        self.skip_generated = skip_generated

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def resolve(self, sf_path: str) -> Resolution:
        """Resolve one recorded path into a :class:`Resolution`."""
        if os.path.isabs(sf_path):
            path = Path(sf_path)
            resolved = path.resolve() if path.exists() else path
            return self._resolution(resolved, resolved.exists(), stripped=None)

        normalized = str(sf_path).replace("\\", "/")
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        stripped = [part for part in parts if part != ".."]

        candidates = self._direct_candidates(normalized, parts)
        for candidate in candidates:
            if candidate.exists():
                return self._resolution(candidate, True, stripped=stripped)

        match = self._search_by_basename(parts)
        if match is not None:
            return self._resolution(match, True, stripped=stripped)

        fallback = candidates[0] if candidates else (self.project_root / normalized)
        return self._resolution(fallback.resolve(), False, stripped=stripped)

    def resolve_path(self, sf_path: str) -> Path:
        """Best-effort absolute path — the ``_resolve_source_path`` contract."""
        return self.resolve(sf_path).path

    def rewrite_info(self, info_path, *, relative: bool) -> None:
        """Rewrite an LCOV file's ``SF:`` records in place.

        ``relative=True`` writes project-relative paths and leaves a
        record alone when the file resolves outside the project;
        ``relative=False`` writes absolute paths.
        """
        self._rewrite_records(info_path, "SF:", relative=relative)

    def rewrite_desc(self, desc_path, *, relative: bool = True) -> None:
        """Rewrite a Coverview ``.desc`` file's ``SN:`` records in place."""
        self._rewrite_records(desc_path, "SN:", relative=relative)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _rewrite_records(self, path, prefix: str, *, relative: bool) -> None:
        path = Path(path)
        out_lines = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.startswith(prefix):
                    out_lines.append(line)
                    continue
                resolution = self.resolve(line[len(prefix) :].strip())
                if relative:
                    if resolution.project_relative is None:
                        out_lines.append(line)
                        continue
                    out_lines.append(f"{prefix}{resolution.project_relative}\n")
                else:
                    out_lines.append(f"{prefix}{resolution.path}\n")
        with path.open("w", encoding="utf-8") as fh:
            fh.writelines(out_lines)

    def _search_roots(self) -> list[Path]:
        roots = list(self.source_roots)
        if self.project_root not in roots:
            roots.append(self.project_root)
        return roots

    def _direct_candidates(self, normalized: str, parts: list[str]) -> list[Path]:
        candidates: list[Path] = []
        seen: set[str] = set()

        def add(candidate: Path) -> None:
            candidate = candidate.resolve()
            key = str(candidate)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

        anchors = [] if self.base_dir is None else [self.base_dir]
        anchors.extend(self.source_roots)
        anchors.append(self.project_root)
        for anchor in anchors:
            add(anchor / normalized)

        # Trim leading segments and re-anchor the remaining suffix on the
        # project root: `../../design/blk.sv` -> `design/blk.sv`.
        for idx in range(1, len(parts)):
            suffix = parts[idx:]
            if suffix:
                add(self.project_root / Path(*suffix))

        return candidates

    def _is_generated(self, root: Path, match: Path) -> bool:
        if not self.skip_generated:
            return False
        try:
            parts = match.relative_to(root).parts
        except ValueError:
            parts = match.parts
        for part in parts[:-1]:
            if part in GENERATED_DIRS:
                return True
            if part.startswith(GENERATED_DIR_PREFIXES):
                return True
        return False

    def _search_by_basename(self, parts: list[str]) -> Path | None:
        """Unambiguous basename search, most-specific evidence first."""
        if not parts:
            return None
        basename = parts[-1]
        suffix = "/" + "/".join(parts)

        hint_matches: list[Path] = []
        suffix_matches: list[Path] = []
        all_matches: list[Path] = []
        for root in self._search_roots():
            if not root.is_dir():
                continue
            for match in root.rglob(basename):
                if not match.is_file():
                    continue
                if self._is_generated(root, match):
                    continue
                match = match.resolve()
                if root in self.source_roots:
                    hint_matches.append(match)
                if str(match).replace("\\", "/").endswith(suffix):
                    suffix_matches.append(match)
                all_matches.append(match)

        for group in (hint_matches, suffix_matches, all_matches):
            unique = _dedupe(group)
            if len(unique) == 1:
                return unique[0]
        return None

    def _resolution(
        self, path: Path, found: bool, *, stripped: list[str] | None
    ) -> Resolution:
        try:
            relative = path.relative_to(self.project_root).as_posix()
        except ValueError:
            # A file that does not exist and does not sit under the
            # project root is almost always a record whose leading `../`
            # segments the simulator wrote from a run directory: keep the
            # repo-anchored reading rather than dropping the record.
            if not found and stripped:
                relative = Path(*stripped).as_posix()
            else:
                relative = None
        return Resolution(path=path, found=found, project_relative=relative)


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique
