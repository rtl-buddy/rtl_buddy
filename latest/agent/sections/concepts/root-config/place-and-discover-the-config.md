## Place and discover the config

Keep `root_config.yaml` at the project root. RTL Buddy walks upward from the command root, the directory containing the primary command config, and uses the first root config it finds. A command with no primary config walks upward from the shell's current directory. The search checks the starting directory and at most eight parent directories. A config outside the project tree does not fall back to the working directory's project; the error names the directory the search started from and the last one it checked.

Paths inside `root_config.yaml` resolve from its directory. See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/execution-context/) for command-root behavior.
