## An error after `PASS` fails the test only through the exit status

A test whose transcript prints `PASS` and then reports an error fails because the simulator exits nonzero. When the simulator exits 0 after the error, the test stays `PASS`:

- VCS exits 0 after `$error`, `$fatal` and failed assertions.
- Verilator exits 0 when `+verilator+error+limit+<n>` lets the simulation continue past the error.

Set `assertions: true` so Verilator and xezim assertion failures fail the test, or have the testbench print `FAIL` when its own checks fire.
