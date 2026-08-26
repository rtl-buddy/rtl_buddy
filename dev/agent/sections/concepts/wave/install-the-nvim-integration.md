## Install the nvim integration

```bash
rb nvim-install
rb nvim-install --update
```

The command installs a compatible revision of `rtl-buddy-nvim` and writes an auto-loaded setup file; no `init.lua` change is required. It needs Git and network access. For an offline checkout:

```bash
rb nvim-install --source /path/to/rtl-buddy-nvim --ref <branch>
```

Use `--force` to replace a broken install. Run `:checkhealth rtlbuddy` in nvim to verify hub, language-server, and wave integration.
