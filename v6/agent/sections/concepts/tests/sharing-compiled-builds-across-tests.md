## Sharing compiled builds across tests

Use `--share-build` when tests differ only at runtime:

```bash
rb test --share-build
rb regression --share-build
```

RTL Buddy stores shared builds under `artefacts/.shared-builds/obj_dir_<hash>/`. The key includes the resolved simulator executable, compile options, plusdefines, compile environment, and resolved filelist. Plusargs, seeds, and simulation timeouts do not affect it.

A compile stamp records the content hash of every tracked input under the project root, plus toolchain identity. Reuse occurs only while the stamp matches, and content is what decides: regenerating a source byte-for-byte reuses the build; any real edit rebuilds it, including one a node's cached `stat` still describes as the old file. Verilator also reports consumed dependencies, so included headers, `-y` library files, standard includes, and the underlying Verilator binary invalidate the build. VCS and Icarus report none, so the stamp additionally lists each `+incdir+` and `-y` directory the filelist names: without a dependency file, editing, adding, or removing a file in one rebuilds; with one, the listing is compared by name alone, because the dependency file already decides the content of everything the build read and an added `-y` file is the case it cannot report. Listings are unfiltered by suffix; an `+incdir+` is walked recursively and a `-y` directory listed flat, following what each option's search can reach. The walk skips dot-directories, `__pycache__`, and RTL Buddy's own `artefacts/`, `.shared-builds/` and `obj_dir*` trees, plus editor and VCS bookkeeping files and RTL Buddy's own outputs by name (`run.f`, `compile.log`, `test.log`, `result.json`, `rtl_buddy.log`, the stamp, and the rest) — all of those are written after the fingerprint that would list them, so stamping one would make every later run recompile. A header a `preproc` hook generates into its `artifact_dir` **is** tracked, and other dot-files are too, since `` `include ".config.svh" `` resolves. After a change outside what the listing and a dependency file cover — a hidden toolchain change, an include reached by a path no `+incdir+` names — force compilation with `--rebuild`.

Reuse is announced rather than inferred from a missing log:

```bash
rb test smoke --share-build
# smoke: reused shared build obj_dir_b21cded073f27c1c (built 2m14s ago, Verilator 5.026 2024-11-05 rev v5.026); nothing compiled

rb test smoke --share-build --rebuild   # compile it again anyway
```

The test's `compile.log` records the same breadcrumb, with the command a rebuild would run. `--rebuild` forces one rebuild per build directory per invocation and says nothing about whether builds are shared; dropping `--share-build` does not force one under `--dispatch`, which implies it.

A Verilator build directory also records which Verilator compiled into it (`rb-toolchain.json`: the resolved executable, its version, and the host platform), whether or not builds are shared. When the next compile would run under a different one — an upgrade, another install, or the same checkout built on a laptop and then on a cluster node — or under `--rebuild`, RTL Buddy deletes the directory's `*.o`, `*.d` and `*.a` first, so the C++ build starts clean instead of make following dependency files that name the old toolchain's headers (`No rule to make target '.../include/verilated.cpp'`). The console says so: `basic: dropped 12 stale object/dependency files from … (built by …, now …); the C++ build starts clean`. An ordinary source edit keeps the objects and stays incremental. A directory from before this record starts clean once.

Verilator, VCS, and Icarus support shared builds. An unsupported builder or an absolute `builder-simv` uses the test's own build directory and logs why cross-test sharing was declined. RTL Buddy overrides relative output-location options so the shared directory owns `simv`.

### Persistent build cache

`artefacts/.shared-builds/` lives inside the workspace, so a CI job that wipes the workspace after every run (`deleteDir()`, `git clean -ffdx`) throws the cache away and recompiles inputs that never changed. Point `shared-build-root` at a directory outside the workspace and the cache outlives the run:

```yaml
cfg-rtl-reg:
  reg-cfg-path: regression.yaml
  shared-build-root: /shared/nfs/rb-build-cache
