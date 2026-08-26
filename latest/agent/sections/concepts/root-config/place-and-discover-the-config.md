## Place and discover the config

Keep `root_config.yaml` at the project root. RTL Buddy walks upward from the command root—the directory containing the primary command config—and uses the first root config it finds. Commands without a primary config walk upward from the shell's current directory.

Paths inside `root_config.yaml` resolve from its directory. See [Execution Context](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/) for command-root behavior.
