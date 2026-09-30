## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time and array settings. `-j` or `cfg-dispatch.jobs` is the only limit, so size it for the heaviest test's memory. `SIGKILL` of the head can orphan `rb _test-job` children; find and stop them. See [Run on one host](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/dispatch/#run-on-one-host).
