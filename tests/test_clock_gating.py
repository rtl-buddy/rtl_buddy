"""Clock gating with Yosys `clockgate` before `dfflibmap` (#786)."""

import logging
from pathlib import Path

import pytest
from serde.yaml import from_yaml

from rtl_buddy.config.model import ModelConfig
from rtl_buddy.config.synth import (
    SynthConfig,
    SynthEffortConfig,
    SynthEffortConfigFile,
    SynthEffortYosysFile,
    SynthToolConfig,
    SynthToolConfigFile,
    SynthToolOptsFile,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools.synth_openroad import OpenRoadSynth
from rtl_buddy.tools.synth_yosys import YosysSynth

from test_abc_constr import _ConstrPlatform, _Root, _options, _platform_cfg
from test_split_liberty import _ns_lib

BACKENDS = ["yosys", "openroad"]


def _events(caplog, event):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


class _GatingPlatform(_ConstrPlatform):
    def __init__(self, libs, dont_use=(), icg=""):
        super().__init__(libs)
        self._dont_use = list(dont_use)
        self._icg = icg

    def get_clock_gating_cell(self):
        return self._icg


def _synth(
    tmp_path,
    backend,
    *,
    tool_opts=None,
    effort=0,
    overrides=None,
    icg="",
    mapped=True,
):
    sv = tmp_path / "top.sv"
    sv.write_text("module top; endmodule\n")
    fl = tmp_path / "synth.f"
    fl.write_text(f"{sv}\n")
    lib = _ns_lib(tmp_path / "cells.lib")
    cfg = SynthConfig(
        name=f"g_{backend}",
        desc="g",
        model=ModelConfig(name="top", filelist=[], path=str(tmp_path / "models.yaml")),
        tool=backend,
        constraints=None,
        params=None,
        defines=None,
        platform="p" if mapped else None,
        _reglvl=None,
        tool_overrides={backend: overrides} if overrides else None,
        lib_paths=[],
    )
    synth_cls = YosysSynth if backend == "yosys" else OpenRoadSynth
    synth = synth_cls(
        name=f"t/{backend}",
        synth_cfg=cfg,
        tool_cfg=SynthToolConfig(
            SynthToolConfigFile(
                name=backend, tool=backend, opts=tool_opts or SynthToolOptsFile()
            )
        ),
        suite_dir=str(tmp_path),
        root_cfg=_Root(_GatingPlatform([lib], ["FILL*"], icg)),
        effort_cfg=SynthEffortConfig(
            SynthEffortConfigFile(
                name="e",
                yosys=SynthEffortYosysFile(clock_gating_min_flops=effort),
            )
        ),
    )
    write = synth._write_script if backend == "yosys" else synth._write_yosys_script
    return synth, Path(write(str(fl))).read_text(), lib


@pytest.mark.parametrize("backend", BACKENDS)
def test_off_by_default(tmp_path, backend):
    synth, script, _lib = _synth(tmp_path, backend)
    assert "clockgate" not in script
    assert "clock_gating" not in _options(synth, backend)


@pytest.mark.parametrize("backend", BACKENDS)
def test_effort_inserts_clockgate_between_chformal_and_dfflibmap(tmp_path, backend):
    synth, script, lib = _synth(tmp_path, backend, effort=8)
    lines = script.splitlines()
    i = lines.index("chformal -remove")
    assert lines[i + 1] == f"clockgate -liberty {lib} -dont_use FILL* -min_net_size 8"
    assert lines[i + 2].startswith("dfflibmap ")
    assert _options(synth, backend)["clock_gating"] == {"min_flops": 8, "cell": None}


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_platform_icg_is_used_with_pos(tmp_path, backend):
    _synth_obj, script, _lib = _synth(
        tmp_path, backend, effort=4, icg="sky130_fd_sc_hd__dlclkp_1 GATE:CLK:GCLK"
    )
    assert (
        "clockgate -pos sky130_fd_sc_hd__dlclkp_1 GATE:CLK:GCLK -min_net_size 4"
        in script.splitlines()
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_precedence_is_override_then_effort_then_tool(tmp_path, backend):
    tool = SynthToolOptsFile(clock_gating_min_flops=2)

    def _n(**kwargs):
        _s, script, _lib = _synth(tmp_path, backend, tool_opts=tool, **kwargs)
        return [ln for ln in script.splitlines() if ln.startswith("clockgate")]

    assert _n()[0].endswith("-min_net_size 2")
    assert _n(effort=16)[0].endswith("-min_net_size 16")
    assert _n(effort=16, overrides={"clock_gating_min_flops": 32})[0].endswith(
        "-min_net_size 32"
    )
    # A run override of 0 turns it off.
    assert _n(effort=16, overrides={"clock_gating_min_flops": 0}) == []


def test_an_unmapped_run_warns_and_gates_nothing(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _synth_obj, script, _lib = _synth(tmp_path, "yosys", effort=8, mapped=False)
    assert "clockgate" not in script
    [rec] = _events(caplog, "synth.clock_gating_ignored")
    assert rec.rtl_fields["min_flops"] == 8


@pytest.mark.parametrize("bad", [-1, True, "8"])
def test_a_bad_min_flops_is_a_config_error(tmp_path, bad):
    with pytest.raises(FatalRtlBuddyError, match="clock_gating_min_flops|clock-gating"):
        _synth(tmp_path, "yosys", overrides={"clock_gating_min_flops": bad})


def test_the_keys_load_from_yaml():
    opts = from_yaml(SynthToolOptsFile, "clock-gating-min-flops: 8\n")
    assert opts.clock_gating_min_flops == 8
    effort = from_yaml(
        SynthEffortConfigFile, "name: e\nyosys:\n  clock-gating-min-flops: 12\n"
    )
    assert SynthEffortConfig(effort).get_yosys_clock_gating_min_flops() == 12
    cfg = _platform_cfg(
        "name: p\npdk: k\nclock-gating-cell: 'dlclkp_1  GATE:CLK:GCLK'\n"
    )
    assert cfg.get_clock_gating_cell() == "dlclkp_1 GATE:CLK:GCLK"
    assert _platform_cfg("name: p\npdk: k\n").get_clock_gating_cell() == ""


@pytest.mark.parametrize(
    "spec", ["dlclkp_1", "dlclkp_1 GATE:CLK", "dlclkp_1 GATE::GCLK", "a b c"]
)
def test_a_malformed_icg_is_a_config_error(spec):
    with pytest.raises(FatalRtlBuddyError, match="clock-gating-cell"):
        _platform_cfg(f"name: p\npdk: k\nclock-gating-cell: '{spec}'\n")
