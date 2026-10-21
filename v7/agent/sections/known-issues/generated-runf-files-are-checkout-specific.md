## Generated `run.f` files are checkout-specific

rtl_buddy writes sources, `+incdir+` and `-y` directories as absolute paths. Do not commit or copy `run.f` between checkouts, and spell a checkout's path the same way every time, because path spelling affects compile keys.

- An include directory whose path contains `+` stays relative, because filelist parsers read `+incdir+a+b` as two directories. rtl_buddy warns `filelist.incdir_unrepresentable`. Remove the `+` from the path.
- A path containing whitespace is quoted, which Icarus's `-f` parser does not understand. Keep whitespace out of checkout paths.
