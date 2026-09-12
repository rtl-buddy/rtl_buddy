"""Tests for the hub-served synth+power pane (rtl-buddy/rtl_buddy#558).

Modelled on ``test_hub_cov_page.py``, because the pane is modelled on the
coverage pane. Five surfaces, in the order a user meets them:

1. ``GET /phy.json`` — the newest run's physical model + manifest,
   assembled by the *same* builder ``rb phys summary`` uses. The point
   pinned here is that the numbers agree: a pane that recomputed totals
   would eventually disagree with the CLI, and the disagreement would be
   discovered by a person defending an area number in a review.
2. ``GET /phy`` — one self-contained HTML document. The offline rule is
   checked structurally (no external ``src``/``href``, no CDN host),
   because "it worked on my laptop" is exactly the failure mode a hub on
   an air-gapped build machine hits.
3. Presence + advertisement — the landing card and the SPA's
   ``__RTL_BUDDY_PHY_URL__`` global follow discovered artefacts, not a
   build-time flag.
4. ``phys_focus`` — the wire type behind ``rb hub send phys-focus``:
   schema-valid, broadcast to peers, and replayed to a pane that
   connects after the fact, which is what makes "send it before the tab
   is open" work.
5. The origin→label map, the seam between the wire's ``phys`` and the
   chrome's ``phy``.

The page is static HTML plus one inline script, so what can be asserted
server-side is its *structure*. Where the behaviour is genuinely a
function — the null rules, the dynamic-power sum, the ranking — the
function is sliced out of the page between markers and exercised in
``node``, which is the only rig the repo has and needs none of a DOM.
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


# ---------------------------------------------------------------------------
# fixtures — one run's physical artefacts on disk
#
# Written with the phase-1 producers rather than by hand, the same rule
# ``test_phys_query.py`` follows: a fixture that invented its own
# document shape would keep passing after the producers stopped writing
# that shape.
# ---------------------------------------------------------------------------

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


def _write_run(root: Path, run: str, *, modules=None, instances=None, mtime=None):
    """One run's artefact directory, written the way the producers do."""

    phys_dir = root / "verif" / "blk" / "artefacts" / run
    phys_dir.mkdir(parents=True, exist_ok=True)

    model = None
    if modules is not None:
        model = build_synth_model(
            top="blk", modules=modules, area_um2=576.5, gate_count=162
        )
    if instances is not None:
        power = build_power_model(
            top="blk",
            instances=instances,
            internal_w=2.78e-6,
            switching_w=0.3175e-6,
            leakage_w=0.08e-6,
            total_w=3.171e-6,
        )
        model = (
            merge_model(model, power, own_half="instances")
            if model is not None
            else power
        )
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
    """Presence is memoised for five seconds, keyed on the root path.

    ``tmp_path`` is unique per test so a stale hit is unlikely, but the
    cache is module state and a test that asserts on a *miss* must not
    depend on that.
    """

    phys_page._presence_cache.clear()  # noqa: SLF001
    yield
    phys_page._presence_cache.clear()  # noqa: SLF001


# ---------------------------------------------------------------------------
# build_phys_payload
# ---------------------------------------------------------------------------


def test_payload_is_the_cli_builder_plus_a_hub_block(phys_project: Path):
    """The pane and ``rb phys summary`` may differ in presentation, never
    in numbers — so the payload IS the query builder's output."""

    payload = phys_page.build_phys_payload(phys_project)
    ctx = phys_query.load_context(phys_project)
    expected = phys_query.summary_payload(ctx, limit=0)
    hub = payload.pop("hub")
    assert payload == expected
    assert hub["schema_version"] == phys_page.PAGE_SCHEMA_VERSION
    assert hub["metrics"] == ["cells", "area", "leakage", "dynamic", "total"]
    assert hub["power_columns"] == list(phys_query.POWER_COLUMNS)
    assert hub["model"].endswith("artefacts/both/phys-model.json")


