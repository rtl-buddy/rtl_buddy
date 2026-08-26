## Run with randomization

Run a test once with a new random seed:

```bash
uv run rb test basic --rnd-new
```

Run the same test 5 times with different seeds:

```bash
uv run rb randtest basic 5
```

Repeat a specific iteration from a previous `randtest` run:

```bash
uv run rb randtest basic 5 --rnd-rpt 3
```
