## Pruning Verilator intermediates

Enable intermediate pruning when shared Verilator builds consume too much disk space and you only need their executable for later simulations:

```yaml
cfg-rtl-reg:
  reg-cfg-path: regression.yaml
  shared-build-prune: true
```

The [configuration reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#regression-manifest-defaults) defines the setting. It defaults to false and applies with `--share-build`, including dispatch and persistent caches. Only a newly completed, successfully stamped Verilator build is pruned; existing cache hits, other simulators, and the verilate-only half of a split build are left intact. Use `--rebuild` to rebuild and prune an existing entry.

Pruning removes generated C++ sources, headers, makefiles, object files, static archives, dependency files and precompiled headers from the build directory's top level. Generated sources are identified by Verilator's prefix metadata and file headers. It preserves `simv`, the compile stamp, toolchain metadata, the build lock, subdirectories, shared libraries and other runtime assets. Symlink targets are never traversed.

The saved stamp still tracks consumed RTL and headers, so unchanged inputs reuse the executable and edited inputs rebuild correctly. A rebuild regenerates the removed files and performs a full C++ compile. Waveforms and coverage remain available from the executable, but generated C++ is unavailable for debugging until pruning is disabled and the build is forced again.

Cleanup holds the build lock and invalidates split-verilation, skip-identical and [reusable-verilation](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tests/#changing-only-c-compile-or-link-flags) metadata before removing generated sources. A directory that only holds a verilation for builds with other C++ flags is never pruned. If the filesystem cannot acquire the lock, pruning is skipped. Cleanup errors warn without failing the compiled simulation. `compile.shared_build_pruned` records `removed_files` and `removed_bytes` in the run log.
