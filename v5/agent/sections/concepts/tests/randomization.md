## Randomization

Two seed options are available with the `test` subcommand:

- `--rnd-new`: use a randomly generated seed instead of the root config seed. The seed is saved to `artefacts/{test_name}/test.randseed`.
- `--rnd-last`: repeat the test with the seed from the last `--rnd-new` run.

For running a test many times with different seeds, use `randtest`. See the [CLI reference](https://rtl-buddy.github.io/rtl_buddy/v5/reference/cli/#randtest).
