## Parse command results

Structured commands print this top-level shape:

```json
{
  "command": "test",
  "exit_code": 0,
  "meta": {
    "rtl_buddy_version": "6.40.0",
    "argv": ["rb", "--machine", "test", "basic"],
    "cwd": "/path/to/suite",
    "git": {"branch": "main", "commit": "abc1234", "modified": 0, "staged": 0}
  },
  "payload": {
    "results": [{"name": "basic", "result": "PASS", "desc": "basic completed"}]
  }
}
```

`meta.cwd` is the invocation directory and `meta.git` describes the project root, so they differ when `rb` runs from outside the checkout.

Parse the whole stdout with `json.loads()`. `command`, `exit_code`, `meta`, and the command-specific `payload` are stable. Optional fields may be added under `meta` or `payload`; an incompatible change needs a major version.

Payload conventions:

- Listing commands use `payload.names`.
- Regression results use `payload.results` and include `suite`.
- Elaboration results include top, source and diagnostic counts, elapsed time, peak memory, and `result_json`.
- `docs list` uses `payload.pages`.
- Coverage and formal results carry structured metrics and artefact paths.

See [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/) and [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpv/) for their payloads, and [Tests](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/tests/#interpret-results) for statuses and exit codes.
