"""Resolution of tool path fields in ``root_config.yaml``.

``cfg-rtl-builder.builder``, ``cfg-verible.path``, ``cfg-surfer.path`` and the ``tool:`` field of the ``cfg-*-tools`` blocks go through :func:`resolve_tool_path`. It expands ``~`` and ``$VAR``, and accepts a single string or a list of candidates. The first candidate that expands cleanly and exists wins, for example::

    path: ["${RB_TOOLS}/bin/surfer", "/opt/rb-tools/current/bin/surfer", "surfer"]

A candidate with an unset variable is skipped. A bare name (no path separator) exists when it is on ``PATH``; other candidates must be executable files, or directories when ``directory=True`` (``cfg-verible.path``). If nothing exists, the last cleanly expanded candidate is returned, so a trailing bare name is the ``PATH`` fallback. The returned value is the candidate, never an absolute path found by ``shutil.which``.
"""

import logging
import os
import re
import shutil

from ..logging_utils import log_event

logger = logging.getLogger(__name__)


#: expandvars leaves an unset variable in the output instead of raising.
_UNRESOLVED_VAR_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


def expand_path(value: str) -> str | None:
    """Expand ``~`` and ``$VAR`` in ``value``; None if a variable is unset."""
    expanded = os.path.expanduser(os.path.expandvars(value))
    if _UNRESOLVED_VAR_RE.search(expanded):
        return None
    return expanded


def is_bare_name(value: str) -> bool:
    """True when ``value`` has no path separator and does not start with ``.``, so it is looked up on ``PATH``."""
    if not value or value.startswith("."):
        return False
    if os.sep in value:
        return False
    return not (os.altsep and os.altsep in value)


def _binary_exists(value: str, base_dir: str | None) -> bool:
    """Is ``value`` an executable, on ``PATH`` if bare, else anchored at ``base_dir``?

    The executable test must match the callers' availability checks, so a non-executable candidate falls through.
    """
    if is_bare_name(value):
        return shutil.which(value) is not None
    probe = value
    if not os.path.isabs(probe) and base_dir:
        probe = os.path.join(base_dir, probe)
    return os.path.isfile(probe) and os.access(probe, os.X_OK)


def _directory_exists(value: str, base_dir: str | None) -> bool:
    """Is ``value`` an existing directory? Never consults ``PATH``."""
    probe = value
    if not os.path.isabs(probe) and base_dir:
        probe = os.path.join(base_dir, probe)
    return os.path.isdir(probe)


def _candidate_exists(value: str, base_dir: str | None, directory: bool) -> bool:
    """Does ``value`` point at something usable?"""
    if directory:
        return _directory_exists(value, base_dir)
    return _binary_exists(value, base_dir)


#: ``(block, name, field, candidates)`` tuples already warned about; resolution runs per command construction.
_UNRESOLVED_WARNED: set[tuple[str, str, str, str]] = set()


def reset_unresolved_warnings() -> None:
    """Clear the warned-once set (tests)."""
    _UNRESOLVED_WARNED.clear()


def resolve_tool_path(
    value: str | list[str],
    *,
    base_dir: str | None = None,
    block: str = "",
    name: str = "",
    field: str = "path",
    directory: bool = False,
) -> str:
    """Pick the effective value of a tool path field.

    `value` is one path or a list of candidates in preference order. `base_dir` anchors relative candidates for the existence test only; the result is never joined to it. `block`, `name` and `field` label log events. `directory` marks a directory-valued field.

    Returns the first candidate that expands cleanly and exists, else the last that expanded cleanly, else the last raw candidate.
    """
    candidates = [value] if isinstance(value, str) else list(value)
    if not candidates:
        return ""

    expanded_ok: list[str] = []
    unresolved: list[str] = []
    for raw in candidates:
        expanded = expand_path(raw)
        if expanded is None:
            unresolved.append(raw)
            continue
        expanded_ok.append(expanded)
        if _candidate_exists(expanded, base_dir, directory):
            if len(candidates) > 1:
                log_event(
                    logger,
                    logging.DEBUG,
                    "tool_path.resolved",
                    block=block,
                    name=name,
                    field=field,
                    value=expanded,
                    candidates=len(candidates),
                )
            return expanded

    if not expanded_ok:
        # Every candidate has an unset variable: warn once per field and return the literal.
        listed = ", ".join(unresolved)
        key = (block, name, field, listed)
        if key not in _UNRESOLVED_WARNED:
            _UNRESOLVED_WARNED.add(key)
            log_event(
                logger,
                logging.WARNING,
                "tool_path.unresolved_var",
                block=block,
                name=name,
                field=field,
                candidates=listed,
            )
        return candidates[-1]

    return expanded_ok[-1]
