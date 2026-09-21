## Gate scripts and CI

Exit behavior depends on the invocation:

| Invocation | Exit | Meaning |
|---|---:|---|
| `rb tool-check` | 0 | Informational, regardless of tool state |
| `rb tool-check --strict` | 0 | All required tools are ready |
| `rb tool-check --strict` | 1 | A required tool is missing, outdated, or unsupported |
| `rb tool-check --required-for <subcommand>` | 0 | That command's required tools are ready |
| `rb tool-check --required-for <subcommand>` | 2 | That command is blocked |

`--required-for` implies enforcement. Optional dependencies do not fail the global `--strict` check, but they do fail a focused check for a command that declares them required.

The JSON payload contains `tools`, `subcommands`, and `exit_code`. Each `tools` entry carries `status`, `version`, `path`, `optional`, and `minimum_version` when one is declared. A tool whose subcommands need different versions also carries `subcommand_minimum_versions`, and the matching `subcommands` entry reports `outdated` with a `minimum_versions` map naming the floor that was missed. The tool itself stays `ok`, because its other subcommands still work. `rtl-buddy-view` is the built-in case: 0.3.0 is enough for `rb hier`, `rb hier-query` and `rb hub`, while `rb graph` needs 0.4.0. Optional binaries are deliberately absent from it: they are documentation of what a tool can additionally use, not a state anything can gate on, so machine consumers see no field for them. `rb --machine tool-check --explain <tool>` mirrors the human explanation verbatim in the payload's `instructions` field, which is where they do appear. `exit_code` reports the would-be enforced result even when the informational command itself exits 0. `rb --machine tool-check` wraps the same payload in the standard machine envelope; prefer that form for agents.

Example focused CI gate:

```bash
rb tool-check --required-for fpv --strict || {
  echo "rb fpv is not ready"
  exit 1
}
```
