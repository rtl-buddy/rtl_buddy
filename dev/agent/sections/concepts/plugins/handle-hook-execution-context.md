## Handle hook execution context

Hooks run through `exec()` in the invocation working directory, not the suite directory. `__name__` is `"__rtl_buddy_hook__"`, so place hook logic at module scope; an `if __name__ == "__main__":` branch is skipped.

Hook `print()` output is captured as `hook.stdout`, appears on stderr and in `rtl_buddy.log`, and cannot corrupt `--machine` JSON on stdout. Prefer `logger` when a message needs a level.

Child-process output is not captured automatically. Capture it and print it through the hook:

```python
res = subprocess.run(cmd, capture_output=True, text=True, check=True)
print(res.stdout, end="")
```

The captured `sys.stdout` has no usable `fileno()` or `.buffer`. If a third-party generator can only write relative to its working directory, change to `suite_dir` temporarily and restore the prior directory in `finally`.

See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/) for path ownership rules.
