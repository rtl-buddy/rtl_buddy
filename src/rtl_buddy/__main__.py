# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import logging
from .rtl_buddy import RtlBuddy

DESCRIPTION = """
RTL Buddy is a RTL development build system.
"""

def main() -> int:
  rb = RtlBuddy(name='rtl_buddy_inst')
  return rb.run()

if __name__ == '__main__':
  raise SystemExit(main())
