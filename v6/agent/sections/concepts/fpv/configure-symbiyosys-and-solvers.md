## Configure SymbiYosys and solvers

Declare project-wide tool settings in `root_config.yaml`:

```yaml
cfg-fpv-tools:
  - name: sby
    tool: sby
    opts:
      timeout: 600
      extra-args: ""
      solver-versions:
        yices: "2.6.4"
        z3: "4.13.0"
```

`solver-versions` pins exact versions. rtl_buddy probes all pins before a run and fails once with every mismatch; resolved versions are logged. Supported pin names are `yices`, `z3`, `boolector`, `bitwuzla`, `btormc`, and `abc`.

<a id="choosing-a-frontend"></a>
