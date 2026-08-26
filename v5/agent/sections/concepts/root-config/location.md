## Location

`rtl_buddy` discovers `root_config.yaml` by walking **up** from the command root (the directory containing the command's primary config — see [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/)), not from the directory you ran `rb` from. Paths declared inside `root_config.yaml` are resolved relative to the `root_config.yaml` file itself. (Standalone commands that have no primary config — e.g. `rb tool-check` — fall back to walking up from the current directory.)
