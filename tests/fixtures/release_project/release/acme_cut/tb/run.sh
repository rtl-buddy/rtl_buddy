#!/usr/bin/env bash
# Compile and run the release testbench. Run from the release root.
set -euo pipefail
vcs -full64 -sverilog -timescale=1ns/1ps -f design/acme_top.f -f verif/tb.f -top tb_acme -o simv -l compile.log >/dev/null
./simv -l sim.log
