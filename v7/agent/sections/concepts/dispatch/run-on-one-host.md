## Run on one host

`local-parallel` runs one subprocess pool across all suites. The default pool size is `min(4, CPU count)`.

```bash
rb regression --dispatch local-parallel
rb regression --dispatch local-parallel -j 8
rb randtest my_test 20 --dispatch local-parallel -j 4
```

- Builds run first. A simulation starts only after its build succeeds; if the build fails, its simulations are reported as failed.
- CPU, memory, and time reservations are not enforced. Choose `--jobs` for the memory demand of your heaviest concurrent tests.
- `Ctrl-C` stops the run and its child processes. `SIGKILL` can leave children running.
- Verilation is never split off and simulations are never released early.
