---
description: Quirks, non-conventional behaviors, and known issues with rtl_buddy, including workarounds for simulator-specific behaviors.
---

# Quirks & Known Issues

The home for rtl_buddy behavior that does not follow convention: quirks, surprising defaults, simulator-specific workarounds, and known limitations. If something tripped you up because it works differently than you'd expect, add it here so the next person — or agent — finds it first.

Keep this page alive. When you hit or introduce a quirk, write it down rather than leaving it in commit history or someone's memory. Use one `##` section per quirk, name it after the behavior, and say what to do about it.

## v6.26.0's `rtl_buddy[graph-extract]` extra fails at pip/uv resolution

The v6.26.0 wheel advertises a `graph-extract` extra whose dependency, `rtl-buddy-graph-extract`, has no PyPI release yet — so `pip install "rtl_buddy[graph-extract]"` (or `uv add "rtl_buddy[graph-extract]"`) fails with a resolution error on that version. Nothing is wrong with your environment.

Workaround on v6.26.0: install rtl_buddy without the extra and add the extractor directly with `uv pip install rtl-buddy-graph-extract` — `rb graph build` discovers it on PATH; without it the binding tier is simply reported as skipped. Releases from v6.26.1 through v6.33.1 do not advertise the extra at all (the dependency moved to an unpublished dev group). With rtl-buddy-graph-extract 0.1.0 on PyPI, the extra returns as a normal `rtl_buddy[graph-extract]` (with a `>= 0.1.0` floor) in the next minor release after v6.33.1.

## Coverage follows the platform builder, not a per-test/suite `builder:`

