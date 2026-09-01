## Check the environment

```bash
rb tool-check                         # informational text report
rb tool-check --required-for fpv      # only FPV dependencies; enforced
rb tool-check --explain surfer        # status and install instructions
rb tool-check --strict                # gate all required tools
rb tool-check --format json           # bare JSON for scripts
rb --machine tool-check               # standard machine envelope
```

Optional tools appear by default. Use `--no-include-optional` to hide them. The report contains:

- **Tools:** canonical name, `ok` / `missing` / `outdated`, detected version, resolved path, minimum version, and optional status.
- **Subcommand readiness:** each declared `rb` command and the dependencies that block it. An optional feature does not make unrelated commands unready.

Use `--required-for <subcommand>` for a focused preflight. Use `--explain <tool>` after a wrapper reports a missing dependency; it prints the detected state, commands that use the tool, any optional binaries, and platform-specific install hints.

A tool that declares optional binaries lists them under `Optional binaries (not required; not detected as this tool)`, each with what it buys. They enrich the tool without being part of it: they never satisfy detection, never supply the probed version, and never change a `ok` / `missing` / `outdated` status. Slurm's `scontrol` is the example — `scontrol show config` supplies the cluster's `MaxArraySize`, so dispatch can split a resource group too large for one job array, and a submit host without it dispatches normally once `cfg-dispatch.max-array-size` is set. Reading the absence of an optional binary as a missing tool, or its presence as a present one, is exactly the confusion the separate section exists to prevent: a host with `scontrol` but no `sbatch` reports slurm `missing`.

Aliases are accepted by `--explain` and runtime dependency checks. Output always uses the canonical tool name. For example, `rtl-buddy-sch` resolves to `rtl-buddy-view`; an unknown-name machine response includes the known names and alias mapping.
