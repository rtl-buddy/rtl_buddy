# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
"""Parses, resolves and writes Verilog filelists."""

import contextlib
import logging

logger = logging.getLogger(__name__)
from ..errors import FilelistError
from ..logging_utils import log_event
from .artifact_paths import atomic_tmp_name
import fnmatch
import os
import os.path
import re


# `+define+NAME[=VALUE]` is a preprocessor define, not a path, so it skips path resolution.
_DEFINE_PREFIX = "+define+"

# Options whose values are paths pinned to absolute form under ``absolute_sources``.
_ABSOLUTE_UNDER_PIN = (None, "-v ", "+incdir+", "-y ")


def _quote_filelist_path(path: str) -> str:
    """Quote a generated path when a filelist parser would split it."""
    if not any(char.isspace() for char in path) and '"' not in path:
        return path
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def incdirs_from_filelist(fl_path: str) -> list[str]:
    """Return the deduplicated ``+incdir+`` directories of a generated filelist, in order.

    Each directory resolves against the filelist that declares it.
    """
    fl_dir = os.path.dirname(os.path.abspath(fl_path))
    incdirs: list[str] = []
    try:
        with open(fl_path) as f:
            lines = f.readlines()
    except OSError:
        return incdirs
    for line in lines:
        line = line.strip()
        if not line.startswith("+incdir+"):
            continue
        for entry in line[len("+incdir+") :].split("+"):
            if not entry:
                continue
            inc = os.path.normpath(os.path.join(fl_dir, entry))
            if inc not in incdirs:
                incdirs.append(inc)
    return incdirs


def apply_exclude_globs(
    files: list[str], patterns: list[str], project_root: str
) -> tuple[list[str], int]:
    """Drop absolute paths matching any exclude glob; return ``(kept, excluded_count)`` in original order.

    Globs use fnmatch against the project-root-relative path with ``/``
    separators, so ``*`` also crosses directories.
    """
    if not patterns:
        return list(files), 0
    kept: list[str] = []
    excluded = 0
    for path in files:
        rel = os.path.relpath(path, project_root).replace(os.sep, "/")
        if any(fnmatch.fnmatch(rel, pat) for pat in patterns):
            excluded += 1
            continue
        kept.append(path)
    return kept, excluded


