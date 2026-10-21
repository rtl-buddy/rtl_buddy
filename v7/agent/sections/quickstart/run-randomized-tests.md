## Run randomized tests

```bash
uv run rb test basic --rnd-new         # one run with a new seed
uv run rb randtest basic 5             # five distinct iterations
uv run rb randtest basic 5 --rnd-rpt 3 # replay iteration 3
```

Seeds are recorded with the test artefacts.
