"""Per-corner macro Liberty: a `lib-paths` entry that maps each platform corner to its own Liberty.

Covered here: loading the mapping, the setup check against the platform's corners, the `rb pnr` Tcl with and without an SDC mode per corner, the resume identity, the Liberty time-unit check, and `rb power`. A single path keeps the Tcl it had before mappings existed.
"""

import logging
import re
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
from test_liberty_units import _lib
from test_multi_corner import _PNR_LOG_TAIL, _pnr_backend_over
from test_pnr_corner_modes import (
    _CORNERS,
    _cfg,
    _pdk,
    _platform,
    _render,
    _setup_backend,
)
from test_power import _FakePlatform, _make_power_backend

from rtl_buddy.config.lib_paths import (
    fit_lib_paths,
    lib_paths_overlap,
    lib_paths_problem,
)
from rtl_buddy.config.pnr import PnrSuiteConfig
from rtl_buddy.config.power import PowerSuiteConfig
from rtl_buddy.errors import FatalRtlBuddyError


def _macro_libs(tmp_path, *names):
    paths = []
    for name in names:
        path = tmp_path / "macros" / f"{name}.lib"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'library ({name}) {{ time_unit : "1ns" ; }}\n')
        paths.append(str(path))
    return paths


def _per_corner(tmp_path):
    """One macro with a Liberty per corner of `_CORNERS`."""
    return dict(zip(_CORNERS, _macro_libs(tmp_path, *(f"m_{c}" for c in _CORNERS))))


def _liberty_section(text: str) -> list[str]:
    """The Liberty, LEF and scene lines between the Liberty read and the netlist read."""
    start = text.index('puts ">>> Reading Liberty + LEF"')
    end = text.index('puts ">>> Reading netlist"')
    return [
        line
        for line in text[start:end].splitlines()
        if re.match(
            r"(read_liberty|read_lef|define_corners|define_scene|set_mode) ", line
        )
    ]


# ── config ─────────────────────────────────────────────────────────────────


def _load_pnr(tmp_path, lib_paths: str):
    path = tmp_path / "pnr.yaml"
    path.write_text(
        dedent(f"""\
        rtl-buddy-filetype: pnr_config
        runs:
          - name: r
            desc: d
            synth: s
            synth-path: synth.yaml
            platform: p
            constraints: top.sdc
            lib-paths: {lib_paths}
        """)
    )
    return PnrSuiteConfig(str(path)).get_runs("r")[0]


def test_lib_paths_takes_paths_and_corner_mappings_resolved_like_paths(tmp_path):
    cfg = _load_pnr(tmp_path, "[m/one.lib, {ss: m/ss.lib, tt: ../tt.lib}]")

    one = str(tmp_path / "m" / "one.lib")
    ss = str(tmp_path / "m" / "ss.lib")
    tt = str((tmp_path / ".." / "tt.lib").resolve(strict=False))
    assert cfg.get_lib_path_entries() == [one, {"ss": ss, "tt": tt}]
    assert cfg.get_lib_paths() == [one, ss, tt]


def test_power_lib_paths_takes_a_corner_mapping(tmp_path):
    path = tmp_path / "power.yaml"
    path.write_text(
        dedent("""\
        rtl-buddy-filetype: power_config
        runs:
          - name: p
            desc: d
            synth: s
            synth-path: synth.yaml
            constraints: top.sdc
            platform: mc
            lib-paths:
              - {tt: m/tt.lib, ss: m/ss.lib}
        """)
    )
    cfg = PowerSuiteConfig(str(path)).get_runs("p")[0]

    entry = {"tt": str(tmp_path / "m" / "tt.lib"), "ss": str(tmp_path / "m" / "ss.lib")}
    assert cfg.get_lib_path_entries() == [entry]
    assert cfg.get_lib_paths() == list(entry.values())


