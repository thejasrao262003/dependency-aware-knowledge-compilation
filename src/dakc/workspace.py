"""Workspace layout and configuration (``dakc.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dakc.hashing import entity_key, h_obj
from dakc.providers import ProviderBundle, make_provider
from dakc.sources import FilesystemSource, Source, SourceSet, make_source
from dakc.storage import Store

CONFIG_NAME = "dakc.toml"
STATE_DIR = ".dakc"


@dataclass
class Config:
    source_dir: str = "knowledge"
    output_dir: str = "wiki"
    provider: str = "heuristic"
    jobs: int = 4
    provenance: str = "report"
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)  # [[sources]] tables (RFC 0001); empty: source_dir
    publishers: list[dict[str, Any]] = field(default_factory=list)  # [[publishers]] tables (RFC 0001)
    conflicts: dict[str, Any] = field(default_factory=dict)  # [conflicts]: scope rules and accepted findings (RFC 0002)

    @property
    def conflict_rules(self) -> list:
        from dakc.conflicts import rules_from_config

        return rules_from_config(self.conflicts)[0]

    @property
    def conflict_verifier(self):
        """The conflict verifier's settings (``dakc.conflicts.verifier.Settings``), or None when off."""
        from dakc.conflicts.verifier import Settings

        return Settings.from_config(self.conflicts)

    @property
    def accepted_conflicts(self) -> dict[str, str]:
        """Conflict id -> the reason it was accepted."""
        from dakc.conflicts import rules_from_config

        return rules_from_config(self.conflicts)[1]

    @property
    def alias_map(self) -> dict[str, str]:
        """normalized alias key -> canonical display name"""
        return {entity_key(k): v for k, v in sorted(self.aliases.items())}

    @property
    def fingerprint(self) -> str:
        """Hash of configuration that changes derived content (entity aliases)."""
        from dakc import versions

        return h_obj({"aliases": self.alias_map, "entity_resolution": versions.ENTITY_RESOLUTION, "provenance": self.provenance})

    def to_toml(self) -> str:
        lines = [
            "[dakc]",
            f'source_dir = "{self.source_dir}"',
            f'output_dir = "{self.output_dir}"',
            f'provider = "{self.provider}"   # heuristic | mock | claude-code | anthropic | openai | gemini | grok | deepseek | mistral | groq | openrouter | together | ollama | lmstudio | custom',
            f"jobs = {self.jobs}",
            f'provenance = "{self.provenance}"   # report | strict (strict: pages keep only sentences that cite a claim)',
            "",
            "# Options for LLM providers (only the selected provider is used). Each has defaults:",
            "# set only what you want to change. Hosted APIs read their key from the environment:",
            "# OPENAI_API_KEY, GEMINI_API_KEY, XAI_API_KEY, DEEPSEEK_API_KEY, MISTRAL_API_KEY, ...",
            "",
            "# Claude through your Claude Code login (no API key): haiku | sonnet | opus | a model id",
            "[provider.claude-code]",
            'model = "haiku"',
            "",
            "# Claude through the API (pip install 'dakc[anthropic]'; ANTHROPIC_API_KEY)",
            "[provider.anthropic]",
            'model = "claude-opus-5-5"               # or claude-sonnet-5-5, claude-haiku-5-5',
            "",
            "[provider.openai]",
            'model = "gpt-5.4-mini"',
            '# base_url = "http://localhost:8000/v1"  # any other OpenAI-compatible server (vLLM, a gateway)',
            "",
            "[provider.gemini]",
            'model = "gemini-flash-latest"',
            "",
            "[provider.ollama]",
            'model = "llama3.1"                      # a model you have pulled',
            "",
            "# Bring your own LLM client: a callable returning an object with",
            "# .fingerprint and .complete(system, user, json_schema=None) -> Completion.",
            "[provider.custom]",
            '# factory = "my_client:make"',
            '# path = "plugins"                     # relative to this file',
            "",

            "# More sources, or other kinds of source (docs/rfcs/0001-sources-and-publishers.md).",
            "# Without [[sources]], source_dir above is the only source.",
            "# [[sources]]",
            '# name = "docs"',
            '# type = "filesystem"                  # or an installed connector, or "custom" with factory = "module:attr"',
            '# path = "knowledge"',
            '# prefix = ""                          # paths appear as prefix/...; defaults to the name',
            "",
            "# Where compiled knowledge goes besides wiki/ (a change feed, a vector database plugin, ...).",
            "# [[publishers]]",
            '# name = "feed"',
            '# type = "jsonl"',
            '# path = "feed/changes.jsonl"',
            '# kinds = ["claim", "page", "summary"]',
            "",
            "# Conflict detection (docs/rfcs/0002-conflicts.md). The rules are free and always on. The",
            "# verifier asks the provider's model about pairs of related claims the rules cannot compare;",
            "# it is off by default and costs model calls (`dakc compile --dry-run` counts them).",
            "# [conflicts]",
            '# verifier = "llm"                      # or "none"',
            "# max_checks = 500                      # pairs judged per compile; the rest wait for the next one",
            "# Tell it which documents describe another time, release or environment, so their",
            "# differences are reported as scoped, not as conflicts.",
            "# [[conflicts.scope]]",
            '# path = "docs/mvp/**"',
            '# label = "release 1"                   # or status = "past" | "proposed"',
            "# A conflict you have reviewed and decided to keep:",
            "# [[conflicts.accept]]",
            '# id = "cfl:0123456789"',
            '# reason = "the two services really do use different ports"',
            "",
            "# Map alternative spellings to one entity, e.g. \"Postgres\" = \"PostgreSQL\".",
            "[aliases]",
        ]
        lines += [f'"{k}" = "{v}"' for k, v in sorted(self.aliases.items())]
        return "\n".join(lines) + "\n"


