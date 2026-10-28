"""The CTS controls of `rb pnr`: `cts-sink-clustering` options, `cts-root-buffer`, `cts-obstruction-aware` and `cts-repair-clock-nets` with `max-wire-length`, on a platform and a run."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_pnr_checkpoints import _pdk, _pnr_cfg
from test_pnr_repair import _render
from test_pnr_run_overrides import (
    _digest_cfg,
    _platform,
    _resolved,
    _run,
    _run_backend,
)

from rtl_buddy.config.pnr import CHECKPOINT_STAGES
from rtl_buddy.config.pnr_platform import PnrRunOverrides
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy import logging_utils
from rtl_buddy.tools import pnr_abstract
from rtl_buddy.tools.pnr_openroad import OpenRoadPnr, _cts_obstruction_option

_PLAIN_CTS_CALL = (
    "clock_tree_synthesis \\\n"
    "    -root_buf $CTS_BUF \\\n"
    "    -buf_list $CTS_BUF \\\n"
    "    -sink_clustering_enable\n"
)


def _clustering(platform):
    return (
        platform.get_cts_sink_clustering(),
        platform.get_cts_sink_clustering_size(),
        platform.get_cts_sink_clustering_max_diameter(),
    )


def _repair(platform):
    return (
        platform.get_cts_repair_clock_nets(),
        platform.get_cts_repair_max_wire_length(),
    )


# -- parsing and precedence --------------------------------------------------------


def test_the_new_keys_default_to_today_s_flow(tmp_path):
    platform = _platform(_pdk(tmp_path))
    assert _clustering(platform) == (True, None, None)
    assert _repair(platform) == (False, None)
    assert platform.get_cts_root_buffer() is None
    assert platform.get_cts_obstruction_aware() is None


@pytest.mark.parametrize(
    ("platform_kw", "run_keys", "expected"),
    [
        ({}, {}, (True, None, None)),
        ({"cts_sink_clustering": False}, {}, (False, None, None)),
        ({"cts_sink_clustering": True}, {}, (True, None, None)),
        (
            {"cts_sink_clustering": {"size": 10, "max-diameter": 30}},
            {},
            (True, 10, 30.0),
        ),
        (
            {"cts_sink_clustering": {"enable": False, "size": 10}},
            {},
            (False, 10, None),
        ),
        ({}, {"cts-sink-clustering": False}, (False, None, None)),
        ({}, {"cts-sink-clustering": {}}, (True, None, None)),
        (
            {"cts_sink_clustering": False},
            {"cts-sink-clustering": {"size": 20, "max-diameter": 25}},
            (True, 20, 25.0),
        ),
        (
            {"cts_sink_clustering": {"size": 10, "max-diameter": 30}},
            {"cts-sink-clustering": True},
            (True, None, None),
        ),
        (
            {"cts_sink_clustering": {"size": 10, "max-diameter": 30}},
            {"cts-sink-clustering": {"max-diameter": 12.5}},
            (True, None, 12.5),
        ),
        (
            {"cts_sink_clustering": {"size": 10}},
            {"cts-sink-clustering": False},
            (False, None, None),
        ),
        (
            {"cts_sink_clustering": {"size": 10}},
            {"cts-sink-clustering": {"enable": False}},
            (False, None, None),
        ),
    ],
)
def test_a_run_s_sink_clustering_replaces_the_platform_s_whole(
    tmp_path, platform_kw, run_keys, expected
):
    platform = _platform(_pdk(tmp_path), **platform_kw)
    before = _clustering(platform)

    assert _clustering(_resolved(tmp_path, platform, **run_keys)) == expected
    assert _clustering(platform) == before


@pytest.mark.parametrize(
    ("platform_kw", "run_keys", "expected"),
    [
        ({"cts_repair_clock_nets": True}, {}, (True, None)),
        ({"cts_repair_clock_nets": {"max-wire-length": 1000}}, {}, (True, 1000.0)),
        ({"cts_repair_clock_nets": {}}, {}, (True, None)),
        ({}, {"cts-repair-clock-nets": {"max-wire-length": 1000}}, (True, 1000.0)),
        ({}, {"cts-repair-clock-nets": {"max-wire-length": 0.5}}, (True, 0.5)),
        (
            {"cts_repair_clock_nets": {"max-wire-length": 1000}},
            {"cts-repair-clock-nets": True},
            (True, None),
        ),
        (
            {"cts_repair_clock_nets": {"max-wire-length": 1000}},
            {"cts-repair-clock-nets": False},
            (False, None),
        ),
        (
            {"cts_repair_clock_nets": True},
            {"cts-repair-clock-nets": {"max-wire-length": 500}},
            (True, 500.0),
        ),
        (
            {"cts_repair_clock_nets": {"max-wire-length": 1000}},
            {"cts-repair-clock-nets": {"max-wire-length": 500}},
            (True, 500.0),
        ),
    ],
)
def test_a_run_s_repair_clock_nets_replaces_the_platform_s_whole(
    tmp_path, platform_kw, run_keys, expected
):
    platform = _platform(_pdk(tmp_path), **platform_kw)
    before = _repair(platform)

    assert _repair(_resolved(tmp_path, platform, **run_keys)) == expected
    assert _repair(platform) == before


@pytest.mark.parametrize(
    ("platform_kw", "run_keys", "getter", "expected"),
    [
        ({"cts_root_buffer": "ROOT"}, {}, "get_cts_root_buffer", "ROOT"),
        ({}, {"cts-root-buffer": "ROOT"}, "get_cts_root_buffer", "ROOT"),
        (
            {"cts_root_buffer": "ROOT"},
            {"cts-root-buffer": "BIG"},
            "get_cts_root_buffer",
            "BIG",
        ),
        ({"cts_obstruction_aware": False}, {}, "get_cts_obstruction_aware", False),
        ({}, {"cts-obstruction-aware": False}, "get_cts_obstruction_aware", False),
        (
            {"cts_obstruction_aware": False},
            {"cts-obstruction-aware": True},
            "get_cts_obstruction_aware",
            True,
        ),
        (
            {"cts_obstruction_aware": True},
            {"cts-obstruction-aware": False},
            "get_cts_obstruction_aware",
            False,
        ),
    ],
)
def test_a_run_value_wins_over_the_platform_for_the_new_keys(
    tmp_path, platform_kw, run_keys, getter, expected
):
    platform = _platform(_pdk(tmp_path), **platform_kw)
    before = getattr(platform, getter)()

    assert getattr(_resolved(tmp_path, platform, **run_keys), getter)() == expected
    assert getattr(platform, getter)() == before


def test_a_run_that_leaves_the_new_keys_unset_keeps_the_platform_s(tmp_path):
    platform = _platform(
        _pdk(tmp_path),
        cts_sink_clustering={"size": 10, "max-diameter": 30},
        cts_repair_clock_nets={"max-wire-length": 800},
        cts_root_buffer="ROOT",
        cts_obstruction_aware=False,
    )

    resolved = _resolved(tmp_path, platform, **{"cts-latency-balancing": False})

    assert _clustering(resolved) == (True, 10, 30.0)
    assert _repair(resolved) == (True, 800.0)
    assert resolved.get_cts_root_buffer() == "ROOT"
    assert resolved.get_cts_obstruction_aware() is False


# -- validation --------------------------------------------------------------------

_BAD_VALUES = [
    ({"cts-sink-clustering": "yes"}, "cts-sink-clustering must be true, false or a"),
    ({"cts-sink-clustering": 1}, "cts-sink-clustering must be true, false or a"),
    ({"cts-sink-clustering": [20]}, "cts-sink-clustering must be true, false or a"),
    (
        {"cts-sink-clustering": {"enable": "no"}},
        "cts-sink-clustering.enable must be true or false",
    ),
    (
        {"cts-sink-clustering": {"size": 0}},
        "cts-sink-clustering.size must be a positive integer",
    ),
    (
        {"cts-sink-clustering": {"size": 2.5}},
        "cts-sink-clustering.size must be a positive integer",
    ),
    (
        {"cts-sink-clustering": {"size": True}},
        "cts-sink-clustering.size must be a positive integer",
    ),
    (
        {"cts-sink-clustering": {"size": "20"}},
        "cts-sink-clustering.size must be a positive integer",
    ),
    (
        {"cts-sink-clustering": {"max-diameter": 0}},
        "cts-sink-clustering.max-diameter must be a positive number of microns",
    ),
    (
        {"cts-sink-clustering": {"max-diameter": -3}},
        "cts-sink-clustering.max-diameter must be a positive number of microns",
    ),
    (
        {"cts-sink-clustering": {"max-diameter": "25"}},
        "cts-sink-clustering.max-diameter must be a positive number of microns",
    ),
    (
        {"cts-sink-clustering": {"max-diameter": True}},
        "cts-sink-clustering.max-diameter must be a positive number of microns",
    ),
    (
        {"cts-sink-clustering": {"max-diameter": float("inf")}},
        "cts-sink-clustering.max-diameter must be a positive number of microns",
    ),
    (
        {"cts-sink-clustering": {"max-diameter": 10**1000}},
        "cts-sink-clustering.max-diameter must be a positive number of microns",
    ),
    (
        {"cts-repair-clock-nets": {"max-wire-length": 10**1000}},
        "cts-repair-clock-nets.max-wire-length must be a positive number of microns",
    ),
    ({"cts-repair-clock-nets": 5}, "cts-repair-clock-nets must be true, false or a"),
    (
        {"cts-repair-clock-nets": "yes"},
        "cts-repair-clock-nets must be true, false or a",
    ),
    (
        {"cts-repair-clock-nets": {"max-wire-length": 0}},
        "cts-repair-clock-nets.max-wire-length must be a positive number of microns",
    ),
    (
        {"cts-repair-clock-nets": {"max-wire-length": -1}},
        "cts-repair-clock-nets.max-wire-length must be a positive number of microns",
    ),
    (
        {"cts-repair-clock-nets": {"max-wire-length": "1000"}},
        "cts-repair-clock-nets.max-wire-length must be a positive number of microns",
    ),
    ({"cts-root-buffer": "A B"}, "cts-root-buffer must be one buffer cell name"),
    ({"cts-root-buffer": ""}, "cts-root-buffer must be one buffer cell name"),
    ({"cts-root-buffer": ["A"]}, "cts-root-buffer must be one buffer cell name"),
    ({"cts-obstruction-aware": "no"}, "cts-obstruction-aware must be true or false"),
    ({"cts-obstruction-aware": 1}, "cts-obstruction-aware must be true or false"),
]


@pytest.mark.parametrize(("keys", "message"), _BAD_VALUES)
def test_a_bad_run_value_is_refused_naming_the_run_and_the_key(tmp_path, keys, message):
    with pytest.raises(FatalRtlBuddyError) as exc:
        _run(tmp_path, **keys)
    assert f"pnr run 'r': {message}" in str(exc.value)


@pytest.mark.parametrize(("keys", "message"), _BAD_VALUES)
def test_a_bad_platform_value_is_refused_naming_the_platform_and_the_key(
    tmp_path, keys, message
):
    kw = {k.replace("-", "_"): v for k, v in keys.items()}
    with pytest.raises(FatalRtlBuddyError) as exc:
        _platform(_pdk(tmp_path), **kw)
    assert f"pnr platform 'pl': {message}" in str(exc.value)


def test_a_misspelt_key_in_the_sink_clustering_block_is_refused(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match=r"did you mean 'max-diameter'\?"):
        _run(tmp_path, **{"cts-sink-clustering": {"max-diametre": 25}})


def test_a_misspelt_key_in_the_repair_block_is_refused(tmp_path):
    with pytest.raises(FatalRtlBuddyError, match=r"did you mean 'max-wire-length'\?"):
        _run(tmp_path, **{"cts-repair-clock-nets": {"max-wire-lenght": 1000}})


# -- rendered flow -----------------------------------------------------------------


def test_leaving_every_new_key_unset_renders_the_original_cts_flow(tmp_path):
    single = _render(tmp_path, _platform(_pdk(tmp_path), cts_buffer="BUF_X4"))
    listed = _render(tmp_path, _platform(_pdk(tmp_path), cts_buffer=["A", "B"]))

    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf $CTS_BUF \\\n"
        "    -buf_list $CTS_BUF \\\n"
        "    -sink_clustering_enable\n"
    ) in single
    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf [lindex $CTS_BUF 0] \\\n"
        "    -buf_list $CTS_BUF \\\n"
        "    -sink_clustering_enable\n"
    ) in listed
    assert "repair_clock_nets" not in single
    assert "obstruction_aware" not in single


def test_explicit_defaults_render_the_original_cts_flow(tmp_path):
    plain = _render(tmp_path, _platform(_pdk(tmp_path), cts_buffer="BUF_X4"))
    explicit = _render(
        tmp_path,
        _platform(
            _pdk(tmp_path),
            cts_buffer="BUF_X4",
            cts_sink_clustering={"enable": True},
            cts_repair_clock_nets=False,
        ),
    )

    assert explicit == plain


def test_a_plain_true_repair_renders_repair_clock_nets_without_options(tmp_path):
    text = _render(tmp_path, _platform(_pdk(tmp_path), cts_repair_clock_nets=True))

    assert (
        'puts ">>> Repair clock nets"\n'
        "estimate_parasitics -placement\n"
        "repair_clock_nets\n"
    ) in text


def test_a_repair_mapping_renders_the_wire_length_cap(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path)),
        **{"cts-repair-clock-nets": {"max-wire-length": 1000}},
    )

    text = _render(tmp_path, platform)

    assert (
        'puts ">>> Repair clock nets"\n'
        "estimate_parasitics -placement\n"
        "repair_clock_nets -max_wire_length 1000\n"
    ) in text
    assert text.count("repair_clock_nets") == 1


def test_a_fractional_wire_length_renders_as_written(tmp_path):
    platform = _platform(
        _pdk(tmp_path), cts_repair_clock_nets={"max-wire-length": 1234.5}
    )

    assert "repair_clock_nets -max_wire_length 1234.5\n" in _render(tmp_path, platform)


@pytest.mark.parametrize(
    ("clustering", "options"),
    [
        (True, "-sink_clustering_enable"),
        ({}, "-sink_clustering_enable"),
        (
            {"size": 20},
            "-sink_clustering_enable -sink_clustering_size 20",
        ),
        (
            {"max-diameter": 25},
            "-sink_clustering_enable -sink_clustering_max_diameter 25",
        ),
        (
            {"size": 20, "max-diameter": 25},
            "-sink_clustering_enable -sink_clustering_size 20 "
            "-sink_clustering_max_diameter 25",
        ),
        (
            {"size": 20, "max-diameter": 12.5},
            "-sink_clustering_enable -sink_clustering_size 20 "
            "-sink_clustering_max_diameter 12.5",
        ),
        (False, ""),
        ({"enable": False}, ""),
        ({"enable": False, "size": 20, "max-diameter": 25}, ""),
    ],
)
def test_sink_clustering_renders_its_options(tmp_path, clustering, options):
    platform = _platform(
        _pdk(tmp_path), cts_buffer="BUF_X4", cts_sink_clustering=clustering
    )

    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf $CTS_BUF \\\n"
        "    -buf_list $CTS_BUF \\\n"
        f"    {options}\n"
    ) in _render(tmp_path, platform)


def test_a_run_s_sink_clustering_reaches_the_cts_call(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path), cts_buffer="BUF_X4"),
        **{"cts-sink-clustering": {"size": 20, "max-diameter": 25}},
    )

    assert (
        "    -sink_clustering_enable -sink_clustering_size 20 "
        "-sink_clustering_max_diameter 25\n"
    ) in _render(tmp_path, platform)


@pytest.mark.parametrize(
    ("buffers", "set_line"),
    [
        ("BUF_X4", "set CTS_BUF         BUF_X4\n"),
        (["BUF_X4", "BUF_X8"], "set CTS_BUF         {BUF_X4 BUF_X8}\n"),
    ],
)
def test_a_root_buffer_is_the_root_and_cts_buffer_is_the_branch_list(
    tmp_path, buffers, set_line
):
    platform = _platform(_pdk(tmp_path), cts_buffer=buffers, cts_root_buffer="BUF_X16")

    text = _render(tmp_path, platform)

    assert set_line in text
    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf BUF_X16 \\\n"
        "    -buf_list $CTS_BUF \\\n"
        "    -sink_clustering_enable\n"
    ) in text


def test_a_run_s_root_buffer_reaches_the_cts_call(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path), cts_buffer=["A", "B"]),
        **{"cts-root-buffer": "BIG"},
    )

    assert "    -root_buf BIG \\\n    -buf_list $CTS_BUF \\\n" in _render(
        tmp_path, platform
    )


def test_a_root_buffer_is_not_checked_against_the_dont_use_lists(tmp_path):
    platform = _platform(
        _pdk(tmp_path),
        cts_buffer="BUF_X4",
        cts_root_buffer="BUF_X16",
        cts_dont_use_cells=["BUF_X16"],
    )

    assert "    -root_buf BUF_X16 \\\n" in _render(tmp_path, platform)


@pytest.mark.parametrize(
    ("aware", "has_flag", "option"),
    [
        (None, True, ""),
        (None, False, ""),
        (False, True, " -no_obstruction_aware"),
        (True, True, ""),
        (True, False, " -obstruction_aware"),
    ],
)
def test_obstruction_awareness_renders_what_the_openroad_takes(
    tmp_path, aware, has_flag, option
):
    platform = _platform(_pdk(tmp_path), cts_obstruction_aware=aware)

    assert _cts_obstruction_option(platform, has_flag) == option


def test_obstruction_aware_false_renders_the_flag_last(tmp_path):
    platform = _platform(
        _pdk(tmp_path),
        cts_buffer="BUF_X4",
        cts_apply_ndr="none",
        cts_latency_balancing=False,
        cts_obstruction_aware=False,
    )

    assert (
        "    -sink_clustering_enable -apply_ndr none -no_insertion_delay "
        "-no_obstruction_aware\n"
    ) in _render(tmp_path, platform)


def test_obstruction_aware_true_adds_no_flag_on_a_default_on_openroad(tmp_path):
    platform = _platform(
        _pdk(tmp_path), cts_buffer="BUF_X4", cts_obstruction_aware=True
    )

    assert _PLAIN_CTS_CALL in _render(tmp_path, platform)


def test_every_new_key_renders_into_one_cts_call(tmp_path):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path), cts_buffer=["BUF_X2", "BUF_X4"]),
        **{
            "cts-sink-clustering": {"size": 20, "max-diameter": 25},
            "cts-root-buffer": "BUF_X16",
            "cts-obstruction-aware": False,
            "cts-repair-clock-nets": {"max-wire-length": 1000},
        },
    )

    text = _render(tmp_path, platform)

    assert (
        "clock_tree_synthesis \\\n"
        "    -root_buf BUF_X16 \\\n"
        "    -buf_list $CTS_BUF \\\n"
        "    -sink_clustering_enable -sink_clustering_size 20 "
        "-sink_clustering_max_diameter 25 -no_obstruction_aware\n"
        'puts ">>> Repair clock nets"\n'
        "estimate_parasitics -placement\n"
        "repair_clock_nets -max_wire_length 1000\n"
    ) in text


@pytest.mark.parametrize("kind", ["checkpoints", "stages"])
def test_the_new_options_reach_the_stage_scripts(tmp_path, kind):
    platform = _resolved(
        tmp_path,
        _platform(_pdk(tmp_path), cts_buffer="BUF_X4"),
        **{
            "cts-sink-clustering": {"size": 20, "max-diameter": 25},
            "cts-root-buffer": "BUF_X16",
            "cts-obstruction-aware": False,
            "cts-repair-clock-nets": {"max-wire-length": 1000},
        },
    )
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
    backend._ckpt_run_dir = str(tmp_path / "ck" / "run")
    os.makedirs(backend._ckpt_run_dir, exist_ok=True)
    script = Path(backend._write_script(platform, pnr_cfg.get_floorplan()))

    scripts = [script.read_text()] + [
        p.read_text() for p in Path(backend._ckpt_run_dir).glob("*.tcl")
    ]
    cts = [t for t in scripts if "clock_tree_synthesis \\\n    -root_buf" in t]

    assert cts
    for text in cts:
        assert "-root_buf BUF_X16" in text
        assert (
            "-sink_clustering_size 20 -sink_clustering_max_diameter 25 "
            "-no_obstruction_aware"
        ) in text
        assert "repair_clock_nets -max_wire_length 1000\n" in text


# -- setup: the OpenROAD the obstruction flag needs ----------------------------------


def _cts_script(backend) -> str:
    return Path(backend._script_path()).read_text()


@pytest.mark.parametrize(
    ("aware", "has_flag", "option"),
    [
        (False, True, " -no_obstruction_aware"),
        (True, True, None),
        (True, False, " -obstruction_aware"),
    ],
)
def test_a_run_asks_openroad_which_obstruction_flag_it_takes(
    tmp_path, monkeypatch, aware, has_flag, option
):
    backend = _run_backend(
        tmp_path, monkeypatch, PnrRunOverrides(cts_obstruction_aware=aware)
    )
    asked = []
    monkeypatch.setattr(
        backend,
        "_has_command_option",
        lambda command, flag: asked.append((command, flag)) or has_flag,
    )

    backend.run()

    assert asked == [("clock_tree_synthesis", "-no_obstruction_aware")]
    text = _cts_script(backend)
    if option is None:
        assert "obstruction_aware" not in text
    else:
        assert f"{option}\n" in text


def test_a_run_without_the_key_does_not_probe_openroad_for_it(tmp_path, monkeypatch):
    backend = _run_backend(tmp_path, monkeypatch, PnrRunOverrides())
    monkeypatch.setattr(
        backend,
        "_has_command_option",
        lambda *_a: pytest.fail("probed without cts-obstruction-aware"),
    )

    backend.run()

    assert "obstruction_aware" not in _cts_script(backend)


def test_obstruction_aware_false_on_an_openroad_without_the_flag_fails_setup(
    tmp_path, monkeypatch
):
    backend = _run_backend(
        tmp_path, monkeypatch, PnrRunOverrides(cts_obstruction_aware=False)
    )
    monkeypatch.setattr(backend, "_has_command_option", lambda *_a: False)

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert res.results["desc"] == (
        "cts-obstruction-aware: false needs a clock_tree_synthesis with "
        "-no_obstruction_aware; OpenROAD 26Q2-stub has none"
    )
    assert not Path(backend._script_path()).exists()


@pytest.mark.parametrize(
    ("output", "found"),
    [
        ("clock_tree_synthesis [-buf_list buflist] [-no_obstruction_aware]\n", True),
        ("clock_tree_synthesis [-buf_list buflist] [-obstruction_aware]\n", False),
        ("clock_tree_synthesis [-buf_list buflist]\n", False),
        ("", False),
    ],
)
def test_the_option_probe_reads_openroad_s_help(tmp_path, monkeypatch, output, found):
    import subprocess

    from rtl_buddy.tools import pnr_openroad

    backend = _run_backend(tmp_path, monkeypatch, PnrRunOverrides())
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"], seen["input"] = list(cmd), kwargs["input"]
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(pnr_openroad.subprocess, "run", _fake_run)

    assert (
        backend._has_command_option("clock_tree_synthesis", "-no_obstruction_aware")
        is found
    )
    assert seen["cmd"][-2:] == ["-no_init", "-exit"]
    assert seen["input"] == "help clock_tree_synthesis\nexit\n"
    assert seen["cwd"] == backend.artefact_dir


def test_an_openroad_without_the_flag_logs_why_the_run_stopped():
    message = logging_utils._human_message(
        "pnr.cts_obstruction_aware_unsupported",
        {"pnr": "blk", "exe": "/opt/openroad", "version": "25Q1"},
    )

    assert message == (
        'P&R "blk": cts-obstruction-aware: false needs clock_tree_synthesis '
        "-no_obstruction_aware, which OpenROAD 25Q1 does not take; unset the key "
        "or use a newer OpenROAD"
    )


# -- configuration digest ----------------------------------------------------------


def _config(tmp_path, overrides=PnrRunOverrides(), **platform_kw):
    return pnr_abstract.abstract_config(
        _digest_cfg(tmp_path, overrides), _platform(_pdk(tmp_path), **platform_kw)
    )


@pytest.mark.parametrize(
    ("overrides", "key", "value"),
    [
        (PnrRunOverrides(cts_sink_clustering=False), "cts_sink_clustering", False),
        (
            PnrRunOverrides(
                cts_sink_clustering=True,
                cts_sink_clustering_size=20,
                cts_sink_clustering_max_diameter=25.0,
            ),
            "cts_sink_clustering",
            {"size": 20, "max_diameter": 25.0},
        ),
        (
            PnrRunOverrides(cts_sink_clustering=True, cts_sink_clustering_size=20),
            "cts_sink_clustering",
            {"size": 20},
        ),
        (PnrRunOverrides(cts_root_buffer="BIG"), "cts_root_buffer", "BIG"),
        (
            PnrRunOverrides(cts_obstruction_aware=False),
            "cts_obstruction_aware",
            False,
        ),
        (
            PnrRunOverrides(cts_obstruction_aware=True),
            "cts_obstruction_aware",
            True,
        ),
        (
            PnrRunOverrides(
                cts_repair_clock_nets=True, cts_repair_max_wire_length=1000.0
            ),
            "cts_repair_clock_nets",
            {"max_wire_length": 1000.0},
        ),
    ],
)
def test_a_new_run_value_changes_the_digest(tmp_path, overrides, key, value):
    plain = _config(tmp_path)
    over = _config(tmp_path, overrides)

    assert key not in plain
    assert over[key] == value
    assert pnr_abstract.config_digest(over) != pnr_abstract.config_digest(plain)


@pytest.mark.parametrize(
    ("platform_kw", "key", "value"),
    [
        ({"cts_sink_clustering": False}, "cts_sink_clustering", False),
        (
            {"cts_sink_clustering": {"size": 20, "max-diameter": 25}},
            "cts_sink_clustering",
            {"size": 20, "max_diameter": 25.0},
        ),
        ({"cts_root_buffer": "BIG"}, "cts_root_buffer", "BIG"),
        ({"cts_obstruction_aware": False}, "cts_obstruction_aware", False),
        (
            {"cts_repair_clock_nets": {"max-wire-length": 1000}},
            "cts_repair_clock_nets",
            {"max_wire_length": 1000.0},
        ),
    ],
)
def test_a_new_platform_value_changes_the_digest(tmp_path, platform_kw, key, value):
    plain = _config(tmp_path)
    over = _config(tmp_path, **platform_kw)

    assert over[key] == value
    assert pnr_abstract.config_digest(over) != pnr_abstract.config_digest(plain)


def test_a_wire_length_cap_digests_apart_from_a_plain_repair(tmp_path):
    plain = _config(tmp_path, cts_repair_clock_nets=True)
    capped = _config(tmp_path, cts_repair_clock_nets={"max-wire-length": 1000})
    other = _config(tmp_path, cts_repair_clock_nets={"max-wire-length": 500})

    assert plain["cts_repair_clock_nets"] is True
    digests = {pnr_abstract.config_digest(c) for c in (plain, capped, other)}
    assert len(digests) == 3


def test_unset_and_default_values_add_nothing_to_the_digest(tmp_path):
    plain = _config(tmp_path)
    for key in (
        "cts_sink_clustering",
        "cts_root_buffer",
        "cts_obstruction_aware",
        "cts_repair_clock_nets",
    ):
        assert key not in plain
    explicit = _config(
        tmp_path,
        PnrRunOverrides(cts_sink_clustering=True, cts_repair_clock_nets=False),
        cts_sink_clustering={"enable": True},
    )
    assert pnr_abstract.config_digest(explicit) == pnr_abstract.config_digest(plain)


def test_the_pnr_yaml_keys_reach_the_digest(tmp_path):
    (tmp_path / "synth.yaml").write_text(
        "rtl-buddy-filetype: synth_config\nsyntheses: []\n"
    )
    run = _run(
        tmp_path,
        harden=True,
        **{
            "cts-sink-clustering": {"size": 20, "max-diameter": 25},
            "cts-root-buffer": "BIG",
            "cts-obstruction-aware": False,
            "cts-repair-clock-nets": {"max-wire-length": 1000},
        },
    )

    config = pnr_abstract.abstract_config(run, _platform(_pdk(tmp_path)))

    assert config["cts_sink_clustering"] == {"size": 20, "max_diameter": 25.0}
    assert config["cts_root_buffer"] == "BIG"
    assert config["cts_obstruction_aware"] is False
    assert config["cts_repair_clock_nets"] == {"max_wire_length": 1000.0}
