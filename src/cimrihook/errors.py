"""CimriHook's own error types."""


class CimriHookError(Exception):
    """Base of all CimriHook errors."""


class HookPayloadError(CimriHookError):
    """A Claude Code hook payload or a tool result does not match the expected schema."""


class ConfigError(CimriHookError):
    """The environment variable configuration is invalid."""


class BenchError(CimriHookError):
    """The evaluation harness could not be set up or a run could not be measured."""


class TranscriptError(CimriHookError):
    """A transcript could not be read or its record is not in the expected format."""


class LedgerError(CimriHookError):
    """The ledger database could not be accessed (lock, permissions or a corrupt file)."""