@pytest.mark.parametrize(
    "value, match",
    [
        ("[{}]", r"lib-paths\[0\] must be a Liberty path or a mapping"),
        ("[a.lib, '']", r"lib-paths\[1\] must be a Liberty path or a mapping"),
        ("[{ss: ''}]", r"lib-paths\[0\]: corner 'ss' must map to a Liberty path"),
        ("[{ss: [a.lib]}]", r"lib-paths\[0\]: corner 'ss' must map to a Liberty path"),
    ],
)
def test_lib_paths_rejects_an_entry_without_a_path(tmp_path, value, match):
    with pytest.raises(FatalRtlBuddyError, match=match):
        _load_pnr(tmp_path, value)


def test_a_mapping_must_name_every_corner_and_no_other():
    entries = ["/m/one.lib", {"tt": "/a", "sss": "/b"}]

    problem = lib_paths_problem(entries, _CORNERS)

    assert "lib-paths[1] has no Liberty for corner(s) ss, ff" in problem
    assert (
        "lib-paths[1] names corner(s) sss not among the platform's (tt, ss, ff)"
        in problem
    )
    assert (
        lib_paths_problem(["/m/one.lib", dict.fromkeys(_CORNERS, "/a")], _CORNERS)
        is None
    )
    assert lib_paths_problem([{"tt": "/a"}], ["ss"]) == (
        "lib-paths[0] has no Liberty for corner(s) ss; lib-paths[0] names corner(s) "
        "tt not among the platform's (ss)"
    )


def test_fitting_orders_a_mapping_by_corner_and_collapses_one_corner():
    entries = ["/m/one.lib", {"ff": "/f", "tt": "/t", "ss": "/s"}]

    assert fit_lib_paths(entries, _CORNERS) == [
        "/m/one.lib",
        {"tt": "/t", "ss": "/s", "ff": "/f"},
    ]
    assert fit_lib_paths(["/m/one.lib", {"ss": "/s"}], ["ss"]) == ["/m/one.lib", "/s"]


def test_a_mapping_may_name_corners_the_platform_lacks_when_told_to_ignore_them():
    entries = [{"tt": "/t", "ss": "/s", "ff": "/f"}]

    assert lib_paths_problem(entries, ["tt"], ignore_unknown=True) is None
    assert lib_paths_problem(entries, ["tt", "ss"], ignore_unknown=True) is None
    assert lib_paths_problem(entries, ["typ"], ignore_unknown=True) == (
        "lib-paths[0] has no Liberty for corner(s) typ"
    )
    assert fit_lib_paths(entries, ["tt"]) == ["/t"]
    assert fit_lib_paths(entries, ["ff", "tt"]) == [{"ff": "/f", "tt": "/t"}]


def test_a_plain_path_that_a_mapping_also_names_is_an_overlap(tmp_path):
    (one, other) = _macro_libs(tmp_path, "one", "other")
    mapped = {"tt": one, "ss": other}
    respelled = str(Path(one).parent / ".." / "macros" / Path(one).name)

    assert lib_paths_overlap([one, mapped]) is not None
    assert lib_paths_overlap([mapped, respelled]) == (
        f"{one} is listed both as a plain lib-paths entry and in a per-corner "
        "mapping; drop the plain entry"
    )
    assert lib_paths_overlap([mapped, "/elsewhere/x.lib", dict(mapped)]) is None


# ── rb pnr setup ───────────────────────────────────────────────────────────


def _pnr_setup(tmp_path, platform, lib_paths, *, modes=True):
    backend, probes = _setup_backend(
        tmp_path, platform, str(tmp_path / "top.sdc"), modes=modes
    )
    backend.pnr_cfg = replace(backend.pnr_cfg, lib_paths=lib_paths)
    return backend, probes


