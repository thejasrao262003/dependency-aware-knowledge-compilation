"""Versions of DAKC's own deterministic logic.

Provider fingerprints cover what models and prompts produce. These constants
cover what DAKC's code produces. Bump one whenever the corresponding code would
produce different output for the same input. Every build records them, and a
mismatch with the store invalidates exactly the affected layer:

* PARSER             parsing and segmentation    -> re-parse every source
* CLAIM_IDENTITY     claim normalization / ids    -> re-derive every claim (from the extraction cache)
* ENTITY_RESOLUTION  entity keys, names, pairing  -> re-resolve entities (part of the config fingerprint)
* RENDER             page / summary / index text  -> re-render, no provider calls

A store whose builds predate this mechanism has no recorded versions and is
treated as outdated in every layer.
"""

PARSER = "parse/2"  # 2: Markdown tables parsed, one labelled block per row
CLAIM_IDENTITY = "claim-id/2"
ENTITY_RESOLUTION = "entity/1"
RENDER = "render/1"


def current() -> dict[str, str]:
    return {
        "parser": PARSER,
        "claim_identity": CLAIM_IDENTITY,
        "entity_resolution": ENTITY_RESOLUTION,
        "render": RENDER,
    }
