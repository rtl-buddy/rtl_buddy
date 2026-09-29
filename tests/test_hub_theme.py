"""Tests for the hub's shared token sheet and brand assets.

They guard against palette blocks drifting apart (the checked-in file must equal the generator output), a token the apps consume disappearing, and routes that break same-origin loading or read outside their directory.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from rtl_buddy.hub import cov_page, graph_page, landing_page, theme
from rtl_buddy.hub.server import HubServer
from rtl_buddy.hub.viewer_http import PLACEHOLDER_HTML, ViewerServer


def test_generated_blocks_are_up_to_date():
    """``theme.css`` equals what ``python -m rtl_buddy.hub.theme`` writes, so a hand-written ``[data-theme]`` block fails."""

    source = theme.read_theme_source()
    assert theme.build_theme_css(source) == source


def test_generation_is_idempotent():
    """Running the generator on its own output changes nothing."""

    once = theme.build_theme_css(theme.read_theme_source())
    assert theme.build_theme_css(once) == once


def test_override_blocks_cover_every_dark_token():
    """The light and dark override blocks have the same key set, so a light pin beats the media query and a dark pin beats the ``:root`` defaults."""

    css = theme.THEME_CSS
    light, dark = theme.parse_palettes(css.split(theme.GENERATED_MARKER)[0])
    light_by_name = dict(light)
    generated = css.split(theme.GENERATED_MARKER)[1]
    light_block = generated.split(':root[data-theme="light"] {')[1].split("}")[0]
    dark_block = generated.split(':root[data-theme="dark"] {')[1].split("}")[0]

    assert "color-scheme: light;" in light_block
    assert "color-scheme: dark;" in dark_block
    for name, dark_value in dark:
        assert f"{name}:" in light_block, name
        assert f"{name}:" in dark_block, name
        assert dark_value in dark_block, name
        assert light_by_name[name] in light_block, name


def test_regenerate_entry_point_is_source_checkout_only():
    """``python -m rtl_buddy.hub.theme`` rewrites the sheet in place, which is only safe in a git checkout; the entry-point guard must pass in the source tree."""

    assert theme.in_source_checkout() is True


def test_theme_tokens_the_apps_consume_exist():
    """The token names the apps consume exist; renaming one also changes the schematic SPA."""

    css = theme.THEME_CSS
    for token in (
        # surfaces / text / accent
        "--bg",
        "--panel",
        "--panel-2",
        "--line",
        "--line-strong",
        "--fg",
        "--fg-muted",
        "--fg-faint",
        "--accent",
        "--accent-contrast",
        # status + banner tints
        "--ok",
        "--warn",
        "--err",
        "--info",
        "--ok-bg",
        "--warn-bg",
        "--err-bg",
        # coverage ramp
        "--cov-l",
        "--cov-none",
        "--cov-0",
        "--cov-50",
        "--cov-100",
        # brand (identity marks only)
        "--brand-ink",
        "--brand-green",
        "--brand-red",
        # type / shape / elevation
        "--font-mono",
        "--font-sans",
        "--fs-base",
        "--fs-small",
        "--radius-1",
        "--radius-2",
        "--radius-3",
        "--shadow-1",
        "--shadow-2",
    ):
        assert f"{token}:" in css, token
    for column in graph_page.COLUMN_ORDER:
        assert f"--col-{column}:" in css, column


def test_light_is_the_default():
    """``:root`` is light; dark comes only from the media query."""

    light, dark = theme.parse_palettes(theme.THEME_CSS.split(theme.GENERATED_MARKER)[0])
    assert dict(light)["--bg"] == "#f8fafc"
    assert dict(dark)["--bg"] == "#0f1115"
    assert "@media (prefers-color-scheme: dark)" in theme.THEME_CSS
    assert "@media (prefers-color-scheme: light)" not in theme.THEME_CSS


def test_brand_colours_are_never_the_accent_or_a_status():
    """The brand green fails as text on white, so brand colours are never the accent or a status colour."""

    light, _dark = theme.parse_palettes(
        theme.THEME_CSS.split(theme.GENERATED_MARKER)[0]
    )
    values = dict(light)
    brand = {values["--brand-ink"], values["--brand-green"], values["--brand-red"]}
    for token in ("--accent", "--ok", "--warn", "--err", "--info"):
        assert values[token] not in brand, token


def test_vendored_assets_are_present_and_small():
    """Assets are vendored so panes stay same-origin, and small so identity marks stay cheap."""

    names = theme.asset_names()
    for name in (theme.FAVICON_16, theme.FAVICON_32, theme.LOGO_80, theme.MASCOT_240):
        assert name in names, name
        body = theme.asset_bytes(name)
        assert body is not None and body[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(body) < 32 * 1024, (name, len(body))


def test_asset_lookup_refuses_anything_not_shipped():
    assert theme.asset_bytes("nope.png") is None
    assert theme.asset_bytes("../theme.css") is None
    assert theme.asset_bytes("../../__init__.py") is None


def test_every_hub_page_links_the_favicon():
    """Landing, graph pane, coverage pane, and the no-bundle placeholder link the favicon."""

    for page in _hub_pages().values():
        assert theme.FAVICON_16 in page
        assert theme.FAVICON_32 in page
        assert theme.THEME_CSS_ROUTE in page


# Hub pages repeat a few token values inline as a fallback for a 404ing sheet. The fallback must not shadow the sheet (`:root` ties, so document order decides) and must not go stale.

_STYLE_RE = re.compile(r"<style>(?P<body>.*?)</style>", re.S)
_PAGE_ROOT_BLOCK_RE = re.compile(r":root \{(?P<body>[^{}]*)\}")
_PAGE_DECL_RE = re.compile(r"(?P<name>--[a-z0-9-]+)\s*:\s*(?P<value>[^;{}]+);")
_VAR_FALLBACK_RE = re.compile(
    r"var\(\s*(?P<name>--[a-z0-9-]+)\s*,\s*(?P<value>[^(),]+)\)"
)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}$")

PAGE_NAMES = ("landing", "graph", "cov", "placeholder")


def _hub_pages() -> dict[str, str]:
    """Return every HTML document the hub serves, rendered."""

    return {
        "landing": landing_page.render_landing_html(hub_addr="127.0.0.1:1").decode(
            "utf-8"
        ),
        "graph": graph_page.render_graph_html(hub_addr="127.0.0.1:1").decode("utf-8"),
        "cov": cov_page.render_cov_html(hub_addr="127.0.0.1:1").decode("utf-8"),
        "placeholder": PLACEHOLDER_HTML,
    }


def _stylesheet_link() -> str:
    return f'<link rel="stylesheet" href="{theme.THEME_CSS_ROUTE}">'


def _css(fragment: str) -> str:
    """Return the CSS in a fragment of HTML, comments stripped."""

    css = "\n".join(m.group("body") for m in _STYLE_RE.finditer(fragment))
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _light_palette() -> dict[str, str]:
    light, _dark = theme.parse_palettes(
        theme.THEME_CSS.split(theme.GENERATED_MARKER)[0]
    )
    return {name: " ".join(value.split()) for name, value in light}


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_inline_fallbacks_never_shadow_the_sheet(name: str):
    """No ``:root`` block may re-declare a sheet token after the link.

    A later inline ``:root`` block beats ``theme.css``, including its dark media query, while ``data-theme`` keeps working. Page-local tokens the sheet does not define are fine.
    """

    page = _hub_pages()[name]
    link = _stylesheet_link()
    assert link in page, name
    shared = _light_palette()
    for block in _PAGE_ROOT_BLOCK_RE.finditer(_css(page.split(link, 1)[1])):
        for decl in _PAGE_DECL_RE.finditer(block.group("body")):
            token = decl.group("name")
            assert token not in shared, (
                f"{name}: `{token}` is re-declared in a :root block after the "
                f"{theme.THEME_CSS_ROUTE} link, so the inline value beats the "
                "sheet and prefers-color-scheme dark never applies"
            )


@pytest.mark.parametrize("name", PAGE_NAMES)
def test_inline_fallback_values_match_the_sheet(name: str):
    """Inline fallbacks must match the sheet.

    ``:root`` fallback blocks match exactly. For ``var(--token, fallback)`` only colours are pinned; generic fallbacks like ``var(--font-sans, system-ui)`` are not copies.
    """

    page = _hub_pages()[name]
    shared = _light_palette()
    checked = 0
    for block in _PAGE_ROOT_BLOCK_RE.finditer(_css(page)):
        for decl in _PAGE_DECL_RE.finditer(block.group("body")):
            token = decl.group("name")
            if token not in shared:
                continue
            value = " ".join(decl.group("value").split())
            assert value == shared[token], f"{name}: {token} is stale"
            checked += 1
    for var in _VAR_FALLBACK_RE.finditer(_css(page)):
        token, value = var.group("name"), var.group("value").strip()
        if token not in shared or not _HEX_RE.match(value):
            continue
        assert value == shared[token], f"{name}: var({token}) fallback is stale"
        checked += 1
    assert checked, f"{name}: no inline fallback found to check"


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
async def test_theme_css_route(hub_and_viewer: ViewerServer):
    url = f"http://127.0.0.1:{hub_and_viewer.http_port}{theme.THEME_CSS_ROUTE}"
    status, headers, body = await asyncio.to_thread(_http_get, url)
    assert status == 200
    assert "text/css" in headers.get("Content-Type", "")
    # Same cache policy as every other hub body: pane HTML is read once at import, so a stale browser-cached sheet would outlive the hub.
    assert headers.get("Cache-Control") == "no-store"
    assert body == theme.theme_css_bytes()


@pytest.mark.asyncio
async def test_assets_route_serves_the_marks(hub_and_viewer: ViewerServer):
    base = f"http://127.0.0.1:{hub_and_viewer.http_port}{theme.ASSETS_ROUTE_PREFIX}"
    for name in theme.asset_names():
        status, headers, body = await asyncio.to_thread(_http_get, base + name)
        assert status == 200
        assert headers.get("Content-Type") == "image/png"
        assert headers.get("Cache-Control") == "no-store"
        assert body == theme.asset_bytes(name)


@pytest.mark.asyncio
async def test_assets_route_404s_unknown_and_traversal(hub_and_viewer: ViewerServer):
    base = f"http://127.0.0.1:{hub_and_viewer.http_port}{theme.ASSETS_ROUTE_PREFIX}"
    for name in ("nope.png", "..%2Ftheme.css", "../theme.css"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            await asyncio.to_thread(_http_get, base + name)
        assert excinfo.value.code == 404
