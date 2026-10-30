## `rb nvim-install` requires git and network access

The default install clones a pinned `rtl-buddy-nvim` revision. On an air-gapped system, pass a local checkout with `rb nvim-install --source /path/to/rtl-buddy-nvim --ref <ref>`. The pinned plugin must speak the hub protocol shipped by rtl_buddy.
