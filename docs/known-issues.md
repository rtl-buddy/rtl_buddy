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

## VCS hierarchical seed file

When using VCS with hierarchical instance seeding (`-xlrm hier_inst_seed`), VCS writes a `HierInstanceSeed.txt` file in the simulation directory after the run. `rtl_buddy` looks for this file to record the seed for reproducibility.

If the file is missing, a `sim.hier_seed_missing` warning is emitted in the log and the seed is not recorded, but the test result is not affected.

Ensure your VCS compile-time flags include `-xlrm hier_inst_seed` and that the simulation directory is writable so VCS can write the file.

## VCS VPD traces convert at profile time, with two fallbacks

`rb axi-profile run` ingests FST and VCD natively, but a VCS debug run dumps
Synopsys-proprietary `vcdplus.vpd` (`$vcdpluson`), so the wrapper converts it
on the fly — `vpd2vcd` → temporary VCD → `vcd2fst` → cached `vcdplus.fst`
next to the VPD (skipped when the cache is newer). Two non-obvious behaviors
inside that flow:

- **`vpd2vcd` is invoked with `-full64` first, bare second.** 64-bit-only VCS
  installs ship no 32-bit `vpd2vcd.exe`, so the bare wrapper fails outright
  with `… linux/bin/vpd2vcd.exe: No such file or directory`; older 32-bit
  installs may not accept `-full64`. The wrapper tries both, in that order.
  Both attempts (and their output) are recorded in
  `artefacts/axi/<test>/vpd-convert.log`.
- **Missing `vcd2fst` degrades, not fails.** Without GTKWave's `vcd2fst` on
  PATH the intermediate VCD is kept as `vcdplus.vcd` and ingested directly —
  results are identical, but the file is roughly 15x larger than the FST
  (the AXI 2x2 demo: 15.8M VCD vs 1.1M FST vs 376K VPD). A WARNING
  (`axi_profile_run.vcd2fst_missing`) flags it; install GTKWave to get the
  compact cache.

The cached conversion artifacts live next to the VPD in the *test* artefact
dir (`artefacts/<test>/`), not in axi-profile's own root — deliberate, so the
cache-invalidation mtime comparison and the trace stay co-located, and `rb
wave` conventions can open the converted FST from the standard place.
