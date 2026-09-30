## Verible lint findings are on stderr

`verible-verilog-lint` writes findings to stderr and signals findings through its exit code. A pipeline that reads only stdout sees nothing. Capture stderr, or use `rb lint`, which scans both streams.
