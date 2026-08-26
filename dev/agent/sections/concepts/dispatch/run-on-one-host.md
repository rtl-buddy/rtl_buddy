## Run on one host

`local-parallel` uses one global subprocess pool across all suites and resource groups:

```bash
rb regression --dispatch local-parallel       # min(4, CPU count)
rb regression --dispatch local-parallel -j 8
rb randtest my_test 20 --dispatch local-parallel -j 4
```

The default is `min(4, CPU count)`. Build jobs are prioritized because they unblock their suite. A simulation starts only after its build exits 0; a failed build prevents dependent simulations from starting and makes them dispatch failures.

CPU, memory, and time reservations are not enforced locally. A non-default reservation logs `dispatch.reservations_ignored`; choose `--jobs` for the memory demand of the heaviest concurrent tests. Local runs also produce no reservation advice.

Use `Ctrl-C` to stop the head and its process groups. `SIGKILL` prevents cleanup and may leave child processes running.
