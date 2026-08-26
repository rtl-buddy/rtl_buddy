## Run with randomized seeds

```bash
rb test smoke --rnd-new
rb test smoke --rnd-last
rb randtest smoke 20
```

`--rnd-new` records a generated seed; `--rnd-last` reuses it. `randtest` runs repeated seeded iterations. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/#randtest) for replay and selection options.
