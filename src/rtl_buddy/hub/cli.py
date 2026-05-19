"""``rb hub`` Typer subcommand surface.

Phase 10b ships in slices; this module covers the CLI plumbing for all
of the user-facing entry points so the muscle memory locks in early.
The server-side commands (``start``, ``stop``) currently exercise only
the discovery + config layers — the asyncio server lands in PR 2.

Command summary (per §4.1 of the protocol spec):

* ``rb hub start``    — bind, write ``.rtl-buddy/hub.json``, run loop.
* ``rb hub stop``     — SIGTERM the PID in ``hub.json``.
* ``rb hub status``   — print the current discovery record + liveness.
* ``rb hub log``      — tail ``.rtl-buddy/hub.log``.
* ``rb hub config validate`` — schema-check ``.rtl-buddy/hub.toml``.
"""

from __future__ import annotations

import logging
import os
import time
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer
from typing_extensions import Annotated

from ..config.root import discover_project_root
from ..errors import FatalRtlBuddyError
from ..logging_utils import emit_console_text, log_event
from . import config as hub_config
from . import discovery
from . import loop as hub_loop


logger = logging.getLogger(__name__)


app = typer.Typer(help="manage the rtl-buddy-hub daemon", no_args_is_help=True)
config_app = typer.Typer(help="hub.toml utilities", no_args_is_help=True)
app.add_typer(config_app, name="config")


def _resolve_project_root() -> Path:
    """Find the rtl-buddy project root for the current invocation.

    The hub is strictly per-project (§4.1 / §4.7). Failing here with a
    pointer at ``rb`` discovery rules is more useful than a downstream
    "no such file" error from the discovery layer.
    """

    try:
        return discover_project_root()
    except FatalRtlBuddyError as exc:
        raise FatalRtlBuddyError(
            f"{exc} The hub is per-project: run `rb hub ...` from inside a project tree."
        ) from exc


def _resolve_config(project_root: Path) -> hub_config.HubConfig:
    path = hub_config.default_config_path(project_root)
    return hub_config.load_hub_config(path if path.exists() else None)


@app.command("start", help="start the rtl-buddy-hub daemon for this project")
def cmd_start(
    foreground: Annotated[
        bool,
        typer.Option("--foreground/--daemon", help="Run in the foreground (default)."),
    ] = True,
    serve_viewer: Annotated[
        bool,
        typer.Option(
            "--serve-viewer/--no-serve-viewer",
            help=(
                "Also serve the viewer HTTP+WebSocket layer at the http_port. "
                "When no --viewer-bundle is given, a placeholder page proves "
                "the transport works."
            ),
        ),
    ] = False,
    viewer_bundle: Annotated[
        Path | None,
        typer.Option(
            "--viewer-bundle",
            help=(
                "Path to a rtl-buddy-view SPA build (directory containing "
                "index.html, or a path to a single index.html). Only used "
                "with --serve-viewer."
            ),
        ),
    ] = None,
) -> None:
    """Bind, write ``hub.json``, run the server loop.

    Preflight (project root, config, conflict check) runs before the
    asyncio loop starts so a misconfigured project fails immediately
    rather than hanging in an event loop. The loop exits cleanly on
    SIGINT / SIGTERM / ``rb hub stop`` and removes its discovery file.
    """

    if not foreground:
        emit_console_text(
            "rb hub start --daemon: background detach not implemented yet; "
            "wrap with `nohup rb hub start &` or use a process manager. "
            "Running in foreground.",
            style="yellow",
        )

    if viewer_bundle is not None and not serve_viewer:
        emit_console_text(
            "rb hub start --viewer-bundle: ignored without --serve-viewer.",
            style="yellow",
        )

    project_root = _resolve_project_root()
    cfg = _resolve_config(project_root)

    existing = discovery.read_record(project_root)
    if existing is not None and discovery._pid_is_live(existing.pid):  # noqa: SLF001
        raise discovery.HubAlreadyRunningError(
            existing.pid, discovery.discovery_path(project_root)
        )

    log_event(
        logger,
        logging.INFO,
        "hub.start.preflight_ok",
        project_root=str(project_root),
        listen_port=cfg.hub.listen_port,
        http_port=cfg.hub.http_port,
        foreground=foreground,
        serve_viewer=serve_viewer,
        viewer_bundle=str(viewer_bundle) if viewer_bundle else "",
    )
    raise typer.Exit(
        code=hub_loop.serve(
            project_root,
            cfg,
            serve_viewer=serve_viewer,
            viewer_bundle=viewer_bundle,
        )
    )


