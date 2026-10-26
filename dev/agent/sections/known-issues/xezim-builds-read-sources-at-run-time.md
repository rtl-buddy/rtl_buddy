## xezim builds read sources at run time

A xezim `simv` is a wrapper that re-reads the sources each run, through a copy of `run.f` kept in the build directory. With `shared-build-root`, a build compiled in one checkout reads that checkout's sources when another checkout reuses it, so removing the first checkout breaks the reuse. Pass `--rebuild` after moving or deleting a checkout.
