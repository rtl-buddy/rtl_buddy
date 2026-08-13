"""Tests for the hub landing page at ``GET /`` (#398).

The landing exists to answer "what can this hub do for me right now",
so the things worth pinning are the *advertisement rules* rather than
the markup: which card is live, which is greyed and why, and which app
already has a tab attached — the last one because the hub allows one
client per origin and a second tab supersedes the first, so the warning
has to arrive before the click.

The page itself is held to the graph pane's offline rule: no build step
and no reference off localhost.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub import landing_page, theme
from rtl_buddy.hub.server import HubServer
from rtl_buddy.hub.viewer_http import ViewerServer


def _apps(payload: dict) -> dict[str, dict]:
    return {app["id"]: app for app in payload["apps"]}


# ---------------------------------------------------------------------------
# advertisement rules
# ---------------------------------------------------------------------------


def test_graph_card_follows_data_presence():
    """Same rule ``_has_graph_json`` applies to the SPA's graph global."""

    without = _apps(build := landing_page.build_state_payload(hub_addr="h:1"))
    assert without["graph"]["available"] is False
    assert "rb graph build" in without["graph"]["note"]
    assert build["graph"]["present"] is False

    with_graph = _apps(
        landing_page.build_state_payload(
            hub_addr="h:1",
            graph_present=True,
            graph_path="artefacts/graph/graph.json",
            graph_mtime=time.time() - 120,
        )
    )
    assert with_graph["graph"]["available"] is True
    assert with_graph["graph"]["note"] is None


def test_graph_freshness_is_reported():
    """ "Built 3 days ago" is the difference between a graph worth
    opening and one that predates the branch."""

    now = 1_000_000.0
    payload = landing_page.build_state_payload(
        hub_addr="h:1",
        graph_present=True,
        graph_path="artefacts/graph/graph.json",
        graph_mtime=now - 3600,
        now=now,
    )
    assert payload["graph"]["age_seconds"] == pytest.approx(3600)
    assert payload["graph"]["built_at"].startswith("1970-01-12")

    unbuilt = landing_page.build_state_payload(hub_addr="h:1")
    assert unbuilt["graph"]["age_seconds"] is None
    assert unbuilt["graph"]["built_at"] is None


def test_cov_card_advertises_on_data_presence():
    """The pane ships (rtl-buddy/rtl_buddy#400), so the card is live and
    availability follows the artefacts — same rule as the graph's.

    A hub with no coverage run keeps the card, muted, carrying the
    command that would produce some: "the pane exists and you have not
    collected coverage" is the useful message, and a hidden card cannot
    deliver it.
    """

    cov = _apps(landing_page.build_state_payload(hub_addr="h:1"))["cov"]
    assert cov["status"] == "live"
    assert cov["available"] is False
    assert "coverage" in cov["note"]

    ready = _apps(landing_page.build_state_payload(hub_addr="h:1", cov_available=True))
    assert ready["cov"]["status"] == "live"
    assert ready["cov"]["available"] is True
    assert ready["cov"]["note"] is None


def test_already_open_comes_from_the_peer_registry():
    """One client per origin: a second tab supersedes the first."""

    payload = landing_page.build_state_payload(
        hub_addr="h:1", peers=["graph", "cli", "wave"]
    )
    apps = _apps(payload)
    assert apps["graph"]["open"] is True
    assert apps["view"]["open"] is False
    # ``cli`` is the hub's own one-shot peer, never an app someone opened.
    assert payload["peers"] == ["graph", "wave"]


def test_view_card_notes_a_missing_bundle_but_stays_live():
    """``/view`` always answers — without a bundle it serves the
    placeholder, which explains itself better than a greyed card."""

    apps = _apps(
        landing_page.build_state_payload(
            hub_addr="h:1", view_note="no viewer bundle installed"
        )
    )
    assert apps["view"]["available"] is True
    assert apps["view"]["note"] == "no viewer bundle installed"


def test_every_card_names_a_real_origin_and_route():
    payload = landing_page.build_state_payload(hub_addr="h:1")
    from rtl_buddy.hub.protocol import Origin

    known = {o.value for o in Origin}
    for app in payload["apps"]:
        assert app["route"].startswith("/")
        assert app["task"] and app["why"]
        # Every shipped app is a real hub origin — that is what makes
        # the "already open" badge possible at all.
        if app["status"] == "live":
            assert app["origin"] in known, app["origin"]


