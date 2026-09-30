## Quick start

Start the hub from the project root:

```bash
uv run rb hub start --serve-viewer
```

Open the printed `http://127.0.0.1:<http_port>/` URL. The landing page links the apps:

| Route | App |
| --- | --- |
| `/sch` | Interactive schematic |
| `/gph` | Design knowledge graph |
| `/cov` | Coverage browser |
| `/phy` | Synthesis area and power browser |

From a second shell, inspect or stop the process:

```bash
uv run rb hub status
uv run rb hub log --follow
uv run rb hub stop
```

`rb hub start` stays in the foreground by default. `--daemon` detaches and logs to `.rtl-buddy/hub.log`; an early failure returns non-zero with the log tail.

The schematic needs `rtl-buddy-sch`, and live wave integration needs the rtl-buddy Surfer fork. See [Installation](https://rtl-buddy.github.io/rtl_buddy/v6/install/#external-tools-by-feature) and [Waveform Viewer](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/wave/).
