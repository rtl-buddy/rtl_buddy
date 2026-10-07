"""The ``rb release`` flow: collect, protect, package and verify one customer release.

Stages, each a directory tree with the same layout as the delivered package:

- ``stage/src``: the sources as they are in the repository (internal reference).
- ``stage/obf``: comments stripped and identifiers obfuscated, per file policy.
- ``stage/pkg``: ``stage/obf`` with the encrypted files replaced by their
  encrypted form, plus notes, documents and the manifest. This is what is tarred.

Verification runs the release's own command in a copy of each requested stage
(the package from the unpacked tarball), so a release whose obfuscation or
encryption changed behaviour cannot be produced.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from importlib.metadata import version as _pkg_version
from ..config.model import ModelConfigLoader
from ..config.root import discover_project_root
from ..errors import FatalRtlBuddyError, FilelistError
from ..logging_utils import log_event
from ..tools.vlog_filelist import VlogFilelist
from . import encrypt as enc
from . import obfuscate as obf
from . import sdc as sdc_mod
from .config import (
    TOOL_MACROS,
    FileRule,
    Protection,
    ReleaseConfig,
    resolve_protection,
)
from .namemap import NameMap, previous_map, seed_map
from .sv_text import (
    declared_units,
    identifiers,
    include_targets,
    lexical_hazards,
    paste_patterns,
    strip_comments,
)

logger = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


DESIGN_DIR = "design"
VERIF_DIR = "verif"
CONSTRAINTS_DIR = "design/constraints"


@dataclass
class Item:
    """One shipped file."""

    src: Path
    section: str  # "design" or "testbench"
    #: "unit" (named in the filelist), "lib" (named with -v) or "header" (reached by `include).
    role: str
    protect: Protection
    rules: list[FileRule] = field(default_factory=list)
    #: Package directory chosen by a rule; ``design`` or ``verif`` by section otherwise.
    pkg_dir: str | None = None

    @property
    def name(self) -> str:
        return self.src.name

    @property
    def subdir(self) -> str:
        if self.pkg_dir:
            return self.pkg_dir
        return DESIGN_DIR if self.section == "design" else VERIF_DIR

    def shipped_name(self) -> str:
        if self.protect.encrypt and self.role != "header":
            return enc.encrypted_name(self.name)
        return self.name

    def level(self) -> str:
        parts = [
            p
            for p, on in (
                ("obfuscated", self.protect.obfuscate),
                ("encrypted", self.protect.encrypt),
            )
            if on
        ]
        return "+".join(parts) or "clear"


@dataclass
class FilelistLine:
    kind: str  # "item", "incdir", "define", "external", "libext"
    value: str
    item: Item | None = None
    #: Package directory whose filelist carries a non-item line.
    dir: str | None = None

    @property
    def target(self) -> str:
        if self.item is not None:
            return self.item.subdir
        assert self.dir is not None
        return self.dir


@dataclass
class Collected:
    items: list[Item]
    design_lines: list[FilelistLine]
    tb_lines: list[FilelistLine]
    defines: set[str]
    external_sources: list[Path]
    top: str


@dataclass
class ReleaseOptions:
    allow_dirty: bool = False
    verify: bool = True
    force: bool = False
    #: Run every stage but archive nothing, so the version stays unreleased.
    trial: bool = False
    #: A released version's archived manifest (``maps/<version>.json``, its map beside it): re-cut that release from its own names and require identical plaintext.
    reproduce: Path | None = None


class ReleaseFlow:
    def __init__(
        self,
        cfg: ReleaseConfig,
        artefact_dir: Path,
        opts: ReleaseOptions,
        verible_dir: str | None = None,
    ):
        self.cfg = cfg
        self.out = artefact_dir
        self.opts = opts
        self.verible_dir = verible_dir
        self.project_root = discover_project_root(start_dir=cfg.root)
        self.package_name = f"{cfg.name}-{cfg.version}"

    # ---- entry -------------------------------------------------------------

    def run(self) -> Path:
        cfg = self.cfg
        git = self._git_state()
        if git["dirty"] and not self.opts.allow_dirty:
            raise FatalRtlBuddyError(
                f"the project tree at {self.project_root} has uncommitted changes; "
                "a release must be reproducible from a commit (pass --allow-dirty "
                "for a trial run; the manifest records it)"
            )
        self.commit_time = git["commit_time"]
        ref = self._load_reference(git) if self.opts.reproduce else None
        map_out = cfg.map_path()
        if map_out.exists() and not (self.opts.force or self.opts.trial or ref):
            raise FatalRtlBuddyError(
                f"{map_out} already exists: version {cfg.version} was released. "
                "Bump `version`, or pass --force to replace that release's map "
                "(the old map is the only way to read its obfuscated names)"
            )
        verible = obf.find_verible(cfg.obfuscation.verible, self.verible_dir)
        vcs = enc.find_vcs(cfg.encryption.vcs)
        if not cfg.encryption.key_file.is_file():
            raise FatalRtlBuddyError(
                f"encryption key file not found: {cfg.encryption.key_file}"
            )
        notes = cfg.notes_path()
        if not notes.is_file():
            raise FatalRtlBuddyError(
                f"release notes for {cfg.version} not found: {notes} "
                "(every release ships its notes)"
            )
        for doc in cfg.package.docs:
            if not doc.is_file():
                raise FatalRtlBuddyError(f"package document not found: {doc}")

        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)

        col = self.collect()
        # Outside git there is no commit to reproduce from; the manifest records commit: null.
        uncommitted = (
            self._uncommitted_inputs(self._input_paths(col)) if git["commit"] else []
        )
        if uncommitted:
            if not self.opts.allow_dirty:
                raise FatalRtlBuddyError(
                    "release inputs are not committed, so the release could not be "
                    "reproduced from its commit:\n  " + "\n  ".join(uncommitted[:50])
                )
            git["dirty"] = True
            git["changes"] = git.get("changes", []) + [
                f"input {u}" for u in uncommitted
            ]
        stripped = self._strip(col)
        preserve, preserve_report, interfaces = self._preserve_set(
            col, stripped, verible
        )
        if ref is not None:
            prev_path = self.opts.reproduce.with_suffix(".map")
            seed = NameMap.load(prev_path)
            renamed_now = sorted(n for n in preserve if seed.entries.get(n, n) != n)
            if renamed_now:
                raise FatalRtlBuddyError(
                    "names the release renamed are now preserved, so the inputs "
                    f"differ from the released ones: {', '.join(renamed_now[:20])}"
                )
            seed.entries.update({n: n for n in preserve})
            dropped = []
        else:
            prev_path = self._previous_map_path()
            prev = NameMap.load(prev_path) if prev_path else None
            seed, dropped = seed_map(preserve, prev)
        if dropped:
            log_event(
                logger,
                logging.WARNING,
                "release.map_entries_dropped",
                count=len(dropped),
                names=", ".join(dropped[:20]),
            )

        stage = self.out / "stage"
        self._write_stage_src(col, stage / "src")
        name_map = self._write_stage_obf(col, stripped, seed, verible, stage / "obf")
        if ref is not None:
            invented = sorted(set(name_map.entries) - set(seed.entries))
            if invented:
                raise FatalRtlBuddyError(
                    "the inputs hold identifiers the released map does not, so they "
                    f"differ from the released ones: {', '.join(invented[:20])}"
                )
        self._write_constraints(col, name_map, interfaces, stage / "obf")
        self._write_stage_pkg(col, vcs, stage / "obf", stage / "pkg")
        tarball = self._tar(stage / "pkg", git["commit_time"])
        if ref is not None:
            self._compare_reference(ref, col, stage)

        results = {}
        if self.opts.verify and cfg.verify is not None:
            results = self._verify(stage, tarball)
        elif cfg.verify is None:
            log_event(logger, logging.WARNING, "release.verify_unconfigured")

        self._archive(
            name_map, col, git, verible, vcs, prev_path, preserve_report, results
        )
        log_event(
            logger,
            logging.INFO,
            "release.done",
            path=str(tarball),
            map=str(map_out),
        )
        return tarball

    # ---- collect -----------------------------------------------------------

    def _extract(self, lines: list[str], anchor: Path) -> list[tuple[str, str | None]]:
        try:
            return VlogFilelist("release/collect", None, None)._extract(
                lines, True, str(anchor)
            )
        except FilelistError as exc:
            raise FatalRtlBuddyError(str(exc)) from exc

    def _external_for(self, path: str):
        for ext in self.cfg.design.externals:
            base = os.path.normpath(ext.path)
            if path == base or path.startswith(base + os.sep):
                return ext
        return None

    def _ship_external(self, path: str) -> str:
        ext = self._external_for(path)
        assert ext is not None
        rel = os.path.relpath(path, os.path.normpath(ext.path))
        return ext.ship_as if rel == "." else f"{ext.ship_as.rstrip('/')}/{rel}"

    def _relpath(self, path: str) -> str:
        return os.path.relpath(path, self.project_root).replace(os.sep, "/")

    def _inside_project(self, path: str) -> bool:
        rel = os.path.relpath(path, self.project_root)
        return not (rel == os.pardir or rel.startswith(os.pardir + os.sep))

    def _classify(
        self,
        entries: list[tuple[str, str | None]],
        section: str,
        seen: dict[str, Item],
        lines: list[FilelistLine],
        defines: set[str],
        incdirs: list[str],
        external_sources: list[Path],
    ) -> list[Item]:
        cfg = self.cfg
        default = cfg.design.protect if section == "design" else cfg.testbench.protect  # type: ignore[union-attr]
        rules = cfg.design.files if section == "design" else cfg.testbench.files  # type: ignore[union-attr]
        new: list[Item] = []
        home = DESIGN_DIR if section == "design" else VERIF_DIR
        emitted = {(ln.kind, ln.value) for ln in lines if ln.item is None}

        def option_line(kind: str, value: str, pkg_dir: str | None = None) -> None:
            # Nested filelists repeat their options; each is written once.
            if (kind, value) not in emitted:
                emitted.add((kind, value))
                lines.append(FilelistLine(kind, value, dir=pkg_dir or home))

        for value, option in entries:
            if option == "+define+":
                defines.add(value.split("=", 1)[0])
                option_line("define", value)
                continue
            if option == "+libext+":
                option_line("libext", value)
                continue
            path = os.path.normpath(value)
            ext = self._external_for(path)
            if ext:
                shipped = self._ship_external(path)
                if option == "+incdir+":
                    option_line("external", f"+incdir+{shipped}", ext.dir)
                elif option == "-y ":
                    option_line("external", f"-y {shipped}", ext.dir)
                else:
                    prefix = "-v " if option == "-v " else ""
                    option_line("external", f"{prefix}{shipped}", ext.dir)
                    external_sources.append(Path(path))
                continue
            if not self._inside_project(path):
                raise FatalRtlBuddyError(
                    f"{path} is outside the project and not under any "
                    "`design.externals` path: either ship it from the project or "
                    "declare where the customer gets it"
                )
            if option == "+incdir+":
                incdirs.append(path)
                continue
            if option == "-y ":
                raise FatalRtlBuddyError(
                    f"-y {path}: library directories inside the project are not "
                    "supported by the release flow; list the files"
                )
            if path in seen:
                continue
            if not os.path.isfile(path):
                raise FatalRtlBuddyError(f"{section} source not found: {path}")
            role = "lib" if option == "-v " else "unit"
            prot, applied, pkg_dir = resolve_protection(
                os.path.basename(path), self._relpath(path), default, rules
            )
            item = Item(Path(path), section, role, prot, applied, pkg_dir)
            seen[path] = item
            new.append(item)
            lines.append(FilelistLine("item", path, item))
        return new

    def _headers(
        self,
        items: list[Item],
        incdirs: list[str],
        section: str,
        seen: dict[str, Item],
    ) -> list[Item]:
        cfg = self.cfg
        default = cfg.design.protect if section == "design" else cfg.testbench.protect  # type: ignore[union-attr]
        rules = cfg.design.files if section == "design" else cfg.testbench.files  # type: ignore[union-attr]
        found: list[Item] = []
        queue = list(items)
        while queue:
            item = queue.pop(0)
            text = item.src.read_text(errors="replace")
            for target in include_targets(text):
                candidates = [item.src.parent / target] + [
                    Path(d) / target for d in incdirs
                ]
                hit = next((c for c in candidates if c.is_file()), None)
                if hit is None:
                    ext_hit = any(
                        (Path(ext.path) / target).is_file()
                        for ext in cfg.design.externals
                    )
                    if ext_hit:
                        continue
                    raise FatalRtlBuddyError(
                        f'{item.src}: `include "{target}" not found in its '
                        "directory or the filelist's include directories"
                    )
                path = os.path.normpath(str(hit.resolve()))
                if self._external_for(path):
                    continue
                if path in seen:
                    continue
                if "/" in target:
                    raise FatalRtlBuddyError(
                        f'{item.src}: `include "{target}" names a directory; the '
                        "release flattens files into one directory, so include by "
                        "file name and add the directory with +incdir+"
                    )
                prot, applied, pkg_dir = resolve_protection(
                    os.path.basename(path), self._relpath(path), default, rules
                )
                header = Item(Path(path), section, "header", prot, applied, pkg_dir)
                seen[path] = header
                found.append(header)
                queue.append(header)
        return found

    def collect(self) -> Collected:
        cfg = self.cfg
        model = ModelConfigLoader(str(cfg.design.model_config)).get_model(
            cfg.design.model
        )
        top = cfg.design.top or model.get_top()
        seen: dict[str, Item] = {}
        design_lines: list[FilelistLine] = []
        tb_lines: list[FilelistLine] = []
        defines: set[str] = set()
        external_sources: list[Path] = []
        d_inc: list[str] = []
        entries = self._extract(
            model.get_filelist(), Path(model.get_model_path()).resolve()
        )
        d_items = self._classify(
            entries, "design", seen, design_lines, defines, d_inc, external_sources
        )
        d_items += self._headers(d_items, d_inc, "design", seen)
        items = list(d_items)
        if cfg.testbench is not None:
            t_inc: list[str] = []
            t_entries = self._extract(cfg.testbench.filelist, cfg.path)
            t_items = self._classify(
                t_entries, "testbench", seen, tb_lines, defines, t_inc, external_sources
            )
            t_items += self._headers(t_items, t_inc + d_inc, "testbench", seen)
            items += t_items

        by_name: dict[str, Item] = {}
        for item in items:
            other = by_name.setdefault(item.name, item)
            if other is not item:
                raise FatalRtlBuddyError(
                    f"two shipped files share the name {item.name}: {other.src} and "
                    f"{item.src}; the release flattens each section into one directory"
                )
        for section, rules in (
            ("design", cfg.design.files),
            ("testbench", cfg.testbench.files if cfg.testbench else []),
        ):
            used = {id(r) for i in items if i.section == section for r in i.rules}
            stale = [r.match for r in rules if id(r) not in used]
            if stale:
                raise FatalRtlBuddyError(
                    f"{section}.files rule(s) {stale} match no shipped file; remove "
                    "or fix them so the reviewed exceptions stay accurate"
                )
        log_event(
            logger,
            logging.INFO,
            "release.collected",
            design=len(d_items),
            testbench=len(items) - len(d_items),
            externals=len(external_sources),
        )
        return Collected(items, design_lines, tb_lines, defines, external_sources, top)

    # ---- protection --------------------------------------------------------

    def _strip(self, col: Collected) -> dict[Path, str]:
        keep = re.compile("|".join(self.cfg.obfuscation.keep_comments), re.I)
        out: dict[Path, str] = {}
        for item in col.items:
            text = item.src.read_text(errors="replace")
            out[item.src] = (
                strip_comments(text, keep) if item.protect.strip_comments else text
            )
        return out

    def _preserve_set(
        self, col: Collected, stripped: dict[Path, str], verible: str
    ) -> tuple[set[str], dict, dict[str, set[str]]]:
        cfg = self.cfg
        decl_file: dict[str, Item] = {}
        for item in col.items:
            if item.section != "design":
                continue
            for unit in declared_units(stripped[item.src]):
                decl_file.setdefault(unit, item)
        wanted = [col.top, *cfg.design.preserve_interfaces]
        missing = [m for m in wanted if m not in decl_file]
        if missing:
            raise FatalRtlBuddyError(
                f"preserved interface module(s) {missing} are not declared by any "
                "shipped design file"
            )
        iface_files = sorted({decl_file[m].src for m in wanted})
        harvested = obf.interface_names(
            verible,
            iface_files + sorted(set(col.external_sources)),
            lexical_fallback=frozenset(col.external_sources),
        )
        interfaces = {m: harvested[decl_file[m].src] for m in wanted}

        preserve: set[str] = set(TOOL_MACROS) | set(cfg.design.preserve_identifiers)
        preserve |= obf.SV_BUILTIN_METHODS
        preserve |= col.defines
        for names in harvested.values():
            preserve |= names

        pasted: set[str] = set()
        if cfg.obfuscation.token_paste == "preserve":
            patterns: list = []
            all_names: set[str] = set()
            for item in col.items:
                text = stripped[item.src]
                all_names |= identifiers(text)
                if item.protect.obfuscate:
                    pats, literals = paste_patterns(text)
                    patterns += pats
                    pasted |= literals
            for name in all_names:
                for pat in patterns:
                    m = pat.fullmatch(name)
                    if m:
                        # An identifier passed as the pasted argument keeps its spelling too.
                        pasted.add(name)
                        pasted |= {g for g in m.groups() if g in all_names}
            if pasted:
                log_event(
                    logger,
                    logging.WARNING,
                    "release.token_paste_preserved",
                    count=len(pasted),
                    patterns=", ".join(sorted({p.pattern for p in patterns})),
                )
        preserve |= pasted

        obf_decls: dict[str, Item] = {}
        for unit, item in decl_file.items():
            if item.protect.obfuscate:
                obf_decls[unit] = item
        forced: dict[str, list[str]] = {}
        allowed = set(cfg.testbench.allow_design_refs if cfg.testbench else [])
        allowed |= set(wanted)
        for item in col.items:
            if item.protect.obfuscate:
                continue
            names = identifiers(stripped[item.src])
            preserve |= names
            for unit in sorted(names & set(obf_decls) - set(wanted)):
                forced.setdefault(unit, []).append(item.name)
        tb_refs = {
            unit: files
            for unit, files in forced.items()
            if unit not in allowed
            and any(i.section == "testbench" and i.name in files for i in col.items)
        }
        if tb_refs:
            detail = "; ".join(f"{u} (from {', '.join(f)})" for u, f in tb_refs.items())
            raise FatalRtlBuddyError(
                "the testbench names design units that would otherwise be "
                f"obfuscated: {detail}. A clear-text testbench forces every name "
                "it uses into the clear across the whole design; publish what it "
                "needs deliberately by listing it in `testbench.allow-design-refs`, "
                "or stop referring to it"
            )
        if forced:
            log_event(
                logger,
                logging.WARNING,
                "release.names_forced_clear",
                count=len(forced),
                names=", ".join(sorted(forced)),
            )
        report = {
            "preserved": len(preserve),
            "interfaces": {m: sorted(n) for m, n in interfaces.items()},
            "forced_clear_units": {u: f for u, f in sorted(forced.items())},
            "token_paste_preserved": sorted(pasted),
        }
        return preserve, report, interfaces

    def _previous_map_path(self) -> Path | None:
        cf = self.cfg.obfuscation.continue_from
        if cf == "none":
            return None
        if cf == "previous":
            return previous_map(self.cfg.obfuscation.map_dir, self.cfg.version)
        path = (self.cfg.root / cf).resolve()
        if not path.is_file():
            raise FatalRtlBuddyError(f"obfuscation.continue-from map not found: {path}")
        return path

    # ---- stages ------------------------------------------------------------

    def _filelist_text(
        self,
        lines: list[FilelistLine],
        incdirs: list[str],
        title: str,
        pkg: bool,
        only_dir: str | None = None,
    ) -> str:
        """Render filelist lines with release-root-relative paths; ``only_dir`` keeps the lines one package directory carries."""
        out = [
            f"// {self.cfg.name} {self.cfg.version}: {title}",
            "// Paths are relative to the release root; run tools from there.",
            *(f"+incdir+{d}" for d in incdirs),
        ]
        for ln in lines:
            if only_dir is not None and ln.target != only_dir:
                continue
            if ln.kind == "item":
                assert ln.item is not None
                name = ln.item.shipped_name() if pkg else ln.item.name
                prefix = "-v " if ln.item.role == "lib" else ""
                out.append(f"{prefix}{ln.item.subdir}/{name}")
            elif ln.kind == "define":
                out.append(f"+define+{ln.value}")
            elif ln.kind == "libext":
                out.append(f"+libext+{ln.value}")
            elif ln.kind == "external":
                out.append(ln.value)
        return "\n".join(out) + "\n"

    def _filelists(self, col: Collected, root: Path, pkg: bool) -> None:
        """One filelist per package directory, and ``sim.f`` with every line in compile order.

        A directory's filelist carries its files and the options and external
        references assigned to it: the design's go to ``design/<top>.f``, the
        testbench's to ``verif/tb.f``, an external's to its ``dir``.
        """
        everything = col.design_lines + col.tb_lines
        dirs = [DESIGN_DIR]
        for ln in everything:
            if ln.target not in dirs:
                dirs.append(ln.target)
        tb = self.cfg.testbench is not None
        if tb and VERIF_DIR not in dirs:
            dirs.append(VERIF_DIR)
        names = {
            DESIGN_DIR: (f"{col.top}.f", f"{col.top} design filelist"),
            VERIF_DIR: ("tb.f", "testbench filelist"),
        }
        for d in dirs:
            (root / d).mkdir(parents=True, exist_ok=True)
            fname, title = names.get(d, (f"{Path(d).name}.f", f"{d} filelist"))
            (root / d / fname).write_text(
                self._filelist_text(everything, [d], title, pkg, only_dir=d)
            )
        (root / "sim.f").write_text(
            self._filelist_text(
                everything,
                dirs,
                "simulation filelist: every file in compile order",
                pkg,
            )
        )
        if tb:
            for extra in self.cfg.testbench.extra_files:
                if not extra.is_file():
                    raise FatalRtlBuddyError(f"testbench extra file not found: {extra}")
                shutil.copy2(extra, root / VERIF_DIR / extra.name)

    def _write_stage_src(self, col: Collected, root: Path) -> None:
        for item in col.items:
            dst = root / item.subdir / item.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.src, dst)
        self._filelists(col, root, pkg=False)

    def _write_stage_obf(
        self,
        col: Collected,
        stripped: dict[Path, str],
        seed: NameMap,
        verible: str,
        root: Path,
    ) -> NameMap:
        to_obf = [i for i in col.items if i.protect.obfuscate]
        allow_paste = self.cfg.obfuscation.token_paste == "preserve"
        hazards = []
        for item in to_obf:
            for h in lexical_hazards(stripped[item.src]):
                if h.rule == "token-paste" and allow_paste:
                    continue
                hazards.append(f"{item.name}:{h.line}: {h.rule}: {h.detail}")
        if hazards:
            raise FatalRtlBuddyError(
                "constructs the lexical obfuscator renames inconsistently:\n  "
                + "\n  ".join(hazards[:50])
                + "\nRewrite them, exclude the file with `obfuscate: false` and a reason, "
                "or for token pasting set `obfuscation.token-paste: preserve`"
            )
        pre = self.out / "obf_in"
        jobs: list[tuple[Path, Path]] = []
        for item in col.items:
            dst = root / item.subdir / item.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if item.protect.obfuscate:
                tmp = pre / item.name
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(stripped[item.src])
                jobs.append((tmp, dst))
            else:
                dst.write_text(stripped[item.src])
        name_map = obf.obfuscate_files(verible, jobs, seed, self.out / "obf_work")
        leaks = {}
        for _, dst in jobs:
            leaked = obf.leaked_names(dst.read_text(), name_map)
            if leaked:
                leaks[dst.name] = sorted(leaked)[:10]
        if leaks:
            raise FatalRtlBuddyError(f"original names survived obfuscation: {leaks}")
        self._filelists(col, root, pkg=False)
        return name_map

    def _write_constraints(
        self,
        col: Collected,
        name_map: NameMap,
        interfaces: dict[str, set[str]],
        root: Path,
    ) -> None:
        problems: list[str] = []
        for c in self.cfg.design.constraints:
            if c.scope not in interfaces:
                raise FatalRtlBuddyError(
                    f"constraint {c.src.name}: scope {c.scope} must be a preserved "
                    "interface (the top or `design.preserve.interfaces`), or its "
                    "ports would be renamed"
                )
            if c.mode == "verbatim":
                text = c.src.read_text()
                errs = sdc_mod.check_verbatim(c.src, c.scope, interfaces[c.scope])
            else:
                text, errs = sdc_mod.rewrite(
                    c.src, name_map, c.scope, interfaces[c.scope]
                )
            problems += [f"{c.src.name}: {e}" for e in errs]
            dst = root / CONSTRAINTS_DIR / c.ship_as
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text)
        if problems:
            raise FatalRtlBuddyError(
                "constraints do not resolve against the released design:\n  "
                + "\n  ".join(problems)
            )

    def _write_stage_pkg(
        self, col: Collected, vcs: str, obf_root: Path, root: Path
    ) -> None:
        shutil.copytree(obf_root, root)
        jobs = []
        for item in col.items:
            if not item.protect.encrypt:
                continue
            src = obf_root / item.subdir / item.name
            jobs.append((src, root / item.subdir / item.shipped_name()))
        enc.encrypt_files(
            vcs,
            self.cfg.encryption.key_file,
            self.cfg.encryption.extra_args,
            jobs,
            self.cfg.encryption.jobs,
        )
        for item in col.items:
            if item.protect.encrypt and item.shipped_name() != item.name:
                (root / item.subdir / item.name).unlink()
        self._filelists(col, root, pkg=True)
        shutil.copy2(self.cfg.notes_path(), root / "RELEASE_NOTES.md")
        if self.cfg.package.docs:
            (root / "docs").mkdir(exist_ok=True)
            for doc in self.cfg.package.docs:
                shutil.copy2(doc, root / "docs" / doc.name)
        self._write_shipped_manifest(col, root)

    def _write_shipped_manifest(self, col: Collected, root: Path) -> None:
        levels = {f"{i.subdir}/{i.shipped_name()}": i.level() for i in col.items}
        lines = [
            f"{self.cfg.name} {self.cfg.version}",
            f"commit date {_dt.datetime.fromtimestamp(self.commit_time, _dt.timezone.utc).strftime('%Y-%m-%d')}",
            "",
            "sha256                                                            protection            file",
        ]
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {levels.get(rel, 'clear'):<20}  {rel}")
        (root / "MANIFEST").write_text("\n".join(lines) + "\n")

    def _tar(self, root: Path, mtime: int) -> Path:
        """Write the package deterministically: sorted entries, one timestamp, no owner, no gzip timestamp."""
        tarball = self.out / f"{self.package_name}.tar.gz"

        def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
            return info

        entries = [root, *sorted(root.rglob("*"))]
        with (
            open(tarball, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
        ):
            for path in entries:
                arcname = (Path(self.package_name) / path.relative_to(root)).as_posix()
                tar.add(path, arcname=arcname, recursive=False, filter=normalise)
        return tarball

    # ---- verify ------------------------------------------------------------

    def _run_verify(self, stage: str, root: Path) -> dict:
        v = self.cfg.verify
        assert v is not None
        log_path = self.out / "verify" / f"{stage}.log"
        env = dict(os.environ, RELEASE_ROOT=str(root), RELEASE_STAGE=stage)
        try:
            proc = subprocess.run(
                ["bash", "-c", v.command],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=v.timeout,
                check=False,
            )
            output, rc = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            rc = None
        log_path.write_text(output)
        passed = rc == 0 and re.search(v.passed, output, re.M) is not None
        compare = (
            [m.group(0) for m in re.finditer(v.compare, output, re.M)]
            if v.compare
            else []
        )
        log_event(
            logger,
            logging.INFO if passed else logging.ERROR,
            "release.verify_stage",
            stage=stage,
            passed=passed,
            returncode=rc,
            log=str(log_path),
        )
        return {
            "passed": passed,
            "returncode": rc,
            "log": str(log_path),
            "compare": compare,
        }

    def _verify(self, stage: Path, tarball: Path) -> dict:
        v = self.cfg.verify
        assert v is not None
        vdir = self.out / "verify"
        vdir.mkdir(parents=True, exist_ok=True)
        results = {}
        for name in v.stages:
            if name == "pkg":
                dest = vdir / "pkg"
                dest.mkdir()
                with tarfile.open(tarball) as tar:
                    tar.extractall(dest, filter="data")
                root = dest / self.package_name
            else:
                root = vdir / name
                shutil.copytree(stage / name, root)
            results[name] = self._run_verify(name, root)
        failed = [s for s, r in results.items() if not r["passed"]]
        if failed:
            raise FatalRtlBuddyError(
                f"release verification failed in stage(s) {failed}; see "
                + ", ".join(results[s]["log"] for s in failed)
            )
        if v.compare:
            ref_stage = v.stages[0]
            ref = results[ref_stage]["compare"]
            if not ref:
                raise FatalRtlBuddyError(
                    f"verify.compare matched nothing in stage {ref_stage}; the "
                    "comparison would be vacuous"
                )
            diff = [s for s, r in results.items() if r["compare"] != ref]
            if diff:
                raise FatalRtlBuddyError(
                    f"stage(s) {diff} produced different `verify.compare` lines than "
                    f"{ref_stage}: obfuscation or encryption changed behaviour"
                )
        return results

    # ---- archive -----------------------------------------------------------

    def _git_state(self) -> dict:
        def git(*args):
            return subprocess.run(
                ["git", *args],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )

        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return {"commit": None, "dirty": False, "changes": [], "commit_time": 0}
        # Untracked files count: one placed earlier on an include path shadows a
        # committed file. Gitignored files (artefacts/) do not.
        status = git("status", "--porcelain", "--untracked-files=normal")
        changes = [ln for ln in status.stdout.splitlines() if ln.strip()]
        when = git("log", "-1", "--format=%ct", "HEAD").stdout.strip()
        return {
            "commit": head.stdout.strip(),
            "dirty": bool(changes),
            "changes": changes,
            "commit_time": int(when) if when.isdigit() else 0,
        }

    def _load_reference(self, git: dict) -> dict:
        path = self.opts.reproduce
        assert path is not None
        if not path.is_file() or not path.with_suffix(".map").is_file():
            raise FatalRtlBuddyError(
                f"--reproduce needs a released manifest and its map: {path} and "
                f"{path.with_suffix('.map')}"
            )
        ref = json.loads(path.read_text())
        if ref.get("version") != self.cfg.version or ref.get("name") != self.cfg.name:
            raise FatalRtlBuddyError(
                f"{path} is release {ref.get('name')} {ref.get('version')}, but "
                f"release.yaml is {self.cfg.name} {self.cfg.version}"
            )
        if ref.get("commit") != git["commit"]:
            raise FatalRtlBuddyError(
                f"release {self.cfg.version} was cut from {ref.get('commit')}, but "
                f"HEAD is {git['commit']}; check out the release tag first"
            )
        return ref

    def _compare_reference(self, ref: dict, col: Collected, stage: Path) -> None:
        """Fail unless every shipped file's plaintext matches the released manifest."""
        released = {f["shipped"]: f for f in ref.get("files", [])}
        problems = []
        for i in col.items:
            shipped = f"{i.subdir}/{i.shipped_name()}"
            want = released.pop(shipped, None)
            got = _sha256(stage / "obf" / i.subdir / i.name)
            if want is None:
                problems.append(f"{shipped}: not in release {self.cfg.version}")
            elif want.get("plaintext_sha256") not in (None, got):
                problems.append(f"{shipped}: plaintext differs")
            elif want.get("plaintext_sha256") is None:
                problems.append(
                    f"{shipped}: the released manifest predates plaintext digests"
                )
        problems += [f"{s}: missing from this cut" for s in released]
        if problems:
            raise FatalRtlBuddyError(
                f"the re-cut does not reproduce release {self.cfg.version}:\n  "
                + "\n  ".join(problems[:50])
            )
        log_event(
            logger,
            logging.INFO,
            "release.reproduced",
            version=self.cfg.version,
            files=len(col.items),
        )

    def _input_paths(self, col: Collected) -> list[Path]:
        cfg = self.cfg
        paths = [i.src for i in col.items]
        paths += [
            cfg.path,
            cfg.design.model_config,
            cfg.notes_path(),
            cfg.encryption.key_file,
        ]
        paths += cfg.package.docs + [c.src for c in cfg.design.constraints]
        if cfg.testbench is not None:
            paths += cfg.testbench.extra_files
        return sorted({Path(p).resolve() for p in paths})

    def _uncommitted_inputs(self, paths: list[Path]) -> list[str]:
        """Inputs that are not tracked, or differ from HEAD, in the repository that holds them (submodules included)."""
        problems = []
        for path in paths:

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=path.parent,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            if git("ls-files", "--error-unmatch", "--", path.name).returncode != 0:
                ignored = git("check-ignore", "-q", "--", path.name).returncode == 0
                problems.append(f"{path} ({'gitignored' if ignored else 'untracked'})")
            elif git("diff", "--quiet", "HEAD", "--", path.name).returncode != 0:
                problems.append(f"{path} (modified)")
        return problems

    def _archive(
        self,
        name_map: NameMap,
        col: Collected,
        git: dict,
        verible: str,
        vcs: str,
        prev_path: Path | None,
        preserve_report: dict,
        results: dict,
    ) -> None:
        cfg = self.cfg
        name_map.save(self.out / f"{cfg.version}.map")
        manifest = {
            "name": cfg.name,
            "version": cfg.version,
            "metadata": cfg.metadata,
            "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "commit": git["commit"],
            "dirty": git["dirty"],
            "rtl_buddy": _pkg_version("rtl-buddy"),
            "verible": obf.verible_version(verible),
            "vcs": vcs,
            "vcs_version": enc.vcs_version(vcs),
            "key_file": cfg.encryption.key_file.name,
            "top": col.top,
            "previous_map": prev_path.name if prev_path else None,
            "renamed": len(name_map.renamed()),
            "preserve": preserve_report,
            "uncommitted": git.get("changes", []),
            "files": [
                {
                    "source": os.path.relpath(i.src, self.project_root),
                    "shipped": f"{i.subdir}/{i.shipped_name()}",
                    "protection": i.level(),
                    "rules": [r.reason for r in i.rules],
                    "source_sha256": _sha256(i.src),
                    "plaintext_sha256": _sha256(
                        self.out / "stage" / "obf" / i.subdir / i.name
                    ),
                }
                for i in col.items
            ],
            "verify": {s: {"passed": r["passed"]} for s, r in results.items()},
        }
        (self.out / f"{cfg.version}.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        if self.opts.trial or self.opts.reproduce or git["dirty"] or not results:
            # A trial (requested, dirty or unverified) release is never archived as a real one.
            log_event(
                logger,
                logging.WARNING,
                "release.not_archived",
                path=str(self.out),
                reason=(
                    "--trial"
                    if self.opts.trial
                    else "--reproduce"
                    if self.opts.reproduce
                    else "dirty tree"
                    if git["dirty"]
                    else "not verified"
                ),
            )
            return
        cfg.obfuscation.map_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.out / f"{cfg.version}.map", cfg.map_path())
        shutil.copy2(
            self.out / f"{cfg.version}.json", cfg.map_path().with_suffix(".json")
        )
