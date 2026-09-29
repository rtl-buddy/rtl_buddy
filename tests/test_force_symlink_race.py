# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""``force_symlink`` is atomic when many dispatched array elements repoint one shared
link concurrently.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from rtl_buddy.tools.vlog_sim import force_symlink


def test_force_symlink_survives_concurrent_writers(tmp_path: Path):
    # Concurrent writers repointing one shared link, released together. Check-then-act
    # raised here.
    link = tmp_path / "test.log"
    n_workers, n_iters = 16, 60
    targets = [tmp_path / f"target-{i}.log" for i in range(n_workers)]
    for t in targets:
        t.write_text("x")

    start = threading.Barrier(n_workers)
    errors: list[BaseException] = []

    def hammer(target: Path):
        start.wait()
        try:
            for _ in range(n_iters):
                force_symlink(str(target), str(link))
        except BaseException as e:  # noqa: BLE001 - surface the race
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(t,)) for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"force_symlink raced: {errors[:3]}"
    # The link is intact, still a symlink, and points at one of the targets.
    assert os.path.islink(link)
    assert Path(os.readlink(link)) in targets
    # No temporary files leaked.
    assert not list(tmp_path.glob("*.tmp"))
