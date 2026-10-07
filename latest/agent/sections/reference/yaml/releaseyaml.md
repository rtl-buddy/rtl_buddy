## release.yaml

Required keys are `rtl-buddy-filetype: release_config`, `name`, `version`, `design` and `encryption`. Unknown keys are errors, so a misspelt protection setting cannot ship a file unprotected. Paths resolve relative to `release.yaml`. See [Customer releases](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/release/).

```yaml
rtl-buddy-filetype: release_config
name: acme
version: 1.0.0
metadata: {cut: acme_cut}

design:
  model: acme_top
  model-config: ../../rtl/models.yaml
  top: acme_top
  protect: {obfuscate: true, encrypt: true, strip-comments: true}
  preserve:
    interfaces: [acme_core_wrap]
    identifiers: [ACME_SIM]
  files:
    - {match: "acme_pub_pkg.sv", encrypt: false, reason: "constants the customer programs"}
  externals:
    - {path: "$VENDOR_LIB", ship-as: "$VENDOR_LIB"}
  constraints:
    - {src: ../../rtl/acme_top.sdc, scope: acme_top}

testbench:
  filelist: [ "tb/tb_acme.sv" ]
  extra-files: [ "tb/run.sh" ]
  allow-design-refs: [acme_pub_pkg]

verify:
  command: "bash verif/run.sh"
  pass: "^TEST PASSED"
  compare: "^SIG: .*"

encryption:
  key-file: keys/vendor_keys.txt

package:
  notes: "notes/{version}.md"
  docs: [ docs/user_guide.md ]
```

| Field | Requirement | Meaning |
|---|---|---|
| `name` | Required | Release name; the tarball is `<name>-<version>.tar.gz` |
| `version` | Required | Release version; names the archived map `maps/<version>.map`. Versions order numerically by part |
| `metadata` | Optional map | Copied into the internal manifest |
| `design.model` / `design.model-config` | Required | The model whose filelist is released, and its `models.yaml` |
| `design.top` | Default model top | Top module; its interface is always preserved |
| `design.protect` | Default all true | `obfuscate`, `encrypt` and `strip-comments` defaults for design files |
| `design.preserve.interfaces` | Optional list | Modules whose name, ports and parameters are kept: hardening boundaries and constraint scopes |
| `design.preserve.identifiers` | Optional list | Further names kept everywhere |
| `design.files` | Optional list | Per-file overrides: `match` (base-name glob), `reason` (required), and any of `obfuscate`, `encrypt`, `strip-comments`. Later rules win; a rule matching nothing is an error |
| `design.externals` | Optional list | `path` (environment variables expanded) whose files are not shipped, and `ship-as`, the prefix written in their place |
| `design.constraints` | Optional list | `src` SDC file, `scope` (a preserved module), optional `ship-as` file name, and `mode`: `rewrite` (default; evaluate and translate) or `verbatim` (ship unchanged after a port check) |
| `testbench.filelist` | Optional list | Filelist lines, as in `models.yaml`, relative to `release.yaml` |
| `testbench.extra-files` | Optional list | Files copied into `verif/` unchanged, such as run scripts |
| `testbench.protect` / `testbench.files` | Default not protected | As for `design`; testbench obfuscation is not supported |
| `testbench.allow-design-refs` | Optional list | Design units the testbench may name, which therefore ship unobfuscated |
| `verify.command` | Required in `verify` | Shell command run from the root of each stage |
| `verify.pass` | Required in `verify` | Regex the output must match, with exit status 0 |
| `verify.compare` | Optional | Regex whose matching lines must be identical in every stage |
| `verify.stages` | Default `[src, obf, pkg]` | Stages to run |
| `verify.timeout` | Default 3600 | Seconds per stage |
| `encryption.key-file` | Required | IEEE-1735 key file for `vcs -ipprotect` |
| `encryption.vcs` | Default `vcs` | VCS executable |
| `encryption.extra-args` | Optional list | Extra `vcs` arguments |
| `encryption.jobs` | Default 4 | Files encrypted in parallel |
| `obfuscation.verible` | Default `cfg-verible`, then `PATH` | Path to `verible-verilog-obfuscate` |
| `obfuscation.map-dir` | Default `maps` | Where maps and manifests are archived |
| `obfuscation.continue-from` | Default `previous` | `previous`, `none`, or a map file to start from |
| `obfuscation.keep-comments` | Default directive patterns | Regexes; a matching comment survives stripping |
| `obfuscation.token-paste` | Default `refuse` | `refuse` fails on token pasting that forms names; `preserve` keeps every name it can form |
| `package.notes` | Default `notes/{version}.md` | Release notes, required for every release; shipped as `RELEASE_NOTES.md` |
| `package.docs` | Optional list | Documents shipped in `docs/` |
