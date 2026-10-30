## hub send graph-focus

```text
Usage: rtl-buddy hub send graph-focus [OPTIONS] NODE

 Broadcast graph_focus{node}: point the graph pane (/gph) at one node. NODE is a graph
 node id as returned by `rb graph query`, such as 'module:fifo', 'inst:top/top.u_fifo',
 'test:verif/dma#smoke' or 'covitem:dma#DMA-COV-1'. The hub replays the focus when the
 pane connects, so it can be sent before the tab is open.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    node      TEXT  graph node id, e.g. test:verif/dma#smoke [required]             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
