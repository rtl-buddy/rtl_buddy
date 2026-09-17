## List the runs a project holds

A project accumulates one artefact directory per partition, per corner and per power mode. `rb phys runs` lists them, newest first:

```bash
rb phys runs
rb phys runs --limit 0
```

Each row carries the run name and top, the backends that produced it, the power mode and what drove the switching, a fingerprint of the configuration that shaped the netlist, the `rb xplr` experiment id when the run sits under one, and when it was generated. The row marked `*` is the newest — the run the other verbs read without `--phys-dir`. The artefact directories are printed under the table rather than in it, one per run, because that is the value the next command takes:

```bash
rb phys summary --phys-dir verif/blk/artefacts/nightly
```

The listing reads one manifest per run and opens no model, so it stays cheap on a project with many. A manifest that cannot be read is listed with its error rather than dropped: discovery found the file, and a menu that omitted it would report fewer runs than the project has. A project with no physical artefacts lists nothing and exits 0, unlike the other three verbs — they were asked about a run, and this one is asking what runs there are.
