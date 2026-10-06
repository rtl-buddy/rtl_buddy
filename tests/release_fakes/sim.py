#!/usr/bin/env python3
"""Stand-in 'simulator' for release verify tests: checks every filelist entry exists.

Prints a SIG line derived from the shipped file names' count, so every stage
agrees, and TEST PASSED when nothing is missing. RB_FAKE_SIM_SIG overrides the
SIG for the stage named by RELEASE_STAGE, to test the compare gate.
"""

import os
import sys

missing = []
count = 0
for fl in sys.argv[1:]:
    for line in open(fl):
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("+"):
            continue
        path = line.split()[-1]
        if path.startswith("$"):
            continue
        count += 1
        if not os.path.exists(path):
            missing.append(path)
sig = os.environ.get(
    "RB_FAKE_SIM_SIG_" + os.environ.get("RELEASE_STAGE", ""), str(count)
)
print(f"SIG: files={sig}")
print("TEST FAILED: missing " + " ".join(missing) if missing else "TEST PASSED")
