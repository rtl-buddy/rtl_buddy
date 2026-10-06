"""Tests for the hub-served coverage pane, modelled on ``test_hub_graph_page.py``.

Covers ``/cov.json`` (built by the same builders as ``rb cov summary``),
``/cov/source`` (contained under the project root), the self-contained ``/cov`` page,
presence and advertisement, the ``cov_focus`` wire type and the status-strip version
label. The page is static HTML plus one inline script, so tests assert its structure and
run pure helpers sliced out of it in ``node``.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.cov import manifest as manifest_mod
from rtl_buddy.cov import model as model_mod
from rtl_buddy.cov import query as cov_query
from rtl_buddy.hub import cov_page, theme
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
from rtl_buddy.hub.state import CovFocus
from rtl_buddy.hub.viewer_http import ViewerServer, render_index_html


# fixtures: one run's coverage artefacts on disk

_SOURCE = """module blk (input clk, input a, output reg q);
  always @(posedge clk) begin
    if (a) q <= 1'b1;
    else   q <= 1'b0;
  end
  cover property (@(posedge clk) a) BLK_WRITE;
endmodule
"""


def _totals(found: int, hit: int) -> dict:
    return {"found": found, "hit": hit, "ratio": None if not found else hit / found}


def _empty_totals() -> dict:
    return {
        "line": _totals(0, 0),
        "branch": _totals(0, 0),
        "toggle": _totals(0, 0),
        "expression": _totals(0, 0),
        "cover": _totals(0, 0),
    }


def _model() -> dict:
    file_totals = _empty_totals()
    file_totals["line"] = _totals(3, 2)
    file_totals["branch"] = _totals(2, 1)
    file_totals["toggle"] = _totals(1, 0)
    file_totals["cover"] = _totals(1, 1)
    basic = _empty_totals()
    basic["line"] = _totals(3, 2)
    extra = _empty_totals()
    extra["line"] = _totals(3, 1)
    return {
        "schema_version": model_mod.MODEL_SCHEMA_VERSION,
        "generator": "rtl-buddy 0.0.0+test",
        "simulator": "verilator",
        "totals": dict(file_totals),
        "counts": {"files": 1, "tests": 2, "modules": 1},
        "modules": {"blk": ["design/blk.sv"]},
        "tests": [
            {"name": "basic", "suite": "verif/blk/tests.yaml", "totals": basic},
            {"name": "extra", "suite": "verif/blk/tests.yaml", "totals": extra},
        ],
        "files": [
            {
                "path": "design/blk.sv",
                "modules": ["blk"],
                "totals": file_totals,
                "line": [
                    {"line": 2, "hits": 7, "tests": {"basic": 7, "extra": 0}},
                    {"line": 3, "hits": 4, "tests": {"basic": 4, "extra": 0}},
                    {"line": 4, "hits": 0, "tests": {"basic": 0, "extra": 0}},
                ],
                "branch": [
                    {
                        "line": 3,
                        "column": 5,
                        "name": "if",
                        "module": "blk",
                        "hits": 4,
                        "tests": {"basic": 4},
                    },
                    {
                        "line": 4,
                        "column": 5,
                        "name": "else",
                        "module": "blk",
                        "hits": 0,
                        "tests": {"basic": 0},
                    },
                ],
                "toggle": [
                    {
                        "line": 1,
                        "column": 9,
                        "name": "q[0]",
                        "module": "blk",
                        "hits": 0,
                        "tests": {"basic": 0},
                    }
                ],
                "expression": [],
                "cover": [
                    {
                        "line": 6,
                        "column": 3,
                        "name": "BLK_WRITE",
                        "module": "blk",
                        "hits": 3,
                        "tests": {"basic": 3},
                    }
                ],
            }
        ],
    }


@pytest.fixture
def covered_project(tmp_path: Path) -> Path:
    """A project root with one run's ``cov_dir`` (manifest + model)."""

    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "blk.sv").write_text(_SOURCE, encoding="utf-8")
    cov_dir = tmp_path / "verif" / "blk" / "cov_dir"
    cov_dir.mkdir(parents=True)
    model_path = model_mod.write_model(_model(), cov_dir)
    manifest = manifest_mod.build_manifest(
        project_root=tmp_path,
        cov_dir=cov_dir,
        command="regression",
        suite=tmp_path / "verif" / "blk" / "regression.yaml",
        builder="verilator",
        simulator_family="verilator",
        merge_mode="raw",
        model_path=model_path,
        totals=_model()["totals"],
        merged={"info": cov_dir / "coverage_merged.info"},
        tests=[{"name": "basic", "raw": cov_dir / "basic.dat"}],
    )
    manifest_mod.write_manifest(manifest, cov_dir)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_presence_cache():
    """Each test starts with empty module caches.

    :func:`cov_page.cov_data_present` memoises for a few seconds and
    :func:`cov_page.model_file_set` memoises per model path; both are cleared so no
    test inherits another's answer.
    """

    cov_page._presence_cache.clear()
    cov_page._file_set_cache.clear()
    yield
    cov_page._presence_cache.clear()
    cov_page._file_set_cache.clear()


# build_cov_payload


def test_payload_is_the_cli_builder_plus_a_hub_block(covered_project: Path):
    """The payload is the query builder's output plus a hub block, so numbers agree with
    ``rb cov summary``."""

    payload = cov_page.build_cov_payload(covered_project)
    ctx = cov_query.load_context(covered_project)
    expected = cov_query.detail_payload(ctx)
    hub = payload.pop("hub")
    assert payload == expected
    assert hub["schema_version"] == cov_page.PAGE_SCHEMA_VERSION
    assert hub["metrics"] == ["line", "branch", "toggle", "expression", "cover"]
    assert hub["source_route"] == cov_page.COV_SOURCE_ROUTE
    assert hub["model"] == "verif/blk/cov_dir/coverage-model.json"


def test_payload_carries_points_and_their_attribution(covered_project: Path):
    """The payload carries points and their attribution, so the client needs no request
    per file."""

    payload = cov_page.build_cov_payload(covered_project)
    (row,) = payload["files"]
    assert [p["line"] for p in row["line"]] == [2, 3, 4]
    assert row["line"][0]["tests"] == {"basic": 7, "extra": 0}
    # The detail the LCOV export erases and only the raw database keeps.
    assert row["toggle"][0]["name"] == "q[0]"
    assert [p["name"] for p in row["branch"]] == ["if", "else"]
    assert payload["covers"][0]["name"] == "BLK_WRITE"
    assert payload["artefacts"]["model"] == "verif/blk/cov_dir/coverage-model.json"
    assert [t["name"] for t in payload["tests"]] == ["basic", "extra"]


def test_payload_bytes_404_names_the_command_that_makes_data(tmp_path: Path):
    status, body = cov_page.cov_payload_bytes(tmp_path)
    assert status == 404
    error = json.loads(body)["error"]
    assert "cov_dir" in error and "coverage" in error


def test_presence_follows_discovered_artefacts(tmp_path: Path, covered_project: Path):
    assert cov_page.cov_data_present(covered_project) is True
    assert cov_page.cov_data_present(None) is False
    empty = tmp_path / "no-coverage-here"
    empty.mkdir()
    assert cov_page.cov_data_present(empty) is False


def test_presence_is_cached_for_the_ttl(covered_project: Path, monkeypatch):
    """Presence is cached for the TTL."""

    calls = []
    real = manifest_mod.discover_manifests

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(cov_page.manifest_mod, "discover_manifests", counted)
    assert cov_page.cov_data_present(covered_project) is True
    assert cov_page.cov_data_present(covered_project) is True
    assert len(calls) == 1
    # ttl=0 means "ask again now".
    assert cov_page.cov_data_present(covered_project, ttl=0) is True
    assert len(calls) == 2


# read_source_lines: containment rule


def test_source_lines_are_returned_one_per_line(covered_project: Path):
    status, body = cov_page.read_source_lines(covered_project, "design/blk.sv")
    assert status == 200
    payload = json.loads(body)
    assert payload["path"] == "design/blk.sv"
    # Line N at index N-1, the shape the gutter renders against.
    assert payload["lines"][0].startswith("module blk")
    assert payload["lines"][5].strip().startswith("cover property")


@pytest.mark.parametrize(
    "requested",
    [
        "../outside.sv",
        "design/../../outside.sv",
        "/etc/hosts",
        # Absolute and lexically prefixed by the root: a string comparison would let
        # this escape unless the path is resolved first.
        "{root}/../outside.sv",
        "{root}/design/../../outside.sv",
    ],
)
def test_source_refuses_paths_outside_the_project(
    covered_project: Path, requested: str
):
    """Source refuses paths outside the project."""

    (covered_project.parent / "outside.sv").write_text("secret\n", encoding="utf-8")
    requested = requested.format(root=covered_project)
    status, body = cov_page.read_source_lines(covered_project, requested)
    assert status == 403
    assert "outside the project root" in json.loads(body)["error"]
    assert b"secret" not in body


def test_source_accepts_an_absolute_path_inside_the_project(covered_project: Path):
    """Source accepts an absolute path inside the project."""

    requested = str(covered_project / "design/blk.sv")
    status, body = cov_page.read_source_lines(covered_project, requested)
    assert status == 200
    assert json.loads(body)["lines"][0].startswith("module blk")


