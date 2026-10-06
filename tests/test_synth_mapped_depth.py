"""Yosys-backed checks of the mapped run: the default ABC script keeps a wide adder log-depth, and `synth_stat.json` parses with a gzipped Liberty."""

import gzip
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.synth import (
    SynthConfig,
    SynthEffortConfig,
    SynthEffortConfigFile,
    SynthEffortYosysFile,
    SynthToolConfig,
    SynthToolConfigFile,
)
from rtl_buddy.tools.synth_openroad import OpenRoadSynth
from rtl_buddy.tools.synth_yosys import YosysSynth

pytestmark = pytest.mark.skipif(
    shutil.which("yosys") is None, reason="yosys not installed"
)

WIDTH = 42

_CELLS = {
    "BUF": (["A"], "A", 1.0),
    "INV": (["A"], "!A", 1.0),
    "NAND2": (["A", "B"], "!(A&B)", 1.5),
    "NOR2": (["A", "B"], "!(A|B)", 1.5),
    "AND2": (["A", "B"], "A&B", 2.0),
    "OR2": (["A", "B"], "A|B", 2.0),
    "XOR2": (["A", "B"], "A^B", 3.0),
    "XNOR2": (["A", "B"], "!(A^B)", 3.0),
    "AOI21": (["A", "B", "C"], "!((A&B)|C)", 2.0),
    "OAI21": (["A", "B", "C"], "!((A|B)&C)", 2.0),
}


def _liberty() -> str:
    """A combinational Liberty whose delays scale with area."""
    out = [
        "library (mini) {\n"
        "  delay_model : table_lookup;\n"
        '  time_unit : "1ns";\n'
        "  capacitive_load_unit (1, pf);\n"
        '  voltage_unit : "1V";\n'
        '  current_unit : "1mA";\n'
        '  pulling_resistance_unit : "1kohm";\n'
        "  lu_table_template (tmpl) {\n"
        "    variable_1 : input_net_transition;\n"
        "    variable_2 : total_output_net_capacitance;\n"
        '    index_1 ("0.01, 0.5");\n'
        '    index_2 ("0.001, 0.1");\n'
        "  }\n"
    ]
    for name, (inputs, function, area) in _CELLS.items():
        d = 0.05 * area
        table = f'values ("{d:.3f}, {2 * d:.3f}", "{1.5 * d:.3f}, {3 * d:.3f}");'
        out.append(f"  cell ({name}) {{\n    area : {area};\n")
        for pin in inputs:
            out.append(
                f"    pin ({pin}) {{ direction : input; capacitance : 0.002; }}\n"
            )
        out.append(
            f'    pin (Y) {{\n      direction : output;\n      function : "{function}";\n'
        )
        for pin in inputs:
            out.append(f'      timing () {{\n        related_pin : "{pin}";\n')
            for group in (
                "cell_rise",
                "cell_fall",
                "rise_transition",
                "fall_transition",
            ):
                out.append(f"        {group} (tmpl) {{ {table} }}\n")
            out.append("      }\n")
        out.append("    }\n  }\n")
    out.append("}\n")
    return "".join(out)


class _Platform:
    def __init__(self, lib):
        self._lib = lib

    def get_paths(self):
        return [self._lib]

    def get_lef_paths(self):
        return []

    def get_dont_use_cells(self):
        return []


class _Root:
    def __init__(self, lib):
        self._lib = lib

    def get_synth_platform_cfg(self, name):
        return _Platform(self._lib)

    def get_synth_tool_cfg(self, name):
        from rtl_buddy.errors import FatalRtlBuddyError

        raise FatalRtlBuddyError(f"tool '{name}' not found")


def _effort(abc_script=""):
    # `synth` runs Yosys' generic `abc`, whose default script has `dc2`, unless given -noabc.
    return SynthEffortConfig(
        SynthEffortConfigFile(
            name="depth",
            yosys=SynthEffortYosysFile(synth_args="-noabc", abc_script=abc_script),
        )
    )


