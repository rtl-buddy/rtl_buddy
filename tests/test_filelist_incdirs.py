"""Filelist ``+incdir+`` entries reach the non-simulation flows (#519).

The generated filelist each flow reads back carries the model's
``+incdir+`` directories; before #519 every flow but simulation dropped
them and handed the tool sources only. These tests cover the shared
extraction helper and the Vivado Tcl rendering; the per-flow script tests
live beside each flow's other tests.
"""

from rtl_buddy.tools.cdc_vivado import render_cdc_tcl
from rtl_buddy.tools.fpga_vivado_flow import include_dirs_arg, render_flow_tcl
from rtl_buddy.tools.vlog_filelist import incdirs_from_filelist


def test_incdirs_resolve_against_the_filelist_in_order_without_duplicates(tmp_path):
    fl = tmp_path / "gen" / "run.f"
    fl.parent.mkdir()
    fl.write_text(
        "+incdir+../inc\n"
        "-v ../top.sv\n"
        "+incdir+/abs/inc+../inc/nested\n"
        "+incdir+../inc\n"
        "+define+X=1\n"
    )
    assert incdirs_from_filelist(str(fl)) == [
        str(tmp_path / "inc"),
        "/abs/inc",
        str(tmp_path / "inc" / "nested"),
    ]


def test_incdirs_of_a_missing_filelist_are_empty(tmp_path):
    assert incdirs_from_filelist(str(tmp_path / "nope.f")) == []


def test_include_dirs_arg_braces_each_directory():
    assert include_dirs_arg([]) == ""
    assert include_dirs_arg(["/a/inc", "/b c/inc"]) == (
        " -include_dirs {{/a/inc} {/b c/inc}}"
    )


def test_flow_tcl_passes_include_dirs_to_synth_design():
    script = render_flow_tcl(
        top="t",
        part="xc7a35t",
        verilog_sources=["a.sv"],
        xdc_files=[],
        include_dirs=["/proj/inc"],
    )
    assert "synth_design -top t -part xc7a35t -include_dirs {{/proj/inc}}\n" in script
    without = render_flow_tcl(
        top="t", part="xc7a35t", verilog_sources=["a.sv"], xdc_files=[]
    )
    assert "synth_design -top t -part xc7a35t\n" in without


def test_cdc_tcl_passes_include_dirs_to_synth_design():
    script = render_cdc_tcl(
        top="t",
        part="xc7a35t",
        verilog_sources=["a.sv"],
        sdc_file="a.sdc",
        include_dirs=["/proj/inc"],
    )
    assert "synth_design -top t -part xc7a35t -include_dirs {{/proj/inc}}" in script
