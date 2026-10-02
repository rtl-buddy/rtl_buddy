"""Standard-cell libraries split across several Liberty files in one PDK corner."""

import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.pdk import PdkConfig, PdkConfigFile
from rtl_buddy.config.synth import SynthConfig, SynthToolConfig, SynthToolConfigFile
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.process_utils import ManagedProcessResult
from rtl_buddy.tools import openroad_corners, pnr_abstract, synth_openroad, synth_yosys
from rtl_buddy.tools.synth_openroad import OpenRoadSynth
from rtl_buddy.tools.synth_yosys import YosysSynth, yosys_env

from test_multi_corner import _platform
from test_pnr import _render_flow
from test_power import _FakePlatform, _make_power_backend

_ABC_SCRIPT = (
    "strash; &get -n; &fraig -x; &put; scorr; dretime; strash; "
    "&get -n; &dch -f; &nf {D}; &put"
)

#: The scripts a one-file platform corner with no `lib-paths` generated before corners took lists.
_GOLDEN = {
    "yosys": f"""\
read_liberty -lib <T>/cells.lib
read_verilog -sv -defer <T>/top.sv
synth -top top
chformal -remove
dfflibmap -dont_use FILL* -liberty <T>/cells.lib
abc -liberty <T>/cells.lib -dont_use FILL* -D 2500 -script "+{_ABC_SCRIPT}; stime -p"
write_verilog <T>/artefacts/g_yosys/synth_netlist.v
stat -liberty <T>/cells.lib
tee -q -o <T>/artefacts/g_yosys/synth_stat.json stat -json -liberty <T>/cells.lib
""",
    "openroad": f"""\
read_liberty -lib <T>/cells.lib
read_verilog -sv -defer <T>/top.sv
synth -top top
chformal -remove
dfflibmap -dont_use FILL* -liberty <T>/cells.lib
abc -liberty <T>/cells.lib -dont_use FILL* -script "+{_ABC_SCRIPT}"
write_verilog <T>/artefacts/g_openroad/synth_netlist.v
stat -liberty <T>/cells.lib
tee -q -o <T>/artefacts/g_openroad/synth_stat.json stat -json -liberty <T>/cells.lib
""",
}


class _Platform:
    def __init__(self, libs, dont_use=()):
        self._libs = list(libs)
        self._dont_use = list(dont_use)

    def get_paths(self):
        return list(self._libs)

    def get_lef_paths(self):
        return []

    def get_dont_use_cells(self):
        return list(self._dont_use)


class _Root:
    def __init__(self, libs, dont_use=()):
        self._platform = _Platform(libs, dont_use)

    def get_synth_platform_cfg(self, _name):
        return self._platform

    def get_synth_tool_cfg(self, name):
        raise FatalRtlBuddyError(f"tool '{name}' not found")


def _backend(tmp_path, backend, *, libs, platform="p", lib_paths=(), dont_use=()):
    sv = tmp_path / "top.sv"
    if not sv.exists():
        sv.write_text("module top; endmodule\n")
    fl = tmp_path / "synth.f"
    fl.write_text(f"{sv}\n")
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 2.5 [get_ports clk]\n")
    cfg = SynthConfig(
        name=f"g_{backend}",
        desc="g",
        model=ModelConfig(name="top", filelist=[], path=str(tmp_path / "models.yaml")),
        tool=backend,
        constraints=str(sdc),
        params=None,
        defines=None,
        platform=platform,
        _reglvl=None,
        tool_overrides=None,
        lib_paths=list(lib_paths),
    )
    kwargs = dict(
        name=f"t/{backend}",
        synth_cfg=cfg,
        tool_cfg=SynthToolConfig(SynthToolConfigFile(name=backend, tool=backend)),
        suite_dir=str(tmp_path),
        root_cfg=_Root(libs, dont_use),
    )
    if backend == "yosys":
        synth = YosysSynth(**kwargs)
        return synth, Path(synth._write_script(str(fl))).read_text()
    synth = OpenRoadSynth(**kwargs)
    return synth, Path(synth._write_yosys_script(str(fl))).read_text()


def _ns_lib(path: Path) -> str:
    path.write_text('library (c) {\n  time_unit : "1ns" ;\n}\n')
    return str(path)


# Synthesis scripts


