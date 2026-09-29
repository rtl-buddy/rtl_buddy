"""Tests for the hub-served synth and power pane: ``/phy.json``, the offline ``/phy``
page, presence and advertisement, ``phys_focus`` and the origin-to-label map.

Assertions on the page are structural; pure helpers are sliced out between markers
and run in ``node``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub import phys_page, theme
from rtl_buddy.hub.protocol import (
    Envelope,
    HubProtocolError,
    Kind,
    Origin,
    decode,
    encode,
    new_id,
)
from rtl_buddy.hub.server import HubServer
from rtl_buddy.hub.state import PhysFocus
from rtl_buddy.hub.viewer_http import ViewerServer, render_index_html
from rtl_buddy.phys import manifest as manifest_mod
from rtl_buddy.phys import query as phys_query
from rtl_buddy.phys.manifest import build_manifest, write_manifest
from rtl_buddy.phys.model import (
    build_power_model,
    build_synth_model,
    merge_model,
    write_model,
)


MODULE_ROWS = [
    {"module": "blk", "cell_count": 120, "area_um2": 480.5},
    {"module": "sub", "cell_count": 40, "area_um2": 96.0},
    {"module": "tiny", "cell_count": 2, "area_um2": None},
]

INSTANCE_ROWS = [
    {
        "instance_path": "u_sub/_64_",
        "module": "sub",
        "leakage_uw": 0.079,
        "internal_uw": 2.28,
        "switching_uw": 0.0675,
        "total_uw": 2.42,
    },
    {
        "instance_path": "u_sub/u_leaf/_12_",
        "module": "tiny",
        "leakage_uw": 0.001,
        "internal_uw": 0.5,
        "switching_uw": 0.25,
        "total_uw": 0.751,
    },
]


# Both halves of a fixture run record the same netlist hash, as a ``rb synth`` then ``rb
# power`` pair does; the merge requires it.
_FIXTURE_NETLIST_SHA256 = "0" * 64


def _write_run(
    root: Path,
    run: str,
    *,
    modules=None,
    instances=None,
    mtime=None,
    publication=None,
    artefacts=None,
):
    """One run's artefact directory, written by the real producers.

    ``publication`` stamps both documents with one token; left off, they carry
    ``None``. ``artefacts`` overrides where the run lands, for containment tests.
    """

    phys_dir = (artefacts or root / "verif" / "blk" / "artefacts") / run
    phys_dir.mkdir(parents=True, exist_ok=True)

    model = None
    if modules is not None:
        model = build_synth_model(
            top="blk",
            modules=modules,
            area_um2=576.5,
            gate_count=162,
            netlist_sha256=_FIXTURE_NETLIST_SHA256,
        )
    if instances is not None:
        power = build_power_model(
            top="blk",
            instances=instances,
            internal_w=2.78e-6,
            switching_w=0.3175e-6,
            leakage_w=0.08e-6,
            total_w=3.171e-6,
            netlist_sha256=_FIXTURE_NETLIST_SHA256,
        )
        model = (
            merge_model(model, power, own_half="instances")
            if model is not None
            else power
        )
    if publication is not None:
        model["publication"] = publication
    model_path = write_model(model, phys_dir)

    manifest = build_manifest(
        project_root=root,
        phys_dir=phys_dir,
        command="power" if instances is not None else "synth",
        run=run,
        top="blk",
        model_path=model_path,
        totals=model["totals"],
        synth=(
            None
            if modules is None
            else {
                "backend": "yosys",
                "run": run,
                "stats": phys_dir / "synth_stat.json",
                "netlist": phys_dir / "synth_netlist.v",
                "log": phys_dir / "synth.log",
            }
        ),
        power=(
            None
            if instances is None
            else {
                "backend": "openroad",
                "run": run,
                "netlist_source": "synth",
                "report": phys_dir / "power.rpt",
                "instances": phys_dir / "power_instances.rpt",
                "cells": phys_dir / "power_instances.cells",
                "log": phys_dir / "power.log",
            }
        ),
    )
    if publication is not None:
        manifest["publication"] = publication
    manifest_path = write_manifest(manifest, phys_dir)
    if mtime is not None:
        os.utime(manifest_path, (mtime, mtime))
    return phys_dir


@pytest.fixture
def phys_project(tmp_path: Path) -> Path:
    """A project whose newest run measured both halves."""

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write_run(root, "old_synth", modules=MODULE_ROWS, mtime=1_000_000)
    _write_run(
        root, "both", modules=MODULE_ROWS, instances=INSTANCE_ROWS, mtime=2_000_000
    )
    return root


@pytest.fixture(autouse=True)
def _clear_presence_cache():
    """Presence is memoised for five seconds per root path; tests asserting on a miss
    clear the cache.
    """

    phys_page._presence_cache.clear()  # noqa: SLF001
    yield
    phys_page._presence_cache.clear()  # noqa: SLF001


def test_payload_is_the_cli_builder_plus_a_hub_block(phys_project: Path):
    """The payload is the query builder's output, so the pane and ``rb phys summary``
    agree on numbers.
    """

    payload = phys_page.build_phys_payload(phys_project)
    ctx = phys_query.load_context(phys_project)
    expected = phys_query.summary_payload(ctx, limit=0)
    hub = payload.pop("hub")
    # The run selector's menu is the ``rb phys runs`` payload verbatim.
    runs = payload.pop("runs")
    assert runs == phys_query.runs_payload(phys_project, limit=phys_page.RUNS_LIMIT)
    assert payload == expected
    assert hub["schema_version"] == phys_page.PAGE_SCHEMA_VERSION
    assert hub["metrics"] == ["cells", "area", "leakage", "dynamic", "total"]
    assert hub["power_columns"] == list(phys_query.POWER_COLUMNS)
    assert hub["model"].endswith("artefacts/both/phys-model.json")
    # This fixture writes documents directly, so there is no token and the page falls
    # back to manifest path plus top.
    assert hub["publication"] is None


def test_payload_carries_the_models_publication_token(tmp_path: Path):
    """The body carries the publication token, which the page uses to compare reloads."""

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write_run(
        root,
        "both",
        modules=MODULE_ROWS,
        instances=INSTANCE_ROWS,
        publication="0123456789abcdef",
    )

    payload = phys_page.build_phys_payload(root)

    assert payload["hub"]["publication"] == "0123456789abcdef"


def test_payload_truncates_nothing(phys_project: Path):
    """With ``limit=0`` the rankings are the raw rows, a permutation rather than a head,
    so the payload carries no second copy.
    """

    payload = phys_page.build_phys_payload(phys_project)
    assert payload["limit"] == 0
    assert len(payload["modules"]) == len(MODULE_ROWS)
    assert len(payload["instances"]) == len(INSTANCE_ROWS)
    assert {row["module"] for row in payload["modules"]} == {
        row["module"] for row in MODULE_ROWS
    }
    # Ranked, and the CLI's ranking: cells first, area as the tie-break.
    assert [row["module"] for row in payload["modules"]] == ["blk", "sub", "tiny"]
    assert [row["instance_path"] for row in payload["instances"]] == [
        "u_sub/_64_",
        "u_sub/u_leaf/_12_",
    ]


def test_payload_carries_the_halves_and_the_totals_to_check_them_against(
    phys_project: Path,
):
    """The sanity block carries both the flows' own log scrape (``totals``) and the rows
    from a different scrape; neither is derived from the other.
    """

    payload = phys_page.build_phys_payload(phys_project)
    assert payload["totals"]["cell_count"] == 162
    assert payload["totals"]["area_um2"] == 576.5
    assert payload["halves"]["modules"]["present"] is True
    assert payload["halves"]["instances"]["produced_by"] == "rb power"
    assert payload["missing_halves"] == []
    assert payload["counts"] == {"modules": 3, "instances": 2}
    assert payload["artefacts"]["synth_netlist"].endswith("synth_netlist.v")


def test_a_half_the_run_did_not_produce_is_named_not_guessed(tmp_path: Path):
    """``halves`` and ``missing_halves`` are present, since an empty ranking cannot tell
    "no synthesis" from "no cells".
    """

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write_run(root, "power_only", instances=INSTANCE_ROWS)

    payload = phys_page.build_phys_payload(root)
    assert payload["modules"] == []
    assert payload["halves"]["modules"] == {
        "present": False,
        "rows": None,
        "produced_by": "rb synth",
        "netlist_hash": False,
        # A synthesis has no power mode or activity; the keys exist so the pane can walk
        # both halves alike.
        "mode": None,
        "activity": None,
    }
    assert payload["missing_halves"] == ["modules"]
    assert payload["counts"]["modules"] is None


def test_payload_bytes_404_names_the_commands_that_make_data(tmp_path: Path):
    status, body = phys_page.phys_payload_bytes(tmp_path)
    assert status == 404
    error = json.loads(body)["error"]
    assert "phys-manifest.json" in error
    assert "rb synth" in error and "rb power" in error


def test_presence_follows_discovered_artefacts(tmp_path: Path, phys_project: Path):
    assert phys_page.phys_data_present(phys_project) is True
    assert phys_page.phys_data_present(None) is False
    empty = tmp_path / "no-physics-here"
    empty.mkdir()
    assert phys_page.phys_data_present(empty) is False


def test_presence_is_cached_for_the_ttl(phys_project: Path, monkeypatch):
    """The presence walk is cached, since a walk per landing poll is costly on a big
    tree.
    """

    calls = []
    real = manifest_mod.discover_manifests

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(phys_page.manifest_mod, "discover_manifests", counted)
    assert phys_page.phys_data_present(phys_project) is True
    assert phys_page.phys_data_present(phys_project) is True
    assert len(calls) == 1
    # ttl=0 forces a fresh walk.
    assert phys_page.phys_data_present(phys_project, ttl=0) is True
    assert len(calls) == 2


def test_page_injects_hub_address():
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_PHY_URL__ = '/phy.json'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """Every non-anchor ``src``/``href`` is a same-origin absolute path; no CDN, remote
    font or import.
    """

    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "<script src=" not in body
    assert "@import" not in body
    for host in ("cdn.", "unpkg", "jsdelivr", "googleapis", "//fonts"):
        assert host not in body
    for attr in ("href=", "src="):
        for chunk in body.split(attr)[1:]:
            quote = chunk[0]
            value = chunk[1:].split(quote)[0] if quote in "\"'" else chunk.split()[0]
            assert value.startswith("/"), f"{attr}{value}"
    for scheme in ("https://", "http://"):
        assert scheme not in body


def test_page_links_the_shared_token_sheet_with_a_fallback():
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<link rel="stylesheet" href="/hub/theme.css">' in body
    assert theme.FAVICON_16 in body and theme.FAVICON_32 in body
    for token in (
        "--bg:",
        "--panel:",
        "--fg:",
        "--accent:",
        "--heat-l0:",
        "--heat-none:",
    ):
        assert token in body, token
    # Light default, and the fallback before the link, or it out-ranks the sheet at
    # equal specificity.
    assert "--bg:          #f8fafc;" in body
    assert body.index("--bg:          #f8fafc;") < body.index('href="/hub/theme.css"')


def test_the_heat_ramp_lives_in_the_sheet_not_in_the_page():
    """The phys ramp's endpoints are sheet tokens, repeated only in the 404 fallback
    block and read through ``var()`` elsewhere.
    """

    sheet = theme.THEME_CSS
    for token in ("--heat-h:", "--heat-s:", "--heat-l0:", "--heat-l1:", "--heat-none:"):
        assert token in sheet, token
    # Both themes, or the heaviest row vanishes into a dark ground.
    light, dark = theme.parse_palettes(sheet.split(theme.GENERATED_MARKER)[0])
    assert dict(light)["--heat-l1"] == "66%"
    assert dict(dark)["--heat-l1"] == "42%"

    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "calc(var(--heat-l0) + (var(--heat-l1) - var(--heat-l0)) * var(--f))" in body


def test_page_carries_the_pieces_the_issue_asks_for():
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # Every metric of the switcher, which is the wire enum verbatim.
    for metric in ("cells", "area", "leakage", "dynamic", "total"):
        assert f"'{metric}'" in body, metric
    # The four power columns the model carries, plus the one it does not.
    for column in ("internal_uw", "switching_uw", "leakage_uw", "total_uw"):
        assert column in body, column
    # The hub chrome vocabulary.
    for word in ("connected", "connecting…", "offline"):
        assert word in body, word
    # The envelope vocabulary: registers as its own origin, handles focus, drives the
    # other panes.
    assert "'phys'" in body
    # …politely: the first hello asks for the slot, it does not seize it.
    assert "takeover: true" not in body
    assert "phys_focus" in body
    assert "graph_focus" in body
    assert "selection_changed" in body
    # The missing-half banner and the empty state both name the producing commands;
    # either fills a half.
    assert "rb synth" in body and "rb power" in body
    assert theme.MASCOT_240 in body


def test_the_page_is_the_phy_route_and_the_phys_origin():
    """The route and label are ``phy``; the ``hello`` client is ``phys``."""

    assert phys_page.PHYS_PAGE_ROUTE == "/phy"
    assert phys_page.PHYS_JSON_ROUTE == "/phy.json"
    js = _page_js()
    assert "client: 'phys', version: '1.0.0', capabilities: ['phys_focus']" in js
    assert "origin: 'phys', kind: 'request', type: 'hello'," in js
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "<title>rtl-buddy-phy</title>" in body


def _page_js() -> str:
    """The page's inline script — the last ``<script>`` in the body."""

    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    return body.split("<script>")[-1].split("</script>")[0]


