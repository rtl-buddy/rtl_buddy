## hub send graph-focus

```text
Usage: rtl-buddy hub send graph-focus [OPTIONS] NODE

 Broadcast graph_focus{node} — point the hub's design knowledge graph pane
 (http://127.0.0.1:<http_port>/gph) at one node of artefacts/graph/graph.json. NODE is
 a graph node id: 'module:fifo', 'inst:top/top.u_fifo', 'test:verif/dma#smoke',
 'covitem:dma#DMA-COV-1' — the vocabulary `rb graph query` returns and
 docs/concepts/graph.md lists. The hub caches the focus and replays it to the pane on
 connect, so sending this before the browser tab is open works.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────╮
│ *    node      TEXT  graph node id, e.g. test:verif/dma#smoke [required]             │
╰──────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```
