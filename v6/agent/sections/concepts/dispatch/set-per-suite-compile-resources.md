## Set per-suite compile resources

`cfg-dispatch.compile` is one reservation for every suite's build job, so a repo with one large top-level testbench and many leaf-cell benches sizes them all for the largest. A suite that differs states its own reservation at the **top level of its `tests.yaml`**, in the same `{cpus, mem, time}` shape:

```yaml
rtl-buddy-filetype: test_config

compile:
  mem: 48G          # this suite's verilation only; cpus and time inherited

testbenches:
  - name: soc_tb
    ...
```

The compile reservation resolves field by field in this order: suite `compile`, `cfg-dispatch.compile`, `cfg-dispatch.resources`, built-in defaults. A field the suite omits inherits, so the example above keeps the cluster-wide `cpus: 8` and `time: "02:00:00"` and moves only memory. The block sizes the suite's build job, and — for a builder that cannot share a build — the compile half of the field-wise maximum that sizes each simulation job.

`parallel` is not accepted at suite level. It sizes the build job against the partition's widest node, which is a cluster fact; keep it in `cfg-dispatch.compile`. Note that unknown keys are dropped silently rather than rejected, so a `parallel` written here simply has no effect.

The block is a scheduling fact only. It is not part of the compile fingerprint, so adding or changing it never invalidates a shared build stamp.

Size `compile.time` for the longest batch the build job runs, not for one build. With `compile.parallel: N` the suite's unique compile keys are compiled N at a time, so the job's wall clock is the makespan of a work queue N workers deep: each worker takes the next unbuilt key as it frees up, and the job ends when the last one finishes. `ceil(distinct builds / N)` times the slowest build is a safe upper bound to size against, and it is close to the real figure only when the builds take similar times; a mix of one long build and several short ones finishes nearer the long one alone. At the default `parallel: 1` it is the serial total of every key.

Size `compile.mem` for `parallel` concurrent builds: the head scales only the `cpus` reservation, and N elaborations need roughly N times the memory. Size it from elaboration, not simulation. Large generated structures can make elaboration the memory peak; Slurm reports `OUT_OF_MEMORY`, while local runs may show `Killed`, SIGKILL, or exit 137. Raise the field named by `reservation_advice[*].edit_hint`, not `sim_timeout`.

VCS license wait under `-licqueue` counts against the Slurm time limit. Give `compile.time` queue headroom; `compile.license_queued` records only completed builds that waited. N concurrent elaborations hold up to N licenses at once, so raising `parallel` multiplies license pressure and can convert compute time into queue time; keep it at or below what the site's license pool can serve.

Dispatch requests `--acctg-freq=task=1` unless `sbatch-args` already supplies it. Keep fine-grained accounting if you want useful memory advice for short jobs.

<a id="retrying-a-license-queue-kill"></a>
