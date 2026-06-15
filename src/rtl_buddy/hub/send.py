"""``rb hub send`` — one-shot CLI for driving the hub.

Each subcommand opens an :class:`~rtl_buddy.hub.client.HubClient`
(origin ``cli``), sends one envelope, and exits. Requests block on
the response and print the payload as JSON; events fire-and-forget by
default. Exit codes follow the rest of ``rb``:

* ``0`` — success.
* ``1`` — graceful failure (hub returned ``error``, timeout, malformed
  arguments).
* ``2`` — no hub running for this project / ``$RTL_BUDDY_HUB`` unset.

Aimed at agents and scripts; users mostly drive the hub through the
SPA / surfer / nvim peers, but having a one-liner for every wire type
keeps the protocol testable from a shell prompt.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from ..logging_utils import emit_console_text
from .client import HubClient, HubClientError, HubUnavailable
from .protocol import Envelope, Kind


logger = logging.getLogger(__name__)


send_app = typer.Typer(
    help="One-shot peer for the running rtl-buddy-hub. Connects as origin=cli.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_or_exit() -> HubClient:
    """Connect or exit with the appropriate code + message."""

    try:
        return HubClient.connect()
    except HubUnavailable as exc:
        emit_console_text(str(exc), style="yellow")
        raise typer.Exit(code=2)
    except HubClientError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=1)


def _print_response(env: Envelope) -> None:
    """Render the response/error payload as pretty JSON to stdout."""

    payload = env.payload if isinstance(env.payload, dict) else {}
    if env.kind is Kind.ERROR:
        emit_console_text(
            f"hub error: {payload.get('code')}: {payload.get('message')}",
            style="red",
        )
        if payload.get("context"):
            sys.stdout.write(json.dumps(payload.get("context"), indent=2) + "\n")
        raise typer.Exit(code=1)
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


_FILE_LINE_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?$")
"""Parses ``path/to/file.sv:42`` and ``path/to/file.sv:42:5``.

Greedy ``.+?`` plus anchored ``\\d+`` for ``line`` lets Windows-style
``C:\\path:42`` parse correctly — the colon after the drive letter
isn't followed by a pure-digit ``line``."""


def _parse_file_line(spec: str) -> tuple[str, int, int]:
    """``path/to/file.sv:42[:5]`` → ``("path/to/file.sv", 42, 5)``."""

    m = _FILE_LINE_RE.match(spec)
    if m is None:
        raise typer.BadParameter(
            f"expected file:line[:col], got {spec!r}",
            param_hint="<file>:<line>[:<col>]",
        )
    return m.group("file"), int(m.group("line")), int(m.group("col") or 1)


def _parse_diag(spec: str) -> dict[str, object]:
    """``file:line:severity:code:message...`` → diagnostic item dict.

    Splits on the first four colons so the message can contain colons
    freely (column attribution lives in ``rb hub send open`` instead).
    """

    parts = spec.split(":", 4)
    if len(parts) != 5:
        raise typer.BadParameter(
            f"expected <file>:<line>:<severity>:<code>:<message>, got {spec!r}",
            param_hint="diagnostic item",
        )
    file, line_s, severity, code, message = parts
    try:
        line = int(line_s)
    except ValueError as exc:
        raise typer.BadParameter(
            f"non-integer line in {spec!r}: {exc}",
            param_hint="diagnostic item",
        )
    if severity not in {"error", "warning", "info", "hint"}:
        raise typer.BadParameter(
            f"severity must be one of error/warning/info/hint, got {severity!r}",
            param_hint="diagnostic item",
        )
    return {
        "file": file,
        "line": line,
        "col": 1,
        "severity": severity,
        "code": code,
        "message": message,
    }


# ---------------------------------------------------------------------------
# state events (broadcast)
# ---------------------------------------------------------------------------


@send_app.command("select", help="Broadcast selection_changed{instance_path}.")
def cmd_select(
    instance_path: Annotated[
        str, typer.Argument(help="view.json instance_path, e.g. top.u_fifo.u_wr_ptr")
    ],
) -> None:
    with _open_or_exit() as h:
        h.emit("selection_changed", {"instance_path": instance_path})


