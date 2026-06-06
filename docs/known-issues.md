---
description: Quirks, non-conventional behaviors, and known issues with rtl_buddy, including workarounds for simulator-specific behaviors.
---

# Quirks & Known Issues

The home for rtl_buddy behavior that does not follow convention: quirks, surprising defaults, simulator-specific workarounds, and known limitations. If something tripped you up because it works differently than you'd expect, add it here so the next person — or agent — finds it first.

Keep this page alive. When you hit or introduce a quirk, write it down rather than leaving it in commit history or someone's memory. Use one `##` section per quirk, name it after the behavior, and say what to do about it.

## Instance pRNG seeding with Verilator

Random testing does not behave reliably with Verilator. While Verilator supports multiple random tests with different seeds, tests are not always reproducible even with the same seed.

VCS is recommended for stable random testing due to its hierarchical instance seeding. VCS seeds instantiated modules, process threads, and classes based on their hierarchical names. For stable random seeding with VCS, name your instances explicitly.

If you require reproducible randomized testing on macOS (where VCS is not available), this is a known limitation.

## Hook scripts run at the invocation directory, not the suite

`sweep` and `preproc` hooks execute via `exec()` inside the `rb` process and share its working directory, which is `invocation_cwd` — your shell's cwd — not the suite directory. Resolve suite-local inputs and outputs from the injected `suite_dir` / `artifact_dir` variables, never from `os.getcwd()`.

The footgun is a hook that delegates to a **third-party generator** which writes its outputs relative to `os.getcwd()` and offers no output-directory argument. Under v4, `regression` chdir'd into each suite so such a generator dropped files under the suite; under v5 it drops them under the invocation directory instead (e.g. polluting the repo root when running `regression` from there). The failure is silent: generation "succeeds", but the test fails much later at sim time with a generic `cannot open <suite_dir>/<gen_dir>/<file>`. It only reproduces when `invocation_cwd != suite_dir`, so it passes when run from inside the suite and fails under `regression` from the repo root.

Wrap the generator call in a `chdir` to the suite (restore afterwards):

```python
prev = os.getcwd()
os.chdir(suite_dir)
try:
    gen_dir = third_party_generate(...)   # writes relative to cwd
finally:
    os.chdir(prev)
```

See [Migrations: v4 to v5](migrations.md#v4-to-v5) for the full behavior change.

## Compilation-unit `bind` under `frontend: verilog` elaborates zero formal cells

A property file that binds its checker module at compilation-unit scope (`bind dut dut_props u_props (...);` at the top level of the file, outside any module) does **not** error under the default `frontend: verilog` — but yosys's native verilog frontend never resolves the bind. The checker is stored as `$abstract` and removed as unused before any assertion cell is generated, so the proof runs against **zero** formal cells. With no guard, sby would prove nothing and report a silent **PASS** — a false pass indistinguishable from a real one.

`rb fpv` guards the primary proof against this: when a verification lists `properties:`, the generated sby script asserts that at least one formal cell (`$assert` / `$assume` / `$cover` / `$live` / `$check`) survives `prep`. A suite that elaborates none fails loud with:

> sby reported ERROR (…) — zero formal cells elaborated: the property set produced no assert/assume/cover cells, so the proof would otherwise have passed vacuously (frontend='verilog' cannot resolve a compilation-unit-scope `bind`; set `frontend: slang` for bind-based property modules)

The fix is to set `frontend: slang` on that verification: yosys-slang reads all files in one `read_slang --top` invocation, so a compilation-unit-scope bind resolves and the asserts elaborate. Inline-assertion suites (`properties: []`, with assertions in the DUT) are not bind-based and are intentionally not guarded. See [Choosing a frontend](concepts/fpv.md#choosing-a-frontend).

## VCS hierarchical seed file

When using VCS with hierarchical instance seeding (`-xlrm hier_inst_seed`), VCS writes a `HierInstanceSeed.txt` file in the simulation directory after the run. `rtl_buddy` looks for this file to record the seed for reproducibility.

If the file is missing, a `sim.hier_seed_missing` warning is emitted in the log and the seed is not recorded, but the test result is not affected.

Ensure your VCS compile-time flags include `-xlrm hier_inst_seed` and that the simulation directory is writable so VCS can write the file.
