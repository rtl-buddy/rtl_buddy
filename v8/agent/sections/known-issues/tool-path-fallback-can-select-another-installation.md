## Tool-path fallback can select another installation

A configured tool directory wins only when it contains the requested executable. Otherwise rtl_buddy may use a matching executable on `PATH` and warns once per process. After changing `root_config.yaml`, `.rtl-buddy/.env` or the environment, restart long-running `rb hub` and `rb mcp` processes.