@send_app.command("signal", help="Broadcast signal_selected{signal, wave_scope}.")
def cmd_signal(
    signal: Annotated[str, typer.Argument(help="signal name, e.g. wr_ptr_q")],
    wave_scope: Annotated[
        str,
        typer.Option(
            "--wave-scope",
            help="surfer/VCD scope owning the signal, e.g. tb.dut.u_fifo",
        ),
    ],
) -> None:
    with _open_or_exit() as h:
        h.emit("signal_selected", {"signal": signal, "wave_scope": wave_scope})


@send_app.command("cursor", help="Broadcast cursor_time_changed{t_fs}.")
def cmd_cursor(
    t_fs: Annotated[
        int,
        typer.Argument(help="cursor time in femtoseconds (decimal integer)"),
    ],
) -> None:
    if t_fs < 0:
        raise typer.BadParameter(f"t_fs must be non-negative, got {t_fs}")
    with _open_or_exit() as h:
        h.emit("cursor_time_changed", {"t_fs": str(t_fs)})


@send_app.command("scope", help="Broadcast scope_changed{wave_scope}.")
def cmd_scope(
    wave_scope: Annotated[
        str, typer.Argument(help="surfer/VCD scope, e.g. tb.dut.u_fifo")
    ],
) -> None:
    with _open_or_exit() as h:
        h.emit("scope_changed", {"wave_scope": wave_scope})


@send_app.command("open", help="Broadcast source_focused{file, line, col}.")
def cmd_open(
    spec: Annotated[
        str,
        typer.Argument(help="file:line[:col], e.g. design/dma/dma.sv:42:7"),
    ],
) -> None:
    file, line, col = _parse_file_line(spec)
    with _open_or_exit() as h:
        h.emit("source_focused", {"file": file, "line": line, "col": col})


@send_app.command(
    "diagnose",
    help=(
        "Push a diagnostics_set bundle for SOURCE. Each ITEM is "
        "<file>:<line>:<severity>:<code>:<message>. --clear sends an empty "
        "set (clears any cached diagnostics from SOURCE). Use "
        "--instance to attach a view.json instance_path hint that consumers "
        "(the SPA's on-canvas badge layer in particular) use as a fast path "
        "instead of the file+line resolver."
    ),
)
def cmd_diagnose(
    source: Annotated[
        str,
        typer.Argument(
            help="producer key (e.g. 'rtl-buddy-cdc', 'claude-analysis'); "
            "latest-writer-wins per source on the hub's cache"
        ),
    ],
    items: Annotated[
        Optional[list[str]],
        typer.Argument(help="<file>:<line>:<sev>:<code>:<msg> ...", show_default=False),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option("--clear", help="Send an empty items list (clears SOURCE)."),
    ] = False,
    instance_path: Annotated[
        Optional[str],
        typer.Option(
            "--instance",
            help=(
                "Optional view.json instance_path to attach to every ITEM in "
                "this push. Use when the producer knows which instance a finding "
                "pertains to (most one-shot agent calls do); skip for batch "
                "lint output where each item lives at a different file:line."
            ),
        ),
    ] = None,
) -> None:
    if clear and items:
        raise typer.BadParameter("--clear is incompatible with item arguments")
    if not clear and not items:
        raise typer.BadParameter("provide at least one ITEM, or pass --clear")
    if clear and instance_path:
        raise typer.BadParameter("--instance has no effect with --clear")
    parsed_items: list[dict[str, object]] = (
        [] if clear else [_parse_diag(s) for s in (items or [])]
    )
    if instance_path:
        for it in parsed_items:
            it["instance_path"] = instance_path
    with _open_or_exit() as h:
        h.emit("diagnostics_set", {"source": source, "items": parsed_items})


# ---------------------------------------------------------------------------
# hub-handled requests
# ---------------------------------------------------------------------------


@send_app.command(
    "state",
    help="Snapshot the hub's cached state (active model, selection, cursor, scope, peers).",
)
def cmd_state() -> None:
    with _open_or_exit() as h:
        _print_response(h.request("state_snapshot", {}))


