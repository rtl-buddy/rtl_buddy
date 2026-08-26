## Discovery (`.rtl-buddy/hub.json`)

When the hub binds, it writes a small JSON record under the project root's `.rtl-buddy/` directory:

```json
{
  "v": 1,
  "pid": 41231,
  "tcp": "127.0.0.1:53201",
  "server_version": "0.5.0",
  "project_root": "/path/to/project",
  "started_at": "2026-05-19T12:34:56+00:00",
  "http_port": 53202,
  "active_model": "ip_demo_tiny_npu"
}
```

The TCP listener address is the single `tcp` `host:port` string (there is no `listen_port` field). `v` is the discovery-schema version and `server_version` is the hub build. `http_port` is present only when the hub was started with `--serve-viewer`; `active_model` is present when the hub started with `--model NAME` or after a `GET /view.json?model=` switch (both optional keys are omitted when unset).

Peers (the viewer SPA, the `rb wave` bridge, the nvim plugin) read this file to find the hub. The hub deletes the record on clean shutdown; a stale record after a crash is detected by `rb hub status` (PID not live) and the next `rb hub start` overwrites it.

Override discovery resolution with the `RTL_BUDDY_HUB` environment variable when running outside a project tree — set it to the hub's `host:port` (the `tcp` value from `hub.json`, e.g. `RTL_BUDDY_HUB=127.0.0.1:53201`), **not** a path to a file.
