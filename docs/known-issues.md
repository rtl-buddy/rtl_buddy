---
description: Quirks, non-conventional behaviors, and known issues with rtl_buddy, including workarounds for simulator-specific behaviors.
---

# Quirks & Known Issues

The home for rtl_buddy behavior that does not follow convention: quirks, surprising defaults, simulator-specific workarounds, and known limitations. If something tripped you up because it works differently than you'd expect, add it here so the next person — or agent — finds it first.

Keep this page alive. When you hit or introduce a quirk, write it down rather than leaving it in commit history or someone's memory. Use one `##` section per quirk, name it after the behavior, and say what to do about it.

## v6.26.0's `rtl_buddy[graph-extract]` extra fails at pip/uv resolution

The v6.26.0 wheel advertises a `graph-extract` extra whose dependency, `rtl-buddy-graph-extract`, has no PyPI release yet — so `pip install "rtl_buddy[graph-extract]"` (or `uv add "rtl_buddy[graph-extract]"`) fails with a resolution error on that version. Nothing is wrong with your environment.

Workaround on v6.26.0: install rtl_buddy without the extra and add the extractor directly with `uv pip install git+https://github.com/rtl-buddy/rtl-buddy-graph-extract` — `rb graph build` discovers it on PATH; without it the binding tier is simply reported as skipped. Later releases do not advertise the extra at all (the dependency moved to an unpublished dev group); it will return as a real extra once the extractor's first PyPI release lands.

## Coverage follows the platform builder, not a per-test/suite `builder:`

