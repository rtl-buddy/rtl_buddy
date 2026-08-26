## Exit codes

| Exit | Meaning |
|------|---------|
| `0` | All required tools present and up-to-date (optional gaps don't matter) |
| `1` | At least one required tool missing or outdated |
| `2` | `--required-for <sub>` was passed and that subcommand's deps are missing/outdated |

`--strict` is implied for `--required-for` — exit code semantics differ from the default to make the "this one subcommand is broken" case distinguishable from the broader "some tool, somewhere, is missing" case.
