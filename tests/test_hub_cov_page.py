"""Tests for the hub-served coverage pane (rtl-buddy/rtl_buddy#400).

Modelled on ``test_hub_graph_page.py``, because the pane is modelled on
the graph pane. Five surfaces, in the order a user meets them:

1. ``GET /cov.json`` — the newest run's coverage model + manifest,
   assembled by the *same* builders ``rb cov summary`` uses. The point
   pinned here is that the numbers agree: a pane that recomputed totals
   would eventually disagree with the CLI, and the disagreement would be
   discovered by a person defending a coverage number in a review.
2. ``GET /cov/source`` — the file text the annotation renders against.
   It takes a path from a query string, so containment under the project
   root is asserted rather than assumed.
3. ``GET /cov`` — one self-contained HTML document. The offline rule is
   checked structurally (no external ``src``/``href``, no CDN host),
   because "it worked on my laptop" is exactly the failure mode a hub on
   an air-gapped build machine hits.
4. Presence + advertisement — the landing card and the SPA global follow
   discovered artefacts, not a build-time flag.
5. ``cov_focus`` — the wire type behind ``rb hub send cov-focus``:
   schema-valid, broadcast to peers, and replayed to a pane that
   connects after the fact, which is what makes "send it before the tab
   is open" work.
"""

from __future__ import annotations

import asyncio
import json
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


# ---------------------------------------------------------------------------
# fixtures — one run's coverage artefacts on disk
# ---------------------------------------------------------------------------

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
    """Each test starts with an empty presence cache.

    :func:`cov_page.cov_data_present` memoises for a few seconds so the
    landing poll does not walk the tree on every request; inside a test
    that TTL would leak one project's answer into the next one's
    ``tmp_path``.
    """

    cov_page._presence_cache.clear()
    yield
    cov_page._presence_cache.clear()


# ---------------------------------------------------------------------------
# build_cov_payload
# ---------------------------------------------------------------------------


def test_payload_is_the_cli_builder_plus_a_hub_block(covered_project: Path):
    """The pane and ``rb cov summary`` may differ in presentation, never
    in numbers — so the payload IS the query builder's output."""

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
    """The summary reports totals; the pane renders points. Dropping the
    points would put the "which test hit this line" join on the client
    and cost a request per file."""

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
    """A walk per landing poll would be a walk per second on a big tree."""

    calls = []
    real = manifest_mod.discover_manifests

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(cov_page.manifest_mod, "discover_manifests", counted)
    assert cov_page.cov_data_present(covered_project) is True
    assert cov_page.cov_data_present(covered_project) is True
    assert len(calls) == 1
    # ttl=0 is the "ask again now" escape hatch the tests (and a future
    # explicit refresh) need.
    assert cov_page.cov_data_present(covered_project, ttl=0) is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# read_source_lines — the one lazy edge, and its containment rule
# ---------------------------------------------------------------------------


def test_source_lines_are_returned_one_per_line(covered_project: Path):
    status, body = cov_page.read_source_lines(covered_project, "design/blk.sv")
    assert status == 200
    payload = json.loads(body)
    assert payload["path"] == "design/blk.sv"
    # Line N at index N-1 — the shape the gutter renders against.
    assert payload["lines"][0].startswith("module blk")
    assert payload["lines"][5].strip().startswith("cover property")


@pytest.mark.parametrize(
    "requested",
    [
        "../outside.sv",
        "design/../../outside.sv",
        "/etc/hosts",
        # Absolute AND lexically prefixed by the root: containment is a
        # string comparison, so this escapes unless the path is resolved
        # first.
        "{root}/../outside.sv",
        "{root}/design/../../outside.sv",
    ],
)
def test_source_refuses_paths_outside_the_project(
    covered_project: Path, requested: str
):
    """The route takes its argument from a query string, and a browser
    tab is reachable by anything that can reach the port."""

    (covered_project.parent / "outside.sv").write_text("secret\n", encoding="utf-8")
    requested = requested.format(root=covered_project)
    status, body = cov_page.read_source_lines(covered_project, requested)
    assert status == 403
    assert "outside the project root" in json.loads(body)["error"]
    assert b"secret" not in body


