---
description: Configure and run SymbiYosys formal verification, choose a SystemVerilog frontend, inspect proof quality, and debug failures.
---

# Formal property verification

`rb fpv` generates a SymbiYosys configuration from a model filelist, optional constraints, and SystemVerilog properties. Each verification produces an overall verdict, logs, and a counterexample VCD when available.

## Install the formal toolchain

Install `sby` and at least one solver, then run `rb tool-check --required-for fpv`. The [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) bundles Yosys, SymbiYosys, and solvers. Otherwise follow the upstream SymbiYosys installation instructions and put a solver such as Yices, Z3, Boolector, or ABC on `PATH`.

Only the `sby` backend is supported. Configure its executable as an absolute path in `cfg-fpv-tools` when it is not on `PATH`.

## Configure `fpv.yaml`

Each entry names a model, proof mode, and property inputs:

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

Paths are relative to `fpv.yaml`. `top` defaults to the model's root module (its `top:` in `models.yaml`, itself defaulting to the model name), `depth` to 20, and `engines` to `smtbmc yices`. `properties` may be omitted when assertions live in RTL under `` `ifdef FORMAL ``. Modes are `bmc`, `prove`, `cover`, and `live`.

Optional run controls include:

- `params` for top-level parameter overrides;
- `tool_overrides` for `timeout` or `extra_args`;
- `frontend: verilog|slang`;
- `coi` and `vacuity` analysis toggles;
- `covers` for [spec traceability](spec-traceability.md);
- `xfail` or `xfail_strict` for [expected failures](expected-failures.md).

See [YAML formats](../reference/yaml.md) for the complete schema.

## Configure SymbiYosys and solvers

Declare project-wide tool settings in `root_config.yaml`:

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

`solver-versions` pins exact versions. rtl_buddy probes all pins before a run and fails once with every mismatch; resolved versions are logged. Supported pin names are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, and `abc`.

<a id="choosing-a-frontend"></a>

## Choose the SystemVerilog frontend

Use the simplest frontend that elaborates the property set:

| Frontend | Use when | Requirements and limits |
|---|---|---|
| `verilog` | Immediate assertions and simple concurrent assertions | Built into Yosys; no plugin. Does not support implications, sequence operators, or compilation-unit `bind` correctly |
| `slang` | `bind`, `|->`, `|=>`, sequences, or richer SystemVerilog | `yosys-slang` plugin configured by `cfg-fpv-tools[].opts.plugin-path` or `RTL_BUDDY_SLANG_PLUGIN` |

