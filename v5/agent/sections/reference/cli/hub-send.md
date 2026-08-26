## hub send

```text
Usage: rtl-buddy hub send [OPTIONS] COMMAND [ARGS]...                                  
                                                                                        
 One-shot peer for the running rtl-buddy-hub. Connects as origin=cli.                   
                                                                                        
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────╮
│ select         Broadcast selection_changed{instance_path}.                           │
│ signal         Broadcast signal_selected{signal, wave_scope}.                        │
│ cursor         Broadcast cursor_time_changed{t_fs}.                                  │
│ scope          Broadcast scope_changed{wave_scope}.                                  │
│ open           Broadcast source_focused{file, line, col}.                            │
│ diagnose       Push a diagnostics_set bundle for SOURCE. Each ITEM is                │
│                <file>:<line>:<severity>:<code>:<message>. --clear sends an empty set │
│                (clears any cached diagnostics from SOURCE). Use --instance to attach │
│                a view.json instance_path hint that consumers (the SPA's on-canvas    │
│                badge layer in particular) use as a fast path instead of the          │
│                file+line resolver.                                                   │
│ state          Snapshot the hub's cached state (active model, selection, cursor,     │
│                scope, peers).                                                        │
│ wave-add       Ask the wave peer (surfer) to add one or more signals to the view.    │
│ wave-cursor    Ask the wave peer (surfer) to move its cursor to T_FS.                │
│ wave-scope     Ask the wave peer (surfer) to switch its active scope without         │
│                populating the variable panel (maps to WCP set_scope).                │
│ wave-pan       Pan surfer's viewport to center on T_FS (zoom unchanged). Maps to WCP │
│                set_viewport_to.                                                      │
│ wave-zoom      Zoom + pan surfer to fit [START_FS, END_FS]. Maps to WCP              │
│                set_viewport_range.                                                   │
│ wave-zoom-fit  Zoom surfer out to fit the whole waveform. Maps to WCP zoom_to_fit.   │
│ view-pan       Ask the view peer (SPA) to pan/center on INSTANCE_PATH.               │
│ overlay        Flip an overlay's enabled state on the SPA. Built-in NAMES are        │
│                'clock', 'reset', 'axi-perf', 'wave'; an unknown name is a no-op. Use │
│                --on / --off (default --on). Useful for agents or scripted demos that │
│                want to direct the user's attention to a specific overlay layer       │
│                without a UI click.                                                   │
│ capture        Ask the view peer (SPA) to snapshot the current graph and write it to │
│                --out. Graph-only — surrounding panels are not captured. Useful for   │
│                agents that want to look at what the user is seeing without a browser │
│                screenshot tool.                                                      │
│ open-source    Ask the src peer (nvim) to open FILE at line+col.                     │
│ resolve        resolve coordinates via the hub's view.json + tb_prefix mapping       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
