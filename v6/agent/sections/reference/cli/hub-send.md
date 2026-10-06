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
│ graph-focus    Broadcast graph_focus{node}: point the graph pane (/gph) at one node. │
│                NODE is a graph node id as returned by `rb graph query`, such as      │
│                'module:fifo', 'inst:top/top.u_fifo', 'test:verif/dma#smoke' or       │
│                'covitem:dma#DMA-COV-1'. The hub replays the focus when the pane      │
│                connects, so it can be sent before the tab is open.                   │
│ cov-focus      Broadcast cov_focus{target}: point the coverage pane (/cov) at one    │
│                target. TARGET is 'file:design/blk.sv', 'module:blk' or               │
│                'test:verif/blk#basic'; an unprefixed string is a file path. --metric │
│                foregrounds one coverage kind, --line scrolls a file target to a      │
│                line, and --item names a bin or SVA cover point. --by switches the    │
│                pane's figures between per-elaboration and source points. The hub     │
│                replays the focus when the pane connects, so it can be sent before    │
│                the tab is open.                                                      │
│ phys-focus     Broadcast phys_focus{target}: point the synth and power pane (/phy)   │
│                at one target. TARGET is 'instance:u_cpu/u_alu' or 'module:alu'; an   │
│                unprefixed string is an instance path. --metric foregrounds one       │
│                physical metric. The graph pane (/gph) also follows: it turns on its  │
│                heat overlay and highlights the target's module. The hub replays the  │
│                focus when either pane connects, so it can be sent before the tabs    │
│                are open.                                                             │
│ diagnose       Push a diagnostics_set bundle for SOURCE. Each ITEM is                │
│                <file>:<line>:<severity>:<code>:<message>. --clear sends an empty     │
│                set, clearing SOURCE's diagnostics. --instance attaches a view.json   │
│                instance_path so consumers skip file-and-line resolution.             │
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
│ overlay        Enable or disable an overlay on the schematic. NAME is 'clock',       │
│                'reset', 'axi-perf' or 'wave'; an unknown name does nothing.          │
│ capture        Ask the schematic (rtl-buddy-sch) to snapshot the current graph and   │
│                write it to --out. Only the graph is captured, not the surrounding    │
│                panels.                                                               │
│ open-source    Ask the src peer (nvim) to open FILE at line+col.                     │
│ resolve        resolve coordinates via the hub's view.json + tb_prefix mapping       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