def test_payload_truncates_nothing(phys_project: Path):
    """``limit=0``: a terminal that printed every leaf instance is a
    terminal nobody reads, which is why the verb truncates; a table that
    cannot show the row you are looking for is broken, which is why the
    pane does not.

    The rankings ARE the raw rows at that limit — a permutation, not a
    head — which is why the payload carries no second copy of them.
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
    """Both halves of the sanity block ride along: the flows' own log
    scrape (``totals``) and the rows a different scrape produced. The
    pane compares them; neither is derived from the other."""

    payload = phys_page.build_phys_payload(phys_project)
    assert payload["totals"]["cell_count"] == 162
    assert payload["totals"]["area_um2"] == 576.5
    assert payload["halves"]["modules"]["present"] is True
    assert payload["halves"]["instances"]["produced_by"] == "rb power"
    assert payload["missing_halves"] == []
    assert payload["counts"] == {"modules": 3, "instances": 2}
    assert payload["artefacts"]["synth_netlist"].endswith("synth_netlist.v")


def test_a_half_the_run_did_not_produce_is_named_not_guessed(tmp_path: Path):
    """An empty ranking is ambiguous on its own — "no synthesis ran" and
    "a synthesis ran and found no cells" are the same empty list — so
    the pane reads ``halves``/``missing_halves``, and they have to be
    there."""

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _write_run(root, "power_only", instances=INSTANCE_ROWS)

    payload = phys_page.build_phys_payload(root)
    assert payload["modules"] == []
    assert payload["halves"]["modules"] == {
        "present": False,
        "rows": None,
        "produced_by": "rb synth",
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
    """A walk per landing poll would be a walk per second on a big tree,
    and this walk cannot even shortcut on a directory name."""

    calls = []
    real = manifest_mod.discover_manifests

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(phys_page.manifest_mod, "discover_manifests", counted)
    assert phys_page.phys_data_present(phys_project) is True
    assert phys_page.phys_data_present(phys_project) is True
    assert len(calls) == 1
    # ttl=0 is the "ask again now" escape hatch the tests (and a future
    # explicit refresh) need.
    assert phys_page.phys_data_present(phys_project, ttl=0) is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# render_phys_html — the offline rule
# ---------------------------------------------------------------------------


def test_page_injects_hub_address():
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_PHY_URL__ = '/phy.json'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """No CDN, no remote font, no import, no off-machine reference.

    Every ``src``/``href`` that is not a page anchor must be a
    same-origin absolute path served by this same hub process, so a hub
    on a machine with no route off localhost still renders the pane.
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
    # Light default (#398), and the fallback BEFORE the link, or it would
    # out-rank the sheet at equal specificity and kill dark mode.
    assert "--bg:          #f8fafc;" in body
    assert body.index("--bg:          #f8fafc;") < body.index('href="/hub/theme.css"')


def test_the_heat_ramp_lives_in_the_sheet_not_in_the_page():
    """The cov pane carries a comment scar about exactly this: its ramp's
    saturation used to be page-local, which is one ramp in two places.

    So the phys ramp's endpoints are SHEET tokens — the page repeats them
    only in its 404 fallback block, and every rule that paints with them
    reads them through ``var()``.
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
    # The envelope vocabulary: it registers as its own origin, handles
    # the focus, and drives the other panes.
    assert "'phys'" in body
    # …politely: the first hello asks for the slot, it does not seize it.
    assert "takeover: true" not in body
    assert "phys_focus" in body
    assert "graph_focus" in body
    assert "selection_changed" in body
    # The missing-half banner and the empty state both name the
    # producing commands — either one alone fills a half.
    assert "rb synth" in body and "rb power" in body
    assert theme.MASCOT_240 in body


def test_the_page_is_the_phy_route_and_the_phys_origin():
    """A page rename does not touch the wire: the route and the label are
    ``phy``, the ``hello`` client is ``phys``."""

    assert phys_page.PHYS_PAGE_ROUTE == "/phy"
    assert phys_page.PHYS_JSON_ROUTE == "/phy.json"
    js = _page_js()
    assert "client: 'phys', version: '1.0.0', capabilities: ['phys_focus']" in js
    assert "origin: 'phys', kind: 'request', type: 'hello'," in js
    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "<title>rtl-buddy-phy</title>" in body


# ---------------------------------------------------------------------------
# the pure helpers, sliced out and run in node
# ---------------------------------------------------------------------------


def _page_js() -> str:
    """The page's inline script — the last ``<script>`` in the body."""

    body = phys_page.render_phys_html(hub_addr="127.0.0.1:1").decode("utf-8")
    return body.split("<script>")[-1].split("</script>")[0]


def _marked_js(marker: str) -> str:
    """One block of pure helpers, sliced out of the page by its markers.

    Nothing between the markers may touch the DOM or close over page
    state, which is exactly what evaluating them in bare ``node``
    enforces.
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
    """The whole inline script, not just the sliced helpers: a syntax
    error anywhere in it is a blank pane with a console message nobody
    is looking at."""

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
    """internal + switching. The model has no ``dynamic`` column — the
    producers report the two halves and the total — and this is the one
    place the pane invents it."""

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
    """A column no row measured stays null. Yosys writes no ``area``
    without a Liberty, and "this design is free" is the wrong reading of
    that."""

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
    """``rb phys``'s own rule (``_sort_key_desc``): a row whose metric was
    never measured is not a zero, so it sorts below every measured row
    ascending as well as descending — otherwise "smallest first" would
    open on the rows nobody measured."""

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


def test_equal_rows_keep_a_stable_order():
    """A re-sort of equal rows must not jitter between renders, so ties
    break on the name and only then on the payload's own order."""

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
    """OpenSTA prints ``/`` and every RTL-side consumer spells the same
    path with ``.``; ``rb phys`` admits both. The pane has to pick one
    on the way OUT, and the wire's vocabulary is dots."""

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


