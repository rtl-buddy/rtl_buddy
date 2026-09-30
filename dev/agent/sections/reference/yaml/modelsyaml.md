## models.yaml

Required keys are `rtl-buddy-filetype: model_config` and `models`.

```yaml
rtl-buddy-filetype: model_config
models:
  - name: my_design
    filelist: [-F my_design.f]
    spec: ../../spec/my_design/specs.yaml
    elaborations:
      - name: smoke
        defines: {CHECKS_ENABLED: 1}
        parameters: {DATA_WIDTH: 32}
        resources: {cpus: 2, mem: 2G, time: "00:10:00"}
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Model identifier. Unique across every `models.yaml`, regardless of `graph:`. See [Model names](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#model-names) |
| `filelist` | Required | Filelist entries resolved from `models.yaml`. See [Filelists](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#filelists) |
| `desc` | Optional | Human-readable description |
| `spec` | Optional | `specs.yaml` path for `rb spec`; no simulation effect |
| `axi_bundles` | Optional | `axi-bundles.yaml` path relative to `models.yaml`, written by `rb axi-profile discover` |
| `axi_monitor_out` | Optional | Path relative to `models.yaml` where `rb axi-profile gen-monitor` writes the monitor |
| `cdc` | Optional | `cdc.yaml` path relative to `models.yaml`, optionally with `#analysis_name`. `rb hub` reads it for the clock-domain overlay |
| `synth` | Optional | Synthesis ownership pointer, optionally with `#entry`; no current runtime consumer |
| `tests` | Optional | Test-suite ownership pointer, optionally with `#entry`; no current runtime consumer |
| `graph` | Optional | `false` opts the model out of the `rb graph build` design tier. Default `true` |
| `top` | Optional | Root module of the filelist when it is not named after the model. Default `name`. See [Model top](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#model-top) |
| `elaborations` | Optional, default empty | Named pyslang profile deltas used by `rb elab --profile` and `rb elab-regression`. The model stays directly elaborable without them |

### Model names

A model name is also a directory name (`artefacts/hier/<name>/`, `artefacts/graph/design/<name>/`, and the per-model directory every flow writes). It is checked at load time: it must start with a letter, digit, or underscore, and contain only letters, digits, underscore, dot, or hyphen. Path separators, absolute paths, `.`, and `..` are refused.

No two models may share a `name`, including models with `graph: false`. Per-model artefact paths and every selector (`--model NAME`, a test's `model:`, a back-pointer) are keyed on the name, so a duplicate silently shadows the other entry. A duplicate within one file is rejected by the loader. `rb graph build` refuses a duplicate across the files it selects before invoking the exporter, naming both models and both `models.yaml` files; rename one entry.

### Model top

`top` is the model's root module in every flow and is binding, not advisory. It roots `rb hier`, `rb hier-query`, `rb axi-profile`, and the `rb graph build` design-tier export, and it is the target of the graph's `model --maps_to--> module:` edge. It is also the default top of a `cdc.yaml`, `synth.yaml`, `lint.yaml`, `fpga.yaml`, `fpv.yaml`, or `mut.yaml` run against the model.

- Only `fpv.yaml` and `mut.yaml` have a `top:` field of their own; where one is set it wins, because a formal checker top lives in the run's own `properties:`.
- Setting `top` changes artefact names that embed it. The FPGA bitstream is `<top>.bit`, and OpenROAD's design name follows the synthesis top.
- `top` is checked at load time: a letter or underscore, then letters, digits, or underscores. It is interpolated unquoted into Tcl and used in artefact names, so a path separator, newline, `$`, shell or Tcl metacharacter, or SystemVerilog escaped identifier is refused rather than escaped per tool. Rename such a module, or wrap it in one with a plain name.

### Graph opt-out and top collisions

Models that `rb graph build` would both export must not share a `top`. `module:<top>` is a global graph id and DUT ids are not suite-qualified, so two such exports merge into one hybrid hierarchy. The build refuses this before invoking the exporter and names both models and both `models.yaml` files. Give the models distinct roots, or set `graph: false` on the one that is not the design of record. Models outside the build's selection are not considered.

Set `graph: false` for a model with no elaborable root, such as an SV `interface` published as a library entry or a filelist of vendored IP with no module named after the model.

- `rb graph build` records the model, and every testbench and non-simulation run rooted at it, in the design tier's `skipped` list. It also removes any `artefacts/graph/design/<model>/` left by an earlier build.
- The config tier still emits the model node, so `spec:` and test cross-references resolve. The node carries `graph: false` and no `maps_to` edge.
- The opt-out affects only the design tier. `rb hier`, `rb hier-query`, and `rb axi-profile` still run against the model and fail if its root does not elaborate.
- Prefer `top:` when the filelist elaborates and only the root module name differs.

```yaml
models:
  - name: apb_intf
    desc: APB interface library
    filelist: [-v apb_intf.sv]
    graph: false
  - name: pp_axi
    desc: Vendored AXI collection
    filelist: [-F pp_axi.f]
    top: axi_xbar
```

### Filelists

Filelists support `-F` recursion, `+incdir+`, `+libext+`, `+define+`, `-v`, `-y`, and source paths. Environment variables in entries are expanded.

- Every path-valued entry, including `+incdir+` and `-y` directories, resolves against the directory of the filelist that declares it. A filelist pulled in with `-F` can therefore carry the include path its own sources need.
- `+define+NAME[=VALUE]` declares a preprocessor macro. Several may share one entry separated by `+`, so a value cannot contain `+`.

Flows differ in which entries they honor:

- Simulation and model elaboration apply `+incdir+` and `+define+`.
- Synthesis (`read_verilog -I` and `read_slang -I`, on both yosys backends), FPGA (openXC7's `read_verilog -I`, Vivado's `synth_design -include_dirs`), and the Vivado CDC tool forward every `+incdir+`, each directory resolved against the filelist that declared it.
- `rb cdc` and the hub's domain-map build use rtl-buddy-cdc, which has no include-path option. The run logs `cdc.filelist_incdirs_unsupported` naming the directories, and the analyzer's own `Cannot find include file` error follows if a header resolves only through them.
- `rb synth` applies `+define+` entries. The `synth.yaml` entry's `defines:` win on conflict and log `synth.filelist_defines_overridden`.
- Renderer-only flows drop definitions.

### Elaboration profiles

A profile is a delta on its containing model, not another model reference. Paths in `prepend_sources`, `append_sources`, and `include_dirs` resolve from `models.yaml`. Top precedence is profile `top`, then model `top`, then model `name`. Profile names are unique ignoring case, and every case variant of `base` is reserved so artifact paths stay portable to case-insensitive filesystems.

| Field | Default and validation |
|---|---|
| `name` | Required, unique within the model, and a safe single path segment. `base` is reserved for the bare-model artifact |
| `desc` | Optional description |
| `top` | Model top; optional simple SystemVerilog identifier |
| `reglvl` | 0; non-negative integer used by `elab-regression` |
| `prepend_sources` / `append_sources` | Empty; source or filelist entries placed before or after the model's expanded filelist |
| `include_dirs` | Empty; extra include directories placed before the model filelist |
| `defines` | Empty map of identifier to string, integer, boolean, or null. Booleans render as `1`/`0`; null defines only the name. String values cannot be empty or contain whitespace or `+`. Profile definitions win over same-named definitions in the model filelist |
| `parameters` | Empty map of top-level parameter overrides. String values are SystemVerilog expression text; booleans render as `1`/`0`. Unknown and local parameter names fail elaboration |
| `vcs_compat` | false; enables slang VCS compatibility mode |
| `single_unit` | false; parses primary sources as one compilation unit |
| `libraries_inherit_macros` | false; requires `single_unit: true` and shares primary-unit macros with library sources |
| `timescale` | Unset; command-line timescale such as `1ns/1ps` |
| `max_parse_depth` | Unset, so slang's 1024-level parser nesting limit applies. An integer from 1 to 65536; raise it for generated RTL with a deeply nested single expression, such as a long conditional or concatenation chain |
| `ignored_directives` | Empty; directive names for slang to ignore |
| `warnings` | Empty; warning controls without the `-W` prefix, such as `all`, `none`, `no-unused`, or `error=unused`. They cannot suppress hard compilation errors |
| `resources` | Inherits `cfg-dispatch.resources` field by field. `cpus` must be positive and also sets pyslang worker threads |

`rb elab MODEL -c models.yaml` runs the base model. `rb elab MODEL --profile NAME -c models.yaml` applies one profile. Outputs are `artefacts/elab/<model>/<base-or-profile>/elab.f`, `elab.log`, and `result.json`. See [Model Elaboration](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/elaboration/).
