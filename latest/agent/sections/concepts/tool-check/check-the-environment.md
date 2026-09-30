## Check the environment

```bash
rb tool-check                         # informational text report
rb tool-check --required-for fpv      # only FPV dependencies; enforced
rb tool-check --explain surfer        # status and install instructions
rb tool-check --strict                # gate all required tools
rb tool-check --format json           # bare JSON for scripts
rb --machine tool-check               # standard machine envelope
```

The report has two parts:

- **Tools:** name, status (`ok`, `missing`, `outdated` or `unsupported`), detected version, path, minimum version and whether the tool is optional. `unsupported` marks a tool newer than the range rtl-buddy is built against (pyslang 12 and later, for example) and blocks commands as `outdated` does.
- **Subcommand readiness:** each `rb` command and the dependencies blocking it. An optional tool can still block the commands that need it: pyslang does not block the core install, but blocks `elab` and `elab-regression`.

Optional tools are shown by default; `--no-include-optional` hides them. `--required-for <subcommand>` is a preflight for one command. `--explain <tool>` prints the detected state, the commands using the tool and platform install hints; use it after a wrapper reports a missing dependency. Aliases work, so `rtl-buddy-sch` resolves to `rtl-buddy-view`. An unknown name exits 1.

Versions are cached per binary path and modification time. `--no-probe-versions` skips probing for a faster presence-only check; versions show as unknown.
