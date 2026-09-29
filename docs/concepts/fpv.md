---
description: Configure and run SymbiYosys formal verification, choose a SystemVerilog frontend, inspect proof quality, and debug failures.
---

# Formal property verification

`rb fpv` generates a SymbiYosys (sby) configuration from a model filelist, optional constraints and SystemVerilog properties, runs it, and reports an overall verdict, logs and a counterexample VCD when one exists.

## Install the formal toolchain

Install `sby` and at least one solver, then run `rb tool-check --required-for fpv`. The [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) bundles Yosys, SymbiYosys and solvers. Otherwise follow the upstream SymbiYosys instructions and put a solver such as Yices, Z3, Boolector or ABC on `PATH`.

Only the `sby` backend is supported. If it is not on `PATH`, set its absolute path in `cfg-fpv-tools`.

## Configure `fpv.yaml`

Each entry names a model, a proof mode and the property inputs:

```yaml
rtl-buddy-filetype: fpv_config

verifications:
  - name: demo_fpv_fifo
    tool: sby
    model: demo_fifo
    model_path: ../../design/demo_fifo/models.yaml
    top: demo_fifo
    constraints: shared_clock_reset.sv
    properties: [demo_fifo_props.sv]
    mode: bmc
    depth: 32
    engines: [smtbmc yices]
    reglvl: 1000
```

- Paths are relative to `fpv.yaml`.
- Modes are `bmc`, `prove`, `cover` and `live`.
- `top` defaults to the model's root module (its `top:` in `models.yaml`, which defaults to the model name), `depth` to 20 and `engines` to `smtbmc yices`.
- `properties` may be omitted when assertions live in RTL under `` `ifdef FORMAL ``.

Optional controls: `params` (top-level parameter overrides), `tool_overrides` (`timeout`, `extra_args`), `frontend: verilog|slang`, `coi` and `vacuity` toggles, `covers` for [spec traceability](spec-traceability.md), and `xfail` / `xfail_strict` for [expected failures](expected-failures.md). See [YAML formats](../reference/yaml.md) for the schema.

## Configure SymbiYosys and solvers

Project-wide tool settings go in `root_config.yaml`:

```yaml
cfg-fpv-tools:
  - name: sby
    tool: sby
    opts:
      timeout: 600
      extra-args: ""
      solver-versions:
        yices: "2.6.4"
        z3: "4.13.0"
```

`solver-versions` pins exact versions for `yices`, `z3`, `boolector`, `bitwuzla`, `btormc` and `abc`. Before a run, rtl_buddy probes every pin and fails once listing all mismatches. Resolved versions are logged.

## Choose the SystemVerilog frontend

Use the simplest frontend that elaborates your properties.

| Frontend | Use when | Requirements and limits |
|---|---|---|
| `verilog` | Immediate assertions and simple concurrent assertions | Built into Yosys, no plugin. Does not correctly support implications, sequence operators or compilation-unit `bind` |
| `slang` | `bind`, `|->`, `|=>`, sequences, or richer SystemVerilog | Needs the `yosys-slang` plugin, set by `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN` |

- With `frontend: verilog`, a property set that elaborates no assert, assume or cover cells fails instead of reporting a vacuous PASS.
- For concurrent SVA under slang, use a build that lowers the constructs you need; the [rtl-buddy yosys-slang branch](https://github.com/rtl-buddy/yosys-slang/tree/rtl-buddy) supports the property flow.
- Accepted sampled-value functions and sequence constructs vary by build. Probe your build before writing a large property set, one construct at a time, because one rejected construct aborts the whole read:

```systemverilog
module probe(input logic clk, a, b);
  a1: assert property (@(posedge clk) a |-> b);
  a2: assert property (@(posedge clk) $past(a) |-> b);
  a3: cover property (@(posedge clk) a && b);
