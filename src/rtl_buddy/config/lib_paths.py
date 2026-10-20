"""`lib-paths` of `pnr.yaml` and `power.yaml`: each entry is one Liberty read into every corner, or a mapping of platform corner to the Liberty read into that corner only.

Whether a mapping names the platform's corners is checked when the run starts (see `lib_paths_problem`), since the platform is resolved then.
"""

import os

from ..errors import FatalRtlBuddyError

#: One `lib-paths` entry: a Liberty path, or corner name -> Liberty path.
LibPath = str | dict[str, str]


def load_lib_paths(where: str, value, config_dir: str) -> list[LibPath]:
    """Return `lib-paths` with every path resolved against `config_dir`, each mapping in config order."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise FatalRtlBuddyError(f"{where}: lib-paths must be a list, not {value!r}")
    out: list[LibPath] = []
    for index, entry in enumerate(value):
        if isinstance(entry, str) and entry:
            out.append(os.path.normpath(os.path.join(config_dir, entry)))
            continue
        if not isinstance(entry, dict) or not entry:
            raise FatalRtlBuddyError(
                f"{where}: lib-paths[{index}] must be a Liberty path or a mapping "
                f"of each platform corner to its Liberty path, not {entry!r}"
            )
        mapping: dict[str, str] = {}
        for corner, path in entry.items():
            if not isinstance(corner, str) or not isinstance(path, str) or not path:
                raise FatalRtlBuddyError(
                    f"{where}: lib-paths[{index}]: corner {corner!r} must map to "
                    f"a Liberty path, not {path!r}"
                )
            mapping[corner] = os.path.normpath(os.path.join(config_dir, path))
        out.append(mapping)
    return out


def lib_files(entries: list[LibPath]) -> list[str]:
    """Every Liberty file `entries` name, in entry order, a mapping's in its order."""
    return [
        path
        for entry in entries
        for path in ([entry] if isinstance(entry, str) else entry.values())
    ]


def lib_paths_problem(entries: list[LibPath], corners: list[str]) -> str | None:
    """Why a mapping in `entries` does not fit a platform of `corners`, or None: each mapping must name every corner and no other."""
    problems = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            continue
        missing = [c for c in corners if c not in entry]
        unknown = [c for c in entry if c not in corners]
        if missing:
            problems.append(
                f"lib-paths[{index}] has no Liberty for corner(s) {', '.join(missing)}"
            )
        if unknown:
            problems.append(
                f"lib-paths[{index}] names corner(s) {', '.join(unknown)} not among "
                f"the platform's ({', '.join(corners)})"
            )
    return "; ".join(problems) or None


def fit_lib_paths(entries: list[LibPath], corners: list[str]) -> list[LibPath]:
    """Return `entries` for a platform of `corners`, which they must fit (see `lib_paths_problem`): on one corner a mapping becomes its one path, on several it is put in `corners` order."""
    if len(corners) == 1:
        return [e if isinstance(e, str) else e[corners[0]] for e in entries]
    return [e if isinstance(e, str) else {c: e[c] for c in corners} for e in entries]


def corner_lib_file(entry: LibPath, corner: str) -> str:
    """The Liberty `entry` reads into `corner`."""
    return entry if isinstance(entry, str) else entry[corner]
