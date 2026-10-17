class LockstepError(Exception):
    """Base class; ``exit_code`` follows the command contract (1 check, 2 usage, 3 Lean)."""

    exit_code = 1


class UsageError(LockstepError):
    exit_code = 2


class ConfigError(UsageError):
    pass


class CheckFailed(LockstepError):
    exit_code = 1


class LeanError(LockstepError):
    exit_code = 3


class Refused(LeanError):
    """The guarded runner declined to start (memory, or a compiler already running); nothing ran."""


class ReadOnly(UsageError):
    """The harness database cannot be written from this process (sandbox or permissions)."""
