## Exit codes

The default invocation is **purely informational and always exits `0`**, even when a required tool is missing or outdated — so a bare `rb tool-check` in a script never fails the shell. Pass `--strict` (or `--required-for`) to make the process exit non-zero:

| Invocation | Exit | Meaning |
|------------|------|---------|
| `rb tool-check` (no flag) | `0` | Always — the report is printed and the command exits cleanly regardless of tool state |
| `rb tool-check --strict` | `0` | All required tools present and up-to-date (optional gaps don't matter) |
| `rb tool-check --strict` | `1` | At least one required tool is missing or outdated |
| `rb tool-check --required-for <sub>` | `0` | That subcommand's required deps are all present and up-to-date |
| `rb tool-check --required-for <sub>` | `2` | That subcommand's deps are missing/outdated |

`--required-for` always enforces (it implies `--strict`), and uses exit `2` to make the "this one subcommand is broken" case distinguishable from `--strict`'s broader "some required tool, somewhere, is missing" exit `1`. Note that the JSON output's top-level `exit_code` always reports the *would-be strict* code (e.g. `1` when a required tool is missing) even in the default mode where the process itself exits `0` — so CI can either read that field or use `--strict` to gate on the process exit.