def _marked_js(marker: str) -> str:
    """Pure helpers sliced out between markers and run in bare ``node``; nothing between
    the markers may touch the DOM.
    """

    match = re.search(rf"// >>> {marker}\n(.*?)// <<< {marker}", _page_js(), re.S)
    assert match, f"the {marker} markers moved"
    return match.group(1)


def _node(script: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the dev machine
        pytest.skip("node not installed")
    done = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_page_javascript_parses(tmp_path: Path):
    """The whole inline script parses; a syntax error blanks the pane silently."""

    script = tmp_path / "phys_page.js"
    script.write_text(_page_js(), encoding="utf-8")
    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the dev machine
        pytest.skip("node not installed")
    done = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


def test_dynamic_power_is_summed_here_because_no_producer_writes_it():
    """``dynamic`` is internal plus switching; the model has no such column, and this is
    the one place the pane derives it.
    """

    out = _node(
        _marked_js("derived-metrics")
        + """
        console.log(JSON.stringify([
          dynamicOf({ internal_uw: 2.5, switching_uw: 0.5 }),
          dynamicOf({ internal_uw: 2.5, switching_uw: null }),
          dynamicOf({ internal_uw: null, switching_uw: null }),
          dynamicOf({}),
          cellValue({ internal_uw: 1, switching_uw: 2 }, 'dynamic'),
          cellValue({ area_um2: null }, 'area_um2')
        ]));
        """
    )
    assert json.loads(out) == [3.0, 2.5, None, None, 3.0, None]


def test_a_null_column_never_sums_to_zero():
    """A column no row measured stays null, not zero."""

    out = _node(
        _marked_js("derived-metrics")
        + """
        var rows = [{ area_um2: null }, { area_um2: null }];
        var mixed = [{ area_um2: null }, { area_um2: 4.5 }];
        console.log(JSON.stringify([
          sumColumn(rows, 'area_um2'),
          sumColumn(mixed, 'area_um2'),
          sumColumn([], 'area_um2'),
          sumColumn([{ area_um2: 0 }], 'area_um2')
        ]));
        """
    )
    assert json.loads(out) == [None, 4.5, None, 0]


def test_unmeasured_rows_sink_in_both_directions():
    """As in ``rb phys``'s ``_sort_key_desc``, unmeasured rows sort below every measured
    row in both directions.
    """

    out = _node(
        _marked_js("derived-metrics")
        + _marked_js("row-ordering")
        + """
        var rows = [
          { module: 'tiny', area_um2: null },
          { module: 'sub',  area_um2: 96 },
          { module: 'blk',  area_um2: 480.5 }
        ];
        function names(list) { return list.map(function (r) { return r.module; }); }
        console.log(JSON.stringify(names(rankRows(rows, 'area_um2', 'desc',
          function (r) { return r.module; }))));
        console.log(JSON.stringify(names(rankRows(rows, 'area_um2', 'asc',
          function (r) { return r.module; }))));
        """
    )
    desc, asc = out.strip().splitlines()
    assert json.loads(desc) == ["blk", "sub", "tiny"]
    assert json.loads(asc) == ["sub", "blk", "tiny"]


def test_equal_cell_counts_break_on_area_as_the_cli_does():
    """The generic tie-break follows ``heaviest_modules()`` (cell_count, area_um2,
    module), so the pane and ``rb phys summary`` order ties alike.
    """

    out = _node(
        _marked_js("derived-metrics")
        + _marked_js("row-ordering")
        + """
        var rows = [
          { module: 'alpha', cell_count: 4, area_um2: 10 },
          { module: 'beta',  cell_count: 4, area_um2: 90 },
          { module: 'gamma', cell_count: 4, area_um2: null }
        ];
        function names(list) { return list.map(function (r) { return r.module; }); }
        console.log(JSON.stringify(names(rankRows(rows, 'cell_count', 'desc',
          function (r) { return r.module; }))));
        console.log(JSON.stringify(names(rankRows(rows, 'cell_count', 'asc',
          function (r) { return r.module; }))));
        """
    )
    desc, asc = out.strip().splitlines()
    # Descending is the CLI's order: bigger area first, unmeasured area last.
    assert json.loads(desc) == ["beta", "alpha", "gamma"]
    assert json.loads(asc) == ["alpha", "beta", "gamma"]


def test_the_cli_and_the_pane_rank_equal_cell_counts_alike(tmp_path: Path):
    """The pane's order is compared with ``heaviest_modules`` directly, so drift fails
    here.
    """
    from rtl_buddy.phys.query import heaviest_modules

    rows = [
        {"module": "alpha", "cell_count": 4, "area_um2": 10},
        {"module": "beta", "cell_count": 4, "area_um2": 90},
        {"module": "gamma", "cell_count": 4, "area_um2": None},
    ]
    cli = [row["module"] for row in heaviest_modules({"modules": rows})]

    out = _node(
        _marked_js("derived-metrics")
        + _marked_js("row-ordering")
        + f"""
        var rows = {json.dumps(rows)};
        console.log(JSON.stringify(rankRows(rows, 'cell_count', 'desc',
          function (r) {{ return r.module; }}).map(function (r) {{ return r.module; }})));
        """
    )

    assert json.loads(out) == cli


def test_equal_rows_keep_a_stable_order():
    """Ties break on the name, then on the payload's order, so re-sorts do not jitter."""

    out = _node(
        _marked_js("derived-metrics")
        + _marked_js("row-ordering")
        + """
        var rows = [
          { module: 'zeta', cell_count: 4 },
          { module: 'alpha', cell_count: 4 },
          { module: 'mid', cell_count: 4 }
        ];
        console.log(JSON.stringify(
          rankRows(rows, 'cell_count', 'desc', function (r) { return r.module; })
            .map(function (r) { return r.module; })));
        """
    )
    assert json.loads(out) == ["alpha", "mid", "zeta"]


def test_the_separator_is_levelled_only_on_the_way_to_the_wire():
    """The pane emits dot-separated paths on the wire, though OpenSTA prints ``/`` and
    ``rb phys`` accepts both.
    """

    out = _node(
        _marked_js("path-normalise")
        + """
        console.log(JSON.stringify([
          toWirePath('u_cpu/u_alu/_12_'),
          toWirePath('u_cpu.u_alu._12_'),
          toWirePath(null),
          samePath('u_cpu/u_alu', 'u_cpu.u_alu'),
          samePath('u_cpu/u_alu', 'u_cpu.u_alu2'),
          samePath('', 'u_cpu')
        ]));
        """
    )
    assert json.loads(out) == [
        "u_cpu.u_alu._12_",
        "u_cpu.u_alu._12_",
        "",
        True,
        False,
        False,
    ]


# Paths pinning both copies of the levelling rule (``level_path`` with ``/``,
# ``toWirePath`` with ``.``): a backslash opening a segment runs to the whitespace that
# ends it, or to the path end.
ESCAPED_PATHS = [
    r"u_top/\gen[0].u_x",  # the escape a reader actually stores
    "u_top/\\gen[0].u_x /u_ff",  # terminated, as Verilog spells it
    r"u_top/\a/b",  # an escape may contain the other separator too
    r"u_top/x\a/b",  # a backslash mid-segment leads no escape
    r"u_top.u_sub",  # and nothing about plain paths changes
]


def test_the_wire_levelling_keeps_an_escaped_identifier_whole():
    r"""An escaped name such as `\gen[0].u_x` is one leaf; a `.` or `/` inside it is not
    a level.
    """

    out = _node(
        _marked_js("path-normalise")
        + "console.log(JSON.stringify(%s.map(toWirePath)));" % json.dumps(ESCAPED_PATHS)
    )
    assert json.loads(out) == [
        r"u_top.\gen[0].u_x",
        r"u_top.\gen[0].u_x.u_ff",
        r"u_top.\a/b",
        r"u_top.x\a.b",
        r"u_top.u_sub",
    ]


def test_the_pane_and_the_query_layer_level_the_same_way():
    """The anti-drift pin: two copies of one rule, one separator apart."""

    from rtl_buddy.phys.query import level_path

    assert [level_path(path) for path in ESCAPED_PATHS] == [
        r"u_top/\gen[0].u_x",
        r"u_top/\gen[0].u_x/u_ff",
        r"u_top/\a/b",
        r"u_top/x\a/b",
        r"u_top/u_sub",
    ]


def test_an_escaped_row_is_still_reachable_from_the_wire():
    r"""A `\gen[0].u_x` leaf sent out rooted matches on the way back."""

    out = _node(
        _marked_js("path-normalise")
        + r"""
        var rows = [{ instance_path: 'u_top/\\gen[0].u_x' }];
        function hit(path, rooted) {
          var row = findByPath(rows, path, 'blk', rooted);
          return row === null ? null : row.instance_path;
        }
        console.log(JSON.stringify([
          withTop('u_top/\\gen[0].u_x', 'blk'),
          hit('blk.u_top.\\gen[0].u_x', true),
          hit('u_top.\\gen[0].u_x', false),
          hit('u_top/\\gen[0].u_x ', false),
          hit('u_top.\\gen[0].u_x2', false)
        ]));
        """
    )
    assert json.loads(out) == [
        r"blk.u_top.\gen[0].u_x",
        r"u_top/\gen[0].u_x",
        r"u_top/\gen[0].u_x",
        r"u_top/\gen[0].u_x",
        None,
    ]


def test_the_design_top_is_added_on_the_way_out():
    """Model rows are rootless and a schematic ``instance_path`` is rooted, so the top
    is always prepended; a prefix test would misread `cpu/alu` under top `cpu`.
    """

    out = _node(
        _marked_js("path-normalise")
        + """
        console.log(JSON.stringify([
          withTop('u_sub/u_leaf', 'blk'),
          withTop('blk', 'blk'),                // a level that shares the name
          withTop('cpu/alu', 'cpu'),            // the ambiguous case
          withTop('u_sub/u_leaf', ''),          // no top known: plain levelling
          withTop('', 'blk')
        ]));
        """
    )
    assert json.loads(out) == [
        "blk.u_sub.u_leaf",
        "blk.blk",
        "cpu.cpu.alu",
        "u_sub.u_leaf",
        "",
    ]


def test_an_inbound_path_is_resolved_against_the_rows_not_by_its_prefix():
    """Inbound paths may or may not carry the top and no prefix test can tell (under top
    `cpu`, `cpu.alu` is both), so both readings are tried against the rows, the
    sender's convention first.
    """

    out = _node(
        _marked_js("path-normalise")
        + """
        // A design whose top name is also an instance name, with both
        // readings present as rows.
        var rows = [
          { instance_path: 'cpu/alu' },
          { instance_path: 'alu' }
        ];
        function hit(path, rooted) {
          var row = findByPath(rows, path, 'cpu', rooted);
          return row === null ? null : row.instance_path;
        }
        console.log(JSON.stringify([
          hit('cpu.cpu.alu', true),    // what withTop('cpu/alu') sent out
          hit('cpu.alu', true),        // rooted: the top level is the top
          hit('cpu.alu', false),       // a target in the model's spelling
          hit('cpu.nope', true),
          hit('u_sub.u_leaf', true)
        ]));
        // With only the rootless row present, a rooted sender still
        // reaches it: the reading that names no row loses to the one
        // that does.
        var one = [{ instance_path: 'cpu/alu' }];
        console.log(JSON.stringify([
          findByPath(one, 'cpu.cpu.alu', 'cpu', true).instance_path,
          findByPath(one, 'cpu.alu', 'cpu', true).instance_path,
          findByPath(one, 'cpu.alu', 'cpu', false).instance_path
        ]));
        """
    )
    both, alone = out.strip().splitlines()
    assert json.loads(both) == ["cpu/alu", "alu", "cpu/alu", None, None]
    assert json.loads(alone) == ["cpu/alu", "cpu/alu", "cpu/alu"]


def test_the_wire_and_a_focus_target_are_read_with_their_own_convention():
    """A ``selection_changed`` comes rooted from the schematic; a ``phys_focus`` target
    is typically rootless, from ``rb phys`` output.
    """

    js = _page_js()
    assert "function instanceRow(path, rooted) {" in js
    assert "return findByPath(rowsOf('instances'), path, designTop(), rooted);" in js
    # The wire says so; every other caller takes the rootless default.
    assert "if (focusInstance(ip, true)) { note(focusNote('selected ' + ip)); }" in js
    assert "ok = focusInstance(target.slice(9));" in js


def test_the_module_column_sorts_by_name_rather_than_by_null():
    """``module`` is a string column and must not go through the numeric reader."""

    out = _node(
        _marked_js("derived-metrics")
        + _marked_js("row-ordering")
        + """
        var rows = [
          { instance_path: 'c', module: 'NAND2_X1' },
          { instance_path: 'a', module: 'DFF_X1' },
          { instance_path: 'b', module: 'XOR2_X1' }
        ];
        function cells(list) { return list.map(function (r) { return r.module; }); }
        console.log(JSON.stringify(cellValue(rows[0], 'module')));
        console.log(JSON.stringify(cells(rankRows(rows, 'module', 'asc',
          function (r) { return r.instance_path; }))));
        console.log(JSON.stringify(cells(rankRows(rows, 'module', 'desc',
          function (r) { return r.instance_path; }))));
        """
    )
    value, ascending, descending = out.strip().splitlines()
    assert json.loads(value) == "NAND2_X1"
    assert json.loads(ascending) == ["DFF_X1", "NAND2_X1", "XOR2_X1"]
    assert json.loads(descending) == ["XOR2_X1", "NAND2_X1", "DFF_X1"]


def test_the_module_lens_says_when_the_join_cannot_see_the_rows():
    """A module on a mapped hierarchical design matches no leaf, since leaves carry
    Liberty cell names; the empty state says so instead of implying zero power.
    """

    js = _page_js()
    assert "function joinMissNote()" in js
    # The empty branch consults it before falling back to the plain state.
    assert "joinMissNote() ||" in js
    assert "elem('p', 'muted', 'no instances match.')" in js
    # What it says, and the three cases it declines to say it in.
    assert "Liberty cell names, not RTL module names" in js
    assert "hierarchy join" in js
    assert "if (state.module === null || state.filter) { return null; }" in js
    # Membership uses ``namespacesOf``, shared with the collision note.
    assert "if (spaces.indexOf('liberty') >= 0) { return null; }" in js


def test_a_name_in_both_namespaces_is_a_collision_the_pane_says_out_loud():
    """A name that is both an RTL module and a Liberty cell gets a collision note, as
    ``rb phys module`` reports ``instance_join``; the pane makes the same test
    itself.
    """

    js = _page_js()
    assert "function collisionNote()" in js
    # Rendered under the lens pill, so it shows even when the filter empties the table.
    assert "var collision = collisionNote();" in js
    assert "if (collision) { els.instances.appendChild(collision); }" in js
    # Only a name in BOTH namespaces, and it says which measurement is
    # whose and what the module's own power is waiting on.
    assert "if (spaces.length < 2) { return null; }" in js
    assert "name collision: " in js
    assert "is an RTL module in the " in js
    assert "a Liberty cell in the power half" in js
    assert "needs the hierarchy join" in js


def test_the_pane_splits_the_two_module_namespaces_the_way_the_query_does():
    """``namespacesOf`` mirrors ``phys.query.namespaces_of``: ``['rtl']`` is a miss and
    ``['rtl', 'liberty']`` a collision.
    """

    out = _node(
        _marked_js("namespace-split")
        + """
        var modules = [{ module: 'blk' }, { module: 'sub' }];
        var instances = [
          { instance_path: 'u_sub/_64_', module: 'DFF_X1' },
          { instance_path: 'u_sub/_65_', module: 'sub' },
          { instance_path: 'u_sub/_66_', module: null }
        ];
        console.log(JSON.stringify([
          namespacesOf(modules, instances, 'blk'),
          namespacesOf(modules, instances, 'DFF_X1'),
          namespacesOf(modules, instances, 'sub'),
          namespacesOf(modules, instances, 'nowhere'),
          namespacesOf(modules, instances, null)
        ]));
        """
    )
    assert json.loads(out) == [
        ["rtl"],
        ["liberty"],
        # The collision, ordered as the query layer orders it.
        ["rtl", "liberty"],
        [],
        # A `null` module column is not a row named "null".
        [],
    ]


def test_the_module_instance_counts_are_counted_once_per_payload():
    """The per-module leaf count comes from a map built once, not from scanning the
    instance array per row.
    """

    out = _node(
        _marked_js("instance-counts")
        + """
        var counts = countByModule([
          { module: 'DFF_X1' },
          { module: 'NAND2_X1' },
          { module: 'DFF_X1' },
          { module: 'constructor' },
          { module: null },
          {}
        ]);
        console.log(JSON.stringify([
          counts['DFF_X1'],
          counts['NAND2_X1'],
          counts['constructor'],   // a null-prototype map, not Object's
          counts['toString'],
          counts['absent'] || 0
        ]));
        // The key set IS the liberty namespace, so a row with no module
        // column must not put a name in it.
        console.log(JSON.stringify(Object.keys(counts).sort()));
        console.log(JSON.stringify(countByModule(null)));
        """
    )
    counts, keys, empty = out.strip().splitlines()
    assert json.loads(counts) == [2, 1, 1, None, 0]
    assert json.loads(keys) == ["DFF_X1", "NAND2_X1", "constructor"]
    assert json.loads(empty) == {}

    js = _page_js()
    # Built once per payload and read from the cache per row.
    assert "state.instanceCounts = countByModule(rowsOf('instances'));" in js
    assert "state.instanceCounts = null;" in js
    # The cache is dropped only when the payload changes: on ingest and on forgetting
    # after a failed load.
    assert js.count("state.instanceCounts = null;") == 2


def test_model_identity_is_the_publication_or_the_document_it_came_from():
    """A reload is compared by the publication token when present, else by manifest path
    and top.
    """

    out = _node(
        _marked_js("model-identity")
        + """
        var a = { manifest: 'verif/blk/artefacts/both/phys-manifest.json', top: 'blk' };
        var b = { manifest: 'verif/other/artefacts/both/phys-manifest.json', top: 'blk' };
        var c = { manifest: a.manifest, top: 'other_top' };
        var reread = { manifest: a.manifest, top: 'blk', counts: { modules: 9 } };
        console.log(JSON.stringify([
          modelIdentity(a) === modelIdentity(reread),
          modelIdentity(a) === modelIdentity(b),
          modelIdentity(a) === modelIdentity(c),
          modelIdentity({ publication: 'ff', manifest: a.manifest }) ===
            modelIdentity({ publication: 'ff', manifest: b.manifest }),
          modelIdentity({ hub: { publication: 'ff' } }) ===
            modelIdentity({ publication: 'ff' }),
          modelIdentity(null)
        ]));
        """
    )
    assert json.loads(out) == [True, False, False, True, True, None]


def test_a_republished_model_at_the_same_path_is_a_different_model():
    """With a token in the body, republishing the same run under the same top is a new
    model, which drops the lens and selection.
    """

    out = _node(
        _marked_js("model-identity")
        + """
        var manifest = 'verif/blk/artefacts/both/phys-manifest.json';
        var before = { manifest: manifest, top: 'blk', hub: { publication: 'aaa' } };
        var after = { manifest: manifest, top: 'blk', hub: { publication: 'bbb' } };
        var reread = { manifest: manifest, top: 'blk', hub: { publication: 'aaa' } };
        var untokened = { manifest: manifest, top: 'blk', hub: {} };
        console.log(JSON.stringify([
          modelIdentity(before) === modelIdentity(after),
          modelIdentity(before) === modelIdentity(reread),
          modelIdentity(before) === modelIdentity(untokened),
          modelIdentity(untokened) === modelIdentity({ manifest: manifest, top: 'blk' })
        ]));
        """
    )

    # A new token is a new model; the same token is the same model; a body
    # with no token falls back to the document it came from.
    assert json.loads(out) == [False, True, False, True]

    # A different identity clears the reader's lens and selection.
    body = _page_js().split("function ingest(payload) {")[1]
    dropped = body.split("if (replaced) {")[1].split("}")[0]
    assert "state.module = null;" in dropped and "state.instance = null;" in dropped


def test_a_model_change_drops_the_lens_and_the_selection():
    """A reload onto a different run or design drops the lens even when rootless names
    such as `blk` exist in both.
    """

    js = _page_js()
    body = js.split("function ingest(payload) {")[1].split("renderMetricPicker();")[0]
    assert "var identity = modelIdentity(payload);" in body
    assert "state.identity !== null && state.identity !== identity" in body
    dropped = body.split("if (replaced) {")[1].split("}")[0]
    assert "state.module = null;" in dropped
    assert "state.instance = null;" in dropped
    # Decided before the payload is installed, or ``modelIdentity`` would compare the
    # new payload with itself.
    assert body.index("var identity") < body.index("state.payload = payload;")
    # The reader's own controls survive, as they do a failed load.
    for kept in ("state.metric", "state.sort", "state.filter"):
        assert kept not in dropped, kept
    # A focus that arrived before the fetch landed is replayed at the end of the ingest.
    ingest = js.split("function ingest(payload) {")[1]
    assert ingest.index("if (replaced) {") < ingest.index("if (focus) {")
    assert "applyFocus(focus);" in ingest


def test_a_forgotten_model_leaves_no_identity_to_compare_against():
    """The load after a failure must not read as a model change."""

    js = _page_js()
    body = js.split("function forgetModel() {")[1].split("\n  }")[0]
    assert "state.identity = null;" in body


def test_a_failed_load_forgets_the_model_it_was_showing():
    """The empty panel drops the held model, so the header and inbound focus do not use
    rows nobody can see.
    """

    js = _page_js()
    body = js.split("function forgetModel() {")[1].split("\n  }")[0]
    for cleared in (
        "state.payload = null;",
        "state.instanceCounts = null;",
        "state.module = null;",
        "state.instance = null;",
        "resetInstanceWindow();",
        "els.counts.textContent = '';",
        "clear(els.metric);",
        "clear(els.banner);",
        "clear(els.totals);",
        "clear(els.modules);",
        "clear(els.instances);",
        "clear(els.run);",
    ):
        assert cleared in body, cleared
    # The hub connection and the reader's metric pick are not payload state and survive
    # a failed reload.
    for kept in ("ws", "peers", "state.metric", "state.sort", "state.filter"):
        assert kept not in body, kept
    # A focus held for the load that has not landed is still held.
    assert "state.pending" not in body

    # One caller, and it forgets before it paints.
    empty = js.split("function showEmpty(message) {")[1]
    assert empty.strip().splitlines()[0].strip() == "forgetModel();"
    assert js.count("forgetModel()") == 2
    # Both failure paths reach it through ``loadFailed``.
    assert (
        "if (!res.ok) { loadFailed(requested, res.body && res.body.error); return; }"
        in js
    )
    assert "loadFailed(requested, 'could not read ' + url" in js
    # With no payload, replay paths pend instead of acting.
    assert "state.pending = payload;" in js
    assert "state.pendingSelection = ip;" in js


def test_the_banner_offers_the_merge_only_when_the_halves_could_pair():
    """A power half from ``netlist-source: pnr`` has no netlist hash, so a later ``rb
    synth`` replaces the model rather than completing it; the banner must not advise
    it.
    """

    out = _node(
        _marked_js("half-advice")
        + """
        var paired = { halves: { instances: { present: true, netlist_hash: true } } };
        var fromPnr = { halves: { instances: { present: true, netlist_hash: false } } };
        var neither = { halves: {
          modules: { present: false, netlist_hash: false },
          instances: { present: false, netlist_hash: false }
        } };
        console.log(JSON.stringify([
          halfAdvice(paired, 'modules'),
          halfAdvice(fromPnr, 'modules'),
          halfAdvice(neither, 'modules'),
          halfAdvice({ halves: { modules: { present: true, netlist_hash: false } } },
                     'instances'),
          halfAdvice(null, 'modules')
        ]));
        """
    )
    paired, from_pnr, neither, reverse, empty = json.loads(out)
    assert paired == {"other": "instances", "pairable": True}
    assert from_pnr == {"other": "instances", "pairable": False}
    # Nothing to preserve when the other half is absent too, so the plain advice stands.
    assert neither == {"other": "instances", "pairable": True}
    # Symmetric: a synthesis half with no hash cannot be merged onto by a later ``rb
    # power``.
    assert reverse == {"other": "modules", "pairable": False}
    assert empty == {"other": "instances", "pairable": True}

    # The unpairable banner names what does work.
    js = _page_js()
    body = js.split("function renderBanner() {")[1].split("\n  }")[0]
    assert "into the same artefact directory to fill" in body
    assert "would replace this model rather than complete it" in body
    assert "half records no netlist hash to pair on" in body
    assert "on the netlist it writes, so both halves measure the same one." in body


def test_a_superseded_reload_neither_installs_nor_blanks(tmp_path: Path):
    """Out-of-order ``/phy.json`` responses: the older must not install over a newer
    load, nor blank a fresh model on failure.
    """

    out = _node(
        _marked_js("load-generation")
        + """
        var state = { generation: 0 };
        var first = nextGeneration(state);
        var second = nextGeneration(state);
        console.log(JSON.stringify({
          tokens: [first, second],
          stale: applies(state, first),
          current: applies(state, second)
        }));
        """
    )
    seen = json.loads(out)
    assert seen["tokens"] == [1, 2]
    assert seen["stale"] is False
    assert seen["current"] is True

    js = _page_js()
    body = js.split("function load(dir) {")[1].split("\n  }")[0]
    # The token is taken before the request, so the response carries its own load.
    assert "var generation = nextGeneration(state);" in body
    assert body.index("nextGeneration(state)") < body.index("fetch(url")
    # Both arms guard; a stale failure would destroy visible data.
    assert body.count("if (!settle(state, generation)) { return; }") == 2
    success = body.split("}).then(function (res) {")[1]
    assert success.index("settle(state, generation)") < success.index("loadFailed(")
    assert success.index("settle(state, generation)") < success.index("ingest(")
    failure = body.split("}).catch(function (e) {")[1]
    assert failure.index("settle(state, generation)") < failure.index("loadFailed(")
    # The generation is not payload state; a failed load must not reset it.
    forget = js.split("function forgetModel() {")[1].split("\n  }")[0]
    assert "generation" not in forget


def test_the_instances_column_is_a_dash_outside_the_liberty_namespace():
    """A module name absent from the instances column (leaves carry Liberty cells) reads
    as the null dash, not ``0``; names the leaves carry keep their count.
    """

    js = _page_js()
    counted = js.split("function instanceCount(module) {")[1].split("\n  }")[0]
    # No power half at all is already a dash, and stays one.
    assert "if (!halfPresent('instances')) { return '\u2014'; }" in counted
    # Membership, not a falsy count.
    assert "Object.prototype.hasOwnProperty.call(counts, key)" in counted
    assert "return '\u2014';" in counted.split("hasOwnProperty")[1]
    # A name the leaves carry (Liberty cell or collision) prints its measured count.
    assert "return String(counts[key]);" in counted
    assert "|| 0" not in counted
    # The header explains the dash, so the column reads without the lens note.
    assert "instancesHead.title" in js
    assert "Em dash where the join cannot measure it" in js


def test_the_instance_window_is_bounded_and_moves_to_hold_the_selection():
    """The DOM is windowed: ``/phy.json`` holds the whole power half, so rendering every
    row per keystroke would freeze the tab. A selection deep in the ranking moves the
    window instead of growing it.
    """

    out = _node(
        _marked_js("row-window")
        + """
        function win(total, cap, selected) {
          var w = rowWindow(total, cap, selected);
          return [w.start, w.count];
        }
        console.log(JSON.stringify([
          win(100000, 500, -1),   // the cap bounds a huge design
          win(120, 500, -1),      // a small one is whole
          win(0, 500, -1),
          win(100000, 1000, -1),  // one 'show more' later
          win(100000, 100000, -1) // 'show all'
        ]));
        // A row selected from elsewhere is in the DOM even when it ranks
        // below the cap — a highlight nobody can see is not a highlight —
        // but the slice stays cap-sized wherever it lands.
        console.log(JSON.stringify([
          win(100000, 500, 8123),
          win(100000, 500, 12),    // already on the first page: unmoved
          win(100000, 500, 99999), // the very last row: the window ends there
          win(100000, 500, 500)    // one past the first page
        ]));
        """
    )
    bounded, selected = out.strip().splitlines()
    assert json.loads(bounded) == [[0, 500], [0, 120], [0, 0], [0, 1000], [0, 100000]]
    assert json.loads(selected) == [[7873, 500], [0, 500], [99500, 500], [250, 500]]
    # Whatever it does, the slice contains the selection and is capped.
    for (start, count), rank in zip(json.loads(selected), [8123, 12, 99999, 500]):
        assert count == 500
        assert start <= rank < start + count


def test_the_window_resets_when_the_row_set_changes():
    """A raised window resets when the matching set changes (sort, filter, lens), not
    when a row is selected.
    """

    js = _page_js()
    assert "var INSTANCE_WINDOW = 500;" in js
    assert "function resetInstanceWindow() { state.shown = INSTANCE_WINDOW; }" in js
    # Every change to which rows are in the table, including dropping the model on a
    # failed load.
    assert js.count("resetInstanceWindow();") == 8
    # Heat maxima are over every matching row, not the window, so tints do not rescale
    # on "show more".
    assert "maxes[column.key] = maxOf(rows, column.key);" in js
    assert "ranked.slice(win.start, win.start + win.count).forEach" in js


def test_the_window_control_offers_more_and_all():
    js = _page_js()
    assert "function renderWindowControl(win, total) {" in js
    assert "if (win.count >= total) { return; }" in js
    assert "' rows shown'" in js
    assert "'show ' + step.toLocaleString() + ' more'" in js
    assert "'show all ' + total.toLocaleString()" in js
    # A window that moved off the top says what it skipped, in both directions.
    assert "' above, '" in js
    assert "' below the selection)'" in js


def test_the_filter_rule_is_one_rule_for_both_of_its_readers():
    """``filterHides`` answers "is this row on screen" for the renderers and inbound
    focus alike.
    """

    out = _node(
        _marked_js("filter-hides")
        + """
        console.log(JSON.stringify([
          filterHides('', ['u_cpu/_31_', 'DFF_X1']),      // no filter: nothing hidden
          filterHides('cpu', ['u_cpu/_31_', 'DFF_X1']),   // the path matches
          filterHides('dff', ['u_cpu/_31_', 'DFF_X1']),   // the cell matches, cased
          filterHides('alu', ['u_cpu/_31_', 'DFF_X1']),   // neither: hidden
          filterHides('alu', [null, undefined]),          // a row with no text
          filterHides('sub', ['sub'])                     // the module table's one column
        ]));
        """
    )

    assert json.loads(out) == [False, False, False, True, True, False]


def test_an_inbound_focus_is_not_left_behind_the_search_box():
    """A focus onto a row the active filter hides removes the search, and only then."""

    js = _page_js()
    assert "function revealPastFilter(texts) {" in js
    assert "if (!filterHides(state.filter, texts)) { return; }" in js
    assert "state.filter = '';" in js
    assert "els.search.value = '';" in js
    # Both focus paths, each offering the columns its own table filters on.
    assert "revealPastFilter([name]);" in js
    assert "revealPastFilter([row.instance_path, row.module]);" in js
    # And the renderers ask the same question of the same helper.
    assert "return !filterHides(state.filter, [row.instance_path, row.module]);" in js
    assert "return !filterHides(state.filter, [text]);" in js


def test_a_focus_that_took_the_search_off_says_so():
    """Both ingresses print through ``focusNote``, which tells the reader which control
    the focus moved.
    """

    js = _page_js()
    assert "searchCleared: false," in js
    assert "state.searchCleared = true;" in js
    assert "return text + ' (search cleared to show it)';" in js
    # Read once and cleared: the flag belongs to one focus.
    assert "state.searchCleared = false;\n    return text" in js
    assert "if (focusInstance(ip, true)) { note(focusNote('selected ' + ip)); }" in js
    assert "note(ok ? focusNote('focused ' + target)" in js


def test_row_clicks_are_delegated_to_the_table_body():
    """One listener per table, not per row, to avoid thousands of closures per
    keystroke.
    """

    js = _page_js()
    assert "function delegate(tbody, attribute, activate) {" in js
    assert "delegate(tbody, 'data-module', activateModule);" in js
    assert "delegate(tbody, 'data-path', activateInstance);" in js
    # The row carries its identity in an attribute instead of a closure.
    assert "tr.setAttribute('data-module'" in js
    assert "tr.setAttribute('data-path'" in js
    # No row wires up its own listener.
    assert "tr.addEventListener(" not in js


def test_an_early_selection_is_held_until_the_model_arrives():
    """A ``selection_changed`` replayed after ``welcome`` is held until the
    ``/phy.json`` fetch lands, like ``phys_focus``.
    """

    js = _page_js()
    assert "pendingSelection: null" in js
    assert "state.pendingSelection = ip;" in js
    # Applied at ingest, and an explicit focus outranks a passive one.
    assert "var focus = state.pending, selection = state.pendingSelection;" in js
    assert "} else if (selection) {" in js
    assert "focusInstanceFromWire(selection);" in js


def test_a_focus_arriving_mid_reload_waits_for_the_new_model():
    """A focus arriving while ``/phy.json`` is in flight is held and applied to the
    incoming model, not resolved against the outgoing rows.
    """

    # A load is in flight from taking its token until its own response settles it; a
    # superseded response settles nothing.
    out = _node(
        _marked_js("load-generation")
        + """
        var state = { generation: 0, inFlight: 0 };
        var seen = [loadInFlight(state)];              // idle before any load
        var first = nextGeneration(state);
        seen.push(loadInFlight(state));                // out
        var second = nextGeneration(state);
        seen.push(settle(state, first));               // the stale one settles
        seen.push(loadInFlight(state));                // ...nothing: still out
        seen.push(settle(state, second));
        seen.push(loadInFlight(state));                // landed
        console.log(JSON.stringify(seen));
        """
    )
    assert json.loads(out) == [False, True, False, True, True, False]

    js = _page_js()
    # Both inbound paths take the same gate and use the existing pending slots.
    assert "if (!state.payload || loadInFlight(state)) {" in js
    assert js.count("if (!state.payload || loadInFlight(state)) {") == 2
    focus = js.split("function applyFocus(payload) {")[1]
    assert focus.index("loadInFlight(state)") < focus.index("state.pending = payload;")
    wire = js.split("function focusInstanceFromWire(ip) {")[1]
    assert wire.index("loadInFlight(state)") < wire.index(
        "state.pendingSelection = ip;"
    )
    # Ingest installs the new payload before draining held targets.
    ingest = js.split("function ingest(payload) {")[1].split("\n  }")[0]
    assert ingest.index("state.payload = payload;") < ingest.index("drainPending();")


def test_a_refused_switch_still_answers_the_focus_it_held():
    """A ``dir=`` the server refuses keeps the current run (``loadFailed``), so a focus
    held for that switch is drained.
    """

    js = _page_js()
    failed = js.split("function loadFailed(requested, message) {")[1].split("\n  }")[0]
    # Only on the arm that keeps the model; ``showEmpty`` drops the payload and a held
    # target waits for a real one.
    assert failed.count("drainPending();") == 1
    assert failed.index("drainPending();") < failed.index("showEmpty(message);")
    assert js.count("drainPending();") == 2
    assert "function drainPending() {" in js


def test_the_first_hello_is_polite():
    """A hello is polite by default, so a second tab does not start an eviction war."""

    out = _node(
        _marked_js("hello-payload")
        + """
        console.log(JSON.stringify(helloPayload(false)));
        console.log(JSON.stringify(helloPayload(true)));
        """
    )
    polite, forceful = out.strip().splitlines()
    assert json.loads(polite) == {
        "client": "phys",
        "version": "1.0.0",
        "capabilities": ["phys_focus"],
    }
    assert json.loads(forceful)["takeover"] is True


def test_version_label_agrees_with_the_other_panes():
    """The lockstep cases pinned in the graph, cov and landing tests."""

    out = _node(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel('6.26.2.dev13+g3f5b890e3.d20260806'),
          versionLabel('6.26.2'),
          versionLabel('1.0+gabc'),
          versionLabel(''),
          versionLabel('+g3f5b890e3')
        ]));
        """
    )
    assert json.loads(out) == [
        "6.26.2.dev13 @ 3f5b890e3",
        "6.26.2",
        "1.0",
        None,
        None,
    ]


def _http_get(url: str) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5.0) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest_asyncio.fixture
async def hub_and_viewer(
    phys_project: Path,
) -> AsyncIterator[tuple[HubServer, ViewerServer]]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())

    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        project_root=phys_project,
        hub_server=hub,
    )
    await viewer.start()
    viewer_task = asyncio.create_task(viewer.serve_forever())
    try:
        yield hub, viewer
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (viewer_task, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_http_phys_page_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/phy"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert f"{viewer.hub_host}:{viewer.hub_port}".encode("utf-8") in body
    assert b"rtl-buddy-phy" in body


@pytest.mark.asyncio
async def test_http_phys_json_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/phy.json"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    payload = json.loads(body)
    assert payload["hub"]["schema_version"] == phys_page.PAGE_SCHEMA_VERSION
    assert payload["totals"]["cell_count"] == 162
    assert payload["modules"][0]["module"] == "blk"


@pytest.mark.asyncio
async def test_http_phys_json_selects_a_run_by_dir(hub_and_viewer):
    """Bare ``/phy.json`` is the newest run, ``?dir=`` picks one, and a path outside the
    project root is refused before anything is read.
    """

    _hub, viewer = hub_and_viewer
    base = f"http://127.0.0.1:{viewer.http_port}/phy.json"

    _status, _headers, body = await asyncio.to_thread(_http_get, base)
    assert json.loads(body)["run"] == "both"

    _status, _headers, body = await asyncio.to_thread(
        _http_get, base + "?dir=verif/blk/artefacts/old_synth"
    )
    payload = json.loads(body)
    assert payload["run"] == "old_synth"
    assert payload["runs"]["count"] == 2

    with pytest.raises(urllib.error.HTTPError) as refused:
        await asyncio.to_thread(_http_get, base + "?dir=../elsewhere")
    assert refused.value.code == 403


@pytest.mark.asyncio
async def test_http_index_advertises_the_phy_url(hub_and_viewer):
    """The hub sets the global that gates the SPA's ``/phy`` app-switcher entry."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/view"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    assert b"window.__RTL_BUDDY_PHY_URL__ = '/phy.json'" in body


