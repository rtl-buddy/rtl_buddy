---
description: Configure and run Yosys synthesis with optional OpenROAD timing analysis, PDK mapping, slang parsing, and effort levels.
---

# Synthesis

`rb synth` reads one or more runs from `synth.yaml`, resolves RTL through a model file, and writes a netlist and reports under the config directory.

## Choose a backend

| `tool:` | Flow | Clock handling | Results |
| --- | --- | --- | --- |
| `yosys` | Yosys and ABC | Uses the minimum SDC clock period | Gates, area, WNS |
| `openroad` | Yosys mapping, then OpenROAD STA | Reads the full multi-clock SDC | Gates, area, WNS, TNS |

Use `yosys` for technology-independent synthesis or a quick mapped result. Use `openroad` when timing must respect multiple clocks or you need OpenROAD STA.

Both backends use Yosys for RTL elaboration and mapping. The OpenROAD backend adds a second stage over the mapped netlist.

## Install the tools

RTL Buddy validates against the [RTL Buddy Yosys fork](https://github.com/rtl-buddy/yosys):

```bash
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang
make -j 8
make install
yosys --version
```

Use `make config-gcc` on Linux when appropriate. Ensure `yosys` is on `PATH`.

For `tool: openroad`, build OpenROAD and put `openroad` on `PATH`:

```bash
openroad -version
```

On macOS, use the source-build instructions in the project template's `tools/openroad/SETUP_OSX.md`.

## Define synthesis runs

A minimal `synth.yaml` can contain an unmapped and a technology-mapped run:

```yaml
rtl-buddy-filetype: synth_config

syntheses:
  - name: sandbox_rtl
    desc: Technology-independent synthesis
    model: test_module
    model_path: ../../design/sandbox/models.yaml
    tool: yosys
    reglvl: 0

  - name: sandbox_openroad
    desc: SKY130 mapping and timing
    model: test_module
    model_path: ../../design/sandbox/models.yaml
    tool: openroad
    platform: sky130hd_tt
    constraints: constraints.sdc
    params:
      WIDTH: 8
    defines:
      TARGET_SYNTH: 1
    reglvl: 0
```

Paths resolve from `synth.yaml`. The synthesis top is the model's root module — its `top:` in `models.yaml`, defaulting to the model name. `platform` enables Liberty mapping; the OpenROAD backend additionally requires LEF assets.

Use `lef-paths` and `lib-paths` for block-specific hard macros. Use `tool_overrides` only for backend options that have no portable equivalent. See [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml) for all fields.

## Configure tools and the PDK

Define backend defaults and map a named synthesis platform to a PDK corner in `root_config.yaml`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      synth-args: ""
      abc-args: ""
      frontend: verilog

  - name: openroad
    tool: openroad
    opts:
      strategy: AREA
      frontend: verilog

cfg-pdks:
  - name: sky130hd
    site: unithd
    corners:
      tt: pdk/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
    tech-lef: pdk/sky130hd/lef/sky130_fd_sc_hd.tlef
    macro-lef: pdk/sky130hd/lef/sky130_fd_sc_hd_merged.lef

cfg-synth-platforms:
  - name: sky130hd_tt
    pdk: sky130hd
    corner: tt
```

All paths resolve from `root_config.yaml`.

The Yosys backend uses Liberty for mapping, area, and timing. The OpenROAD backend requires Liberty and technology/macro LEF; a missing LEF fails before running the tool. Keep large PDK files untracked and provide a reproducible fetch script.

OpenROAD `strategy` values are `AREA`, `TIMING`, `TIMING_ANNEAL`, and `TIMING_GENETIC`. `AREA` reports the initial mapping; the timing strategies request OpenROAD resynthesis.

## Use SDC constraints

A Yosys run extracts `create_clock` periods from the SDC and supplies the shortest period to ABC. It warns when multiple clocks require this approximation.

An OpenROAD run loads the complete SDC and reports actual worst and total negative slack. Use it for multi-clock timing decisions.

## SystemVerilog frontend

Use yosys-slang when the built-in `read_verilog -sv` frontend cannot parse the design:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      frontend: slang
      plugin-path: ../yosys-slang/build/slang.so
      single-unit: false
```

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If omitted, RTL Buddy checks `RTL_BUDDY_SLANG_PLUGIN`; that environment value must be absolute, although `~` is expanded.

Set `single-unit: true` only when source files intentionally share preprocessor definitions across file boundaries. It applies only to slang; with the Verilog frontend it is ignored with a warning. Non-Boolean values are fatal.

For one run, override the Yosys elaboration stage:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
    single_unit: true
```

Under `cfg-synth-tools.opts`, fields use kebab case such as `plugin-path` and `single-unit`. Under `tool_overrides.yosys`, use snake case such as `plugin_path` and `single_unit`. Unknown override keys are warned about and ignored.

The override key remains `yosys` even when the run's backend is `openroad`, because Yosys owns elaboration.

## Gate static-lifetime subroutines

A `function` or `task` declared at module, interface, package, program, or
compilation-unit scope without an explicit `automatic` lifetime has *static*
lifetime: every formal argument is one shared storage location. Simulation is
unaffected, because a call completes atomically inside a process, but
yosys-slang lowers the declaration literally and gives every call site the same
net per formal. Two calls in one combinational process then alias their
arguments and the netlist is wrong with no error and no warning.

RTL Buddy scans the sources named by the synthesis filelist, and the headers
they `` `include ``, before Yosys starts, and reports each declaration as
`file:line: function <name>`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      static-functions: error      # error | warn | allow
      conflicting-drivers: error   # error | allow
```

`static-functions` defaults to `error` with `frontend: slang`, which
miscompiles the design, and to `warn` with the legacy `verilog` frontend, which
inlines per call site — correct there, but not portable. `error` fails the run
before Yosys; `warn` logs one warning per finding and records
`static_function_findings` in the result envelope; `allow` skips the scan.

Fix a finding by adding the keyword:

```systemverilog
function automatic ptr_t inc(input ptr_t p);
  return p + 1;
endfunction
```

### What the scan sees

`` `include `` directives are followed, resolved against the including file's
directory and then the filelist's `+incdir+` entries. Each inclusion is scanned
in its own context — a header included from a class is exempt there and still
reported when the same header is included from an ordinary module — and one
declaration is reported once however many places include it.

`` `ifdef `` / `` `ifndef `` / `` `elsif `` / `` `else `` / `` `endif `` are
evaluated on definedness and updated by `` `define ``, `` `undef `` and
`` `undefineall `` in the sources. The macro table is seeded to match the Yosys
invocation exactly:

- the run's `defines:`, which is all `rb synth` passes to `read_verilog -D` /
  `read_slang -D`;
- the macros the selected frontend defines for itself, which differ:
  `read_verilog` predefines `SYNTHESIS` and `YOSYS`, while `read_slang`
  predefines `SYNTHESIS` and slang's own built-ins (`__slang__`, the
  `SV_COV_*` constants) but **not** `YOSYS`. So a `` `ifndef SYNTHESIS ``
  simulation-only helper is never reported under either, an
  `` `ifdef SYNTHESIS `` region always is, and a `` `ifndef YOSYS `` helper is
  reported only under `frontend: slang`, which is the frontend that compiles
  it.

`+define+` entries in the generated filelist are **not** included, because the
synthesis flow does not pass them to Yosys either; seeding the scan with them
would make it skip a declaration that really is elaborated. A run whose
filelist carries `+define+` macros that do not reach Yosys as written logs one
`synth.filelist_defines_ignored` warning naming them, in three groups:

- macros synthesis never sees at all;
- macros it elaborates with a *different* value than the filelist gives, with
  both values shown;
- **bare** `+define+X` entries paired with a synthesis value, which cannot be
  compared and are always reported. Tools disagree about what a valueless
  macro expands to: Verilator and Yosys's `read_verilog` give it an empty body,
  while Icarus and slang give it `1`. Write `+define+X=1` if a value is meant.

That divergence from the simulation flow, which does apply these macros,
predates this gate and is tracked separately — move the macro to the synth.yaml
entry's `defines:` if the design needs it.

`` `undefineall `` follows the frontend in use, which differ: slang clears the
source's own macros but re-applies the command-line ones, so the seed above
survives; Yosys's `read_verilog` clears its command-line cache as well, so
nothing does. A `` `ifndef `` guarded on a `defines:` macro after an
`` `undefineall `` is therefore compiled under `frontend: verilog` and not
under `frontend: slang`, and the scan reports it accordingly.

