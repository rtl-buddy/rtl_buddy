"""A P&R run's own routing, CTS, repair and tap-cell values over its platform's, and the platform's `tapcell-tcl` over the PDK's (rtl_buddy#853, #841, #868)."""

import os
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
import yaml
from test_pnr_checkpoints import _backend, _pdk, _pnr_cfg
from test_pnr_repair import _render

from rtl_buddy.config.pnr import CHECKPOINT_STAGES, PnrSuiteConfig
from rtl_buddy.config.pnr_platform import (
    PnrPlatformConfig,
    PnrPlatformConfigFile,
    PnrRoutingLayersFile,
    PnrRunOverrides,
)
from rtl_buddy.config.pnr_repair import GlobalRouteRepair, RepairDesign
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr, tcl_source


def _platform(pdk, config_dir="", **kw):
    base = dict(
        name="pl",
        pdk="p",
        cts_buffer="BUF",
        routing_layers=PnrRoutingLayersFile(signal="metal2-metal6", clock="metal4"),
    )
    base.update(kw)
    return PnrPlatformConfig(
        PnrPlatformConfigFile(**base), lambda _name: pdk, config_dir
    )


def _run(tmp_path, **keys):
    """Load one `pnr.yaml` run with `keys` (YAML spelling) and return its config."""
    run = {
        "name": "r",
        "desc": "d",
        "synth": "s",
        "synth-path": "synth.yaml",
        "floorplan": {"utilization": 0.55},
        "platform": "pl",
        **keys,
    }
    path = tmp_path / "pnr.yaml"
    path.write_text(yaml.safe_dump({"rtl-buddy-filetype": "pnr_config", "runs": [run]}))
    return PnrSuiteConfig(str(path)).get_runs("r")[0]


def _resolved(tmp_path, platform, **keys):
    return platform.with_run_overrides(_run(tmp_path, **keys).get_overrides())


# -- precedence ------------------------------------------------------------------


def test_a_run_without_overrides_keeps_the_platform_itself(tmp_path):
    platform = _platform(_pdk(tmp_path))
    run = _run(tmp_path)
    assert run.get_overrides() == PnrRunOverrides()
    assert platform.with_run_overrides(run.get_overrides()) is platform


@pytest.mark.parametrize(
    ("platform_kw", "run_keys", "getter", "expected"),
    [
        ({}, {"cts-buffer": "CLKBUF_X3"}, "get_cts_buffers", ["CLKBUF_X3"]),
        (
            {"cts_buffer": ["A", "B"]},
            {"cts-buffer": ["C", "D"]},
            "get_cts_buffers",
            ["C", "D"],
        ),
        ({}, {"cts-buffer": ["C", "D"]}, "get_cts_buffer", "C"),
        (
            {},
            {"cts-clock-nets": ["clk", "clk_div[0]"]},
            "get_cts_clock_nets",
            ("clk", "clk_div[0]"),
        ),
        ({}, {"cts-clock-nets": "clk"}, "get_cts_clock_nets", ("clk",)),
        ({}, {"cts-repair-clock-nets": True}, "get_cts_repair_clock_nets", True),
        (
            {"cts_repair_clock_nets": True},
            {"cts-repair-clock-nets": False},
            "get_cts_repair_clock_nets",
            False,
        ),
        ({}, {"cts-latency-balancing": False}, "get_cts_latency_balancing", False),
        (
            {"cts_latency_balancing": False},
            {"cts-latency-balancing": True},
            "get_cts_latency_balancing",
            True,
        ),
        ({}, {"post-cts-setup-repair": True}, "get_post_cts_setup_repair", True),
        (
            {"post_cts_setup_repair": True},
            {"post-cts-setup-repair": False},
            "get_post_cts_setup_repair",
            False,
        ),
        (
            {"routing_layer_adjustment": 0.1},
            {"routing-layer-adjustment": 0.3},
            "get_routing_layer_adjustment",
            0.3,
        ),
        ({}, {"routing-layer-adjustment": 0}, "get_routing_layer_adjustment", 0.0),
        (
            {"port_buffer": "BUF_X1"},
            {"port-buffer": "BUF_X4"},
            "get_port_buffer",
            "BUF_X4",
        ),
    ],
)
def test_a_run_value_wins_over_the_platform(
    tmp_path, platform_kw, run_keys, getter, expected
):
    platform = _platform(_pdk(tmp_path), **platform_kw)
    before = getattr(platform, getter)()

    resolved = _resolved(tmp_path, platform, **run_keys)

    assert getattr(resolved, getter)() == expected
    # The platform keeps its own.
    assert getattr(platform, getter)() == before


