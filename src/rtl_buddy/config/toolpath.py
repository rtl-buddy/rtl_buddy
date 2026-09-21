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

"Exists" means *executable file* for the binary-valued fields, matching
the availability check the callers apply afterwards, so a candidate that
exists without being runnable falls through instead of winning and then
being reported unavailable. ``cfg-verible.path`` is the one field naming
a *directory*; it passes ``directory=True``, which tests candidates as
directories under ``base_dir`` and never as ``PATH`` lookups.

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


def _binary_exists(value: str, base_dir: str | None) -> bool:
    """Is ``value`` a usable *executable*?

    Bare names are looked up on ``PATH`` (``shutil.which`` already tests
    the executable bit); everything else is anchored at ``base_dir`` when
    relative and must be a file the process may execute. The executable
    test — rather than a bare ``os.path.exists`` — is what keeps
    resolution and the callers' own availability checks in agreement:
    :meth:`SurferConfigFile.initialise
    <rtl_buddy.config.surfer.SurferConfigFile.initialise>` requires
    ``isfile`` + ``X_OK``, so a candidate that exists without being
    executable must fall through to the next one rather than win here and
    be reported unavailable afterwards.
    """
    if is_bare_name(value):
        return shutil.which(value) is not None
    probe = value
    if not os.path.isabs(probe) and base_dir:
        probe = os.path.join(base_dir, probe)
    return os.path.isfile(probe) and os.access(probe, os.X_OK)


def _directory_exists(value: str, base_dir: str | None) -> bool:
    """Is ``value`` an existing directory?

    ``cfg-verible.path`` names a *directory* of binaries, not a binary, so
    a separator-free candidate is a relative directory name and must never
    be handed to ``shutil.which`` — doing so makes an existing
    ``verible-arm/`` next to ``root_config.yaml`` invisible.
    """
    probe = value
    if not os.path.isabs(probe) and base_dir:
        probe = os.path.join(base_dir, probe)
    return os.path.isdir(probe)


def _candidate_exists(value: str, base_dir: str | None, directory: bool) -> bool:
    """Does ``value`` point at something usable? See the two helpers above."""
    if directory:
        return _directory_exists(value, base_dir)
    return _binary_exists(value, base_dir)


#: ``(block, name, field, candidates)`` tuples already reported through
#: ``tool_path.unresolved_var``. Resolution runs on every ``get_exe()`` /
#: ``get_executable()`` call — several times per test — so without this
#: a single unset variable would emit thousands of identical WARNINGs
#: across a regression. The condition is a static property of the config
#: and the environment, so saying it once is saying it.
_UNRESOLVED_WARNED: set[tuple[str, str, str, str]] = set()


def reset_unresolved_warnings() -> None:
    """Forget which ``tool_path.unresolved_var`` warnings were emitted.

    Only for tests that assert on the warning; production code has no
    reason to re-announce a condition that cannot change mid-run.
    """
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

    Args:
      value: A single path/name, or a list of candidates in preference order.
      base_dir: Directory relative candidates are anchored at for the
        existence test. The returned value is never joined against it —
        callers keep their own relative-path semantics.
      block, name, field: Identify the config field in log events
        (e.g. ``cfg-surfer`` / ``surfer-macos`` / ``path``).
      directory: The field names a directory of binaries rather than a
        binary (``cfg-verible.path``). A separator-free candidate is then
        a relative directory name, not a ``PATH`` lookup.

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
        # Every candidate referenced an unset variable. There is nothing
        # to fall through to, so return the literal and say so loudly —
        # the alternative is a subprocess failing on a path with a
        # literal "${...}" in it and no explanation. Said once per field:
        # resolution runs per command construction, not once at load.
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

    # Nothing existed. The last cleanly-expanded candidate is the
    # fallback slot — conventionally a bare name, left for PATH (or for
    # the caller's own availability check) to have the final word.
    return expanded_ok[-1]
