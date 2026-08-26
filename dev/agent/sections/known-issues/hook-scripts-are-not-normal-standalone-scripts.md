## Hook scripts are not normal standalone scripts

`sweep` and `preproc` execute with the invocation directory as CWD and with `__name__ == "__rtl_buddy_hook__"`. Use injected `suite_dir`, `artifact_dir`, and `run_artifact_dir` paths; do not put required hook logic behind `if __name__ == "__main__":`.

rtl_buddy captures Python-level `print()` output as `hook.stdout` events. The capture has no `.buffer` or file descriptor, and child-process output bypasses it. Capture child output explicitly and print the text you want logged. For a generator that can write only relative to CWD, temporarily change to `suite_dir` and restore the previous directory. See [Plugins](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/plugins/).
