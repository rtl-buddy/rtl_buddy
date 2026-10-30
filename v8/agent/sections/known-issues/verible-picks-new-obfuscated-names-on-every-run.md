## Verible picks new obfuscated names on every run

`verible-verilog-obfuscate` has no seed: two runs on the same input give different names. `rb release` keeps names stable by starting each release from the previous release's map, and reproduces a past release only from that release's own map with `--reproduce`. Keep every `maps/<version>.map`.
