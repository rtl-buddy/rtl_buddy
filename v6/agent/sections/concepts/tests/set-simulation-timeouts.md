## Set simulation timeouts

`sim_timeout` defaults to 60 seconds. Add a builder-wide allowance for licensed simulators that may wait before running:

```yaml
cfg-rtl-builder:
  - name: vcs
    extra-sim-timeout: 900
```

The allowance is added to each test's timeout. Override it for one command with `--extra-sim-timeout N`; use 0 to disable a configured allowance. Negative values are rejected. The setting affects simulation only, not compilation, and is forwarded to local-parallel and Slurm jobs.

For VCS runs using `-licqueue`, RTL Buddy pauses the test timeout while recognized license-queue banner output is active, for at most one hour. The timer resumes on other simulation output or after the cap. This avoids false timeouts without allowing an indefinite queue wait. Builder allowance remains useful for unrecognized or silent license managers.
