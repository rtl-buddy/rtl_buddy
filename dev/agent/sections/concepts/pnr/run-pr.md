## Run P&R

```bash
rb pnr --list -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml -l 1000
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --png
rb pnr demo_pnr_nangate45 -c pnr/demo/pnr.yaml --gds-mode strict
```

`--png` and `--gds-mode` imply `--gds`. RTL Buddy invokes KLayout after a successful OpenROAD run. In the default `preview` mode a KLayout failure produces a warning and does not change the P&R verdict; use the OpenROAD timing and DRC results as the run outcome. In `strict` mode an export that could not be delivered fails the run — see [Stream-out completeness](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/pnr/#stream-out-completeness).

### OpenROAD threads

OpenROAD runs on one thread unless the run asks for more, and reserving CPUs from a scheduler does not change that on its own. Set `threads:` on the run:

```yaml
runs:
  - name: demo_pnr_nangate45
    # ...
    threads: 8        # or: auto
```

| Value | Threads OpenROAD is given |
| --- | --- |
| unset | 1, OpenROAD's default. `pnr.tcl` is unchanged and carries no `set_thread_count` |
| a positive integer | That many, emitted as `set_thread_count N` before the first `read_liberty` |
| `auto` | The CPUs of the allocation the run is in; 1 when there is none. Never the host's core count |

Zero, negative numbers, booleans, quoted numbers and any other string fail configuration loading. `0` is refused rather than passed through because OpenROAD reads it as "every core on the host".

**Allocations.** `rb pnr` itself is not dispatched; run it inside an allocation (`srun -c 8 rb pnr ...`, or from an `sbatch` script) to give OpenROAD CPUs. RTL Buddy treats as the allocation, taking the smaller when both apply:

- inside a Slurm job (`SLURM_JOB_ID` set), `SLURM_CPUS_PER_TASK`, else `SLURM_CPUS_ON_NODE`;
- a CPU affinity mask smaller than the machine (`taskset`, a cpuset cgroup, a bound Slurm step; Linux only).

A count above the allocation is clamped to it, and the run logs `openroad.threads_capped` at WARNING naming both numbers; it never runs more threads than were reserved. The clamp is a warning rather than an error so one checked-in `pnr.yaml` works in allocations of any size. Outside an allocation an explicit count is used as given. OpenROAD then applies its own limit, the host's hardware thread count. A container CPU quota (`docker --cpus`) is not visible to either check. Simulator compile and run concurrency are unaffected.

**Provenance.** The resolved plan is logged as `openroad.threads`. The run's result carries `openroad_threads`: the `requested` value, the `effective` count, and the `allocation` and its `allocation_source`. `effective` is the count OpenROAD itself reported (`[INFO ORD-0030] Using N thread(s).` in `pnr.log`) when it reported one, otherwise the count RTL Buddy set, which is 1 when the key is unset. The same key and contract apply to [`rb power`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/power/) and to the OpenROAD stage of [`rb synth`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/synthesis/). The thread count is not part of any result fingerprint. OpenROAD documents its multi-threaded stages as producing the same result, but no bit-for-bit match across thread counts is claimed here; compare DRC and slack yourself if it matters.

### Stream-out inputs

Stream-out reads more than the routed DEF. The layout comes from the PDK's `cell-gds` — one path or a list of them — followed by the run's own `gds-paths`, which is where the layout of a hard macro belongs: an OpenRAM SRAM has its LEF in `lef-paths` and its GDS in `gds-paths`, and each path resolves against the file that names it, `root_config.yaml` for the PDK and `pnr.yaml` for the run. The DEF reader is also given the LEFs, so it can resolve the masters the DEF instantiates: technology LEF, the PDK's macro LEF, then the run's `lef-paths`, in that order and de-duplicated, appended to whatever the KLayout technology file already lists rather than replacing it. Both lists reach KLayout through `def2stream.inputs.json` in the artefact directory, which is also where to read back what a given run streamed.

An input the config names and the disk does not have stops the export before KLayout is launched, with every missing path reported at once — a stream-out run without it succeeds and writes a GDS with the unresolvable cells left empty, which is a layout that looks produced.

### Stream-out completeness

A cell the DEF instantiates whose layout is in none of the GDS files is streamed as an empty placeholder. Sometimes that is deliberate — an ORFS `fakeram45` macro exists only in LEF, and a preview of the floorplan around it is exactly what was wanted — and sometimes it is a design that forgot its real SRAM GDS and got a plausible picture with a hole in it. `gds-mode` says which the run means:

```yaml
runs:
  - name: demo_pnr_signoff
    # ...
    gds-mode: strict          # default: preview
    gds-allow-empty:          # cells that are empty on purpose
      - fakeram45_*
```

- `preview` (the default) keeps the layout and reports what is missing from it: `pnr.gds_incomplete` at WARNING naming every cell, `gds_status: incomplete` with `gds_missing_cells` and `gds_missing_cell_count` in `--machine` output, `GDS incomplete: …` in the run's description, and `gds+png (incomplete: N missing)` in the summary's Outputs column. The run still passes; the preview is never reported as a complete stream-out.
- `strict` refuses to publish it. Cells with no layout, a missing KLayout executable, a PDK with no `klayout-tech`, a configured input that is not on disk, a stream-out that failed for any other reason, and a `--png` render that failed each make the export a `FAIL` whose `fail_stage` is `export`. The GDS, the PNG and the stream-out report are removed, so nothing is left to be read as a successful artefact. The routed DEF, netlist, SDC and ODB stay: OpenROAD finished cleanly and those outputs are its, not the export's. Because the failure is not the flow's verdict on the design, an `xfail:` marker does not excuse it.

`gds-allow-empty` takes cell names or `fnmatch` globs, matched case-sensitively, and is the per-run form of the legacy `GDS_ALLOW_EMPTY` environment regex, which is still honoured. A cell it covers is not missing in either mode; it is reported as intentionally empty, in `gds_allowed_empty_cells` and as `(N empty by design)` in the Outputs column.

Completeness is decided from `def2stream.report.json`, which the bundled KLayout helper writes into the artefact directory after the layout — not from KLayout's console output. A run whose report is absent, unreadable or from another schema has a failed export, whatever else is on disk: nothing vouched for that layout.