class Workspace:
    def __init__(self, root: Path, config: Config):
        self.root = root.resolve()
        self.config = config

    # ----------------------------------------------------------- construction
    @classmethod
    def load(cls, root: Path) -> Workspace:
        path = root / CONFIG_NAME
        if not path.exists():
            raise FileNotFoundError(f"no {CONFIG_NAME} in {root.resolve()} (run `dakc init` or `dakc ingest <dir>`)")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        core = data.get("dakc", {})
        cfg = Config(
            source_dir=core.get("source_dir", "knowledge"),
            output_dir=core.get("output_dir", "wiki"),
            provider=core.get("provider", "heuristic"),
            jobs=int(core.get("jobs", 4)),
            provenance=core.get("provenance", "report"),
            provider_options=data.get("provider", {}),
            aliases=dict(data.get("aliases", {})),
            sources=[dict(t) for t in data.get("sources", [])],
            publishers=[dict(t) for t in data.get("publishers", [])],
            conflicts=dict(data.get("conflicts", {})),
        )
        return cls(root, cfg)

    @classmethod
    def init(cls, root: Path, source_dir: str = "knowledge", provider: str = "heuristic", output_dir: str = "wiki") -> Workspace:
        root.mkdir(parents=True, exist_ok=True)
        path = root / CONFIG_NAME
        if path.exists():
            return cls.load(root)
        cfg = Config(source_dir=source_dir, output_dir=output_dir, provider=provider)
        path.write_text(cfg.to_toml(), encoding="utf-8")
        return cls(root, cfg)

    # ------------------------------------------------------------------ paths
    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def db_path(self) -> Path:
        return self.state_dir / "dakc.db"

    @property
    def source_root(self) -> Path:
        return (self.root / self.config.source_dir).resolve()

    @property
    def output_root(self) -> Path:
        return (self.root / self.config.output_dir).resolve()

    @property
    def excluded(self) -> tuple[Path, ...]:
        return (self.state_dir, self.output_root)

    def store(self) -> Store:
        return Store(self.db_path)

    def source_set(self) -> SourceSet:
        """Every configured source; without [[sources]], the one directory ``source_dir``."""
        if self.config.sources:
            return SourceSet([make_source(spec, self.root, self.excluded) for spec in self.config.sources])
        return SourceSet([Source(Path(self.config.source_dir).name or "knowledge", FilesystemSource(self.source_root, self.excluded))])

    def publishers(self) -> list[tuple[str, Any]]:
        """Configured [[publishers]] as (name, publisher)."""
        from dakc.errors import PublishError
        from dakc.publishers import make_publisher

        out = [make_publisher(spec, self.root) for spec in self.config.publishers]
        names = [n for n, _ in out]
        if len(set(names)) != len(names):
            raise PublishError(f"publisher names must be unique: {names}")
        return out

    def provider(self, override: str | None = None) -> ProviderBundle:
        name = override or self.config.provider
        options = {} if name in ("heuristic", "mock") else dict(self.config.provider_options.get(name, {}))
        if name == "custom" and options.get("path"):
            options["path"] = str((self.root / options["path"]).resolve())
        return make_provider(name, options)
