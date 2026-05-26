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
from . import launchagent
from . import loop as hub_loop
from . import model_discovery as hub_model_discovery
from . import status_client
from . import view_builder as hub_view_builder


logger = logging.getLogger(__name__)


app = typer.Typer(help="manage the rtl-buddy-hub daemon", no_args_is_help=True)
config_app = typer.Typer(help="hub.toml utilities", no_args_is_help=True)
app.add_typer(config_app, name="config")

from . import send as _send  # noqa: E402 — keep imports clustered after `app`

app.add_typer(_send.send_app, name="send")


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
                "When no --viewer-bundle is given, the hub auto-discovers the "
                "SPA shipped by rtl-buddy-view (if installed) and falls back "
                "to a placeholder page if neither is available."
            ),
        ),
    ] = False,
    viewer_bundle: Annotated[
        Path | None,
        typer.Option(
            "--viewer-bundle",
            help=(
                "Override the auto-discovered SPA with this path (directory "
                "containing index.html, or a path to a single index.html). "
                "Use this when iterating on the SPA from a checkout — the "
                "auto-discovered bundle ships with the installed wheel and "
                "won't reflect uncommitted viewer/ changes. Only used with "
                "--serve-viewer."
            ),
        ),
    ] = None,
    listen_port: Annotated[
        int | None,
        typer.Option(
            "--listen-port",
            help=(
                "TCP port for adapter peers (nvim, rb wave). Overrides "
                "[hub].listen_port from hub.toml. 0 = OS-assigned. Pin to a "
                "specific number so peers' discovery records stay stable "
                "across restarts."
            ),
            min=0,
            max=65535,
        ),
    ] = None,
    http_port: Annotated[
        int | None,
        typer.Option(
            "--http-port",
            help=(
                "HTTP/WS port for the browser-side SPA. Overrides "
                "[hub].http_port from hub.toml. 0 = OS-assigned. Pin to a "
                "specific number so the SPA URL stays the same across "
                "restarts. Only used with --serve-viewer."
            ),
            min=0,
            max=65535,
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Generate view.json on hub start for this model name (looked "
                "up in models.yaml). Replaces the legacy workflow of running "
                "`rb hier <model> --format json -o .rtl-buddy/view.json` "
                "manually before each hub start. When unset the hub falls "
                "back to [mapping].view_json from hub.toml. Requires "
                "--serve-viewer."
            ),
        ),
    ] = None,
    models_file: Annotated[
        Path | None,
        typer.Option(
            "--models-file",
            help=(
                "Explicit models.yaml that owns the --model entry. Skips the "
                "project-tree discovery walk. Use this to disambiguate when "
                "the same model name exists in more than one models.yaml."
            ),
        ),
    ] = None,
    axi_perf_from: Annotated[
        Path | None,
        typer.Option(
            "--axi-perf-from",
            help=(
                "Path to an axi-perf.json (output of `rb axi-profile run`). "
                "The hub bakes its per-bundle/interconnect throughput overlay "
                "into every generated view.json AND records the source's "
                "test/suite_dir so the SPA's 'Open in marimo' button skips "
                "its prompt. Use the canonical "
                "<suite>/artefacts/axi/<test>/axi-perf.json layout so the "
                "test/suite_dir derivation lands. Only used with "
                "--serve-viewer."
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
    if model is not None and not serve_viewer:
        # The view.json is only served by the viewer HTTP layer;
        # generating it without --serve-viewer would silently
        # discard the work. Fail loud instead.
        raise FatalRtlBuddyError(
            "rb hub start --model: requires --serve-viewer "
            "(the generated view.json is only served via the SPA HTTP layer)."
        )
    if models_file is not None and model is None:
        emit_console_text(
            "rb hub start --models-file: ignored without --model.",
            style="yellow",
        )

    project_root = _resolve_project_root()
    cfg = _resolve_config(project_root)

    # CLI flags override hub.toml — the user typed them on this invocation
    # and we should trust that over the on-disk default. Frozen config so
    # we rebuild via dataclasses.replace.
    if listen_port is not None or http_port is not None:
        import dataclasses

        cfg = dataclasses.replace(
            cfg,
            hub=dataclasses.replace(
                cfg.hub,
                listen_port=listen_port
                if listen_port is not None
                else cfg.hub.listen_port,
                http_port=http_port if http_port is not None else cfg.hub.http_port,
            ),
        )

    existing = discovery.read_record(project_root)
    if existing is not None and discovery._pid_is_live(existing.pid):  # noqa: SLF001
        raise discovery.HubAlreadyRunningError(
            existing.pid, discovery.discovery_path(project_root)
        )

    # When --model is given, resolve and generate the view.json
    # before the asyncio loop starts so a missing model or a tool
    # failure surfaces synchronously with a clear error, not as a
    # 404 the user discovers in the browser later.
    # Up-front existence check on --axi-perf-from so the user gets a
    # clear error before the hub binds its sockets, not later on the
    # first SPA refresh.
    if axi_perf_from is not None and not axi_perf_from.is_file():
        emit_console_text(
            f"--axi-perf-from: file not found: {axi_perf_from}", style="red"
        )
        raise typer.Exit(code=2)

    view_json_override: Path | None = None
    if model is not None:
        _models_yaml, loader = hub_model_discovery.resolve_model(
            project_root, model, models_file=models_file
        )
        model_cfg = loader.get_model(model)
        view_json_override = hub_view_builder.build_view_json(
            project_root=project_root,
            model_cfg=model_cfg,
            axi_perf_source=axi_perf_from,
        )
        emit_console_text(
            f"rb hub: generated view.json for {model!r} at {view_json_override}",
            style="green",
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
        model=model or "",
        axi_perf_from=str(axi_perf_from) if axi_perf_from else "",
    )
    raise typer.Exit(
        code=hub_loop.serve(
            project_root,
            cfg,
            serve_viewer=serve_viewer,
            viewer_bundle=viewer_bundle,
            view_json_override=view_json_override,
            initial_model=model,
            models_file_pin=models_file,
            axi_perf_source=axi_perf_from,
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
    if record.active_model is not None:
        emit_console_text(f"  active_model   : {record.active_model}")
    emit_console_text(f"  server_version : {record.server_version}")
    emit_console_text(f"  started_at     : {record.started_at}")

    if not live:
        raise typer.Exit(code=1)

    # Live hub: query the registry over TCP and render per-peer state.
    # A connect / hello failure is a peer-level note, not a fatal — the
    # hub may still be running but mid-shutdown, or have just accepted
    # another CLI client (origin-cli dedup, §3.2). The user sees the
    # underlying error verbatim either way.
    host, _, port_str = record.tcp.rpartition(":")
    try:
        port = int(port_str)
    except ValueError:
        emit_console_text(
            f"  peers          : (unparseable tcp address {record.tcp!r})",
            style="yellow",
        )
        return

    emit_console_text("  peers")
    try:
        registered = status_client.query_registered_origins_sync(host, port)
    except status_client.HubStatusQueryError as exc:
        emit_console_text(f"    (query failed: {exc})", style="yellow")
        return

    registered_set = {o for o in registered if o != "cli"}
    for origin in status_client.DISPLAY_ORIGINS:
        if origin in registered_set:
            emit_console_text(f"    {origin:<6} CONNECTED", style="green")
        else:
            emit_console_text(f"    {origin:<6} not connected", style="yellow")


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


@app.command(
    "install-launchagent",
    help="install the macOS LaunchAgent so the hub auto-starts at login",
)
def cmd_install_launchagent() -> None:
    """Render and ``launchctl load`` the user-level LaunchAgent.

    Runs from the current project root — the agent is project-scoped,
    so multiple projects each install their own agent under a unique
    plist path. Re-run after moving the project; the old plist needs
    a manual ``rb hub uninstall-launchagent`` from the prior location.
    """
    try:
        project_root = _resolve_project_root()
    except FatalRtlBuddyError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=2)
    try:
        target = launchagent.install(project_root=project_root)
    except launchagent.LaunchAgentUnsupportedError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=2)
    except launchagent.LaunchAgentError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=1)
    emit_console_text(f"LaunchAgent installed at {target}", style="green")
    emit_console_text("  agent: " + launchagent.LABEL)
    emit_console_text(f"  project_root: {project_root}")
    emit_console_text(
        "The hub will auto-start at next login and stay up across crashes. "
        "Use `rb hub stop` for a one-off shutdown; the agent will restart it. "
        "Use `rb hub uninstall-launchagent` to remove."
    )


@app.command(
    "uninstall-launchagent",
    help="remove the macOS LaunchAgent",
)
def cmd_uninstall_launchagent() -> None:
    """``launchctl unload`` and delete the plist."""
    try:
        removed = launchagent.uninstall()
    except launchagent.LaunchAgentUnsupportedError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=2)
    except launchagent.LaunchAgentError as exc:
        emit_console_text(str(exc), style="red")
        raise typer.Exit(code=1)
    if removed:
        emit_console_text(
            f"LaunchAgent removed from {launchagent.default_plist_path()}",
            style="green",
        )
    else:
        emit_console_text(
            f"no LaunchAgent installed at {launchagent.default_plist_path()}",
            style="yellow",
        )


def _package_version() -> str:
    try:
        return _pkg_version("rtl-buddy")
    except Exception:
        return "0.0.0+unknown"


__all__ = ["app"]
