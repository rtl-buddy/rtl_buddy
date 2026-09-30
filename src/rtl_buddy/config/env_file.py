"""Project-local environment defaults from ``.rtl-buddy/.env``.

The file holds machine-local, uncommitted values (e.g. ``RTL_BUDDY_SLANG_PLUGIN``, ``SYSTEMC_HOME``).
It is a fallback only: a variable already in the process environment is never overridden.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event

# Under .rtl-buddy/ because a bare .env collides with docker-compose/node/direnv.
ENV_FILE_RELPATH = Path(".rtl-buddy") / ".env"


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from an env file.

    Blank lines and ``#`` comments are skipped, a leading ``export `` is accepted, and matching surrounding quotes are stripped.
    Values are otherwise literal. A line without ``=`` or with an empty key raises ``FatalRtlBuddyError``.
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

    Only keys absent from the process environment are applied. A missing file is a no-op.
    Returns the variables applied.
    """
    path = Path(project_root) / ENV_FILE_RELPATH
    if not path.is_file():
        return {}
    parsed = parse_env_file(path)
    applied = {k: v for k, v in parsed.items() if k not in os.environ}
    os.environ.update(applied)
    log_event(
        logger,
        logging.INFO if applied else logging.DEBUG,
        "env_file.applied",
        path=str(path),
        applied=sorted(applied),
        skipped_already_set=sorted(set(parsed) - set(applied)),
    )
    return applied
