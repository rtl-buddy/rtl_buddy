## VCS license waits pause the simulation timeout

While VCS prints its license-queue banner, rtl_buddy pauses `sim_timeout` for up to one hour, so a queued run can outlive its timeout. An unrecognized newer banner resumes the clock too early; a timeout next to license messages in `test.err` indicates this. Set the builder's `extra-sim-timeout` as a backstop.