A test can pick its simulator with a per-test or suite-wide `builder:` (see [Selecting the simulator builder](reference/yaml.md#selecting-the-simulator-builder)), but coverage collection and reporting — `rb test --coverage`, the Coverview packer, and the `builder`/`simulator_family` labels on coverage artifacts — key off the **platform-selected** builder, not the test's effective one. When a test's effective builder differs from the platform default *and* no `--builder` override is in effect, the coverage layer can mislabel or misparse results.

Workaround: run the suite with `--builder <name>` (which forces the builder consistently across simulation and coverage) or make that builder the platform default. In practice this only bites Verilator — the sole family that emits line/toggle coverage today; VCS and Icarus collect no coverage through this path. See the [tests.yaml limitation note](reference/yaml.md#selecting-the-simulator-builder) for the full rationale.

## Instance pRNG seeding with Verilator

Random testing does not behave reliably with Verilator. While Verilator supports multiple random tests with different seeds, tests are not always reproducible even with the same seed.

VCS is recommended for stable random testing due to its hierarchical instance seeding. VCS seeds instantiated modules, process threads, and classes based on their hierarchical names. For stable random seeding with VCS, name your instances explicitly.

If you require reproducible randomized testing on macOS (where VCS is not available), this is a known limitation.

## Verible resolves from PATH when `cfg-verible.path` lacks the binaries

`rb verible` resolves each executable in a fixed precedence: the configured `cfg-verible.path` directory wins **when it actually contains the binary**, otherwise rtl_buddy falls back to whatever is on `PATH`, and only as a last resort returns the configured join (so a genuine "not found" still names the expected directory).

This means a site can expose Verible through its environment (a `module load`, or a sourced setup script that puts `verible-verilog-*` on `PATH`) and leave `cfg-verible.path` at the committed default — no per-checkout edit to `root_config.yaml`. The flip side: if your configured directory does not contain the binaries but a *different* Verible is on `PATH`, that PATH copy is used instead. rtl_buddy **warns** when that happens (`verible.exe_fallback`), naming both the configured path and what `PATH` resolved — but only for the entry the active platform routes to, so another platform's entry stays quiet. If `rb verible` seems to run a different build than the one you configured, check `PATH` — the configured directory only takes precedence when the binary is present there. This mirrors how `cfg-surfer` already resolves its executable.

## A broken tool pin is announced once per process, so a daemon never repeats it

The `verible.exe_fallback` warning above and `tool_path.unresolved_var` (every
candidate of a `builder:` / `path:` / `tool:` list referencing an unset
variable) are deduped in a module-level set: resolution runs on every
`get_exe()` call — several times per test — so an undeduped warning would emit
thousands of identical lines across a regression. The condition is a static
property of the config plus the environment, so saying it once is saying it —
but `rb mcp` and `rb hub` are long-running processes, and a pin fixed (or
broken) while one is up is never re-announced. Restart the daemon after
editing `root_config.yaml` or `.rtl-buddy/.env` if you are relying on those
warnings.

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

What "real simulator output" means matters, because the pause ends on the first line outside the banner's vocabulary. If a VCS release adds a line to the banner that rtl_buddy does not recognise, the clock restarts while the sim is still queuing and every queued sim fails with `Sim hit timeout`, which reads as a design or testbench failure rather than a license one. The tell is `Licensed number of users already reached` in `test.err` alongside a timeout verdict. `extra-sim-timeout` on the builder is the backstop; see [Extra simulation timeout per builder](concepts/tests.md#extra-simulation-timeout-per-builder).

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

`regression --dispatch <backend>` (and `randtest --dispatch <backend>` and
`test --dispatch <backend>`, for `slurm` and `local-parallel` alike) turn on
`--share-build` even if you didn't pass it: a **dispatched build job**
compiling one shared `simv` per unique compile key on a compute node is
exactly what lets each sim job skip compilation and re-enter at simulation.
This changes compile behaviour versus a plain local run — tests with
identical compile inputs compile once, not once each. Builders share-build
cannot handle fall back to compiling inside each job, which widens that
job's reservation to cover the compile. The build job is skipped for them
too, but only while there is nothing to serialize: a test fanned out over
several runs would otherwise have every element compiling into the one
`artefacts/<test>/` at once, so it gets a build job and its elements wait
for it. See
[Parallel dispatch](concepts/dispatch.md#builders-that-compile-inside-the-job).
The promotion is logged as `dispatch.share_build_implied`. Also
note `--dispatch` cannot be combined with `--early-stop`: a build job
compiles and the sim jobs run sim+post, so no earlier stop point is
expressible per job (rtl_buddy rejects the combination loudly rather than
ignoring the flag). And `randtest -r <n> --dispatch slurm` runs locally — a
single-seed replay gains nothing from the queue — logging
`randtest.dispatch_ignored_for_replay` so the ignored flag isn't silent.

## `cfg-dispatch.backend` does not apply to `rb test`

`cfg-dispatch.backend` defaults the backend for `rb regression` and
`rb randtest` only. **`rb test` dispatches only when `--dispatch` is passed
on the command line** — set `backend: slurm` in `root_config.yaml`, run
`rb test basic`, and it still runs in-process. That is deliberate: `rb test`
is the local iteration command, and a project that pointed its regressions
at a cluster must not find single-test runs queueing behind it after an
upgrade (it also keeps `--early-stop`, which dispatch rejects, usable by
default). Nothing else in `cfg-dispatch` is ignored — `resources`, `retry`,
`jobs`, `max-wait` and the rest all configure the run once `--dispatch`
opts in. Pass `--dispatch slurm` (or `local-parallel`) explicitly. See
[Parallel dispatch](concepts/dispatch.md).

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

## A suite's build job compiles every compile key serially, in one reservation

The per-suite build job is a single `sbatch` job running `rb _build-job`,
which walks the suite's planned tests in order and compiles each unique
compile key. That is deliberate — it is what makes a VCS build take one
license seat at a time instead of one per concurrent element — but it has
two consequences that are easy to miss when sizing `cfg-dispatch.compile`:

- **`compile.time` must cover the sum of those compiles, not one of them.**
  A multi-testbench suite has one key per testbench (at least), so its build
  job's wall-clock is their total. A limit sized from one observed compile
  gets the build job killed, and because every sim job is gated on it with
  `afterok`, the entire array is cancelled and reported as dispatch failures
  pointing at a build log that simply stops mid-compile.
- **The whole array waits for the slowest suite-wide build**, including
  tests whose own compile key finished first. There is no partial release.

The lever is the compile-key count, not the reservation: tests that differ
only in their `plusdefines:` each cost a key. The build job logs one
`compile.start` per key, which is the cheapest way to count them (a key
already valid in the shared dir short-circuits before that event, so the
count is of real compiles).

## Memory right-sizing depends on the accounting sampling interval

`MaxRSS` is a high-water mark over `JobAcctGatherFrequency` samples, so a
job shorter than one interval is sampled at most once and reports a peak far
below the truth — measured 17-27x low on a site running the stock 30 s
default, where the resulting suggestion for every short test was the 128M
floor ([#365](https://github.com/rtl-buddy/rtl_buddy/issues/365)). Dispatch
exists to produce lots of short jobs, so this is the common case rather than
the corner one.

Two things now stand between that and a bad reservation, and it is worth
knowing both because the first can be turned off:

- every job is submitted with **`--acctg-freq=task=1`** unless your
  `cfg-dispatch.sbatch-args` already sets `--acctg-freq`. Setting it
  yourself — including to a coarse value, or to `task=0` — is honoured, and
  is the supported way for a site that must not raise the sampling rate;
- utilization-based **memory advice is suppressed** for any test whose
  longest run finished inside the interval actually in force, logged as
  `rightsize.mem_advice_unsampled` with the test names. Time and CPU advice
  are unaffected (elapsed and `TotalCPU` are not sampled the same way), and
  an `OUT_OF_MEMORY` kill still raises.

Build jobs were never affected: they run for minutes and get sampled.

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
- **Those `preproc` runs are concurrent, and `artifact_dir` is test-keyed.**
  Every element of a dispatched `randtest` (or a seed fan-out) runs the hook
  against the *same* `artifact_dir` while its siblings' simulations read from
  it, so a generator using `open(path, "w")` truncates a file another element
  is reading and the short read surfaces as a *design* failure — a checker
  mismatch against empty expected data, not a harness error. Two rules keep a
  hook safe: write to `artifact_dir` **atomically** (temp file plus
  `os.replace`), and if the generated content depends on the run or the seed,
  write to the injected `run_artifact_dir` instead — it is unique per run
  ([#415](https://github.com/rtl-buddy/rtl_buddy/issues/415)). See
  [Where a generator should write](concepts/plugins.md#where-a-generator-should-write).

## A build job orders the fan-out; only the stamp keeps it from recompiling

When a self-compiling test is fanned out over several runs, dispatch submits
a build job and gates every element on it with `afterok`
([#369](https://github.com/rtl-buddy/rtl_buddy/issues/369)). That dependency
makes the elements start *after* the build — it does not make them exclusive
of each other. What actually stops them recompiling into the one
`artefacts/<test>/` is the compile stamp the build job leaves, and anything
that invalidates it invalidates it for **all N at once**.

The realistic way in is a `preproc` that regenerates a file listed in the
filelist. The stamp records each source's `st_mtime_ns`, so a regenerated
file invalidates it even when the bytes are identical — and a hook that
writes per run is a supported pattern
([#415](https://github.com/rtl-buddy/rtl_buddy/issues/415)). Every element
then runs the full compile concurrently into one directory, which is exactly
the failure the build job exists to prevent, and the result is a
`Compile failed` that looks like a design error.

`compile.prebuilt_stamp_invalid` (WARNING) is emitted by any element that
compiles despite having been gated, and is the line that identifies this.
Two ways out until a lock or a per-run compile dir exists: make the
generator write to `run_artifact_dir` (per-run output does not belong in a
filelist source anyway), or emit test-keyed files only when their content
changes, so the mtime is stable across elements.

## A retry truncates the sim capture that justified it

`cfg-dispatch.retry` re-submits a job with the **same** result-envelope and
artefact paths, so the second attempt overwrites `artefacts/<test>/test.log`
and the per-job `rtl_buddy-<tag>.log` of the first. Only the **scheduler**
log is kept per attempt (`slurm-<tag>-retry<N>.log`) — so if the license-queue
banner that justified the retry landed only in the sim's own capture, the
retry destroys the evidence for itself. When debugging a run that retried and
still failed, read the `dispatch.result_missing` entry in `rtl_buddy.log` and
the per-attempt scheduler logs, not `test.log`. See
[Retrying a license-queue kill](concepts/dispatch.md#retrying-a-license-queue-kill).

## With retry enabled, `max-wait` bounds each wait, not their sum

`cfg-dispatch.max-wait` is a per-wait deadline, and every retry round waits
again — so a run with `attempts: 2` can take up to roughly
`attempts × (backoff + max-wait)`, not `max-wait`. Each round's deadline is
additionally widened by the backoff the head asked for, because a job held on
`--begin` is outstanding (PENDING) for the whole delay and a `max-wait`
shorter than the backoff would otherwise trip before the job was allowed to
start. If you set `max-wait` as a run-duration ceiling, divide it by
`attempts + 1` or leave retry off. See
[Retry and `max-wait`](concepts/dispatch.md#retrying-a-license-queue-kill).

## A `--begin` in `sbatch-args` silently disables the retry backoff

The retry emits `--begin=now+<delay>` **before** `cfg-dispatch.sbatch-args`,
and `sbatch` takes the last occurrence of a duplicated flag — so a site that
passes its own `--begin` through `sbatch-args` wins and the backoff never
happens. The retried jobs go straight back in front of the pool that killed
them, which is the synchronised retry storm the jittered backoff exists to
avoid. Consistent with every other `sbatch-args` override, but silent: drop
`--begin` from `sbatch-args` if you want the backoff. See
[Retrying a license-queue kill](concepts/dispatch.md#retrying-a-license-queue-kill).

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

## Shared-build reuse sees header edits only where the builder reports them

With [`--share-build`](concepts/tests.md#sharing-compiled-builds-across-tests)
([#293](https://github.com/rtl-buddy/rtl_buddy/issues/293)), a compile is
skipped when the `rb-compile-stamp.json` in the shared build dir matches the
current compile inputs. The stamp covers the compile command, the extra
compile env, the size/mtime of every **file listed in the resolved `run.f`**,
and — since [#303](https://github.com/rtl-buddy/rtl_buddy/issues/303) — every
input the builder reports having **consumed**, read from the dependency file
it emits. What that last part covers depends entirely on the builder:

- **Verilator** emits `V<prefix>__ver.d`, so included headers, files reached
  through `-y` library dirs, its own std includes and the `verilator_bin`
  binary are all tracked. A header-only edit and an in-place Verilator
  upgrade each invalidate the stamp.
- **VCS and Icarus** emit nothing comparable. Their stamps record
  `"deps": null` — *no dependency information*, not *no dependencies* — and
  reuse behaves as it did before: **editing a header reachable only through
  `+incdir+`/`-y` does not invalidate the stamp**, and a warm run reuses a
  simv built from the old header.

Two things stay untracked for every builder: the **ambient environment** the
compile subprocess inherits (only the *extra* compile env is recorded, so a
`PATH` change that selects a different binary is invisible unless the
dependency file names that binary), and anything a builder consumes without
declaring.

The escape hatch where tracking does not reach: delete the stamp, or run
once without `--share-build`, to force a fresh compile. **Which stamp
depends on the builder**, and the distinction matters most for exactly the
builders with no dependency tracking:

- a *shared* build stamps `artefacts/.shared-builds/obj_dir_<key>/`, so
  deleting `artefacts/.shared-builds/` covers it;
- a build that could not be shared (any non-Verilator/VCS/Icarus family, or
  an absolute `builder-simv:`) stays in the test's own directory and stamps
  `artefacts/<test>/rb-compile-stamp.json`, which that deletion does **not**
  touch.

When a warm run rebuilds and you want to know why, `compile.build_dep_changed`
(DEBUG) names the input that changed.

A stamp written by an rtl_buddy older than #303 has no dependency record at
all, and its silence cannot be told from "there were none" — such a stamp is
rejected, so the first run after upgrading recompiles once per compile key.

Within-run and concurrent safety are separate concerns and *are* handled —
same-key tests share by construction, and cross-process races are excluded by
the
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

## `rb graph results --coverage <merged.info>` joins design heat by file, so two modules in one file share numbers

The `info` source ingests a merged LCOV `.info`, and LCOV records files, never module names. A `module:` node therefore gets the numbers of exactly the `SF:` record that resolves to the file the node claims — so **two modules declared in one source file both wear that file's whole coverage**. The entry says so (`joined_by: "file"`), and the richer `model` / `artefacts` sources do not have this behavior: they read the simulator's own per-module attribution. If you need per-module numbers from one file, join from a coverage-mode run's `cov_dir/manifest.json` (`--coverage auto`, the default) rather than from an `.info`.

Two related bounds on the same source. An `SF:` path is believed only when it reaches a real file — either as written (absolute, or relative to the `.info`'s own directory) or after leading segments are trimmed to re-anchor it on the project root, and **never down to a bare basename**: a repo-scope `--coverage-merge` can rewrite duplicate basenames against the wrong suite's root, so a basename that happens to exist under your root is not evidence. Records that only resolved after trimming are listed in `summary.reanchored_files`; records that resolved to nothing land in `summary.unresolved_files` with a `problems` row. And because a merged `.info` carries no test column and no SVA cover points, per-test badges and `covitem:` verdicts still come from the per-test `coverage.dat` databases — with none in the tree, `summary.items` counts what the graph *declares* while `summary.items_scored` is `0`, which is what distinguishes "nothing scored these" from "the run hit none of them".

## `rb graph results --coverage` takes a value now, so a bare `--coverage` no longer parses

In v6.30.x, `--coverage/--no-coverage` was a boolean pair and the join was on by default. It now **names a source** — `auto` (the default), `model`, `none`, or a path to a merged LCOV `.info` — so `rb graph results --coverage` with no value fails with *"Option '--coverage' requires an argument"*. The break is loud rather than silent, and passing the flag bare was always redundant (the join was already on), but a script that did so needs one edit.

The fix is whichever you meant: drop the flag entirely (the default is unchanged), or write `--coverage auto`. `--no-coverage` is untouched and still disables the join, as does `--coverage none`. Click can express an optional-value option (`is_flag=False, flag_value=...`), which would have kept the bare form working; Typer forwards neither kwarg and deprecates both, so that route is closed until Typer supports it.

## The viewer answers to four different names: dist `rtl-buddy-sch`, executable `rtl-buddy-view`

The schematic viewer's PyPI **distribution** was renamed `rtl-buddy-view` → `rtl-buddy-sch` at 0.7.0; `rtl-buddy-view` is frozen at 0.5.0 and gets no further releases. Nothing else moved, so one tool now wears four names at once:

| What | Name | Notes |
|------|------|-------|
| PyPI distribution | `rtl-buddy-sch` | `uv tool install rtl-buddy-sch` / `pip install rtl-buddy-sch`. Releases up to 0.5.0 are on PyPI as `rtl-buddy-view` |
| Executable | `rtl-buddy-view` | Unchanged — it is what `rb hier`, `rb graph build`, `rb hub` and `--tool` resolve. 0.7.0+ also installs an `rtl-buddy-sch` alias |
| `rb tool-check` key | `rtl-buddy-view` | `rb tool-check --explain rtl-buddy-view`. `--explain rtl-buddy-sch` exits 1 with "unknown tool" and lists the known keys |
| Import package | `rtl_buddy_view` | What the hub imports for the in-env SPA bundle |

rtl_buddy probes the **distribution** under both names, `rtl-buddy-sch` first, so either install satisfies every version floor and `rb tool-check` reports a version either way. What you have to know: **pip cannot upgrade one into the other.** There is no rename metadata, so `pip install -U rtl-buddy-sch` over an existing `rtl-buddy-view` leaves two distributions claiming the same console script and the same `rtl_buddy_view` import package, with each one's `RECORD` describing files the other may have overwritten. Uninstall first:

```bash
pip uninstall -y rtl-buddy-view && pip install -U rtl-buddy-sch
```

One consequence worth expecting: after `uv tool install rtl-buddy-sch` (an isolated tool env) a project venv that still holds the old wheel has PATH at the new version and in-venv metadata at the old one. `rb tool-check` prefers the executable in exactly that case — a version read from the pre-rename dist name is dropped when the binary is on PATH, so the probe answers — but the stale wheel is still worth removing.
