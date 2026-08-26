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

Use `--required-for <subcommand>` for a focused preflight. Use `--explain <tool>` after a wrapper reports a missing dependency; it prints the detected state, commands that use the tool, and platform-specific install hints.

Aliases are accepted by `--explain` and runtime dependency checks. Output always uses the canonical tool name. For example, `rtl-buddy-sch` resolves to `rtl-buddy-view`; an unknown-name machine response includes the known names and alias mapping.
