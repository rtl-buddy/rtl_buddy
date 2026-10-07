## Reproducing a release

Verible picks new names on every run, and IEEE-1735 encryption uses a fresh session key every time. So a release cannot be rebuilt byte for byte from its commit alone, but it can be rebuilt exactly from its commit and its archived map:

- the internal manifest (`maps/<version>.json`) records the commit, the tool versions and, for every shipped file, the SHA-256 of its source and of its plaintext before encryption;
- `rb release --reproduce <manifest>`, run at that commit, re-cuts the release with every name pinned to the archived map and fails unless every file's plaintext matches. It archives nothing;
- the tarball is written deterministically (sorted entries, the commit time as every timestamp, no owner, no gzip timestamp), so everything except the encrypted payloads is identical between cuts.
