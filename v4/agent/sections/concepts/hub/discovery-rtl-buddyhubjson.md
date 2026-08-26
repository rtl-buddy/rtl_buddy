## Discovery (`.rtl-buddy/hub.json`)

When the hub binds, it writes a small JSON record under the project root's `.rtl-buddy/` directory:

```json
{
  "pid": 41231,
  "listen_port": 53201,
  "http_port": 53202,
  "started_at": "2026-05-19T12:34:56Z",
  "project_root": "/path/to/project",
  "active_model": "ip_demo_tiny_npu"
}
```

`active_model` is optional — present when the hub started with `--model NAME` or after a `GET /view.json?model=` switch.

Peers (the viewer SPA, the `rb wave` bridge, the nvim plugin) read this file to find the hub. The hub deletes the record on clean shutdown; a stale record after a crash is detected by `rb hub status` (PID not live) and the next `rb hub start` overwrites it.

Override discovery resolution with the `RTL_BUDDY_HUB` environment variable when running outside a project tree — it should point at a `hub.json` directly.