resolve_app = typer.Typer(
    help="resolve coordinates via the hub's view.json + tb_prefix mapping",
    no_args_is_help=True,
)
send_app.add_typer(resolve_app, name="resolve")


@resolve_app.command("view-to-wave", help="instance_path → wave_scope")
def cmd_resolve_view_to_wave(
    instance_path: Annotated[str, typer.Argument(help="view.json instance_path")],
) -> None:
    with _open_or_exit() as h:
        _print_response(
            h.request("resolve_view_to_wave", {"instance_path": instance_path})
        )


@resolve_app.command("wave-to-view", help="wave_scope → instance_path")
def cmd_resolve_wave_to_view(
    wave_scope: Annotated[str, typer.Argument(help="surfer/VCD wave_scope")],
) -> None:
    with _open_or_exit() as h:
        _print_response(h.request("resolve_wave_to_view", {"wave_scope": wave_scope}))


@resolve_app.command(
    "signal-to-view",
    help="signal + wave_scope → driver instance_path(s) and driven port",
)
def cmd_resolve_signal_to_view(
    signal: Annotated[str, typer.Argument(help="signal name (e.g. wr_ptr_q)")],
    wave_scope: Annotated[
        str, typer.Option("--wave-scope", help="enclosing wave scope")
    ],
) -> None:
    with _open_or_exit() as h:
        _print_response(
            h.request(
                "resolve_signal_to_view",
                {"signal": signal, "wave_scope": wave_scope},
            )
        )


# ---------------------------------------------------------------------------
# peer-routed requests
# ---------------------------------------------------------------------------


@send_app.command(
    "wave-add",
    help="Ask the wave peer (surfer) to add one or more signals to the view.",
)
def cmd_wave_add(
    variables: Annotated[
        list[str],
        typer.Argument(help="fully-scoped variable names, e.g. tb.dut.u_fifo.wr_ptr_q"),
    ],
) -> None:
    if not variables:
        raise typer.BadParameter("at least one variable required")
    with _open_or_exit() as h:
        _print_response(h.request("wave_add_variables", {"variables": list(variables)}))


@send_app.command(
    "wave-cursor", help="Ask the wave peer (surfer) to move its cursor to T_FS."
)
def cmd_wave_cursor(
    t_fs: Annotated[
        int,
        typer.Argument(help="cursor time in femtoseconds (decimal integer)"),
    ],
) -> None:
    if t_fs < 0:
        raise typer.BadParameter(f"t_fs must be non-negative, got {t_fs}")
    with _open_or_exit() as h:
        _print_response(h.request("wave_set_cursor", {"t_fs": str(t_fs)}))


@send_app.command(
    "wave-scope",
    help="Ask the wave peer (surfer) to switch its active scope without populating the variable panel (maps to WCP set_scope).",
)
def cmd_wave_scope(
    wave_scope: Annotated[str, typer.Argument(help="surfer/VCD scope")],
) -> None:
    with _open_or_exit() as h:
        _print_response(h.request("wave_set_scope", {"wave_scope": wave_scope}))


@send_app.command(
    "wave-pan",
    help="Pan surfer's viewport to center on T_FS (zoom unchanged). Maps to WCP set_viewport_to.",
)
def cmd_wave_pan(
    t_fs: Annotated[
        int,
        typer.Argument(help="center time in femtoseconds"),
    ],
) -> None:
    if t_fs < 0:
        raise typer.BadParameter(f"t_fs must be non-negative, got {t_fs}")
    with _open_or_exit() as h:
        _print_response(h.request("wave_set_viewport", {"t_fs": str(t_fs)}))


@send_app.command(
    "wave-zoom",
    help="Zoom + pan surfer to fit [START_FS, END_FS]. Maps to WCP set_viewport_range.",
)
def cmd_wave_zoom(
    start_fs: Annotated[int, typer.Argument(help="range start in femtoseconds")],
    end_fs: Annotated[int, typer.Argument(help="range end in femtoseconds")],
) -> None:
    if start_fs < 0 or end_fs < 0:
        raise typer.BadParameter("start_fs/end_fs must be non-negative")
    if end_fs <= start_fs:
        raise typer.BadParameter(f"end_fs ({end_fs}) must be > start_fs ({start_fs})")
    with _open_or_exit() as h:
        _print_response(
            h.request(
                "wave_zoom_to_range",
                {"start_fs": str(start_fs), "end_fs": str(end_fs)},
            )
        )


