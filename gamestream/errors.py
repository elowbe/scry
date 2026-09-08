class GameStreamError(RuntimeError):
    """An actionable, user-facing host error."""


class PreflightError(GameStreamError):
    """A required host capability is unavailable."""


class SessionConflict(GameStreamError):
    """A second game or controlling client was requested."""