def test_a_mapping_that_misses_a_corner_fails_setup_naming_it(tmp_path, caplog):
    platform = _platform(_pdk(tmp_path))
    per_corner = _per_corner(tmp_path)
    del per_corner["ff"]
    backend, probes = _pnr_setup(tmp_path, platform, [per_corner])

    with caplog.at_level(logging.ERROR):
        failed = backend._check_corner_constraints(platform, "26Q4")

    assert failed.results["fail_stage"] == "setup"
    assert failed.results["desc"] == "lib-paths[0] has no Liberty for corner(s) ff"
    assert not probes
    assert (
        'P&R "demo_pnr": lib-paths[0] has no Liberty for corner(s) ff; a per-corner '
        'lib-paths mapping names each corner of platform "mc" and no other'
    ) in caplog.text


def test_a_mapping_that_names_an_unknown_corner_fails_setup(tmp_path):
    platform = _platform(_pdk(tmp_path))
    backend, _ = _pnr_setup(
        tmp_path, platform, [{**_per_corner(tmp_path), "fast": "/m/fast.lib"}]
    )

    failed = backend._check_corner_constraints(platform, "26Q4")

    assert "names corner(s) fast not among the platform's" in failed.results["desc"]


def test_a_single_corner_mapping_must_name_that_corner(tmp_path):
    platform = _platform(_pdk(tmp_path), corners=["ss"])
    backend, _ = _pnr_setup(tmp_path, platform, [_per_corner(tmp_path)])

    failed = backend._check_corner_constraints(platform, "26Q4")

    assert (
        "names corner(s) tt, ff not among the platform's (ss)"
        in (failed.results["desc"])
    )


def test_a_single_corner_mapping_is_its_one_path(tmp_path):
    platform = _platform(_pdk(tmp_path), corners=["ss"])
    (ss,) = _macro_libs(tmp_path, "m_ss")
    backend, _ = _pnr_setup(tmp_path, platform, ["/m/one.lib", {"ss": ss}])

    assert backend._check_corner_constraints(platform, "26Q4") is None

    assert backend.pnr_cfg.get_lib_path_entries() == ["/m/one.lib", ss]


def test_a_file_listed_plain_and_in_a_mapping_fails_setup(tmp_path, caplog):
    platform = _platform(_pdk(tmp_path))
    per_corner = _per_corner(tmp_path)
    respelled = str(tmp_path / "macros" / ".." / "macros" / "m_tt.lib")
    backend, probes = _pnr_setup(tmp_path, platform, [per_corner, respelled])

    with caplog.at_level(logging.ERROR):
        failed = backend._check_corner_constraints(platform, "26Q4")

    assert failed.results["fail_stage"] == "setup"
    assert failed.results["desc"] == (
        f"{per_corner['tt']} is listed both as a plain lib-paths entry and in a "
        "per-corner mapping; drop the plain entry"
    )
    assert not probes
    assert any(
        getattr(r, "rtl_event", None) == "pnr.lib_paths_duplicate"
        for r in caplog.records
    )


def test_a_plain_path_beside_a_mapping_of_other_files_is_accepted(tmp_path):
    platform = _platform(_pdk(tmp_path))
    (one,) = _macro_libs(tmp_path, "one")
    backend, _ = _pnr_setup(tmp_path, platform, [one, _per_corner(tmp_path)])

    assert backend._check_corner_constraints(platform, "26Q4") is None


def test_a_mapping_is_put_in_corner_order_at_setup(tmp_path):
    platform = _platform(_pdk(tmp_path))
    per_corner = _per_corner(tmp_path)
    backend, _ = _pnr_setup(tmp_path, platform, [dict(reversed(per_corner.items()))])

    assert backend._check_corner_constraints(platform, "26Q4") is None

    assert list(backend.pnr_cfg.get_lib_path_entries()[0]) == _CORNERS