@send_app.command(
    "wave-zoom-fit",
    help="Zoom surfer out to fit the whole waveform. Maps to WCP zoom_to_fit.",
)
def cmd_wave_zoom_fit() -> None:
    with _open_or_exit() as h:
        _print_response(h.request("wave_zoom_to_fit", {}))


@send_app.command(
    "wave-items",
    help="List the items currently in surfer's wave view (id, type, name). Maps to WCP get_item_list + get_item_info.",
)
def cmd_wave_items() -> None:
    with _open_or_exit() as h:
        _print_response(h.request("wave_get_items", {}))


@send_app.command(
    "wave-remove",
    help="Ask the wave peer (surfer) to remove items by id. IDs come from wave-add / wave-items. Reports removed vs not_found.",
)
def cmd_wave_remove(
    ids: Annotated[
        list[int],
        typer.Argument(help="DisplayedItemRef ids to remove, e.g. 3 5 7"),
    ],
) -> None:
    if not ids:
        raise typer.BadParameter("at least one id required")
    if any(i < 0 for i in ids):
        raise typer.BadParameter("ids must be non-negative")
    with _open_or_exit() as h:
        _print_response(h.request("wave_remove_items", {"ids": list(ids)}))


@send_app.command(
    "wave-move",
    help=(
        "Reorder items in surfer's view. Move the given IDS (in the order "
        "listed) so the block starts at --to INDEX, or just before --before "
        "ID. Exactly one of --to / --before is required."
    ),
)
def cmd_wave_move(
    ids: Annotated[
        list[int],
        typer.Argument(help="DisplayedItemRef ids to move, e.g. 5 6"),
    ],
    to_index: Annotated[
        Optional[int],
        typer.Option("--to", help="target visible index (0 = top of view)", min=0),
    ] = None,
    before: Annotated[
        Optional[int],
        typer.Option(
            "--before",
            help="move the block to just before this item id (resolved via wave-items)",
            min=0,
        ),
    ] = None,
) -> None:
    if not ids:
        raise typer.BadParameter("at least one id required")
    if any(i < 0 for i in ids):
        raise typer.BadParameter("ids must be non-negative")
    if (to_index is None) == (before is None):
        raise typer.BadParameter("pass exactly one of --to / --before")
    with _open_or_exit() as h:
        target = to_index
        if before is not None:
            # Resolve the target id to a visible index via a live item-list
            # snapshot, then move the block to sit at that slot.
            env = h.request("wave_get_items", {})
            if env.kind is Kind.ERROR:
                return _print_response(env)
            body = env.payload if isinstance(env.payload, dict) else {}
            raw_items = body.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            index = next(
                (
                    i
                    for i, it in enumerate(items)
                    if isinstance(it, dict) and it.get("id") == before
                ),
                None,
            )
            if index is None:
                emit_console_text(
                    f"--before id {before} is not in the current view", style="red"
                )
                raise typer.Exit(code=1)
            target = index
        _print_response(
            h.request("wave_move_items", {"ids": list(ids), "to_index": target})
        )


@send_app.command(
    "wave-comment",
    help="Add comment rows (named dividers) to surfer's view. Returns the new item ids. Maps to WCP add_dividers.",
)
def cmd_wave_comment(
    texts: Annotated[
        list[str],
        typer.Argument(help="comment labels, one divider per entry"),
    ],
    after: Annotated[
        Optional[int],
        typer.Option(
            "--after",
            help="insert the comments after this item id (default: end of view)",
            min=0,
        ),
    ] = None,
) -> None:
    if not texts:
        raise typer.BadParameter("at least one comment text required")
    if any(not t.strip() for t in texts):
        raise typer.BadParameter("comment text must be non-empty")
    payload: dict[str, object] = {"texts": list(texts)}
    if after is not None:
        payload["after_id"] = after
    with _open_or_exit() as h:
        _print_response(h.request("wave_add_comments", payload))


