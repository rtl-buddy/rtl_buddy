## Build a hierarchy from a clean tree

On a clean tree the top's synthesis needs the blocks' abstracts. `--synth` runs each run's upstream synthesis just before it:

```sh
rb pnr -c pnr/top/pnr.yaml --synth
```

- A synthesis runs once however many runs read it, regardless of its own `reglvl`. All syntheses are resolved before the first run starts, so a misspelt `synth:` stops the command up front.
- A synthesis that does not pass fails its P&R run with `fail_stage: synth`, which blocks the run's consumers. `--accept-stale` applies to syntheses too.
- `-j N` runs up to `N` P&R runs at once (default 1). A run starts as soon as every block it names has finished. Each run is a whole OpenROAD session with its own [`threads:`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/pnr/#openroad-threads), so size the two together.
