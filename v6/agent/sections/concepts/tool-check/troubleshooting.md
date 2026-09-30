## Troubleshooting

| Symptom | Action |
|---|---|
| ``<tool> not found — run `rb tool-check --explain <tool>` for install instructions`` | Run the suggested command and install the tool |
| Tool `missing` although installed | Put it on `PATH`, or set its path in `root_config.yaml` (see [Apply project configuration](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/tool-check/#apply-project-configuration)) |
| Tool `outdated` or `unsupported` | Install a version in the supported range, or adjust `cfg-tools` minimums |
| Command blocked by an optional tool | Install that tool, or do not run the command |
| `--explain` exits 1 | The tool name is unknown; use a name or alias from `rb tool-check` |
| `--required-for` exits 2 | The named command is blocked; read the readiness section |
| Slurm reports `missing` with only `scontrol` present | None of `sbatch`, `squeue`, `sacct` or `scancel` was found on `PATH`; install them, since `scontrol` alone is not detected as Slurm |
