# Changelog

## v3.0.0 (2026-03-19)

### Architecture
- Stripped to 3 LLM agents: Codebase Analyst, Security Auditor, Fix Strategist
- Removed remediation stack (AgentField, coders, worktrees, convergence)
- Removed Hive/Swarm discovery
- Removed learning/fine-tuning system

### Added
- Opengrep SAST integration (49 custom rules + community rules)
- Deterministic evaluation framework (48 checks across 7 dimensions)
- OWASP AIVSS scoring for AI/agentic projects
- Quality gate with configurable profiles (forge-way, strict, startup)
- ASVS/STRIDE/NIST SSDF compliance mapping
- CLI commands: status, config set/get, auth
- Per-agent false positive tracking

### Changed
- Score now driven by deterministic findings only (LLM findings are advisory)
- Pipeline reduced from 12 agents to 3 (~5 LLM calls)
- Scan cost reduced from $20+ to ~$0.50