def test_the_time_unit_check_reads_every_per_corner_file(tmp_path, monkeypatch, caplog):
    platform = _platform(_pdk(tmp_path))
    backend = _pnr_backend_over(tmp_path, monkeypatch, platform, _PNR_LOG_TAIL)
    per_corner = {
        "tt": _lib(tmp_path / "m" / "tt.lib", "1ns"),
        "ss": _lib(tmp_path / "m" / "ss.lib", "1ps"),
        "ff": _lib(tmp_path / "m" / "ff.lib", "1ns"),
    }
    backend.pnr_cfg = replace(backend.pnr_cfg, lib_paths=[per_corner])
    monkeypatch.setattr(
        "rtl_buddy.tools.pnr_openroad.subprocess.run",
        MagicMock(side_effect=AssertionError("openroad must not start")),
    )

    with caplog.at_level(logging.ERROR):
        res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "disagree on time_unit" in res.results["desc"]
    (event,) = [
        r
        for r in caplog.records
        if getattr(r, "rtl_event", None) == "pnr.liberty_time_unit_error"
    ]
    assert event.rtl_fields["units"][per_corner["ss"]] == "1ps"


def test_a_hardened_block_digests_a_one_corner_mapping_as_its_path(tmp_path):
    from rtl_buddy.tools.pnr_abstract import abstract_config

    platform = _platform(_pdk(tmp_path), corners=["tt"])
    (tt,) = _macro_libs(tmp_path, "m_tt")

    plain = abstract_config(_cfg(tmp_path, lib_paths=[tt]), platform)
    mapped = abstract_config(_cfg(tmp_path, lib_paths=[{"tt": tt}]), platform)

    assert mapped == plain


# ── rb pnr Tcl ─────────────────────────────────────────────────────────────


def test_each_scene_times_its_corners_macro_liberty(tmp_path):
    platform = _platform(_pdk(tmp_path))
    (one,) = _macro_libs(tmp_path, "one")
    per_corner = _per_corner(tmp_path)
    lib = {c: str(tmp_path / "pdk" / "lib" / f"{c}.lib") for c in _CORNERS}

    section = _liberty_section(_render(tmp_path, platform, lib_paths=[one, per_corner]))

    assert section == [
        *(f"read_liberty {lib[c]}" for c in _CORNERS),
        "read_lef     $TECH_LEF",
        "read_lef     $MACRO_LEF",
        f"read_liberty {one}",
        *(f"read_liberty {per_corner[c]}" for c in _CORNERS),
        *(
            line
            for c in _CORNERS
            for line in (
                f"set_mode {c}",
                f"define_scene {c} -mode {c} -liberty {{{lib[c]} {one} {per_corner[c]}}}",
            )
        ),
        "set_mode tt",
    ]


def test_a_file_two_corners_share_is_read_once(tmp_path):
    platform = _platform(_pdk(tmp_path))
    slow, fast = _macro_libs(tmp_path, "slow", "fast")

    section = _liberty_section(
        _render(tmp_path, platform, lib_paths=[{"tt": slow, "ss": slow, "ff": fast}])
    )

    assert [
        line for line in section if "macros" in line and "define_scene" not in line
    ] == [
        f"read_liberty {slow}",
        f"read_liberty {fast}",
    ]


def test_without_modes_each_file_is_read_into_its_corner_only(tmp_path):
    platform = _platform(_pdk(tmp_path))
    (one,) = _macro_libs(tmp_path, "one")
    per_corner = _per_corner(tmp_path)

    section = _liberty_section(
        _render(tmp_path, platform, modes=False, lib_paths=[one, per_corner])
    )

    assert [line for line in section if "macros" in line] == [
        *(f"read_liberty -corner {c} {one}" for c in _CORNERS),
        *(f"read_liberty -corner {c} {per_corner[c]}" for c in _CORNERS),
    ]


