## JSON output

```bash
rb tool-check --format json
```

Emits a structured payload with `tools`, `subcommands`, and a top-level `exit_code` (the same code the process exits with). Schema sketch:

```json
{
  "tools": {
    "verible": { "status": "ok", "version": "v0.0-3724", "path": "/opt/homebrew/bin/...", "optional": false },
    "sby":     { "status": "missing", "version": null, "path": null, "optional": true }
  },
  "subcommands": {
    "fpv":  { "status": "missing", "missing": ["sby"], "outdated": [], "optional_feature": true },
    "test": { "status": "ok", "missing": [], "outdated": [] }
  },
  "exit_code": 1
}
```

JSON output is the wire format for CI agents and IDE integrations — `rb tool-check --format json` is stable enough to script against. Combine with `--required-for` to narrow the result to a single subcommand.
