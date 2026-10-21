## Add curated mutants

`mutants` lists hand-written faults the operators cannot express, such as a flipped product bit or an off-by-one index. Each entry is a unified diff, scored like a generated mutant: in its own copy of the model tree, against the same baseline and kill oracles, with one entry in the report.

```yaml
operators: []
mutants:
  - name: alu_prod_bit2
    patch: mutants/alu_prod_bit2.patch
  - name: alu_lane_swap
    patch: mutants/alu_lane_swap.patch
    strip: 1
```

- `patch` is relative to `mut.yaml`; a missing file is fatal at load.
- Paths inside the diff are relative to the model directory, the one containing `models.yaml`. `strip` (default 1) removes leading path components, as `git apply -p<strip>`; the default matches the `a/` and `b/` prefixes of `git diff`. Make a patch from the model directory with `git diff --relative`.
- The diff is applied with `git apply`. A diff that no longer applies, for example after an RTL edit moved its context, makes the mutant `ERRORED`, with git's message in the mutation column and a `mut_runner.user_mutant_not_applied` warning. A diff that would write outside the model tree also errors.
- `name` must be unique and contain only letters, digits, underscore, dot or hyphen. The mutant id is `user_<name>` and its operator is `user`.
- User mutants run after the baseline and before generated mutants. The budget and `per_file` counts cover generated mutants only; `scope` needs a non-empty `operators`.
