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
| `name` | Required | Model identifier; must be unique across every `models.yaml`, not only within one, and regardless of `graph:`. Must start with a letter, digit or underscore and contain only letters, digits, underscore, dot or hyphen |
| `filelist` | Required | Filelist entries resolved from `models.yaml` |
| `desc` | Required | Human-readable description |
| `spec` | Optional | `specs.yaml` path for `rb spec`; no simulation effect |
| `synth` | Optional | Synthesis ownership pointer, optionally with `#entry`; no current runtime consumer |
| `tests` | Optional | Test-suite ownership pointer, optionally with `#entry`; no current runtime consumer |
| `graph` | Optional | `false` opts the model out of `rb graph build`'s design tier; default `true` |
| `top` | Optional | Root module of the filelist when it is not named after the model; default `name`. Letters, digits and underscore only (no `$`), and unique across the graphable models `rb graph build` selects |
| `elaborations` | Optional, default empty | Named pyslang profile deltas used by `rb elab --profile` and `rb elab-regression`; the model remains directly elaborable without this field |

`top` is the model's root module everywhere rtl_buddy elaborates it, and it is binding, not advisory: a model has one root module, and a model whose name is not a module was already broken in every one of these flows. It roots `rb hier`, `rb hier-query`, and `rb axi-profile`, it roots the `rb graph build` design-tier export, it is the target of the graph's `model --maps_to--> module:` edge, and it is the default top of a `cdc.yaml`, `synth.yaml`, `lint.yaml`, `fpga.yaml`, `fpv.yaml`, or `mut.yaml` run against the model. Only `fpv.yaml` and `mut.yaml` have a `top:` field of their own; where one is set it wins, because a formal checker top lives in the run's own `properties:`. Setting `top` therefore changes artefact names that embed it — the FPGA bitstream is `<top>.bit`, and OpenROAD's design name follows the synthesis top.

Models in a `rb graph build` selection must not collide, and the build refuses either collision before invoking the exporter, naming both models and both `models.yaml` files.

A model name is also a directory name — `artefacts/hier/<name>/`, `artefacts/graph/design/<name>/`, and the per-model directory every flow writes — so it is restricted to a single safe path segment and rejected at load time otherwise. Path separators, absolute paths, `.` and `..` are refused.

`top` is checked at load time too, against a stricter rule: a letter or underscore, then letters, digits or underscore. It does not stay in HDL — the FPGA flows name the bitstream `<top>.bit`, and the Yosys, Vivado and OpenROAD generators interpolate it into Tcl unquoted — so a value carrying a path separator, a newline or a shell or Tcl metacharacter is refused rather than escaped per tool. That is narrower than SystemVerilog allows, deliberately: `$` is legal in an SV identifier but substitutes in Tcl, so `synth_design -top foo$bar` would elaborate a different module than the YAML names; and escaped identifiers (`\name `) can carry `/` and `;`. A top that really needs either has to be renamed, or wrapped in a module whose name does not.

**No two models may share a `name`, opted out or not.** Every per-model artefact path is keyed on it, so two exports overwrite each other in `artefacts/graph/design/<name>/` and `artefacts/hier/<name>/` while the tier reports both as built. Distinct `top:` values do not make that safe, and neither does `graph: false`: a name is also how every selector spells a model — `--model NAME`, a test's `model:`, a back-pointer — so a duplicate shadows the other entry in any lookup by name, silently. Rename one of them. A duplicate within one file is already rejected by the loader; this is the across-files half of the same rule.

**No two models that would both be exported may share a top.** `module:<top>` is a global graph id and DUT ids are never suite-qualified, so two such exports merge into a single hybrid hierarchy rather than staying apart. Give them distinct roots, or set `graph: false` on the one that is not the design of record — an opted-out model is never exported, so it claims no graph id.

Models the build is not selecting are not considered by either rule.

Set `graph: false` for a model with no elaborable root — an SV `interface` published as a library entry, or a filelist of vendored IP with no module named after the model. `rb graph build` then records the model, and every testbench and non-simulation run rooted at it, under the design tier's `skipped` list instead of attempting an export that can only fail, and removes any `artefacts/graph/design/<model>/` a previous build left behind. The config tier still emits the model node, so `spec:` and test cross-references keep resolving; it carries `graph: false` and no `maps_to` edge. The opt-out is design-tier-only: `rb hier`, `rb hier-query`, and `rb axi-profile` still run against the model and still fail if its root does not elaborate. Prefer `top:` when the filelist does elaborate and only the root module name differs.

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

Filelists support `-F` recursion, `+incdir+`, `+libext+`, `+define+`, `-v`, `-y`, and source paths. Environment variables in entries are expanded. Every path-valued entry, including `+incdir+` and `-y` search directories, resolves against the directory of the filelist that declares it, so a filelist pulled in with `-F` can carry the include path its own sources need. `+define+NAME[=VALUE]` declares a preprocessor macro; several may share one entry with `+` separators, so a value cannot contain `+`.

Not every flow honors `+incdir+` and `+define+`. Simulation and model elaboration apply both. Synthesis, CDC, and FPGA drop `+incdir+` when they read the generated filelist back, so a header those flows need requires a search path configured for them instead (see [FPGA Implementation](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpga/)). `rb synth` also drops `+define+` in favour of the synth.yaml entry's `defines:` and warns when the filelist carries macros it is not applying. Renderer-only flows drop definitions.

### Elaboration profiles

A profile is a delta on its containing model, not another model reference. Paths in `prepend_sources`, `append_sources`, and `include_dirs` resolve from `models.yaml`. Top precedence is profile `top`, then model `top`, then model `name`. Profile names are unique ignoring case, and every case variant of `base` is reserved so artifact paths remain portable to case-insensitive filesystems.

| Field | Default and validation |
|---|---|
| `name` | Required, unique within the model, and a safe single path segment; `base` is reserved for the bare-model artifact |
| `desc` | Optional description |
| `top` | Model top; optional simple SystemVerilog identifier |
| `reglvl` | 0; non-negative integer used by `elab-regression` |
| `prepend_sources` / `append_sources` | Empty; source or filelist entries placed before or after the model's expanded filelist |
| `include_dirs` | Empty; extra include directories placed before the model filelist |
| `defines` | Empty map of identifier to string, integer, boolean, or null. Boolean values render as `1`/`0`; null defines only the name. String values cannot be empty or contain whitespace or `+`. Profile definitions take precedence over same-named definitions in the model filelist |
| `parameters` | Empty map of top-level parameter overrides. String values are SystemVerilog expression text; booleans render as `1`/`0`. Unknown and local parameter names fail elaboration |
| `vcs_compat` | false; enables slang VCS compatibility mode |
| `single_unit` | false; parses primary sources as one compilation unit |
| `libraries_inherit_macros` | false; requires `single_unit: true` and shares primary-unit macros with library sources |
| `timescale` | Unset; command-line timescale such as `1ns/1ps` |
| `ignored_directives` | Empty; directive names for slang to ignore |
| `warnings` | Empty; warning controls without the `-W` prefix, such as `all`, `none`, `no-unused`, or `error=unused`. These cannot suppress hard compilation errors |
| `resources` | Inherits `cfg-dispatch.resources` field by field; `cpus` must be positive and also controls pyslang worker threads |

`rb elab MODEL -c models.yaml` runs the base model. `rb elab MODEL --profile NAME -c models.yaml` applies one profile. Outputs are `artefacts/elab/<model>/<base-or-profile>/elab.f`, `elab.log`, and `result.json`. See [Model Elaboration](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/elaboration/).
