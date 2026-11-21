"""Workspace layout and configuration (``dakc.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dakc.hashing import entity_key, h_obj
from dakc.providers import ProviderBundle, make_provider
from dakc.storage import Store

CONFIG_NAME = "dakc.toml"
STATE_DIR = ".dakc"


@dataclass
class Config:
    source_dir: str = "knowledge"
    output_dir: str = "wiki"
    provider: str = "heuristic"
    jobs: int = 4
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def alias_map(self) -> dict[str, str]:
        """normalized alias key -> canonical display name"""
        return {entity_key(k): v for k, v in sorted(self.aliases.items())}

    @property
    def fingerprint(self) -> str:
        """Hash of configuration that changes derived content (entity aliases)."""
        return h_obj({"aliases": self.alias_map})

    def to_toml(self) -> str:
        lines = [
            "[dakc]",
            f'source_dir = "{self.source_dir}"',
            f'output_dir = "{self.output_dir}"',
            f'provider = "{self.provider}"   # heuristic | mock | openai | anthropic',
            f"jobs = {self.jobs}",
            "",
            "# Options for LLM providers (only the selected provider is used).",
            "[provider.openai]",
            'base_url = "http://localhost:11434/v1"   # Ollama; or https://api.openai.com/v1',
            'model = "llama3.1"',
            'api_key_env = "OPENAI_API_KEY"',
            'json_mode = "none"                      # none | json_object | json_schema',
            "",
            "[provider.anthropic]",
            'model = "claude-opus-5-5"',
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
            provider_options=data.get("provider", {}),
            aliases=dict(data.get("aliases", {})),
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

    def provider(self, override: str | None = None) -> ProviderBundle:
        name = override or self.config.provider
        options = {} if name in ("heuristic", "mock") else dict(self.config.provider_options.get(name, {}))
        return make_provider(name, options)
