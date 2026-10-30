## Resolve command paths

RTL Buddy uses three anchors:

| Anchor | Meaning |
| --- | --- |
| `invocation_cwd` | The shell directory where `rb` was invoked |
| `command_root` | The directory containing the command's primary config |
| `artifact_root` | `<command_root>/artefacts/`, or `<command_root>/artefacts/.runs/<tag>/` under `--run-tag` |

Artefacts, builder scratch and `rtl_buddy.log` use the command root. Explicit CLI input and output paths use normal shell semantics and resolve from `invocation_cwd`.

```bash
cd repo/design/block
rb test basic -c ../../verif/block/tests.yaml
```

This runs under `repo/verif/block/artefacts/basic/` and writes `repo/verif/block/rtl_buddy.log`. An explicit output such as `rb filelist model out.f ...` still writes `out.f` in `repo/design/block`.
