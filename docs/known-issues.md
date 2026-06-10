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

## Verible resolves from PATH when `cfg-verible.path` lacks the binaries

`rb verible` resolves each executable in a fixed precedence: the configured `cfg-verible.path` directory wins **when it actually contains the binary**, otherwise rtl_buddy falls back to whatever is on `PATH`, and only as a last resort returns the configured join (so a genuine "not found" still names the expected directory).

This means a site can expose Verible through its environment (a `module load`, or a sourced setup script that puts `verible-verilog-*` on `PATH`) and leave `cfg-verible.path` at the committed default — no per-checkout edit to `root_config.yaml`. The flip side: if your configured directory does not contain the binaries but a *different* Verible is on `PATH`, that PATH copy is used silently. If `rb verible` seems to run a different build than the one you configured, check `PATH` — the configured directory only takes precedence when the binary is present there. This mirrors how `cfg-surfer` already resolves its executable.

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

## pywellen must keep the random-access Waveform API (`<0.25`)

`rb wave` value annotations and `rb saif` read traces through pywellen's
random-access `Waveform` API (`hierarchy`, `get_signal`,
`get_signal_from_path`), which pywellen 0.25.0 removed in its streaming
rewrite. The dependency is therefore bounded to `pywellen >= 0.20.0, <0.25`
([#263](https://github.com/rtl-buddy/rtl_buddy/issues/263)).

If an environment force-resolves a newer pywellen anyway (e.g. a manually
upgraded venv), both tools fail loudly with a `FatalRtlBuddyError` naming the
missing API and the fix (`pywellen.api_missing`) — `rb wave` checks at launch,
before Surfer starts. They do **not** degrade to blank annotations or partial
output. Porting to the streaming API is tracked in #263; the bound, the
runtime guard (`tools/pywellen_compat.py`), and the CI surface test
(`tests/test_surfer_wcp.py::TestPywellenApiSurface`) are lifted together when
that lands.

## The artefact-tree lock is per tree, and its lock file stays behind

Every artifact-writing command takes an exclusive `flock` on
`<artifact_root>/.rtl-buddy.lock` and **fails immediately** if another
rtl-buddy process holds it ([#73](https://github.com/rtl-buddy/rtl_buddy/issues/73)) —
see [Execution Context](concepts/execution-context.md#one-run-per-artefact-tree).
Three consequences that can surprise:

- **Contention is per artefact tree, not per command family.** When
  `tests.yaml`, `cdc.yaml`, `synth.yaml`, etc. share a suite directory, they
  share one `artefacts/` — so `rb hier` or `rb cdc` during a long `rb test`
  in the same suite fails loud, even though their output subtrees are
  disjoint. Deliberate: one coarse lock per tree, no partial-overlap edge
  cases. Wait for the running command, or work from a different suite.
- **A `.rtl-buddy.lock` file lingers in `artefacts/`.** It is holder
  metadata only; the actual lock is kernel-managed and vanishes when the
  holding process exits (crash and `kill` included). A leftover file means
  nothing is locked — do not "clean it up" mid-run thinking it's stale
  state, and don't be alarmed by it after runs finish.
- **No protection across hosts (NFS).** `flock` is relied on with local
  semantics only; on an NFS-mounted workspace, two runs on *different
  machines* may both acquire "the" lock and proceed. Whether flock spans
  NFS depends on protocol version, mount options, and the server's lock
  daemon — rtl_buddy assumes it doesn't. Same-host concurrent runs are the
  protected case; cross-host coordination is on you.

## `rb nvim-install` requires git + network, and pins the plugin by hand

Unlike the old `rb wave-install-nvim` (which copied a bundled `.lua` offline),
`rb nvim-install` (the new primary name; `wave-install-nvim` is now an alias)
fetches the [`rtl-buddy-nvim`](https://github.com/rtl-buddy/rtl-buddy-nvim)
plugin with `git clone`. It therefore needs **`git` on PATH and network
access**. For air-gapped machines, install from a local checkout instead:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <ref>
```

The revision it clones is pinned in `RTL_BUDDY_NVIM_REF`
(`src/rtl_buddy/tools/nvim_install.py`). That pin and the hub wire-protocol
version (`PROTOCOL_VERSION` in `src/rtl_buddy/hub/protocol.py`) are coupled but
not mechanically linked: the pinned plugin speaks one protocol version, and the
hub enforces it on the wire (`decode()` rejects a mismatched `v`), so a mismatch
would surface only as a failed handshake on a *user's* machine — never at build
time.

The guard against that drift is a CI tripwire: `_PIN_PROTOCOL_VERSION` sits next
to the pin and `test_pin_tracks_hub_protocol_version` asserts it equals the hub
`PROTOCOL_VERSION`. When you bump `PROTOCOL_VERSION`, the test fails until you
(1) tag a compatible `rtl-buddy-nvim` release, (2) bump `RTL_BUDDY_NVIM_REF` to
it, and (3) bump `_PIN_PROTOCOL_VERSION`. Keep the three in lockstep.