An `` `include `` chain deeper than 1024 — slang's own limit — fails the run
with the tail of the chain named, rather than silently skipping the header and
losing whatever it declares.

The macro table follows the compilation-unit boundary the frontend actually
uses. With `single-unit: false` — the default — each source is its own
compilation unit, so a `` `define `` in one file does not reach the next and
the table is re-seeded from the run's defines for each; `single-unit: true`
under `frontend: slang` shares it, matching `read_slang --single-unit`. A
header always shares its includer's table, because `` `include `` is textual.

`(* ... *)` attributes are ignored wholesale, so an identifier inside one
never qualifies the declaration it decorates.

Exempt: class methods — including out-of-body definitions such as
`function int C::f(...)`, though not an escaped `\C::f`, which is one
identifier — `extern` and `pure virtual` prototypes, DPI imports
and exports, and any scope declared `module automatic` (or
`package`/`interface`/`program` `automatic`).

The scan is a tokenizer, not an elaborator, and its limits run in both
directions:

| Limit | Effect |
| --- | --- |
| Macro bodies are skipped at their `` `define `` | A declaration produced by a macro is never reported, in either direction; macros are expanded by the compiler, not by the scan |
| `-y` library directories are not scanned | The filelist never names their contents, so declarations there are missed |
| An unresolvable `` `include `` is logged at DEBUG and skipped | That header's declarations are missed; the run is not failed |
| An unknown `frontend` or a missing slang plugin | Fails the run as a configuration error (exit 2) before the gates, not as a synthesis `FAIL` |
| `` `if `` expression evaluation is not implemented | Only definedness is evaluated. This is not SystemVerilog anyway, so it costs nothing in practice |
| Scope nesting is tracked by keyword pairing | Pathological but legal code can change which declarations are exempt, in either direction |

Use Verible's `explicit-function-lifetime` rule through `rb lint` and
`cfg-verible` as the style-lint complement covering testbench and
non-synthesisable sources.

## Gate conflicting drivers

When call sites are split across a combinational and a clocked process, the
shared net takes conflicting drivers and folds to `x`, taking its register and
everything downstream with it. Yosys reports this as a
`multiple conflicting drivers` warning and still exits 0.
`conflicting-drivers: error`, the default, turns those warnings into a failed
run naming the count and the log path. Set `allow` only when the warnings are
understood and accepted.

A legitimate multi-driver tristate bus produces the same warning, one per bit.
Those are not counted: a warning whose drivers are all `$tribuf` / `$_TBUF_`
cells and module ports is a working design, and only a warning with at least
one other driver — a flop, a process action — fails the run.

Both gates apply to the Yosys elaboration stage, which the `yosys` and
`openroad` backends share. An unrecognized value for either option is fatal.

### Upgrading

These gates are new, and `static-functions` defaults to `error` under
`frontend: slang`. A slang synthesis run that passed before can now **fail**,
including one whose subroutines have a single call site and whose netlist
happens to be correct — the scan reports the declaration, not the aliasing.
That default is deliberate: the failure mode it guards is a silently corrupted
netlist with plausible area and timing numbers. To stage the migration, set
`static-functions: warn` while the declarations are fixed; each run then still
reports `static_function_findings` in its machine output.

## Select an effort

Define reusable effort levels in `root_config.yaml`:

```yaml
cfg-synth-efforts:
  - name: quick
    yosys:
      synth-args: -flatten
      abc-args: -fast
    openroad:
      run: false

  - name: standard
    openroad:
      run: true

  - name: accurate
    openroad:
      run: true
      pre-sta-tcl: |
        set_wire_load_mode top
        set_wire_load_model -name Small