endmodule
```

## Understand input processing

rtl_buddy reads inputs in this order: design sources from the model filelist, `constraints`, then `properties`. Filelist include directories and defines go to the selected frontend. The proof, vacuity pass and COI analysis share the same sources, defines, frontend and parameters.

Both frontends define `FORMAL` and not `SYNTHESIS`. Defines are normalized:

- A user `+define+FORMAL` is dropped with a warning.
- For repeated names the last value wins; identical repeats are deduplicated.
- A value containing whitespace is dropped with a warning, because Yosys script tokenization cannot represent it.

Slang still preprocesses `synthesis translate_off` regions, so includes and macros there must resolve.

## Run formal verification

```bash
rb fpv
rb fpv demo_fpv_fifo -c fpv/demo_fifo/fpv.yaml
rb fpv -c fpv/demo_fifo/fpv.yaml --list
rb fpv-regression -c fpv_regression.yaml -l 1000
```

The summary shows the overall verdict, mode, depth, engines, engine result mix, runtime and counterexample path. sby gives no per-assertion verdicts, so per-engine status is the finest granularity.

A run passes when `sby_workdir/status` contains `PASS`, or when sby exits 0 without a status file. `FAIL`, `UNKNOWN`, `ERROR` or a nonzero exit is a failed run. A regression entry above the selected level is `SKIP`.

## Check proof quality

A green verdict can come from unreachable antecedents, unused logic or over-strong assumptions. Keep the default analyses on and run a negative check.

- **Cone of influence (COI).** With `coi: true` (default), Yosys reports the fraction of design cells in the backward cone of at least one assertion, with per-module detail in machine results. If Yosys is missing or the analysis errors, it warns and leaves COI unavailable without changing the verdict.
- **Dead assumptions.** The COI pass counts assumptions whose input logic intersects an assertion cone, excluding clock and reset network edges. A dead assumption is structurally disconnected; a used one is not necessarily semantically necessary. Assume-to-assume chains are not followed to a fixpoint.
- **Vacuity.** For `bmc` and `prove`, checking defaults on; `cover` and `live` default it off; override with `vacuity: false`. rtl_buddy derives a cover for each single-line `|->` or `|=>` antecedent and runs a secondary cover proof. Unreached antecedents are reported vacuous; missing results are unknown. Clocking and same-line `disable iff` are retained; sequence antecedents are treated as boolean reachability conditions.
- **Negative check.** Mutate the RTL, or strengthen a property beyond the design guarantee, and confirm the expected assertion fails. [Mutation testing](mut.md) automates this across a suite.

## Debug `UNKNOWN` from `prove`

`bmc` checks behavior only to `depth`. `prove` uses temporal k-induction, and its induction step may start from unreachable states that satisfy the assumptions and prior assertion hypotheses. An `UNKNOWN` trace is therefore either a real reachable bug or a counterexample to induction.

1. Open the induction trace and decide whether its initial state is reachable.
2. Add invariants that exclude impossible predecessor states or state relationships between pipeline stages.
3. Check environment assumptions: under-constrained ones create false failures, over-constrained ones hide bugs.
4. Raise `depth` only when the design legitimately needs more steps to become inductive; depth-dependent proofs can break under design changes.

All assertions strengthen the induction hypothesis together, so a companion invariant can close another property. Keep each assertion meaningful and validate it independently where practical.

## Constrain reset and keep covers reachable

For a stateful design without initialized registers, constrain reset at the initial cycle in `constraints`:

```systemverilog
module fpv_reset_pin(input logic clk, rst_n);
  logic f_init = 1'b1;
  always_ff @(posedge clk) f_init <= 1'b0;
  assume property (@(posedge clk) f_init |-> !rst_n);
endmodule

bind dut fpv_reset_pin u_reset_pin(.clk(clk), .rst_n(rst_n));
```

This uses `bind`, so it needs `frontend: slang`. If the reset pin makes a cover's target states unreachable, keep covers in a separate verification without the constraint:

```yaml
- name: fifo_assertions
  constraints: reset_pin.sv
  properties: [fifo_asserts.sv]
- name: fifo_covers
  properties: [fifo_covers.sv]
```

Match `disable iff` polarity to the design reset; for an active-low reset use `disable iff (!rst_n)`.

## Avoid frontend-specific property failures

- Use packed checker storage for a variable index. A variable index into an unpacked array fails because the array elaborates as a memory.
- Check that `(* anyconst *)` produces an `$anyconst` cell in your frontend; some builds drop it. See [Known Issues](../known-issues.md#verify-that-anyconst-elaborates).
- Replace unsupported `$past` or `$stable` uses with explicit history registers.
- A slang `bind` can connect checker ports to DUT ports, interface members, internal nets and parameters. Pass checker parameters from the target scope instead of hard-coding them.

## Prove reduced configurations

When the full state space is impractical, use `params` to shrink a width or depth:

```yaml
verifications:
  - name: my_block_proof_k8
    tool: sby
    model: my_block
    top: my_block
    params:
      K: 8
    mode: bmc
    depth: 24
```

- Names must be identifiers. Values are integers, booleans, or strings holding verbatim SystemVerilog literal text, so a string parameter needs embedded quotes: `MODE: '"small"'`.
- Whitespace in values is rejected, as are YAML 1.1 boolean-like keys such as unquoted `on` or `off`.
- The verilog frontend applies overrides with `chparam`; slang applies them with `-G` in `read_slang`. The proof, vacuity and COI passes all use the same values.

A reduced proof establishes only that configuration. Keep a full-size run at a feasible depth if the shipping configuration also needs coverage.

## Inspect artefacts and counterexamples

Output goes to `<fpv.yaml dir>/artefacts/<run>/` (see [Execution Context](execution-context.md)):

| File | Contents |
|---|---|
| `fpv.log` | Full sby output |
| `fpv.f` | Generated stripped and deduplicated filelist |
| `fpv.sby` | Generated SymbiYosys configuration |
| `sby_workdir/status` | Overall verdict |
| `sby_workdir/engine_<N>/logfile.txt` | Engine log |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample, when produced |
| `vacuity_covers.sv`, `vacuity.sby`, `vacuity.log`, `vacuity_workdir/` | Vacuity pass |
| `coi.ys`, `coi.log` | COI and dead-assume analysis |

Open the first available counterexample in the configured Surfer:

```bash
rb wave-fpv demo_fpv_fifo
```

`-c` selects another `fpv.yaml` and `--surfer <name>` overrides platform routing. The command errors if the verification has not run or produced no trace. For sby modes and engines, see the [SymbiYosys reference](https://symbiyosys.readthedocs.io/en/latest/reference.html).