def test_source_serves_only_what_the_model_names(covered_project: Path):
    """Source serves only files the model names, not everything under the root.

    A readable in-root file the model does not name is refused with the same status
    as one outside the root.
    """

    (covered_project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    status, body = cov_page.read_source_lines(covered_project, ".env")
    assert status == 403
    assert "not in this run's coverage model" in json.loads(body)["error"]
    assert b"secret" not in body

    # A file the model names is served, absolute request or not.
    for requested in ("design/blk.sv", str(covered_project / "design/blk.sv")):
        status, body = cov_page.read_source_lines(covered_project, requested)
        assert status == 200
        assert json.loads(body)["lines"][0].startswith("module blk")


def test_source_grant_follows_the_model_on_disk(covered_project: Path):
    """A rerun that widens the model widens the grant without a restart.

    The set is memoised on the model file's ``(mtime, size)``.
    """

    (covered_project / "design" / "other.sv").write_text(
        "module other; endmodule\n", encoding="utf-8"
    )
    assert cov_page.read_source_lines(covered_project, "design/other.sv")[0] == 403

    model = _model()
    extra = dict(model["files"][0])
    extra["path"] = "design/other.sv"
    model["files"].append(extra)
    model_mod.write_model(model, covered_project / "verif" / "blk" / "cov_dir")

    status, body = cov_page.read_source_lines(covered_project, "design/other.sv")
    assert status == 200
    assert json.loads(body)["lines"][0].startswith("module other")


def test_source_missing_and_empty_requests(covered_project: Path):
    status, body = cov_page.read_source_lines(covered_project, "")
    assert status == 400 and "?path=" in json.loads(body)["error"]
    # Named by the model but gone from disk: 404. A path the model never named is a 403
    # whether or not it exists, so the route is not an existence oracle.
    (covered_project / "design" / "blk.sv").unlink()
    status, body = cov_page.read_source_lines(covered_project, "design/blk.sv")
    assert status == 404 and "design/blk.sv" in json.loads(body)["error"]


def test_source_refuses_a_file_over_the_annotation_limit(
    covered_project: Path, monkeypatch
):
    monkeypatch.setattr(cov_page, "MAX_SOURCE_BYTES", 8)
    status, body = cov_page.read_source_lines(covered_project, "design/blk.sv")
    assert status == 413
    assert "annotation limit" in json.loads(body)["error"]


# render_cov_html: the offline rule


def test_page_injects_hub_address():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_COV_URL__ = '/cov.json'" in body
    assert "window.__RTL_BUDDY_COV_SOURCE_URL__ = '/cov/source'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """The page is self-contained: every ``src``/``href`` that is not a page anchor is a
    same-origin absolute path."""

    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
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
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<link rel="stylesheet" href="/hub/theme.css">' in body
    assert theme.FAVICON_16 in body and theme.FAVICON_32 in body
    for token in ("--bg:", "--panel:", "--fg:", "--accent:", "--cov-l:", "--cov-none:"):
        assert token in body, token
    # Light default, with the fallback before the link or it out-ranks the sheet at
    # equal specificity and breaks dark mode.
    assert "--bg:          #f8fafc;" in body
    assert body.index("--bg:          #f8fafc;") < body.index('href="/hub/theme.css"')


def test_page_carries_the_pieces_the_issue_asks_for():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # Every metric, the shared ramp and the hub chrome vocabulary.
    for metric in ("line", "branch", "toggle", "expression", "cover"):
        assert f"'{metric}'" in body, metric
    assert "hsl(var(--h), var(--tint-s), var(--cov-l))" in body
    for word in ("connected", "connecting…", "offline"):
        assert word in body, word
    # The envelope vocabulary: registers as its own origin, handles focus, drives other
    # panes.
    assert "'cov'" in body
    # The first hello asks for the slot politely; see the registration section below.
    assert "takeover: true" not in body
    assert "cov_focus" in body
    assert "source_focused" in body
    assert "open_source" in body
    assert "graph_focus" in body
    assert "selection_changed" in body
    # The empty state names a command that produces coverage and carries the pane's one
    # piece of artwork.
    assert "rb regression --coverage-merge" in body
    assert theme.MASCOT_240 in body


# the per-elaboration / source-point figures toggle (#651)


def _figures_js() -> str:
    return _marked_js("figures")


def test_the_figures_picker_offers_both_readings():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    assert '<select id="by">' in body
    assert '<option value="elab">per elaboration</option>' in body
    assert '<option value="source">source points</option>' in body
    assert "/cov?by=source" in body
    assert (
        "els.by.addEventListener('change', function () { setBy(els.by.value); });" in js
    )
    # A model with no source figures greys the choice out instead of hiding it.
    assert "source.textContent = has ? 'source points' : 'source points (none)';" in js


def test_figures_pick_source_totals_at_every_scope():
    """The run, test and file rows read their source_totals under the toggle; a row
    without them keeps its totals."""

    out = _node(
        _figures_js()
        + """
        var row = {
          totals: { line: { found: 4, hit: 2, ratio: 0.5 } },
          source_totals: { line: { found: 2, hit: 2, ratio: 1 } }
        };
        console.log(JSON.stringify(figuresOf(row, 'line', FIGURES_ELAB)));
        console.log(JSON.stringify(figuresOf(row, 'line', FIGURES_SOURCE)));
        console.log(JSON.stringify(figuresOf({ totals: row.totals }, 'line', FIGURES_SOURCE)));
        console.log(JSON.stringify(figuresOf(row, 'toggle', FIGURES_SOURCE)));
        console.log(JSON.stringify(figuresOf(null, 'line', FIGURES_SOURCE)));
        """
    )
    elab, source, older, absent, nothing = out.strip().splitlines()
    assert json.loads(elab) == {"found": 4, "hit": 2, "ratio": 0.5}
    assert json.loads(source) == {"found": 2, "hit": 2, "ratio": 1}
    assert json.loads(older) == {"found": 4, "hit": 2, "ratio": 0.5}
    empty = {"found": 0, "hit": 0, "ratio": None}
    assert json.loads(absent) == empty
    assert json.loads(nothing) == empty


def test_the_by_param_opens_and_follows_the_toggle():
    out = _node(
        _figures_js()
        + """
        console.log(JSON.stringify([
          openedBy('?by=source'), openedBy('?x=1&by=source'), openedBy('?by=elab'),
          openedBy('?by=sources'), openedBy(''), openedBy(null)
        ]));
        console.log(JSON.stringify([
          searchWithBy('', FIGURES_SOURCE), searchWithBy('?x=1', FIGURES_SOURCE),
          searchWithBy('?by=source&x=1', FIGURES_ELAB), searchWithBy('?by=source', FIGURES_ELAB),
          searchWithBy('?by=source', FIGURES_SOURCE), searchWithBy('?byline=2', FIGURES_ELAB)
        ]));
        """
    )
    opened, searches = out.strip().splitlines()
    assert json.loads(opened) == ["source", "source", "elab", "elab", "elab", "elab"]
    assert json.loads(searches) == [
        "?by=source",
        "?x=1&by=source",
        "?x=1",
        "",
        "?by=source",
        "?byline=2",
    ]


def test_source_count_collapses_elaborations_under_a_hit_function():
    """The test-lens recount of a file's source points keys like source_point_key."""

    out = _node(
        _figures_js()
        + """
        var points = [
          { line: 5, column: 3, name: 'if', module: 'blk__W13', hits: 0, tests: { a: 0 } },
          { line: 5, column: 3, name: 'if', module: 'blk__Wc', hits: 2, tests: { a: 2 } },
          { line: 5, column: 4, name: 'else', module: 'blk__W13', hits: 1, tests: { a: 0 } },
          { line: 7, column: 3, name: 'block', module: 'blk__Wc', hits: 0, tests: {} }
        ];
        console.log(JSON.stringify(sourceCount(points, function (p) { return p.hits; })));
        console.log(JSON.stringify(sourceCount(points, function (p) {
          return (p.tests || {}).a || 0;
        })));
        console.log(JSON.stringify(sourceCount(null, function () { return 1; })));
        """
    )
    merged, lensed, empty = out.strip().splitlines()
    assert json.loads(merged) == {"found": 3, "hit": 2, "ratio": 2 / 3}
    assert json.loads(lensed) == {"found": 3, "hit": 1, "ratio": 1 / 3}
    assert json.loads(empty) == {"found": 0, "hit": 0, "ratio": None}


def test_the_toggle_swaps_cells_but_not_the_order():
    """Same files, same order as `rb cov summary --by-source`: ranking reads `totals`,
    the cells read the chosen figures."""

    js = _page_js()
    ordering = _file_ordering_js()
    assert "row.totals[metric]" in ordering
    assert "source_totals" not in ordering
    render_files = js[js.index("function renderFiles()") : js.index("function setLens")]
    assert "var t = shownTotals(row, state.metric);" in render_files
    render_tests = js[
        js.index("function renderTests()") : js.index("function renderDashboard()")
    ]
    assert render_tests.count("shownTotals(") == 2
    assert "var t = shownTotals(p, metric);" in js
    assert "var t = figuresOf(p, metric, by);" in js
    # The file view's header follows too, and recounts source points under a test lens.
    assert "return sourceCount(row[metric], hitsFor);" in js
    # Toggling re-renders every table and records the pick in the address.
    set_by = js[js.index("function setBy(by)") : js.index("function initialMetric")]
    for call in (
        "renderHeader();",
        "renderFiles();",
        "renderTests();",
        "renderDashboard();",
        "history.replaceState(",
    ):
        assert call in set_by, call


# the marks column, its badges and the bit grid


def _page_js() -> str:
    """The page's inline script: the last ``<script>`` in the body."""

    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    return body.split("<script>")[-1].split("</script>")[0]


def _marked_js(marker: str) -> str:
    """One block of pure helpers sliced out of the page by its markers.

    Nothing between the markers may touch the DOM or page state, which evaluating
    them in bare ``node`` enforces.
    """

    match = re.search(rf"// >>> {marker}\n(.*?)// <<< {marker}", _page_js(), re.S)
    assert match, f"the {marker} markers moved"
    return match.group(1)


def _toggle_grouping_js() -> str:
    return _marked_js("toggle-grouping")


def _file_ordering_js() -> str:
    return _marked_js("file-ordering")


def _module_names_js() -> str:
    return _marked_js("module-names")


def _elaboration_lens_js() -> str:
    """The lens helpers build on ``baseModuleName``, so they are sliced separately."""

    return _marked_js("module-names") + _marked_js("elaboration-lens")


def _node(script: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not installed")
    done = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


# hub registration: polite hello, one takeover retry, and the way back
# One client per origin: the hub refuses a hello for an occupied slot unless it sets
# `takeover`.
# The flow mirrors the schematic SPA's (`useHub.js`); test_hub_graph_page.py asserts it
# in the same words.


def test_the_first_hello_is_polite():
    """The first hello is polite (no takeover), so two tabs do not evict each other."""

    out = _node(
        _marked_js("hello-payload")
        + """
        console.log(JSON.stringify(
          [helloPayload(false), helloPayload(true), helloPayload(undefined)]));
        """
    )
    polite, takeover, unset = json.loads(out)
    assert polite == {
        "client": "cov",
        "version": "1.0.0",
        "capabilities": ["cov_focus"],
    }
    # Omitted, not `false`.
    assert "takeover" not in polite
    assert unset == polite
    assert takeover["takeover"] is True

    js = _page_js()
    # Every hello comes from one helper; there is no `takeover: true` literal.
    assert "payload: helloPayload(pendingTakeover)" in js
    assert "ws.addEventListener('open', sendHello);" in js
    assert "var pendingTakeover = false;" in js
    assert "takeover: true" not in js


def test_an_occupied_slot_is_retried_once_with_takeover():
    """A refused hello is retried exactly once with takeover."""

    js = _page_js()
    handler = js.split("function handleHubError(payload) {")[1].split("\n  }")[0]
    assert "payload.code === 'not_connected' && !pendingTakeover &&" in handler
    assert "/already registered/i.test(payload.message || '')" in handler
    assert "pendingTakeover = true;" in handler
    assert "sendHello();" in handler
    # Cleared on welcome, so a later reconnect starts polite again.
    welcome = js.split("case 'welcome':")[1].split("break;")[0]
    assert "pendingTakeover = false;" in welcome
    # A handled registration error stays out of the message area.
    assert (
        "if (env.kind === 'error' && env.payload && !handleHubError(env.payload)) {"
        in js
    )


def test_superseded_stops_reconnecting_and_offers_the_slot_back():
    """Losing the slot to a newer tab stops reconnecting and offers the slot back in the
    status strip."""

    js = _page_js()
    handler = js.split("function handleHubError(payload) {")[1].split("\n  }")[0]
    assert "payload.code === 'superseded'" in handler
    assert "superseded = true;" in handler
    assert "showSuperseded();" in handler
    assert "var superseded = false;" in js
    # Disarmed at the timer and at the close that follows the eviction.
    sched = js.split("function scheduleReconnect() {")[1].split("\n  }")[0]
    assert "if (superseded) { return; }" in sched
    close = js.split("ws.addEventListener('close', function () {")[1].split(
        "\n    });"
    )[0]
    assert close.index("if (superseded) { return; }") < close.index(
        "scheduleReconnect();"
    )
    # A distinct strip state: offline dot, its own wording, clickable.
    show = js.split("function showSuperseded() {")[1].split("\n  }")[0]
    assert "els.wsDot.className = 'dot offline';" in show
    assert "els.wsStatus.textContent = SUPERSEDED_TEXT;" in show
    assert "els.wsStatus.className = 'take-back';" in show
    assert "els.wsStatus.setAttribute('role', 'button');" in show
    assert "another coverage tab took this connection — click to take back" in js
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert ".take-back {" in body
    assert "cursor: pointer;" in body.split(".take-back {")[1].split("}")[0]


def test_taking_the_slot_back_hellos_with_takeover():
    """Taking the slot back hellos with takeover."""

    js = _page_js()
    back = js.split("function takeBack() {")[1].split("\n  }")[0]
    assert "if (!superseded) { return; }" in back
    assert "superseded = false;" in back
    assert "pendingTakeover = true;" in back
    # Backoff starts over.
    assert "retryMs = 500;" in back
    assert "connect();" in back
    # The status word is the control while superseded; `takeBack` no-ops otherwise, so
    # the listener is bound once.
    assert "els.wsStatus.addEventListener('click', takeBack);" in js


def test_an_ordinary_drop_still_reconnects():
    """An ordinary drop still reconnects."""

    js = _page_js()
    close = js.split("ws.addEventListener('close', function () {")[1].split(
        "\n    });"
    )[0]
    assert "els.wsDot.className = 'dot offline';" in close
    assert "els.wsStatus.textContent = 'offline';" in close
    assert "els.wsStatus.title = 'lost /ws — retrying';" in close
    assert "scheduleReconnect();" in close
    sched = js.split("function scheduleReconnect() {")[1].split("\n  }")[0]
    assert "setTimeout(connect, retryMs);" in sched
    assert "retryMs = Math.min(retryMs * 2, 10000);" in sched


def test_page_javascript_parses(tmp_path: Path):
    """The page JavaScript parses."""

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not installed")
    script = tmp_path / "cov_page.js"
    script.write_text(_page_js(), encoding="utf-8")
    done = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


def test_every_metric_gets_a_column():
    """Every metric (L B T E C) gets a column, in the run's metric order."""

    js = _page_js()
    assert "METRICS.forEach(function (metric) {" in js
    assert "cell.dataset.metric = metric;" in js
    assert "function renderCell(box, lineNo, metric)" in js
    assert "function entriesOn(lineNo, metric)" in js
    # The `L` column is the hit-count gutter with a header.
    assert "if (metric === 'line') {" in js
    # Tinted by the share of the line's points hit (#678).
    assert "tint(cell, point.hit / point.points.length);" in js
    assert (
        "var byLine = foldLinePoints(pointsOfElaboration(row.line, state.elab), hitsFor);"
        in js
    )
    assert "bindCellClick(cell, n, 'line');" in js
    # An empty cell stays empty.
    assert "if (!list.length) { return; }" in js
    # The source view does not read the picker.
    assert (
        "state.metric"
        not in js[js.index("function renderCell") : js.index("function bitCell")]
    )


def test_the_source_table_heads_each_column_with_the_file_totals():
    """The source table heads each column with the file totals."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<thead id="src-head">' in body
    assert "function renderSrcHead(row)" in js
    assert "METRIC_INITIAL[metric] + ' ' +" in js
    assert "(t.found ? Math.round(t.ratio * 100) + '%' : '—')" in js
    assert "elem('div', 'mh-num', t.hit + '/' + t.found)" in js
    # Without a lens the header is the model's numbers; with one it is recounted through
    # the lens.
    assert "function fileTotals(row, metric)" in js
    assert "if (!state.test && !elab) { return t; }" in js
    assert "if (!elab && !t.found) { return t; }" in js
    assert "var t = fileTotals(row, metric);" in js
    # Sticky; the shared `table th` rule already sticks, this block only sizes it.
    assert "position: sticky; top: 0;" in body
    assert "table#src thead th {" in body
    # Clicking a header sets the ranking metric.
    assert "th.addEventListener('click', function () { setMetric(metric); });" in js
    assert "function setMetric(metric)" in js
    assert "table#src thead th.mh:hover, table#src thead th.mh.sort" in body
    assert "els.fileHead.appendChild(pill);" in js  # module pills stay
    assert "METRIC_INITIAL[metric] + ' ' + t.hit + '/' + t.found + ' ' + pct" not in js


def test_the_annotation_column_collapses_to_one_badge():
    """The annotation column collapses many points on a line to one badge."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # Toggle always collapses; another metric only when there is more than one of it.
    assert "function collapses(metric, list)" in js
    assert "return metric === 'toggle' || list.length > 1;" in js
    assert "function metricBadge(metric, list, lineNo)" in js
    # The metric is the column, so the badge is the fraction alone and the chip the name
    # alone.
    assert "hit + '/' + list.length);" in js
    assert "function namedMark(entry)" in js
    assert "entry.point.name || '(unnamed)');" in js
    # The cap is on an inner block, not the cell: max-width on a `td` is advisory in
    # auto table layout.
    assert "--markcol:  8rem;" in body
    assert "table#src td.marks .marks-in {" in body
    assert "max-width: var(--markcol); overflow: hidden;" in body


def test_the_pane_opens_on_toggle():
    """The pane opens on toggle."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "var DEFAULT_METRIC = 'toggle';" in js
    assert "function initialMetric(payload)" in js
    # An .info-only run has no toggles; fall through to the first metric with points.
    assert "return METRICS.indexOf(metric) >= 0 && (totals[metric] || {}).found;" in js
    # A reload keeps the metric the user picked.
    assert "if (!state.metric || METRICS.indexOf(state.metric) < 0) {" in js
    assert "metric: null," in js
    # The dropdown says it drives the file list.
    assert "Which metric the FILE LIST is ranked and barred on" in body
    assert "The source view itself always shows all five." in body


def test_a_focus_item_needs_no_metric_hint():
    """A focus item needs no metric hint; the point's name says which column it is in."""

    js = _page_js()
    assert "function focusedCell()" in js
    assert "found = { line: parseInt(key, 10), metric: entry.metric };" in js
    # A lone named point is its own chip and carries the selection; no panel behind it.
    assert (
        "if (found && !collapses(found.metric, entriesOn(found.line, found.metric))) {"
        in js
    )
    assert "openDetail(open, metric, { scroll: target == null });" in js
    # The picker is neither consulted nor moved.
    assert "metricOfItem" not in js


def test_detail_panel_is_docked_outside_the_code_scroller():
    """The detail panel is a sibling of the code area, outside the code scroller."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "expandedLine: null," in js
    assert "function toggleDetail(lineNo, metric)" in js
    assert (
        "if (state.expandedLine === lineNo && state.expandedMetric === metric) {" in js
    )
    assert "function closeDetail()" in js
    # The file view scrolls its code area and the panel follows it, never inside `#src-
    # scroll`.
    scroller = body.index('<div id="src-scroll">')
    scroller_end = body.index("</div>", body.index("</table>"))
    panel = body.index('<div id="detail" hidden>')
    assert scroller < scroller_end < panel
    assert "#src-scroll { flex: 1 1 auto; overflow: auto; min-height: 0; }" in body
    assert "#file { flex: 1 1 auto; display: flex; flex-direction: column;" in body
    # An id selector setting `display` outranks the UA sheet's [hidden].
    assert "[hidden] { display: none !important; }" in body
    # Capped with its own scroll so a many-signal line does not eat the code view.
    assert "max-height: 40vh;" in body
    assert "#detail-scroll { flex: 1 1 auto; overflow: auto;" in body
    # The line stays marked while the panel is open, without painting over the hit tint.
    assert "host.classList.add('open');" in js
    assert "table#src tr.open td.code { background: var(--panel-2); }" in body
    assert (
        "table#src tr.open td.no { box-shadow: inset 2px 0 0 var(--accent); }" in body
    )


def test_the_hit_count_column_opens_the_same_panel():
    """The hit-count column opens the same panel as the badges."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "function openLineDetail(lineNo, opts)" in js
    # One slot, five columns: "click again to close" needs the (line, metric) pair.
    assert "expandedMetric: null," in js
    assert "if (metric === 'line') { openLineDetail(lineNo, {}); }" in js
    assert "bindDetailToRow(host, lineNo, 'line', opts);" in js
    assert "showPoint('line', point);" in js
    # The cell is clickable; its click is not the row's, so inspecting attribution does
    # not drive the editor and schematic.
    assert "table#src td.hits.act { cursor: pointer; }" in body
    assert "cell.classList.add('act');" in js
    assert "function bindCellClick(node, lineNo, metric)" in js
    handler = js[
        js.index("function bindCellClick") : js.index("function bindDetailToRow")
    ]
    assert "ev.stopPropagation();" in handler
    assert "toggleDetail(lineNo, metric);" in handler


def test_the_tests_table_pins_a_merged_row():
    """The tests table pins a merged row, with a way back to all tests."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "'all tests (merged)'" in js
    assert "elem('tr', 'row all-tests')" in js
    assert "all.classList.toggle('sel', !state.test);" in js
    assert (
        "tr.row.all-tests td { border-bottom: 1px solid var(--line-strong); }" in body
    )
    # Every route into the lens goes through one setter, which no-ops when unchanged.
    assert "function setLens(name)" in js
    assert "if (state.test === name) { return; }" in js
    assert "all.addEventListener('click', function () { setLens(null); });" in js
    # Clicking the selected test again still works...
    assert "setLens(state.test === row.name ? null : row.name);" in js
    # ...and the lens pill is a way out wherever it is shown.
    assert "function lensPill()" in js
    assert "'lens: ' + state.test + ' ×'" in js
    assert "els.fileHead.appendChild(lensPill());" in js
    assert "els.detailHead.appendChild(lensPill());" in js
    assert ".pill.act { cursor: pointer; }" in body


def test_point_attribution_docks_in_the_same_panel():
    """Point attribution docks in the same panel, under the grid clicked."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    detail = body.index('<div id="detail" hidden>')
    scroll = body.index('<div id="detail-scroll">')
    point = body.index('<div id="point" hidden></div>')
    assert detail < scroll < point
    assert "els.detail.hidden = false;" in js
    # Clicking another cell moves the selection.
    assert "function selectDetail(node)" in js
    assert "selectDetail(node);" in js and "selectDetail(mark);" in js
    # A chip kept inline has no line panel behind it, so the panel must still be
    # closable.
    assert "if (!els.detailHead.firstChild) {" in js
    assert "function appendDetailClose()" in js


def test_bit_grid_encodes_both_directions_in_one_cell():
    """The bit grid encodes 0->1 (top half) and 1->0 (bottom half) with the page's ramp."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "--tgl-hit:  hsl(120, var(--tint-s), var(--cov-l));" in body
    assert "--tgl-miss: hsl(0,   var(--tint-s), var(--cov-l));" in body
    assert "--dir-up: var(--tgl-miss); --dir-down: var(--tgl-miss);" in body
    assert "background: linear-gradient(to bottom," in body
    assert ".bit.up { --dir-up: var(--tgl-hit); }" in body
    assert ".bit.down { --dir-down: var(--tgl-hit); }" in body
    # The focused cell reuses the page's selection treatment.
    assert ".bit.sel { outline: 2px solid var(--accent); outline-offset: 1px; }" in body
    # A cell is per-bit, tooltipped with both directions under the active lens, and
    # clickable to per-test attribution.
    assert "' — 0→1: '" in js
    assert "' · 1→0: '" in js
    assert "showPoint('toggle', pick)" in js
    # A per-signal summary.
    assert "covered + '/' + group.total + ' dirs'" in js


def test_focus_item_opens_the_line_and_selects_the_bit():
    """``cov_focus {item: "paddr[3]:0->1"}`` opens the line and selects the bit inside a
    collapsed badge."""

    js = _page_js()
    assert "function focusedCell()" in js
    assert "entry.point.name !== state.focusItem" in js
    assert "node.classList.add('sel');" in js
    assert "openDetail(open, metric, { scroll: target == null });" in js
    # A lens change re-renders the rows and re-opens the panel on the same column.
    assert "var reopen = state.expandedLine;" in js
    assert "var reopenMetric = state.expandedMetric;" in js
    assert "var open = cell ? cell.line : reopen;" in js
    assert "var metric = cell ? cell.metric : reopenMetric;" in js
    # A different file has different line numbers and elaborations.
    assert "if (state.file !== path) {" in js
    assert "state.expandedLine = null;\n      state.expandedMetric = null;" in js


def test_file_list_reranks_on_the_selected_metric():
    """The file list re-ranks on the selected metric."""

    js = _page_js()
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "function coldestFirst(rows, metric)" in js
    # The ordering wraps the filter so filtering and ranking agree.
    assert "return coldestFirst(rows.filter(function (row) {" in js
    assert "}), state.metric);" in js
    # The dropdown says it drives the ranking.
    assert "Which metric the FILE LIST is ranked and barred on" in body
    assert "files with no points of that kind last" in body
    assert "The source view itself always shows all five." in body


def test_file_ordering_matches_the_builders_rule():
    """File ordering matches ``coldest_first`` in ``rtl_buddy.cov.query``: ratio
    ascending, then absolute misses descending."""

    out = _node(
        _file_ordering_js()
        + """
        function row(path, totals) { return { path: path, totals: totals }; }
        function m(found, hit) {
          return { found: found, hit: hit, ratio: found ? hit / found : null };
        }
        var rows = [
          row('cold.sv',   { line: m(10, 2),  toggle: m(4, 4) }),
          row('warm.sv',   { line: m(10, 9),  toggle: m(100, 10) }),
          row('silent.sv', { line: m(0, 0),   toggle: m(8, 1) }),
          row('same.sv',   { line: m(10, 2),  toggle: m(0, 0) }),
          row('big.sv',    { line: m(100, 20), toggle: m(0, 0) })
        ];
        function paths(metric) {
          return coldestFirst(rows, metric).map(function (r) { return r.path; });
        }
        console.log(JSON.stringify(paths('line')));
        console.log(JSON.stringify(paths('toggle')));
        console.log(JSON.stringify(coldestFirst([], 'line')));
        console.log(JSON.stringify(paths('line')));
        """
    )
    line, toggle, empty, again = out.strip().splitlines()
    # 20%, 20%, 20% then 90%; among the three at 20% the most absolute misses (80) comes
    # first and the others keep payload order.
    assert json.loads(line) == ["big.sv", "cold.sv", "same.sv", "warm.sv", "silent.sv"]
    # A different metric is a different ranking: 12.5%, 10%, 100%, then the two silent
    # files in payload order.
    assert json.loads(toggle) == [
        "warm.sv",
        "silent.sv",
        "cold.sv",
        "same.sv",
        "big.sv",
    ]
    # A file with no points of the metric is silent, not cold: last, never a null ratio
    # read as zero.
    assert json.loads(line)[-1] == "silent.sv"
    assert json.loads(empty) == []
    # Ties keep the payload order.
    assert again == line


def test_toggle_grouping_is_per_signal_msb_first():
    """Toggle grouping is per signal, MSB first."""

    out = _node(
        _toggle_grouping_js()
        + """
        var names = ['clk:0->1', 'clk:1->0',
                     'paddr[0]:0->1', 'paddr[1]:1->0',
                     'paddr[3]:0->1', 'paddr[3]:1->0',
                     'mem[1].d[2]:0->1'];
        var groups = groupToggles(names.map(function (name) {
          return { metric: 'toggle', point: { name: name, module: 'blk' } };
        }));
        console.log(JSON.stringify(groups.map(function (g) {
          return {
            base: g.base, scalar: g.scalar, max: g.max, min: g.min, total: g.total,
            cells: g.cells.map(function (c) {
              return [c.bit, !!c.up, !!c.down, !!c.absent];
            })
          };
        })));
        """
    )
    groups = json.loads(out)
    assert [g["base"] for g in groups] == ["clk", "paddr", "mem[1].d"]

    scalar, bus, nested = groups
    # A scalar is a one-cell grid.
    assert scalar["scalar"] is True and scalar["total"] == 2
    assert scalar["cells"] == [[None, True, True, False]]
    # MSB first; bit 2, which the database never emitted, keeps its place.
    assert bus["max"] == 3 and bus["min"] == 0 and bus["total"] == 4
    assert bus["cells"] == [
        [3, True, True, False],
        [2, False, False, True],
        [1, False, True, False],
        [0, True, False, False],
    ]
    # The bracket that indexes the bus is the last one before the colon.
    assert nested["base"] == "mem[1].d" and nested["cells"] == [[2, True, False, False]]


def test_toggle_grouping_keeps_names_it_cannot_parse():
    """A point this pane cannot parse is still reachable."""

    out = _node(
        _toggle_grouping_js()
        + """
        var groups = groupToggles([
          { metric: 'toggle', point: { name: 'not_a_toggle_point' } },
          { metric: 'toggle', point: { name: null } }
        ]);
        console.log(JSON.stringify(groups.map(function (g) {
          return [g.base, g.scalar, g.total];
        })));
        console.log(JSON.stringify(chunkBits([9, 8, 7, 6, 5], 2)));
        console.log(JSON.stringify(parseToggleName('paddr[12]:1->0')));
        """
    )
    bad, chunks, parsed = out.strip().splitlines()
    assert json.loads(bad) == [["not_a_toggle_point", True, 1], ["(unnamed)", True, 1]]
    # Rows of 32 in the page; the chunker is size-agnostic.
    assert json.loads(chunks) == [[9, 8], [7, 6], [5]]
    assert json.loads(parsed) == {"base": "paddr", "bit": 12, "dir": "1->0"}


# elaborated module names vs the design's own vocabulary


def test_the_parameterisation_suffix_is_stripped_once():
    """One trailing ``__<alnum>`` group is stripped (``ip_async_fifo__DB13`` ->
    ``ip_async_fifo``), never more."""

    out = _node(
        _module_names_js()
        + """
        var names = [
          'ip_async_fifo__DB13', 'apb_intf__A8', 'demo_tiny_alu_subsys_top__Az1',
          'demo_tiny_alu', 'ip_cdc_sync', 'ip_cdc_sync__W4',
          'axi__lite__W8', 'axi__lite', '__A8', '', 'a__', 'blk__'
        ];
        console.log(JSON.stringify(names.map(baseModuleName)));
        console.log(JSON.stringify([baseModuleName(null), baseModuleName(undefined)]));
        """
    )
    stripped, nullish = out.strip().splitlines()
    assert json.loads(stripped) == [
        "ip_async_fifo",
        "apb_intf",
        "demo_tiny_alu_subsys_top",
        # No suffix, no change.
        "demo_tiny_alu",
        "ip_cdc_sync",
        "ip_cdc_sync",
        # Exactly one group comes off: `axi__lite__W8` is `axi__lite`.
        "axi__lite",
        # A real name ending in a group is indistinguishable and is stripped; pinned so
        # changing the rule is deliberate.
        "axi",
        # `__A8` is a whole name, so nothing is stripped.
        "__A8",
        "",
        # A trailing `__` has no alnums and is not a suffix.
        "a__",
        "blk__",
    ]
    assert json.loads(nullish) == ["", ""]


def test_two_parameterisations_of_one_module_are_one_chip():
    """Two parameterisations of one module collapse into one chip that remembers both."""

    out = _node(
        _module_names_js()
        + """
        console.log(JSON.stringify(moduleChips(
          ['ip_cdc_handshake__W13', 'ip_cdc_handshake__Wc'])));
        console.log(JSON.stringify(moduleChips(
          ['ip_cdc_sync', 'ip_cdc_sync__W4'])));
        console.log(JSON.stringify(moduleChips(['tb_top', 'EndHook'])));
        console.log(JSON.stringify([moduleChips([]), moduleChips(null)]));
        """
    )
    twice, mixed, plain, empty = out.strip().splitlines()
    assert json.loads(twice) == [
        {
            "base": "ip_cdc_handshake",
            "names": ["ip_cdc_handshake__W13", "ip_cdc_handshake__Wc"],
        }
    ]
    assert json.loads(mixed) == [
        {"base": "ip_cdc_sync", "names": ["ip_cdc_sync", "ip_cdc_sync__W4"]}
    ]
    # First-seen order, so the header does not reshuffle.
    assert json.loads(plain) == [
        {"base": "tb_top", "names": ["tb_top"]},
        {"base": "EndHook", "names": ["EndHook"]},
    ]
    assert json.loads(empty) == [[], []]


def test_inbound_module_targets_match_either_vocabulary():
    """Inbound module targets match the graph's source names or the model's elaborated
    names; exact wins, stripped follows."""

    out = _node(
        _module_names_js()
        + """
        var known = ['apb_intf__A8', 'axi__lite', 'demo_tiny_alu',
                     'ip_async_fifo__DB13', 'ip_cdc_sync', 'ip_cdc_sync__W4'];
        function r(name) { return resolveModuleName(known, name); }
        console.log(JSON.stringify([
          r('ip_async_fifo'), r('ip_async_fifo__DB13'), r('apb_intf'),
          r('demo_tiny_alu'), r('ip_cdc_sync'), r('ip_cdc_sync__W4'),
          r('axi__lite'), r('nope'), r(''), r(null),
          resolveModuleName([], 'ip_async_fifo')
        ]));
        """
    )
    got = json.loads(out.strip())
    assert got == [
        # The source name lands on the one elaboration that carries it.
        "ip_async_fifo__DB13",
        # The elaborated name is still accepted verbatim.
        "ip_async_fifo__DB13",
        "apb_intf__A8",
        "demo_tiny_alu",
        # `ip_cdc_sync` is both a model key and the base of `ip_cdc_sync__W4`; exact-
        # first keeps it off the twin.
        "ip_cdc_sync",
        "ip_cdc_sync__W4",
        # A name that contains `__` matches itself rather than being stripped.
        "axi__lite",
        None,
        None,
        None,
        None,
    ]


def test_the_module_chip_is_clickable_and_reads_as_source():
    """The module chip is clickable and emits the source name, since graph_focus misses
    are silent."""

    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    # Affordance: the class the page uses for clickable pills.
    assert ".pill.act { cursor: pointer; }" in body
    assert (
        ".pill.act:hover { border-color: var(--accent); color: var(--accent); }" in body
    )
    assert "elem('span', 'pill act', chip.base)" in js
    # Built from the collapsing helper, not the raw list.
    assert "moduleChips(row.modules).forEach(function (chip) {" in js
    assert "focusModuleElsewhere(chip.base)" in js
    # The elaborated names live in the tooltip.
    assert "'elaborated as ' + chip.names.join(', ')" in js
    # The wire carries the source name.
    assert "emit('graph_focus', { node: 'module:' + name })" in js
    assert "var name = baseModuleName(base);" in js
    # The dropdown keeps the model's own keys, and says so.
    assert "These are the ELABORATED names the simulator compiled" in body
    # Other surfaces printing a module name follow the chip's rule.
    assert "parts.push(baseModuleName(point.module));" in js
    assert "elem('td', null, row.module ? baseModuleName(row.module) : '—')" in js


def test_inbound_focus_resolves_before_it_filters():
    """An inbound focus resolves before it filters, because the dropdown is keyed on
    elaborated names."""

    js = _page_js()
    assert "var resolved = resolveModuleName(known, name);" in js
    assert "if (resolved === null) { return false; }" in js
    assert "state.module = resolved;" in js
    assert "return (row.modules || []).indexOf(resolved) >= 0;" in js
    # The instance-path heuristic speaks source names too.
    assert "if (resolveModuleName(known, candidates[i]) !== null) {" in js


# cross-app send


def test_the_file_header_offers_a_send_for_every_sibling_app():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    apps = js.split("var APPS = [")[1].split("\n  ];")[0]
    assert "{ origin: 'graph', prose: 'the graph pane' }," in apps
    assert "{ origin: 'view', prose: 'the schematic' }" in apps
    for route in ("/gph", "/sch", "/phy"):
        assert f'<a href="{route}" target="_blank" rel="noopener"' in body
    row = js.split("function renderActions(base) {")[1].split("\n  }")[0]
    assert "'send → ' + originLabel(app.origin)," in row
    assert "'open ' + " not in row
    assert "window.open(" not in row
    # The row lands in the file header, beside the module pills.
    head = js.split("function renderFile() {")[1].split("\n  }")[0]
    assert "actionsEl = renderActions(actionsBase);" in head
    assert "els.fileHead.appendChild(actionsEl);" in head
    assert head.index("moduleChips(row.modules).forEach") < head.index(
        "els.fileHead.appendChild(actionsEl);"
    )
    assert ".actions { display: inline-flex; gap: .25rem; flex-wrap: wrap; }" in body


def test_the_send_row_speaks_for_the_first_module_pill():
    """The send row speaks for the first module pill."""

    js = _page_js()
    head = js.split("function renderFile() {")[1].split("\n  }")[0]
    assert "var primary = null;" in head
    assert "if (primary === null) { primary = chip.base; }" in head
    assert "actionsBase = primary;" in head
    # The send goes through the pill's path, so the wire carries the source name.
    row = js.split("function renderActions(base) {")[1].split("\n  }")[0]
    assert row.count("focusModuleElsewhere(base)") == 1
    assert "emit('graph_focus', { node: 'module:' + name })" in js
    assert "var name = baseModuleName(base);" in js


def test_both_sends_emit_the_one_broadcast_and_say_so():
    """`send -> gph` and `send -> sch` emit the same broadcast, and the tooltips say so."""

    js = _page_js()
    assert (
        "var OVERLAP = 'Hub events are broadcasts: this same graph_focus moves ' +\n"
        "    'the graph pane AND the schematic, whichever of them is open.';" in js
    )
    row = js.split("function renderActions(base) {")[1].split("\n  }")[0]
    assert row.count("OVERLAP") == 1
    # No second wire type.
    assert row.count("emit(") == 0
    assert "selection_changed" not in row


def test_a_send_is_dark_when_its_app_is_not_connected():
    js = _page_js()
    row = js.split("function renderActions(base) {")[1].split("\n  }")[0]
    assert "var live = hasPeer(app.origin);" in row
    assert "!base || !live," in row
    assert "app.prose + ' is not connected — open it from the header links'" in row
    # A file the model records no module for can address neither app.
    assert "!base ? NO_MODULE" in row
    assert "var NO_MODULE = 'This file records no module" in js
    # The peer list is kept and only the row repaints, preserving scroll position and
    # the open detail panel.
    assert "function hasPeer(origin) { return peers.indexOf(origin) >= 0; }" in js
    assert "if (changed) { refreshActions(); }" in js
    refresh = js.split("function refreshActions() {")[1].split("\n  }")[0]
    assert "actionsEl.parentNode.replaceChild(next, actionsEl);" in refresh


def test_the_action_row_has_no_open_buttons():
    """The action row has sends only; open links live in the header switcher."""

    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    row = js.split("function renderActions(base) {")[1].split("\n  }")[0]
    assert "window.open(" not in row
    assert "'open '" not in row
    for route in ("/gph", "/sch", "/phy"):
        assert f'<a href="{route}" target="_blank" rel="noopener"' in body


def test_a_qualified_test_target_matches_the_models_bare_name():
    """A qualified ``test:<suite>#<name>`` target falls back to the model's bare test
    name."""

    js = _page_js()
    focus = js.split("function focusTest(name) {")[1].split("\n  }")[0]
    assert "if (!known(name)) {" in focus
    assert "var hash = String(name).lastIndexOf('#');" in focus
    assert "var bare = hash < 0 ? null : String(name).slice(hash + 1);" in focus
    assert "if (!bare || !known(bare)) { return false; }" in focus
    # Exact first, so a test really called `a#b` still wins.
    assert focus.index("if (!known(name)) {") < focus.index("lastIndexOf('#')")


# the elaboration lens


def test_the_files_elaborations_come_from_its_points_not_its_modules():
    """A file's elaborations come from the modules its points were recorded against,
    line points included; a point with no module never contributes."""

    out = _node(
        _elaboration_lens_js()
        + """
        var row = {
          modules: ['ip_cdc_sync', 'ip_cdc_sync__W4'],
          line: [{ line: 20, module: 'ip_cdc_sync__W8', hits: 2152 }],
          branch: [
            { line: 21, module: 'ip_cdc_sync__W4', hits: 36 },
            { line: 21, module: 'ip_cdc_sync', hits: 108 }
          ],
          toggle: [{ line: 12, module: 'ip_cdc_sync', hits: 1608 }],
          expression: [], cover: []
        };
        var metrics = ['line', 'branch', 'toggle', 'expression', 'cover'];
        console.log(JSON.stringify(elaborationsOf(row, metrics)));
        console.log(JSON.stringify(elaborationsOf(
          { line: [{ line: 1, hits: 3 }] }, metrics)));
        console.log(JSON.stringify(elaborationsOf(null, metrics)));
        """
    )
    spans, lines_only, empty = out.strip().splitlines()
    assert json.loads(spans) == ["ip_cdc_sync", "ip_cdc_sync__W4", "ip_cdc_sync__W8"]
    # Module-less line points (an older model) leave nothing to choose between.
    assert json.loads(lines_only) == []
    assert json.loads(empty) == []


def test_segment_labels_drop_the_base_only_when_it_is_shared():
    """Segment labels drop the base only when it is shared by every segment, e.g. `all ·
    W13 · Wc`."""

    out = _node(
        _elaboration_lens_js()
        + """
        function labels(names) {
          return elaborationSegments(names).map(function (s) { return s.label; });
        }
        console.log(JSON.stringify(labels(
          ['ip_cdc_handshake__W13', 'ip_cdc_handshake__Wc'])));
        console.log(JSON.stringify(labels(['ip_cdc_sync__W4', 'ip_cdc_sync'])));
        console.log(JSON.stringify(labels(['blk__A1', 'other__A2'])));
        console.log(JSON.stringify(elaborationSegments(
          ['ip_cdc_handshake__Wc', 'ip_cdc_handshake__W13', 'ip_cdc_handshake__Wc'])));
        console.log(JSON.stringify(labels(['axi__lite__W8', 'axi__lite__W4'])));
        console.log(JSON.stringify([elaborationSegments([]), elaborationSegments(null)]));
        """
    )
    one, mixed, several, dedup, dunder, empty = out.strip().splitlines()
    assert json.loads(one) == ["W13", "Wc"]
    # The un-parameterised elaboration keeps its plain name.
    assert json.loads(mixed) == ["ip_cdc_sync", "W4"]
    assert json.loads(several) == ["blk__A1", "other__A2"]
    # Sorted and de-duplicated.
    assert json.loads(dedup) == [
        {"name": "ip_cdc_handshake__W13", "label": "W13"},
        {"name": "ip_cdc_handshake__Wc", "label": "Wc"},
    ]
    # Only the last group is a parameterisation, so the shared base is `axi__lite`.
    assert json.loads(dunder) == ["W4", "W8"]
    assert json.loads(empty) == [[], []]


def test_the_lens_filters_points_and_the_groups_score_them():
    """The lens filters points; grouping scores them per elaboration when the lens is
    off."""

    out = _node(
        _elaboration_lens_js()
        + """
        var points = [
          { name: 'if', module: 'ip_cdc_sync', hits: 108, tests: { a: 108, b: 0 } },
          { name: 'if', module: 'ip_cdc_sync__W4', hits: 0, tests: { a: 0, b: 0 } },
          { name: 'else', module: 'ip_cdc_sync__W4', hits: 4, tests: { a: 0, b: 4 } }
        ];
        function names(list) { return list.map(function (p) { return p.module; }); }
        console.log(JSON.stringify(names(pointsOfElaboration(points, 'ip_cdc_sync__W4'))));
        console.log(JSON.stringify(names(pointsOfElaboration(points, null)).length));
        console.log(JSON.stringify(names(pointsOfElaboration(points, 'nope'))));
        console.log(JSON.stringify(pointsOfElaboration(null, 'x')));

        var entries = points.map(function (p) {
          return { metric: 'branch', point: p };
        });
        // Merged hits: the lens-off reading.
        var merged = elaborationGroups(entries, function (p) { return p.hits; });
        console.log(JSON.stringify(merged.map(function (g) {
          return [g.module, g.hit, g.entries.length];
        })));
        // Composed with the test lens: only what `b` hit counts.
        var lensed = elaborationGroups(entries, function (p) {
          return (p.tests || {}).b || 0;
        });
        console.log(JSON.stringify(lensed.map(function (g) {
          return [g.module, g.hit, g.entries.length];
        })));
        // A point with no module recorded still has to be reachable.
        console.log(JSON.stringify(elaborationGroups(
          [{ metric: 'branch', point: { hits: 1 } }],
          function (p) { return p.hits; }
        ).map(function (g) { return [g.module, g.hit]; })));
        """
    )
    only, all_of, miss, nullish, merged, lensed, unnamed = out.strip().splitlines()
    assert json.loads(only) == ["ip_cdc_sync__W4", "ip_cdc_sync__W4"]
    assert json.loads(all_of) == 3
    assert json.loads(miss) == []
    assert json.loads(nullish) == []
    # Sorted by name so the groups do not reorder.
    assert json.loads(merged) == [["ip_cdc_sync", 1, 1], ["ip_cdc_sync__W4", 1, 2]]
    # The intersection: `b` hit nothing in the first elaboration and one of two points
    # in the second.
    assert json.loads(lensed) == [["ip_cdc_sync", 0, 1], ["ip_cdc_sync__W4", 1, 2]]
    assert json.loads(unnamed) == [["", 1]]


def test_the_header_control_appears_only_when_there_is_a_choice():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    assert "var elabs = elaborationsOf(row, METRICS);" in js
    assert "if (elabs.length > 1) {" in js
    assert "els.fileHead.appendChild(elabControl(elaborationSegments(elabs)));" in js
    # A lens naming an elaboration the file has no points for is an empty pane.
    assert (
        "if (state.elab && elabs.indexOf(state.elab) < 0) { state.elab = null; }" in js
    )
    # The segmented control, styled as one strip.
    assert ".seg { display: inline-flex; }" in body
    assert ".seg button.on {" in body
    assert "elem('button', state.elab ? null : 'on', 'all')" in js
    assert "setElab(null)" in js
    assert "setElab(seg.name)" in js


def test_the_lens_recounts_found_for_every_metric():
    """The elaboration lens recounts `found` for every metric, line included (#678)."""

    js = _page_js()
    assert "var elab = state.elab;" in js
    assert "metric === 'line' ? null : state.elab" not in js
    assert "var points = pointsOfElaboration(row[metric], elab);" in js
    assert "var found = elab ? points.length : t.found;" in js
    assert "return { found: found, hit: hit, ratio: found ? hit / found : null };" in js
    # Cells and badges read the same filter through the file view's index.
    assert (
        "pointsOfElaboration(row[metric], state.elab).forEach(function (point) {" in js
    )
    # The column header says which way it reads.
    assert "', counting only ' + state.elab" in js
    assert "elaboration lens leaves it merged" not in js


def test_line_points_fold_per_source_line():
    """Several line points on one source line fold into one L cell: hits and per-test
    hits summed, the hit share scored by the lens's hit function (#678)."""

    out = _node(
        _elaboration_lens_js()
        + """
        var points = [
          { line: 5, column: 3, name: 'if', module: 'blk__W13', hits: 4,
            tests: { a: 4, b: 0 } },
          { line: 5, column: 4, name: 'else', module: 'blk__W13', hits: 0,
            tests: { a: 0, b: 0 } },
          { line: 5, column: 3, name: 'if', module: 'blk__Wc', hits: 2,
            tests: { a: 0, b: 2 } },
          { line: 7, column: 3, name: 'block', module: 'blk__Wc', hits: 0 },
          { line: null, hits: 9 }
        ];
        var merged = foldLinePoints(points, function (p) { return p.hits; });
        console.log(JSON.stringify(Object.keys(merged)));
        var five = merged[5];
        console.log(JSON.stringify([five.hits, five.tests, five.hit, five.points.length,
                                    five.module]));
        console.log(JSON.stringify([merged[7].hits, merged[7].hit, merged[7].module]));
        // The test lens scores by that test's hits.
        var lensed = foldLinePoints(points, function (p) { return (p.tests || {}).b || 0; });
        console.log(JSON.stringify(lensed[5].hit));
        // The elaboration lens folds only that elaboration, plus module-less points.
        var one = foldLinePoints(
          pointsOfElaboration(points.concat([{ line: 5, hits: 1 }]), 'blk__W13'),
          function (p) { return p.hits; });
        console.log(JSON.stringify([Object.keys(one), one[5].points.length, one[5].hit]));
        console.log(JSON.stringify(foldLinePoints(null, function () { return 0; })));
        """
    )
    keys, five, seven, lensed, one, empty = out.strip().splitlines()
    assert json.loads(keys) == ["5", "7"]
    assert json.loads(five) == [6, {"a": 4, "b": 2}, 2, 3, None]
    assert json.loads(seven) == [0, 0, "blk__Wc"]
    assert json.loads(lensed) == 1
    assert json.loads(one) == [["5"], 3, 2]
    assert json.loads(empty) == {}


def test_the_panel_breaks_down_per_elaboration_and_the_subhead_is_the_way_in():
    js = _page_js()
    assert "function renderDetailBody(metric, entries)" in js
    assert "var groups = state.elab ? null : elaborationGroups(entries, hitsFor);" in js
    assert "if (!groups || groups.length < 2) {" in js
    assert "els.detailBody.appendChild(elabSubhead(group));" in js
    assert "els.detailBody.appendChild(detailBlock(metric, group.entries));" in js
    # The subhead carries that group's score and sets the lens.
    assert "elem('span', 'muted', group.hit + '/' + group.entries.length)" in js
    assert "setElab(group.module || null);" in js
    # Bit grids and named chips both go through it.
    assert "groupToggles(entries).forEach(function (group) {" in js
    assert (
        "entries.forEach(function (entry) { chips.appendChild(namedMark(entry)); });"
        in js
    )
    # A lens on is an abnormal state, said where the numbers are, and saying it is the
    # way out.
    assert "if (state.elab) { els.detailHead.appendChild(elabPill()); }" in js
    assert (
        "elem('span', 'pill hot act', 'elab: ' + baseElabLabel(state.elab) + ' ×')"
        in js
    )


def test_an_inbound_elaborated_name_also_sets_the_lens():
    """An inbound elaborated name also sets the lens; the stripped fallback must not
    answer it as the module."""

    js = _page_js()
    assert "var spans = elaborationsOf(rows[0], METRICS).length > 1;" in js
    assert "var elab = spans && resolved === String(name) ? resolved : null;" in js
    assert "line: opts.line, item: opts.item, metric: opts.metric, elab: elab" in js
    # selectFile owns the reset.
    assert "if (opts.elab !== undefined) { state.elab = opts.elab; }" in js
    assert "state.elab = null;" in js


# the hub version label
# Same contract as graph_page.html and the view SPA's `viewer/src/buildInfo.js`; the
# cases match `tests/test_hub_graph_page.py` word for word.


def test_a_dev_build_is_labelled_with_its_git_sha():
    """A dev build is labelled with the git SHA from the ``g``-prefixed run in
    setuptools-scm's local segment."""

    out = _node(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel('6.26.2.dev13+g3f5b890e3.d20260806'),
          versionLabel('6.26.2.dev13+g3f5b890e3'),
          versionLabel('6.26.2.dev1+g0abcdef12.d20260101.dirty'),
          versionLabel('6.26.2.dev13+d20260806.g3f5b890e3')
        ]));
        """
    )
    assert json.loads(out) == [
        "6.26.2.dev13 @ 3f5b890e3",
        "6.26.2.dev13 @ 3f5b890e3",
        "6.26.2.dev1 @ 0abcdef12",
        # The run may sit anywhere in the local segment.
        "6.26.2.dev13 @ 3f5b890e3",
    ]


def test_a_release_is_labelled_by_its_version_alone():
    """A release is labelled by its version alone."""

    out = _node(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify(
          ['6.26.2', '6.26.2.dev13', '0.0.0'].map(versionLabel)));
        """
    )
    assert json.loads(out) == ["6.26.2", "6.26.2.dev13", "0.0.0"]


