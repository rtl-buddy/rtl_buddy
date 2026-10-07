---
description: Cut an obfuscated and encrypted customer release of a design with rb release, keep released names stable, ship constraints that still match, and verify the package before it leaves.
---

# Customer releases

`rb release` turns a design model into a source drop for an outside consumer. By default every shipped design file is comment-stripped, obfuscated and encrypted. The package also carries a testbench the consumer runs to check their tool setup, the design's constraints rewritten for the released names, release notes and user guides. Before the package is accepted, the flow runs that testbench on the original sources, on the obfuscated sources and on the unpacked package, and requires the same result from each.

Each release is driven by one `release.yaml`. A project with several deliverables keeps one directory per deliverable, each with its own `release.yaml`, notes, documents and name maps:

```text
release/
  acme_cut/
    release.yaml
    maps/<version>.map      # archived name map, never shipped
    maps/<version>.json     # internal manifest
    notes/<version>.md
    docs/user_guide.md
    tb/                     # the release testbench and its run script
```

Run it from that directory, or pass `-c`:

```bash
rb release                               # release.yaml in the current directory
rb release -c release/acme_cut/release.yaml
rb release --trial                       # full run, verification included; nothing is archived
rb release --allow-dirty --no-verify     # quick look from a work-in-progress tree
rb release --reproduce /path/to/maps/1.0.0.json   # at the release's commit: re-cut it and compare
```

## Install the tools

`rb release` needs `verible-verilog-obfuscate` (part of the Verible release) and Synopsys VCS for IEEE-1735 encryption, plus whatever the verification command runs. Check with `rb tool-check --required-for release`. Constraint rewriting needs the `tcl` constraint reader, that is, a Python with `tkinter`; `rb tool-check` reports which reader is active (see [Tool Dependency Check](tool-check.md)).

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

The map and an internal manifest are copied into `maps/` only for a release cut from a clean tree with verification run. A clean tree has no modified or untracked file, submodules included, and every input (sources, headers, configs, notes, documents, constraints, key file) is tracked and unchanged in its repository. Otherwise an untracked or gitignored file placed earlier on an include path would ship in place of the committed one. Commit them with the release. A trial (`--trial`, `--allow-dirty` or `--no-verify`) leaves them in the artefact directory.

## Reproducing a release

Verible picks new names on every run, and IEEE-1735 encryption uses a fresh session key every time. So a release cannot be rebuilt byte for byte from its commit alone, but it can be rebuilt exactly from its commit and its archived map:

- the internal manifest (`maps/<version>.json`) records the commit, the tool versions and, for every shipped file, the SHA-256 of its source and of its plaintext before encryption;
- `rb release --reproduce <manifest>`, run at that commit, re-cuts the release with every name pinned to the archived map and fails unless every file's plaintext matches. It archives nothing;
- the tarball is written deterministically (sorted entries, the commit time as every timestamp, no owner, no gzip timestamp), so everything except the encrypted payloads is identical between cuts.

## Constraints

Each `design.constraints` entry names an SDC file and the preserved module it constrains (`scope`). The file is evaluated in the constraint reader's Tcl interpreter, so variables and loops are expanded, and written back out with:

- each `get_ports` pattern checked against the scope module's ports, failing on a pattern that matches none;
- each `get_cells` / `get_pins` / `get_nets` path translated segment by segment through the name map, including `_reg` and bit-select suffixes;
- `get_clocks` and the `all_*` queries passed through.

A wildcard that would have to match renamed names, `-filter`, and `source` cannot be translated faithfully and fail the release; list the objects explicitly. The rewritten files ship under `design/constraints/`.

A constraint script that queries the design while it runs (`get_property`, loops over `all_inputs`) cannot be evaluated without a netlist. Give it `mode: verbatim`: it ships unchanged after a check that it names no internal objects (`get_cells`, `get_pins`, `get_nets`) and that each literal `get_ports` pattern matches a port of its scope. Patterns built from variables are not checked.

## Verification

`verify.command` runs in a copy of each stage tree (`src`, `obf`) and in the unpacked tarball (`pkg`), from the release root, with `RELEASE_ROOT` and `RELEASE_STAGE` set. A stage passes when the command exits 0 and its output matches `verify.pass`. When `verify.compare` is set, the lines it matches must be identical in every stage, so a rename that changes behaviour cannot ship. Logs are in `artefacts/<name>-<version>/verify/`.

The flow also checks, independently of the testbench:

- every obfuscated file is the same length as its input, and no renamed original name remains in it;
- every encrypted file holds nothing outside its protected envelope;
- no file to be obfuscated uses a macro string quote, or token pasting that joins two name pieces (`` a``_nxt ``), which the lexical obfuscator renames inconsistently. A double backtick that only delimits an argument (`` ``O``.field ``) is fine. With `obfuscation.token-paste: preserve`, every name a paste can form, its literal pieces and any identifier pasted into it keep their spelling instead; the internal manifest lists them under `token_paste_preserved`.

## The package

```text
<name>-<version>/
  RELEASE_NOTES.md        # from package.notes
  MANIFEST                # sha256 and protection level of every file
  sim.f                   # every file of every directory, in compile order
  docs/                   # package.docs
  design/<top>.f          # the design's files, defines and external references
  design/*.svp, *.svh     # protected sources
  design/constraints/     # rewritten SDC
  <dir>/<dir>.f, <dir>/...# files a `dir:` rule moved, such as models the consumer replaces
  verif/tb.f, verif/...   # testbench, design files moved to verif/, testbench.extra-files
```

Design files ship in `design/` and testbench files in `verif/`. A `design.files` or `testbench.files` rule with `dir:` ships the matching files in another directory instead: behavioural memories and cells the consumer replaces with their own, or simulation-only support code that does not belong with the synthesisable sources. Each directory gets a filelist of its own files, and `sim.f` lists all of them in the original compile order, so packages still precede their users when files are split across directories. Paths in every filelist are relative to the release root.

The tarball is `artefacts/<name>-<version>/<name>-<version>.tar.gz`. The `stage/` directories beside it are internal: `stage/src` and `stage/obf` hold readable sources and must not be shipped.

See the [`release.yaml` reference](../reference/yaml.md#releaseyaml) for every key.