def test_the_new_platform_knobs_default_to_today_s_flow(tmp_path):
    platform = _platform(_pdk(tmp_path))
    assert platform.get_cts_latency_balancing() is True
    assert platform.get_cts_repair_clock_nets() is False
    assert platform.get_cts_clock_nets() == ()
    assert platform.get_tapcell_tcl() == ""


def test_run_routing_layers_override_field_by_field(tmp_path):
    platform = _platform(_pdk(tmp_path))

    signal = _resolved(tmp_path, platform, **{"routing-layers": {"signal": "metal2"}})
    clock = _resolved(tmp_path, platform, **{"routing-layers": {"clock": "metal5"}})

    assert (signal.get_signal_layers(), signal.get_clock_layers()) == (
        "metal2",
        "metal4",
    )
    assert (clock.get_signal_layers(), clock.get_clock_layers()) == (
        "metal2-metal6",
        "metal5",
    )


def test_tapcell_tcl_comes_from_the_most_specific_level(tmp_path):
    pdk = _pdk(tmp_path, tapcell_tcl="pdk/tap.tcl")
    assert _platform(pdk).get_tapcell_tcl() == str(tmp_path / "pdk" / "tap.tcl")

    platform = _platform(pdk, config_dir=str(tmp_path), tapcell_tcl="plat/tap.tcl")
    assert platform.get_tapcell_tcl() == str(tmp_path / "plat" / "tap.tcl")
    assert platform.get_tcl_hooks()["tapcell-tcl"] == platform.get_tapcell_tcl()

    blk = tmp_path / "blk"
    blk.mkdir()
    resolved = _resolved(blk, platform, **{"tapcell-tcl": "../run/tap.tcl"})
    assert resolved.get_tapcell_tcl() == str(tmp_path / "run" / "tap.tcl")
    assert platform.get_tapcell_tcl() == str(tmp_path / "plat" / "tap.tcl")


def test_a_platform_tapcell_tcl_without_a_pdk_one_joins_the_hooks_in_flow_order(
    tmp_path,
):
    pdk = _pdk(tmp_path, platform_tcl="plat.tcl", tracks_tcl="tracks.tcl")
    platform = _platform(pdk, config_dir=str(tmp_path), tapcell_tcl="tap.tcl")
    assert list(platform.get_tcl_hooks()) == [
        "platform-tcl",
        "tracks-tcl",
        "tapcell-tcl",
    ]
    # The PDK keeps its own.
    assert "tapcell-tcl" not in pdk.get_tcl_hooks()


def test_a_root_config_resolves_a_platform_tapcell_tcl_from_its_own_directory(
    minimal_project,
):
    from test_config_pnr_unknown_keys import PDK_ALL, _with_root_blocks

    from rtl_buddy.config.root import RootConfig

    _with_root_blocks(
        minimal_project,
        [PDK_ALL],
        [
            {"name": "pl1", "pdk": "pdk1", "corner": "typ"},
            {"name": "pl2", "pdk": "pdk1", "corner": "typ", "tapcell-tcl": "t2.tcl"},
        ],
    )

    cfg = RootConfig(name="tap")

    assert cfg.get_pnr_platform_cfg("pl1").get_tapcell_tcl() == str(
        minimal_project / "tap.tcl"
    )
    assert cfg.get_pnr_platform_cfg("pl2").get_tapcell_tcl() == str(
        minimal_project / "t2.tcl"
    )


# -- global-route-hold-repair on a run ---------------------------------------------


def test_a_run_hold_repair_shorthand_adds_the_hold_repair(tmp_path):
    platform = _platform(_pdk(tmp_path))
    run = _run(tmp_path, **{"global-route-hold-repair": True})

    resolved = platform.with_run_repairs(
        run.global_route_repair, run.post_route_repair, "r"
    )

    assert resolved.get_global_route_repair() == GlobalRouteRepair(hold_margin=0.0)


def test_a_run_hold_repair_false_turns_the_platform_shorthand_off(tmp_path):
    platform = _platform(_pdk(tmp_path), global_route_hold_repair=True)
    run = _run(tmp_path, **{"global-route-hold-repair": False})

    resolved = platform.with_run_repairs(
        run.global_route_repair, run.post_route_repair, "r"
    )

    assert resolved.get_global_route_repair() is None
    assert platform.get_global_route_repair() == GlobalRouteRepair(hold_margin=0.0)


