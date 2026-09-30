## Gate scripts and CI

| Invocation | Exit | Meaning |
|---|---:|---|
| `rb tool-check` | 0 | Informational, regardless of tool state |
| `rb tool-check --strict` | 0 | All required tools are ready |
| `rb tool-check --strict` | 1 | A required tool is missing, outdated or unsupported |
| `rb tool-check --required-for <subcommand>` | 0 | That command's required tools are ready |
| `rb tool-check --required-for <subcommand>` | 2 | That command is blocked |

`--required-for` implies enforcement. Optional dependencies do not fail the global `--strict` check, but do fail a focused check for a command that requires them.

```bash
rb tool-check --required-for fpv --strict || {
  echo "rb fpv is not ready"
  exit 1
}
```
