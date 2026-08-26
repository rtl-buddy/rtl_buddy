## Load graph.json directly

The file is plain JSON; NetworkX is optional:

```python
import json
import networkx as nx

with open("artefacts/graph/graph.json") as handle:
    data = json.load(handle)

graph = nx.node_link_graph(data, edges="links")
```

Do not assume that a config-only graph has no dangling endpoints: config-to-design edges intentionally name modules that the design tier supplies when included.
