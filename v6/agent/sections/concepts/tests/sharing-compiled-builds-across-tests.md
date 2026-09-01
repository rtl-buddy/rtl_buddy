## Sharing compiled builds across tests

Use `--share-build` when tests differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

RTL Buddy stores shared builds under `artefacts/.shared-builds/obj_dir_<hash>/`. The key includes the resolved simulator executable, compile options, plusdefines, compile environment, and resolved filelist. Plusargs, seeds, and simulation timeouts do not affect it.

A compile stamp records the content hash of every tracked input under the project root, plus toolchain identity. Reuse occurs only while the stamp matches, and content is what decides: regenerating a source byte-for-byte reuses the build; any real edit rebuilds it, including one a node's cached `stat` still describes as the old file. Verilator also reports consumed dependencies, so included headers, `-y` library files, standard includes, and the underlying Verilator binary invalidate the build. VCS and Icarus report none, so for every builder the stamp additionally lists the contents of each `+incdir+` and `-y` directory the filelist names: editing, adding, or removing a file in one rebuilds whatever the simulator, and an added `-y` file is the case no dependency file can report. Listings are unfiltered by suffix; an `+incdir+` is walked recursively and a `-y` directory listed flat, following what each option's search can reach. The walk skips dot-directories and RTL Buddy's own `artefacts/`, `.shared-builds/` and `obj_dir*` trees, plus editor and VCS bookkeeping files and RTL Buddy's own per-test outputs by name (`run.f`, `compile.log`, `test.log`, `result.json`, the stamp, and the rest) — all of those are written after the fingerprint that would list them, so stamping one would make every later run recompile. A header a `preproc` hook generates into its `artifact_dir` **is** tracked, and other dot-files are too, since `` `include ".config.svh" `` resolves. After a change outside what the listing and a dependency file cover — a hidden toolchain change, an include reached by a path no `+incdir+` names — force compilation with `--rebuild`.

Reuse is announced rather than inferred from a missing log:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild   # compile it again anyway
```

The test's `compile.log` records the same breadcrumb, with the command a rebuild would run. `--rebuild` forces one rebuild per build directory per invocation and says nothing about whether builds are shared; dropping `--share-build` does not force one under `--dispatch`, which implies it.

Verilator, VCS, and Icarus support shared builds. An unsupported builder or an absolute `builder-simv` uses the test's own build directory and logs why cross-test sharing was declined. RTL Buddy overrides relative output-location options so the shared directory owns `simv`.
