## Gate scripts and CI

Exit behavior depends on the invocation:

| Invocation | Exit | Meaning |
|---|---:|---|
| `rb tool-check` | 0 | Informational, regardless of tool state |
| `rb tool-check --strict` | 0 | All required tools are ready |
| `rb tool-check --strict` | 1 | A required tool is missing or outdated |
| `rb tool-check --required-for <subcommand>` | 0 | That command's required tools are ready |
| `rb tool-check --required-for <subcommand>` | 2 | That command is blocked |

`--required-for` implies enforcement. Optional dependencies do not fail `--strict`.

The JSON payload contains `tools`, `subcommands`, and `exit_code`. Each `tools` entry carries `status`, `version`, `path`, `optional`, and `minimum_version` when one is declared. Optional binaries are deliberately absent from it: they are documentation of what a tool can additionally use, not a state anything can gate on, so machine consumers see no field for them. `rb --machine tool-check --explain <tool>` mirrors the human explanation verbatim in the payload's `instructions` field, which is where they do appear. `exit_code` reports the would-be enforced result even when the informational command itself exits 0. `rb --machine tool-check` wraps the same payload in the standard machine envelope; prefer that form for agents.

Example focused CI gate:

```bash
rb tool-check --required-for fpv --strict || {
  echo "rb fpv is not ready"
  exit 1
}
```