@pytest.mark.parametrize(
    "corners, modes", [(["ss"], True), (_CORNERS, True), (_CORNERS, False)]
)
def test_single_paths_render_as_before_per_corner_liberty(tmp_path, corners, modes):
    """The macro Liberty lines a single path gives, as before mappings existed; a mapping that names one file for every corner renders the same script."""
    platform = _platform(_pdk(tmp_path), corners=corners)
    a, b = _macro_libs(tmp_path, "a", "b")
    lib = {c: str(tmp_path / "pdk" / "lib" / f"{c}.lib") for c in _CORNERS}

    text = _render(tmp_path, platform, modes=modes, lib_paths=[a, b])

    if len(corners) == 1:
        expected = [
            "read_liberty $LIBERTY",
            "read_lef     $TECH_LEF",
            "read_lef     $MACRO_LEF",
            f"read_liberty {a}",
            f"read_liberty {b}",
        ]
    elif modes:
        expected = [
            *(f"read_liberty {lib[c]}" for c in corners),
            "read_lef     $TECH_LEF",
            "read_lef     $MACRO_LEF",
            f"read_liberty {a}",
            f"read_liberty {b}",
            *(
                line
                for c in corners
                for line in (
                    f"set_mode {c}",
                    f"define_scene {c} -mode {c} -liberty {{{lib[c]} {a} {b}}}",
                )
            ),
            "set_mode tt",
        ]
    else:
        expected = [
            "define_corners tt ss ff",
            *(f"read_liberty -corner {c} {lib[c]}" for c in corners),
            "read_lef     $TECH_LEF",
            "read_lef     $MACRO_LEF",
            *(f"read_liberty -corner {c} {a}" for c in corners),
            *(f"read_liberty -corner {c} {b}" for c in corners),
        ]
    assert _liberty_section(text) == expected
    assert (
        _render(
            tmp_path,
            platform,
            modes=modes,
            lib_paths=[dict.fromkeys(corners, a), dict.fromkeys(corners, b)],
        )
        == text
    )


def test_another_mapping_gives_another_script_and_input_fingerprint(tmp_path):
    platform = _platform(_pdk(tmp_path))
    per_corner = _per_corner(tmp_path)
    swapped = {**per_corner, "tt": per_corner["ss"], "ss": per_corner["tt"]}

    def _inputs(lib_paths):
        from rtl_buddy.tools.pnr_openroad import OpenRoadPnr

        script = _render(tmp_path, platform, lib_paths=lib_paths)
        cfg = _cfg(tmp_path, lib_paths=lib_paths)
        backend = OpenRoadPnr("demo/openroad", cfg, str(tmp_path), MagicMock())
        backend._resolve_netlist_path = lambda: str(tmp_path / "netlist.v")
        return script, [
            e["path"] for e in backend._checkpoint_inputs(platform, None)["liberty"]
        ]

    script, liberty = _inputs([per_corner])
    other_script, other_liberty = _inputs([swapped])

    assert script != other_script
    assert liberty[-3:] == list(per_corner.values())
    assert other_liberty[-3:] == list(swapped.values())


# ── rb power ───────────────────────────────────────────────────────────────


_POWER_CORNERS = {c: f"/pdk/{c}.lib" for c in _CORNERS}


def _power_script(tmp_path, macro_libs, corners=_POWER_CORNERS):
    backend = _make_power_backend(
        tmp_path, platform=_FakePlatform(liberty="/pdk/tt.lib", corners=corners)
    )
    inner = backend._resolve_inputs
    backend._resolve_inputs = lambda: {
        **inner(),
        "macro_libs": macro_libs,
        "macro_lefs": [],
    }
    return backend, Path(backend._write_script()).read_text().splitlines()


def test_power_reads_each_per_corner_file_into_its_corner(tmp_path):
    (one,) = _macro_libs(tmp_path, "one")
    per_corner = _per_corner(tmp_path)

    backend, lines = _power_script(tmp_path, [one, per_corner])

    assert [line for line in lines if "macros" in line] == [
        *(f"read_liberty -corner {c} {one}" for c in _CORNERS),
        *(f"read_liberty -corner {c} {per_corner[c]}" for c in _CORNERS),
    ]
    assert backend._script_technology["macro_libs"] == [one, *per_corner.values()]