def test_hub_block_carries_the_project_state():
    payload = landing_page.build_state_payload(
        hub_addr="127.0.0.1:5",
        server_version="9.9.9",
        project_root=Path("/tmp/proj"),
        active_model="ip_demo",
        active_test="smoke",
    )
    assert payload["hub"] == {
        "addr": "127.0.0.1:5",
        "server_version": "9.9.9",
        "project_root": "/tmp/proj",
        "active_model": "ip_demo",
        "active_test": "smoke",
    }


def test_graph_state_reads_mtime(tmp_path: Path):
    present, rel, mtime = landing_page.graph_state(tmp_path)
    assert (present, rel, mtime) == (False, None, None)

    graph_dir = tmp_path / "artefacts" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")
    present, rel, mtime = landing_page.graph_state(tmp_path)
    assert present is True
    assert rel == "artefacts/graph/graph.json"
    assert mtime is not None

    assert landing_page.graph_state(None) == (False, None, None)


def test_graph_state_agrees_with_the_graph_route(tmp_path: Path):
    """One predicate, two consumers: the landing's ``graph_state`` and the
    ``/graph.json`` route's ``graph_files_present`` must derive from the
    same path, or the landing could advertise a graph the route 404s on."""

    from rtl_buddy.hub import graph_page

    assert landing_page.graph_state(tmp_path)[0] is graph_page.graph_files_present(
        tmp_path
    )

    path = graph_page.graph_json_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    assert landing_page.graph_state(tmp_path)[0] is True
    assert graph_page.graph_files_present(tmp_path) is True


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def test_landing_injects_the_hub_address():
    body = landing_page.render_landing_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "%HUB_INJECTION%" not in body


def test_landing_is_self_contained():
    """Same offline rule as the graph pane: every reference is a
    same-origin absolute path, nothing points off the machine."""

    body = landing_page.render_landing_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "<script src=" not in body
    assert "@import" not in body
    for host in ("cdn.", "unpkg", "jsdelivr", "googleapis", "//fonts"):
        assert host not in body
    for attr in ("href=", "src="):
        for chunk in body.split(attr)[1:]:
            quote = chunk[0]
            value = chunk[1:].split(quote)[0] if quote in "\"'" else chunk.split()[0]
            assert value.startswith("/"), f"{attr}{value}"


def test_landing_carries_the_chrome_the_contract_asks_for():
    body = landing_page.render_landing_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # identity left: ~40px chip logo beside the wordmark
    assert theme.LOGO_80 in body
    assert 'width="40" height="40"' in body
    assert "rtl-buddy hub" in body
    # app switcher right, with the ⌂ hub marker
    assert "⌂ hub" in body
    # bottom strip: one connection vocabulary
    for word in ("connected", "connecting…", "offline"):
        assert word in body, word
    # empty state may carry one small mascot
    assert theme.MASCOT_240 in body
    # it polls instead of holding an origin — see the module docstring
    assert landing_page.STATE_JSON_ROUTE in body
    assert "new WebSocket" not in body


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def _http_get(url: str) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5.0) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest_asyncio.fixture
async def hub_and_viewer(tmp_path: Path) -> AsyncIterator[ViewerServer]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        project_root=tmp_path,
        initial_model="ip_demo",
        hub_server=hub,
    )
    await viewer.start()
    viewer_task = asyncio.create_task(viewer.serve_forever())
    try:
        yield viewer
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
async def test_root_serves_the_landing(hub_and_viewer: ViewerServer):
    url = f"http://127.0.0.1:{hub_and_viewer.http_port}/"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert b"rtl-buddy hub" in body
    # ...and it is NOT the SPA any more.
    assert b"schematic placeholder" not in body


@pytest.mark.asyncio
async def test_state_json_reflects_the_live_hub(hub_and_viewer: ViewerServer):
    url = f"http://127.0.0.1:{hub_and_viewer.http_port}{landing_page.STATE_JSON_ROUTE}"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    payload = json.loads(body)
    assert payload["hub"]["active_model"] == "ip_demo"
    assert payload["hub"]["addr"] == hub_and_viewer.hub_address
    assert payload["hub"]["project_root"] is not None
    apps = _apps(payload)
    assert apps["graph"]["available"] is False  # tmp_path has no graph
    assert apps["view"]["note"]  # no bundle in this fixture


