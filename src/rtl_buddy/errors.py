class RtlBuddyError(Exception):
  """Base exception for rtl_buddy."""


class FatalRtlBuddyError(RtlBuddyError):
  """Fatal configuration or environment error."""


class FilelistError(RtlBuddyError):
  """Per-test filelist validation failure (bad path, malformed line, missing file, etc.)."""


class SetupScriptError(RtlBuddyError):
  """Test-scoped setup failure such as sweep or preproc script errors."""
