## Tool-path fallback can select another installation

Configured tool directories take precedence only when they contain the requested executable. Otherwise rtl_buddy may use a matching executable on `PATH` and logs a fallback warning.

Warnings for fallback paths and unresolved variables are emitted once per process. Restart long-running `rb hub` or `rb mcp` processes after changing `root_config.yaml`, `.rtl-buddy/.env`, or the environment if you need the warning to be evaluated again.