def test_a_local_segment_without_a_sha_still_labels_the_version():
    """A local segment without a SHA labels the base version and drops the ``@``."""

    out = _node(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel('1.0+local'),
          versionLabel('1.0+d20260806'),
          versionLabel('1.0+gitlab'),
          versionLabel('1.0+'),
          versionLabel('1.0+gabc')
        ]));
        """
    )
    assert json.loads(out) == [
        "1.0",
        "1.0",
        # `gitlab` starts with g but `itlab` is not hex.
        "1.0",
        "1.0",
        # Fewer than 4 hex digits is not a SHA; matches the SPA copy (`buildInfo.js`).
        "1.0",
    ]


def test_no_version_means_no_label_at_all():
    """No ``server_version`` means no label."""

    out = _node(
        _marked_js("version-label")
        + """
        console.log(JSON.stringify([
          versionLabel(''), versionLabel(undefined), versionLabel(null),
          versionLabel('+g3f5b890e3')
        ]));
        """
    )
    # A version that is only a local segment names no release.
    assert json.loads(out) == [None, None, None, None]


def test_the_footer_carries_the_version_and_every_welcome_rewrites_it():
    """The footer carries the version label and every welcome rewrites it."""

    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<span id="hub-version" class="muted"></span>' in body
    # After the peers span, before the flexible gap.
    peers = body.index('<span id="peers"')
    version = body.index('<span id="hub-version"')
    assert peers < version < body.index('<span class="grow"></span>', peers)

    js = _page_js()
    assert "setHubVersion(env.payload && env.payload.server_version);" in js
    assert "els.hubVersion.textContent = label ? 'rtl-buddy ' + label : '';" in js
    assert "els.hubVersion.title = full;" in js


# HTTP endpoints


def _http_get(url: str) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5.0) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest_asyncio.fixture
async def hub_and_viewer(
    covered_project: Path,
) -> AsyncIterator[tuple[HubServer, ViewerServer]]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())

    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        project_root=covered_project,
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
async def test_http_cov_page_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/cov"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert f"{viewer.hub_host}:{viewer.hub_port}".encode("utf-8") in body
    assert b"rtl-buddy-cov" in body


@pytest.mark.asyncio
async def test_http_cov_page_served_with_the_source_figures_param(hub_and_viewer):
    """``/cov?by=source`` is the same page; the script reads the parameter (#651)."""
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/cov?by=source"
    status, _headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert b"state.by = openedBy(location.search);" in body


@pytest.mark.asyncio
async def test_http_cov_json_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/cov.json"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    payload = json.loads(body)
    assert payload["hub"]["schema_version"] == cov_page.PAGE_SCHEMA_VERSION
    assert payload["totals"]["line"]["found"] == 3
    assert payload["files"][0]["path"] == "design/blk.sv"


@pytest.mark.asyncio
async def test_http_cov_source_served_and_contained(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    base = f"http://127.0.0.1:{viewer.http_port}/cov/source?path="
    status, headers, body = await asyncio.to_thread(
        _http_get, base + urllib.parse.quote("design/blk.sv")
    )
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    assert json.loads(body)["lines"][0].startswith("module blk")

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        await asyncio.to_thread(_http_get, base + urllib.parse.quote("../outside.sv"))
    assert excinfo.value.code == 403


@pytest.mark.asyncio
async def test_http_index_advertises_the_cov_url(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/view"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    assert b"window.__RTL_BUDDY_COV_URL__ = '/cov.json'" in body


@pytest.mark.asyncio
async def test_hub_state_marks_the_cov_card_available(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/hub/state.json"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    cards = {app["id"]: app for app in json.loads(body)["apps"]}
    assert cards["cov"]["available"] is True
    assert cards["cov"]["route"] == cov_page.COV_PAGE_ROUTE


def test_index_omits_cov_url_without_coverage():
    body = render_index_html(bundle_index=None, hub_addr="127.0.0.1:1")
    assert b"__RTL_BUDDY_COV_URL__" not in body


@pytest.mark.asyncio
async def test_http_cov_json_404s_without_coverage(tmp_path: Path):
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
                _http_get, f"http://127.0.0.1:{viewer.http_port}/cov.json"
            )
        assert excinfo.value.code == 404
        assert "cov_dir" in json.loads(excinfo.value.read())["error"]

        # The page itself is still 200; its empty state says "collect some coverage".
        page_status, _h, _b = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{viewer.http_port}/cov"
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
async def test_http_cov_json_400_without_project_root():
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(hub_host=hub_host, hub_port=hub_port, http_port=0)
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        for route in ("/cov.json", "/cov/source?path=x"):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                await asyncio.to_thread(
                    _http_get, f"http://127.0.0.1:{viewer.http_port}{route}"
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


# cov_focus: the wire type


@pytest.mark.parametrize(
    "payload",
    [
        {"target": "design/blk.sv"},
        {"target": "module:blk", "metric": "branch"},
        {"target": "file:design/blk.sv", "line": 4, "item": "else"},
        {"target": "test:verif/blk#basic"},
    ],
)
def test_cov_focus_envelope_validates(payload: dict):
    env = Envelope(
        origin=Origin.CLI,
        kind=Kind.EVENT,
        type="cov_focus",
        id=new_id(),
        payload=payload,
    )
    assert decode(encode(env).encode("utf-8")).payload == payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"target": ""},
        {"target": "module:blk", "metric": "statement"},
        {"target": "module:blk", "line": 0},
        {"target": "module:blk", "extra": 1},
        {"target": "module:blk", "metric": None},
    ],
)
def test_cov_focus_rejects_malformed_payloads(payload: dict):
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="cov_focus",
                id=new_id(),
                payload=payload,
            )
        )


