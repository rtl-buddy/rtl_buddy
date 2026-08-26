## Coverage uses the platform builder

Coverage collection and labels use the platform-selected builder, even when a suite or test selects another `builder:`. A mismatch can mislabel or misparse coverage. Use `--builder <name>` for the run or make that builder the platform default. See [YAML Formats](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/).
