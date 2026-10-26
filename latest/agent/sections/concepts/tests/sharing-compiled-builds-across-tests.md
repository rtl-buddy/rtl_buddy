## Sharing compiled builds across tests

`--share-build` reuses one compiled build across tests that differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

`--dispatch` implies it. Verilator, VCS, Icarus and xezim support it; with another builder, or an absolute `builder-simv`, the test uses its own build directory and logs why sharing was declined.

- Builds live in `artefacts/.shared-builds/obj_dir_<hash>/`. Plusargs, seeds and simulation timeouts do not affect the build; compile options, plusdefines, the simulator and the resolved filelist do.
- Reuse requires that no tracked input under the project root changed by content. Regenerating a file byte-for-byte reuses the build; any real edit rebuilds it.
- Verilator reports the files it read, so headers, `-y` library files and the Verilator binary itself invalidate the build.
- VCS, Icarus and xezim report none, so every file under each `+incdir+` directory (recursively) and `-y` directory (flat) is tracked. Editing, adding or removing any of them rebuilds.
- After a change rtl_buddy cannot see, such as a toolchain change or an include reached through a path no `+incdir+` names, force a compile with `--rebuild`.

### See whether a build was reused

Reuse is announced once per build directory:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild
```

The test's `compile.log` records the same message with the command a rebuild would run. A compile that ran leaves its transcript there. `--rebuild` forces one rebuild per build directory per invocation, and dropping `--share-build` under `--dispatch` does not stop reuse.

### Changed Verilator toolchain

If a Verilator build directory was compiled by a different Verilator (an upgrade, another install, or the same checkout built on a laptop and a cluster node) or runs under `--rebuild`, rtl_buddy deletes its `*.o`, `*.d` and `*.a` files first. This prevents make errors such as `No rule to make target '.../include/verilated.cpp'`. The console reports it:

```text
basic: dropped 12 stale object/dependency files from … (built by …, now …); the C++ build starts clean
```

An ordinary source edit keeps the objects and stays incremental unless [intermediate pruning](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/tests/#pruning-verilator-intermediates) is enabled.

### Changing only C++ compile or link flags

Verilator passes `-CFLAGS` and `-LDFLAGS` only to the C++ compile and link of the model. A Verilator build that differs from another only in those options reuses that build's verilation and reruns just the C++ build:

```text
basic: reusing the verilation in obj_dir_c6d5c70100b51c6d; only the C++ build runs, since the compile line differs from it only in -CFLAGS/-LDFLAGS
```

- Each set of C++ flags still gets its own build directory, keyed on the whole compile line, so builds with different flags never share object files or an executable. Existing build directories keep their keys.
- The verilation lives in the build directory of the same compile line without its `-CFLAGS` and `-LDFLAGS`. A build with neither option verilates there as before and records its verilation in `.rb-verilation.json`. A build with them verilates there without them if no recorded verilation matches the current inputs, then copies the generated C++ into its own directory, rewrites the C++ flag lists in `V<top>.mk` to its own, and runs `verilator ... --no-verilate`. The resulting executable is stamped like any other build.
- The copy rewrites only files whose content changed, so after a source edit make recompiles only what the new verilation changed.
- A verilation whose make failed is still recorded, so after a link failure (for example, a model too large for the default code model) adding a flag such as `-CFLAGS -mcmodel=large` does not verilate again.
- In YAML, compile options are split on whitespace, so give each flag its own option: `-CFLAGS -O2 -CFLAGS -g`, not `-CFLAGS "-O2 -g"`.
- The full compile runs as before under `--rebuild`, with `--hierarchical`, with a Verilator without `--no-verilate`, when a `-f` file or a library or object named on the compile line precedes a `-CFLAGS`/`-LDFLAGS` option (the flag order could differ from Verilator's), and when the verilation belongs to another checkout sharing a [persistent cache](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/tests/#persistent-build-cache).
- Only `-CFLAGS` and `-LDFLAGS` are treated this way. `-MAKEFLAGS` and every other option still key the verilation.
