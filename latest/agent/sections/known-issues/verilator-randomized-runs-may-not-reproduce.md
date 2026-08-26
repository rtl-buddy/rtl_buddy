## Verilator randomized runs may not reproduce

Verilator can produce different behavior for the same random seed. Use VCS with `-xlrm hier_inst_seed` when reproducibility is required, and give instances stable explicit names.

With hierarchical seeding, VCS writes `HierInstanceSeed.txt` in the simulation directory. If it is missing, rtl_buddy logs `sim.hier_seed_missing` and cannot record the seed, but does not change the test verdict.
