## Map renamed instances

A netlist prepared for a simulator often renames instances, for example `u_a.u0` to `u_a__u0`, so the trace's instance names differ from the database's and their pins read no activity. Give `rb power` the pairs in a file and point `activity.instance-map` at it:

```text
# trace name    database name
u_a__u0         u_a.u0
u_a__mem_3_     u_a.mem[3]
```

```yaml
    activity:
      saif: ../../verif/demo/artefacts/gate_smoke/dump.saif
      scope: tb_top/u_dut
      instance-map: sim_names.map
```

- One pair per line; blank lines and lines starting with `#` are skipped. A backslash escapes the next character, so `\u_a.u0`, `u_a\.u0` and `u_a.u0` are one name.
- Each pair renames every SAIF `INSTANCE` of that name, at any depth, where the keyword and the name share a line, as SAIF writers put them. `activity.scope` names the instances as the file has them after the rename.
- A gzip-compressed SAIF works too; the renamed copy is plain text.
- The map applies to `activity.saif` only. `rb power` writes the renamed copy to [`activity.mapped.saif`](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/power/#inspect-artefacts) and reads that; the recorded trace and its hash stay the SAIF's, and the map's hash joins the options digest.
- A malformed line, a trace name given two database names, or a database name `read_saif` cannot read fails setup. A map that renames nothing warns (`power.instance_map_applied`).
