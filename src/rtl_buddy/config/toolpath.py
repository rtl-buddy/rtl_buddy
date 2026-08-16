"""Environment-aware resolution of tool path fields in ``root_config.yaml``.

Every block that pins *where a tool lives* — ``cfg-rtl-builder.builder``,
``cfg-verible.path``, ``cfg-surfer.path`` and the ``tool:`` field of the
``cfg-*-tools`` blocks — routes its value through :func:`resolve_tool_path`.

Two things fall out of that:

* **``~`` and ``$VAR`` expansion**, the same treatment
  :meth:`rtl_buddy.config.systemc.SystemCConfig.get_home` has always
  applied to ``cfg-systemc.home``: ``expanduser(expandvars(...))``, with
  an unresolved ``${VAR}`` (POSIX ``expandvars`` returns the literal for
  an unset variable) detected and treated as "this candidate does not
  apply" rather than as a path containing a literal dollar sign.
* **A candidate list.** ``path:`` may be a single string (today's shape,
  unchanged) or a list. The first candidate that expands cleanly *and*
  exists wins; a bare name (no path separator) counts as existing when
  it is on ``PATH``. Nothing resolving leaves the last cleanly-expanded
  candidate, so a trailing bare name is the documented "fall back to
  ``PATH``" entry.

Together with the gitignored ``.rtl-buddy/.env`` — already loaded into
the process environment before configs are read — that is the full
precedence chain a multi-platform project wants, expressed in one
committed file::

    path: ["${RB_TOOLS}/bin/surfer", "/opt/rb-tools/current/bin/surfer", "surfer"]

individual env override -> committed canonical path -> ``PATH``.

Resolution deliberately returns the *candidate*, not the absolute path
``shutil.which`` found for it: callers already have their own semantics
for a relative path (``cfg-verible`` and ``cfg-surfer`` anchor one at
``root_config.yaml``) and for a bare name (hand it to the subprocess and
let the OS resolve it). ``base_dir`` only widens the existence *test*.
"""

import logging
import os
import re
import shutil

from ..logging_utils import log_event

logger = logging.getLogger(__name__)


#: Matches ``${VAR}`` and ``$VAR``. If :func:`os.path.expandvars` leaves
#: either in its output the variable was unset — POSIX semantics return
#: the literal rather than raising. Kept identical to the ``cfg-systemc``
#: regex so both blocks agree on what "unresolved" means.
_UNRESOLVED_VAR_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")


def expand_path(value: str) -> str | None:
    """Expand ``~`` and ``$VAR`` in ``value``.

    Returns None when the expansion left an unresolved ``$VAR`` — i.e.
    the environment variable is not set, so this value does not apply.
    Callers fall through to the next candidate (or report the literal).
    """
    expanded = os.path.expanduser(os.path.expandvars(value))
    if _UNRESOLVED_VAR_RE.search(expanded):
        return None
    return expanded


def is_bare_name(value: str) -> bool:
    """True when ``value`` names a binary to be resolved off ``PATH``.

    A bare name carries no path separator and does not start with ``.``
    — the same test :class:`~rtl_buddy.config.surfer.SurferConfig` has
    always used to decide between "join against root_config.yaml" and
    "hand to ``shutil.which``".
    """
    if not value or value.startswith("."):
        return False
    if os.sep in value:
        return False
    return not (os.altsep and os.altsep in value)


def _candidate_exists(value: str, base_dir: str | None) -> bool:
    """Does ``value`` point at something that exists?

    Bare names are looked up on ``PATH``; relative paths are anchored at
    ``base_dir`` when one is supplied (``cfg-verible`` / ``cfg-surfer``
    resolve theirs against ``root_config.yaml``'s directory).
    """
    if is_bare_name(value):
        return shutil.which(value) is not None
    probe = value
    if not os.path.isabs(probe) and base_dir:
        probe = os.path.join(base_dir, probe)
    return os.path.exists(probe)


def resolve_tool_path(
    value: str | list[str],
    *,
    base_dir: str | None = None,
    block: str = "",
    name: str = "",
    field: str = "path",
) -> str:
    """Pick the effective value of a tool path field.

    Args:
      value: A single path/name, or a list of candidates in preference order.
      base_dir: Directory relative candidates are anchored at for the
        existence test. The returned value is never joined against it —
        callers keep their own relative-path semantics.
      block, name, field: Identify the config field in log events
        (e.g. ``cfg-surfer`` / ``surfer-macos`` / ``path``).

    Returns:
      The first candidate that expands cleanly and exists, else the last
      candidate that at least expanded cleanly (the documented ``PATH``
      fallback slot), else the last raw candidate so a "not found" error
      still names what was configured.
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
        if _candidate_exists(expanded, base_dir):
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
        # Every candidate referenced an unset variable. There is nothing
        # to fall through to, so return the literal and say so loudly —
        # the alternative is a subprocess failing on a path with a
        # literal "${...}" in it and no explanation.
        log_event(
            logger,
            logging.WARNING,
            "tool_path.unresolved_var",
            block=block,
            name=name,
            field=field,
            candidates=", ".join(unresolved),
        )
        return candidates[-1]

    # Nothing existed. The last cleanly-expanded candidate is the
    # fallback slot — conventionally a bare name, left for PATH (or for
    # the caller's own availability check) to have the final word.
    return expanded_ok[-1]