def test_cov_origin_is_its_own_peer_slot():
    """The pane has its own ``cov`` peer slot, so it does not evict the ``view`` or
    ``graph`` tab."""

    assert Origin.COV.value == "cov"
    env = Envelope(
        origin=Origin.COV,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={"client": "cov", "version": "1.0.0", "capabilities": ["cov_focus"]},
    )
    assert decode(encode(env).encode("utf-8")).origin is Origin.COV


def test_cov_focus_state_slot_omits_unset_hints():
    """The cov_focus state slot omits unset hints, since the schema forbids null."""

    assert CovFocus(target="module:blk", origin=Origin.CLI).payload() == {
        "target": "module:blk"
    }
    assert CovFocus(
        target="file:design/blk.sv",
        origin=Origin.CLI,
        metric="branch",
        line=4,
        item="else",
    ).payload() == {
        "target": "file:design/blk.sv",
        "metric": "branch",
        "line": 4,
        "item": "else",
    }


class _Peer:
    """Minimal TCP peer, same shape as ``test_hub_graph_page``."""

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
async def test_cov_focus_broadcasts_to_the_pane(bare_hub: HubServer):
    pane = await _Peer.connect(bare_hub.host, bare_hub.port)
    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        assert (await pane.hello(Origin.COV)).type == "welcome"
        assert (await driver.hello(Origin.CLI)).type == "welcome"
        assert (await pane.recv()).type == "peer_joined"

        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="cov_focus",
                id=new_id(),
                payload={"target": "module:blk", "metric": "toggle"},
            )
        )
        env = await pane.recv()
        assert env.type == "cov_focus"
        assert env.origin is Origin.CLI
        assert env.payload == {"target": "module:blk", "metric": "toggle"}
    finally:
        await pane.close()
        await driver.close()


