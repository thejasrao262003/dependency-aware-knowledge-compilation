"""Provider protocols shared by deterministic and LLM-backed implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from dakc.claims import ExtractedClaim


@dataclass(frozen=True)
class ClaimView:
    """A claim as shown to a page writer. ``ref`` is the citation label (C1, C2, ...)."""

    ref: str
    text: str
    mentions: tuple[str, ...]


@dataclass(frozen=True)
class PageLead:
    title: str
    slug: str
    lead: str


@dataclass
class ExtractResult:
    claims: list[ExtractedClaim]
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 1  # provider calls made (a JSON repair retry makes it 2)


@dataclass
class GenResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class Component(Protocol):
    fingerprint: str  # changes whenever outputs could change (algorithm, model, prompt)
    is_llm: bool  # counts towards "LLM calls"
    free: bool  # deterministic and costless: safe to execute during a dry run


class Extractor(Component, Protocol):
    def extract(self, passage_text: str, targets: tuple[int, ...] | None = None) -> ExtractResult:
        """Extract claims from ``passage_text``.

        ``targets``: indices into ``split_sentences(passage_text)``; when given,
        return only claims stated (at least partly) by those sentences, using the
        rest as context. Every claim must carry its ``evidence`` sentence indices.
        """
        ...


class PageWriter(Component, Protocol):
    def write(self, name: str, claims: tuple[ClaimView, ...]) -> GenResult: ...


class Summarizer(Component, Protocol):
    def summarize(self, topic: str, pages: tuple[PageLead, ...]) -> GenResult: ...


@dataclass
class ProviderBundle:
    name: str
    extractor: Extractor
    writer: PageWriter
    summarizer: Summarizer
    notes: list[str] = field(default_factory=list)

    @property
    def free(self) -> bool:
        return self.extractor.free and self.writer.free and self.summarizer.free

    def fingerprints(self) -> dict[str, str]:
        return {
            "extractor": self.extractor.fingerprint,
            "writer": self.writer.fingerprint,
            "summarizer": self.summarizer.fingerprint,
        }


def estimate_tokens(text: str) -> int:
    """Rough token estimate used by mock providers: ceil(chars / 4)."""
    return (len(text) + 3) // 4
