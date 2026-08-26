## Troubleshooting

**`rb hub start` exits with "already running"** — `.rtl-buddy/hub.json` exists and its PID is live. If the prior daemon really is gone, the file is stale (clean shutdown didn't run); delete it and retry. `rb hub status` distinguishes the two cases.

**Port already in use** — pin `listen_port` (and `http_port` if using `--serve-viewer`) to a free port in `hub.toml`, or leave them at `0` to let the OS pick. The chosen port lands in `hub.json` either way.

**Peer can't find the hub from outside the project tree** — set `RTL_BUDDY_HUB=/path/to/.rtl-buddy/hub.json` in the peer's environment. The default discovery walks up from `cwd` looking for `.rtl-buddy/hub.json`, which doesn't work for processes launched from elsewhere.

**`rb wave` bridge reports surfer disconnected** — the bridge owns the WCP TCP connection, not the hub. Check the surfer fork is on PATH and built with WCP support; see [Waveform Viewer](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/wave/). The hub stays up regardless; reconnect is automatic.

**`rb hub config validate` reports "unknown section"** — typo. The schema accepts exactly `[hub]` and `[mapping]`; everything else (surfer flags, nvim keymaps, …) belongs in the adapters' own config.

**Hub log empty or absent** — `rb hub log` tails `.rtl-buddy/hub.log` by default. The `[hub].log_path` setting controls the location; logs route through `log_event()` like the rest of `rtl_buddy`, so `--machine` mode produces JSON Lines.
