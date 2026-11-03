"""Deadline-bounded JSON Lines reads for socket and subprocess tests."""

import json
import os
import select
import time


class JsonLineReader:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = b""

    def read(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.stream], [], [], remaining)[0]:
                raise TimeoutError("Timed out waiting for a complete JSON line")
            chunk = os.read(self.stream.fileno(), 65536)
            assert chunk, "Peer closed the stream before a complete JSON line"
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return json.loads(line)

    def read_event(self, event_type: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.read(timeout=deadline - time.monotonic())
            if event["type"] == event_type:
                return event
        raise TimeoutError(f"Timed out waiting for {event_type}")
