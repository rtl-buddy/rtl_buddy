## Choose the mapped-run ABC script

A Liberty-mapped run, which is every `tool: openroad` run and a `tool: yosys` run with a `platform` or `lib-paths`, maps logic to cells with one `abc -liberty` command. `abc-script` sets the ABC commands it runs, or names a preset: `default` or `delay`. The `default` preset is Yosys' default Liberty script without `dc2` and `&fraig -x`:

```text
strash; scorr; dretime; strash; &get -n; &dch -f; &nf {D}; &put
```

- `dc2` rebuilds the log-depth carry networks that `techmap` produces for adders, negates and incrementers as ripple chains, so it is left out.
- `&fraig -x` is a SAT sweep that allows up to a million solver conflicts per node. On deep arithmetic, such as a chain of wide multipliers, it ran for hours without finishing, so it is left out too. `scorr` still merges equivalent registers.

Set another script in an effort, or for one run in `tool_overrides.yosys.abc_script`:

```yaml
cfg-synth-efforts:
  - name: large
    yosys:
      synth-args: -noabc
      abc-script: "strash; dretime; map {D}"
```

- rtl_buddy keeps `-liberty` and `-dont_use`. On `tool: yosys` with an SDC clock it also passes `-D <period_ps>`, the shortest SDC period in picoseconds, and appends `stime -p`, whose report gives the run's WNS. Write `{D}` where a mapping command should take the delay target; `tool: openroad` passes none, so `{D}` is empty there.
- Write the script on one line, with commands separated by `;` and no double quotes. A multi-line value or a double quote is a configuration error. Yosys replaces commas with spaces.
- `strash; dretime; map {D}` is the script of `abc -fast`. It maps a large flat design much faster than the default, at some cost in quality.
- `abc-args` applies only to unmapped `tool: yosys` runs, as `abc <abc-args>`. A mapped run ignores it and warns `synth.abc_args_ignored`.
- `synth` runs Yosys' generic `abc`, whose script includes `dc2`, before the mapped-run ABC step. Add `-noabc` to `synth-args` to keep log-depth carry networks.

### Keep prefix adders log-depth with the `delay` preset

`synth-args: -noabc -extra-map +/choices/kogge-stone.v` asks Yosys for a Kogge-Stone carry network; `sklansky.v` and `han-carlson.v` are the other `+/choices/` maps. The `default` preset can undo it:

- ABC maps the whole flattened module as one network, with one required time: the deepest arrival anywhere in the module. A `-D` target does not change this.
- `&dch -f` adds structural choices, and `&nf` area recovery uses them to rebuild every adder with slack in that view as a low-area ripple chain. An adder off the module's critical path loses its log depth because of unrelated logic beside it.

The `delay` preset is the `default` preset without `&dch -f`:

```text
strash; scorr; dretime; strash; &get -n; &nf {D}; &put
```

When `abc-script` is unset and the `synth-args` of the run pass `-extra-map +/choices/<map>`, the run uses `delay` instead of `default` and logs `synth.abc_delay_preset` at INFO. Set `abc-script: default`, or a script, to keep choices.

`delay` keeps such adders log-depth for more area, and it can lengthen the module's critical path, which no longer gets the choices. On sky130hd tt, a registered 32-bit Kogge-Stone adder beside two serial 32-bit multiply-adds:

| Preset | adder path | module critical path | area |
| --- | ---: | ---: | ---: |
| `default` | 3.99 ns | 9.91 ns | 42952 |
| `delay` | 1.89 ns | 10.79 ns | 46590 |

Compare timing under both presets before keeping one. `delay` suits datapaths with many adders on separate register-to-register paths.
