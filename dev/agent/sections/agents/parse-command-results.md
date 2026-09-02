## Parse command results

Structured commands emit this top-level shape:

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

Parse the whole stdout value with `json.loads()`. The stable top-level fields are `command`, `exit_code`, `meta`, and command-specific `payload`. Optional fields may be added under `meta` or `payload`; incompatible changes require a major version change.

Common payload conventions:

- listing commands use `payload.names`;
- regression results use `payload.results` and include `suite`;
- elaboration results include top, source and diagnostic counts, elapsed time, peak memory, and `result_json`;
- `docs list` uses `payload.pages`;
- coverage and formal commands attach structured metrics and artefact paths to their results.

Use [Coverage](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/coverage/) and [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpv/) for their payload-specific contracts. Use [Tests](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/tests/#interpret-results) for status and exit-code semantics.
