## The three anchors

Every command has three paths it cares about:

| Anchor | What it is |
| --- | --- |
| `invocation_cwd` | The directory you ran `rb` from — your shell's working directory. |
| `command_root` | The directory containing the command's primary config file. |
| `artifact_root` | Where the artifact tree lives. Defaults to `command_root/artefacts/`. |

And one rule that ties them together:

> **Config-driven commands anchor to their primary config. Explicit CLI input/output paths anchor to your shell's cwd.**

Generated outputs (`artefacts/<name>/`, `rtl_buddy.log`, builder scratch) go under the command root. Things you typed on the command line (`-o out.svg`, an output filelist path) follow normal shell semantics — they land where you told them to.
