"""Router exceptions."""

from datetime import datetime


class RouterError(Exception):
    """A router call could not be completed."""


class QuotaExhausted(RouterError):
    """Every usable entry for a role is out of quota. A normal state: stop, resume later."""

    def __init__(self, role: str, earliest_reset: datetime | None, attempts: list[str]):
        self.role = role
        self.earliest_reset = earliest_reset
        self.attempts = attempts
        when = earliest_reset.isoformat() if earliest_reset else "unknown"
        super().__init__(f"{role}: quota exhausted on every entry; earliest reset {when}")


class AllEntriesFailed(RouterError):
    """Every entry failed for reasons other than quota (bad keys, bad output, outages)."""

    def __init__(self, role: str, attempts: list[str]):
        self.role = role
        self.attempts = attempts
        super().__init__(f"{role}: every entry failed: " + "; ".join(attempts))