@pytest.mark.parametrize("backend", ["yosys", "openroad"])
def test_a_one_file_corner_without_lib_paths_writes_the_same_script(tmp_path, backend):
    lib = _ns_lib(tmp_path / "cells.lib")
    _synth, script = _backend(tmp_path, backend, libs=[lib], dont_use=["FILL*"])
    assert script.replace(str(tmp_path), "<T>") == _GOLDEN[backend]


@pytest.mark.parametrize("backend", ["yosys", "openroad"])
def test_every_corner_file_maps_and_macros_are_only_read(tmp_path, backend):
    a = _ns_lib(tmp_path / "invbuf.lib")
    b = _ns_lib(tmp_path / "simple.lib")
    macro = _ns_lib(tmp_path / "sram.lib")
    _synth, script = _backend(tmp_path, backend, libs=[a, b], lib_paths=[macro])
    lines = script.splitlines()
    both = f"-liberty {a} -liberty {b}"
    assert lines[:3] == [
        f"read_liberty -lib {a}",
        f"read_liberty -lib {b}",
        f"read_liberty -lib {macro}",
    ]
    assert f"dfflibmap {both}" in lines
    assert any(line.startswith(f"abc {both} ") for line in lines)
    assert f"stat {both}" in lines
    assert lines[-1].endswith(f"stat -json {both}")
    assert not [line for line in lines[3:] if macro in line]


def test_without_a_platform_every_lib_path_is_a_cell_library(tmp_path):
    a = _ns_lib(tmp_path / "a.lib")
    b = _ns_lib(tmp_path / "b.lib")
    _synth, script = _backend(
        tmp_path, "yosys", libs=[], platform=None, lib_paths=[a, b]
    )
    assert f"dfflibmap -liberty {a} -liberty {b}" in script.splitlines()


def test_the_libs_fingerprint_lists_every_corner_file(tmp_path):
    a = _ns_lib(tmp_path / "a.lib")
    b = _ns_lib(tmp_path / "b.lib")
    synth, _script = _backend(tmp_path, "yosys", libs=[a, b])
    assert synth._phys_options(mapped=True)["libs"] == [a, b]


# PDK configuration


def _pdk_yaml(tmp_path, corners: str) -> PdkConfig:
    cfg = from_yaml(PdkConfigFile, f"name: p\ncorners:\n{corners}")
    return PdkConfig(cfg, str(tmp_path / "root_config.yaml"))


def test_a_corner_takes_a_string_or_a_list(tmp_path):
    pdk = _pdk_yaml(
        tmp_path, "  tt: lib/tt.lib\n  ss: [lib/ss_a.lib, lib/ss_b.lib.gz]\n"
    )
    assert pdk.get_corner_paths("tt") == [str(tmp_path / "lib/tt.lib")]
    assert pdk.get_corner_paths("ss") == [
        str(tmp_path / "lib/ss_a.lib"),
        str(tmp_path / "lib/ss_b.lib.gz"),
    ]


def test_a_corner_that_names_no_file_is_an_error(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match="corner 'tt' names no Liberty"):
        _pdk_yaml(tmp_path, "  tt: []\n")


# P&R and power


def _split_pdk(tmp_path):
    return PdkConfig(
        PdkConfigFile(
            name="p",
            site="s",
            corners={"tt": ["lib/tt_a.lib", "lib/tt_b.lib"], "ss": "lib/ss.lib"},
            tech_lef="lef/tech.lef",
            macro_lef="lef/cells.lef",
        ),
        str(tmp_path / "root_config.yaml"),
    )


def test_pnr_reads_every_file_of_a_one_corner_platform(tmp_path):
    text = _render_flow(tmp_path, _platform(_split_pdk(tmp_path), sta_corner="tt"))
    lines = text.splitlines()
    first = lines.index("read_liberty $LIBERTY")
    assert lines[first + 1] == f"read_liberty {tmp_path / 'lib/tt_b.lib'}"
    assert f"set LIBERTY         {tmp_path / 'lib/tt_a.lib'}" in lines


def test_pnr_reads_every_file_into_each_corner(tmp_path):
    platform = _platform(_split_pdk(tmp_path), sta_corners=["tt", "ss"])
    lines = openroad_corners.liberty_tcl(platform.get_sta_corner_lib_paths(), [])
    assert lines == [
        "define_corners tt ss",
        f"read_liberty -corner tt {tmp_path / 'lib/tt_a.lib'}",
        f"read_liberty -corner tt {tmp_path / 'lib/tt_b.lib'}",
        f"read_liberty -corner ss {tmp_path / 'lib/ss.lib'}",
    ]


