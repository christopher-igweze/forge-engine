# Changelog

## v1.5.0 (2026-04-10)

### Scoring
- Tuned dimension deductions to be severity-weighted and harsher: critical=-35, high=-20, medium=-8, low=-3.
- Standardized all deterministic check deductions through a central `severity_deduction()` helper so one dimension with 5+ critical/high failures scores below 50.
- Applied the same severity table to Opengrep-derived scores for parity with built-in checks.

### Reports
- `discovery_report.findings` now includes failed deterministic checks alongside LLM and Opengrep findings, with de-duplication so a single issue is never reported twice.
- Report generation runs even for repos whose only issues are deterministic-check failures.
- Added `findings_sources` breakdown to the discovery report so consumers can tell which findings came from which pipeline.

### Filtering
- `.forgeignore` rules are now applied to Opengrep findings BEFORE the v3 evaluation scores them, so suppressed findings no longer inflate deductions.

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
