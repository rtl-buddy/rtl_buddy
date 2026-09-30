## Troubleshooting

- **Already running:** run `rb hub status`. Stop the live process, or remove `.rtl-buddy/hub.json` only if the recorded PID is stale.
- **Port in use:** choose a free fixed port in `hub.toml`, override it on the command line, or use `0` for OS assignment.
- **Peer cannot discover the hub:** set `RTL_BUDDY_HUB` to the `tcp` address in `hub.json`.
- **Wave bridge disconnected:** check that the supported Surfer fork is running with WCP enabled. The hub keeps running while the bridge reconnects.
- **Empty hub log:** foreground mode logs to the terminal; `--daemon` and the LaunchAgent write to the log file.
- **Viewer placeholder:** install `rtl-buddy-sch`, or pass `--viewer-bundle PATH` for a development SPA build.
- **`/graph.json` or `/phy.json` returns 404:** run the command the hint names (`rb graph build`, `rb synth`, `rb power`).