class _SplitPowerPlatform(_FakePlatform):
    def get_sta_lib_paths(self):
        return ["/pdk/fake/a.lib", "/pdk/fake/b.lib"]


def test_power_reads_every_corner_file(tmp_path):
    backend = _make_power_backend(tmp_path, platform=_SplitPowerPlatform())
    lines = Path(backend._write_script()).read_text().splitlines()
    assert lines[1:3] == [
        "read_liberty /pdk/fake/a.lib",
        "read_liberty /pdk/fake/b.lib",
    ]
    assert backend._phys_technology()[:2] == ["/pdk/fake/a.lib", "/pdk/fake/b.lib"]


def test_an_abstract_records_a_one_file_corner_as_before():
    assert pnr_abstract.one_or_many(["/a.lib"]) == "/a.lib"
    assert pnr_abstract.one_or_many(["/a.lib", "/b.lib"]) == ["/a.lib", "/b.lib"]


def test_a_split_corner_must_match_the_abstract_file_for_file(tmp_path):
    a = _ns_lib(tmp_path / "a.lib")
    b = tmp_path / "b.lib"
    b.write_text("library (b) {}\n")
    records = [pnr_abstract.file_fingerprint(p, None) for p in (a, str(b))]
    block = SimpleNamespace(
        manifest={"technology": {"liberty": records}},
        ref=SimpleNamespace(name="blk", pnr_run="r", pnr_suite_path="pnr.yaml"),
    )
    pnr_abstract.check_technology(block, liberty=[a, str(b)], tech_lef=None)
    for wrong in ([a], [str(b), a]):
        with pytest.raises(pnr_abstract.BlockResolutionError, match="mismatch"):
            pnr_abstract.check_technology(block, liberty=wrong, tech_lef=None)


# Real Yosys and ABC on a split library

_TEMPLATE = (
    "  lu_table_template (tmpl) {\n"
    "    variable_1 : input_net_transition ;\n"
    "    variable_2 : total_output_net_capacitance ;\n"
    '    index_1 ("1.0, 10.0") ;\n'
    '    index_2 ("1.0, 10.0") ;\n'
    "  }\n"
)


def _cell(name, area, inputs, function):
    out = [f"  cell ({name}) {{\n    area : {area} ;\n"]
    out += [
        f"    pin ({p}) {{ direction : input ; capacitance : 1.0 ; }}\n" for p in inputs
    ]
    out.append(f'    pin (Y) {{ direction : output ; function : "{function}" ;\n')
    for pin in inputs:
        out.append(f'      timing () {{ related_pin : "{pin}" ;\n')
        for group in ("cell_rise", "cell_fall", "rise_transition", "fall_transition"):
            out.append(
                f'        {group} (tmpl) {{ values ("10.0, 12.0", "11.0, 13.0") ; }}\n'
            )
        out.append("      }\n")
    out.append("    }\n  }\n")
    return "".join(out)


def _library(name, cells):
    return (
        f'library ({name}) {{\n  delay_model : table_lookup ;\n  time_unit : "1ps" ;\n'
        "  capacitive_load_unit (1,ff) ;\n" + _TEMPLATE + "".join(cells) + "}\n"
    )