@pytest.mark.asyncio
async def test_hub_state_marks_the_phys_card_available(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/hub/state.json"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    cards = {app["id"]: app for app in json.loads(body)["apps"]}
    assert cards["phys"]["available"] is True
    assert cards["phys"]["route"] == phys_page.PHYS_PAGE_ROUTE
    assert cards["phys"]["origin"] == Origin.PHYS.value


def test_index_omits_phy_url_without_physical_artefacts():
    body = render_index_html(bundle_index=None, hub_addr="127.0.0.1:1")
    assert b"__RTL_BUDDY_PHY_URL__" not in body


@pytest.mark.asyncio
async def test_http_phys_json_404s_without_artefacts(tmp_path: Path):
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host, hub_port=hub_port, http_port=0, project_root=tmp_path
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(
                _http_get, f"http://127.0.0.1:{viewer.http_port}/phy.json"
            )
        assert excinfo.value.code == 404
        assert "rb synth" in json.loads(excinfo.value.read())["error"]

        # The page is still 200; its empty state says to run a synthesis.
        page_status, _h, _b = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{viewer.http_port}/phy"
        )
        assert page_status == 200
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (vtask, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_http_phys_json_400_without_project_root():
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(hub_host=hub_host, hub_port=hub_port, http_port=0)
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(
                _http_get, f"http://127.0.0.1:{viewer.http_port}/phy.json"
            )
        assert excinfo.value.code == 400
        assert "project_root" in json.loads(excinfo.value.read())["error"]
    finally:
        await viewer.shutdown()
        await hub.shutdown()
        for t in (vtask, hub_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.parametrize(
    "payload",
    [
        {"target": "u_sub/_64_"},
        {"target": "instance:u_sub/_64_", "metric": "dynamic"},
        {"target": "module:sub", "metric": "area"},
        {"target": "module:sub", "metric": "cells"},
    ],
)
def test_phys_focus_envelope_validates(payload: dict):
    env = Envelope(
        origin=Origin.CLI,
        kind=Kind.EVENT,
        type="phys_focus",
        id=new_id(),
        payload=payload,
    )
    assert decode(encode(env).encode("utf-8")).payload == payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"target": ""},
        # ``switching`` is a model column but not a focus metric; the enum offers
        # ``dynamic``, its sum with ``internal``.
        {"target": "module:sub", "metric": "switching"},
        {"target": "module:sub", "metric": "line"},
        {"target": "module:sub", "line": 4},
        {"target": "module:sub", "extra": 1},
        {"target": "module:sub", "metric": None},
    ],
)
def test_phys_focus_rejects_malformed_payloads(payload: dict):
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="phys_focus",
                id=new_id(),
                payload=payload,
            )
        )