def test_the_design_top_is_added_on_the_way_out_and_ignored_on_the_way_in():
    """A schematic `instance_path` is rooted at the design top; an
    OpenSTA row is not. Neither producer is wrong, so the pane roots what
    it sends and un-roots what it compares — and never rewrites a row."""

    out = _node(
        _marked_js("path-normalise")
        + """
        console.log(JSON.stringify([
          withTop('u_sub/u_leaf', 'blk'),
          withTop('blk.u_sub.u_leaf', 'blk'),   // already rooted: unchanged
          withTop('blk', 'blk'),                // the top itself
          withTop('u_sub/u_leaf', ''),          // no top known: plain levelling
          stripTop('blk.u_sub.u_leaf', 'blk'),
          stripTop('u_sub/u_leaf', 'blk'),
          stripTop('blkish.u_sub', 'blk'),      // a prefix is not a level
          samePath('u_sub/u_leaf', 'blk.u_sub.u_leaf', 'blk'),
          samePath('blk/u_sub', 'u_sub', 'blk'),
          samePath('u_sub', 'u_other', 'blk')
        ]));
        """
    )
    assert json.loads(out) == [
        "blk.u_sub.u_leaf",
        "blk.u_sub.u_leaf",
        "blk",
        "u_sub.u_leaf",
        "u_sub.u_leaf",
        "u_sub.u_leaf",
        "blkish.u_sub",
        True,
        True,
        False,
    ]


def test_the_module_column_sorts_by_name_rather_than_by_null():
    """`module` is a string column of the instance table. Routing it
    through the numeric reader made every value null, so clicking the
    header did nothing."""

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
    """An RTL module clicked on a mapped hierarchical design matches no
    leaf, because the leaves carry Liberty cell names. A bare "no
    instances match" would read as "this block burns no power"."""

    js = _page_js()
    assert "function joinMissNote()" in js
    # The empty branch consults it before falling back to the plain state.
    assert "joinMissNote() ||" in js
    assert "elem('p', 'muted', 'no instances match.')" in js
    # What it says, and the three cases it declines to say it in.
    assert "Liberty cell names, not RTL module names" in js
    assert "hierarchy join" in js
    assert "if (state.module === null || state.filter) { return null; }" in js
    assert "if (matched) { return null; }" in js


def test_an_early_selection_is_held_until_the_model_arrives():
    """The hub replays the cached selection right after `welcome`, which
    routinely beats the `/phy.json` fetch. `phys_focus` was already held
    for that race; a `selection_changed` was dropped."""

    js = _page_js()
    assert "pendingSelection: null" in js
    assert "state.pendingSelection = ip;" in js
    # Applied at ingest, and an explicit focus outranks a passive one.
    assert "var focus = state.pending, selection = state.pendingSelection;" in js
    assert "} else if (selection) {" in js
    assert "focusInstanceFromWire(selection);" in js


def test_the_first_hello_is_polite():
    """The common case is no other phys tab open, and a polite hello wins
    that outright. Asking for a takeover unconditionally is what turned a
    second cov tab into an eviction war."""

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
    """Same lockstep cases the graph, cov and landing copies pin — now
    five copies of one rule."""

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


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


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
async def test_http_index_advertises_the_phy_url(hub_and_viewer):
    """The SPA pre-landed its ``/phy`` app-switcher entry gated on this
    global, so the hub setting it is what makes the entry appear."""

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

        # The page itself is still 200 — its empty state is the better
        # place to say "run a synthesis" than a blank browser tab.
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


# ---------------------------------------------------------------------------
# phys_focus — the wire type
# ---------------------------------------------------------------------------


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
        # `switching` is a real column of the model and deliberately NOT
        # a focus metric: the enum offers `dynamic`, its sum with
        # `internal`.
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
    """The pane must not share ``view``, ``graph`` or ``cov``.

    One client per origin, and the point of the pane is to drive the
    others — clicking the module that owns the area selects it in the
    schematic — so a shared slot would evict whichever tab was looked at
    second.
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
    """``additionalProperties: false`` with no nullable hint: an unset
    metric has to be absent on the wire, not null."""

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
    """``rb hub send phys-focus`` before the tab is open still lands —
    metric and all, or a replay would silently downgrade "this module, on
    leakage" to "this module"."""

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
    """One slot, no backlog: a late-joining pane opens on the most recent
    target rather than replaying every focus it missed."""

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
            # …and nothing else queued behind it. `peer_joined` for the
            # driver is the only other traffic this pane can see.
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


# ---------------------------------------------------------------------------
# display names vs wire origins
#
# See the same section in ``tests/test_hub_cov_page.py``: the apps were
# renamed, the ``Origin`` enum was not, and the origin→label map is the
# seam between the two vocabularies. Each pane carries its own copy, so
# each pane is tested for it.
# ---------------------------------------------------------------------------


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
