## Future: redirecting the artifact root

The artifact root defaults to `command_root/artefacts/`. The `ExecutionContext` carrier is built to accept an explicit override so a future `--artifact-root` flag (or `root_config.yaml` field) can redirect large artifacts — synthesis netlists, simulation waveforms — onto a separate disk without touching command code. None of this is wired today; this page will update when it ships.