def test_phys_origin_is_its_own_peer_slot():
    """The pane registers as ``phys``, not ``view``, ``graph`` or ``cov``; a shared slot
    would evict the tab it drives.
    """

    assert Origin.PHYS.value == "phys"
    env = Envelope(
        origin=Origin.PHYS,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={
            "client": "phys",
            "version": "1.0.0",
            "capabilities": ["phys_focus"],
        },
    )
    assert decode(encode(env).encode("utf-8")).origin is Origin.PHYS


def test_phys_focus_state_slot_omits_an_unset_metric():
    """An unset metric is absent on the wire, not null (``additionalProperties:
    false``).
    """

    assert PhysFocus(target="module:sub", origin=Origin.CLI).payload() == {
        "target": "module:sub"
    }
    assert PhysFocus(
        target="instance:u_sub/_64_", origin=Origin.CLI, metric="leakage"
    ).payload() == {"target": "instance:u_sub/_64_", "metric": "leakage"}


class _Peer:
    """Minimal TCP peer, same shape as ``test_hub_cov_page``."""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "_Peer":
        r, w = await asyncio.open_connection(host, port)
        return cls(r, w)

    async def send(self, env: Envelope) -> None:
        self.writer.write(encode(env).encode("utf-8") + b"\n")
        await self.writer.drain()

    async def recv(self, *, timeout: float = 2.0) -> Envelope:
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        return decode(line)

    async def hello(self, origin: Origin) -> Envelope:
        await self.send(
            Envelope(
                origin=origin,
                kind=Kind.REQUEST,
                type="hello",
                id=new_id(),
                payload={
                    "client": origin.value,
                    "version": "0.1",
                    "capabilities": [],
                },
            )
        )
        return await self.recv()

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest_asyncio.fixture
async def bare_hub() -> AsyncIterator[HubServer]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    await hub.start()
    task = asyncio.create_task(hub.serve_forever())
    try:
        yield hub
    finally:
        await hub.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_phys_focus_broadcasts_to_the_pane(bare_hub: HubServer):
    pane = await _Peer.connect(bare_hub.host, bare_hub.port)
    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        assert (await pane.hello(Origin.PHYS)).type == "welcome"
        assert (await driver.hello(Origin.CLI)).type == "welcome"
        assert (await pane.recv()).type == "peer_joined"

        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="phys_focus",
                id=new_id(),
                payload={"target": "module:sub", "metric": "area"},
            )
        )
        env = await pane.recv()
        assert env.type == "phys_focus"
        assert env.origin is Origin.CLI
        assert env.payload == {"target": "module:sub", "metric": "area"}
    finally:
        await pane.close()
        await driver.close()