@app.command("stop", help="ask the running hub to shut down")
def cmd_stop() -> None:
    project_root = _resolve_project_root()
    record = discovery.read_record(project_root)
    if record is None:
        emit_console_text(
            f"no hub running for {project_root} (no .rtl-buddy/hub.json).",
            style="yellow",
        )
        raise typer.Exit(code=1)
    if not discovery._pid_is_live(record.pid):  # noqa: SLF001
        emit_console_text(
            f"hub.json points at pid {record.pid} but that process is not running; "
            "removing the stale file.",
            style="yellow",
        )
        discovery.delete_record_if_owner(project_root, expected_pid=record.pid)
        raise typer.Exit(code=1)

    discovery.signal_process(record.pid)
    log_event(
        logger,
        logging.INFO,
        "hub.stop.signal_sent",
        pid=record.pid,
        project_root=str(project_root),
    )
    emit_console_text(f"sent SIGTERM to hub pid {record.pid}.")


@app.command("status", help="print the running hub's discovery record")
def cmd_status() -> None:
    project_root = _resolve_project_root()
    record = discovery.read_record(project_root)
    if record is None:
        emit_console_text(f"no hub running for {project_root}.", style="yellow")
        raise typer.Exit(code=1)

    live = discovery._pid_is_live(record.pid)  # noqa: SLF001
    state = "RUNNING" if live else "STALE (pid not alive)"
    style = "green" if live else "red"

    emit_console_text(f"hub {state}", style=style)
    emit_console_text(f"  project_root   : {record.project_root}")
    emit_console_text(f"  pid            : {record.pid}")
    emit_console_text(f"  tcp            : {record.tcp}")
    if record.http_port is not None:
        emit_console_text(f"  viewer_url     : http://127.0.0.1:{record.http_port}/")
    emit_console_text(f"  server_version : {record.server_version}")
    emit_console_text(f"  started_at     : {record.started_at}")
    if not live:
        raise typer.Exit(code=1)


@app.command("log", help="tail the hub log")
def cmd_log(
    lines: Annotated[
        int,
        typer.Option("-n", "--lines", help="Trailing lines to print before following."),
    ] = 50,
    follow: Annotated[
        bool,
        typer.Option("-f/--no-follow", help="Follow the log (tail -f)."),
    ] = False,
) -> None:
    project_root = _resolve_project_root()
    cfg = _resolve_config(project_root)
    log_path = (project_root / cfg.hub.log_path).resolve()
    if not log_path.exists():
        emit_console_text(f"log not found: {log_path}", style="yellow")
        raise typer.Exit(code=1)

    _tail(log_path, lines=lines)
    if follow:
        _follow(log_path)


def _tail(path: Path, *, lines: int) -> None:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        tail = fh.readlines()[-lines:]
    for line in tail:
        emit_console_text(line.rstrip("\n"))


def _follow(path: Path) -> None:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                emit_console_text(line.rstrip("\n"))
        except KeyboardInterrupt:
            return


@config_app.command("validate", help="schema-check .rtl-buddy/hub.toml")
def cmd_config_validate(
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Override the default project hub.toml path."),
    ] = None,
) -> None:
    if path is None:
        project_root = _resolve_project_root()
        path = hub_config.default_config_path(project_root)

    if not path.exists():
        emit_console_text(
            f"{path} does not exist — defaults will be used at startup.",
            style="yellow",
        )
        return

    try:
        cfg = hub_config.load_hub_config(path)
    except hub_config.HubConfigError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=1)

    emit_console_text(f"ok: {path}")
    emit_console_text(f"  listen_port    : {cfg.hub.listen_port}")
    emit_console_text(f"  http_port      : {cfg.hub.http_port}")
    emit_console_text(f"  log_path       : {cfg.hub.log_path}")
    emit_console_text(f"  tb_prefix      : {cfg.mapping.tb_prefix!r}")
    emit_console_text(f"  signal_aliases : {len(cfg.mapping.signal_aliases)}")


def _package_version() -> str:
    try:
        return _pkg_version("rtl-buddy")
    except Exception:
        return "0.0.0+unknown"


__all__ = ["app"]
