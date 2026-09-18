## Raise the parser nesting limit

Set a profile's `max_parse_depth` when a run fails with `language constructs are
too deeply nested`. slang stops recursive descent after 1024 nesting levels, and
generated RTL can exceed that inside a single expression — a long chain of
conditional expressions or concatenations is the usual cause. It is unset by
default and moves the limit only for the profile that declares it:

```yaml
    elaborations:
      - name: parse
        max_parse_depth: 8192
```

It lifts a depth guard, not a correctness one: malformed sources still fail with
the same diagnostics. Raise it to what the generated source needs rather than to
the accepted maximum — the guard turns runaway recursion into a diagnostic, and
past it the passes after the parser can exhaust the C stack and kill the worker
with no diagnostic at all. See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/v6/reference/yaml/#elaboration-profiles)
for the accepted range and [Quirks & Known Issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/) for the
platform limit on nesting depth.
