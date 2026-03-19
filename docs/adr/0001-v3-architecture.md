# ADR 0001: v3 Architecture — 3 Agents, Deterministic Scoring

## Status
Accepted (2026-03-19)

## Context
FORGE v2 used 12 agents with remediation loops, costing $20+ per scan. Quality auditor and architecture reviewer produced 50%+ false positives.

## Decision
Strip to 3 LLM agents (Codebase Analyst, Security Auditor, Fix Strategist) + deterministic evaluation (Opengrep + 48 checks + AIVSS). Score from deterministic findings only.

## Consequences
- Scan cost dropped from $20+ to ~$0.50
- Score is deterministic (same code = same score)
- Remediation happens outside FORGE (Claude Code, Cursor)
- Quality/architecture covered by deterministic checks
