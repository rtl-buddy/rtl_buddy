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

Use `yosys` for technology-independent synthesis or a quick mapped result. Use `openroad` when timing must respect multiple clocks. Both backends use Yosys for elaboration and mapping.

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

`tool: openroad` also needs `openroad` on `PATH` (`openroad -version`). On macOS, follow `tools/openroad/SETUP_OSX.md` in the project template.

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

Paths resolve from `synth.yaml`. The synthesis top is the model's root module: its `top:` in `models.yaml`, defaulting to the model name. `platform` enables Liberty mapping; the OpenROAD backend also needs LEF files.

Use `lef-paths` and `lib-paths` for block-specific hard macros, and `tool_overrides` for backend options with no portable equivalent. All fields are in [YAML Formats: synth.yaml](../reference/yaml.md#synthyaml).

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

Paths resolve from `root_config.yaml`. The Yosys backend needs Liberty. The OpenROAD backend needs Liberty plus technology and macro LEF, and fails before running the tool if a LEF is missing. Keep large PDK files untracked and provide a fetch script.

OpenROAD `strategy` is `AREA`, `TIMING`, `TIMING_ANNEAL`, or `TIMING_GENETIC`. `AREA` reports the initial mapping; the timing strategies request OpenROAD resynthesis.

Synthesis reads only a PDK's Liberty corner, LEFs and `dont-use-cells`:

- **Nangate45**: one Liberty file, `NangateOpenCellLibrary_typical.lib`. The template's `synth/demo_tiny_alu_subsys/download_pdk.sh` fetches it.
- **sky130hd**: one Liberty per corner, plus a `dont-use-cells` list for the probe and `lpflow` cells. See the template's `sky130hd` entry and `synth/demo_tiny_alu_subsys_hier/download_pdk.sh`.
- **ASAP7**: not validated. Its cells are split across gzipped Liberty files per cell group, Vt and corner, and `corners:` takes one file per corner. Merge a corner's files into one Liberty first.

P&R-side PDK notes are in [Place-and-Route: PDK setup notes](pnr.md#pdk-setup-notes).

A PDK's `dont-use-cells` list excludes cells from mapping. Each pattern becomes a `-dont_use` argument to Yosys `dfflibmap` and `abc`, and the OpenROAD backend also applies `set_dont_use` before resynthesis. [P&R](pnr.md#tune-the-process-dependent-steps) reads the same list, so two runs that exclude different cells count as two experiments. A synth platform's own `dont-use-cells` is appended after the PDK's.

## Use SDC constraints

A Yosys run extracts `create_clock` periods from the SDC, passes the shortest to ABC, and warns when several clocks make that an approximation. An OpenROAD run loads the complete SDC and reports actual worst and total negative slack, so use it for multi-clock timing decisions.

## How the SDC is read

`rb` reads SDC and XDC text with one of two readers. `rb tool-check` names the active one under `In-process readers`.

- **`tcl`**: a safe Tcl interpreter, so line continuations, braces, nested collections, `$variables` and `[expr ...]` read as they do in Vivado and OpenSTA. The safe interpreter has no `exec`, `open`, `file`, `socket`, `cd`, `glob` or `source`, and a resource limit stops runaway loops. `source` includes are not followed; read the included file directly.
- **`tokenizer`**: the fallback when no Tcl interpreter can start, for example a Python without `_tkinter`. It splits words but evaluates nothing, so `-period $p` or `-period [expr ...]` is reported as unevaluated. Restore the `tcl` reader by installing tkinter (`uv python install --managed-python`, `brew install python-tk@<X.Y>`, or `dnf install python3-tkinter`).

The interpreter runs in a short-lived worker process, not inside `rb`. Loading `tkinter` in `rb` itself can leave a later fork and exec hanging on macOS.

Either reader returns syntax only. `get_ports`, `get_cells` and `-filter` collections stay opaque names, because resolving them needs a linked netlist that OpenROAD or Vivado owns.

Set `RTL_BUDDY_CONSTRAINT_READER=tokenizer` to force the fallback. Set `=tcl` to require the interpreter and fail if no worker can start one.

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

Build the plugin against the same Yosys installation. `plugin-path` resolves from the project root. If it is omitted, `RTL_BUDDY_SLANG_PLUGIN` is used; that value must be an absolute path, though `~` is expanded.

An unknown `frontend` or a missing slang plugin fails the run as a configuration error (exit 2) before any gate runs. It is not a synthesis `FAIL`.

- `single-unit: true` shares preprocessor definitions across source files. Set it only when the sources rely on that.
- `best-effort-hierarchy: true` forwards `read_slang --best-effort-hierarchy` and keeps module instances as hierarchy. yosys-slang otherwise inlines every instance and drops `(* keep_hierarchy *)`. Set it when mapping needs the hierarchy, for example when a flattened cone of `keep_hierarchy` multipliers stalls ABC. Reports still include the hierarchy roll-up.
- Both options apply only to slang. With the Verilog frontend they are ignored with a warning. A non-Boolean value is fatal.

To change the elaboration stage for one run, use `tool_overrides`:

```yaml
tool_overrides:
  yosys:
    frontend: slang
    plugin_path: ../yosys-slang/build/slang.so
    single_unit: true
    best_effort_hierarchy: true
```

Fields under `cfg-synth-tools.opts` use kebab case (`plugin-path`). Fields under `tool_overrides.yosys` use snake case (`plugin_path`), and unknown keys are warned about and ignored. The override key stays `yosys` when the backend is `openroad`, because Yosys owns elaboration.

## Gate static-lifetime subroutines

A `function` or `task` declared at module, interface, package, program or compilation-unit scope without `automatic` has static lifetime, so each formal argument is one shared storage location. Simulation is unaffected. yosys-slang lowers the declaration literally, so two calls in one combinational process alias their arguments and the netlist is wrong with no error or warning.

Before Yosys starts, rtl_buddy scans the filelist's sources and the headers they `` `include ``, and reports each declaration as `file:line: function <name>`:

```yaml
cfg-synth-tools:
  - name: yosys
    tool: yosys
    opts:
      static-functions: error        # error | warn | allow
      conflicting-drivers: error     # error | allow
      unresolved-interfaces: warn    # error | warn | allow
```

- `error` fails the run before Yosys starts. It is the default with `frontend: slang`.
- `warn` logs one warning per finding and records `static_function_findings` in the machine output. It is the default with `frontend: verilog`, which inlines per call site, so the result is correct but not portable.
- `allow` skips the scan.

The scan reports a declaration, not an actual alias, so a subroutine with a single call site also fails. To stage a fix, set `static-functions: warn` while adding the keyword:

```systemverilog
function automatic ptr_t inc(input ptr_t p);
  return p + 1;
endfunction
```

Verible's `explicit-function-lifetime` rule, run through `rb lint` and `cfg-verible`, covers testbench and non-synthesisable sources. The remaining gates in the block above are described in [Gate conflicting drivers](#gate-conflicting-drivers) and [Gate unbound interface instances](#gate-unbound-interface-instances). All three apply to the Yosys elaboration stage of both backends, and an unrecognized value is fatal.

## Static-function scan coverage

The scan is a tokenizer, not an elaborator.

- **Includes**: `` `include `` is followed against the including file's directory, then the filelist's `+incdir+` entries. Each inclusion is scanned in its own context, so a header included from a class is exempt there and reported when included from a module. A declaration is reported once. A chain deeper than 1024 fails the run and names the chain's tail.
- **Conditionals**: `` `ifdef ``, `` `ifndef ``, `` `elsif ``, `` `else `` and `` `endif `` are evaluated on definedness, updated by `` `define ``, `` `undef `` and `` `undefineall ``. `` `if `` expressions are not evaluated.
- **Attributes**: `(* ... *)` is ignored.
- **Exempt**: class methods (including `function int C::f(...)`), `extern` and `pure virtual` prototypes, DPI imports and exports, and any scope declared `automatic`.

Limits, in both directions:

| Limit | Effect |
| --- | --- |
| Macro bodies are skipped at their `` `define `` | A declaration produced by a macro is never reported |
| `-y` library directories are not scanned | Declarations there are missed |
| An unresolvable `` `include `` is skipped (DEBUG log) | That header's declarations are missed; the run does not fail |
| Scope nesting is tracked by keyword pairing | Pathological legal code can change which declarations are exempt |

## Preprocessor definitions in the scan

The macro table starts with the filelist's `+define+` entries, then the run's `defines:`, then the frontend's own macros. `read_verilog` predefines `SYNTHESIS` and `YOSYS`. `read_slang` predefines `SYNTHESIS` and slang's built-ins but not `YOSYS`. So a `` `ifndef YOSYS `` helper is reported only under `frontend: slang`.

- A bare `+define+X` takes the value the selected frontend gives a valueless `-D`. Tools disagree on it (Verilator and `read_verilog` use empty, Icarus and slang use `1`), so write `+define+X=1` when a value is meant.
- When `defines:` overrides a filelist entry, one `synth.filelist_defines_overridden` warning names both. It fires when the values differ, and when the filelist entry is bare (no value) and `defines:` sets any value, because the tools disagree on what a bare macro means. Simulation then uses the filelist value and synthesis the `synth.yaml` one; drop one to make them agree.
- A filelist `+define+` value containing whitespace is fatal, because a Yosys script line cannot carry it.
- After `` `undefineall ``, slang keeps command-line macros and `read_verilog` does not. A `` `ifndef `` on a `defines:` macro can therefore compile under `verilog` and not under `slang`.
- With `single-unit: false` (default) the macro table is re-seeded for each source file. `single-unit: true` shares it. A header always shares its includer's table.

## Gate conflicting drivers

When one net is driven from both a combinational and a clocked process, for example through aliased static-function arguments, it folds to `x` and takes its register and everything downstream with it. Yosys reports this only as a `multiple conflicting drivers` warning and exits 0.

`conflicting-drivers: error` (default) fails the run and names the count and log path. Set `allow` only when the warnings are understood.

A tristate bus produces the same warning, one per bit. A warning whose drivers are all `$tribuf` or `$_TBUF_` cells and module ports is not counted. A warning with any other driver, such as a flop or a process action, fails the run.

## Gate unbound interface instances

`read_verilog` cannot bind a SystemVerilog interface instance to a child module's interface port. It warns ``Could not find interface instance for `<instance>' in `<module>'`` and exits 0. The interface's members survive as `<instance>.<member>` wires, but the instance's own port connections are dropped:

```systemverilog
interface bus_if (input logic clk);
  logic [7:0] data;
  logic       vld;
  modport src (input clk, output data, vld);
  modport dst (input clk, input  data, vld);
endinterface

module top (input logic clk, ...);
  bus_if b (.clk(clk));          // .clk is dropped
  producer u_p (.b(b), ...);     // both children clock off an
  consumer u_c (.b(b), ...);     // undriven \b.clk
endmodule
```

Here `\b.data` and `\b.vld` still connect the children, but `\b.clk` is undriven and every flop in the subtree loses its clock. `frontend: slang` binds the instance correctly and shows neither the warning nor the disconnect.

`unresolved-interfaces` gates that warning:

- `warn` (default) logs one `synth.unresolved_interface` per instance, records `unresolved_interfaces` in the machine output, and passes. The fallback is correct for interfaces with no ports of their own.
- `error` fails the run and drops the netlist, so `rb pnr` and `rb power` cannot consume it. Use it in projects that give interfaces ports.
- `allow` silences the warning.

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

Select one with `effort: quick` in the run, or on the command line:

```bash
rb synth sandbox_openroad --effort quick
rb synth-regression --effort accurate
```

Precedence is the run's `tool_overrides`, then the selected effort, then `cfg-synth-tools`. Without a configured or selected effort, the built-in `standard` behavior applies.

`openroad.run: false` skips OpenROAD and returns the Yosys result. `pre-sta-tcl` is raw Tcl run before STA. Test it on a small design, because syntax errors appear only at runtime.

The OpenROAD stage uses one thread unless the entry sets `threads:` to a positive integer or `auto` (the CPUs of the current allocation). This matters most for a `pre-sta-tcl` that runs global placement. See [OpenROAD threads](pnr.md#openroad-threads). It does not affect the Yosys stage.

## Synthesize hard macros

For each hard macro:

1. Add its physical LEF to `lef-paths`.
2. Add its timing Liberty to `lib-paths`.
3. Provide a port-only RTL `(* blackbox *)` declaration for the frontend.

When the macro exists in the supplied LEF or Liberty, the OpenROAD stage uses it instead of generating a Verilog stub, so its area and timing arcs are kept. With no physical or timing master, rtl_buddy generates a port-only stub and the reported PPA does not represent the macro.

## Run synthesis

```bash
rb synth --list -c synth/block/synth.yaml
rb synth block_openroad -c synth/block/synth.yaml
rb synth -c synth/block/synth.yaml
rb synth-regression -c synth_regression.yaml
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

A Yosys run passes when the process exits 0, the log has no `ERROR:` line, and no correctness gate fires. An OpenROAD run also needs the OpenROAD stage to exit 0 with no `[ERROR ...]` line. Any failed stage reports `FAIL`.

When a run fails, read the failing stage's log first. Missing tools, plugin paths, Liberty or LEF inputs are configuration failures: fix the path or installation and rerun the named synthesis.

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
| `phys-model.json` | Both | Physical model: per-module rows plus design totals |
| `phys-manifest.json` | Both | Which physical artefacts this run produced, and where |
| `phys-publish.lock` | Both | Held while the model and manifest are rewritten; empty otherwise |

Query the model with `rb phys`; see [Physical Metrics](phys.md).

A failed run leaves no netlist. The netlists are deleted at the start of every run, before the tool is looked up, so `rb pnr` and `rb power` never consume a previous run's design. They report that `rb synth` must run first. Copy out a netlist you want to keep before rerunning.

The physical model is a by-product, not a gate. If Yosys writes no readable `stat -json`, the model keeps the design totals, its `modules` block is `null`, a warning says so, and the run still passes.

A power run for the same top in the same artefact directory fills the model's per-instance half instead of replacing it. See [Pair the model with a synthesis run](power.md#pair-the-model-with-a-synthesis-run). A re-synthesis keeps those rows only when its netlist is byte-identical to the one the power run read. After an RTL edit the rows are dropped (`instances: null`); rerun `rb power` to measure the new netlist. Concurrent publishers take `phys-publish.lock` in turn, so both halves survive whichever finishes first.