def _split_libraries(tmp_path) -> list[str]:
    """INVBUF, AND/OR and SEQ files; ABC needs three cell classes in each combinational file."""
    invbuf = tmp_path / "invbuf.lib"
    invbuf.write_text(
        _library(
            "invbuf",
            [
                _cell("INVx1", 1.0, ["A"], "!A"),
                _cell("BUFx1", 1.5, ["A"], "A"),
                _cell("TIEHIx1", 0.5, [], "1"),
                _cell("TIELOx1", 0.5, [], "0"),
            ],
        )
    )
    simple = tmp_path / "simple.lib"
    simple.write_text(
        _library(
            "simple",
            [
                _cell("AND2x1", 3.0, ["A", "B"], "A&B"),
                _cell("OR2x1", 3.5, ["A", "B"], "A|B"),
                _cell("AND3x1", 4.0, ["A", "B", "C"], "A&B&C"),
            ],
        )
    )
    seq = tmp_path / "seq.lib"
    seq.write_text(
        _library("seq", [])[:-2] + "  cell (DFFx1) {\n    area : 5.0 ;\n"
        '    ff (IQ, IQN) { next_state : "D" ; clocked_on : "CLK" ; }\n'
        "    pin (CLK) { direction : input ; clock : true ; capacitance : 1.0 ; }\n"
        "    pin (D) { direction : input ; capacitance : 1.0 ; }\n"
        '    pin (Q) { direction : output ; function : "IQ" ; }\n'
        "  }\n}\n"
    )
    return [str(invbuf), str(simple), str(seq)]


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not installed")
@pytest.mark.parametrize("backend", ["yosys", "openroad"])
def test_yosys_maps_to_cells_of_every_file_and_reports_their_area(tmp_path, backend):
    (tmp_path / "top.sv").write_text(
        "module top (input clk, input [3:0] a, b, output reg [3:0] q);\n"
        "  always @(posedge clk) q <= (a & b) | ~a;\nendmodule\n"
    )
    synth, script = _backend(tmp_path, backend, libs=_split_libraries(tmp_path))
    log = Path(synth.artefact_dir) / "real.log"
    subprocess.run(
        ["yosys", "-q", "-l", str(log), "-s", _script_path(synth)],
        cwd=synth.artefact_dir,
        check=True,
        capture_output=True,
    )
    stat = log.read_text().split("Printing statistics")[-1]
    area = {
        cell: float(a)
        for a, cell in re.findall(r"^\s+\d+\s+([\d.]+)\s+(\w+x1)\s*$", stat, re.M)
    }
    assert "DFFx1" in area
    assert {"INVx1", "BUFx1"} & set(area)
    assert {"AND2x1", "OR2x1", "AND3x1"} & set(area)
    assert all(value > 0 for value in area.values())


def _script_path(synth):
    if isinstance(synth, YosysSynth):
        return synth._script_path()
    return synth._yosys_script_path()


# Yosys temp directory


@pytest.mark.parametrize("backend", ["yosys", "openroad"])
def test_yosys_runs_with_tmpdir_in_its_artefact_dir(tmp_path, monkeypatch, backend):
    lib = _ns_lib(tmp_path / "cells.lib")
    synth, _script = _backend(tmp_path, backend, libs=[lib])
    seen = []

    def _fake(cmd, stdout, **kwargs):
        seen.append(kwargs)
        return ManagedProcessResult(returncode=1)

    if backend == "yosys":
        monkeypatch.setattr(synth_yosys, "run_managed_process", _fake)
        monkeypatch.setattr(synth, "_write_filelist", lambda: str(tmp_path / "synth.f"))
        synth.run()
    else:
        monkeypatch.setattr(synth_openroad.subprocess, "run", _fake)
        synth._run_yosys_stage(str(tmp_path / "synth.f"))
    tmp_dir = Path(synth.artefact_dir) / "yosys-tmp"
    assert [kw["env"]["TMPDIR"] for kw in seen] == [str(tmp_dir)]
    assert tmp_dir.is_dir()


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not installed")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_foreign_scl_cache_in_the_inherited_tmpdir_is_not_used(tmp_path, monkeypatch):
    (tmp_path / "top.sv").write_text(
        "module top (input clk, input [3:0] a, b, output reg [3:0] q);\n"
        "  always @(posedge clk) q <= (a & b) | ~a;\nendmodule\n"
    )
    synth, _script = _backend(tmp_path, "yosys", libs=_split_libraries(tmp_path))
    shared = tmp_path / "shared-tmp"
    foreign = shared / "yosys-liberty-scl-cache"
    foreign.mkdir(parents=True)
    foreign.chmod(0o555)
    monkeypatch.setenv("TMPDIR", str(shared))

    def _log(env):
        return subprocess.run(
            ["yosys", "-s", _script_path(synth)],
            cwd=synth.artefact_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    try:
        inherited = _log(dict(os.environ))
        if "scl" not in inherited.lower():
            pytest.skip("this yosys has no merged SCL cache")
        assert "falling back to liberty format" in inherited
        own = _log(yosys_env(synth.artefact_dir))
    finally:
        foreign.chmod(0o755)
    assert "falling back to liberty format" not in own
    assert "yosys-tmp/yosys-liberty-scl-cache" in own


def test_yosys_tmpdir_is_absolute_for_a_relative_artefact_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = synth_yosys.yosys_env("run")
    assert env["TMPDIR"] == str(tmp_path / "run" / synth_yosys.YOSYS_TMP_DIRNAME)
    assert (tmp_path / "run" / synth_yosys.YOSYS_TMP_DIRNAME).is_dir()
