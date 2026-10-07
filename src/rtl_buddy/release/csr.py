"""The customer register map: the whitelisted registers of a release's SystemRDL windows.

Each window is one SystemRDL address map at an absolute base. A register ships
when it is eligible (the ``gate`` property is true on it or an enclosing regfile
or memory, or no gate is configured) and a ``csr.registers`` entry matches it or
an enclosing regfile or memory. Everything else is removed. The surviving tree
ships as one SystemRDL file instancing every window at its base, a C header
(PeakRDL cheader), a SystemVerilog header of absolute addresses and a Markdown
register map. Shipped RDL keeps explicit offsets, so removing a register never
moves another.

User-defined properties are internal annotations and never ship. A field named
by ``obfuscate-fields`` keeps its position, width, access and reset but ships as
``f<lsb>`` with no name, description or encoding.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import FatalRtlBuddyError
from ..logging_utils import log_event
from .config import CsrSection

logger = logging.getLogger(__name__)

#: peakrdl-systemrdl writes the compiler's ``intr type`` pseudo-property as an assignment; SystemRDL spells it as a modifier.
_INTR_TYPE = re.compile(r"^(\s*)intr type = (\w+);$", re.M)


def _import():
    try:
        from peakrdl_cheader.exporter import CHeaderExporter
        from peakrdl_systemrdl import SystemRDLExporter
        from peakrdl_systemrdl.identifier_filter import kw_filter
        from systemrdl import RDLCompileError, RDLCompiler
    except ImportError as exc:
        raise FatalRtlBuddyError(
            "release.yaml has a `csr:` section, which needs SystemRDL support: "
            "uv add 'rtl_buddy[release-csr]'"
        ) from exc
    return RDLCompiler, RDLCompileError, SystemRDLExporter, CHeaderExporter, kw_filter


@dataclass
class CsrOutputs:
    rdl: Path
    c_header: Path
    sv_header: Path
    markdown: Path
    #: Shipped registers per window, for the log and manifest.
    counts: dict[str, int]

    def files(self) -> list[Path]:
        return [self.rdl, self.c_header, self.sv_header, self.markdown]


def _compile(window, incdirs: list[Path]):
    RDLCompiler, RDLCompileError, *_ = _import()
    comp = RDLCompiler()
    try:
        comp.compile_file(str(window.rdl), incl_search_paths=[str(p) for p in incdirs])
        root = comp.elaborate(window.top)
    except RDLCompileError as exc:
        raise FatalRtlBuddyError(
            f"csr window {window.name}: {window.rdl} does not compile: {exc}"
        ) from exc
    return comp, root.top


def _is_unit(node) -> bool:
    from systemrdl.node import MemNode, RegfileNode, RegNode

    return isinstance(node, (RegNode, RegfileNode, MemNode))


def _path(node, top) -> str:
    return node.get_rel_path(top, array_suffix="", empty_array_suffix="")


@dataclass
class _Tally:
    """What one ``csr.registers`` entry selected, across windows."""

    shipped: int = 0
    internal: list[str] = field(default_factory=list)
    #: ``obfuscate-fields`` patterns that renamed at least one field.
    used: set[str] = field(default_factory=set)


def _select(
    csr: CsrSection, window, comp, top, tally: dict[int, _Tally]
) -> tuple[set[int], dict[int, list]]:
    """Ids of the components that ship, and the entries obfuscating fields per register."""
    from systemrdl.node import AddrmapNode, MemNode, RegfileNode, RegNode

    if csr.gate and csr.gate not in comp.env.property_rules.user_properties:
        raise FatalRtlBuddyError(
            f"csr window {window.name}: {window.rdl} does not declare the gate "
            f"property `{csr.gate}`"
        )

    def eligible(node) -> bool:
        if not csr.gate:
            return True
        while _is_unit(node):
            if node.get_property(csr.gate, default=False):
                return True
            node = node.parent
        return False

    units = [n for n in top.descendants() if _is_unit(n)]
    keep: set[int] = set()
    obf: dict[int, list] = {}
    for e in csr.registers:
        matched = [
            u
            for u in units
            if fnmatch.fnmatchcase(f"{window.name}.{_path(u, top)}", e.match)
        ]
        regs = []
        for unit in matched:
            leaves = (
                [unit]
                if isinstance(unit, (RegNode, MemNode))
                else [
                    n for n in unit.descendants() if isinstance(n, (RegNode, MemNode))
                ]
            )
            regs += [r for r in leaves if eligible(r)]
        t = tally[id(e)]
        t.shipped += len(regs)
        if matched and not regs:
            t.internal += [f"{window.name}.{_path(u, top)}" for u in matched]
        for r in regs:
            keep.add(id(r.inst))
            node = r.parent
            while isinstance(node, (RegfileNode, AddrmapNode)) and node is not top:
                keep.add(id(node.inst))
                node = node.parent
            if e.obfuscate_fields and isinstance(r, RegNode):
                obf.setdefault(id(r.inst), []).append(e)
    return keep, obf


def _prune(top, keep: set[int], udps: set[str]) -> None:
    """Remove every register, regfile, memory and signal not kept; strip user-defined properties."""
    from systemrdl.component import Addrmap, Field, Mem, Reg, Regfile

    def walk(inst) -> None:
        for name in udps:
            inst.properties.pop(name, None)
        kids = []
        for child in inst.children:
            if isinstance(inst, Mem):
                pass
            elif isinstance(child, (Reg, Regfile, Mem, Addrmap)):
                if id(child) not in keep:
                    continue
            elif not isinstance(child, Field):
                continue
            kids.append(child)
            walk(child)
        inst.children = kids

    walk(top.inst)


def _refs(top) -> list[tuple]:
    """Every reference-valued property as ``(node, key, target node, target property)``.

    Resolved before pruning, while every target still exists.
    """
    from systemrdl.node import Node
    from systemrdl.rdltypes import ComponentRef, PropertyReference

    refs = []
    for node in [top, *top.descendants()]:
        for key, value in node.inst.properties.items():
            if not isinstance(value, (ComponentRef, PropertyReference)):
                continue
            target = node.get_property(key)
            if isinstance(target, PropertyReference):
                refs.append((node, key, target.node, target.name))
            elif isinstance(target, Node):
                refs.append((node, key, target, None))
    return refs


def _detach_refs(top, refs: list[tuple]) -> tuple[list[tuple], list[str]]:
    """Remove every reference from the tree (peakrdl-systemrdl cannot write one).

    Returns the dynamic assignments that restore those whose both ends ship and
    lie outside arrays, and the dropped ones.
    """
    alive = {id(n.inst) for n in top.descendants()}
    restore, dropped = [], []
    for node, key, target, prop in refs:
        node.inst.properties.pop(key, None)
        if id(node.inst) not in alive:
            continue
        lhs, rhs = _rel(node, top), _rel(target, top)
        if id(target.inst) not in alive or lhs is None or rhs is None:
            dropped.append(f"{node.get_path()}->{key}")
            continue
        restore.append((node, key, target, prop))
    return restore, dropped


def _rel(node, top) -> str | None:
    """Path of ``node`` below ``top``; None inside an array, where one path names every element."""
    parts = []
    while node is not top:
        if getattr(node.inst, "is_array", False):
            return None
        parts.append(node.inst_name)
        node = node.parent
    return ".".join(reversed(parts))


def _assignments(top, restore: list[tuple]) -> str:
    lines = []
    for node, key, target, prop in restore:
        rhs = _rel(target, top) + (f"->{prop}" if prop else "")
        lines.append(f"    {_rel(node, top)}->{key} = {rhs};")
    return "\n".join(lines)


def _obfuscate_fields(top, obf: dict[int, list], tally: dict[int, _Tally]) -> int:
    from systemrdl.node import RegNode

    count = 0
    for reg in top.descendants():
        if not isinstance(reg, RegNode) or id(reg.inst) not in obf:
            continue
        names = {f.inst_name for f in reg.fields()}
        hidden = []
        for f in reg.fields():
            hits = [
                (e, p)
                for e in obf[id(reg.inst)]
                for p in e.obfuscate_fields
                if fnmatch.fnmatchcase(f.inst_name, p)
            ]
            if not hits:
                continue
            for e, p in hits:
                tally[id(e)].used.add(p)
            opaque = f"f{f.lsb}"
            if opaque in names - {f.inst_name}:
                raise FatalRtlBuddyError(
                    f"csr register {reg.get_path()}: obfuscating field "
                    f"{f.inst_name} would collide with field {opaque}"
                )
            hidden.append(f.inst_name)
            f.inst.inst_name = opaque
            for key in ("name", "desc", "encode"):
                f.inst.properties.pop(key, None)
            count += 1
        if hidden:
            text = " ".join(
                str(n.inst.properties.get(k, ""))
                for n in (reg, *reg.fields())
                for k in ("name", "desc")
            )
            named = [h for h in hidden if re.search(rf"\b{re.escape(h)}\b", text)]
            if named:
                raise FatalRtlBuddyError(
                    f"csr register {reg.get_path()}: its name or description still "
                    f"names obfuscated field(s) {', '.join(named)}"
                )
    return count


_INCLUDE = re.compile(r'^\s*`include\s+"([^"]+)"', re.M)


def sources(csr: CsrSection) -> list[Path]:
    """Every RDL file the windows read: their sources and, transitively, what they `include."""
    seen: list[Path] = []
    todo = [w.rdl for w in csr.windows]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.append(path)
        for name in _INCLUDE.findall(path.read_text()):
            for d in [path.parent, *csr.include_dirs]:
                if (d / name).is_file():
                    todo.append((d / name).resolve())
                    break
    return seen


def generate(csr: CsrSection, release_name: str, out_dir: Path) -> CsrOutputs:
    """Write the customer register map into ``out_dir``."""
    RDLCompiler, RDLCompileError, SystemRDLExporter, CHeaderExporter, kw_filter = (
        _import()
    )
    tally = {id(e): _Tally() for e in csr.registers}
    out_dir.mkdir(parents=True, exist_ok=True)
    name = csr.name or f"{release_name}_csr"
    parts = []
    empty: list[str] = []
    counts: dict[str, int] = {}
    obf_count = 0
    for window in csr.windows:
        comp, top = _compile(window, csr.include_dirs)
        keep, obf = _select(csr, window, comp, top, tally)
        if not keep:
            empty.append(window.name)
            continue
        refs = _refs(top)
        _prune(top, keep, set(comp.env.property_rules.user_properties))
        restore, dropped = _detach_refs(top, refs)
        if dropped:
            log_event(
                logger,
                logging.INFO,
                "release.csr_refs_dropped",
                window=window.name,
                refs=", ".join(dropped[:20]),
            )
        obf_count += _obfuscate_fields(top, obf, tally)
        top.inst.type_name = f"{name}_{window.name}"
        tmp = out_dir / f".{window.name}.rdl"
        SystemRDLExporter().export(top, str(tmp))
        text = _INTR_TYPE.sub(r"\1\2 intr;", tmp.read_text()).rstrip()
        if restore:
            assert text.endswith("};")
            text = text[:-2] + _assignments(top, restore) + "\n};"
        parts.append(text)
        tmp.unlink()
        counts[window.name] = sum(1 for _ in _regs(top))

    problems = []
    for e in csr.registers:
        t = tally[id(e)]
        if not t.shipped and t.internal:
            problems.append(
                f"`{e.match}` matches only registers without `{csr.gate}`: "
                + ", ".join(t.internal[:10])
            )
        elif not t.shipped:
            problems.append(f"`{e.match}` matches no register")
        problems += [
            f"`{e.match}`: obfuscate-fields `{p}` matches no field"
            for p in e.obfuscate_fields
            if t.shipped and p not in t.used
        ]
    if problems:
        raise FatalRtlBuddyError(
            "csr.registers entries that ship nothing:\n  " + "\n  ".join(problems)
        )
    if empty:
        raise FatalRtlBuddyError(
            f"csr window(s) {', '.join(empty)} ship no register; whitelist one in "
            "`csr.registers` or remove the window"
        )

    top_lines = [f"addrmap {name} {{"]
    for window in csr.windows:
        top_lines.append(
            f"    {name}_{window.name} {kw_filter(window.name)} @ 0x{window.base:X};"
        )
    top_lines.append("};")
    rdl = out_dir / f"{name}.rdl"
    rdl.write_text(
        f"// {name}: customer-visible registers, absolute addresses.\n\n"
        + "\n".join(p.rstrip() + "\n" for p in parts)
        + "\n"
        + "\n".join(top_lines)
        + "\n"
    )

    # The shipped file must compile on its own; everything else is generated from it.
    comp = RDLCompiler()
    try:
        comp.compile_file(str(rdl))
        root = comp.elaborate(name)
    except RDLCompileError as exc:
        raise FatalRtlBuddyError(
            f"the generated {rdl.name} does not compile: {exc}"
        ) from exc
    c_header = out_dir / f"{name}.h"
    CHeaderExporter().export(root.top, str(c_header))
    sv_header = out_dir / f"{name}.svh"
    sv_header.write_text(_svh(root.top, name, csr.prefix or name.upper()))
    markdown = out_dir / f"{name}.md"
    markdown.write_text(_markdown(root.top, name))
    log_event(
        logger,
        logging.INFO,
        "release.csr",
        registers=sum(counts.values()),
        obfuscated_fields=obf_count,
        path=str(out_dir),
    )
    return CsrOutputs(rdl, c_header, sv_header, markdown, counts)


def _regs(top):
    from systemrdl.node import RegNode

    return (n for n in top.descendants(unroll=True) if isinstance(n, RegNode))


def _svh(top, name: str, prefix: str) -> str:
    guard = f"{name.upper()}__SVH"
    lines = [
        f"// {name}: customer-visible registers. Absolute byte addresses;",
        "// _RESET is the register reset value.",
        "",
        f"`ifndef {guard}",
        f"`define {guard}",
    ]
    aw = max(32, top.size.bit_length())
    for window in top.children():
        lines += ["", f"// {window.inst_name}: base 'h{window.absolute_address:x}"]
        for reg in _regs(window):
            path = reg.get_rel_path(
                window, hier_separator="_", array_suffix="_{index:d}"
            ).upper()
            macro = f"{prefix}_{window.inst_name.upper()}_{path}"
            reset = 0
            for f in reg.fields():
                value = f.get_property("reset")
                if isinstance(value, int):
                    reset |= value << f.lsb
            width = reg.get_property("regwidth")
            lines.append(f"`define {macro} {aw}'h{reg.absolute_address:0{aw // 4}x}")
            lines.append(f"`define {macro}_RESET {width}'h{reset:0{width // 4}x}")
            for f in reg.fields():
                lines.append(f"`define {macro}_{f.inst_name.upper()}_LSB {f.lsb}")
                lines.append(f"`define {macro}_{f.inst_name.upper()}_WIDTH {f.width}")
    lines += ["", "`endif", ""]
    return "\n".join(lines)


def _markdown(top, name: str) -> str:
    lines = [
        f"# {name} register map",
        "",
        "Registers available to software. Addresses are absolute byte addresses.",
    ]
    for window in top.children():
        lines += [
            "",
            f"## {window.inst_name} (base `0x{window.absolute_address:x}`)",
            "",
            "| Register | Address | Reset | Fields |",
            "|---|---|---|---|",
        ]
        for reg in _regs(window):
            path = reg.get_rel_path(window)
            reset = 0
            fields = []
            for f in reg.fields():
                value = f.get_property("reset")
                if isinstance(value, int):
                    reset |= value << f.lsb
                fields.append(
                    f"`{f.inst_name}`[{f.msb}:{f.lsb}] {f.get_property('sw').name}"
                )
            lines.append(
                f"| `{path}` | `0x{reg.absolute_address:08x}` | `0x{reset:08x}` | "
                + ", ".join(fields)
                + " |"
            )
    return "\n".join(lines) + "\n"
