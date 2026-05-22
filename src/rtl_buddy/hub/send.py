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

import json
import logging
import re
import sys
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
        "set (clears any cached diagnostics from SOURCE)."
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
) -> None:
    if clear and items:
        raise typer.BadParameter("--clear is incompatible with item arguments")
    if not clear and not items:
        raise typer.BadParameter("provide at least one ITEM, or pass --clear")
    parsed_items: list[dict[str, object]] = (
        [] if clear else [_parse_diag(s) for s in (items or [])]
    )
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
    help="Ask the wave peer (surfer) to switch its scope (maps to WCP add_scope in v1).",
)
def cmd_wave_scope(
    wave_scope: Annotated[str, typer.Argument(help="surfer/VCD scope")],
) -> None:
    with _open_or_exit() as h:
        _print_response(h.request("wave_set_scope", {"wave_scope": wave_scope}))


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
