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
  csr/                    # customer register map: .rdl, .h, .svh, .md (csr:)
  <dir>/<dir>.f, <dir>/...# files a `dir:` rule moved, such as models the consumer replaces
  verif/tb.f, verif/...   # testbench, design files moved to verif/, testbench.extra-files
```

Design files ship in `design/` and testbench files in `verif/`. A `design.files` or `testbench.files` rule with `dir:` ships the matching files in another directory instead: behavioural memories and cells the consumer replaces with their own, or simulation-only support code that does not belong with the synthesisable sources. Each directory gets a filelist of its own files, and `sim.f` lists all of them in the original compile order, so packages still precede their users when files are split across directories. Paths in every filelist are relative to the release root.

The tarball is `artefacts/<name>-<version>/<name>-<version>.tar.gz`. The `stage/` directories beside it are internal: `stage/src` and `stage/obf` hold readable sources and must not be shipped.

See the [`release.yaml` reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#releaseyaml) for every key.
