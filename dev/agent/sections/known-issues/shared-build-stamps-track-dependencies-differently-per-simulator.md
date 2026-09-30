## Shared-build stamps track dependencies differently per simulator

A build stamp decides whether a shared build can be reused.

- **Verilator** tracks the headers, libraries, standard includes and binary it reports consuming. Inputs under the project root are compared by content hash; inputs outside it, and any single input above 64 MB, by size and mtime.
- **VCS and Icarus** report no dependencies, so their stamps list every file in each `+incdir+` and `-y` directory and compare content. Adding, removing or editing a file there rebuilds, even one nothing includes. Verilator also compares that listing by file name.

The listing skips dot-directories, `artefacts/`, `obj_dir*` and rtl_buddy's own outputs, so a project directory with one of those names under an include path is not tracked. An `+incdir+` on a large tree slows every reuse check.

Do not point `+incdir+` at a directory a simulator or tool writes into: its scratch files change the listing and every run recompiles.

Environment variables, undeclared tool inputs and symlinked subdirectories are not tracked. For VCS and Icarus, an include resolved relative to the including file is also untracked. Force a compile with `--rebuild` after editing any of these.
