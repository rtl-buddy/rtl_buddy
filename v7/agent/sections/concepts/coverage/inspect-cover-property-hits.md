## Inspect cover-property hits

For Verilator, machine output lists each labeled user cover point as `{name, file, line, module, hits}` on the test result and in the run-level aggregate. The data comes from the per-test `coverage.dat`; no merge flag is needed. Hits are combined across tests by file, line, name and module, so the same included property compiled into different modules stays separate.

Other simulator families omit the field. Omitted means not collected, not zero coverage.
