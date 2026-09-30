## Deeply nested expressions can exhaust the elaboration stack on macOS

`rb elab` analyses on threads with the platform default stack: 512 KiB on macOS, 8 MiB on Linux. About 400 nested conditional expressions on macOS kill the worker with no diagnostic, and the run reports `elaboration worker did not produce a result`. `max_parse_depth` does not help. Elaborate such generated RTL on Linux, or reduce the nesting. See [Model Elaboration](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/elaboration/).
