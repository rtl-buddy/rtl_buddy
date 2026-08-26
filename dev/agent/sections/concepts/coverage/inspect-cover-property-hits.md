## Inspect cover-property hits

For Verilator, machine output includes each labeled user cover point as `{name, file, line, module, hits}` on the test result and in the run-level aggregate. This data comes from per-test `coverage.dat` and does not require a merge flag.

Verilator folds repeated instances of one point within a module. rtl_buddy then combines tests by `(file, line, name, module)`. The module remains part of the identity so the same included property compiled into different modules is not mistaken for one covered point.

Other simulator families omit the field. Omitted means not collected, not zero coverage.
