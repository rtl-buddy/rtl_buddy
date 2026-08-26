## Dispatch changes build behavior

`--dispatch` implies `--share-build` and rejects `--early-stop`. `cfg-dispatch.backend` defaults `regression` and `randtest`, but `rb test` remains local unless `--dispatch` is explicit. A one-seed `randtest` replay also stays local.

Shareable builders compile once per compile key. Builders that cannot share compile in their jobs; fanned-out tests still use a build job to serialize access to their compile directory. See [Parallel Dispatch](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/dispatch/).
