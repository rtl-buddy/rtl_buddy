## hub start

```text
Usage: rtl-buddy hub start [OPTIONS]

 start the rtl-buddy-hub daemon for this project

╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --foreground       --daemon                                    Run in the foreground │
│                                                                (default). --daemon   │
│                                                                detaches the hub,     │
│                                                                logs to hub.log, and  │
│                                                                returns once          │
│                                                                .rtl-buddy/hub.json   │
│                                                                is published.         │
│                                                                [default: foreground] │
│ --serve-viewer     --no-serve-viewer                           Also serve the viewer │
│                                                                over HTTP and         │
│                                                                WebSocket. Without    │
│                                                                --viewer-bundle, uses │
│                                                                the SPA from an       │
│                                                                installed             │
│                                                                rtl-buddy-view, else  │
│                                                                a placeholder page.   │
│                                                                [default:             │
│                                                                no-serve-viewer]      │
│ --viewer-bundle                         PATH                   SPA to serve instead  │
│                                                                of the installed one: │
│                                                                a directory           │
│                                                                containing            │
│                                                                index.html, or an     │
│                                                                index.html path. Only │
│                                                                used with             │
│                                                                --serve-viewer.       │
│ --listen-port                           INTEGER RANGE          TCP port for adapter  │
│                                         [0<=x<=65535]          peers (nvim, rb       │
│                                                                wave). Overrides      │
│                                                                .listen_port in       │
│                                                                hub.toml. 0 =         │
│                                                                OS-assigned.          │
│ --http-port                             INTEGER RANGE          HTTP/WebSocket port   │
│                                         [0<=x<=65535]          for the browser SPA.  │
│                                                                Overrides .http_port  │
│                                                                in hub.toml. 0 =      │
│                                                                OS-assigned. Only     │
│                                                                used with             │
│                                                                --serve-viewer.       │
│ --model                                 TEXT                   Model name (from      │
│                                                                models.yaml) to       │
│                                                                generate view.json    │
│                                                                for at start, instead │
│                                                                of running `rb hier`. │
│                                                                Without it the hub    │
│                                                                uses .view_json from  │
│                                                                hub.toml. Requires    │
│                                                                --serve-viewer.       │
│ --models-file                           PATH                   models.yaml that      │
│                                                                holds the --model     │
│                                                                entry, skipping       │
│                                                                discovery. Use it     │
│                                                                when several          │
│                                                                models.yaml files     │
│                                                                define the same name. │
│ --axi-perf-from                         PATH                   axi-perf.json from    │
│                                                                `rb axi-profile run`, │
│                                                                whose throughput      │
│                                                                overlay is added to   │
│                                                                every generated       │
│                                                                view.json. The layout │
│                                                                <suite>/artefacts/ax… │
│                                                                also lets the SPA     │
│                                                                'Open in marimo'      │
│                                                                button skip its       │
│                                                                prompt. Only used     │
│                                                                with --serve-viewer.  │
│ --help                                                         Show this message and │
│                                                                exit.                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
