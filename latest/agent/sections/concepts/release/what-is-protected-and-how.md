## What is protected, and how

| Step | Applies to | What it does |
|---|---|---|
| Collect | design model + `testbench.filelist` | Resolves the filelists, follows `` `include `` through the include directories, and copies each section flat into `design/` or `verif/`. Files under a `design.externals` path are not shipped; their filelist lines are rewritten to the customer's location. |
| Strip comments | `strip-comments: true` (design default) | Removes comments, keeping line numbers and any comment that matches `obfuscation.keep-comments` (synthesis and simulator directives). |
| Obfuscate | `obfuscate: true` (design default) | Renames identifiers with Verible through one name map shared by every file. |
| Encrypt | `encrypt: true` (design default) | Wraps the file in `pragma protect begin`/`end` and encrypts it with `vcs -ipprotect` and the release's key file. A filelist entry gains a `p` (`.sv` becomes `.svp`); an included header keeps its name, so `` `include `` still finds it. |

A testbench defaults to neither obfuscated nor encrypted. `design.files` and `testbench.files` override the defaults per file by base-name glob, and every override carries a `reason`. A rule that matches no shipped file is an error, so the list of exceptions cannot drift from the design.

### Which names are kept

The obfuscator is lexical: it renames a spelling everywhere it appears. The flow seeds its map with the names that must not change, and they are kept in every file:

- the top module's name, ports and parameters, and those of every module in `design.preserve.interfaces`;
- module, port and parameter names declared by external files, so instances of vendor cells still bind;
- every `+define+` name in the filelists, the language's `__FILE__` and `__LINE__`, and the macros tools predefine (`SYNTHESIS`, `VERILATOR`, `VCS`...);
- SystemVerilog's built-in method names (`push_back`, `size`, `len`, `min`...), which the obfuscator would otherwise rename at call sites;
- `design.preserve.identifiers`;
- every identifier used by a shipped file that is not obfuscated, testbench included.

The last rule is what makes a clear testbench compile against an obfuscated design, and it is also its cost: a name the testbench uses is kept across the whole design. So a testbench may name a design unit (a module, package or interface) only if the unit is a preserved interface or is listed in `testbench.allow-design-refs`; anything else is an error. Publish the types or constants a testbench needs in a package made for the purpose rather than widening that list. Hierarchical references into an encrypted module do not compile at all, and the verification run catches them.

### Stable names across releases

`obfuscation.continue-from: previous` (the default) starts each release from the newest older release's map in `maps/`, so a name keeps its released spelling from one drop to the next and a consumer's constraints and reports stay valid. New names get new spellings. A version whose map already exists is refused unless `--force` is given, because the archived map is the only way to read a release's names later.

The map and an internal manifest are copied into `maps/` only for a release cut from a clean tree with verification run. Commit them with the release. A trial (`--trial`, `--allow-dirty` or `--no-verify`) leaves them in the artefact directory.