@pytest.mark.asyncio
async def test_phys_focus_is_replayed_to_a_late_pane(bare_hub: HubServer):
    """A ``phys-focus`` sent before the tab opens is replayed with its metric."""

    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        await driver.hello(Origin.CLI)
        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="phys_focus",
                id=new_id(),
                payload={"target": "instance:u_sub/_64_", "metric": "leakage"},
            )
        )
        await asyncio.sleep(0.1)
        assert bare_hub.state.phys_focus is not None
        assert bare_hub.state.phys_focus.target == "instance:u_sub/_64_"
        assert bare_hub.state.phys_focus.metric == "leakage"

        pane = await _Peer.connect(bare_hub.host, bare_hub.port)
        try:
            assert (await pane.hello(Origin.PHYS)).type == "welcome"
            replayed = await pane.recv()
            assert replayed.type == "phys_focus"
            assert replayed.payload == {
                "target": "instance:u_sub/_64_",
                "metric": "leakage",
            }
        finally:
            await pane.close()
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_latest_writer_wins_one_slot_no_history(bare_hub: HubServer):
    """One slot, no backlog: a late pane opens on the most recent target."""

    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        await driver.hello(Origin.CLI)
        for target in ("module:blk", "module:sub"):
            await driver.send(
                Envelope(
                    origin=Origin.CLI,
                    kind=Kind.EVENT,
                    type="phys_focus",
                    id=new_id(),
                    payload={"target": target},
                )
            )
        await asyncio.sleep(0.1)
        assert bare_hub.state.phys_focus.target == "module:sub"

        pane = await _Peer.connect(bare_hub.host, bare_hub.port)
        try:
            await pane.hello(Origin.PHYS)
            replayed = await pane.recv()
            assert replayed.payload == {"target": "module:sub"}
            # Nothing else is queued; ``peer_joined`` for the driver is the only other
            # traffic.
            with pytest.raises(asyncio.TimeoutError):
                while True:
                    nxt = await pane.recv(timeout=0.4)
                    assert nxt.type != "phys_focus"
        finally:
            await pane.close()
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_hub_state_reset_clears_the_phys_slot(bare_hub: HubServer):
    bare_hub.state.phys_focus = PhysFocus(target="module:sub", origin=Origin.CLI)
    bare_hub.state.reset()
    assert bare_hub.state.phys_focus is None


