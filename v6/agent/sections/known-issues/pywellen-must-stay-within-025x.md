## pywellen must stay within 0.25.x

`rb wave` annotations and `rb saif` depend on pywellen's API, which changes on each pre-1.0 minor release. The supported range is `>=0.25.6,<0.26`. Outside it, the command fails with `pywellen.api_missing`, naming the installed version and the range. Install a version in the range.
