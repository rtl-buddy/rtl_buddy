## Verible lint findings are on stderr

`verible-verilog-lint` writes findings to stderr and uses its exit code for clean versus findings. A pipeline that reads only stdout sees nothing; capture stderr or use `rb lint`, which scans both streams.