def test_power_on_one_corner_reads_the_mapping_as_its_path(tmp_path):
    (one,) = _macro_libs(tmp_path, "one")
    (typ,) = _macro_libs(tmp_path, "m_typ")

    _, lines = _power_script(tmp_path, [one, {"typ": typ}, typ], corners=None)

    assert [line for line in lines if "macros" in line] == [
        f"read_liberty {one}",
        f"read_liberty {typ}",
    ]


def test_power_ignores_mapping_corners_a_one_corner_platform_lacks(tmp_path):
    per_corner = _per_corner(tmp_path)

    backend, lines = _power_script(
        tmp_path, [per_corner], corners={"tt": _POWER_CORNERS["tt"]}
    )

    assert [line for line in lines if "macros" in line] == [
        f"read_liberty {per_corner['tt']}"
    ]
    assert backend._script_technology["macro_libs"] == [per_corner["tt"]]


def test_power_ignores_mapping_corners_a_smaller_platform_lacks(tmp_path):
    per_corner = _per_corner(tmp_path)
    corners = {c: _POWER_CORNERS[c] for c in ("tt", "ss")}

    backend, lines = _power_script(tmp_path, [per_corner], corners=corners)

    assert [line for line in lines if "macros" in line] == [
        f"read_liberty -corner {c} {per_corner[c]}" for c in ("tt", "ss")
    ]
    assert backend._script_technology["macro_libs"] == [
        per_corner["tt"],
        per_corner["ss"],
    ]


def test_power_on_one_corner_fails_setup_when_the_mapping_lacks_that_corner(tmp_path):
    per_corner = _per_corner(tmp_path)
    del per_corner["tt"]
    backend = _make_power_backend(
        tmp_path,
        platform=_FakePlatform(
            liberty="/pdk/tt.lib", corners={"tt": _POWER_CORNERS["tt"]}
        ),
    )
    inner = backend._resolve_inputs
    backend._resolve_inputs = lambda: {**inner(), "macro_libs": [per_corner]}

    res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "lib-paths[0] has no Liberty for corner(s) tt" in res.results["desc"]


def test_power_fails_setup_on_a_mapping_that_misses_a_corner(tmp_path, caplog):
    per_corner = _per_corner(tmp_path)
    del per_corner["tt"]
    backend = _make_power_backend(
        tmp_path, platform=_FakePlatform(liberty="/pdk/tt.lib", corners=_POWER_CORNERS)
    )
    inner = backend._resolve_inputs
    backend._resolve_inputs = lambda: {**inner(), "macro_libs": [per_corner]}

    with caplog.at_level(logging.ERROR):
        res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert "lib-paths[0] has no Liberty for corner(s) tt" in res.results["desc"]
    assert any(
        getattr(r, "rtl_event", None) == "power.lib_paths_corners"
        for r in caplog.records
    )


def _inheriting_backend(tmp_path, inherited, own):
    """An OpenRoadPower on a P&R source whose run declares `inherited` and whose `power.yaml` declares `own`."""
    from rtl_buddy.tools.power_openroad import OpenRoadPower

    pnr_cfg = MagicMock()
    pnr_cfg.get_lib_path_entries.return_value = inherited
    pnr_cfg.get_blocks.return_value = []
    pnr_cfg.get_name.return_value = "demo_pnr"
    pnr_cfg.resolve_synth_cfg.return_value.get_top.return_value = "demo_top"
    power_cfg = MagicMock()
    power_cfg.get_netlist_source.return_value = "pnr"
    power_cfg.resolve_pnr_cfg.return_value = pnr_cfg
    power_cfg.get_pnr_suite_path.return_value = str(tmp_path / "pnr.yaml")
    power_cfg.get_constraints.return_value = None
    power_cfg.get_lib_path_entries.return_value = own
    backend = OpenRoadPower.__new__(OpenRoadPower)
    backend.power_cfg = power_cfg
    return backend


