## Handle hook execution context

Hooks run through `exec()` in the invocation working directory, not the suite directory. `__name__` is `"__rtl_buddy_hook__"`, so an `if __name__ == "__main__":` block never runs; put hook logic at module scope.

`print()` output is captured as `hook.stdout`, shown on stderr, and written to `rtl_buddy.log`. It cannot corrupt `--machine` JSON on stdout. Use `logger` when a message needs a level.

Child-process output is not captured automatically. Capture it and print it from the hook:

```python
res = subprocess.run(cmd, capture_output=True, text=True, check=True)
print(res.stdout, end="")
```

The captured `sys.stdout` has no usable `fileno()` or `.buffer`. If a third-party generator can only write relative to its working directory, `os.chdir(suite_dir)` for the call and restore the previous directory in `finally`.

See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/) for path ownership.
