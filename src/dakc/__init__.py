"""DAKC: Dependency-Aware Knowledge Compiler.

.. code-block:: python

    import dakc

    with dakc.open("path/to/workspace") as kb:
        report = kb.compile()

See :mod:`dakc.kb` for the full API.
"""

__version__ = "0.7.0"

from dakc.compiler import BuildReport  # noqa: E402
from dakc.errors import (  # noqa: E402
    ArtifactNotFound,
    BuildLocked,
    CompileFailed,
    ConcurrentBuildError,
    CostNotAllowed,
    DakcError,
    InvalidArgument,
    PublishError,
    RollbackError,
    SourceError,
    WorkspaceNotFound,
)
from dakc.kb import (  # noqa: E402
    ArtifactContent,
    ArtifactSummary,
    BrokenLink,
    BuildDiff,
    Conflict,
    ConflictLocation,
    ConflictValue,
    Downstream,
    DuplicatePair,
    KnowledgeBase,
    RollbackResult,
    SourceChanges,
    SourceInfo,
    Status,
    VerifyReport,
    ingest,
    init,
)
from dakc.kb import open_kb as open  # noqa: E402,A001  (dakc.open, like tarfile.open)
from dakc.parsing import split_sentences  # noqa: E402
from dakc.provenance import Explanation  # noqa: E402
from dakc.provenance.trace import ClaimTrace, PageTrace  # noqa: E402
from dakc.publishers import Publisher, PublishResult, Record  # noqa: E402
from dakc.sources import Item, SourceConnector  # noqa: E402
from dakc.storage import BuildRecord  # noqa: E402

__all__ = [
    "__version__",
    "open",
    "init",
    "ingest",
    "KnowledgeBase",
    # results
    "BuildReport",
    "BuildRecord",
    "BuildDiff",
    "BrokenLink",
    "Conflict",
    "ConflictLocation",
    "ConflictValue",
    "ArtifactContent",
    "ArtifactSummary",
    "Downstream",
    "DuplicatePair",
    "Explanation",
    "ClaimTrace",
    "PageTrace",
    "RollbackResult",
    "SourceChanges",
    "SourceInfo",
    "Status",
    "VerifyReport",
    # text: the sentences that claim evidence and comment refs index (versioned by PARSER)
    "split_sentences",
    # extending (RFC 0001)
    "Item",
    "SourceConnector",
    "Publisher",
    "PublishResult",
    "Record",
    # errors
    "DakcError",
    "ArtifactNotFound",
    "BuildLocked",
    "CompileFailed",
    "ConcurrentBuildError",
    "CostNotAllowed",
    "InvalidArgument",
    "PublishError",
    "RollbackError",
    "SourceError",
    "WorkspaceNotFound",
]