@pytest.mark.asyncio
async def test_state_json_sees_a_graph_built_after_startup(
    hub_and_viewer: ViewerServer, tmp_path: Path
):
    """Per-request, like ``/models``: no hub restart to advertise a graph."""

    url = f"http://127.0.0.1:{hub_and_viewer.http_port}{landing_page.STATE_JSON_ROUTE}"
    graph_dir = tmp_path / "artefacts" / "graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.json").write_text("{}", encoding="utf-8")
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    graph = _apps(json.loads(body))["graph"]
    assert graph["available"] is True
    assert json.loads(body)["graph"]["path"] == "artefacts/graph/graph.json"


# ---------------------------------------------------------------------------
# display names vs wire origins
#
# The landing is where an app's LONG name is introduced (the cards); the
# short name it is then navigated by rides in the same payload and fills
# the switcher. Neither is the wire value: the schematic still registers
# as ``view``, so the peer list goes through the same origin→label map
# the panes carry.
# ---------------------------------------------------------------------------


def _page_js() -> str:
    body = landing_page.render_landing_html(hub_addr="127.0.0.1:1").decode("utf-8")
    return body.split("<script>")[-1].split("</script>")[0]


def _marked_js(marker: str) -> str:
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


def test_the_origin_label_map_renames_only_the_display():
    out = _node(
        _marked_js("origin-labels")
        + """
        var origins = ['view', 'graph', 'cov', 'wave', 'src', 'cli',
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
        "wave",
        "src",
        "cli",
        "notebook",
        "quantum",
    ]
    assert json.loads(nullish) == ["", "", ""]
    assert json.loads(inherited) == "toString"


def test_both_peer_lists_go_through_the_map():
    js = _page_js()
    # Word for word the panes' map — see their copies of this test.
    assert "var ORIGIN_LABELS = { view: 'sch', graph: 'gph' };" in js
    assert "peers.map(originLabel).join(', ')" in js  # the "this hub" table
    assert "peers.map(originLabel).join(' ')" in js  # the bottom strip


def test_each_card_carries_a_long_name_and_a_short_one():
    """Long name introduces the app, short name is what every other
    surface then calls it. The wire origin is neither."""

    apps = _apps(landing_page.build_state_payload(hub_addr="h:1"))
    assert (apps["view"]["name"], apps["view"]["short"]) == (
        "rtl-buddy-schematic",
        "sch",
    )
    assert (apps["graph"]["name"], apps["graph"]["short"]) == ("rtl-buddy-graph", "gph")
    assert (apps["cov"]["name"], apps["cov"]["short"]) == ("rtl-buddy-coverage", "cov")
    # The rename did not reach the wire, or the "already open" join breaks.
    assert [app["origin"] for app in (apps["view"], apps["graph"], apps["cov"])] == [
        "view",
        "graph",
        "cov",
    ]
    # The PAGE route is the app's short name since #423; the ORIGIN
    # asserted above is what did not move, and that is the real fence.
    assert apps["view"]["route"] == "/sch"


def test_the_card_shows_the_long_name_and_the_switcher_the_short_one():
    js = _page_js()
    card = js.split("function renderCard(app) {")[1].split("\n  }")[0]
    assert "appLine.appendChild(el('span', 'short', app.short));" in card
    assert "app.route ? app.name + '  ' + app.route : app.name));" in card
    switcher = js.split("function renderSwitcher(apps) {")[1].split("\n  }")[0]
    assert "var a = el('a', null, app.short || app.name);" in switcher
    assert "app.name" not in switcher.split("app.short || app.name")[1]


def test_the_footer_carries_the_family_version_label():
    """The three apps show `rtl-buddy <base> @ <sha>` in their strips;
    the landing joins them, fed from /hub/state.json's
    ``hub.server_version`` rather than a welcome (it is not a peer)."""

    body = landing_page.render_landing_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert '<span id="hub-version" class="muted"></span>' in body
    js = _page_js()
    assert "hubVersion: document.getElementById('hub-version')," in js
    assert "var label = versionLabel(hub.server_version);" in js
    assert "els.hubVersion.textContent = label ? 'rtl-buddy ' + label : '';" in js


def test_version_label_agrees_with_the_pane_copies():
    """Same lockstep cases as tests/test_hub_graph_page.py /
    test_hub_cov_page.py pin — four copies of one rule."""

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
