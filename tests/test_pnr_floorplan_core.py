"""Tests for explicit die/core areas, core cut-outs, a run's `pdn-config` and the declarative `pdn:` block (rtl_buddy#105)."""

import textwrap
from importlib.resources import files
from unittest.mock import MagicMock

import pytest
from test_pnr_floorplan_controls import _floorplan, _load, _render
from test_pnr_floorplan_pins import _platform
from test_pnr_macro_pack import _tcl_eval

from rtl_buddy.config.pdk import PdkConfig
from rtl_buddy.config.pnr import (
    PnrConfigFile,
    PnrFloorplanFile,
    PnrPdn,
    PnrPdnFile,
    PnrPdnRing,
    PnrPdnRingFile,
    PnrPdnStripe,
    PnrPdnStripeFile,
    PnrSuiteConfig,
)
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract

_DEFAULT_INIT = (
    "initialize_floorplan \\\n"
    "    -site         $SITE \\\n"
    "    -utilization  $CORE_UTIL_PCT \\\n"
    "    -aspect_ratio $CORE_ASPECT \\\n"
    "    -core_space   $CORE_MARGIN\n"
)


def _run(tmp_path, **fields):
    floorplan = fields.pop("floorplan", {})
    return PnrConfigFile(
        name="fp",
        desc="fp",
        synth="demo",
        synth_path="synth.yaml",
        platform="p",
        floorplan=PnrFloorplanFile(**floorplan),
        **fields,
    ).initialise(str(tmp_path))


# --- die and core areas -----------------------------------------------------


def test_sizing_defaults_are_unchanged(tmp_path):
    fp = _load(tmp_path).get_floorplan()

    assert (fp.utilization, fp.aspect, fp.core_margin) == (0.55, 1.0, 2.0)
    assert fp.die_area is None and fp.core_area is None and fp.core_cutouts == []


def test_explicit_areas_load(tmp_path):
    fp = _load(
        tmp_path,
        die_area=[0, 0, 160, 160],
        core_area=[20, 20.5, 140, 140],
        core_cutouts=[[80, 80, 140, 140]],
    ).get_floorplan()

    assert fp.die_area == (0.0, 0.0, 160.0, 160.0)
    assert fp.core_area == (20.0, 20.5, 140.0, 140.0)
    assert fp.core_cutouts == [(80.0, 80.0, 140.0, 140.0)]
    assert fp.ring_margin() == 20.0


@pytest.mark.parametrize(
    ("floorplan", "message"),
    [
        ({"die_area": [0, 0, 10, 10]}, "die-area and core-area go together"),
        ({"core_area": [1, 1, 9, 9]}, "die-area and core-area go together"),
        (
            {"die_area": [0, 0, 10, 10], "core_area": [1, 1, 9, 9], "utilization": 0.5},
            "utilization cannot be set with them",
        ),
        (
            {
                "die_area": [0, 0, 10, 10],
                "core_area": [1, 1, 9, 9],
                "aspect": 1,
                "core_margin": 1,
            },
            "aspect, core-margin cannot be set",
        ),
        (
            {"die_area": [0, 0, 10], "core_area": [1, 1, 9, 9]},
            r"die-area must be \[x0, y0, x1, y1\]",
        ),
        (
            {"die_area": [0, 0, 10, 10], "core_area": [5, 1, 5, 9]},
            "core-area must have x0 < x1",
        ),
        (
            {"die_area": [0, 0, 10, 10], "core_area": [1, 1, 11, 9]},
            "must lie inside die-area",
        ),
        ({"core_cutouts": [[-1, 0, 5, 5]]}, r"core-cutouts\[0\] is in die coordinates"),
        (
            {
                "die_area": [0, 0, 10, 10],
                "core_area": [1, 1, 9, 9],
                "core_cutouts": [[5, 5, 9, 9], [5, 5, 9.5, 9]],
            },
            r"core-cutouts\[1\] .* must lie inside core-area",
        ),
    ],
)
def test_bad_sizing_fails_at_load(tmp_path, floorplan, message):
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        _load(tmp_path, **floorplan)
    assert "pnr run 'fp': floorplan" in str(err.value)


def test_utilization_sizing_renders_the_plain_command(tmp_path):
    text = _render(tmp_path)

    assert _DEFAULT_INIT in text
    assert "Core cut-outs" not in text


