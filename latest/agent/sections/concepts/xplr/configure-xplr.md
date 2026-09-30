## Configure xplr

All `cfg-xplr` keys are optional:

```yaml
cfg-xplr:
  commit-mode: auto
  source-scope: ["."]
  disk-high-watermark-gb: 50
  disk-hard-cap-gb: 80
  eviction-policy: keep-frontier
  worktree-root: artefacts/xplr/worktrees
```

`rb xplr` needs only a `root_config.yaml` or a Git root and loads no builder or platform configuration. From elsewhere, anchor discovery explicitly:

```bash
rb xplr --root /path/to/project frontier
```

See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v6/reference/cli/) for the full command surface.
