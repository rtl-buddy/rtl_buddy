## Check that every build compiles

`-E comp` under dispatch submits each suite's build job, and its verilate job where [verilation is split off](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/dispatch/#split-verilation-from-the-c-build), and no simulation:

```bash
rb -E comp regression --dispatch slurm
rb -E comp regression --dispatch local-parallel -j 4
```

- Each test reports as an in-process `-E comp` run does: `NA` with `early_stop: true` once its build compiled, and `FAIL` with the build job's error line when it did not, or with the `preproc` or filelist error that stopped it first. A failure exits 1.
- Each test gets its `result.json`, and the earlier logs and coverage of its `randtest` runs are removed. No simulation job runs, so no `dispatch/result-<tag>.json` is written.
- The build job compiles every planned test, including those whose builder cannot share a build. Tests on one compile key share its build and its result.
- Builds compile `compile.parallel` at a time in each suite's build job (see [Compile several builds at once](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/dispatch/#compile-several-builds-at-once)). `-j` runs several suites' build jobs at once; it does not raise the concurrency within one suite.
- A later full run reuses the builds and compiles nothing for unchanged inputs. Under Slurm its verilate job finds them built and releases their simulations at once.
- `-E pre` and `-E sim` are rejected under dispatch. Run them with `--dispatch local`.
- A compile-only run is adopted only by another compile-only run, and a full run only by a full run (see [Interrupted runs](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/dispatch/#interrupted-runs-warn-cancel-adopt)).