def test_explicit_areas_render_die_and_core(tmp_path):
    text = _render(
        tmp_path,
        _floorplan(die_area=(0.0, 0.0, 160.0, 160.0), core_area=(20.0, 20.5, 140, 140)),
    )

    assert (
        "initialize_floorplan \\\n"
        "    -site      $SITE \\\n"
        "    -die_area  {0 0 160 160} \\\n"
        "    -core_area {20 20.5 140 140}\n"
    ) in text
    assert "-utilization" not in text


def test_cutouts_are_checked_hard_blockages_that_cut_rows_and_keep_macros_out(
    tmp_path,
):
    text = _render(tmp_path, _floorplan(core_cutouts=[(80.0, 80.0, 140.0, 140.0)]))

    assert 'puts ">>> Core cut-outs"\nset MACRO_KEEPOUTS {}\n' in text
    assert (
        "set blockage_box [[create_blockage -region {80 80 140 140}] getBBox]\n"
    ) in text
    assert "floorplan.core-cutouts[0] {80 80 140 140} um does not overlap" in text
    assert "[rb::rows::cut_under [ord::get_db_block] $MACRO_KEEPOUTS]" in text
    assert "      lower-left $MACRO_KEEPOUTS]\n" in text
    assert text.index("Core cut-outs") < text.index('puts ">>> Macro placement"')


# --- pdn-config and pdn: ------------------------------------------------------

_SKY130_PDN = {
    "ring": PnrPdnRingFile(layers=["met5", "met4"], width=1.6, spacing=1.7, offset=2),
    "stripes": [
        PnrPdnStripeFile(layer="met1", width=0.48, followpins=True),
        PnrPdnStripeFile(layer="met4", width=1.6, pitch=20, offset=5),
        PnrPdnStripeFile(layer="met5", width=1.6, pitch=20, offset=5, spacing=2),
    ],
}


def test_a_pdn_block_loads_with_the_default_connect_chain(tmp_path):
    pdn = _run(
        tmp_path, pdn=PnrPdnFile(**_SKY130_PDN), floorplan={"core_margin": 10}
    ).get_pdn()

    assert pdn == PnrPdn(
        stripes=(
            PnrPdnStripe("met1", 0.48, followpins=True),
            PnrPdnStripe("met4", 1.6, pitch=20.0, offset=5.0),
            PnrPdnStripe("met5", 1.6, pitch=20.0, offset=5.0, spacing=2.0),
        ),
        connect=(("met1", "met4"), ("met4", "met5")),
        ring=PnrPdnRing(("met5", "met4"), 1.6, 1.7, 2.0),
    )


def test_an_explicit_connect_list_is_kept(tmp_path):
    pdn = _run(
        tmp_path,
        pdn=PnrPdnFile(
            stripes=[PnrPdnStripeFile(layer="M1", width=0.1, followpins=True)],
            connect=[["M1", "M5"]],
        ),
    ).get_pdn()

    assert pdn.connect == (("M1", "M5"),)
    assert pdn.ring is None


@pytest.mark.parametrize(
    ("pdn", "floorplan", "message"),
    [
        ({"stripes": []}, {}, "stripes must list the core grid"),
        (
            {"stripes": [PnrPdnStripeFile(layer="m4", width=1)]},
            {},
            r"stripes\[0\]: a stripe needs a pitch",
        ),
        (
            {"stripes": [PnrPdnStripeFile(layer="m4", width=0, pitch=5)]},
            {},
            "width must be a positive",
        ),
        (
            {"stripes": [PnrPdnStripeFile(layer="m4", width=3, pitch=5)]},
            {},
            "do not fit in the 5 um pitch",
        ),
        (
            {
                "stripes": [
                    PnrPdnStripeFile(layer="m1", width=1, followpins=True, pitch=5)
                ]
            },
            {},
            "pitch does not apply",
        ),
        (
            {
                "stripes": [PnrPdnStripeFile(layer="m1", width=1, followpins=True)],
                "connect": [["m1", "m1"]],
            },
            {},
            r"connect\[0\]: must be two different layers",
        ),
        (
            {
                "stripes": [PnrPdnStripeFile(layer="m1", width=1, followpins=True)],
                "ring": PnrPdnRingFile(layers=["m5"], width=1, spacing=1, offset=0),
            },
            {},
            r"ring: layers must be \[horizontal, vertical\]",
        ),
        (
            {
                "stripes": [PnrPdnStripeFile(layer="m1", width=1, followpins=True)],
                "ring": PnrPdnRingFile(
                    layers=["m5", "m4"], width=1, spacing=1, offset=0.5
                ),
            },
            {},
            r"ring needs 3.5 um outside the core .* leaves 2 um",
        ),
        (
            {
                "stripes": [PnrPdnStripeFile(layer="m1", width=1, followpins=True)],
                "ring": PnrPdnRingFile(
                    layers=["m5", "m4"], width=2, spacing=1, offset=2
                ),
            },
            {"die_area": [0, 0, 100, 100], "core_area": [5, 10, 95, 90]},
            r"ring needs 7 um outside the core .* leaves 5 um",
        ),
    ],
)
def test_a_bad_pdn_block_fails_at_load(tmp_path, pdn, floorplan, message):
    with pytest.raises(FatalRtlBuddyError, match=message) as err:
        _run(tmp_path, pdn=PnrPdnFile(**pdn), floorplan=floorplan)
    assert "pnr run 'fp': pdn" in str(err.value)