def test_power_inherits_the_pnr_runs_mapping(tmp_path):
    per_corner = _per_corner(tmp_path)
    (own,) = _macro_libs(tmp_path, "own")
    backend = _inheriting_backend(tmp_path, [per_corner], [own, dict(per_corner)])

    assert backend._resolve_inputs()["macro_libs"] == [per_corner, own]


def test_power_keeps_a_mapping_and_its_corner_swapped_twin():
    from rtl_buddy.tools.power_openroad import _dedup_lib_paths

    mapping = {"tt": "/m/a.lib", "ss": "/m/b.lib"}
    swapped = {"tt": "/m/b.lib", "ss": "/m/a.lib"}
    reordered = {"ss": "/m/b.lib", "tt": "/m/a.lib"}

    assert _dedup_lib_paths([mapping, swapped, dict(mapping), reordered]) == [
        mapping,
        swapped,
    ]


def test_power_fails_setup_on_a_plain_path_that_the_inherited_mapping_names(
    tmp_path, caplog
):
    per_corner = _per_corner(tmp_path)
    inheriting = _inheriting_backend(tmp_path, [per_corner], [per_corner["ss"]])
    macro_libs = inheriting._resolve_inputs()["macro_libs"]
    backend = _make_power_backend(
        tmp_path, platform=_FakePlatform(liberty="/pdk/tt.lib", corners=_POWER_CORNERS)
    )
    inner = backend._resolve_inputs
    backend._resolve_inputs = lambda: {**inner(), "macro_libs": macro_libs}

    with caplog.at_level(logging.ERROR):
        res = backend.run()

    assert res.results["fail_stage"] == "setup"
    assert (
        f"{per_corner['ss']} is listed both as a plain lib-paths entry and in a "
        "per-corner mapping; drop the plain entry"
    ) in res.results["desc"]
    assert any(
        getattr(r, "rtl_event", None) == "power.lib_paths_duplicate"
        for r in caplog.records
    )


def test_power_accepts_that_plain_path_on_a_platform_that_lacks_the_corner(tmp_path):
    per_corner = _per_corner(tmp_path)

    _, lines = _power_script(
        tmp_path,
        [per_corner, per_corner["ss"]],
        corners={"tt": _POWER_CORNERS["tt"]},
    )

    assert [line for line in lines if "macros" in line] == [
        f"read_liberty {per_corner['tt']}",
        f"read_liberty {per_corner['ss']}",
    ]


def test_resume_refuses_a_checkpoint_made_with_another_mapping(tmp_path, monkeypatch):
    from test_pnr_checkpoints import _runs
    from test_pnr_corner_modes import _modes_backend

    sdc = str(tmp_path / "top.sdc")
    Path(sdc).write_text("create_clock -name clk -period 10 [get_ports clk]\n")
    slow, fast = _macro_libs(tmp_path, "slow", "fast")

    def _backend(lib_paths, **kw):
        backend, artefacts = _modes_backend(tmp_path, monkeypatch, sdc, **kw)
        backend.pnr_cfg = replace(backend.pnr_cfg, lib_paths=lib_paths)
        return backend, artefacts

    backend, artefacts = _backend([{"tt": slow, "ff": fast}], stop_after="place")
    assert backend.run().results["result"] == "PASS"
    (first,) = _runs(artefacts)

    same, _ = _backend([{"tt": slow, "ff": fast}], resume=first.name, resume_from="cts")
    assert same.run().results["result"] == "PASS"
    swapped, _ = _backend(
        [{"tt": fast, "ff": slow}], resume=first.name, resume_from="cts"
    )
    res = swapped.run()

    assert res.results["fail_stage"] == "resume"
    assert "01_floorplan.tcl script generated now differs" in res.results["desc"]
    assert f"now 'read_liberty {fast}'" in res.results["desc"]
