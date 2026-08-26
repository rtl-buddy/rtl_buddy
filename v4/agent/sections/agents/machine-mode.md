## Machine mode

Passing `--machine` switches `rtl_buddy` into a mode designed for programmatic consumption:

- `rtl_buddy.log` is written as **JSON Lines** instead of human-readable text.
- Console output drops Rich formatting, colors, and spinners.
- Commands that produce structured results print a single JSON envelope to **stdout** on exit.

The intent is that an orchestrator can determine the outcome of a run by parsing the stdout envelope, and reconstruct timing or per-event detail from `rtl_buddy.log`, without screen-scraping human-formatted output.

```bash
rtl-buddy --machine test basic
rtl-buddy --machine regression -c design/regression.yaml
```

### Stdout envelope

In machine mode, structured-result commands print a single JSON object to stdout on exit:

```json
{
  "command": "test",
  "exit_code": 0,
  "meta": {
    "rtl_buddy_version": "2.4.0",
    "argv": ["rtl-buddy", "--machine", "test", "basic"],
    "cwd": "/path/to/suite",
    "git": {"branch": "main", "commit": "abc1234", "modified": 0, "staged": 0}
  },
  "payload": {
    "results": [
      {"name": "basic", "result": "PASS", "desc": "basic completed"}
    ]
  }
}
```

The envelope shape is the same across commands:

- `command` — the subcommand that was run (`"test"`, `"regression"`, `"synth"`, …).
- `exit_code` — integer exit code (see [exit codes](https://rtl-buddy.github.io/rtl_buddy/v4/concepts/tests/#exit-codes)).
- `meta` — version, argv, working directory, and git status at invocation.
- `payload` — command-specific structured data.

The top-level envelope fields are reserved and versioned by `meta.rtl_buddy_version` under `rtl_buddy`'s normal semantic-versioning rules. Adding optional fields under `meta` or `payload` is non-breaking; removing fields, renaming fields, changing field types, or changing the meaning of an existing field is breaking.

Conventions inside `payload`:

- `--list` commands (`test --list`, `synth --list`, …) populate `payload.names`.
- Regression commands populate `payload.results`, with a `"suite"` field on each entry.
- `docs list` populates `payload.pages` with `slug`, `title`, and `description` from page frontmatter.

### JSONL log format

In machine mode, each line of `rtl_buddy.log` is a JSON object describing one event:

```json
{"event": "sim.completed", "test": "smoke", "duration_sec": 4.2, "message": "smoke: simulation completed in 4.20s"}
{"event": "postproc.completed", "test": "smoke", "result": "PASS", "desc": "smoke completed", "message": "smoke: post-processing completed with result PASS (smoke completed)"}
```

Common fields:

- `event` — dotted event name identifying what happened (`sim.start`, `compile.failed`, `postproc.completed`, …).
- `message` — the human-readable rendering of the event.
- Event-specific fields — test name, duration, seed, exit code, file paths, etc.

The authoritative per-test outcome is the `postproc.completed` event's `result` and `desc` fields. For multi-suite commands, each suite directory gets its own `rtl_buddy.log`.
