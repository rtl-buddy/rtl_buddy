## Read the JSON payload

Agents should use `rb --machine tool-check`; `--format json` prints the same payload bare.

- The payload has `tools`, `subcommands` and `exit_code`. `exit_code` is the result an enforced run would give, even when the informational command exits 0.
- Each `tools` entry has `status`, `version`, `path`, `optional` and, when declared, `minimum_version`.
- When subcommands of one tool need different versions, the tool stays `ok` and the affected `subcommands` entry reports `outdated` with the missed minimum. `rtl-buddy-view` 0.3.0 is enough for `rb hier`, `rb hier-query` and `rb hub`, but `rb graph` needs 0.4.0.
- `rb --machine tool-check --explain <tool>` returns the human explanation, optional binaries included, in `instructions`. An unknown tool lists the known names and aliases.
