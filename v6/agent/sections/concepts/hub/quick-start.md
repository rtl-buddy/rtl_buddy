## Quick start

Start the hub from the project root:

```bash
uv run rb hub start --serve-viewer
```

Open the printed `http://127.0.0.1:<http_port>/` URL. The landing page links the available apps:

| Route | App |
| --- | --- |
| `/sch` | Interactive schematic. |
| `/gph` | Design knowledge graph. |
| `/cov` | Coverage browser. |

Use a second shell to inspect or stop the process:

```bash
uv run rb hub status
uv run rb hub log --follow
uv run rb hub stop
```

`rb hub start` stays in the foreground by default. Add `--daemon` to detach and log to `.rtl-buddy/hub.log`. Startup waits until the detached process publishes discovery; an early failure returns non-zero with the log tail.

The hub itself has no external binary dependency. The schematic needs `rtl-buddy-sch`, and live wave integration needs the rtl-buddy Surfer fork. See [Installation](https://rtl-buddy.github.io/rtl_buddy/v6/install/#external-tools-by-feature) and [Waveform Viewer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/wave/).