A test can pick its simulator with a per-test or suite-wide `builder:` (see [Selecting the simulator builder](reference/yaml.md#selecting-the-simulator-builder)), but coverage collection and reporting — `rb test --coverage`, the Coverview packer, and the `builder`/`simulator_family` labels on coverage artifacts — key off the **platform-selected** builder, not the test's effective one. When a test's effective builder differs from the platform default *and* no `--builder` override is in effect, the coverage layer can mislabel or misparse results.

Workaround: run the suite with `--builder <name>` (which forces the builder consistently across simulation and coverage) or make that builder the platform default. In practice this only bites Verilator — the sole family that emits line/toggle coverage today; VCS and Icarus collect no coverage through this path. See the [tests.yaml limitation note](reference/yaml.md#selecting-the-simulator-builder) for the full rationale.

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

## Hook scripts see `__name__ == "__rtl_buddy_hook__"`, never `"__main__"`

`sweep` and `preproc` hooks are `exec()`'d into a hand-built namespace rather than imported, so `__name__` is set explicitly to the sentinel `"__rtl_buddy_hook__"`. If your hook wraps its logic in `if __name__ == "__main__":` (a common habit for scripts meant to also run standalone), that block is **always skipped** under `rb` — the hook silently no-ops. Put hook logic at module top level; reserve the `__main__` branch for a standalone entry point only. See [Plugins](concepts/plugins.md#hook-working-directory).

## Compilation-unit `bind` under `frontend: verilog` elaborates zero formal cells

A property file that binds its checker module at compilation-unit scope (`bind dut dut_props u_props (...);` at the top level of the file, outside any module) does **not** error under the default `frontend: verilog` — but yosys's native verilog frontend never resolves the bind. The checker is stored as `$abstract` and removed as unused before any assertion cell is generated, so the proof runs against **zero** formal cells. With no guard, sby would prove nothing and report a silent **PASS** — a false pass indistinguishable from a real one.

`rb fpv` guards the primary proof against this: when a verification lists `properties:`, the generated sby script asserts that at least one formal cell (`$assert` / `$assume` / `$cover` / `$live` / `$check`) survives `prep`. A suite that elaborates none fails loud with:

> sby reported ERROR (…) — zero formal cells elaborated: the property set produced no assert/assume/cover cells, so the proof would otherwise have passed vacuously (frontend='verilog' cannot resolve a compilation-unit-scope `bind`; set `frontend: slang` for bind-based property modules)

The fix is to set `frontend: slang` on that verification: yosys-slang reads all files in one `read_slang --top` invocation, so a compilation-unit-scope bind resolves and the asserts elaborate. Inline-assertion suites (`properties: []`, with assertions in the DUT) are not bind-based and are intentionally not guarded. See [Choosing a frontend](concepts/fpv.md#choosing-a-frontend).

## cocotb on VCS skips its VPI-access flags when you already configured any `-debug_access`/`+acc`

cocotb drives the DUT over VPI, so on a `vcs` builder rtl_buddy injects `-debug_access+all` and `+acc+3` (plus `-load <libcocotbvpi_vcs.so>` and `-LDFLAGS -Wl,--no-as-needed`) at elaboration. To avoid fighting a builder that already enables access, the injection is suppressed per token: if **any** configured compile-time opt starts with `-debug_access` (e.g. `-debug_access+all+class`) the `-debug_access+all` is not added, and likewise for any `+acc*` opt.

The footgun: a *narrower* configured flag counts as "covered". If your `builder-opts` set, say, `-debug_access+line` (or `+acc+1`) but not full read/write access, rtl_buddy will **not** add `+all`/`+acc+3`, and cocotb may fail to drive signals it needs to write. Workaround: configure full access yourself (`-debug_access+all` / `+acc+rw`) for cocotb-targeted VCS builders, or drop the narrow flag and let rtl_buddy inject the defaults. `-top` is similarly only injected when the builder hasn't already pinned a top.

## VCS license-queue waits pause `sim_timeout`, so a sim can visibly outlive it

On a `vcs` builder, when `simv` prints the `-licqueue` banner (`Queuing for License` or `Licensed number of users already reached`), rtl_buddy pauses the per-sim `sim_timeout` clock until real simulator output resumes — a sim stuck behind a busy license server can therefore run far longer than its configured timeout without failing. The pause is announced with a `sim.license_queue` warning and the resume with `sim.license_granted` (both include the queued seconds in the log). Total queued time is capped at 1 hour; past the cap the normal timeout resumes counting and the eventual `sim.timeout` error reports the queue wait. Applies only to the `vcs` simulator family. See [Tests](concepts/tests.md#vcs-license-queue-waits-and-sim_timeout).

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
  `tests.yaml`, `synth.yaml`, `fpv.yaml`, etc. share a suite directory, they
  share one `artefacts/` — so `rb hier` during a long `rb test`
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
- **Dispatched jobs deliberately skip the lock.** Under
  `regression --dispatch slurm`, the head process holds the lock but the
  per-test `rb _test-job` jobs do **not** take it — they are cooperative
  delegates writing disjoint `run-*` dirs and reading the shared build
  read-only. The consequence: while a dispatched regression is running,
  nothing stops you from starting a second `rb test` in the same tree,
  because the head's lock does not cover what the jobs are doing on the
  compute nodes. Don't launch other commands against a tree that has a
  dispatch run in flight.

## `--dispatch` silently implies `--share-build`

`regression --dispatch <backend>` (and `randtest --dispatch <backend>`, for
`slurm` and `local-parallel` alike) turn on
`--share-build` even if you didn't pass it: a **dispatched build job**
compiling one shared `simv` per unique compile key on a compute node is
exactly what lets each sim job skip compilation and re-enter at simulation.
This changes compile behaviour versus a plain local run — tests with
identical compile inputs compile once, not once each. Builders share-build
cannot handle fall back to compiling inside each job, which also skips the
build job and widens that job's reservation to cover the compile; see
[Parallel dispatch](concepts/dispatch.md#builders-that-compile-inside-the-job).
The promotion is logged as `dispatch.share_build_implied`. Also
note `--dispatch` cannot be combined with `--early-stop`: a build job
compiles and the sim jobs run sim+post, so no earlier stop point is
expressible per job (rtl_buddy rejects the combination loudly rather than
ignoring the flag). And `randtest -r <n> --dispatch slurm` runs locally — a
single-seed replay gains nothing from the queue — logging
`randtest.dispatch_ignored_for_replay` so the ignored flag isn't silent.

## `--dispatch local-parallel` ignores `resources:` — `-j` is the only limit

The native-process backend runs jobs as plain subprocesses on one host, and
a host has no portable per-process cap (`ulimit`/`nice`/`taskset` are
coarse and platform-specific). So a `resources:` block that a cluster would
enforce is **inert** here: cpus/mem/time are ignored rather than
half-applied, `cfg-dispatch.max-jobs-per-array` (a Slurm `%N` throttle) is
ignored too, and there is no accounting source, so reservation right-sizing
returns no advice instead of guessing at utilization. A run whose resolved
reservations are non-default — set under `cfg-dispatch` *or* per
testbench/test in `tests.yaml` — **warns** once
(`dispatch.reservations_ignored`) so the config cannot read as enforced.
Practical consequence: `-j`/`cfg-dispatch.jobs`
is your only backpressure, so size it against the **memory** your heaviest
tests need, not just against core count — four unreserved simulators can
swap a laptop even though four cores are free. Right-size reservations from
a Slurm run, not this one. See
[Parallel dispatch](concepts/dispatch.md#on-one-machine-dispatch-local-parallel).

## A `SIGKILL`ed head orphans a `local-parallel` fleet

Dispatched subprocesses are started in their own session so the head owns
their teardown: `Ctrl-C` (or any fatal head error) triggers `cancel_all`,
which signals the whole fleet with `SIGTERM` and then escalates stragglers
to `SIGKILL` against a single 5-second deadline, letting a simulator flush
on the graceful signal. If the head is itself `SIGKILL`ed — a CI timeout,
`kill -9` — that cleanup never runs, and unlike Slurm (where
`--kill-on-invalid-dep` and the controller reap the fleet) **nothing else
will**: the children run their tests to completion and write envelopes no
one reads. Prefer `Ctrl-C`; after a hard kill, check for stray
`rb _test-job` processes.

## An unquoted `time:` in `cfg-dispatch`/`resources:` is YAML sexagesimal

YAML 1.1 parses an unquoted `time: 4:00:00` as the **integer 14400**
(base-60), not the string `"4:00:00"`. Slurm would read that as 14400
*minutes* — a 10-day reservation instead of 4 hours. Leading-zero forms
like `01:00:00` happen to survive (the resolver needs a non-zero leading
digit), which is why the trap is easy to miss. rtl_buddy's `_validate_time`
**rejects the integer form loudly** at config load, telling you to quote
it — so always write `time: "4:00:00"` (or bare minutes as a string,
`time: "240"`). Applies everywhere `resources:` appears: `cfg-dispatch`,
per-testbench, and per-test.

## Reservation right-sizing has floors that can silently suppress advice

The reservation-advice thresholds (`over-threshold`, `near-limit`,
`margin`) are configurable under `cfg-dispatch.rightsize`, but two guards
are fixed constants: a **5-minute time floor** and a **128M memory floor**
on suggestions, plus a keep-ratio that only emits a `reduce` when the
suggestion actually saves ≥25% of the reservation. Consequence: for any
time reservation below ~6m40s (`5min ÷ 0.75`), no `reduce` time advice is
possible regardless of utilization — a very over-provisioned but tiny
reservation can come back with no advice and no explanation. This is
deliberate (churn suppression) but not currently tunable; if you get empty
advice on an obviously over-reserved short job, this is why.

## Under `--dispatch`, `preproc` runs in both the build job and each sim job

Hook scripts (`sweep`, `preproc`, `postproc`) are compatibility-sensitive
APIs, and dispatch changes *how many times* they run:

- **`sweep` runs exactly once**, on the head. The head expands the suite
  and writes a plan manifest; the build job and every sim job rebuild their
  configs from the manifest rather than re-running the hook (so a
  nondeterministic sweep can't expand differently per process and leave a
  sim job's compile key unbuilt).
- **`preproc` runs twice**: once in the build job before the shared compile,
  and again in each sim job before its simulation (locally it runs once per
  test). A non-idempotent `preproc` — one that generates a stimulus file,
  bumps a counter, or otherwise has side effects — behaves differently under
  `--dispatch`. Make `preproc` idempotent, or move one-time setup into the
  sweep/head path.

## A dispatched compile failure surfaces as CompileFail, not DispatchFail

When a test's compile fails, the build job records it and the head maps the
row to a **CompileFail** — the same clean design-error result the
in-process path produces — pointing at the build-job log. (A genuine
infrastructure failure — a job the scheduler killed, or one that vanished
without writing a result — is still a `DispatchFail`, and its desc names
both the sim-job and build-job logs.) The failing test's sim job is still
submitted and recompiles under its **sim** reservation, not the
`cfg-dispatch.compile` one; if that recompile is heavier than the sim
reservation allows it may be OOM/`TIMEOUT`-killed — size `cfg-dispatch`
reservations so a compile fits, or expect the killed recompile to be folded
into the CompileFail above.

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

## Shared-build reuse does not see header edits or toolchain upgrades

With [`--share-build`](concepts/tests.md#sharing-compiled-builds-across-tests)
([#293](https://github.com/rtl-buddy/rtl_buddy/issues/293)), a compile is
skipped when the `rb-compile-stamp.json` in the shared build dir matches the
current compile inputs. The stamp covers the compile command, the extra
compile env, and the size/mtime of every **file listed in the resolved
`run.f`** — but two input classes are not tracked:

- **Include-dir contents.** `+incdir+` and `-y` entries resolve to
  directories, so the stamp records only the raw line. Editing a header that
  is only reachable through an include dir does **not** invalidate the stamp,
  and a warm run reuses a simv built from the old header. Tracking consumed
  headers via Verilator's emitted `.d` dependency file is
  [#303](https://github.com/rtl-buddy/rtl_buddy/issues/303).
- **The toolchain itself.** The builder executable is keyed by its configured
  name (e.g. `verilator`), not its version, and only the *extra* compile env
  is recorded — the ambient environment the subprocess inherits is not. An
  in-place Verilator upgrade, or a `PATH` change that selects a different
  binary, reuses the stale simv silently.

The escape hatch for both: delete `artefacts/.shared-builds/` (or run once
without `--share-build`) to force a fresh compile. Within-run and concurrent
safety are separate concerns and *are* handled — same-key tests share by
construction, and cross-process races are excluded by the
[per-tree artefact lock](#the-artefact-tree-lock-is-per-tree-and-its-lock-file-stays-behind)
(with that quirk's NFS caveat applying here too).

## A wrong FPV plugin path degrades COI coverage silently

`rb fpv` / `rb fpv-regression` treat the cone-of-influence pass as best-effort: when the yosys COI script fails — most commonly because the yosys-slang plugin path is wrong (stale `plugin-path`, an unbuilt `slang.so`, or a sibling-checkout convention that doesn't hold on this machine) — the run only emits `fpv coi_yosys_failed` WARNINGs and drops the COI column. The verification verdict itself still PASSes, because the proof pipeline loads the plugin separately, so a broken plugin location can sit unnoticed while COI coverage quietly reports nothing.

If COI numbers disappear or `coi_yosys_failed` shows up in the log, check the resolved plugin location first: `cfg-fpv-tools[].opts.plugin-path` in `root_config.yaml`, or — when that is unset — the `RTL_BUDDY_SLANG_PLUGIN` environment variable. The per-verification `coi.log` under `artefacts/<name>/` records the exact yosys error (e.g. `Can't load module ...slang.so`).

## `rb fpga --bitstream` downgrades unconstrained-I/O DRCs to write the bitstream

A bitstream write (`rb fpga <run> --bitstream`) downgrades two I/O DRCs to warnings immediately before `write_bitstream`: **NSTD-1** (a port without an `IOSTANDARD`) and **UCIO-1** (a port without a `LOC`). Vivado treats both as errors that abort `write_bitstream`, so a design without a complete board pinout — the common `rb fpga` bring-up/smoke case — could synthesize and route but never produce a bitstream. The downgrade makes the zero-pinout case produce a `.bit` out of the box.

This is deliberate and it is **not silent in the data**: `report_drc` runs *before* the downgrade, so both violations are still counted in `drc_violations` / `drc_by_severity` at their original severity in the results and machine payload — only the gate that blocks `write_bitstream` is relaxed. A board-targeted design with a full pinout has no NSTD-1/UCIO-1 violations, so it is unaffected. If you are taking a design to real hardware, treat any NSTD-1/UCIO-1 in the report as the error it is and add the missing `IOSTANDARD`/`LOC` constraints in your XDC; the downgrade is a bring-up convenience, not a sign-off policy.

## `rb fpga` PASSes a routed run that misses timing (unless `require-timing-met`)

By default a routed run with negative slack (`timing_met: false`, negative `wns_ns`) still reports **PASS**. This is intentional and mirrors `rb pnr`: the metrics carry the truth so an agent can run a timing-closure loop — read `timing_met` / `wns_ns` / `failing_paths` from the machine JSON, edit the XDC or RTL, and rerun — instead of the run simply failing with no actionable detail. Pass/fail keys off the flow completing (synth → route → reports), not off meeting timing.

To make timing a hard gate (e.g. for a regression suite that must not regress closure), set `require-timing-met: true` on the run in `fpga.yaml`. An unmet-timing run then becomes a **FAIL**, but the routed metrics still ride along on the failing result so the closure loop keeps its inputs. The gate only fires when the backend actually reports timing — a backend that cannot measure it (`timing_met: null`) is never gated, since a miss cannot be proven.
