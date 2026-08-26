## Driving the hub from the CLI (`rb hub send`)

`rb hub send` is a one-shot peer: it connects to the running hub as `origin=cli`, sends one request or state event, prints any reply, and disconnects. It exits with code 2 when no hub is running (or `$RTL_BUDDY_HUB` is unset). It is the scripting/automation entry point and the easiest way to poke the hub by hand.

The verbs group into broadcast, wave-control, SPA, source, and resolve families (see the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v5/reference/cli/#hub-send) for the full flag list of each):

- **State broadcast:** `select INSTANCE_PATH`, `signal SIGNAL`, `cursor T_FS`, `scope WAVE_SCOPE`, `open FILE:LINE[:COL]`.
- **Wave control** (routed to surfer via the `rb wave` bridge): `wave-add VARIABLES…`, `wave-cursor T_FS`, `wave-scope WAVE_SCOPE`, `wave-pan T_FS`, `wave-zoom START_FS END_FS`, `wave-zoom-fit`.
- **SPA:** `view-pan INSTANCE_PATH`, `overlay NAME --on/--off` (`clock` / `reset` / `axi-perf` / `wave`), `capture --out PATH [--format png|svg] [--scale …]`.
- **Source:** `open-source FILE:LINE[:COL]`.
- **Diagnostics:** `diagnose SOURCE ITEM…` (each `ITEM` is `file:line:severity:code:message`; `--clear`, `--instance`).
- **State / resolve:** `state` (snapshot of active model / selection / cursor / scope / peers), and `resolve {view-to-wave|wave-to-view|signal-to-view}`.