With `frontend: verilog`, a listed property set that elaborates no assert, assume, or cover cells fails instead of reporting a vacuous PASS. When using slang for concurrent SVA, use a build that lowers the constructs you need; the [rtl-buddy yosys-slang branch](https://github.com/rtl-buddy/yosys-slang/tree/rtl-buddy) supports the rtl_buddy property flow. Probe your installed build before authoring a large property set because accepted sampled-value functions and sequence constructs vary by build.

```systemverilog
module probe(input logic clk, a, b);
  a1: assert property (@(posedge clk) a |-> b);
  a2: assert property (@(posedge clk) $past(a) |-> b);
  a3: cover property (@(posedge clk) a && b);
endmodule
```

Test one uncertain construct at a time; one rejected construct aborts the whole read.

## Understand input processing

rtl_buddy resolves the model through the normal filelist loader and reads inputs in this order: design sources, `constraints`, then `properties`. Filelist include directories and defines are passed to the selected frontend. The proof, vacuity pass, and COI analysis use the same sources, defines, frontend, and parameters.

Both frontends define `FORMAL` and do not define `SYNTHESIS`. Define handling is normalized for consistent elaboration:

- User `+define+FORMAL` is dropped with a warning.
- Repeated names use the last value; identical repeats are deduplicated.
- Values containing whitespace are dropped with a warning because Yosys script tokenization cannot represent them safely.

Slang still preprocesses `synthesis translate_off` regions, so includes and macros there must resolve.

## Run formal verification

```bash
rb fpv
rb fpv demo_fpv_fifo -c fpv/demo_fifo/fpv.yaml
rb fpv -c fpv/demo_fifo/fpv.yaml --list
rb fpv-regression -c fpv_regression.yaml -l 1000
```

The summary reports the overall verdict, mode, depth, engines, engine result mix, runtime, and counterexample path. SymbiYosys does not provide structured per-assertion verdicts, so rtl_buddy reports per-engine status as the finest granularity.

A run is PASS when `sby_workdir/status` contains `PASS`, or when sby exits 0 without a status file. `FAIL`, `UNKNOWN`, `ERROR`, or a nonzero process exit is a failed run. A regression entry above the selected level is SKIP.

<a id="cone-of-influence-coverage"></a>
<a id="dead-assume-detection"></a>
<a id="vacuity-covers"></a>

## Check proof quality

A green verdict can still result from unreachable antecedents, unused logic, or over-strong assumptions. Keep the default analyses enabled and perform a negative check.

**Cone of influence.** With `coi: true` (default), rtl_buddy uses Yosys to report the design-cell fraction in the backward cone of at least one assertion, including per-module detail in machine results. Missing Yosys or an analysis error warns and leaves COI unavailable without changing the primary verdict.

**Dead assumptions.** The COI pass also counts assumptions whose input logic intersects an assertion cone. Clock and reset network edges are excluded. A reported dead assumption is structurally disconnected, but an assumption reported as used is not necessarily semantically necessary. Assume-to-assume chains are not followed to a fixpoint.

**Vacuity.** For `bmc` and `prove`, vacuity checking defaults on. rtl_buddy derives a cover for each single-line `|->` or `|=>` antecedent and runs a secondary cover proof. Unreached antecedents are reported as vacuous; missing results are unknown. Clocking and same-line `disable iff` clauses are retained. Sequence antecedents are treated as boolean reachability conditions. Override with `vacuity: false`; `cover` and `live` default it off.

**Negative check.** Deliberately mutate the RTL or strengthen a property beyond the design guarantee and confirm that the expected assertion fails. Use [mutation testing](mut.md) to automate this across a suite.

## Write properties that prove

`bmc` checks behavior only to `depth`. `prove` uses temporal k-induction and may start its induction step from unreachable states that satisfy the assumptions and prior assertion hypotheses. An `UNKNOWN` trace may therefore be a real reachable bug or a counterexample to induction.

When `prove` returns `UNKNOWN`:

1. Open the induction trace and determine whether its initial state is reachable.
2. Add invariants that exclude impossible predecessor states or express required relationships between pipeline stages.
3. Check environment assumptions; an under-constrained environment creates false failures, while an over-constrained one hides bugs.
4. Increase depth only when the design legitimately needs more steps to become inductive. A depth-dependent proof can be fragile across design changes.

All assertions strengthen the induction hypothesis together. A companion invariant can close another property, but keep each assertion meaningful and validate it independently where practical.

For stateful designs without initialized registers, constrain reset at the initial cycle. Put the reusable environment assumption in `constraints`:

```systemverilog
module fpv_reset_pin(input logic clk, rst_n);
  logic f_init = 1'b1;
  always_ff @(posedge clk) f_init <= 1'b0;
  assume property (@(posedge clk) f_init |-> !rst_n);
endmodule

bind dut fpv_reset_pin u_reset_pin(.clk(clk), .rst_n(rst_n));
```

This example requires `frontend: slang` because it uses `bind`. Keep unconstrained reachability covers in a separate verification if the reset pin makes their target states unreachable:

```yaml
- name: fifo_assertions
  constraints: reset_pin.sv
  properties: [fifo_asserts.sv]
- name: fifo_covers
  properties: [fifo_covers.sv]
```

Match `disable iff` polarity to the design reset. For an active-low reset, use `disable iff (!rst_n)`.

## Avoid frontend-specific property failures

- Prefer packed checker storage when a property uses a variable index. A variable index into an unpacked array fails because the array elaborates as a memory.
- Verify that `(* anyconst *)` produces an `$anyconst` cell in your frontend; some builds drop it. See [Known Issues](../known-issues.md#verify-that-anyconst-elaborates).
- Replace unsupported `$past` or `$stable` uses with explicit history registers when equivalent.
- A slang `bind` may connect checker ports to DUT ports, interface members, internal nets, and parameters. Checker parameters should be passed from the target scope rather than hard-coded.

<a id="reduced-configuration-proofs"></a>

## Prove reduced configurations

Use `params` to reduce a width or depth when the full state space is impractical:

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

Names must be identifiers. Values may be integers, booleans, or strings containing verbatim SystemVerilog literal text. String-valued parameters need embedded quotes, for example `MODE: '"small"'`. Whitespace in values is rejected, as are YAML 1.1 boolean-like keys such as unquoted `on` or `off`.

The verilog frontend applies overrides with `chparam`; slang applies them during `read_slang` with `-G`. The same values apply to the primary proof, vacuity, and COI passes.

A reduced proof establishes only that configuration. Keep a full-size run at a feasible depth when the shipping configuration also needs coverage.

## Inspect artefacts and counterexamples

Per-run output is anchored to the selected config at `<fpv.yaml dir>/artefacts/<run>/`:

| File | Contents |
|---|---|
| `fpv.log` | Full sby output |
| `fpv.f` | Generated stripped and deduplicated filelist |
| `fpv.sby` | Generated SymbiYosys configuration |
| `sby_workdir/status` | Overall verdict |
| `sby_workdir/engine_<N>/logfile.txt` | Engine log |
| `sby_workdir/engine_<N>/trace.vcd` | Counterexample when produced |
| `vacuity_covers.sv`, `vacuity.sby`, `vacuity.log`, `vacuity_workdir/` | Vacuity pass outputs |
| `coi.ys`, `coi.log` | COI and dead-assume analysis |

See [Execution Context](execution-context.md) for artefact anchoring.

Open the first available counterexample in the configured Surfer instance:

```bash
rb wave-fpv demo_fpv_fifo
```

Use `-c` to select another `fpv.yaml` and `--surfer <name>` to override platform routing. The command errors when the verification has not run or produced no trace.

For SymbiYosys modes and engines, see the [SymbiYosys reference](https://symbiyosys.readthedocs.io/en/latest/reference.html).