def test_the_yaml_spelling_loads(tmp_path):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            rtl-buddy-filetype: pnr_config
            runs:
              - name: r
                desc: r
                synth: s
                synth-path: synth.yaml
                platform: p
                pdn-config: pdn/run_pdn.tcl
                floorplan:
                  die-area: [0, 0, 160, 160]
                  core-area: [20, 20, 140, 140]
                  core-cutouts:
                    - [80, 80, 140, 140]
                pdn:
                  ring: {layers: [met5, met4], width: 1.6, spacing: 1.7, offset: 2}
                  stripes:
                    - {layer: met1, width: 0.48, followpins: true}
                    - {layer: met4, width: 1.6, pitch: 20, offset: 5}
            """
        )
    )
    run = PnrSuiteConfig(str(path)).get_runs("r")[0]

    assert run.get_pdn_config() == str(tmp_path / "pdn" / "run_pdn.tcl")
    assert run.get_pdn().connect == (("met1", "met4"),)
    assert run.get_floorplan().core_cutouts == [(80.0, 80.0, 140.0, 140.0)]


def test_the_run_pdn_config_replaces_the_pdks(tmp_path, monkeypatch):
    pdk_pdn = tmp_path / "pdk_pdn.tcl"
    pdk_pdn.write_text("# pdk\n")
    run_pdn = tmp_path / "run_pdn.tcl"
    run_pdn.write_text("# run\n")
    monkeypatch.setattr(PdkConfig, "get_pdn_config", lambda self: str(pdk_pdn))

    text = _render(tmp_path, pdn_config=str(run_pdn))

    assert f"source {run_pdn}\n" in text
    assert str(pdk_pdn) not in text


def test_a_pdn_block_replaces_the_core_grid_before_pdngen(tmp_path, monkeypatch):
    pdk_pdn = tmp_path / "pdk_pdn.tcl"
    pdk_pdn.write_text("# pdk\n")
    monkeypatch.setattr(PdkConfig, "get_pdn_config", lambda self: str(pdk_pdn))
    pdn = _run(
        tmp_path, pdn=PnrPdnFile(**_SKY130_PDN), floorplan={"core_margin": 10}
    ).get_pdn()

    text = _render(tmp_path, pdn=pdn)

    grid = (
        "rb::pdn::source_base $PDN_CONFIG\n"
        "set rb_pdn_grid [rb::pdn::define_core_grid]\n"
        "add_pdn_ring -grid $rb_pdn_grid -layers {met5 met4} -widths 1.6 "
        "-spacings 1.7 -core_offsets 2\n"
        "add_pdn_stripe -grid $rb_pdn_grid -layer {met1} -width 0.48 -followpins "
        "-extend_to_core_ring\n"
        "add_pdn_stripe -grid $rb_pdn_grid -layer {met4} -width 1.6 -pitch 20 "
        "-offset 5 -extend_to_core_ring\n"
        "add_pdn_stripe -grid $rb_pdn_grid -layer {met5} -width 1.6 -pitch 20 "
        "-offset 5 -spacing 2 -extend_to_core_ring\n"
        "add_pdn_connect -grid $rb_pdn_grid -layers {met1 met4}\n"
        "add_pdn_connect -grid $rb_pdn_grid -layers {met4 met5}\n"
        "pdngen\n"
    )
    assert grid in text
    assert f"set PDN_CONFIG {{{pdk_pdn}}}\n" in text
    assert f"source {pdk_pdn}" not in text
    assert text.index("proc rb::pdn::source_base") < text.index(grid)


def test_a_pdn_block_without_any_pdn_config_fails_at_setup(tmp_path, monkeypatch):
    from rtl_buddy.tools import pnr_openroad
    from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

    pnr_cfg = MagicMock()
    pnr_cfg.get_name.return_value = "demo"
    pnr_cfg.get_pdn_config.return_value = None
    pnr_cfg.get_pdn.return_value = _run(
        tmp_path, pdn=PnrPdnFile(**_SKY130_PDN), floorplan={"core_margin": 10}
    ).get_pdn()
    pnr_cfg.get_checkpoints.return_value = None
    platform = MagicMock()
    platform.get_pdk.return_value.get_pdn_config.return_value = ""
    root_cfg = MagicMock()
    root_cfg.get_pnr_platform_cfg.return_value = platform
    monkeypatch.setattr(pnr_openroad.shutil, "which", lambda _n: "/usr/bin/openroad")
    backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), root_cfg)
    monkeypatch.setattr(backend, "_probe_openroad_version", lambda: None)
    monkeypatch.setattr(backend, "_clear_stale_outputs", lambda **kw: None)
    launched = []
    monkeypatch.setattr(
        pnr_openroad.subprocess, "run", lambda *a, **k: launched.append(a)
    )

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "pdn: needs a pdn-config" in res.results["desc"]
    assert launched == []


def _pdn_tcl(script: str) -> str:
    """Evaluate `script` after pdn_grid.tcl, dropping the progress lines it prints."""
    source = files("rtl_buddy.pnr").joinpath("pdn_grid.tcl").read_text()
    out = _tcl_eval(source + "\n" + script)
    return "\n".join(line for line in out.split("\n") if not line.startswith(">>>"))


def test_core_grid_commands_are_skipped_and_the_rest_kept():
    """Under a bare interpreter, stand-in pdn commands record what reaches them."""
    script = textwrap.dedent(
        """\
        set log {}
        foreach c {define_pdn_grid add_pdn_stripe add_pdn_ring add_pdn_connect} {
            proc $c {args} "lappend ::log \\[list $c {*}\\$args\\]"
        }
        set f [file tempfile path]
        puts $f {
            set_voltage_domain_stub
            define_pdn_grid -name {grid} -voltage_domains {CORE} -pins {met5}
            add_pdn_stripe -grid {grid} -layer {met1} -width {0.48} -followpins
            add_pdn_connect -layers {met1 met4}
            define_pdn_grid -name {macro} -voltage_domains {CORE} -macro -default
            add_pdn_connect -layers {met4 met5}
            add_pdn_connect -grid {grid} -layers {met4 met5}
        }
        close $f
        proc set_voltage_domain_stub {} {}
        rb::pdn::source_base $path
        file delete $path
        set name [rb::pdn::define_core_grid]
        set result [join [list $name {*}$log "leftovers=[info procs ::rb::pdn::orig_*]"] "\n"]
        """
    )
    assert _pdn_tcl(script).split("\n") == [
        "grid",
        "define_pdn_grid -name macro -voltage_domains CORE -macro -default",
        "add_pdn_connect -layers {met4 met5}",
        "define_pdn_grid -name grid -voltage_domains CORE -pins met5",
        "leftovers=",
    ]


def test_without_a_core_grid_the_run_grid_is_rb_core():
    script = textwrap.dedent(
        """\
        proc define_pdn_grid {args} { set ::defined $args }
        set result "[rb::pdn::define_core_grid] $::defined"
        """
    )
    assert _pdn_tcl(script) == "rb_core -name rb_core"


def test_new_keys_enter_the_config_digest_only_when_set(tmp_path):
    default = pnr_abstract.abstract_config(_run(tmp_path), _platform(tmp_path))
    for key in ("die_area", "core_area", "core_cutouts"):
        assert key not in default["floorplan"]
    assert "pdn" not in default and "pdn_config" not in default

    shaped = pnr_abstract.abstract_config(
        _run(
            tmp_path,
            pdn_config="run_pdn.tcl",
            pdn=PnrPdnFile(**_SKY130_PDN),
            floorplan={
                "die_area": [0, 0, 160, 160],
                "core_area": [20, 20, 140, 140],
                "core_cutouts": [[80, 80, 140, 140]],
            },
        ),
        _platform(tmp_path),
    )
    assert shaped["floorplan"]["die_area"] == [0.0, 0.0, 160.0, 160.0]
    assert shaped["floorplan"]["core_cutouts"] == [[80.0, 80.0, 140.0, 140.0]]
    assert shaped["pdn"]["connect"] == [["met1", "met4"], ["met4", "met5"]]
    assert shaped["pdn"]["ring"]["layers"] == ["met5", "met4"]
    assert shaped["pdn_config"].endswith("run_pdn.tcl")
    assert pnr_abstract.config_digest(shaped) != pnr_abstract.config_digest(default)