def test_the_origin_label_map_renames_only_the_display():
    out = _node(
        _marked_js("origin-labels")
        + """
        var origins = ['view', 'graph', 'cov', 'phys', 'wave', 'src', 'cli',
                       'notebook', 'quantum'];
        console.log(JSON.stringify(origins.map(originLabel)));
        console.log(JSON.stringify([originLabel(null), originLabel(undefined),
                                    originLabel('')]));
        console.log(JSON.stringify(originLabel('toString')));
        """
    )
    labelled, nullish, inherited = out.strip().splitlines()
    assert json.loads(labelled) == [
        "sch",
        "gph",
        "cov",
        "phy",
        "wave",
        "src",
        "cli",
        "notebook",
        "quantum",
    ]
    assert json.loads(nullish) == ["", "", ""]
    assert json.loads(inherited) == "toString"


def test_every_rendered_origin_goes_through_the_map():
    js = _page_js()
    # The same map, word for word, as the other panes' and the landing's.
    assert "var ORIGIN_LABELS = { view: 'sch', graph: 'gph', phys: 'phy' };" in js
    assert "list.map(originLabel).join(', ')" in js
    assert "originLabel(links[i].getAttribute('data-origin'))" in js


def test_the_rename_did_not_leak_into_the_wire():
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    assert "origin: 'phys', kind: 'event'" in js
    assert 'data-origin="view"' in body
    assert 'data-origin="graph"' in body
    assert 'data-origin="cov"' in body
    assert 'href="/sch"' in body
    assert 'href="/gph"' in body
    assert 'href="/cov"' in body