class VlogFilelist:
    """Parses .f filelists and writes the resolved output filelist."""

    def __init__(self, name, model_cfg, output_path):
        self.name = name
        self.output_path = output_path
        # None when the caller supplies models per call (e.g. write_verible_filelist).
        self.model_cfg = model_cfg

    def _fail(self, event, message, **fields):
        log_event(logger, logging.ERROR, event, **fields)
        raise FilelistError(message)

    def _extract(self, lines_in, unroll, fpath):
        """Return the ``(path, option)`` entries of a .f file in order, following ``-F`` includes when ``unroll`` is true."""
        prefix_parent = os.path.dirname(fpath)
        entries = []
        libexts = set()
        for line in lines_in:
            line = line.strip()
            if (
                not line
                or line.startswith("//")
                or line.startswith("/*")
                or line.startswith("*")
            ):
                continue

            pattern = re.compile(
                r"""^
        (                           # group 1: option, or empty
          (?:-v|                    # '-v'
           -y|         		          # '-y'
           -[Ff])\s+                # '-F' or '-f' (case sensitive) + space(s)
          |                         # OR
          (\+(?:
            incdir|									# '+incdir+'
            libext|									# '+libext+'
            define									# '+define+'
					)\+))
        ?                           # group 1 optional, so path only works
        (.*)                        # group 3: the path part (required)
        $""",
                re.VERBOSE,
            )

            m = pattern.fullmatch(line)

            if not m or not m.group(3):
                self._fail(
                    "filelist.malformed_line",
                    f'{fpath}: malformed filelist line "{line}"',
                    file=fpath,
                    line=line,
                )

            # Determine which style matched, return (option, path)
            if m.group(2):  # +incdir+, +libext+, +define+
                line_option = m.group(2)
                line_path = m.group(3)
            elif m.group(1):  # -v, -F, -f, -y
                line_option = (
                    m.group(1).strip() + " "
                )  # Add a space since +incdir+ does not have a space behind
                line_path = m.group(3)
            else:
                line_option = None
                line_path = m.group(3)

            line_path = os.path.expandvars(line_path)
            if line_option == "-f ":
                log_event(
                    logger,
                    logging.ERROR,
                    "filelist.inline_f_disallowed",
                    file=fpath,
                    line=line,
                )
                raise FilelistError(
                    f'{fpath}: -f not allowed. Found in file {fpath} line: "{line}"'
                )

            elif line_option == "-F " and unroll:  # Recurse if unroll
                path_next = os.path.join(prefix_parent, line_path)  # path of next file
                try:
                    with open(path_next, "r") as f:
                        lines_next = f.readlines()
                        extracted_next = self._extract(lines_next, unroll, path_next)
                        entries.extend(extracted_next)
                except FileNotFoundError:
                    self._fail(
                        "filelist.include_missing",
                        f'{fpath}: included file "{path_next}" not found',
                        file=fpath,
                        include=path_next,
                    )

            elif line_option == "+libext+":  # +libext+ isn't actually a path
                libexts.update(line_path.split("+"))

            elif line_option == _DEFINE_PREFIX:
                # `+define+A+B=C` splits on `+`, so a value cannot contain `+`.
                # `$VAR` was already expanded above, so a literal `$FOO` cannot be defined.
                for one in line_path.split("+"):
                    if one:
                        entries.append((one, _DEFINE_PREFIX))

            else:  # Base case. Handles -v, source lines, and -F if not unroll
                out_path = os.path.join(prefix_parent, line_path)
                entries.append((out_path, line_option))

        # Consolidate all +libext+ into a single entry
        if len(libexts) != 0:
            entries.append(("+".join(libexts), "+libext+"))

        return entries

    def _process(
        self,
        entries,
        output_dir,
        flatten=False,
        strip=False,
        deduplicate=False,
        absolute_sources=False,
    ):
        """Apply flatten, strip and deduplicate to the collected entries and return the output lines.

        Entries arrive already resolved against the filelist that declared
        them. Paths are rewritten relative to ``output_dir``, so the consumer
        must resolve them against the generated filelist's location.

        ``absolute_sources`` instead pins bare, ``-v``, ``+incdir+`` and ``-y``
        paths to absolute form, quoted when they contain whitespace. Order is
        kept, and ``flatten`` takes precedence. A ``+incdir+`` path containing
        ``+`` cannot be pinned and stays relative with a warning. Quoting is
        verified against Verilator only, so whitespace in the checkout path is
        unsupported for Icarus and VCS.
        """
        output_dir = os.path.abspath(output_dir)
        project_root = self._project_root(output_dir)
        escaped: list[str] = []
        unpinnable: list[str] = []
        out_lines = []
        for line_path, line_option in entries:
            if line_option == _DEFINE_PREFIX:
                # Defines have no bare spelling, so strip drops them.
                if strip:
                    continue
                line = f"{_DEFINE_PREFIX}{line_path}\n"
                if not (deduplicate and line in out_lines):
                    out_lines.append(line)
                continue
            resolved_line_path = os.path.normpath(os.path.join(output_dir, line_path))
            relative_line_path = os.path.relpath(resolved_line_path, start=output_dir)
            if line_option == "+incdir+" or line_option == "-y ":
                if not os.path.isdir(resolved_line_path):
                    self._fail(
                        "filelist.directory_missing",
                        f"{relative_line_path} is not a directory",
                        path=relative_line_path,
                    )
            elif line_option != "+libext+":
                if not os.path.isfile(resolved_line_path):
                    self._fail(
                        "filelist.source_missing",
                        f"{relative_line_path} file does not exist",
                        path=relative_line_path,
                    )

            # Warn, not fail, on existing paths outside the project root: a nested
            # worktree's parent checkout can satisfy the exists check with a same-named file.
            if (
                project_root is not None
                and line_option != "+libext+"
                and self._escapes(resolved_line_path, project_root)
            ):
                escaped.append(resolved_line_path)

            if flatten:
                line_path = os.path.basename(relative_line_path)
            elif absolute_sources and line_option in _ABSOLUTE_UNDER_PIN:
                # Relative spellings are re-resolved against the builder's cwd
                # and can pick a same-named file from outside the tree.
                if line_option == "+incdir+" and "+" in resolved_line_path:
                    # `+incdir+a+b` means two directories and quoting does not help;
                    # `-y` is unaffected.
                    if resolved_line_path not in unpinnable:
                        unpinnable.append(resolved_line_path)
                    line_path = relative_line_path
                else:
                    line_path = _quote_filelist_path(resolved_line_path)
            else:
                line_path = relative_line_path

            if strip:
                line_option = ""

            line = f"{line_option}{line_path}\n" if line_option else f"{line_path}\n"
            if deduplicate:
                if line in out_lines:
                    continue

            out_lines.append(line)

        if escaped:
            log_event(
                logger,
                logging.WARNING,
                "filelist.path_escapes_root",
                count=len(escaped),
                root=project_root,
                paths=", ".join(escaped),
            )
        if unpinnable:
            log_event(
                logger,
                logging.WARNING,
                "filelist.incdir_unrepresentable",
                count=len(unpinnable),
                paths=", ".join(unpinnable),
            )

        return out_lines

    @staticmethod
    def _project_root(output_dir: str) -> str | None:
        """Return the project root containing ``output_dir``, or None when there is none (the escape check is then skipped)."""
        from ..config.root import discover_project_root
        from ..errors import FatalRtlBuddyError

        try:
            return str(discover_project_root(start_dir=output_dir))
        except FatalRtlBuddyError:
            return None

    @staticmethod
    def _escapes(resolved_path: str, project_root: str) -> bool:
        """Return whether ``resolved_path`` is outside ``project_root``."""
        rel = os.path.relpath(resolved_path, project_root)
        return rel == os.pardir or rel.startswith(os.pardir + os.sep)

    def write_output(
        self,
        output_filepath=None,
        unroll=False,
        flatten=False,
        strip=False,
        deduplicate=False,
        test_filelist=None,
        suite_dir=None,
        absolute_sources=False,
    ):
        """Write the processed filelist for ``model_cfg`` plus ``test_filelist`` entries.

        ``test_filelist`` entries resolve against ``suite_dir`` (default cwd).
        See :meth:`_process` for ``absolute_sources``.
        """
        if output_filepath is None:
            output_filepath = self.output_path
        log_event(logger, logging.DEBUG, "filelist.write_start", output=output_filepath)

        model_filelist = self.model_cfg.get_filelist()
        entries = self._extract(
            model_filelist, unroll, os.path.abspath(self.model_cfg.get_model_path())
        )

        if test_filelist:
            suite_anchor = (
                os.path.abspath(suite_dir) if suite_dir else os.path.abspath(".")
            )
            entries.extend(
                self._extract(
                    test_filelist,
                    unroll,
                    os.path.join(suite_anchor, "tests.yaml"),
                )
            )

        lines = self._process(
            entries,
            output_dir=os.path.dirname(output_filepath) or ".",
            flatten=flatten,
            strip=strip,
            deduplicate=deduplicate,
            absolute_sources=absolute_sources,
        )

        # Atomic replace: concurrent array elements rewrite the same run.f, and
        # share-build hashes it, so readers must never see a partial file.
        tmp_path = atomic_tmp_name(output_filepath)
        try:
            with open(tmp_path, "w") as f:
                f.write("// rtl-buddy generated model filelist\n")
                f.writelines(lines)
            os.replace(tmp_path, output_filepath)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        log_event(logger, logging.INFO, "filelist.write_done", output=output_filepath)
        return

    def write_elab_output(self, elab_cfg, output_filepath) -> int:
        """Write an absolute, unrolled filelist for a model elaboration.

        Returns the number of explicit source and ``-v`` entries; slang may
        parse more through library directories.
        """
        model = elab_cfg.model
        entries = []
        profile = elab_cfg.profile
        anchor = os.path.abspath(model.get_model_path())
        if profile is not None:
            entries.extend(
                (elab_cfg.resolve_profile_path(path), "+incdir+")
                for path in profile.include_dirs
            )
            entries.extend(self._extract(profile.prepend_sources, True, anchor))
        entries.extend(self._extract(model.get_filelist(), True, anchor))
        if profile is not None:
            entries.extend(self._extract(profile.append_sources, True, anchor))
            overridden_defines = set(profile.defines)
            entries = [
                (path, option)
                for path, option in entries
                if not (
                    option == _DEFINE_PREFIX
                    and path.partition("=")[0] in overridden_defines
                )
            ]
            entries.extend(
                (
                    name
                    if value is None
                    else f"{name}={'1' if value is True else '0' if value is False else value}",
                    _DEFINE_PREFIX,
                )
                for name, value in profile.defines.items()
            )

        lines = self._process(
            entries,
            output_dir=os.path.dirname(output_filepath) or ".",
            absolute_sources=True,
        )
        os.makedirs(os.path.dirname(output_filepath) or ".", exist_ok=True)
        tmp_path = atomic_tmp_name(output_filepath)
        try:
            with open(tmp_path, "w") as file:
                file.write("// rtl-buddy generated elaboration filelist\n")
                file.writelines(lines)
            os.replace(tmp_path, output_filepath)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        log_event(logger, logging.INFO, "elab.filelist_written", output=output_filepath)
        return sum(option in (None, "-v ") for _, option in entries)

    def extract_source_files(self, model_cfg):
        """Return the model's own source files: absolute, normalized, deduplicated, ``-F`` unrolled.

        Library entries (``-v``, ``-y``) and directives are dropped. Used by
        ``rb verible lint/format --model``.
        """
        entries = self._extract(
            model_cfg.get_filelist(),
            unroll=True,
            fpath=os.path.abspath(model_cfg.get_model_path()),
        )
        out: list[str] = []
        seen: set[str] = set()
        for path, option in entries:
            if option is not None:
                continue
            norm = os.path.normpath(path)
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out

    def write_verible_filelist(self, model_cfgs, output_filepath=None):
        """Write a verible.filelist from one or more ModelConfigs.

        Keeps only bare sources, ``+incdir+`` and ``+define+``, the entries
        verible-verilog-ls reads, and unrolls ``-F`` chains.
        """
        if output_filepath is None:
            output_filepath = self.output_path
        log_event(
            logger,
            logging.DEBUG,
            "verible_filelist.write_start",
            output=output_filepath,
            models=[m.name for m in model_cfgs],
        )

        if not model_cfgs:
            self._fail(
                "verible_filelist.no_models",
                "no models supplied for verible filelist generation",
            )

        entries = []
        for cfg in model_cfgs:
            entries.extend(
                self._extract(
                    cfg.get_filelist(),
                    unroll=True,
                    fpath=os.path.abspath(cfg.get_model_path()),
                )
            )

        filtered = [
            (path, opt)
            for path, opt in entries
            if opt is None or opt in ("+incdir+", _DEFINE_PREFIX)
        ]
        lines = self._process(
            filtered,
            output_dir=os.path.dirname(output_filepath) or ".",
            flatten=False,
            strip=False,
            deduplicate=True,
        )

        with open(output_filepath, "w") as f:
            f.write("// rtl-buddy generated verible filelist\n")
            f.writelines(lines)
        log_event(
            logger,
            logging.INFO,
            "verible_filelist.write_done",
            output=output_filepath,
            entries=len(lines),
        )
        return
