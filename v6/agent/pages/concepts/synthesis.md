---
description: Configure and run Yosys synthesis with optional OpenROAD timing analysis, PDK mapping, the slang frontend, correctness gates, and effort levels.
---

# Synthesis

`rb synth` runs one or more synthesis runs from `synth.yaml`. Each run resolves RTL through a model file, then writes a netlist and reports under the config directory.

## Choose a backend

| `tool:` | Flow | Clock handling | Results |
| --- | --- | --- | --- |
| `yosys` | Yosys and ABC | Uses the shortest SDC clock period | Gates, area, WNS |
| `openroad` | Yosys mapping, then OpenROAD STA | Reads the full multi-clock SDC | Gates, area, WNS, TNS |

Use `yosys` for technology-independent synthesis or a quick mapped result. Use `openroad` when timing must respect multiple clocks: a Yosys run passes only the shortest `create_clock` period to ABC and warns when there are several.

## Install the tools

rtl_buddy is validated against the [RTL Buddy Yosys fork](https://github.com/rtl-buddy/yosys). Put `yosys` on `PATH`:

```bash
git clone --recursive https://github.com/rtl-buddy/yosys.git
cd yosys
make config-clang    # or make config-gcc on Linux
make -j 8
make install
yosys --version
```

`tool: openroad` also needs `openroad` on `PATH` (`openroad -version`). On macOS, follow `tools/openroad/SETUP_OSX.md` in the project template. `rb tool-check --explain yosys` reports what is missing.

## Define synthesis runs

A `synth.yaml` can hold an unmapped and a technology-mapped run:

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

Paths resolve from `synth.yaml`. The synthesis top is the model's root module: its `top:` in `models.yaml`, defaulting to the model name. `platform` enables Liberty mapping. Use `lef-paths` and `lib-paths` for block-specific hard macros, and `tool_overrides` for backend options with no portable equivalent. All fields are in [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml).

## Configure tools and the PDK

`root_config.yaml` holds backend defaults and maps a named synthesis platform to a PDK corner:

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

Paths resolve from `root_config.yaml`. The Yosys backend needs Liberty. The OpenROAD backend needs Liberty plus technology and macro LEF. Keep large PDK files untracked and provide a fetch script.

OpenROAD `strategy` is `AREA`, `TIMING`, `TIMING_ANNEAL`, or `TIMING_GENETIC`. `AREA` reports the initial mapping; the timing strategies request OpenROAD resynthesis.

Synthesis reads only a PDK's Liberty corner, LEFs and `dont-use-cells`:

- **Nangate45**: one Liberty file. The template's `synth/demo_tiny_alu_subsys/download_pdk.sh` fetches it.
- **sky130hd**: one Liberty per corner, plus a `dont-use-cells` list for the probe and `lpflow` cells. See the template's `sky130hd` entry.
- **ASAP7**: not validated. List each corner's split Liberty files (AO, INVBUF, OA, SIMPLE, SEQ) under that corner.

A corner takes one Liberty path or a list. Every file of the corner is read and goes to `dfflibmap`, `abc` and `stat`; `lib-paths` macros are only read. Without a `platform`, the `lib-paths` are the cell libraries.

Liberty files may be gzipped. SDC values are in the Liberty `time_unit`, which rb reads from each file's `library` group (1 ns when unset) and uses to convert the ABC delay target and every reported time to picoseconds. All Liberty files a run reads must share one `time_unit`; otherwise the run fails at setup.

`dont-use-cells` patterns exclude cells from mapping. A synth platform's own list is appended to the PDK's. [Place-and-Route](pnr.md#tune-the-process-dependent-steps) reads the PDK's list too, so two runs that exclude different cells count as two experiments. P&R-side PDK notes are in [Place-and-Route: PDK setup notes](pnr.md#pdk-setup-notes).

## How the SDC is read

`rb` reads SDC and XDC text with one of two readers. `rb tool-check` names the active one under `In-process readers`.

- **`tcl`**: a safe Tcl interpreter, so line continuations, braces, nested collections, `$variables` and `[expr ...]` read as they do in Vivado and OpenSTA. It has no `exec`, `open`, `file`, `socket`, `cd`, `glob` or `source`; `source` includes are not followed, so name the included file directly.
- **`tokenizer`**: the fallback when no Tcl interpreter can start, typically a Python without `_tkinter`. It splits words but evaluates nothing, so `-period $p` or `-period [expr ...]` is reported as unevaluated. Install tkinter to restore the `tcl` reader: `uv python install --managed-python`, `brew install python-tk@<X.Y>`, or `dnf install python3-tkinter`.

Either reader returns syntax only. `get_ports`, `get_cells` and `-filter` collections stay opaque names, because OpenROAD or Vivado resolves them against a netlist.

Set `RTL_BUDDY_CONSTRAINT_READER=tokenizer` to force the fallback, or `=tcl` to require the interpreter and fail if none can start.

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
      best-effort-hierarchy: false
```

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If omitted, `RTL_BUDDY_SLANG_PLUGIN` is used; it must be an absolute path, though `~` is expanded. An unknown `frontend` or a missing plugin is a configuration error (exit 2) reported before synthesis starts, not a synthesis `FAIL`.

- `single-unit: true` shares preprocessor definitions across source files. Set it only when the sources rely on that.
- `best-effort-hierarchy: true` keeps module instances as hierarchy and honours `(* keep_hierarchy *)`. Without it yosys-slang inlines every instance. Set it when mapping needs the hierarchy, for example when a flattened cone of `keep_hierarchy` multipliers stalls ABC.

Both apply only to slang; the Verilog frontend ignores them with a warning. A non-Boolean value is fatal.

To change the elaboration stage for one run, use `tool_overrides`:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
```

`cfg-synth-tools.opts` uses kebab case (`plugin-path`); `tool_overrides.yosys` uses snake case (`plugin_path`). Unknown override keys are warned about and ignored. The override key stays `yosys` when the backend is `openroad`.

## Correctness gates

Three gates check the Yosys elaboration stage of both backends for netlists that are wrong without any error. Set them in `cfg-synth-tools.opts`:

```yaml
opts:
  static-functions: error        # error | warn | allow
  conflicting-drivers: error     # error | allow
  unresolved-interfaces: warn    # error | warn | allow
```

An unrecognized value is fatal.

## Gate static-lifetime subroutines

A `function` or `task` declared at module, interface, package, program or compilation-unit scope without `automatic` has static lifetime: each formal argument is one shared storage location. Simulation is unaffected, but yosys-slang lowers it literally, so two calls in one combinational process alias their arguments and the netlist is wrong with no error.

Before Yosys starts, rtl_buddy scans the filelist's sources and their `` `include `` headers and reports each declaration as `file:line: function <name>`.

- `error` fails the run before Yosys starts. It is the default with `frontend: slang`.
- `warn` logs one warning per finding and records `static_function_findings` in `--machine` output. It is the default with `frontend: verilog`, which inlines per call site, so the result is correct but not portable.
- `allow` skips the scan.

The scan reports declarations, not actual aliases, so a subroutine with a single call site also fails. Fix by adding the keyword; set `static-functions: warn` while migrating:

```systemverilog
function automatic ptr_t inc(input ptr_t p);
  return p + 1;
endfunction
```

Class methods, `extern` and `pure virtual` prototypes, DPI imports and exports, and anything declared `automatic` are exempt. For testbench and non-synthesisable sources, Verible's `explicit-function-lifetime` rule runs through `rb lint` and `cfg-verible`.

The scan is a tokenizer, not an elaborator. It follows `` `include `` and evaluates `` `ifdef `` on definedness only. It misses declarations produced by macros, under `-y` library directories, or in unresolvable headers. Scope nesting is tracked by keyword pairing, so unusual but legal code can change which declarations count as exempt.

## Preprocessor definitions

The scan and the frontend see the filelist's `+define+` entries, then the run's `defines:`, then the frontend's own macros. `read_verilog` predefines `SYNTHESIS` and `YOSYS`; `read_slang` predefines `SYNTHESIS` but not `YOSYS`. An `` `ifndef YOSYS `` helper is therefore reported only under `frontend: slang`.

- Write `+define+X=1` when a value is meant. A bare `+define+X` gets the value the frontend gives a valueless `-D`, which differs between tools.
- When `defines:` overrides a filelist entry with a different value, or overrides a bare entry, the run warns with `synth.filelist_defines_overridden`. Simulation then uses the filelist value and synthesis the `synth.yaml` one. Drop one of the two to make them agree.
- A filelist `+define+` value containing whitespace is fatal.

## Gate conflicting drivers

When one net is driven from both a combinational and a clocked process, for example through aliased static-function arguments, it folds to `x` and takes its register and everything downstream with it. Yosys reports only a `multiple conflicting drivers` warning and exits 0.

`conflicting-drivers: error` (default) fails the run and names the count and the log path. Set `allow` only when the warnings are understood.

Tristate-bus warnings (all drivers are tristate cells and ports) are not counted; any other driver, such as a flop, fails the run.

## Gate unbound interface instances

`read_verilog` cannot bind an interface instance to a child module's interface port. It warns ``Could not find interface instance for `<instance>' in `<module>'`` and exits 0. The instance's own port connections are dropped, so an interface that carries a clock or reset leaves it undriven:

```systemverilog
bus_if b (.clk(clk));          // .clk is dropped
producer u_p (.b(b), ...);     // flops in both children
consumer u_c (.b(b), ...);     // lose their clock
```

`frontend: slang` binds the instance correctly. Otherwise `unresolved-interfaces` decides:

- `warn` (default) logs one `synth.unresolved_interface` per instance, records `unresolved_interfaces` in `--machine` output, and passes. The result is correct for interfaces with no ports of their own.
- `error` fails the run and removes the netlist, so `rb pnr` and `rb power` cannot consume it. Use it in projects whose interfaces have ports.
- `allow` silences the warning.

## Select an effort

Define reusable effort levels in `root_config.yaml`:

```yaml
cfg-synth-efforts:
  - name: quick
    yosys:
      synth-args: -flatten
      abc-script: "strash; dretime; map {D}"
    openroad:
      run: false

  - name: accurate
    openroad:
      run: true
      pre-sta-tcl: |
        set_wire_load_mode top
        set_wire_load_model -name Small
```

Select one with `effort: quick` in the run, or on the command line:

```bash
rb synth sandbox_openroad --effort quick
rb synth-regression --effort accurate
```

Precedence is the run's `tool_overrides`, then the selected effort, then `cfg-synth-tools`. With no effort, the built-in `standard` applies.

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl run before STA; syntax errors appear only at runtime. The OpenROAD stage uses one thread unless the entry sets `threads:`; see [OpenROAD threads](pnr.md#openroad-threads).

## Choose the mapped-run ABC script

A Liberty-mapped run, which is every `tool: openroad` run and a `tool: yosys` run with a `platform` or `lib-paths`, maps logic to cells with one `abc -liberty` command. `abc-script` sets the ABC commands it runs. The default is Yosys' default Liberty script without `dc2`:

```text
strash; &get -n; &fraig -x; &put; scorr; dretime; strash; &get -n; &dch -f; &nf {D}; &put
```

`dc2` rebuilds the log-depth carry networks that `techmap` produces for adders, negates and incrementers as ripple chains, so it is left out. Set another script in an effort, or for one run in `tool_overrides.yosys.abc_script`:

```yaml
cfg-synth-efforts:
  - name: large
    yosys:
      synth-args: -noabc
      abc-script: "strash; dretime; map {D}"
```

- rtl_buddy keeps `-liberty` and `-dont_use`. On `tool: yosys` with an SDC clock it also passes `-D <period_ps>`, the shortest SDC period in picoseconds, and appends `stime -p`, whose report gives the run's WNS. Write `{D}` where a mapping command should take the delay target; `tool: openroad` passes none, so `{D}` is empty there.
- Write the script on one line, with commands separated by `;` and no double quotes. A multi-line value or a double quote is a configuration error. Yosys replaces commas with spaces.
- `strash; dretime; map {D}` is the script of `abc -fast`. It maps a large flat design much faster than the default, at some cost in quality.
- `abc-args` applies only to unmapped `tool: yosys` runs, as `abc <abc-args>`. A mapped run ignores it and warns `synth.abc_args_ignored`.
- `synth` runs Yosys' generic `abc`, whose script includes `dc2`, before the mapped-run ABC step. Add `-noabc` to `synth-args` to keep log-depth carry networks.

## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only `(* blackbox *)` RTL declaration for the frontend.

With both files supplied, the OpenROAD stage keeps the macro's area and timing arcs. Without them rtl_buddy generates a port-only stub, and the reported PPA does not represent the macro.

## Run synthesis

```bash
rb synth --list -c synth/block/synth.yaml
rb synth block_openroad -c synth/block/synth.yaml
rb synth-regression -c synth_regression.yaml --reg-level 1000
```

A regression manifest lists config files relative to itself:

```yaml
rtl-buddy-filetype: synth_reg_config
synth-configs:
  - synth/block_a/synth.yaml
  - synth/block_b/synth.yaml
```

## Interpret results

Mapped runs report gates and area. A constrained Yosys run reports WNS as clock period minus critical-path delay. OpenROAD reports actual WNS and TNS: negative values are violations, and TNS 0 means no endpoint has negative slack.

A Yosys run passes when the process exits 0, the log has no `ERROR:` line, and no correctness gate fires. An OpenROAD run also needs the OpenROAD stage to exit 0 with no `[ERROR ...]` line. Any failed stage reports `FAIL`; read that stage's log first.

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
| `synth_stat.json` | Both | Yosys `stat -json`: per-module cell count and area |
| `phys-model.json`, `phys-manifest.json` | Both | Physical model and its manifest; query with `rb phys` ([Physical Metrics](phys.md)) |

A failed run leaves no netlist. Netlists are deleted at the start of every run, so `rb pnr` and `rb power` never consume a previous run's design and report that `rb synth` must run first. Copy out a netlist before rerunning if you want to keep it.

A power run for the same top in the same artefact directory fills the model's per-instance half; see [Pair the model with a synthesis run](power.md#pair-the-model-with-a-synthesis-run). A re-synthesis keeps those rows only when its netlist is byte-identical to the one the power run read, so after an RTL edit rerun `rb power`.

## Troubleshooting

Each entry is a console message and the action it calls for.

- **`static-functions` finding, run fails:** add `automatic` to the listed declarations, or set `static-functions: warn|allow`.
- **`multiple conflicting drivers` warning(s), run fails:** fix the design so each net has one driver style, or set `conflicting-drivers: allow`.
- **Interface instance could not be bound:** use `frontend: slang`, or accept the fallback with `unresolved-interfaces: warn|allow`.
- **multi-clock SDC, abc constraint set to minimum:** use `tool: openroad` for multi-clock timing, or split into one run per clock domain.
- **no `create_clock` found, or `create_clock -period` did not evaluate:** ABC runs unconstrained or skips that clock. Add a literal clock or restore the `tcl` reader.
- **no Tcl interpreter is reachable, or Tcl refused a line:** the tokenizer reader is in use, so `$variables` and `[expr]` stay unevaluated. Install tkinter.
- **`single_unit` or `best_effort_hierarchy` has no effect:** the frontend is not `slang`. Set `frontend: slang` or remove the option.
- **`abc-args` has no effect on a Liberty-mapped run:** set `abc-script` to change the mapping script; keep `abc-args` for unmapped runs.
- **`tool_overrides.yosys` unknown key ignored:** override keys are snake case; the message lists the accepted ones.
- **OpenROAD synthesis requires LEF files:** set `tech-lef` and `macro-lef` on the `cfg-pdks` entry, or `lef-paths` on the run.
- **OpenROAD synthesis requires a mapped library:** set `platform:` on the run and define the matching `cfg-synth-platforms` entry.
- **`phys-model.json` has no per-module breakdown:** Yosys wrote no readable `stat -json`. The run still passes, but `rb phys module` has no rows for it.
- **Previous run's module rows could not be withdrawn:** the run stops rather than leave stale rows over cleared reports. Fix the error the message names (for example a locked or unwritable artefact directory) and rerun.
