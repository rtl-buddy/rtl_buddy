## Persistent build cache

`artefacts/.shared-builds/` lives in the workspace, so a CI job that wipes the workspace recompiles unchanged inputs. Set `shared-build-root` to a directory outside the workspace to keep builds across runs:

```yaml
cfg-rtl-reg:
  reg-cfg-path: regression.yaml
  shared-build-root: /shared/nfs/rb-build-cache
```

```bash
rb regression --share-build --shared-build-root /shared/nfs/rb-build-cache
```

- Precedence: `--shared-build-root`, then `RTL_BUDDY_SHARED_BUILD_ROOT`, then the config key. An empty flag or variable turns the cache off for that run.
- A relative root resolves against the project root (the directory holding `root_config.yaml`). The root is created on demand and applies only with `--share-build`.
- Builds land in `<root>/<suite-namespace>/obj_dir_<key>/`, where the namespace is the suite directory relative to the project root with `/` replaced by `__`.
- Every checkout on the host shares the cache. Checkouts with identical inputs reuse each other's build.
- Switching the cache on or off recompiles once.
- Nothing prunes the cache. Prune between runs, never during one (see [Known issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/)):

```bash
find /shared/nfs/rb-build-cache -mindepth 2 -maxdepth 2 -name 'obj_dir_*' -mtime +14 -exec rm -rf {} +
```

### What keys an entry

The key is built from input contents and project-relative paths, so the same inputs give the same key in any checkout. It covers filelists (expanded recursively), include and library directories, sources, and the values of defines and parameters.

- A generated input that is not byte-reproducible, such as a `preproc` hook stamping a timestamp into a header, changes the key every run and leaves one directory per run behind.
- A path outside the project root is keyed as written, so checkouts that name the same absolute path still share a key.
- A `-f`/`-F` filelist chain nested deeper than the depth bound is only partly read, and the unread entries enter the key as absolute paths. That suite's key becomes checkout-specific, and the console reports `compile.cache_key_depth_bound`.
- An input over 64 MB, such as a ROM image, is keyed by size and modification time. Checkouts with different mtimes stop sharing that suite's builds.
