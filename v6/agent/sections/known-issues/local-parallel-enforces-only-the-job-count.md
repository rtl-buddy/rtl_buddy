## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time, array-throttle, and right-sizing settings. `-j` or `cfg-dispatch.jobs` is the only limit, so size concurrency for the heaviest test's memory use.

Normal interruption terminates the worker process groups. `SIGKILL` of the head process cannot run cleanup and can orphan `rb _test-job` children; inspect and stop them after a hard CI timeout or `kill -9`.
