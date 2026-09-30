## Raise the parser nesting limit

If a run fails with `language constructs are too deeply nested`, set `max_parse_depth` on the profile. slang stops at 1024 nesting levels, and generated RTL can exceed that inside one expression, typically a long chain of conditional expressions or concatenations. The setting is unset by default and affects only the profile that declares it:

```yaml
    elaborations:
      - name: parse
        max_parse_depth: 8192
```

Malformed sources still fail with the same diagnostics. Set it to what the generated source needs, not to the maximum: beyond the guard, later passes can exhaust the C stack and kill the worker with no diagnostic. The accepted range is in [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#elaboration-profiles), and the platform limit on nesting depth is in [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/).
