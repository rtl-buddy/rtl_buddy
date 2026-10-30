## xezim coverage figures differ from xezim's own report

rtl_buddy scores a xezim toggle bit as hit only when it both rose and fell, while xezim counts each direction as a point, so `T:` reads lower than xezim's toggle percentage. Line points are statements, so a line holding two statements counts twice. LCOV and HTML hold line and branch only. See [Collect coverage with xezim](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/coverage/#collect-coverage-with-xezim).
