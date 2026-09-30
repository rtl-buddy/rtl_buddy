## Sharing compiled builds across tests

`--share-build` reuses one compiled build across tests that differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

`--dispatch` implies it. Verilator, VCS and Icarus support it; with another builder, or an absolute `builder-simv`, the test uses its own build directory and logs why sharing was declined.

- Builds live in `artefacts/.shared-builds/obj_dir_<hash>/`. Plusargs, seeds and simulation timeouts do not affect the build; compile options, plusdefines, the simulator and the resolved filelist do.
- Reuse requires that no tracked input under the project root changed by content. Regenerating a file byte-for-byte reuses the build; any real edit rebuilds it.
- Verilator reports the files it read, so headers, `-y` library files and the Verilator binary itself invalidate the build.
- VCS and Icarus report none, so every file under each `+incdir+` directory (recursively) and `-y` directory (flat) is tracked. Editing, adding or removing any of them rebuilds.
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

An ordinary source edit keeps the objects and stays incremental.
