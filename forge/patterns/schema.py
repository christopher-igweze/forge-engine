"""Pydantic models for the Vulnerability Pattern Library.

A VulnerabilityPattern describes a CLASS of vulnerability with both
deterministic signals (regex, dependency, file-presence — zero LLM cost)
and LLM guidance (reasoning prompts, key questions, tech-specific hints).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PatternTier(str, Enum):
    """How the pattern is evaluated."""

    DETERMINISTIC = "deterministic"  # Pure regex/AST — no LLM
    HYBRID = "hybrid"  # Deterministic signals + LLM reasoning
    LLM_ONLY = "llm_only"  # No deterministic signals, LLM only


class PatternSource(str, Enum):
    """Where the pattern originated."""

    CURATED = "curated"  # Hand-written by security engineers
    SCAN_DERIVED = "scan_derived"  # Proposed by extraction pipeline


class SignalType(str, Enum):
    """Type of deterministic signal."""

    REGEX = "regex"
    AST = "ast"
    DEPENDENCY = "dependency"
    FILE_PRESENCE = "file_presence"
    SCHEMA_COLUMN = "schema_column"


class DeterministicSignal(BaseModel):
    """A single deterministic check within a pattern."""

    signal_type: SignalType
    description: str = ""
    patterns: list[str] = Field(default_factory=list)
    file_globs: list[str] = Field(default_factory=list)
    package_names: list[str] = Field(default_factory=list)
    column_name_patterns: list[str] = Field(default_factory=list)
    weight: float = 1.0
    is_positive: bool = True  # True = bad if present, False = bad if absent


class LLMGuidance(BaseModel):
    """Context injected into LLM prompts to guide vulnerability detection."""

    reasoning_prompt: str = ""
    examples: list[dict] = Field(default_factory=list)
    counter_examples: list[dict] = Field(default_factory=list)
    key_questions: list[str] = Field(default_factory=list)
    technology_variants: dict[str, str] = Field(default_factory=dict)


class VulnerabilityPattern(BaseModel):
    """A technology-agnostic vulnerability pattern definition.

    Combines deterministic signals with LLM guidance to detect a CLASS
    of vulnerability across different technology stacks.
    """

    id: str  # "VP-001"
    name: str  # "Client-writable server-authority columns"
    slug: str  # "client-writable-server-authority"
    description: str = ""

    category: str = "security"  # security | reliability | architecture
    severity_default: str = "critical"
    cwe_ids: list[str] = Field(default_factory=list)
    owasp_refs: list[str] = Field(default_factory=list)

    tier: PatternTier = PatternTier.HYBRID

    signals: list[DeterministicSignal] = Field(default_factory=list)
    deterministic_threshold: float = 0.7

    llm_guidance: LLMGuidance = Field(default_factory=LLMGuidance)

    source: PatternSource = PatternSource.CURATED
    source_url: str = ""

    fix_strategy: str = ""
    fix_examples: dict[str, str] = Field(default_factory=dict)

    # Prevalence tracking (updated by extraction pipeline)
    times_detected: int = 0
    times_confirmed: int = 0
    false_positive_rate: float = 0.0
