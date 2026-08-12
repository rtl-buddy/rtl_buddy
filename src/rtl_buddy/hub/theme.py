# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""The hub's shared design tokens + brand assets (rtl-buddy/rtl_buddy#398).

Every hub app — the landing page, the graph pane, the view SPA, the
coverage pane — reads its surfaces, text tiers, accent, status colours,
column hues and type scale from ONE sheet, served same-origin at
:data:`THEME_CSS_ROUTE`. Same-origin is what keeps a pane "self-contained"
in the sense that matters here: the hub is routinely run on machines with
no route off localhost, so a stylesheet is allowed to be a second request
but never a second *host*.

Two halves:

* :data:`THEME_CSS` — ``theme.css``, read once at import (the same rule
  the graph pane's HTML follows). Its ``:root`` block is light, its
  ``@media (prefers-color-scheme: dark)`` block is dark, and the
  ``:root[data-theme="light"|"dark"]`` blocks that let an app pin a theme
  in *both* directions are GENERATED from those two by
  :func:`build_theme_css` — three hand-maintained copies of the same
  palette is three chances to drift. ``python -m rtl_buddy.hub.theme``
  rewrites the generated tail; ``tests/test_hub_theme.py`` fails when the
  checked-in file is not what the generator would write.
* :func:`asset_bytes` — the vendored brand marks under ``assets/``.
  Vendored rather than fetched because the art repo is private and the
  panes must stay same-origin; downscaled to a few kB each so the whole
  identity set costs less than one web font would.
"""

from __future__ import annotations

import re
from pathlib import Path


#: Route serving the token sheet.
THEME_CSS_ROUTE = "/hub/theme.css"

#: Prefix for the vendored brand marks (``/hub/assets/<name>``).
ASSETS_ROUTE_PREFIX = "/hub/assets/"

#: Favicon every hub page links, and the landing page's ~40px chip logo.
FAVICON_16 = "rtl-buddy-favicon-16.png"
FAVICON_32 = "rtl-buddy-favicon-32.png"
LOGO_80 = "rtl-buddy-logo-80.png"
MASCOT_240 = "rtl-buddy-mascot-240.png"

_ASSETS_DIR = Path(__file__).parent / "assets"
_THEME_CSS_PATH = Path(__file__).parent / "theme.css"

#: Everything from this line to EOF in ``theme.css`` is generated.
GENERATED_MARKER = (
    "/* ==== generated below this line — "
    "`python -m rtl_buddy.hub.theme` rewrites it ==== */"
)

_ROOT_BLOCK_RE = re.compile(r"^:root \{\n(?P<body>.*?)^\}", re.S | re.M)
_DARK_BLOCK_RE = re.compile(
    r"^@media \(prefers-color-scheme: dark\) \{\n(?P<body>.*?)^\}", re.S | re.M
)
_DECL_RE = re.compile(r"^\s*(?P<name>--[a-z0-9-]+):\s*(?P<value>[^;]+);", re.M)


def _declarations(css: str) -> list[tuple[str, str]]:
    """``[(--token, value)]`` in source order for one CSS block body."""

    return [(m.group("name"), m.group("value").strip()) for m in _DECL_RE.finditer(css)]


def parse_palettes(source: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(light, dark)`` declaration lists from the authored part of the sheet.

    ``light`` is the whole ``:root`` block; ``dark`` is only what the
    media query *overrides* — which is exactly the set of tokens the two
    generated blocks have to re-state, and why the generator does not
    need to know anything else about the palette.
    """

    root = _ROOT_BLOCK_RE.search(source)
    if root is None:
        raise ValueError("theme.css: no top-level `:root {` block")
    dark_media = _DARK_BLOCK_RE.search(source)
    if dark_media is None:
        raise ValueError("theme.css: no `@media (prefers-color-scheme: dark)` block")
    return _declarations(root.group("body")), _declarations(dark_media.group("body"))


def _block(selector: str, scheme: str, decls: list[tuple[str, str]]) -> str:
    lines = [f"{selector} {{", f"  color-scheme: {scheme};"]
    width = max((len(name) for name, _ in decls), default=0) + 1
    lines += [f"  {(name + ':'):<{width + 1}} {value};" for name, value in decls]
    lines.append("}")
    return "\n".join(lines)


def build_theme_css(source: str) -> str:
    """The full sheet: the authored part, then the generated overrides.

    ``source`` may be the authored part alone or a previously-generated
    file — everything from :data:`GENERATED_MARKER` on is discarded and
    rebuilt, so running this on its own output is a no-op.

    Only the tokens the dark media query overrides are re-stated. A
    ``[data-theme="light"]`` block has to beat the media query, so it
    needs the light value of every token dark changes and nothing else;
    a ``[data-theme="dark"]`` block has to beat the ``:root`` defaults,
    so it needs the dark values, which is the same key set. Tokens that
    are theme-independent (type, shape, brand) live in ``:root`` alone
    and are never copied.
    """

    authored = source.split(GENERATED_MARKER)[0].rstrip("\n")
    light, dark = parse_palettes(authored)
    light_by_name = dict(light)
    overridden = [name for name, _ in dark]
    missing = [name for name in overridden if name not in light_by_name]
    if missing:
        raise ValueError(
            "theme.css: dark block overrides tokens absent from :root: "
            + ", ".join(missing)
        )
    light_overrides = [(name, light_by_name[name]) for name in overridden]
    return "\n".join(
        [
            authored,
            "",
            GENERATED_MARKER,
            "/* An app that pins its theme (a ?theme= param, a toggle, a host",
            " * page) sets data-theme on <html>; these two blocks make that",
            " * attribute win in BOTH directions — a light pin has to beat the",
            " * dark media query, a dark pin has to beat the :root defaults.",
            " * Generated from the two blocks above so the palette exists once. */",
            "",
            _block(':root[data-theme="light"]', "light", light_overrides),
            "",
            _block(':root[data-theme="dark"]', "dark", dark),
            "",
        ]
    )


def read_theme_source() -> str:
    """The checked-in ``theme.css`` as text."""

    return _THEME_CSS_PATH.read_text(encoding="utf-8")


def in_source_checkout() -> bool:
    """Whether ``theme.css`` sits inside a git working tree.

    The regenerate entry point is meaningful only in a source checkout;
    against an installed wheel it would rewrite (or fail to rewrite)
    ``site-packages`` in place.
    """

    return any(
        (parent / ".git").exists() for parent in _THEME_CSS_PATH.resolve().parents
    )


def write_theme_css() -> Path:
    """Regenerate ``theme.css`` in place; return its path."""

    _THEME_CSS_PATH.write_text(build_theme_css(read_theme_source()), encoding="utf-8")
    return _THEME_CSS_PATH


THEME_CSS: str = read_theme_source()
"""The sheet, read once at import — panes are static assets, not hot-reloaded."""


def theme_css_bytes() -> bytes:
    """Body for ``GET /hub/theme.css``."""

    return THEME_CSS.encode("utf-8")


def asset_names() -> list[str]:
    """The vendored brand marks, sorted."""

    return sorted(p.name for p in _ASSETS_DIR.glob("*.png"))


def asset_bytes(name: str) -> bytes | None:
    """Body for ``GET /hub/assets/<name>``; ``None`` when there is no such mark.

    ``name`` is client-supplied, so it is matched against the directory
    listing rather than joined onto a path — the route can never be
    talked into reading a file outside ``assets/``.
    """

    if name not in asset_names():
        return None
    return (_ASSETS_DIR / name).read_bytes()


def favicon_link_tags(prefix: str = ASSETS_ROUTE_PREFIX) -> str:
    """The ``<link rel="icon">`` pair every hub page carries."""

    return (
        f'<link rel="icon" type="image/png" sizes="32x32" href="{prefix}{FAVICON_32}">\n'
        f'  <link rel="icon" type="image/png" sizes="16x16" href="{prefix}{FAVICON_16}">'
    )


def main() -> None:  # pragma: no cover - developer entry point
    if not in_source_checkout():
        raise SystemExit(
            "theme.css is not inside a git working tree — refusing to rewrite an "
            "installed copy. Run this from a source checkout."
        )
    path = write_theme_css()
    print(f"regenerated {path}")


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "ASSETS_ROUTE_PREFIX",
    "FAVICON_16",
    "FAVICON_32",
    "GENERATED_MARKER",
    "LOGO_80",
    "MASCOT_240",
    "THEME_CSS",
    "THEME_CSS_ROUTE",
    "asset_bytes",
    "asset_names",
    "build_theme_css",
    "favicon_link_tags",
    "in_source_checkout",
    "parse_palettes",
    "read_theme_source",
    "theme_css_bytes",
    "write_theme_css",
]
