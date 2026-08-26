## hub start

```text
Usage: rtl-buddy hub start [OPTIONS]                                                   
                                                                                        
 start the rtl-buddy-hub daemon for this project                                        
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --foreground       --daemon                                    Run in the foreground │
│                                                                (default).            │
│                                                                [default: foreground] │
│ --serve-viewer     --no-serve-viewer                           Also serve the viewer │
│                                                                HTTP+WebSocket layer  │
│                                                                at the http_port.     │
│                                                                When no               │
│                                                                --viewer-bundle is    │
│                                                                given, the hub        │
│                                                                auto-discovers the    │
│                                                                SPA shipped by        │
│                                                                rtl-buddy-view (if    │
│                                                                installed) and falls  │
│                                                                back to a placeholder │
│                                                                page if neither is    │
│                                                                available.            │
│                                                                [default:             │
│                                                                no-serve-viewer]      │
│ --viewer-bundle                         PATH                   Override the          │
│                                                                auto-discovered SPA   │
│                                                                with this path        │
│                                                                (directory containing │
│                                                                index.html, or a path │
│                                                                to a single           │
│                                                                index.html). Use this │
│                                                                when iterating on the │
│                                                                SPA from a checkout — │
│                                                                the auto-discovered   │
│                                                                bundle ships with the │
│                                                                installed wheel and   │
│                                                                won't reflect         │
│                                                                uncommitted viewer/   │
│                                                                changes. Only used    │
│                                                                with --serve-viewer.  │
│ --listen-port                           INTEGER RANGE          TCP port for adapter  │
│                                         [0<=x<=65535]          peers (nvim, rb       │
│                                                                wave). Overrides      │
│                                                                .listen_port from     │
│                                                                hub.toml. 0 =         │
│                                                                OS-assigned. Pin to a │
│                                                                specific number so    │
│                                                                peers' discovery      │
│                                                                records stay stable   │
│                                                                across restarts.      │
│ --http-port                             INTEGER RANGE          HTTP/WS port for the  │
│                                         [0<=x<=65535]          browser-side SPA.     │
│                                                                Overrides .http_port  │
│                                                                from hub.toml. 0 =    │
│                                                                OS-assigned. Pin to a │
│                                                                specific number so    │
│                                                                the SPA URL stays the │
│                                                                same across restarts. │
│                                                                Only used with        │
│                                                                --serve-viewer.       │
│ --model                                 TEXT                   Generate view.json on │
│                                                                hub start for this    │
│                                                                model name (looked up │
│                                                                in models.yaml).      │
│                                                                Replaces the legacy   │
│                                                                workflow of running   │
│                                                                `rb hier <model>      │
│                                                                --format json -o      │
│                                                                .rtl-buddy/view.json` │
│                                                                manually before each  │
│                                                                hub start. When unset │
│                                                                the hub falls back to │
│                                                                .view_json from       │
│                                                                hub.toml. Requires    │
│                                                                --serve-viewer.       │
│ --models-file                           PATH                   Explicit models.yaml  │
│                                                                that owns the --model │
│                                                                entry. Skips the      │
│                                                                project-tree          │
│                                                                discovery walk. Use   │
│                                                                this to disambiguate  │
│                                                                when the same model   │
│                                                                name exists in more   │
│                                                                than one models.yaml. │
│ --axi-perf-from                         PATH                   Path to an            │
│                                                                axi-perf.json (output │
│                                                                of `rb axi-profile    │
│                                                                run`). The hub bakes  │
│                                                                its                   │
│                                                                per-bundle/interconn… │
│                                                                throughput overlay    │
│                                                                into every generated  │
│                                                                view.json AND records │
│                                                                the source's          │
│                                                                test/suite_dir so the │
│                                                                SPA's 'Open in        │
│                                                                marimo' button skips  │
│                                                                its prompt. Use the   │
│                                                                canonical             │
│                                                                <suite>/artefacts/ax… │
│                                                                layout so the         │
│                                                                test/suite_dir        │
│                                                                derivation lands.     │
│                                                                Only used with        │
│                                                                --serve-viewer.       │
│ --help                                                         Show this message and │
│                                                                exit.                 │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
