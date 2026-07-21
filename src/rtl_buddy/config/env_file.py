"""Project-local environment defaults from ``.rtl-buddy/.env``.

Machine-local values that are project-scoped but must not be committed
(e.g. ``RTL_BUDDY_SLANG_PLUGIN``, ``SYSTEMC_HOME``) get a home that rb
picks up automatically, instead of dirtying tracked configs or relying
on every shell having sourced a toolchain env script.

Precedence is strictly a fallback: a variable already present in the
process environment is never overridden, so explicit YAML config beats
the process environment beats this file. Loading is idempotent — once a
key is applied it is in the process environment, so a re-entry (e.g. a
regression iterating suites) cannot flip it.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

# Relative to the project root (the directory containing
# root_config.yaml). Lives inside .rtl-buddy/ so it is self-namespaced —
# a bare .env would collide with the docker-compose/node/direnv
# conventions that auto-load one.
ENV_FILE_RELPATH = Path(".rtl-buddy") / ".env"


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from an env file.

    Blank lines and ``#`` comments are skipped; a leading ``export `` is
    tolerated (so shell-style lines can be pasted verbatim); surrounding
    matching single or double quotes are stripped from the value. Values
    are otherwise literal — no ``$VAR`` interpolation, no escapes. A
    line without ``=`` or with an empty key fails loud: this is a config
    file, and a typo silently dropping a variable would surface much
    later as a missing-tool error.
    """
    env: dict[str, str] = {}
    for lineno, raw in enumerate(Path(path).read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise FatalRtlBuddyError(
                f"{path}:{lineno}: expected KEY=VALUE, got {raw.strip()!r}"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key] = value
    return env


def apply_env_file(project_root: str | Path) -> dict[str, str]:
    """Load ``<project_root>/.rtl-buddy/.env`` into ``os.environ``.

    Only keys absent from the process environment are applied; the rest
    are reported as skipped. Missing file is a silent no-op. Returns the
    dict of variables actually applied.
    """
    path = Path(project_root) / ENV_FILE_RELPATH
    if not path.is_file():
        return {}
    parsed = parse_env_file(path)
    applied = {k: v for k, v in parsed.items() if k not in os.environ}
    os.environ.update(applied)
    # INFO when something was actually injected — this mutates the
    # environment of every downstream tool subprocess, so it should be
    # discoverable in rtl_buddy.log; DEBUG otherwise to stay quiet.
    log_event(
        logger,
        logging.INFO if applied else logging.DEBUG,
        "env_file.applied",
        path=str(path),
        applied=sorted(applied),
        skipped_already_set=sorted(set(parsed) - set(applied)),
    )
    return applied