def test_bare_phy_json_still_serves_the_newest_run(phys_project: Path):
    """The default is the run that finished last."""

    status, body = phys_page.phys_payload_bytes(phys_project)
    payload = json.loads(body)

    assert status == 200
    assert payload["run"] == "both"
    assert payload["runs"]["runs"][0]["newest"] is True


def test_a_dir_query_selects_that_run(phys_project: Path):
    status, body = phys_page.phys_payload_bytes(
        phys_project, requested_dir="verif/blk/artefacts/old_synth"
    )
    payload = json.loads(body)

    assert status == 200
    assert payload["run"] == "old_synth"
    # The menu stays whole, so the reader can switch back without a request.
    assert [entry["run"] for entry in payload["runs"]["runs"]] == [
        "both",
        "old_synth",
    ]


def test_a_dir_outside_the_project_is_refused(phys_project: Path, tmp_path: Path):
    """``?dir=`` is untrusted input from any client that can reach the port."""

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for requested in ("../elsewhere", str(outside), "verif/../../elsewhere"):
        status, body = phys_page.phys_payload_bytes(
            phys_project, requested_dir=requested
        )
        assert status == 403, requested
        assert "outside the project root" in json.loads(body)["error"]


def test_a_dir_with_no_manifest_is_a_404_naming_the_listing(phys_project: Path):
    (phys_project / "verif" / "blk" / "artefacts" / "empty").mkdir()

    status, body = phys_page.phys_payload_bytes(
        phys_project, requested_dir="verif/blk/artefacts/empty"
    )

    assert status == 404
    error = json.loads(body)["error"]
    assert "phys-manifest.json" in error and "rb phys runs" in error


def test_containment_admits_the_artefacts_symlink_layout(tmp_path: Path):
    """A suite whose ``artefacts/`` links to scratch storage is a supported layout, so
    the containment test must not resolve links first.
    """

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "verif" / "blk").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    (scratch / "nightly").mkdir(parents=True)
    (root / "verif" / "blk" / "artefacts").symlink_to(scratch)

    admitted = phys_page.contained_phys_dir(root, "verif/blk/artefacts/nightly")
    assert admitted == root / "verif" / "blk" / "artefacts" / "nightly"
    # And the traversal it still refuses.
    assert phys_page.contained_phys_dir(root, "../scratch") is None


def test_containment_refuses_an_in_project_link_that_leaves_the_project(
    tmp_path: Path,
):
    """A logical path under the root is admitted without resolving. Where it resolves
    outside the root, the route also asks discovery's predicate (an ``artefacts``
    component below the root), so links such as `vendor/` or `$HOME` are refused.
    """

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "verif" / "blk" / "artefacts").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "nightly").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    (scratch / "nightly").mkdir(parents=True)

    # Not part of the artefact layout: discovery will not walk it.
    (root / "vendor").symlink_to(elsewhere)
    # An ``artefacts`` link inside a suite: the supported layout.
    (root / "verif" / "blk" / "artefacts" / "runs").symlink_to(scratch)

    assert phys_page.contained_phys_dir(root, "vendor/nightly") is None
    assert phys_page.contained_phys_dir(root, "vendor") is None
    assert phys_page.contained_phys_dir(root, "verif/blk/artefacts/runs/nightly") == (
        root / "verif" / "blk" / "artefacts" / "runs" / "nightly"
    )


def test_the_route_and_discovery_draw_one_boundary(tmp_path: Path):
    """The route and discovery use the same predicate: a directory the walk refuses is
    unreadable.
    """
    from rtl_buddy.phys import manifest as manifest_mod

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "verif" / "blk").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "nightly").mkdir(parents=True)
    (root / "verif" / "blk" / "artefacts").symlink_to(outside)
    (root / "vendor").symlink_to(outside)
    _write_run(root, "nightly", modules=MODULE_ROWS)

    walked = manifest_mod.discover_manifests(root)
    assert [os.path.relpath(path, root) for path in walked] == [
        os.path.join("verif", "blk", "artefacts", "nightly", "phys-manifest.json")
    ]
    # The run the walk found is readable; the same bytes under a refused name are not.
    assert phys_page.contained_phys_dir(root, "verif/blk/artefacts/nightly")
    assert phys_page.contained_phys_dir(root, "vendor/nightly") is None


def test_containment_judges_each_crossed_link_not_the_endpoint(tmp_path: Path):
    """The predicate is applied per link, not once to the whole path: ``vendor ->
    /srv/other`` with ``?dir=vendor/artefacts/run`` is refused even though a
    component below the link is named ``artefacts``.
    """
    from rtl_buddy.phys import manifest as manifest_mod

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    outside = tmp_path / "srv" / "other"
    (outside / "artefacts" / "run").mkdir(parents=True)
    _write_run(outside, "run", modules=MODULE_ROWS, artefacts=outside / "artefacts")
    (root / "vendor").symlink_to(outside)

    # The walk enters nothing: ``vendor`` is a link whose own position has no
    # ``artefacts``.
    assert manifest_mod.discover_manifests(root) == []
    assert phys_page.contained_phys_dir(root, "vendor/artefacts/run") is None
    # Nor any deeper spelling that buries the name further down.
    assert phys_page.contained_phys_dir(root, "vendor/artefacts/run/.") is None

    status, body = phys_page.phys_payload_bytes(
        root, requested_dir="vendor/artefacts/run"
    )
    assert status == 403
    assert "vendor/artefacts/run" in json.loads(body)["error"]


