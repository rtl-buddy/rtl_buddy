## Results table

```
                              Power Results Summary
┏━━━━━━━━┳━━━━━━┳━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━┓
┃ Power  ┃Res…  ┃Desc  ┃Mode  ┃Source ┃Activity┃Total ┃Intern…┃Switch.┃Leakag…┃
┡━━━━━━━━╇━━━━━━╇━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━┩
│ …static│PASS  │…     │static│synth  │default │422 µW│ 344 µW│22.6 µW│54.9 µW│
│ …synth │PASS  │…     │dynam.│synth  │synth.  │515 µW│ 363 µW│93.1 µW│59.3 µW│
│ …saif  │PASS  │…     │dynam.│synth  │saif    │906 µW│ 646 µW│ 203 µW│57.1 µW│
│ …postp │PASS  │…     │dynam.│ pnr   │saif    │1.110 │ 708 µW│ 341 µW│59.8 µW│
│        │      │      │      │       │        │  mW  │       │       │       │
└────────┴──────┴──────┴──────┴───────┴────────┴──────┴───────┴───────┴───────┘
```

Columns:

- **Mode** — `static` or `dynamic` from `power.yaml`.
- **Source** — `synth` (post-synth netlist) or `pnr` (post-PnR ODB). Set by `netlist-source:`.
- **Activity** — resolved activity strategy: `default` (static), `synthetic`, `saif`, or `vcd`.

Power values auto-scale between W / mW / µW / nW for readability. Columns only render when at least one run populates them.
