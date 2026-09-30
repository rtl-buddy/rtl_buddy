## Hook scripts are not normal standalone scripts

`sweep` and `preproc` hooks run with the invocation directory as CWD and `__name__ == "__rtl_buddy_hook__"`. Use the injected `suite_dir`, `artifact_dir` and `run_artifact_dir` paths, and do not put required logic behind `if __name__ == "__main__":`.

Python `print()` output is captured as `hook.stdout` events, but child-process output bypasses the capture, so capture it and print it. See [Plugins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/).
