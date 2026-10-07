## Install the tools

`rb release` needs `verible-verilog-obfuscate` (part of the Verible release) and Synopsys VCS for IEEE-1735 encryption, plus whatever the verification command runs. Check with `rb tool-check --required-for release`. Constraint rewriting needs the `tcl` constraint reader, that is, a Python with `tkinter`; `rb tool-check` reports which reader is active (see [Tool Dependency Check](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/tool-check/)).
