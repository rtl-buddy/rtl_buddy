"""ABC driving cell and output load from the synth platform (`abc -constr`, #787)."""

import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.synth import (
    SynthConfig,
    SynthPlatformConfig,
    SynthPlatformConfigFile,
    SynthToolConfig,
    SynthToolConfigFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import synth_yosys
from rtl_buddy.tools.synth_openroad import OpenRoadSynth
from rtl_buddy.tools.synth_yosys import YosysSynth

from test_split_liberty import _Platform, _ns_lib

BACKENDS = ["yosys", "openroad"]


def _events(caplog, event):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


class _ConstrPlatform(_Platform):
    def __init__(self, libs, cell="", load=None):
        super().__init__(libs)
        self._cell, self._load = cell, load

    def get_abc_driving_cell(self):
        return self._cell

    def get_abc_output_load(self):
        return self._load


class _Root:
    def __init__(self, platform):
        self._platform = platform

    def get_synth_platform_cfg(self, _name):
        return self._platform

    def get_synth_tool_cfg(self, name):
        raise FatalRtlBuddyError(f"tool '{name}' not found")


def _synth(tmp_path, backend, *, cell="", load=None, sdc=None):
    sv = tmp_path / "top.sv"
    if not sv.exists():
        sv.write_text("module top; endmodule\n")
    fl = tmp_path / "synth.f"
    fl.write_text(f"{sv}\n")
    lib = _ns_lib(tmp_path / "cells.lib")
    cfg = SynthConfig(
        name=f"g_{backend}",
        desc="g",
        model=ModelConfig(name="top", filelist=[], path=str(tmp_path / "models.yaml")),
        tool=backend,
        constraints=sdc,
        params=None,
        defines=None,
        platform="p",
        _reglvl=None,
        tool_overrides=None,
        lib_paths=[],
    )
    synth_cls = YosysSynth if backend == "yosys" else OpenRoadSynth
    synth = synth_cls(
        name=f"t/{backend}",
        synth_cfg=cfg,
        tool_cfg=SynthToolConfig(SynthToolConfigFile(name=backend, tool=backend)),
        suite_dir=str(tmp_path),
        root_cfg=_Root(_ConstrPlatform([lib], cell, load)),
    )
    write = synth._write_script if backend == "yosys" else synth._write_yosys_script
    return synth, Path(write(str(fl))).read_text()


def _abc_line(script: str) -> str:
    return next(ln for ln in script.splitlines() if ln.startswith("abc "))


def _options(synth, backend):
    return synth._phys_options(**({"mapped": True} if backend == "yosys" else {}))


# Platform config


def _platform_cfg(text):
    return SynthPlatformConfig(
        from_yaml(SynthPlatformConfigFile, text), lambda _name: _Pdk()
    )


class _Pdk:
    def get_default_corner(self):
        return "tt"

    def get_corner_paths(self, _corner):
        return []

    def get_tech_lef(self):
        return ""

    def get_macro_lef(self):
        return ""

    def get_layer_rc_tcl(self):
        return ""

    def get_dont_use_cells(self):
        return []


def test_platform_keys_default_to_none():
    cfg = _platform_cfg("name: p\npdk: k\n")
    assert cfg.get_abc_driving_cell() == ""
    assert cfg.get_abc_output_load() is None


def test_platform_keys_are_read():
    cfg = _platform_cfg(
        "name: p\npdk: k\nabc-driving-cell: BUFx2_ASAP7_75t_R\nabc-output-load: 1\n"
    )
    assert cfg.get_abc_driving_cell() == "BUFx2_ASAP7_75t_R"
    assert cfg.get_abc_output_load() == 1.0


@pytest.mark.parametrize(
    "text, match",
    [
        ("abc-driving-cell: 'BUF x2'\n", "one Liberty cell name"),
        ("abc-driving-cell: 'BUF*'\n", "one Liberty cell name"),
        ("abc-output-load: -1\n", "non-negative"),
        ("abc-output-load: .nan\n", "non-negative"),
    ],
)
def test_bad_platform_keys_are_config_errors(text, match):
    with pytest.raises(FatalRtlBuddyError, match=match):
        _platform_cfg("name: p\npdk: k\n" + text)


# Script and constraints file


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_keys_write_no_constr(tmp_path, backend):
    synth, script = _synth(tmp_path, backend)
    assert "-constr" not in _abc_line(script)
    assert not (Path(synth.artefact_dir) / "abc.constr").exists()
    assert "abc_constr" not in _options(synth, backend)


@pytest.mark.parametrize("backend", BACKENDS)
def test_driving_cell_and_load_reach_abc(tmp_path, backend):
    sdc = tmp_path / "c.sdc"
    sdc.write_text("create_clock -period 1 clk\n")
    synth, script = _synth(tmp_path, backend, cell="BUFx2", load=1.5, sdc=str(sdc))
    constr = Path(synth.artefact_dir) / "abc.constr"
    line = _abc_line(script)
    assert f" -constr {constr} -D 1000 -script " in line
    assert constr.read_text() == "set_driving_cell BUFx2\nset_load 1.5\n"
    assert _options(synth, backend)["abc_constr"] == {
        "driving_cell": "BUFx2",
        "output_load": 1.5,
    }


@pytest.mark.parametrize("backend", BACKENDS)
def test_either_key_alone_writes_its_line(tmp_path, backend):
    synth, _script = _synth(tmp_path, backend, load=2)
    constr = Path(synth.artefact_dir) / "abc.constr"
    assert constr.read_text() == "set_load 2\n"
    synth, _script = _synth(tmp_path, backend, cell="BUF")
    assert constr.read_text() == "set_driving_cell BUF\n"


@pytest.mark.parametrize("backend", BACKENDS)
def test_removing_the_keys_removes_the_stale_constr(tmp_path, backend):
    synth, _ = _synth(tmp_path, backend, cell="BUF")
    constr = Path(synth.artefact_dir) / "abc.constr"
    assert constr.exists()
    _synth(tmp_path, backend)
    assert not constr.exists()


def test_a_driving_cell_abc_cannot_find_warns(caplog):
    log = (
        'ABC: Setting driving cell to be "NOPE".\n'
        "ABC: Cannot find the default PI driving cell (NOPE) in the library.\n"
    )
    with caplog.at_level(logging.WARNING):
        synth_yosys.warn_abc_driving_cell_missing(
            log, {"driving_cell": "NOPE", "output_load": None}, "s", "synth.log"
        )
        synth_yosys.warn_abc_driving_cell_missing(log, None, "s", "synth.log")
    [rec] = _events(caplog, "synth.abc_driving_cell_missing")
    assert rec.rtl_fields["cell"] == "NOPE"
    assert "abc-driving-cell" in rec.getMessage()


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not installed")
def test_yosys_reads_the_constr_ahead_of_the_script(tmp_path):
    """Yosys inserts `read_constr` before a `+` script, so `buffer` sees the driving cell."""
    lib = tmp_path / "cells.lib"
    lib.write_text(
        'library (c) {\n  time_unit : "1ns" ;\n  capacitive_load_unit (1, ff) ;\n'
        "  cell (BUF) {\n    area : 1 ;\n"
        "    pin (A) { direction : input ; capacitance : 1 ; }\n"
        '    pin (Y) { direction : output ; function : "A" ; }\n  }\n'
        "  cell (INV) {\n    area : 1 ;\n"
        "    pin (A) { direction : input ; capacitance : 1 ; }\n"
        '    pin (Y) { direction : output ; function : "!A" ; }\n  }\n'
        "  cell (NAND) {\n    area : 1 ;\n"
        "    pin (A) { direction : input ; capacitance : 1 ; }\n"
        "    pin (B) { direction : input ; capacitance : 1 ; }\n"
        '    pin (Y) { direction : output ; function : "!(A&B)" ; }\n  }\n}\n'
    )
    constr = tmp_path / "abc.constr"
    constr.write_text("set_driving_cell BUF\nset_load 2\n")
    (tmp_path / "t.v").write_text(
        "module top(input a, b, output y); assign y = a & b; endmodule\n"
    )
    out = subprocess.run(
        [
            "yosys",
            "-p",
            f"read_verilog {tmp_path / 't.v'}; synth -top top; "
            f'abc -liberty {lib} -constr {constr} -script "+strash; map"',
        ],
        cwd=tmp_path,
        env=synth_yosys.yosys_env(str(tmp_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "read_constr" in out.stdout
    assert 'Setting driving cell to be "BUF"' in out.stdout
