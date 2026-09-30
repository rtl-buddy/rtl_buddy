## Choose the mapped-run ABC script

A Liberty-mapped run, which is every `tool: openroad` run and a `tool: yosys` run with a `platform` or `lib-paths`, maps logic to cells with one `abc -liberty` command. `abc-script` sets the ABC commands it runs. The default is Yosys' default Liberty script without `dc2`:

```text
strash; &get -n; &fraig -x; &put; scorr; dretime; strash; &get -n; &dch -f; &nf {D}; &put
```

`dc2` rebuilds the log-depth carry networks that `techmap` produces for adders, negates and incrementers as ripple chains, so it is left out. Set another script in an effort, or for one run in `tool_overrides.yosys.abc_script`:

```yaml
cfg-synth-efforts:
  - name: large
    yosys:
      synth-args: -noabc
      abc-script: "strash; dretime; map {D}"
```

- rtl_buddy keeps `-liberty` and `-dont_use`. On `tool: yosys` with an SDC clock it also passes `-D <period_ps>` and appends `stime -p`, whose report gives the run's WNS. Write `{D}` where a mapping command should take the delay target; `tool: openroad` passes none, so `{D}` is empty there.
- Write the script on one line, with commands separated by `;` and no double quotes. A multi-line value or a double quote is a configuration error. Yosys replaces commas with spaces.
- `strash; dretime; map {D}` is the script of `abc -fast`. It maps a large flat design much faster than the default, at some cost in quality.
- `abc-args` applies only to unmapped `tool: yosys` runs, as `abc <abc-args>`. A mapped run ignores it and warns `synth.abc_args_ignored`.
- `synth` runs Yosys' generic `abc`, whose script includes `dc2`, before the mapped-run ABC step. Add `-noabc` to `synth-args` to keep log-depth carry networks.
