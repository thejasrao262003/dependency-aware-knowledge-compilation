"""Versions of DAKC's own deterministic logic.

Provider fingerprints cover what models and prompts produce. These constants
cover what DAKC's code produces. Bump one whenever the corresponding code would
produce different output for the same input. Every build records them, and a
mismatch with the store invalidates exactly the affected layer:

* PARSER             parsing and segmentation    -> re-parse every source
* CLAIM_IDENTITY     claim normalization / ids    -> re-derive every claim (from the extraction cache)
* ENTITY_RESOLUTION  entity keys, names, pairing  -> re-resolve entities (part of the config fingerprint)
* RENDER             page / summary / index text  -> re-render, no provider calls
* SUMMARY            topic summary structure      -> reconsider every topic (large ones are summarized in sections)
* PAGE               entity page structure        -> reconsider every page (hubs are written in sections)

A store whose builds predate this mechanism has no recorded versions and is
treated as outdated in every layer.
"""

PARSER = "parse/3"  # 2: Markdown tables parsed, one labelled block per row; 3: re-parsing refreshes the heading of unchanged passages
CLAIM_IDENTITY = "claim-id/2"
ENTITY_RESOLUTION = "entity/1"
RENDER = "render/2"  # 2: claims that only past-scoped documents support are listed under History on their pages
SUMMARY = "summary/2"  # 2: topics with more than 64 pages are summarized hierarchically, in sections
PAGE = "page/2"  # 2: entity pages with more than 64 claims are written in sections grouped by source


def current() -> dict[str, str]:
    return {
        "parser": PARSER,
        "claim_identity": CLAIM_IDENTITY,
        "entity_resolution": ENTITY_RESOLUTION,
        "render": RENDER,
        "summary": SUMMARY,
        "page": PAGE,
    }