def test_a_run_hold_repair_false_keeps_the_rest_of_a_platform_block(tmp_path):
    platform = _platform(
        _pdk(tmp_path), global_route_repair={"setup-margin": 0.1, "hold-margin": 0.0}
    )
    run = _run(tmp_path, **{"global-route-hold-repair": False})

    resolved = platform.with_run_repairs(
        run.global_route_repair, run.post_route_repair, "r"
    )

    assert resolved.get_global_route_repair() == GlobalRouteRepair(setup_margin=0.1)


def test_a_run_hold_repair_false_over_no_platform_repair_drops_it_from_the_default(
    tmp_path,
):
    platform = _platform(_pdk(tmp_path))
    run = _run(tmp_path, **{"global-route-hold-repair": False})

    resolved = platform.with_run_repairs(
        run.global_route_repair, run.post_route_repair, "r"
    )

    assert resolved.get_global_route_repair() == GlobalRouteRepair(
        repair_design=RepairDesign(), setup_margin=0.0
    )


def test_a_run_with_the_shorthand_and_the_block_is_refused(tmp_path):
    with pytest.raises(
        FatalRtlBuddyError,
        match=r"pnr run 'r': set either global-route-hold-repair or global-route-repair",
    ):
        _run(
            tmp_path,
            **{
                "global-route-hold-repair": True,
                "global-route-repair": {"hold-margin": 0.0},
            },
        )


# -- validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({"cts-buffer": []}, "pnr run 'r': cts-buffer must be one buffer cell name"),
        ({"cts-buffer": "A B"}, "pnr run 'r': cts-buffer must be one buffer cell name"),
        ({"cts-clock-nets": []}, "pnr run 'r': cts-clock-nets must be one clock net"),
        (
            {"cts-clock-nets": ["clk a"]},
            "pnr run 'r': cts-clock-nets must be one clock net",
        ),
        (
            {"cts-clock-nets": ["clk}"]},
            "pnr run 'r': cts-clock-nets must be one clock net",
        ),
        (
            {"routing-layer-adjustment": 1.5},
            "pnr run 'r': routing-layer-adjustment must be a number from 0 to 1",
        ),
        (
            {"port-buffer": ["BUF_X1"]},
            "pnr run 'r': port-buffer must be one buffer cell name",
        ),
        ({"tapcell-tcl": ""}, "pnr run 'r': tapcell-tcl must be a path to a Tcl file"),
    ],
)
def test_a_bad_run_value_is_refused_naming_the_run_and_the_key(tmp_path, keys, message):
    with pytest.raises(FatalRtlBuddyError) as exc:
        _run(tmp_path, **keys)
    assert message in str(exc.value)


def test_a_bad_platform_tapcell_tcl_names_the_platform(tmp_path):
    with pytest.raises(
        FatalRtlBuddyError,
        match="pnr platform 'pl': tapcell-tcl must be a path to a Tcl file",
    ):
        _platform(_pdk(tmp_path), tapcell_tcl="")


# -- rendered flow ----------------------------------------------------------------

#: The placeholders the overrides added to the flow template.
_NEW_PLACEHOLDERS = (
    "{{ cts_clock_nets_option }}",
    "{{ cts_insertion_delay_option }}",
    "{{ cts_obstruction_option }}",
    "{{ cts_repair_clock_nets }}",
)


@pytest.mark.parametrize("kind", ["one session", "checkpoints", "stages"])
def test_a_flow_without_overrides_is_the_template_as_before(
    tmp_path, monkeypatch, kind
):
    """With no new key set, and the PDK's tap script in place, every script a run writes is the earlier template's render, whole."""
    (tmp_path / "tap.tcl").write_text("# taps\n")
    platform = _platform(_pdk(tmp_path, tapcell_tcl="tap.tcl"))

    def _write(base: bool) -> dict[str, str]:
        pnr_cfg = _pnr_cfg(
            tmp_path,
            CHECKPOINT_STAGES if kind == "checkpoints" else None,
            stages=kind == "stages",
        )
        pnr_cfg.resolve_synth_cfg = MagicMock(
            return_value=MagicMock(get_top=lambda: "demo_top", get_name=lambda: "s")
        )
        backend = OpenRoadPnr("demo/openroad", pnr_cfg, str(tmp_path), MagicMock())
        backend._ps_per_unit = 1.0
        if kind != "one session":
            backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
            os.makedirs(backend._ckpt_run_dir, exist_ok=True)
        if base:
            template = backend._load_template()
            for placeholder in _NEW_PLACEHOLDERS:
                assert template.count(placeholder) == 1
                template = template.replace(placeholder, "")
            monkeypatch.setattr(backend, "_load_template", lambda: template)
        script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))
        written = {"pnr.tcl": script.read_text()}
        if backend._ckpt_run_dir:
            for path in sorted(Path(backend._ckpt_run_dir).glob("*.tcl")):
                written[path.name] = path.read_text()
        return written

    now = _write(base=False)
    before = _write(base=True)

    assert now == before
    assert tcl_source(str(tmp_path / "tap.tcl")) in now["pnr.tcl"]


