## The package

```text
<name>-<version>/
  RELEASE_NOTES.md        # from package.notes
  MANIFEST                # sha256 and protection level of every file
  docs/                   # package.docs
  design/<top>.f          # paths relative to the release root
  design/*.svp, *.svh     # protected sources
  design/constraints/     # rewritten SDC
  verif/tb.f, verif/...   # testbench and testbench.extra-files
```

The tarball is `artefacts/<name>-<version>/<name>-<version>.tar.gz`. The `stage/` directories beside it are internal: `stage/src` and `stage/obf` hold readable sources and must not be shipped.

See the [`release.yaml` reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#releaseyaml) for every key.
