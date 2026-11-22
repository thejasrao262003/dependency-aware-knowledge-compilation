"""Exceptions raised by DAKC's public API.

Every error the library raises on purpose derives from :class:`DakcError`, so
callers can catch one type. Most also derive from the matching built-in
(``FileNotFoundError``, ``LookupError``, ``ValueError``...), so existing
``except`` clauses keep working.
"""

from __future__ import annotations


class DakcError(Exception):
    """Base class for errors raised by DAKC."""


class WorkspaceNotFound(DakcError, FileNotFoundError):
    """The directory has no ``dakc.toml``."""


class ArtifactNotFound(DakcError, LookupError):
    """No current artifact or source matches the given identifier."""


class InvalidArgument(DakcError, ValueError):
    """An argument has an unsupported value (unknown strategy, format, kind...)."""


class BuildLocked(DakcError, RuntimeError):
    """Another process holds the workspace build lock."""


class ConcurrentBuildError(DakcError, RuntimeError):
    """HEAD moved between loading the state and committing (a concurrent writer)."""


class RollbackError(DakcError, ValueError):
    """The requested rollback target is not a committed build, or is already HEAD."""


class SourceError(DakcError, RuntimeError):
    """A source is misconfigured, could not be listed or read, or its plugin could not be loaded.
    A build that hits it fails before anything is committed."""


class PublishError(DakcError, RuntimeError):
    """A publisher is misconfigured or its plugin could not be loaded. (A publisher that
    fails while publishing does not raise: its work stays pending and is reported.)"""


class CostNotAllowed(DakcError, PermissionError):
    """The operation would re-pay LLM calls and cost was not explicitly allowed."""


class CompileFailed(DakcError, RuntimeError):
    """Compilation failed and nothing was committed.

    ``preserved`` is True when HEAD still points at the previous committed
    build; the knowledge base and the wiki are then exactly as before.
    """

    def __init__(self, message: str, *, head: int, preserved: bool):
        super().__init__(message)
        self.head = head
        self.preserved = preserved
