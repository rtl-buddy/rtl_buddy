"""Recording the events the P&R backend logs, for tests that assert on them."""


def _capture_pnr_events(monkeypatch):
    """Record every `log_event` the P&R backend emits, with its level.

    `caplog` cannot be used past the first console write: `task_status` initialises
    rtl_buddy's logging, which clears pytest's root handlers.
    """
    from rtl_buddy.tools import pnr_openroad

    events: list[tuple[int, str, dict]] = []
    real = pnr_openroad.log_event

    def _record(logger, level, event, /, **fields):
        events.append((level, event, fields))
        return real(logger, level, event, **fields)

    monkeypatch.setattr(pnr_openroad, "log_event", _record)
    return events


def _one_event(events, name):
    """The single recorded event called `name`, as `(level, fields)`."""
    matches = [(level, fields) for level, event, fields in events if event == name]
    assert len(matches) == 1, f"{name}: {[e[1] for e in events]}"
    return matches[0]
