## A worked example

Suppose your repo looks like this:

```text
repo/
├── design/<block>/        # RTL sources
└── verif/<block>/
    └── tests.yaml
```

You're sitting in `repo/design/<block>` (looking at the RTL) and want to run a quick test. You point `rb` at the suite with `-c`:

```bash
cd repo/design/<block>
rb test basic -c ../../verif/<block>/tests.yaml
```

Here is what each anchor resolves to:

- `invocation_cwd` = `repo/design/<block>`
- `command_root` = `repo/verif/<block>` (`dirname(tests.yaml)`)
- `artifact_root` = `repo/verif/<block>/artefacts`

So the test creates `repo/verif/<block>/artefacts/basic/...` and `repo/verif/<block>/rtl_buddy.log`. Nothing lands in `design/<block>`.

If you'd passed an explicit output:

```bash
rb filelist <model> out.f -c ../../verif/<block>/models.yaml
```

The filelist lands at `repo/design/<block>/out.f` (your shell's cwd) because `out.f` is a user-supplied output path. The orchestration log still lands at `dirname(models.yaml)/rtl_buddy.log`.
