"""Verible obfuscation with one name map shared by every released file.

``verible-verilog-obfuscate`` renames identifiers lexically, file by file, and
keeps a dictionary across runs through ``--load_map`` / ``--save_map``. That makes
a design obfuscated file by file consistent, as long as every file goes through
the same dictionary in sequence and the dictionary starts with the names that
must not change.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .namemap import NameMap
from .sv_text import tokens

logger = logging.getLogger(__name__)

EXE = "verible-verilog-obfuscate"


def find_verible(configured: str | None, root_cfg_path: str | None) -> str:
    """Locate ``verible-verilog-obfuscate``: the release.yaml path, then the project's ``cfg-verible`` directory, then PATH."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    if root_cfg_path:
        candidates.append(str(Path(root_cfg_path) / EXE))
    for c in candidates:
        if Path(c).is_file():
            return c
    on_path = shutil.which(EXE)
    if on_path:
        return on_path
    raise FatalRtlBuddyError(
        f"{EXE} not found (looked in {candidates or 'nothing configured'} and on PATH); "
        "set `obfuscation.verible` in release.yaml or `cfg-verible` in root_config.yaml"
    )


def verible_version(exe: str) -> str:
    out = subprocess.run(
        [exe, "--version"], capture_output=True, text=True, check=False
    )
    first = (out.stdout or out.stderr).strip().splitlines()
    return first[0] if first else "unknown"


def _run(exe: str, args: list[str], src_text: str, cwd: Path) -> str:
    proc = subprocess.run(
        [exe, *args],
        input=src_text,
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if proc.returncode != 0:
        raise FatalRtlBuddyError(
            f"{EXE} {' '.join(args)} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:2000]}"
        )
    return proc.stdout


def interface_names(exe: str, files: list[Path]) -> dict[Path, set[str]]:
    """Module, port and parameter names declared by each file.

    Runs the obfuscator with ``--preserve_interface`` on each file alone and
    reads back the identity entries it records. Verible's built-in function
    names (``sqrt``, ``ceil``...) are identity entries in every map, so they are
    removed using a run on an empty input.
    """
    out: dict[Path, set[str]] = {}
    with tempfile.TemporaryDirectory(prefix="rb-release-iface-") as tmp:
        tmpdir = Path(tmp)
        empty_map = tmpdir / "empty.map"
        _run(exe, ["--save_map", str(empty_map)], "", tmpdir)
        builtins = set(NameMap.load(empty_map).entries)
        for f in files:
            m = tmpdir / "iface.map"
            _run(
                exe,
                ["--preserve_interface", "--save_map", str(m)],
                f.read_text(errors="replace"),
                tmpdir,
            )
            nm = NameMap.load(m)
            out[f] = {k for k, v in nm.entries.items() if k == v} - builtins
    return out


#: SystemVerilog built-in method names (IEEE 1800 string, enum, array, queue,
#: randomization, event, process, mailbox and semaphore methods). Verible renames
#: most of them like any identifier, which breaks ``q.push_back(x)``, and leaves a
#: few unrenamed after a dot, which splits a user field named ``min``. Keeping
#: them all avoids both.
SV_BUILTIN_METHODS = frozenset(
    """
    len putc getc toupper tolower compare icompare substr atoi atohex atooct atobin
    atoreal itoa hextoa octtoa bintoa realtoa
    first last next prev num name
    size delete exists insert push_front push_back pop_front pop_back
    find find_index find_first find_first_index find_last find_last_index
    min max unique unique_index reverse sort rsort shuffle sum product with index item
    randomize srandom get_randstate set_randstate rand_mode constraint_mode
    pre_randomize post_randomize triggered matched
    self status kill await suspend resume
    put get try_put try_get peek try_peek
    """.split()
)


def obfuscate_files(
    exe: str,
    jobs: list[tuple[Path, Path]],
    seed: NameMap,
    work_dir: Path,
) -> NameMap:
    """Obfuscate each ``(src, dst)`` pair in order through one shared map; return the final map.

    ``src`` is already comment-stripped. Every output is checked to be the same
    length as its input (Verible keeps identifier lengths) and non-empty when
    the input is, which catches the obfuscator dropping a file it cannot lex.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    map_path = work_dir / "running.map"
    seed.save(map_path)
    for src, dst in jobs:
        text = src.read_text(errors="replace")
        result = _run(
            exe,
            ["--load_map", str(map_path), "--save_map", str(map_path)],
            text,
            work_dir,
        )
        if len(result) != len(text) or (text.strip() and not result.strip()):
            raise FatalRtlBuddyError(
                f"{EXE} produced {len(result)} bytes from {len(text)} for {src.name}; "
                "it rewrites identifiers at their own length, so the file was not "
                "obfuscated as a whole (a construct it cannot lex?)"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(result)
    final = NameMap.load(map_path)
    final.check_injective()
    for name, value in seed.entries.items():
        if final.entries.get(name) != value:
            raise FatalRtlBuddyError(
                f"the obfuscator changed the seeded name '{name}' "
                f"(expected '{value}', got '{final.entries.get(name)}')"
            )
    log_event(
        logger,
        logging.INFO,
        "release.obfuscated",
        files=len(jobs),
        renamed=len(final.renamed()),
    )
    return final


def leaked_names(text: str, name_map: NameMap) -> set[str]:
    """Original names that still appear in obfuscated ``text``.

    A token counts as leaked when the map renames it and no other name was
    renamed *to* it (the obfuscated spelling of one name may coincide with an
    original name elsewhere in the design).
    """
    renamed = name_map.renamed()
    produced = set(renamed.values())
    return {
        t.text
        for t in tokens(text)
        if t.kind == "ident" and t.text in renamed and t.text not in produced
    }