@send_app.command(
    "view-pan",
    help="Ask the view peer (SPA) to pan/center on INSTANCE_PATH.",
)
def cmd_view_pan(
    instance_path: Annotated[str, typer.Argument(help="view.json instance_path")],
) -> None:
    with _open_or_exit() as h:
        _print_response(h.request("view_pan_to", {"instance_path": instance_path}))


@send_app.command(
    "overlay",
    help=(
        "Flip an overlay's enabled state on the SPA. Built-in NAMES "
        "are 'clock', 'reset', 'axi-perf', 'wave'; an unknown name is "
        "a no-op. Use --on / --off (default --on). Useful for agents "
        "or scripted demos that want to direct the user's attention "
        "to a specific overlay layer without a UI click."
    ),
)
def cmd_view_overlay(
    name: Annotated[str, typer.Argument(help="overlay name")],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Enable (default) or disable the named overlay.",
        ),
    ] = True,
) -> None:
    with _open_or_exit() as h:
        _print_response(h.request("view_overlay_set", {"name": name, "enabled": on}))


@send_app.command(
    "capture",
    help=(
        "Ask the view peer (SPA) to snapshot the current graph and "
        "write it to --out. Graph-only — surrounding panels are not "
        "captured. Useful for agents that want to look at what the "
        "user is seeing without a browser screenshot tool."
    ),
)
def cmd_capture(
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            "-o",
            help="Destination file. Extension determines format if --format omitted.",
            resolve_path=True,
        ),
    ],
    format: Annotated[
        Optional[str],
        typer.Option(
            "--format",
            "-f",
            help="png (default) or svg. Inferred from --out suffix if not given.",
            case_sensitive=False,
        ),
    ] = None,
    scale: Annotated[
        float,
        typer.Option(
            "--scale",
            help="PNG upscale factor (1.0 = native). Ignored for SVG.",
            min=0.1,
            max=8.0,
        ),
    ] = 1.0,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Seconds to wait for the SPA to reply. Large designs may need longer.",
            min=1.0,
            max=120.0,
        ),
    ] = 15.0,
) -> None:
    fmt = (format or out.suffix.lstrip(".")).lower()
    if fmt not in {"png", "svg"}:
        raise typer.BadParameter(
            f"format must be png or svg, got {fmt!r} (from {'--format' if format else '--out suffix'})"
        )
    payload: dict[str, object] = {"format": fmt}
    if fmt == "png" and scale != 1.0:
        payload["scale"] = scale
    with _open_or_exit() as h:
        try:
            env = h.request("view_capture", payload, timeout=timeout)
        except TimeoutError as exc:
            emit_console_text(str(exc), style="red")
            raise typer.Exit(code=1)
    if env.kind is Kind.ERROR:
        p = env.payload if isinstance(env.payload, dict) else {}
        emit_console_text(
            f"hub error: {p.get('code')}: {p.get('message')}",
            style="red",
        )
        raise typer.Exit(code=1)
    body = env.payload if isinstance(env.payload, dict) else {}
    b64 = body.get("bytes_b64")
    if not isinstance(b64, str) or not b64:
        emit_console_text("view returned no image bytes", style="red")
        raise typer.Exit(code=1)
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError) as exc:
        emit_console_text(f"invalid base64 from view: {exc}", style="red")
        raise typer.Exit(code=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    width = body.get("width")
    height = body.get("height")
    dims = (
        f" {int(width)}x{int(height)}"
        if isinstance(width, (int, float)) and isinstance(height, (int, float))
        else ""
    )
    emit_console_text(
        f"wrote {fmt}{dims} → {out} ({len(data):,} bytes)",
        style="green",
    )


@send_app.command(
    "open-source",
    help="Ask the src peer (nvim) to open FILE at line+col.",
)
def cmd_open_source(
    spec: Annotated[
        str,
        typer.Argument(help="file:line[:col], e.g. design/dma/dma.sv:42:7"),
    ],
) -> None:
    file, line, col = _parse_file_line(spec)
    with _open_or_exit() as h:
        _print_response(
            h.request("open_source", {"file": file, "line": line, "col": col})
        )


__all__ = ["send_app"]
