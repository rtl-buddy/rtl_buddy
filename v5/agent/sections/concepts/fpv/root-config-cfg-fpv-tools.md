## Root config: `cfg-fpv-tools`

`cfg-fpv-tools` declares the FPV tools available to all suites in this project:

```yaml
cfg-fpv-tools:
  - name: "sby"
    tool: "sby"          # binary on PATH, or absolute path
    opts:
      timeout: 600       # seconds per task; optional
      extra-args: ""     # appended to sby invocation; optional
      solver-versions:   # optional pins for reproducible CI; map
        yices: "2.6.4"   # solver name -> exact version string
        z3: "4.13.0"
```

| Field | Description |
|-------|-------------|
| `name` | Referenced by `tool:` in `fpv.yaml` |
| `tool` | Binary name (PATH-resolved) or absolute path |
| `opts.timeout` | Per-task timeout in seconds, written to the sby `[options]` block |
| `opts.extra-args` | Passed through verbatim to the sby command line |
| `opts.solver-versions` | Optional map of solver name → exact version. Probed before every run; hard-fails on mismatch. Known solvers: `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, `abc` |

### Choosing a frontend

Two SystemVerilog frontends are supported under `rb fpv`:

- **`frontend: verilog`** (default) — yosys's native verilog reader. No plugin needed. Handles immediate `assert (expr);` inside `always` blocks plus simple concurrent assertions (`assert property (@clk expr);`). Rejects `|->` / `|=>`, `##N`, sequence operators, and SV `bind`.
- **`frontend: slang`** — yosys-slang plugin. Required for concurrent SVA implications (`a |-> b`, `a |=> b`), sequence operators, and SV `bind` directives to elaborate. Requires `cfg-fpv-tools[].opts.plugin-path` in `root_config.yaml`.

When in doubt, **start with `verilog`** — it's the path most rtl_buddy demos use. Move to `slang` only when you need `|->` / `|=>` for vacuity covers or a richer SVA dialect.

#### Which yosys-slang build to use

povik's [upstream `master`](https://github.com/povik/yosys-slang) does not yet lower concurrent SVA implications (`|->` / `|=>`) to `$check` cells — the in-flight implementation lives in [povik/yosys-slang#317](https://github.com/povik/yosys-slang/pull/317), still in draft as of 2026-05-26. Using upstream master with `rb fpv` `frontend: slang` on a `|->` property surfaces as:

```
error: encountered unsupported SVA feature
```

Until #317 merges upstream, build from the **[rtl-buddy/yosys-slang `rtl-buddy` branch](https://github.com/rtl-buddy/yosys-slang/tree/rtl-buddy)** — three commits ahead of povik master (the SVA-rebase work + a stale-test count fix + a `disable iff` regression fix; ctest 46/46). The rtl-buddy fork tracks [rtl-buddy/yosys-slang#1](https://github.com/rtl-buddy/yosys-slang/issues/1) as its vendoring status; this doc switches back to recommending upstream once the fork's `master` fast-forwards to a povik release that includes the SVA work.

povik upstream master is still fine for `rb synth` and `rb cdc` with `frontend: slang` — those paths don't need the SVA implication lowering. The [rtl-buddy-project-template SETUP_OSX.md](https://github.com/rtl-buddy/rtl-buddy-project-template/blob/main/tools/yosys-slang/SETUP_OSX.md) has the per-use-case build matrix.

### Solver version pinning

`sby` happily picks whatever solver binary it finds on PATH. On CI, different runners can resolve to different versions and silently change proof outcomes — a proof that passes at depth 32 on one machine can time out on another. Set `opts.solver-versions` to lock the resolution: before each run, each pinned solver is probed (`yices-smt2 --version` etc.) and the run hard-fails with a one-shot summary of every mismatch if any pin doesn't match exactly. Resolved versions are logged as `fpv.solver_pins_resolved` so the run artefacts capture exactly what was used.