def test_containment_still_serves_a_run_behind_the_artefacts_link(tmp_path: Path):
    """Judging each link on its own position keeps the supported layout, where the link
    is the ``artefacts`` component.
    """
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "verif" / "blk").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (root / "verif" / "blk" / "artefacts").symlink_to(scratch)
    _write_run(root, "nightly", modules=MODULE_ROWS)

    admitted = phys_page.contained_phys_dir(root, "verif/blk/artefacts/nightly")
    assert admitted == root / "verif" / "blk" / "artefacts" / "nightly"

    status, body = phys_page.phys_payload_bytes(
        root, requested_dir="verif/blk/artefacts/nightly"
    )
    assert status == 200
    assert json.loads(body)["run"] == "nightly"


def test_every_route_the_run_listing_offers_is_one_the_route_serves(tmp_path: Path):
    """Every ``?dir=`` spelling in the ``runs`` block passes the containment test, since
    discovery and the route use the same predicate.
    """
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "verif" / "blk").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (root / "verif" / "blk" / "artefacts").symlink_to(scratch)
    _write_run(root, "nightly", modules=MODULE_ROWS)
    # And a tree the walk refuses, carrying a run of its own.
    outside = tmp_path / "srv" / "other"
    (outside / "artefacts").mkdir(parents=True)
    _write_run(
        outside, "stranger", modules=MODULE_ROWS, artefacts=outside / "artefacts"
    )
    (root / "vendor").symlink_to(outside)

    status, body = phys_page.phys_payload_bytes(root)
    listing = json.loads(body)["runs"]["runs"]

    assert [entry["run"] for entry in listing] == ["nightly"]
    for entry in listing:
        assert phys_page.contained_phys_dir(root, entry["phys_dir"]) is not None
    assert phys_page.contained_phys_dir(root, "vendor/artefacts/stranger") is None


def test_the_runs_block_is_bounded_and_says_it_is(tmp_path: Path):
    """The ``runs`` block carries the untruncated count, so the pane can say the list is
    a head.
    """

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for index in range(4):
        _write_run(root, f"run{index}", modules=MODULE_ROWS, mtime=1_000_000 + index)

    payload = phys_page.build_phys_payload(root, runs_limit=2)

    assert payload["runs"]["count"] == 4
    assert len(payload["runs"]["runs"]) == 2
    assert [entry["run"] for entry in payload["runs"]["runs"]] == ["run3", "run2"]


def test_run_entries_read_as_one_line_that_tells_two_runs_apart():
    """The dropdown label shows the parts after the top, since runs of one design share
    it.
    """

    out = _node(
        _marked_js("run-selector")
        + """
        var saif = {
          run: 'nightly', top: 'blk',
          backends: { synth: 'yosys', power: 'openroad' },
          mode: 'dynamic', activity: { label: 'saif csr_smoke' },
          xplr: { id: 'exp-0007' }
        };
        var bare = { run: 'quick', top: 'blk',
                     backends: { synth: null, power: null } };
        var powerOnly = { phys_dir: 'verif/blk/artefacts/p', top: 'blk',
                          backends: { synth: null, power: 'openroad' },
                          mode: 'static', activity: { label: 'defaults' } };
        console.log(JSON.stringify([
          runLabel(saif), runLabel(bare), runLabel(powerOnly)
        ]));
        """
    )
    saif, bare, power_only = json.loads(out)
    assert saif == (
        "nightly · blk · yosys+openroad · dynamic (saif csr_smoke) · exp-0007"
    )
    # Nothing recorded is nothing shown — no `none`, no empty separators.
    assert bare == "quick · blk"
    # An unnamed run falls back to its directory, the key its entry selects with.
    assert power_only == "verif/blk/artefacts/p · blk · openroad · static (defaults)"


def test_the_shown_run_and_the_newest_are_marked_separately():
    """The pane shows both where the reader is and where the newest is. Choosing the
    newest entry returns the pane to follow-newest instead of pinning a directory
    (see ``runValue``).
    """

    out = _node(
        _marked_js("run-selector")
        + """
        var shown = 'verif/blk/artefacts/old/phys-manifest.json';
        console.log(JSON.stringify([
          runMarks({ manifest: shown, newest: false }, shown),
          runMarks({ manifest: 'other', newest: true }, shown),
          runMarks({ manifest: shown, newest: true }, shown),
          runMarks({ manifest: 'other', newest: false }, shown),
          runMarks({ manifest: 'other', newest: false }, null)
        ]));
        """
    )
    assert json.loads(out) == [
        " [shown]",
        " [newest — follows]",
        " [shown, newest — follows]",
        "",
        "",
    ]


def test_choosing_the_newest_run_keeps_following_discovery():
    """Choosing the newest entry selects with the empty value, so the load fetches bare
    ``/phy.json`` and keeps following the newest. An explicitly chosen older run
    still pins.
    """

    out = _node(
        _marked_js("run-selector")
        + _marked_js("run-url")
        + """
        var newest = { phys_dir: 'verif/blk/artefacts/nightly', newest: true };
        var older = { phys_dir: 'verif/blk/artefacts/old', newest: false };
        var unlisted = { phys_dir: '', newest: false };
        console.log(JSON.stringify({
          newest: runValue(newest),
          older: runValue(older),
          unlisted: runValue(unlisted),
          followUrl: phyUrl('/phy.json', requestedDir(runValue(newest))),
          pinnedUrl: phyUrl('/phy.json', requestedDir(runValue(older))),
          requested: [requestedDir(''), requestedDir(runValue(older))]
        }));
        """
    )
    picked = json.loads(out)

    # The newest run is selected by the absence of a directory, which is "follow
    # discovery" on the wire.
    assert picked["newest"] == ""
    assert picked["followUrl"] == "/phy.json"
    assert picked["requested"][0] is None
    # An older run is pinned.
    assert picked["older"] == "verif/blk/artefacts/old"
    assert picked["pinnedUrl"] == "/phy.json?dir=verif%2Fblk%2Fartefacts%2Fold"
    assert picked["requested"][1] == "verif/blk/artefacts/old"
    assert picked["unlisted"] == ""


def test_the_run_url_appends_its_query_to_a_base_that_has_one():
    """`PHY_URL` is injected, so it is not always the bare route."""

    out = _node(
        _marked_js("run-url")
        + """
        console.log(JSON.stringify([
          phyUrl('/phy.json?token=x', 'verif/b/artefacts/n'),
          phyUrl('/phy.json?token=x', null)
        ]));
        """
    )
    with_query, bare = json.loads(out)
    assert with_query == "/phy.json?token=x&dir=verif%2Fb%2Fartefacts%2Fn"
    assert bare == "/phy.json?token=x"


def test_the_run_on_screen_is_an_entry_even_when_the_listing_headed_it_off():
    """The listing is bounded, so the run being shown may not be in it; the selector
    must agree with the page.
    """

    out = _node(
        _marked_js("run-selector")
        + """
        var payload = {
          run: 'old', top: 'blk', manifest: 'verif/blk/artefacts/old/m.json',
          artefacts: { phys_dir: 'verif/blk/artefacts/old' },
          backends: { synth: 'yosys', power: null },
          power_mode: null, power_activity: null, xplr: null
        };
        var entry = shownEntry(payload);
        console.log(JSON.stringify([
          entry.phys_dir, entry.manifest, entry.newest, runLabel(entry),
          shownEntry(null)
        ]));
        """
    )
    phys_dir, manifest, newest, label, empty = json.loads(out)
    assert phys_dir == "verif/blk/artefacts/old"
    assert manifest == "verif/blk/artefacts/old/m.json"
    # Never marked newest: the payload header cannot know, and it would give the list
    # two newest marks.
    assert newest is False
    assert label == "old · blk · yosys"
    assert empty is None


def test_switching_runs_refetches_rather_than_filtering_client_side():
    """The dropdown re-fetches with ``?dir=`` through the ordinary ingest, where
    ``modelIdentity`` drops the lens and selection of the run being left.
    """

    js = _page_js()
    assert '<select id="run-select">' in phys_page.PHYS_PAGE_HTML
    assert "'dir=' + encodeURIComponent(dir)" in js
    listener = js.split("els.runSelect.addEventListener('change', function () {")[1]
    assert "load(els.runSelect.value);" in listener.split("});")[0]
    # Reload re-reads the run shown, not the newest.
    reload_body = js.split("document.getElementById('reload').addEventListener(")[1]
    assert "load(state.dir);" in reload_body.split("});")[0]


def test_a_refused_switch_keeps_the_run_that_is_on_screen():
    """A 403 or 404 concerns the run asked for; the pane keeps the model on screen."""

    js = _page_js()
    body = js.split("function loadFailed(requested, message) {")[1].split("\n  }")[0]
    assert "if (state.payload) {" in body
    assert "renderRunPicker();" in body
    assert "showEmpty(message);" in body
    # A refused directory never becomes the run the pane thinks it shows, so the next
    # reload does not ask for it again.
    load_body = js.split("function load(dir) {")[1].split("\n  }")[0]
    success = load_body.split("}).then(function (res) {")[1]
    assert success.index("loadFailed(") < success.index("state.dir = requested;")


def test_focus_applies_to_the_run_the_pane_is_showing():
    """``phys_focus`` is unchanged on the wire: the pane addresses one run, chosen by
    the reader.
    """

    js = _page_js()
    assert "An inbound `phys_focus` applies to the run this pane is SHOWING." in js
    # Only two fields are read off the envelope; a run hint would let a sender move a
    # view its user is working in and would need a schema bump.
    focus = js.split("function applyFocus(payload) {")[1].split("\n  }")[0]
    assert "payload.target" in focus and "payload.metric" in focus
    assert "payload.run" not in focus and "payload.phys_dir" not in focus
    assert "payload.dir" not in focus
    # And the pane sends none either — it is a phys_focus consumer only.
    assert "emit('phys_focus'" not in js
