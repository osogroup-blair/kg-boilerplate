"""compat/model-purposes.yaml + compat/pricing.yaml, validated at startup. A bad file is a startup error, never a runtime surprise."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

CLASSIFICATION_ORDER = ["public", "internal", "confidential", "restricted", "local_only"]


class ConfigError(ValueError):
    pass


class PurposeSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    alias: str
    requires: list[str] = []
    fallback_alias: str | None = None
    task_type: str = "general"


class EmbeddingSpec(BaseModel):
    alias: str
    dim: int
    max_input_tokens: int
    prefix: dict[str, str]
    embedding_model_id: str
    embedding_version: int = 1

    @model_validator(mode="after")
    def _check(self) -> EmbeddingSpec:
        if self.dim <= 0 or self.max_input_tokens <= 0:
            raise ValueError("embedding.dim and embedding.max_input_tokens must be positive")
        if set(self.prefix) != {"query", "passage"}:
            raise ValueError("embedding.prefix needs exactly 'query' and 'passage'")
        return self


class EgressPolicy(BaseModel):
    allowed_classifications: list[str]
    denied_classifications: list[str]
    on_denied: str = "skipped_by_policy"

    @model_validator(mode="after")
    def _check(self) -> EgressPolicy:
        both = set(self.allowed_classifications) & set(self.denied_classifications)
        if both:
            raise ValueError(f"classification(s) both allowed and denied: {sorted(both)}")
        unknown = (set(self.allowed_classifications) | set(self.denied_classifications)) - set(CLASSIFICATION_ORDER)
        if unknown:
            raise ValueError(f"unknown classification(s): {sorted(unknown)}")
        return self


class ModelConfig(BaseModel):
    purposes: dict[str, PurposeSpec]
    embedding: EmbeddingSpec
    egress: EgressPolicy
    pricing: dict[str, Any]

    def aliases(self) -> set[str]:
        out = {p.alias for p in self.purposes.values()} | {p.fallback_alias for p in self.purposes.values() if p.fallback_alias}
        return {a for a in out if a} | {self.embedding.alias}

    def egress_allows(self, classification: str) -> bool:
        return classification in self.egress.allowed_classifications and classification not in self.egress.denied_classifications

    def estimate_cost(self, alias: str, prompt: int | None, completion: int | None) -> float | None:
        model = self.pricing.get("alias_primary", {}).get(alias)
        price = self.pricing.get("prices", {}).get(model) if model else None
        if not price or prompt is None:
            return None
        return float(round((prompt * price["input"] + (completion or 0) * price["output"]) / 1_000_000, 8))


def load_config(purposes_path: Path, pricing_path: Path) -> ModelConfig:
    try:
        raw = yaml.safe_load(Path(purposes_path).read_text())
        pricing = yaml.safe_load(Path(pricing_path).read_text())
        purposes = dict(raw["model_purposes"])
        embedding = purposes.pop("embedding", None)
        if embedding is None:
            raise ValueError("model_purposes.embedding is missing")
        return ModelConfig(
            purposes={k: PurposeSpec(**v) for k, v in purposes.items()},
            embedding=EmbeddingSpec(**embedding),
            egress=EgressPolicy(**raw["egress"]),
            pricing=pricing,
        )
    except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"invalid model config: {exc}") from exc
