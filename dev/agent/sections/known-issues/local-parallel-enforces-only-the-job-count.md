## local-parallel enforces only the job count

The `local-parallel` backend ignores CPU, memory, time, array-throttle, and right-sizing settings. `-j` or `cfg-dispatch.jobs` is the only limit, so size concurrency for the heaviest test's memory use.

`cfg-dispatch.compile.parallel` is the exception: it is not a reservation but concurrency the build job itself honours, and that job occupies one pool slot while fanning out inside it. The real ceiling on the host is therefore `jobs` multiplied by `compile.parallel`, and nothing clamps it. Size the two together.

Normal interruption terminates the worker process groups. `SIGKILL` of the head process cannot run cleanup and can orphan `rb _test-job` children; inspect and stop them after a hard CI timeout or `kill -9`.
