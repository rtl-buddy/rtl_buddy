# rtl-buddy
#
# Copyright 2024 rtl_buddy contributors
#
"""force_symlink concurrency (#363).

Under ``--dispatch`` every element of a suite's Slurm array runs at once
and repoints the *same* suite-level ``test.log``/``test.err``/``test.randseed``
links. The old check-then-act form (``lexists`` then ``remove`` then
``symlink``) raced and killed passing elements with ``FileNotFoundError`` /
``FileExistsError``. ``force_symlink`` must be atomic instead.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from rtl_buddy.tools.vlog_sim import force_symlink


def test_force_symlink_survives_concurrent_writers(tmp_path: Path):
    # Many concurrent writers repointing one shared link at their own target,
    # released together to maximize interleaving. The check-then-act form
    # raised here; the atomic create-then-replace must not.
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
    # The link is intact, is still a symlink, and points at one of the
    # targets (whichever wrote last) — never left dangling or half-created.
    assert os.path.islink(link)
    assert Path(os.readlink(link)) in targets
    # No intermediate temp files leaked.
    assert not list(tmp_path.glob("*.tmp"))
