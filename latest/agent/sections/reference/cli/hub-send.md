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
│ graph-focus    Broadcast graph_focus{node} — point the hub's design knowledge graph  │
│                pane (http://127.0.0.1:<http_port>/gph) at one node of                │
│                artefacts/graph/graph.json. NODE is a graph node id: 'module:fifo',   │
│                'inst:top/top.u_fifo', 'test:verif/dma#smoke',                        │
│                'covitem:dma#DMA-COV-1' — the vocabulary `rb graph query` returns and │
│                docs/concepts/graph.md lists. The hub caches the focus and replays it │
│                to the pane on connect, so sending this before the browser tab is     │
│                open works.                                                           │
│ cov-focus      Broadcast cov_focus{target} — point the hub's coverage pane           │
│                (http://127.0.0.1:<http_port>/cov) at one target of the run's         │
│                coverage model. TARGET is prefixed: 'file:design/blk.sv',             │
│                'module:blk', or 'test:verif/blk#basic'; an unprefixed string is read │
│                as a file path. --metric foregrounds one coverage kind, --line        │
│                scrolls a file target to a line, and --item names a                   │
│                branch/toggle/expression bin or an SVA cover point. The hub caches    │
│                the focus and replays it to the pane on connect, so sending this      │
│                before the browser tab is open works.                                 │
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
│ wave-items     List the items currently in surfer's wave view (id, type, name). Maps │
│                to WCP get_item_list + get_item_info.                                 │
│ wave-remove    Ask the wave peer (surfer) to remove items by id. IDs come from       │
│                wave-add / wave-items. Reports removed vs not_found.                  │
│ wave-move      Reorder items in surfer's view. Move the given IDS (in the order      │
│                listed) so the block starts at --to INDEX, or just before --before    │
│                ID. Exactly one of --to / --before is required.                       │
│ wave-comment   Add comment rows (named dividers) to surfer's view. Returns the new   │
│                item ids. Maps to WCP add_dividers.                                   │
│ view-pan       Ask the schematic (rtl-buddy-sch) to pan/center on INSTANCE_PATH.     │
│ overlay        Flip an overlay's enabled state on the SPA. Built-in NAMES are        │
│                'clock', 'reset', 'axi-perf', 'wave'; an unknown name is a no-op. Use │
│                --on / --off (default --on). Useful for agents or scripted demos that │
│                want to direct the user's attention to a specific overlay layer       │
│                without a UI click.                                                   │
│ capture        Ask the schematic (rtl-buddy-sch) to snapshot the current graph and   │
│                write it to --out. Graph-only — surrounding panels are not captured.  │
│                Useful for agents that want to look at what the user is seeing        │
│                without a browser screenshot tool.                                    │
│ open-source    Ask the src peer (nvim) to open FILE at line+col.                     │
│ resolve        resolve coordinates via the hub's view.json + tb_prefix mapping       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