```

Select an effort in the run or on the CLI:

```yaml
effort: quick
```

```bash
rb synth sandbox_openroad --effort quick
rb synth-regression --effort accurate
```

Precedence is per-run `tool_overrides`, then the selected effort, then `cfg-synth-tools`. Without a configured or selected effort, RTL Buddy uses built-in `standard` behavior.

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl executed before STA; test it on a small design because syntax and tool errors appear only at runtime.

## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only RTL `(* blackbox *)` declaration for frontend binding.

The OpenROAD stage avoids generating a Verilog stub when the macro already exists in the supplied LEF or Liberty, preserving its physical area and timing arcs. If no physical or timing master exists, RTL Buddy generates a port-only stub and the reported PPA cannot represent that macro accurately.

## Run synthesis

```bash
rb synth --list -c synth/block/synth.yaml
rb synth block_openroad -c synth/block/synth.yaml
rb synth -c synth/block/synth.yaml
rb synth-regression -c synth_regression.yaml
rb synth-regression -c synth_regression.yaml --reg-level 1000
```

A synthesis regression manifest lists config files relative to itself:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs:
  - synth/block_a/synth.yaml
  - synth/block_b/synth.yaml
```

## Interpret results

Mapped runs report gates and area. Constrained Yosys runs report WNS as clock period minus critical-path delay. OpenROAD reports actual WNS and TNS; negative values indicate violations and TNS 0 indicates no negative endpoint slack.

