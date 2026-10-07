"""IEEE-1735 encryption of released sources with VCS ``-ipprotect``.

Each file is wrapped whole in ``pragma protect begin`` / ``end`` and encrypted
with the key file the release names. VCS writes the result beside the input
with a ``p`` appended to the extension (``.sv`` -> ``.svp``).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .sv_text import tokens

logger = logging.getLogger(__name__)

_BEGIN = "`pragma protect begin"
_END = "`pragma protect end"


def encrypted_name(name: str) -> str:
    return name + "p"


def find_vcs(configured: str) -> str:
    exe = shutil.which(configured) if not Path(configured).is_file() else configured
    if not exe:
        raise FatalRtlBuddyError(
            f"VCS executable '{configured}' not found; encryption needs VCS "
            "(set `encryption.vcs` in release.yaml or put vcs on PATH)"
        )
    return exe


def vcs_version(vcs: str) -> str:
    out = subprocess.run([vcs, "-ID"], capture_output=True, text=True, check=False)
    for line in (out.stdout + out.stderr).splitlines():
        if "version" in line.lower():
            return line.split(":", 1)[-1].strip()
    return "unknown"


def _encrypt_one(
    vcs: str, key_file: Path, extra: list[str], src: Path, dst: Path
) -> None:
    text = src.read_text(errors="replace")
    if "pragma protect" in text:
        raise FatalRtlBuddyError(
            f"{src.name} already carries `pragma protect` directives; the release "
            "flow wraps whole files and cannot encrypt a pre-protected one"
        )
    with tempfile.TemporaryDirectory(prefix="rb-release-enc-") as tmp:
        tmpdir = Path(tmp)
        work = tmpdir / src.name
        work.write_text(f"{_BEGIN}\n{text}\n{_END}\n")
        cmd = [
            vcs,
            "-sverilog",
            "-full64",
            "-ipprotect",
            str(key_file),
            "-ipopt=partialprotect",
            "-ipopt=noincludeprotect",
            "-ipopt=overwrite",
            *extra,
            str(work),
        ]
        proc = subprocess.run(
            cmd, cwd=tmpdir, capture_output=True, text=True, check=False
        )
        out = tmpdir / encrypted_name(src.name)
        if proc.returncode != 0 or not out.is_file():
            raise FatalRtlBuddyError(
                f"VCS encryption of {src.name} failed (exit {proc.returncode}): "
                f"{(proc.stdout + proc.stderr).strip()[-2000:]}"
            )
        problems = plaintext_outside_envelope(out.read_text(errors="replace"))
        if problems:
            raise FatalRtlBuddyError(
                f"encrypted {src.name} still holds plaintext outside its protected "
                f"envelope: {problems[:5]}"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, dst)


def plaintext_outside_envelope(text: str) -> list[str]:
    """Lines of an encrypted file that are neither protected data nor ``pragma protect`` directives.

    Empty means the file discloses nothing but its envelope.
    """
    problems: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("`pragma protect begin_protected"):
            inside = True
            continue
        if line.startswith("`pragma protect end_protected"):
            inside = False
            continue
        if inside or not line or line.startswith("`pragma protect"):
            continue
        if any(t.kind == "ident" for t in tokens(line)):
            problems.append(line[:80])
    if inside:
        problems.append("<unterminated begin_protected block>")
    return problems


def encrypt_files(
    vcs: str,
    key_file: Path,
    extra: list[str],
    jobs: list[tuple[Path, Path]],
    workers: int,
) -> None:
    """Encrypt each ``(src, dst)`` pair; ``dst`` names the output file exactly."""
    if not key_file.is_file():
        raise FatalRtlBuddyError(f"encryption key file not found: {key_file}")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(_encrypt_one, vcs, key_file, extra, src, dst)
            for src, dst in jobs
        ]
        for f in futures:
            f.result()
    log_event(logger, logging.INFO, "release.encrypted", files=len(jobs))
