## Install the nvim integration

```bash
rb nvim-install
rb nvim-install --update
```

This installs a compatible revision of `rtl-buddy-nvim` and writes an auto-loaded setup file, so `init.lua` needs no change. It needs Git and network access. For an offline checkout:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <branch>
```

`--force` replaces a broken install. In nvim, `:checkhealth rtlbuddy` verifies the hub, language-server and wave integration.
