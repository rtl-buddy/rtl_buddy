## Collect coverage

RTL Buddy coverage supports Verilator and xezim and follows the platform-selected builder. Pass `--builder <name>` or make that builder the platform default; a suite- or test-level override alone is not enough. See [Coverage uses the platform builder](https://rtl-buddy.github.io/rtl_buddy/v8/known-issues/#coverage-uses-the-platform-builder). xezim coverage is statement-level and has no expression metric; see [Collect coverage with xezim](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/coverage/#collect-coverage-with-xezim).