def test_the_cts_knobs_render_into_the_cts_call(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path)),
        **{
            "cts-buffer": ["CLKBUF_X2", "CLKBUF_X3"],
            "cts-clock-nets": ["clk", "clk_div[0]"],
            "cts-latency-balancing": False,
        },
    )

    text = _render(tmp_path, platform)

    assert "set CTS_BUF         {CLKBUF_X2 CLKBUF_X3}\n" in text
    assert (
        "    -sink_clustering_enable -clk_nets {clk clk_div[0]} -no_insertion_delay\n"
        in text
    )


def test_repair_clock_nets_runs_right_after_cts(tmp_path):
    hook = tmp_path / "after_cts.tcl"
    hook.write_text("# hook\n")
    platform = _resolved(
        tmp_path, _platform(_pdk(tmp_path)), **{"cts-repair-clock-nets": True}
    )

    text = _render(tmp_path, platform, hooks={"after-cts": (str(hook),)})

    block = (
        'puts ">>> Repair clock nets"\n'
        "estimate_parasitics -placement\n"
        "repair_clock_nets\n"
    )
    assert text.count(block) == 1
    at = text.index(block)
    assert text.rindex("clock_tree_synthesis", 0, at) < at
    assert at < text.index("rb::hook::run after-cts")
    assert at < text.index('puts ">>> Detail placement (post-CTS)"')


def test_the_routing_and_repair_knobs_render_from_the_run(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path), routing_layer_adjustment=0.1),
        **{
            "routing-layers": {"signal": "metal2-metal4", "clock": "metal3-metal4"},
            "routing-layer-adjustment": 0.25,
            "post-cts-setup-repair": True,
            "global-route-hold-repair": True,
            "port-buffer": "BUF_X4",
        },
    )
    run = _run(tmp_path, **{"global-route-hold-repair": True})
    platform = platform.with_run_repairs(
        run.global_route_repair, run.post_route_repair, "r"
    )

    text = _render(tmp_path, platform, harden=True)

    assert "set SIGNAL_LAYERS   metal2-metal4\n" in text
    assert "set CLOCK_LAYERS    metal3-metal4\n" in text
    assert "set_global_routing_layer_adjustment $SIGNAL_LAYERS 0.25\n" in text
    assert (
        "estimate_parasitics -placement\nrepair_timing -setup\nrepair_timing -hold\n"
        in text
    )
    assert 'puts ">>> Post-global-route hold repair"\n' in text
    assert "buffer_ports -inputs -outputs -buffer_cell BUF_X4\n" in text


def test_the_run_tapcell_tcl_replaces_the_pdk_s_in_the_flow(tmp_path):
    (tmp_path / "pdk_tap.tcl").write_text("# pdk taps\n")
    (tmp_path / "run_tap.tcl").write_text("# run taps\n")
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path, tapcell_tcl="pdk_tap.tcl")),
        **{"tapcell-tcl": "run_tap.tcl"},
    )

    text = _render(tmp_path, platform)

    assert tcl_source(str(tmp_path / "run_tap.tcl")) in text
    assert "pdk_tap.tcl" not in text


# -- the run applies them ---------------------------------------------------------


def _run_backend(tmp_path, monkeypatch, overrides: PnrRunOverrides, **pdk_fields):
    pnr_cfg = replace(_pnr_cfg(tmp_path, None), overrides=overrides)
    backend, _artefacts = _backend(
        tmp_path, monkeypatch, pnr_cfg=pnr_cfg, pdk_fields=pdk_fields
    )
    return backend