@pytest.mark.asyncio
async def test_cov_focus_is_replayed_to_a_late_pane(bare_hub: HubServer):
    """``rb hub send cov-focus`` before the tab opens is replayed with its hints."""

    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        await driver.hello(Origin.CLI)
        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="cov_focus",
                id=new_id(),
                payload={"target": "file:design/blk.sv", "line": 4, "item": "else"},
            )
        )
        await asyncio.sleep(0.1)
        assert bare_hub.state.cov_focus is not None
        assert bare_hub.state.cov_focus.target == "file:design/blk.sv"
        assert bare_hub.state.cov_focus.line == 4

        pane = await _Peer.connect(bare_hub.host, bare_hub.port)
        try:
            assert (await pane.hello(Origin.COV)).type == "welcome"
            replayed = await pane.recv()
            assert replayed.type == "cov_focus"
            assert replayed.payload == {
                "target": "file:design/blk.sv",
                "line": 4,
                "item": "else",
            }
        finally:
            await pane.close()
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_hub_state_reset_clears_the_cov_slot(bare_hub: HubServer):
    bare_hub.state.cov_focus = CovFocus(target="module:blk", origin=Origin.CLI)
    bare_hub.state.reset()
    assert bare_hub.state.cov_focus is None


# display names vs wire origins
# Each pane carries its own origin->label map; see the same section in
# `tests/test_hub_graph_page.py`.


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
    # The same map as the graph pane's and the landing's.
    assert "var ORIGIN_LABELS = { view: 'sch', graph: 'gph', phys: 'phy' };" in js
    assert "list.map(originLabel).join(', ')" in js
    assert "originLabel(links[i].getAttribute('data-origin'))" in js
    assert "'send → ' + originLabel(app.origin)," in js


def test_the_header_switcher_links_every_sibling_pane():
    """The header switcher links every sibling pane, including `/phy`."""

    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    switcher = body.split('<nav class="switcher"')[1].split("</nav>")[0]
    for route, origin in (("/sch", "view"), ("/gph", "graph"), ("/phy", "phys")):
        assert (
            f'<a href="{route}" target="_blank" rel="noopener" data-origin="{origin}"'
            in switcher
        )


def test_the_rename_did_not_leak_into_the_wire():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    js = _page_js()
    assert "origin: 'cov', kind: 'request', type: 'hello'," in js
    assert "origin: 'graph'," in js  # the APPS entries address wire values
    assert "origin: 'view'," in js
    assert 'href="/sch"' in body
    assert 'href="/gph"' in body
    assert 'href="/phy"' in body
