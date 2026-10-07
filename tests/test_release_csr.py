"""The `csr:` section of `rb release`: the whitelisted customer register map."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("systemrdl")
pytest.importorskip("peakrdl_systemrdl")
pytest.importorskip("peakrdl_cheader")

from systemrdl import RDLCompiler  # noqa: E402

from rtl_buddy.errors import FatalRtlBuddyError  # noqa: E402
from rtl_buddy.release import csr  # noqa: E402
from rtl_buddy.release.config import (  # noqa: E402
    CsrRegister,
    CsrSection,
    CsrWindow,
    load_release_config,
)

from test_release import _write_cfg  # noqa: E402

RDL = Path(__file__).parent / "fixtures" / "release_project" / "rtl" / "csr"


def _section(registers, windows=None, gate="acme_customer") -> CsrSection:
    return CsrSection(
        name="acme_csr_map",
        prefix="ACME",
        gate=gate,
        include_dirs=[],
        windows=windows
        or [CsrWindow("main", RDL / "acme_csr.rdl", "acme_csr", 0x4000_0000)],
        registers=[CsrRegister(m, list(o)) for m, o in registers],
    )


def _elaborate(path: Path, top: str):
    comp = RDLCompiler()
    comp.compile_file(str(path))
    return comp.elaborate(top).top


def _regs(top) -> dict[str, int]:
    from systemrdl.node import RegNode

    return {
        n.get_path(): n.absolute_address
        for n in top.descendants(unroll=True)
        if isinstance(n, RegNode)
    }


def test_only_gated_whitelisted_registers_ship_at_their_addresses(tmp_path: Path):
    out = csr.generate(_section([("main.*", [])]), "acme", tmp_path)
    top = _elaborate(out.rdl, "acme_csr_map")
    regs = _regs(top)
    assert "acme_csr_map.main.int_tune" not in regs
    assert regs["acme_csr_map.main.irq"] == 0x4000_0008
    assert regs["acme_csr_map.main.chan[1].hi"] == 0x4000_001C
    assert top.find_by_path("main.buf_m").absolute_address == 0x4000_0020
    text = out.rdl.read_text()
    assert "acme_customer" not in text and "acme_domain" not in text
    assert "secret" not in out.c_header.read_text() + out.sv_header.read_text()
    assert out.counts == {"main": 7}


def test_whitelist_narrows_and_dangling_references_are_dropped(tmp_path: Path):
    out = csr.generate(
        _section([("main.ctrl", []), ("main.irq", [])]), "acme", tmp_path
    )
    top = _elaborate(out.rdl, "acme_csr_map")
    assert set(_regs(top)) == {"acme_csr_map.main.ctrl", "acme_csr_map.main.irq"}
    assert top.find_by_path("main.irq.done").get_property("hwclr") is False
    kept = csr.generate(_section([("main.irq*", [])]), "acme", tmp_path / "kept").rdl
    done = _elaborate(kept, "acme_csr_map").find_by_path("main.irq.done")
    assert done.get_property("hwclr").get_path() == "acme_csr_map.main.irq_clr.clr_done"
    assert done.get_property("intr type").name == "posedge"


def test_obfuscated_field_keeps_its_bits_and_loses_its_name(tmp_path: Path):
    out = csr.generate(_section([("main.ctrl", ["dbg_*"])]), "acme", tmp_path)
    ctrl = _elaborate(out.rdl, "acme_csr_map").find_by_path("main.ctrl")
    fields = {f.inst_name: f for f in ctrl.fields()}
    assert set(fields) == {"en", "f4"}
    assert (fields["f4"].lsb, fields["f4"].width) == (4, 4)
    assert fields["f4"].get_property("desc") is None
    assert fields["en"].get_property("desc") == "Enable"
    svh = out.sv_header.read_text()
    assert "`define ACME_MAIN_CTRL 32'h40000000" in svh
    assert "`define ACME_MAIN_CTRL_RESET 32'h00000001" in svh
    assert "`define ACME_MAIN_CTRL_F4_LSB 4" in svh
    assert "dbg" not in svh + out.c_header.read_text() + out.markdown.read_text()


def test_a_description_naming_an_obfuscated_field_is_refused(tmp_path: Path):
    for f in RDL.iterdir():
        text = f.read_text()
        (tmp_path / f.name).write_text(
            text.replace('desc = "Enable"', 'desc = "Enable; see dbg_sel"')
        )
    section = _section([("main.ctrl", ["dbg_*"])])
    section.windows[0].rdl = tmp_path / "acme_csr.rdl"
    with pytest.raises(FatalRtlBuddyError, match="still names obfuscated field"):
        csr.generate(section, "acme", tmp_path / "out")


@pytest.mark.parametrize(
    ("registers", "message"),
    [
        ([("main.int_tune", [])], "only registers without `acme_customer`"),
        ([("main.ctrl", []), ("main.nothing", [])], "matches no register"),
        ([("main.ctrl", ["nope_*"])], "obfuscate-fields `nope_\\*` matches no field"),
    ],
)
def test_a_whitelist_entry_that_ships_nothing_is_refused(
    tmp_path: Path, registers, message
):
    with pytest.raises(FatalRtlBuddyError, match=message):
        csr.generate(_section(registers), "acme", tmp_path)


def test_a_window_shipping_nothing_is_refused(tmp_path: Path):
    windows = [
        CsrWindow("main", RDL / "acme_csr.rdl", "acme_csr", 0x0),
        CsrWindow("alias", RDL / "acme_csr.rdl", "acme_csr", 0x1000),
    ]
    with pytest.raises(FatalRtlBuddyError, match=r"window\(s\) alias ship no register"):
        csr.generate(_section([("main.*", [])], windows), "acme", tmp_path)


def test_alias_windows_ship_their_own_subsets(tmp_path: Path):
    windows = [
        CsrWindow("main", RDL / "acme_csr.rdl", "acme_csr", 0x0),
        CsrWindow("alias", RDL / "acme_csr.rdl", "acme_csr", 0x1000),
    ]
    out = csr.generate(
        _section([("main.*", []), ("alias.irq", [])], windows), "acme", tmp_path
    )
    regs = _regs(_elaborate(out.rdl, "acme_csr_map"))
    assert [r for r in regs if ".alias." in r] == ["acme_csr_map.alias.irq"]
    assert regs["acme_csr_map.alias.irq"] == 0x1008


def test_an_undeclared_gate_is_refused(tmp_path: Path):
    with pytest.raises(FatalRtlBuddyError, match="does not declare the gate"):
        csr.generate(_section([("main.*", [])], gate="nope"), "acme", tmp_path)


def test_sources_follow_includes():
    assert {p.name for p in csr.sources(_section([("main.*", [])]))} == {
        "acme_csr.rdl",
        "acme_udp.rdl",
    }


def test_config_requires_a_window_prefix(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        {
            "csr": {
                "windows": [{"name": "main", "rdl": "a.rdl", "top": "a", "base": 0}],
                "registers": [{"match": "ctrl"}],
            }
        },
    )
    with pytest.raises(FatalRtlBuddyError, match="must start with a window name"):
        load_release_config(p)


def test_config_rejects_an_unknown_csr_key(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        {
            "csr": {
                "windows": [{"name": "main", "rdl": "a.rdl", "top": "a", "base": 0}],
                "registers": [{"match": "main.ctrl", "obfuscate_fields": ["x"]}],
            }
        },
    )
    with pytest.raises(FatalRtlBuddyError, match="unknown key"):
        load_release_config(p)
