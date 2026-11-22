"""Deterministic providers.

``heuristic`` providers are free and are not counted as LLM calls. ``mock``
providers run the same algorithms but account for each call as an LLM call
with estimated tokens, so benchmarks can model LLM cost without a network.
Both share fingerprints because their outputs are identical.
"""

from __future__ import annotations

from collections import Counter

from dakc.claims import heuristic_extract
from dakc.providers.base import ClaimView, ExtractResult, GenResult, PageLead, estimate_tokens

EXTRACT_OVERHEAD = "Extract atomic factual claims and the entities they mention from the passage. " * 3
PAGE_OVERHEAD = "Write a wiki page body using only the numbered claims; cite them as [^Cn]. " * 3
SUMMARY_OVERHEAD = "Write a short topic overview from these page leads. " * 3


class HeuristicExtractor:
    fingerprint = "heuristic-extractor/3"
    free = True

    def __init__(self, as_llm: bool = False):
        self.is_llm = as_llm

    def extract(self, passage_text: str, targets: tuple[int, ...] | None = None) -> ExtractResult:
        claims = heuristic_extract(passage_text, targets)
        out_text = "\n".join(c.text + " | " + ", ".join(c.mentions) for c in claims)
        return ExtractResult(claims, estimate_tokens(EXTRACT_OVERHEAD + passage_text), estimate_tokens(out_text))


class TemplatePageWriter:
    fingerprint = "template-writer/1"
    free = True

    def __init__(self, as_llm: bool = False):
        self.is_llm = as_llm

    def write(self, name: str, claims: tuple[ClaimView, ...]) -> GenResult:
        co = Counter(m for c in claims for m in c.mentions if m.casefold() != name.casefold())
        about = ""
        if co:
            top = sorted(co.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            about = f", most often alongside {top}"
        n = len(claims)
        lead = f"{name} has {n} documented fact{'s' if n != 1 else ''}{about}."
        facts = "\n".join(f"- {c.text} [^{c.ref}]" for c in claims)
        body = f"{lead}\n\n## Facts\n\n{facts}"
        prompt = PAGE_OVERHEAD + name + "\n" + "\n".join(f"{c.ref}: {c.text}" for c in claims)
        return GenResult(body, estimate_tokens(prompt), estimate_tokens(body))


class TemplateSummarizer:
    fingerprint = "template-summarizer/1"
    free = True

    def __init__(self, as_llm: bool = False):
        self.is_llm = as_llm

    def summarize(self, topic: str, pages: tuple[PageLead, ...]) -> GenResult:
        n = len(pages)
        lines = "\n".join(f"- [{p.title}](../pages/{p.slug}.md): {p.lead}" for p in pages)
        body = f"The topic {topic} covers {n} page{'s' if n != 1 else ''}.\n\n## Pages\n\n{lines}"
        prompt = SUMMARY_OVERHEAD + topic + "\n" + "\n".join(f"{p.title}: {p.lead}" for p in pages)
        return GenResult(body, estimate_tokens(prompt), estimate_tokens(body))
