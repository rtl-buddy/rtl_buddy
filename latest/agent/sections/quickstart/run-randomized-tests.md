## Run randomized tests

```bash
uv run rb test basic --rnd-new
uv run rb randtest basic 5
uv run rb randtest basic 5 --rnd-rpt 3
```

These commands run once with a new seed, run five distinct iterations, and replay iteration 3 respectively. Seeds are recorded with the test artefacts.