```

```bash
rb regression --share-build --shared-build-root /shared/nfs/rb-build-cache
```

`--shared-build-root` beats `RTL_BUDDY_SHARED_BUILD_ROOT`, which beats the config key; an empty value at either of the first two turns the cache off for that run, and under `--dispatch` that disable is forwarded to the build and simulation jobs so they do not re-enable it from the config. A relative root resolves against the project root — the directory holding `root_config.yaml` — not the working directory, so a dispatched build job and its simulation jobs resolve one path. The root is created on demand and only applies with `--share-build` (which `--dispatch` implies).

Builds land in `<root>/<suite-namespace>/obj_dir_<key>/`, where the namespace is the suite directory relative to the project root with `/` replaced by `__` (`verif/demo_tiny_alu` → `verif__demo_tiny_alu`); a suite outside the project root gets a digest of its absolute path instead. Nothing in the layout names the checkout, which is the point: every checkout on the host shares the cache.

In this mode the compile key changes shape. It is spelled relative to the project root — `run.f` entries and any absolute in-root path in the compile line alike — and it **includes the content hash** of every tracked input. That covers what the compile *line* names as well as what `run.f` does: an absolute in-root `+incdir+` or `-y` directory, a `-v` file, or a bare source path reaching the builder through `builder-opts.compile-time` contributes the same content identity a filelist entry does (a file its hash, a directory the hashes of the files inside it, pruned and filtered by the same rules). A `-f`/`-F` filelist contributes what it *names*, not just its own bytes: the chain is expanded recursively (bounded depth, cycle-safe) and every in-root source, include directory and further list in it is keyed. A chain deeper than the bound fails closed — what was not read enters the key as an absolute path, which makes that suite's key checkout-specific rather than promising inputs nobody looked at, and `compile.cache_key_depth_bound` says so once. The two options are read the way the simulators read them — a relative entry inside a `-f` list resolves against the builder's working directory, one inside a `-F` list against the directory holding that list, and a nested `-f`/`-F` resets the rule for the file it names — so the key hashes the files the build actually opens. One list reached under two bases is therefore read under each, since its relative entries name different files each time, while the same list under the same base is read once. A relative entry whose base is unknown is left as text rather than guessed at. Otherwise two checkouts whose `run.f` matched but whose header under such an `+incdir+` — or whose RTL behind byte-identical nested lists — differed would take one directory and rebuild over each other. (`run.f` itself has no nested lists: RTL Buddy unrolls every `-F` chain when it writes one.)

Only tokens RTL Buddy resolves as a path are relativised. A `+define+NAME=<path>` — on the compile line or in `tests.yaml` `plusdefines`, which reach `run.f` — a `-D`/`-G`/`-pvalue+`, a `+libext+`, or any other `key=value` token keeps its value exactly as written, because a define's value is compiled *into* the model: two checkouts whose builds bake in different absolute paths must get different keys, not one shared binary. An in-root path *embedded* in a larger option is the other way round — it names a directory the build really reads, so it is relativised **and** keyed by its listing. Both spellings count: an absolute one (`-CFLAGS=-I<root>/inc`) is found by its project-root prefix under any option, and a relative one (`-CFLAGS=-I../../inc`, the usual `builder-opts.compile-time` spelling) by the option that introduces it — `-I`, `+incdir+` or `-y` — then resolved against the builder's working directory. A payload that resolves to nothing under the project root stays text, and one directory named twice in the command line is keyed once. A compile-line path *outside* the project root stays part of the key as text; a relative one is resolved against the directory the builder will run in, so `+incdir+inc` is keyed by its contents like any other, and it falls back to text only where that directory is not yet known. An output location such as `-o` is relativised so the key carries no checkout prefix, but is never read. Only `.shared-builds/` and `obj_dir*` *below the project root* are refused outright, so a workspace that itself sits under a dot-directory (`/home/ci/.worktrees/pr`) is keyed like any other; an `+incdir+` into an artefact tree is listed with RTL Buddy's own outputs excluded by name, which is how a `preproc` hook's generated headers are tracked, and a file named directly there is refused only when its own name is one of those outputs.

An input RTL Buddy cannot hash — one above the 64 MB cap, typically a ROM or memory-init image — falls back to its size and modification time rather than to nothing, so two checkouts with different images cannot collide on one directory. Mtimes differ per checkout, so a suite with such an input stops sharing builds across checkouts; it still reuses its own. Inputs outside the project root are unaffected: two checkouts naming one absolute path name the same bytes.

Two checkouts with byte-identical inputs therefore get the same directory and reuse each other's build wherever they sit; two checkouts on different commits get different directories, instead of rebuilding over one another. That makes the directory content-addressed, so a run keeps one directory per distinct input set rather than one per key — a cache to prune rather than a build to rebuild. The stamp is checked on top of the key as usual, and records the project root it was written from so another checkout can re-anchor its entries.

Switching the cache on or off changes every key, so the first run after either compiles once. Nothing prunes the cache; do it between runs, never during one (the build lock lives inside the directory it guards — see [Known issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/)):

```bash
find /shared/nfs/rb-build-cache -mindepth 2 -maxdepth 2 -name 'obj_dir_*' -mtime +14 -exec rm -rf {} +
```

A generated input that is not reproducible byte-for-byte — a `preproc` hook that stamps a timestamp into a header — moves the key rather than only the stamp here, so it strands a directory per run instead of rebuilding in place.
