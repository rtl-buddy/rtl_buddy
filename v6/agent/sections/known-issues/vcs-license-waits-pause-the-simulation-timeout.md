## VCS license waits pause the simulation timeout

When VCS prints its license-queue banner, rtl_buddy pauses `sim_timeout` until simulator output resumes, for up to one hour. A queued run can therefore outlive its nominal timeout. If a newer VCS banner is not recognized, the clock may resume too early; a timeout beside license messages in `test.err` indicates this case. Use the builder's `extra-sim-timeout` as a backstop.
