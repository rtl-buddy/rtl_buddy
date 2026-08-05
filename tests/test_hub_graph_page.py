"""Tests for the hub-served design-knowledge-graph pane (#382).

Three surfaces, in the order a user meets them:

1. ``GET /graph.json`` — ``artefacts/graph/graph.json`` joined with
   ``artefacts/graph/results-overlay.json`` in memory. The join must not
   touch ``graph.json`` on disk: hash stability across regressions is
   the whole reason the overlay is a separate file (#379).
2. ``GET /graph`` — one self-contained HTML document. The offline rule
   is asserted structurally (no external ``src``/``href``, no CDN host),
   because "it worked on my laptop" is exactly the failure mode a hub
   running on an air-gapped build machine hits.
3. ``graph_focus`` — the wire type behind ``rb hub send graph-focus``:
   schema-valid, broadcast to peers, and replayed to a peer that
   connects after the fact.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub import graph_page
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
from rtl_buddy.hub.viewer_http import ViewerServer, render_index_html


# ---------------------------------------------------------------------------
# fixtures — a minimal built graph on disk
# ---------------------------------------------------------------------------


_GRAPH = {
    "directed": True,
    "multigraph": True,
    "graph": {
        "schema_version": 1,
        "generator": {"tool": "rtl_buddy", "version": "0.0.0+test", "tier": "merged"},
        "project_root_rel": ".",
    },
    "nodes": [
        {
            "id": "module:fifo",
            "type": "module",
            "label": "fifo",
            "tier": "design",
            "file": "design/fifo/src/fifo.sv",
            "line": 3,
        },
        {
            "id": "inst:fifo/fifo.u_wr",
            "type": "instance",
            "label": "u_wr",
            "tier": "design",
            "file": "design/fifo/src/fifo.sv",
            "line": 9,
        },
        {
            "id": "test:verif/fifo#smoke",
            "type": "test",
            "label": "smoke",
            "tier": "config",
            "file": "verif/fifo/tests.yaml",
            "line": 4,
        },
        {
            "id": "test:verif/fifo#burst",
            "type": "test",
            "label": "burst",
            "tier": "config",
            "file": "verif/fifo/tests.yaml",
            "line": 12,
        },
        {
            "id": "py:verif/fifo/cocotb_fifo.py",
            "type": "python_module",
            "label": "cocotb_fifo",
            "tier": "binding",
            "file": "verif/fifo/cocotb_fifo.py",
        },
    ],
    "links": [
        {
            "source": "inst:fifo/fifo.u_wr",
            "target": "module:fifo_wr",
            "type": "instance_of",
            "confidence": "EXTRACTED",
        },
        {
            "source": "test:verif/fifo#smoke",
            "target": "py:verif/fifo/cocotb_fifo.py",
            "type": "binds_to",
            "confidence": "EXTRACTED",
        },
    ],
}

_OVERLAY = {
    "rtl-buddy-filetype": "graph_results_overlay",
    "schema_version": 1,
    "summary": {"tests": 2, "statuses": {"PASS": 1, "FAIL": 1}},
    "tests": {
        "test:verif/fifo#smoke": {
            "id": "test:verif/fifo#smoke",
            "suite": "verif/fifo",
            "test": "smoke",
            "status": "PASS",
            "source": "envelope",
        },
        "test:verif/fifo#burst": {
            "id": "test:verif/fifo#burst",
            "suite": "verif/fifo",
            "test": "burst",
            "status": "FAIL",
            "desc": "checker mismatch at 120 ns",
            "source": "envelope",
        },
    },
}


@pytest.fixture
def built_graph(tmp_path: Path) -> Path:
    """A project root with ``artefacts/graph/{graph,results-overlay}.json``."""

    out = tmp_path / "artefacts" / "graph"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(json.dumps(_GRAPH), encoding="utf-8")
    (out / "results-overlay.json").write_text(json.dumps(_OVERLAY), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# build_graph_payload
# ---------------------------------------------------------------------------


def test_payload_joins_overlay_onto_test_nodes(built_graph: Path):
    payload = graph_page.build_graph_payload(built_graph)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["test:verif/fifo#smoke"]["results"]["status"] == "PASS"
    assert by_id["test:verif/fifo#burst"]["results"]["status"] == "FAIL"
    # Non-test nodes get no entry — the overlay is keyed by test node id.
    assert "results" not in by_id["module:fifo"]


def test_payload_never_writes_graph_json_back(built_graph: Path):
    """The join is in-memory; graph.json stays byte-identical.

    This is the #379 invariant restated at the hub: a pane that wrote
    its annotations back would make graph.json churn on every
    regression and break the build fingerprint's no-op re-run.
    """

    graph_file = built_graph / "artefacts" / "graph" / "graph.json"
    before = graph_file.read_bytes()
    graph_page.build_graph_payload(built_graph)
    graph_page.build_graph_payload(built_graph)
    assert graph_file.read_bytes() == before


def test_payload_hub_block_describes_the_render(built_graph: Path):
    hub = graph_page.build_graph_payload(built_graph)["graph"]["hub"]
    assert hub["schema_version"] == graph_page.PAGE_SCHEMA_VERSION
    assert hub["graph_path"] == "artefacts/graph/graph.json"
    assert hub["overlay_path"] == "artefacts/graph/results-overlay.json"
    # Counts are of the served body, so a dangling link target
    # (``module:fifo_wr``, which no tier exported) is a link but not a
    # node — the page materialises it client-side.
    assert hub["counts"]["nodes"] == len(_GRAPH["nodes"])
    assert hub["counts"]["links"] == len(_GRAPH["links"])
    assert hub["counts"]["with_results"] == 2
    assert hub["tiers"]["design"] == 2
    assert hub["tiers"]["config"] == 2
    assert hub["tiers"]["binding"] == 1
    assert hub["overlay_summary"]["statuses"] == {"PASS": 1, "FAIL": 1}


def test_payload_without_overlay_is_still_served(tmp_path: Path):
    out = tmp_path / "artefacts" / "graph"
    out.mkdir(parents=True)
    (out / "graph.json").write_text(json.dumps(_GRAPH), encoding="utf-8")
    hub = graph_page.build_graph_payload(tmp_path)["graph"]["hub"]
    assert hub["overlay_path"] is None
    assert hub["counts"]["with_results"] == 0


def test_payload_bytes_404_names_the_build_command(tmp_path: Path):
    status, body = graph_page.graph_payload_bytes(tmp_path)
    assert status == 404
    assert "rb graph build" in json.loads(body)["error"]


def test_graph_files_present(tmp_path: Path, built_graph: Path):
    assert graph_page.graph_files_present(built_graph) is True
    unbuilt = tmp_path / "no-graph-here"
    unbuilt.mkdir()
    assert graph_page.graph_files_present(unbuilt) is False


# ---------------------------------------------------------------------------
# render_graph_html — the offline rule
# ---------------------------------------------------------------------------


def test_page_injects_hub_address():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_GRAPH_URL__ = '/graph.json'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """No CDN, no external stylesheet, no remote font, no import."""

    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    assert "<script src=" not in body
    assert 'rel="stylesheet"' not in body
    assert "@import" not in body
    for host in ("cdn.", "unpkg", "jsdelivr", "googleapis", "//fonts"):
        assert host not in body
    # The only absolute URLs may be the SVG namespace and href="/graph"-style
    # same-origin paths; nothing may point off the machine.
    for scheme in ("https://", "http://"):
        for chunk in body.split(scheme)[1:]:
            authority = chunk.split("'")[0].split('"')[0].split(" ")[0]
            assert authority.startswith("www.w3.org"), authority


def test_page_carries_the_pieces_the_issue_asks_for():
    body = graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # colour by tier, pass/fail badges, and the two envelope types
    for token in ("design", "config", "binding", "PASS", "FAIL"):
        assert token in body
    assert "selection_changed" in body
    assert "open_source" in body
    assert "graph_focus" in body
    assert "'graph'" in body  # registers under its own origin


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def _http_get(url: str) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5.0) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest_asyncio.fixture
async def hub_and_viewer(
    built_graph: Path,
) -> AsyncIterator[tuple[HubServer, ViewerServer]]:
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())

    viewer = ViewerServer(
        hub_host=hub_host,
        hub_port=hub_port,
        http_port=0,
        project_root=built_graph,
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
async def test_http_graph_page_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/graph"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert f"{viewer.hub_host}:{viewer.hub_port}".encode("utf-8") in body
    assert b"design knowledge graph" in body


@pytest.mark.asyncio
async def test_http_graph_json_served(hub_and_viewer):
    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/graph.json"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    payload = json.loads(body)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["test:verif/fifo#burst"]["results"]["status"] == "FAIL"
    assert payload["graph"]["hub"]["counts"]["with_results"] == 2


@pytest.mark.asyncio
async def test_http_index_advertises_the_graph_url(hub_and_viewer):
    """A hub with a built graph sets ``__RTL_BUDDY_GRAPH_URL__``."""

    _hub, viewer = hub_and_viewer
    url = f"http://127.0.0.1:{viewer.http_port}/"
    _status, _headers, body = await asyncio.to_thread(_http_get, url)
    assert b"window.__RTL_BUDDY_GRAPH_URL__ = '/graph.json'" in body


def test_index_omits_graph_url_without_a_graph():
    body = render_index_html(bundle_index=None, hub_addr="127.0.0.1:1")
    assert b"__RTL_BUDDY_GRAPH_URL__" not in body


@pytest.mark.asyncio
async def test_http_graph_json_404_without_a_built_graph(tmp_path: Path):
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(
        hub_host=hub_host, hub_port=hub_port, http_port=0, project_root=tmp_path
    )
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/graph.json"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(_http_get, url)
        assert excinfo.value.code == 404
        assert "rb graph build" in json.loads(excinfo.value.read())["error"]

        # The page itself is still 200 — its empty state is the better
        # place to say "run rb graph build" than a blank browser tab.
        page_status, _h, _b = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{viewer.http_port}/graph"
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
async def test_http_graph_json_400_without_project_root():
    hub = HubServer(host="127.0.0.1", port=0, server_version="0.0.0+test")
    hub_host, hub_port = await hub.start()
    hub_task = asyncio.create_task(hub.serve_forever())
    viewer = ViewerServer(hub_host=hub_host, hub_port=hub_port, http_port=0)
    await viewer.start()
    vtask = asyncio.create_task(viewer.serve_forever())
    try:
        url = f"http://127.0.0.1:{viewer.http_port}/graph.json"
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(_http_get, url)
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
# graph_focus — the wire type
# ---------------------------------------------------------------------------


def test_graph_focus_envelope_validates():
    env = Envelope(
        origin=Origin.CLI,
        kind=Kind.EVENT,
        type="graph_focus",
        id=new_id(),
        payload={"node": "test:verif/fifo#smoke"},
    )
    assert decode(encode(env).encode("utf-8")).payload == {
        "node": "test:verif/fifo#smoke"
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"node": ""}, {"node": "module:fifo", "extra": 1}],
)
def test_graph_focus_rejects_malformed_payloads(payload: dict):
    with pytest.raises(HubProtocolError):
        encode(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="graph_focus",
                id=new_id(),
                payload=payload,
            )
        )


def test_graph_origin_is_its_own_peer_slot():
    """The pane must not share ``view`` with the SPA.

    The hub allows one client per origin, and the acceptance criteria
    require both open at once ("clicking a module node selects it in the
    design view"), so a shared slot would make them evict each other.
    """

    assert Origin.GRAPH.value == "graph"
    env = Envelope(
        origin=Origin.GRAPH,
        kind=Kind.REQUEST,
        type="hello",
        id=new_id(),
        payload={"client": "graph", "version": "1.0.0", "capabilities": []},
    )
    assert decode(encode(env).encode("utf-8")).origin is Origin.GRAPH


class _Peer:
    """Minimal TCP peer, same shape as ``test_hub_send_and_snapshot``."""

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
async def test_graph_focus_broadcasts_to_the_pane(bare_hub: HubServer):
    pane = await _Peer.connect(bare_hub.host, bare_hub.port)
    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        assert (await pane.hello(Origin.GRAPH)).type == "welcome"
        assert (await driver.hello(Origin.CLI)).type == "welcome"
        # The pane sees `peer_joined` for the CLI first.
        assert (await pane.recv()).type == "peer_joined"

        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="graph_focus",
                id=new_id(),
                payload={"node": "module:fifo"},
            )
        )
        env = await pane.recv()
        assert env.type == "graph_focus"
        assert env.origin is Origin.CLI
        assert env.payload == {"node": "module:fifo"}
    finally:
        await pane.close()
        await driver.close()


@pytest.mark.asyncio
async def test_graph_focus_is_replayed_to_a_late_pane(bare_hub: HubServer):
    """``rb hub send graph-focus`` before the tab is open still lands."""

    driver = await _Peer.connect(bare_hub.host, bare_hub.port)
    try:
        await driver.hello(Origin.CLI)
        await driver.send(
            Envelope(
                origin=Origin.CLI,
                kind=Kind.EVENT,
                type="graph_focus",
                id=new_id(),
                payload={"node": "covitem:fifo#FIFO-COV-1"},
            )
        )
        await asyncio.sleep(0.1)
        assert bare_hub.state.graph_focus is not None
        assert bare_hub.state.graph_focus.node == "covitem:fifo#FIFO-COV-1"

        pane = await _Peer.connect(bare_hub.host, bare_hub.port)
        try:
            assert (await pane.hello(Origin.GRAPH)).type == "welcome"
            replayed = await pane.recv()
            assert replayed.type == "graph_focus"
            assert replayed.payload == {"node": "covitem:fifo#FIFO-COV-1"}
        finally:
            await pane.close()
    finally:
        await driver.close()