def _write_script(tmp_path, backend, abc_script, gzipped=False):
    sv = tmp_path / "add.sv"
    sv.write_text(
        f"module add (input logic [{WIDTH - 1}:0] a, b, output logic [{WIDTH}:0] z);\n"
        "  assign z = a + b;\nendmodule\n"
    )
    fl = tmp_path / "synth.f"
    fl.write_text(f"{sv}\n")
    lib = tmp_path / "mini.lib"
    lib.write_text(_liberty())
    if gzipped:
        lib = tmp_path / "mini.lib.gz"
        lib.write_bytes(gzip.compress(_liberty().encode()))
    synth_cfg = SynthConfig(
        name=f"add_{backend}",
        desc="adder depth",
        model=ModelConfig(name="add", filelist=[], path=str(tmp_path / "models.yaml")),
        tool=backend,
        constraints=None,
        params=None,
        defines=None,
        platform="mini",
        _reglvl=None,
        tool_overrides=None,
    )
    tool_cfg = SynthToolConfig(SynthToolConfigFile(name=backend, tool=backend))
    kwargs = dict(
        name=f"t/{backend}",
        synth_cfg=synth_cfg,
        tool_cfg=tool_cfg,
        suite_dir=str(tmp_path),
        root_cfg=_Root(str(lib)),
        effort_cfg=_effort(abc_script),
    )
    if backend == "yosys":
        synth = YosysSynth(**kwargs)
        script = synth._write_script(str(fl))
    else:
        synth = OpenRoadSynth(**kwargs)
        script = synth._write_yosys_script(str(fl))
    synth._test_filelist = str(fl)
    return synth, script, lib


def _mapped_depth(tmp_path, backend, abc_script=""):
    """Map the adder with the backend's generated Yosys script; return its `ltp -noff` length."""
    synth, script, lib = _write_script(tmp_path, backend, abc_script)
    artefact_dir = synth.artefact_dir
    subprocess.run(
        ["yosys", "-q", "-s", script], cwd=artefact_dir, check=True, capture_output=True
    )
    netlist = Path(artefact_dir) / "synth_netlist.v"
    ltp = subprocess.run(
        [
            "yosys",
            "-p",
            f"read_liberty -lib {lib}; read_verilog {netlist}; hierarchy -top add; ltp -noff",
        ],
        cwd=artefact_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"Longest topological path in \S+ \(length=(\d+)\)", ltp.stdout)
    assert match, ltp.stdout
    return int(match.group(1))


@pytest.mark.parametrize("backend", ["yosys", "openroad"])
def test_default_mapped_script_keeps_a_wide_adder_log_depth(tmp_path, backend):
    assert _mapped_depth(tmp_path, backend) <= WIDTH // 2


def test_dc2_in_the_mapped_script_makes_the_adder_ripple(tmp_path):
    """The negative control: the script with `dc2`, set through `abc-script`, ripples."""
    dc2_script = (
        "strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash; "
        "&get -n; &dch -f; &nf {D}; &put"
    )
    assert _mapped_depth(tmp_path, "yosys", dc2_script) > WIDTH // 2


def _stat_rows(synth):
    """`synth_stat.json` decoded with the strict parser, as `{module: area}`."""
    doc = json.loads(Path(synth._stats_path()).read_text())
    return {name: stats.get("area") for name, stats in doc["modules"].items()}


def test_yosys_run_writes_valid_stat_json_with_a_gzipped_liberty(tmp_path, monkeypatch):
    """#710: `tee -o` captures Yosys' `Found gzip magic` notice ahead of the JSON; the run strips it."""
    synth, _, _ = _write_script(tmp_path, "yosys", "", gzipped=True)
    monkeypatch.setattr(synth, "_write_filelist", lambda: synth._test_filelist)
    res = synth.run()
    assert res.results["result"] == "PASS", res.results
    rows = _stat_rows(synth)
    assert rows["\\add"] and rows["\\add"] > 0
    assert "Found gzip magic" in Path(synth._log_path()).read_text()


def test_openroad_stage_1_writes_valid_stat_json_with_a_gzipped_liberty(tmp_path):
    synth, _, _ = _write_script(tmp_path, "openroad", "", gzipped=True)
    synth.yosys_executable = "yosys"
    gate_count, ok, desc = synth._run_yosys_stage(synth._test_filelist)
    assert ok, desc
    rows = _stat_rows(synth)
    assert rows["\\add"] and rows["\\add"] > 0
