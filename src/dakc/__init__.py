"""DAKC: Dependency-Aware Knowledge Compiler.

.. code-block:: python

    import dakc

    with dakc.open("path/to/workspace") as kb:
        report = kb.compile()

See :mod:`dakc.kb` for the full API.
"""

__version__ = "0.2.1"

from dakc.compiler import BuildReport  # noqa: E402
from dakc.errors import (  # noqa: E402
    ArtifactNotFound,
    BuildLocked,
    CompileFailed,
    ConcurrentBuildError,
    CostNotAllowed,
    DakcError,
    InvalidArgument,
    RollbackError,
    WorkspaceNotFound,
)
from dakc.kb import (  # noqa: E402
    ArtifactContent,
    ArtifactSummary,
    BuildDiff,
    Downstream,
    DuplicatePair,
    KnowledgeBase,
    RollbackResult,
    SourceChanges,
    Status,
    VerifyReport,
    ingest,
    init,
)
from dakc.kb import open_kb as open  # noqa: E402,A001  (dakc.open, like tarfile.open)
from dakc.provenance import Explanation  # noqa: E402
from dakc.provenance.trace import ClaimTrace, PageTrace  # noqa: E402
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
    "ArtifactContent",
    "ArtifactSummary",
    "Downstream",
    "DuplicatePair",
    "Explanation",
    "ClaimTrace",
    "PageTrace",
    "RollbackResult",
    "SourceChanges",
    "Status",
    "VerifyReport",
    # errors
    "DakcError",
    "ArtifactNotFound",
    "BuildLocked",
    "CompileFailed",
    "ConcurrentBuildError",
    "CostNotAllowed",
    "InvalidArgument",
    "RollbackError",
    "WorkspaceNotFound",
]
