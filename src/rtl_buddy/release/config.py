"""Schema and loader for ``release.yaml``, the configuration of one customer release.

Unknown keys are fatal rather than warned about: a misspelt protection key would
otherwise ship a file in the clear. Paths are resolved relative to the
``release.yaml`` that names them.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config.yaml_loader import load_yaml
from ..errors import FatalRtlBuddyError

FILETYPE = "release_config"

#: Macros the language and tools predefine. Renaming ``__FILE__`` leaves an undefined macro; renaming a tool macro silently changes which branch of an ``ifdef`` a tool takes.
TOOL_MACROS = (
    "__FILE__",
    "__LINE__",
    "SYNTHESIS",
    "SYNOPSYS",
    "VCS",
    "VERILATOR",
    "XILINX_SIMULATOR",
    "MODEL_TECH",
    "QUESTA",
    "XCELIUM",
    "INCA",
    "__ICARUS__",
    "FORMAL",
    "YOSYS",
)

#: Comments kept by the comment stripper: synthesis and simulator directives.
DEFAULT_KEEP_COMMENTS = (
    r"\bsynopsys\b",
    r"\bsynthesis\b",
    r"\bpragma\b",
    r"\btranslate_(on|off)\b",
    r"\bverilator\b",
    r"\bcadence\b",
    r"\bxilinx\b",
)

VERIFY_STAGES = ("src", "obf", "pkg")


@dataclass
class Protection:
    obfuscate: bool
    encrypt: bool
    strip_comments: bool


@dataclass
class FileRule:
    """A per-file override of the section's protection or package directory.

    ``match`` is a glob on the file's base name, or, when it contains ``/``, on its
    path relative to the project root.
    """

    match: str
    reason: str
    obfuscate: bool | None = None
    encrypt: bool | None = None
    strip_comments: bool | None = None
    #: Package directory the file ships in, instead of the section's ``design`` or ``verif``.
    dir: str | None = None

    def matches(self, basename: str, relpath: str) -> bool:
        return fnmatch.fnmatchcase(
            relpath if "/" in self.match else basename, self.match
        )


@dataclass
class External:
    """A directory whose files the customer supplies (a vendor library, say).

    Files under ``path`` are not shipped. Filelist lines naming them are rewritten
    to start with ``ship_as``, and the interfaces of the modules they declare are
    preserved so instances still bind.
    """

    path: str
    ship_as: str
    #: Package directory whose filelist carries these references; ``design`` by default.
    dir: str | None = None


@dataclass
class Constraint:
    src: Path
    scope: str
    ship_as: str
    #: ``rewrite`` (evaluate and translate) or ``verbatim`` (ship unchanged after a port check).
    mode: str = "rewrite"


@dataclass
class DesignSection:
    model: str
    model_config: Path
    top: str | None
    protect: Protection
    preserve_interfaces: list[str]
    preserve_identifiers: list[str]
    files: list[FileRule]
    externals: list[External]
    constraints: list[Constraint]


@dataclass
class TestbenchSection:
    filelist: list[str]
    extra_files: list[Path]
    protect: Protection
    files: list[FileRule]
    #: Names the design declares (modules, packages, interfaces) that the testbench may name, and which therefore ship unobfuscated.
    allow_design_refs: list[str]


@dataclass
class VerifySection:
    command: str
    passed: str
    compare: str | None
    stages: list[str]
    timeout: int


@dataclass
class EncryptionSection:
    key_file: Path
    vcs: str
    extra_args: list[str]
    jobs: int


@dataclass
class ObfuscationSection:
    verible: str | None
    map_dir: Path
    continue_from: str
    keep_comments: list[str]
    #: ``refuse`` or ``preserve`` (keep every name a token paste can form).
    token_paste: str


@dataclass
class CsrWindow:
    """One SystemRDL address map at its absolute base address."""

    name: str
    rdl: Path
    top: str
    base: int


@dataclass
class CsrRegister:
    """A whitelist entry: a glob on ``<window>.<path>`` (instance names, no array indices)."""

    match: str
    #: Field-name globs in the matched registers that ship as ``f<lsb>``.
    obfuscate_fields: list[str]


@dataclass
class CsrSection:
    #: Base name of the shipped files; ``<release name>_csr`` by default.
    name: str | None
    #: SystemVerilog macro prefix; the upper-cased ``name`` by default.
    prefix: str | None
    #: User-defined property that marks a register, regfile or memory eligible to ship.
    gate: str | None
    include_dirs: list[Path]
    windows: list[CsrWindow]
    registers: list[CsrRegister]


@dataclass
class PackageSection:
    notes: str
    docs: list[Path]


@dataclass
class ReleaseConfig:
    path: Path
    name: str
    version: str
    metadata: dict[str, Any]
    design: DesignSection
    testbench: TestbenchSection | None
    verify: VerifySection | None
    encryption: EncryptionSection
    obfuscation: ObfuscationSection
    package: PackageSection
    csr: CsrSection | None = None

    @property
    def root(self) -> Path:
        return self.path.parent

    def notes_path(self) -> Path:
        return self.root / self.package.notes.format(version=self.version)

    def map_path(self) -> Path:
        return self.obfuscation.map_dir / f"{self.version}.map"


# ---- loader -----------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")


class _Reader:
    """Typed access to one YAML mapping that rejects keys nobody read."""

    def __init__(self, data: Any, where: str, path: Path):
        if not isinstance(data, dict):
            raise FatalRtlBuddyError(f"{path}: `{where}` must be a mapping")
        self.data = data
        self.where = where
        self.path = path
        self.seen: set[str] = set()

    def _err(self, msg: str) -> FatalRtlBuddyError:
        return FatalRtlBuddyError(f"{self.path}: {self.where}: {msg}")

    def get(self, key: str, typ, default: Any = ..., *, required: bool = False):
        self.seen.add(key)
        if key not in self.data or self.data[key] is None:
            if required or default is ...:
                raise self._err(f"missing required key `{key}`")
            return default
        value = self.data[key]
        if typ is float and isinstance(value, int):
            value = float(value)
        if (
            typ is not None
            and not isinstance(value, typ)
            or (typ is int and isinstance(value, bool))
        ):
            want = typ.__name__ if isinstance(typ, type) else str(typ)
            raise self._err(
                f"`{key}` must be {want}, got {type(value).__name__} ({value!r})"
            )
        return value

    def str_list(self, key: str) -> list[str]:
        value = self.get(key, list, [])
        for item in value:
            if not isinstance(item, str):
                raise self._err(f"`{key}` entries must be strings, got {item!r}")
        return list(value)

    def sub(self, key: str, *, required: bool = False) -> "_Reader | None":
        value = self.get(key, dict, None, required=required)
        if value is None:
            return None
        return _Reader(value, f"{self.where}.{key}", self.path)

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.seen)
        if unknown:
            raise self._err(
                "unknown key(s) "
                + ", ".join(f"`{k}`" for k in unknown)
                + " (release.yaml rejects unknown keys so a misspelt protection setting cannot ship a file unprotected)"
            )


def _path(base: Path, value: str) -> Path:
    return (base / os.path.expandvars(os.path.expanduser(value))).resolve()


def _protection(r: _Reader | None, default: Protection) -> Protection:
    if r is None:
        return default
    p = Protection(
        obfuscate=r.get("obfuscate", bool, default.obfuscate),
        encrypt=r.get("encrypt", bool, default.encrypt),
        strip_comments=r.get("strip-comments", bool, default.strip_comments),
    )
    r.done()
    return p


def _file_rules(r: _Reader, key: str) -> list[FileRule]:
    rules = []
    for idx, item in enumerate(r.get(key, list, [])):
        fr = _Reader(item, f"{r.where}.{key}[{idx}]", r.path)
        rule = FileRule(
            match=fr.get("match", str, required=True),
            reason=fr.get("reason", str, required=True).strip(),
            obfuscate=fr.get("obfuscate", bool, None),
            encrypt=fr.get("encrypt", bool, None),
            strip_comments=fr.get("strip-comments", bool, None),
            dir=fr.get("dir", str, None),
        )
        fr.done()
        if rule.dir is not None and not _DIR_RE.match(rule.dir):
            raise fr._err(
                f"`dir` {rule.dir!r} must be a relative package directory "
                "(letters, digits, '_', '.', '-', separated by '/')"
            )
        if not rule.reason:
            raise fr._err(
                "`reason` may not be empty: every exception to the default protection is reviewed"
            )
        if (
            rule.obfuscate is None
            and rule.encrypt is None
            and rule.strip_comments is None
            and rule.dir is None
        ):
            raise fr._err(
                "sets nothing; give at least one of `obfuscate`, `encrypt`, `strip-comments`, `dir`"
            )
        rules.append(rule)
    return rules


def load_release_config(path: str | os.PathLike) -> ReleaseConfig:
    path = Path(path).resolve()
    if not path.is_file():
        raise FatalRtlBuddyError(f"release config not found: {path}")
    with open(path) as f:
        try:
            raw = load_yaml(f, path)
        except yaml.YAMLError as exc:
            raise FatalRtlBuddyError(f"{path}: not valid YAML: {exc}") from exc
    base = path.parent
    top = _Reader(raw, "release.yaml", path)
    filetype = top.get("rtl-buddy-filetype", str, required=True)
    if filetype != FILETYPE:
        raise top._err(f"`rtl-buddy-filetype` must be `{FILETYPE}`, got `{filetype}`")
    name = top.get("name", str, required=True)
    version = str(top.get("version", (str, int, float), required=True))
    for label, value in (("name", name), ("version", version)):
        if not _NAME_RE.match(value):
            raise top._err(
                f"`{label}` {value!r} may hold only letters, digits, '_', '.' and '-' "
                "(it names the tarball and the map file)"
            )
    metadata = top.get("metadata", dict, {})

    d = top.sub("design", required=True)
    assert d is not None
    preserve = d.sub("preserve")
    externals = []
    for idx, item in enumerate(d.get("externals", list, [])):
        er = _Reader(item, f"design.externals[{idx}]", path)
        externals.append(
            External(
                path=os.path.normpath(
                    os.path.join(
                        base, os.path.expandvars(er.get("path", str, required=True))
                    )
                ),
                ship_as=er.get("ship-as", str, required=True),
                dir=er.get("dir", str, None),
            )
        )
        er.done()
        if "$" in externals[-1].path:
            raise er._err(
                f"`path` {externals[-1].path!r} names an unset environment variable"
            )
    constraints = []
    for idx, item in enumerate(d.get("constraints", list, [])):
        cr = _Reader(item, f"design.constraints[{idx}]", path)
        src = _path(base, cr.get("src", str, required=True))
        constraints.append(
            Constraint(
                src=src,
                scope=cr.get("scope", str, required=True),
                ship_as=cr.get("ship-as", str, src.name),
                mode=cr.get("mode", str, "rewrite"),
            )
        )
        cr.done()
        if constraints[-1].mode not in ("rewrite", "verbatim"):
            raise cr._err("`mode` must be `rewrite` or `verbatim`")
    design = DesignSection(
        model=d.get("model", str, required=True),
        model_config=_path(base, d.get("model-config", str, required=True)),
        top=d.get("top", str, None),
        protect=_protection(
            d.sub("protect"),
            Protection(obfuscate=True, encrypt=True, strip_comments=True),
        ),
        preserve_interfaces=preserve.str_list("interfaces") if preserve else [],
        preserve_identifiers=preserve.str_list("identifiers") if preserve else [],
        files=_file_rules(d, "files"),
        externals=externals,
        constraints=constraints,
    )
    if preserve:
        preserve.done()
    d.done()

    testbench = None
    t = top.sub("testbench")
    if t is not None:
        testbench = TestbenchSection(
            filelist=t.str_list("filelist"),
            extra_files=[_path(base, p) for p in t.str_list("extra-files")],
            protect=_protection(
                t.sub("protect"),
                Protection(obfuscate=False, encrypt=False, strip_comments=False),
            ),
            files=_file_rules(t, "files"),
            allow_design_refs=t.str_list("allow-design-refs"),
        )
        t.done()
        if testbench.protect.obfuscate or any(r.obfuscate for r in testbench.files):
            raise t._err(
                "testbench obfuscation is not supported yet; a testbench may be "
                "encrypted but not obfuscated"
            )

    verify = None
    v = top.sub("verify")
    if v is not None:
        stages = v.str_list("stages") or list(VERIFY_STAGES)
        bad = [s for s in stages if s not in VERIFY_STAGES]
        if bad:
            raise v._err(f"unknown stage(s) {bad}; choose from {list(VERIFY_STAGES)}")
        verify = VerifySection(
            command=v.get("command", str, required=True),
            passed=v.get("pass", str, required=True),
            compare=v.get("compare", str, None),
            stages=stages,
            timeout=v.get("timeout", int, 3600),
        )
        for label, pattern in (("pass", verify.passed), ("compare", verify.compare)):
            if pattern is not None:
                try:
                    re.compile(pattern, re.M)
                except re.error as exc:
                    raise v._err(f"`{label}` is not a valid regex: {exc}") from exc
        v.done()

    e = top.sub("encryption", required=True)
    assert e is not None
    encryption = EncryptionSection(
        key_file=_path(base, e.get("key-file", str, required=True)),
        vcs=e.get("vcs", str, "vcs"),
        extra_args=e.str_list("extra-args"),
        jobs=e.get("jobs", int, 4),
    )
    e.done()

    o = top.sub("obfuscation") or _Reader({}, "obfuscation", path)
    obfuscation = ObfuscationSection(
        verible=o.get("verible", str, None),
        map_dir=_path(base, o.get("map-dir", str, "maps")),
        continue_from=o.get("continue-from", str, "previous"),
        keep_comments=o.str_list("keep-comments") or list(DEFAULT_KEEP_COMMENTS),
        token_paste=o.get("token-paste", str, "refuse"),
    )
    if obfuscation.token_paste not in ("refuse", "preserve"):
        raise o._err("`token-paste` must be `refuse` or `preserve`")
    o.done()

    csr = _csr(top.sub("csr"), base, path)

    p = top.sub("package") or _Reader({}, "package", path)
    package = PackageSection(
        notes=p.get("notes", str, "notes/{version}.md"),
        docs=[_path(base, x) for x in p.str_list("docs")],
    )
    p.done()
    top.done()

    return ReleaseConfig(
        path=path,
        name=name,
        version=version,
        metadata=metadata,
        design=design,
        testbench=testbench,
        verify=verify,
        encryption=encryption,
        obfuscation=obfuscation,
        package=package,
        csr=csr,
    )


def _csr(c: _Reader | None, base: Path, path: Path) -> CsrSection | None:
    if c is None:
        return None
    windows = []
    for idx, item in enumerate(c.get("windows", list, [], required=True)):
        wr = _Reader(item, f"csr.windows[{idx}]", path)
        window = CsrWindow(
            name=wr.get("name", str, required=True),
            rdl=_path(base, wr.get("rdl", str, required=True)),
            top=wr.get("top", str, required=True),
            base=wr.get("base", int, required=True),
        )
        wr.done()
        if not _IDENT_RE.match(window.name):
            raise wr._err(f"`name` {window.name!r} must be an identifier")
        if any(w.name == window.name for w in windows):
            raise wr._err(f"window `{window.name}` is named twice")
        windows.append(window)
    if not windows:
        raise c._err("`windows` may not be empty")
    registers = []
    for idx, item in enumerate(c.get("registers", list, [], required=True)):
        rr = _Reader(item, f"csr.registers[{idx}]", path)
        entry = CsrRegister(
            match=rr.get("match", str, required=True),
            obfuscate_fields=rr.str_list("obfuscate-fields"),
        )
        rr.done()
        window = entry.match.split(".", 1)[0]
        if "." not in entry.match or not any(
            fnmatch.fnmatchcase(w.name, window) for w in windows
        ):
            raise rr._err(
                f"`match` {entry.match!r} must start with a window name and a '.' "
                f"(windows: {', '.join(w.name for w in windows)})"
            )
        registers.append(entry)
    if not registers:
        raise c._err("`registers` may not be empty")
    section = CsrSection(
        name=c.get("name", str, None),
        prefix=c.get("prefix", str, None),
        gate=c.get("gate", str, None),
        include_dirs=[_path(base, p) for p in c.str_list("include-dirs")],
        windows=windows,
        registers=registers,
    )
    c.done()
    for label, value in (("name", section.name), ("prefix", section.prefix)):
        if value is not None and not _IDENT_RE.match(value):
            raise c._err(f"`{label}` {value!r} must be an identifier")
    return section


def resolve_protection(
    basename: str, relpath: str, default: Protection, rules: list[FileRule]
) -> tuple[Protection, list[FileRule], str | None]:
    """Apply every matching rule in order; later rules win.

    Returns the protection, the rules applied and the package directory a rule
    chose (None for the section's default).
    """
    p = Protection(default.obfuscate, default.encrypt, default.strip_comments)
    applied = []
    pkg_dir = None
    for rule in rules:
        if not rule.matches(basename, relpath):
            continue
        applied.append(rule)
        if rule.dir is not None:
            pkg_dir = rule.dir
        if rule.obfuscate is not None:
            p.obfuscate = rule.obfuscate
        if rule.encrypt is not None:
            p.encrypt = rule.encrypt
        if rule.strip_comments is not None:
            p.strip_comments = rule.strip_comments
    return p, applied, pkg_dir