A Yosys run passes when the process exits 0, its log has no `ERROR:` line, and neither correctness gate fires. An OpenROAD run requires both the Yosys and OpenROAD stages to exit 0 and rejects OpenROAD `[ERROR ...]` lines. Any failed stage reports `FAIL`.

## Inspect artefacts

Outputs land under `<synth-dir>/artefacts/<run>/`.

| File | Backend | Purpose |
| --- | --- | --- |
| `synth.f`, `synth.ys` | Both | Resolved sources and generated Yosys script |
| `synth.rtlil` | Unmapped Yosys | Technology-independent netlist |
| `synth_netlist.v` | Mapped runs | Gate-level Verilog |
| `synth.log` | Yosys-only | Yosys output |
| `synth_yosys.log` | OpenROAD | First-stage Yosys output |
| `synth.tcl`, `synth.log` | OpenROAD | STA script and OpenROAD output |

Both netlists are deleted at the very start of each run, before the filelist is even generated and before Yosys is looked for at all, so every way a run can fail leaves them absent — there is no missing-tool carve-out here, because `rb pnr` and `rb power` resolve the netlist by path and must never be handed the previous run's. A run that fails publishes nothing. Yosys writes the netlist partway through its script and only then runs the trailing `stat`, so it can crash — or log an `ERROR:` line — with the netlist already on disk; and on the OpenROAD backend the Yosys stage can succeed before the timing stage fails. Every one of those paths removes the netlist again, so a `FAIL` never leaves a design for `rb pnr` or `rb power` to pick up. They are the fixed-path inputs `rb pnr` and `rb power` resolve, so a failed rerun that left the last successful run's netlist in place would have those commands place, route, and power-analyse a design that is no longer what the RTL says. A failed run therefore leaves no netlist at all, and `rb pnr` reports that you need to run `rb synth` first. Copy a netlist you want to compare against out of the artefact directory before rerunning.

When a run fails, inspect the relevant stage log first. Missing tools, plugin paths, Liberty, or LEF inputs are configuration failures; correct the path or installation and rerun the named synthesis.
