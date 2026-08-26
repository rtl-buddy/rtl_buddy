## `rb nvim-install` requires git and network access

The default install clones a pinned `rtl-buddy-nvim` revision. For an air-gapped system, provide a local checkout:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <ref>
```

The plugin pin must speak the hub protocol shipped by rtl_buddy; maintainers update both together.