def test_source_accepts_an_absolute_path_inside_the_project(covered_project: Path):
    """The wire schema advertises absolute targets; containment, not the
    shape of the path, is what the route enforces."""

    requested = str(covered_project / "design/blk.sv")
    status, body = cov_page.read_source_lines(covered_project, requested)
    assert status == 200
    assert json.loads(body)["lines"][0].startswith("module blk")


def test_source_missing_and_empty_requests(covered_project: Path):
    status, body = cov_page.read_source_lines(covered_project, "")
    assert status == 400 and "?path=" in json.loads(body)["error"]
    status, body = cov_page.read_source_lines(covered_project, "design/gone.sv")
    assert status == 404 and "design/gone.sv" in json.loads(body)["error"]


def test_source_refuses_a_file_over_the_annotation_limit(
    covered_project: Path, monkeypatch
):
    monkeypatch.setattr(cov_page, "MAX_SOURCE_BYTES", 8)
    status, body = cov_page.read_source_lines(covered_project, "design/blk.sv")
    assert status == 413
    assert "annotation limit" in json.loads(body)["error"]


# ---------------------------------------------------------------------------
# render_cov_html — the offline rule
# ---------------------------------------------------------------------------


def test_page_injects_hub_address():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:54321").decode("utf-8")
    assert "window.__RTL_BUDDY_HUB__ = '127.0.0.1:54321'" in body
    assert "window.__RTL_BUDDY_COV_URL__ = '/cov.json'" in body
    assert "window.__RTL_BUDDY_COV_SOURCE_URL__ = '/cov/source'" in body
    assert "%HUB_INJECTION%" not in body


def test_page_is_self_contained():
    """No CDN, no remote font, no import, no off-machine reference.

    Every ``src``/``href`` that is not a page anchor must be a
    same-origin absolute path served by this same hub process, so a hub
    on a machine with no route off localhost still renders the pane.
    """

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
    # Light default (#398), and the fallback BEFORE the link, or it would
    # out-rank the sheet at equal specificity and kill dark mode.
    assert "--bg:          #f8fafc;" in body
    assert body.index("--bg:          #f8fafc;") < body.index('href="/hub/theme.css"')


def test_page_carries_the_pieces_the_issue_asks_for():
    body = cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8")
    # Every metric, the shared ramp, and the hub chrome vocabulary.
    for metric in ("line", "branch", "toggle", "expression", "cover"):
        assert f"'{metric}'" in body, metric
    assert "hsl(var(--h), var(--tint-s), var(--cov-l))" in body
    for word in ("connected", "connecting…", "offline"):
        assert word in body, word
    # The envelope vocabulary: it registers as its own origin, handles
    # the focus, and drives the other panes.
    assert "'cov'" in body
    assert "takeover: true" in body
    assert "cov_focus" in body
    assert "source_focused" in body
    assert "open_source" in body
    assert "graph_focus" in body
    assert "selection_changed" in body
    # Empty state names a command that produces coverage, and carries the
    # one bit of artwork the artwork budget allows a pane.
    assert "rb regression --coverage-merge" in body
    assert theme.MASCOT_240 in body


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


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

        # The page itself is still 200 — its empty state is the better
        # place to say "collect some coverage" than a blank browser tab.
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


# ---------------------------------------------------------------------------
# cov_focus — the wire type
# ---------------------------------------------------------------------------


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
    """The pane must not share ``view`` or ``graph``.

    One client per origin, and the point of the pane is to drive the
    others — clicking a cold line selects the instance in the design
    view — so a shared slot would evict whichever tab was looked at
    second.
    """

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
    """``additionalProperties: false`` with no nullable hints: an unset
    hint has to be absent on the wire, not null."""

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
    """``rb hub send cov-focus`` before the tab is open still lands —
    hints and all, or a replay would silently downgrade "this branch, on
    line 4" to "this file"."""

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