def test_a_run_writes_its_own_values_into_pnr_tcl(tmp_path, monkeypatch):
    tap = tmp_path / "run_tap.tcl"
    tap.write_text("# taps\n")
    backend = _run_backend(
        tmp_path,
        monkeypatch,
        PnrRunOverrides(
            signal_layers="metal2-metal3",
            cts_buffers=("CLKBUF_X3",),
            cts_latency_balancing=False,
            tapcell_tcl=str(tap),
        ),
    )

    backend.run()

    text = Path(backend._script_path()).read_text()
    assert "set SIGNAL_LAYERS   metal2-metal3\n" in text
    assert "set CTS_BUF         CLKBUF_X3\n" in text
    assert " -no_insertion_delay\n" in text
    assert tcl_source(str(tap)) in text
    inputs = backend._checkpoint_inputs(backend._platform, None)
    assert inputs["tapcell_tcl"]["path"].endswith("run_tap.tcl")


def test_a_missing_run_tapcell_tcl_fails_setup_naming_it(tmp_path, monkeypatch):
    backend = _run_backend(
        tmp_path,
        monkeypatch,
        PnrRunOverrides(tapcell_tcl=str(tmp_path / "gone.tcl")),
    )

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"] == f"tapcell-tcl not found: {tmp_path / 'gone.tcl'}"


# -- configuration digest ---------------------------------------------------------


def _digest_cfg(tmp_path, overrides=PnrRunOverrides(), **kw):
    return replace(_pnr_cfg(tmp_path, None), harden=True, overrides=overrides, **kw)


@pytest.mark.parametrize(
    ("overrides", "key", "value"),
    [
        (PnrRunOverrides(signal_layers="metal2"), "routing", None),
        (PnrRunOverrides(clock_layers="metal5"), "routing", None),
        (PnrRunOverrides(cts_buffers=("CLKBUF_X3",)), "cts_buffers", ["CLKBUF_X3"]),
        (PnrRunOverrides(cts_clock_nets=("clk",)), "cts_clock_nets", ["clk"]),
        (PnrRunOverrides(cts_latency_balancing=False), "cts_latency_balancing", False),
        (PnrRunOverrides(cts_repair_clock_nets=True), "cts_repair_clock_nets", True),
        (PnrRunOverrides(post_cts_setup_repair=False), "post_cts_setup_repair", False),
        (PnrRunOverrides(routing_layer_adjustment=0.2), "routing", None),
        (PnrRunOverrides(port_buffer="BUF_X4"), "port_buffer", "BUF_X4"),
        (PnrRunOverrides(tapcell_tcl="/elsewhere/tap.tcl"), "tcl_hooks", None),
    ],
)
def test_a_set_override_changes_the_digest(tmp_path, overrides, key, value):
    platform = _platform(_pdk(tmp_path))
    plain = pnr_abstract.abstract_config(_digest_cfg(tmp_path), platform)
    over = pnr_abstract.abstract_config(_digest_cfg(tmp_path, overrides), platform)

    assert pnr_abstract.config_digest(over) != pnr_abstract.config_digest(plain)
    assert over[key] != plain.get(key)
    if value is not None:
        assert over[key] == value


def test_unset_overrides_and_platform_defaults_add_nothing_to_the_digest(tmp_path):
    platform = _platform(_pdk(tmp_path))
    config = pnr_abstract.abstract_config(_digest_cfg(tmp_path), platform)
    for key in ("cts_clock_nets", "cts_latency_balancing", "cts_repair_clock_nets"):
        assert key not in config
    # Explicit defaults on the run digest as unset ones do.
    explicit = PnrRunOverrides(
        cts_latency_balancing=True,
        cts_repair_clock_nets=False,
        post_cts_setup_repair=True,
    )
    assert pnr_abstract.config_digest(
        pnr_abstract.abstract_config(_digest_cfg(tmp_path, explicit), platform)
    ) == pnr_abstract.config_digest(config)


def test_a_platform_cts_knob_joins_the_digest(tmp_path):
    plain = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path), _platform(_pdk(tmp_path))
    )
    off = pnr_abstract.abstract_config(
        _digest_cfg(tmp_path),
        _platform(_pdk(tmp_path), cts_latency_balancing=False),
    )
    assert off["cts_latency_balancing"] is False
    assert pnr_abstract.config_digest(off) != pnr_abstract.config_digest(plain)


def test_the_pnr_yaml_keys_reach_the_digest(tmp_path):
    (tmp_path / "synth.yaml").write_text(
        dedent("""\
        rtl-buddy-filetype: synth_config
        syntheses: []
        """)
    )
    run = _run(tmp_path, harden=True, **{"cts-latency-balancing": False})
    config = pnr_abstract.abstract_config(run, _platform(_pdk(tmp_path)))
    assert config["cts_latency_balancing"] is False
