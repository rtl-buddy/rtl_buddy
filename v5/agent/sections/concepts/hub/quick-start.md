## Quick start

```bash
cd <project_root>
uv run rb hub start                   # foreground TCP server only
uv run rb hub start --serve-viewer    # also expose the viewer HTTP+WS endpoint
uv run rb hub status                  # in another shell: who's connected
uv run rb hub stop                    # graceful shutdown via SIGTERM
```

`rb hub start` runs in the foreground by default; backgrounding is the caller's job (`nohup rb hub start &`, a process manager, or — on macOS — the bundled LaunchAgent: see [`rb hub install-launchagent`](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/hub/#auto-start-on-macos-launchagent)). The server binds the OS-assigned port (TCP, and HTTP if `--serve-viewer` is set) unless `hub.toml` pins them; the resolved TCP address (and HTTP port, with `--serve-viewer`) is written to `.rtl-buddy/hub.json` so peers can discover it.
