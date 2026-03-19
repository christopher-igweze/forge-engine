"""Auto-detect AARS amplification factors from code analysis.

Scans the codebase for patterns that indicate agentic AI capabilities.
All detection is deterministic (regex + file scanning, no LLM).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Skip directories
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "vendor",
    ".forge-artifacts", ".forge-worktrees", "dist", "build", ".next",
}


def _iter_source_files(
    repo_path: str, extensions: tuple = (".py", ".js", ".ts", ".jsx", ".tsx")
) -> list[Path]:
    """Collect source files, excluding vendor/test dirs."""
    files = []
    for root, dirs, fnames in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fnames:
            if any(f.endswith(ext) for ext in extensions):
                files.append(Path(root) / f)
    return files


def _read_safe(path: Path, max_size: int = 500_000) -> str:
    """Read file safely, skip binary/large files."""
    try:
        if path.stat().st_size > max_size:
            return ""
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def _search_files(files: list[Path], patterns: list[str], flags: int = re.IGNORECASE) -> int:
    """Count files matching any of the regex patterns."""
    count = 0
    compiled = [re.compile(p, flags) for p in patterns]
    for f in files:
        content = _read_safe(f)
        if any(p.search(content) for p in compiled):
            count += 1
    return count


def detect_aars_factors(repo_path: str, codebase_map: dict | None = None) -> dict[str, float]:
    """Detect AARS factors from codebase analysis.

    Returns dict mapping factor name -> score (0.0, 0.5, or 1.0).
    """
    files = _iter_source_files(repo_path)
    if not files:
        return {f: 0.0 for f in [
            "execution_autonomy", "tool_control_surface", "natural_language_interface",
            "contextual_awareness", "behavioral_non_determinism", "opacity_reflexivity",
            "persistent_state", "dynamic_identity", "multi_agent_interactions",
            "self_modification",
        ]}

    factors: dict[str, float] = {}

    # 1. Execution Autonomy
    approval_patterns = [
        r"confirm|approval|human.in.the.loop|manual.review|await.confirmation",
        r"input\(\s*['\"].*(?:confirm|approve|proceed)",
        r"require_approval|needs_approval|approval_required",
    ]
    auto_exec_patterns = [
        r"auto.?execute|auto.?run|autonomous|self.?executing",
        r"\.execute\(\)|\.run\(\)|agent\.act\(",
    ]
    has_approval = _search_files(files, approval_patterns) > 0
    has_auto = _search_files(files, auto_exec_patterns) > 0

    if has_auto and not has_approval:
        factors["execution_autonomy"] = 1.0
    elif has_auto and has_approval:
        factors["execution_autonomy"] = 0.5
    else:
        factors["execution_autonomy"] = 0.0

    # 2. Tool Control Surface
    tool_patterns = [
        r"@tool|@mcp\.tool|register_tool|add_tool|tool_call",
        r"subprocess\.|os\.system\(|os\.popen\(",
        r"function_calling|tool_use|tools\s*=\s*\[",
    ]
    tool_count = _search_files(files, tool_patterns)
    if tool_count >= 6:
        factors["tool_control_surface"] = 1.0
    elif tool_count >= 1:
        factors["tool_control_surface"] = 0.5
    else:
        factors["tool_control_surface"] = 0.0

    # 3. Natural Language Interface
    nl_patterns = [
        r"prompt|user_message|chat_input|natural_language",
        r"completion|chat\.create|generate\(|\.invoke\(",
        r"langchain|openai\.chat|anthropic\.messages",
    ]
    sanitize_patterns = [
        r"sanitize|validate_input|escape|filter_prompt|injection.?detect",
    ]
    has_nl = _search_files(files, nl_patterns) > 0
    has_sanitize = _search_files(files, sanitize_patterns) > 0

    if has_nl and not has_sanitize:
        factors["natural_language_interface"] = 1.0
    elif has_nl and has_sanitize:
        factors["natural_language_interface"] = 0.5
    else:
        factors["natural_language_interface"] = 0.0

    # 4. Contextual Awareness
    env_patterns = [
        r"os\.environ|os\.getenv|process\.env",
        r"open\(|Path\(|fs\.|readFile",
        r"requests\.get|httpx\.|fetch\(|urllib",
    ]
    env_count = _search_files(files, env_patterns)
    if env_count >= 5:
        factors["contextual_awareness"] = 1.0
    elif env_count >= 1:
        factors["contextual_awareness"] = 0.5
    else:
        factors["contextual_awareness"] = 0.0

    # 5. Behavioral Non-Determinism
    llm_patterns = [
        r"temperature|top_p|top_k|sampling",
        r"openai\.|anthropic\.|llm\.|chat\.completions",
        r"random\.|np\.random|Math\.random",
    ]
    deterministic_patterns = [
        r"temperature\s*=\s*0|temperature\s*:\s*0",
        r"seed\s*=\s*\d+|random_state\s*=",
    ]
    has_llm = _search_files(files, llm_patterns) > 0
    has_determ = _search_files(files, deterministic_patterns) > 0

    if has_llm and not has_determ:
        factors["behavioral_non_determinism"] = 1.0
    elif has_llm and has_determ:
        factors["behavioral_non_determinism"] = 0.5
    else:
        factors["behavioral_non_determinism"] = 0.0

    # 6. Opacity & Reflexivity
    logging_patterns = [
        r"logging\.|logger\.|structlog|loguru",
        r"console\.log|winston|pino",
        r"trace|audit_log|opentelemetry|tracing",
    ]
    log_count = _search_files(files, logging_patterns)
    if log_count == 0:
        factors["opacity_reflexivity"] = 1.0
    elif log_count < len(files) * 0.3:
        factors["opacity_reflexivity"] = 0.5
    else:
        factors["opacity_reflexivity"] = 0.0

    # 7. Persistent State
    persist_patterns = [
        r"database|mongodb|postgres|mysql|sqlite|redis|dynamodb",
        r"session|cookie|localStorage|sessionStorage",
        r"\.save\(\)|\.persist\(\)|\.store\(\)|memory_store",
    ]
    stateless_patterns = [
        r"stateless|no.?state|ephemeral",
    ]
    has_persist = _search_files(files, persist_patterns) > 0
    has_stateless = _search_files(files, stateless_patterns) > 0

    if has_persist and not has_stateless:
        factors["persistent_state"] = 1.0
    elif has_persist:
        factors["persistent_state"] = 0.5
    else:
        factors["persistent_state"] = 0.0

    # 8. Dynamic Identity
    identity_patterns = [
        r"role\s*=|persona|identity|impersonate|as_user",
        r"system_prompt.*role|set_role|switch_role",
    ]
    has_identity = _search_files(files, identity_patterns) > 0
    if has_identity:
        factors["dynamic_identity"] = 0.5  # Conservative default
    else:
        factors["dynamic_identity"] = 0.0

    # 9. Multi-Agent Interactions
    agent_patterns = [
        r"multi.?agent|swarm|crew|autogen|agent.?team",
        r"spawn.*agent|agent\.create|orchestrat",
        r"message_passing|agent_bus|broadcast.*agent",
    ]
    supervised_patterns = [
        r"supervisor|human.?review|approval.?gate",
    ]
    has_multi = _search_files(files, agent_patterns) > 0
    has_supervised = _search_files(files, supervised_patterns) > 0

    if has_multi and not has_supervised:
        factors["multi_agent_interactions"] = 1.0
    elif has_multi:
        factors["multi_agent_interactions"] = 0.5
    else:
        factors["multi_agent_interactions"] = 0.0

    # 10. Self-Modification
    selfmod_patterns = [
        r"self\.code|modify_prompt|update_system_prompt|write.*\.py",
        r"exec\(|eval\(|compile\(",
        r"code_gen|generate_code|write_code",
    ]
    config_mod_patterns = [
        r"update_config|self_tune|auto_adjust|adaptive",
    ]
    has_selfmod = _search_files(files, selfmod_patterns) > 0
    has_configmod = _search_files(files, config_mod_patterns) > 0

    if has_selfmod:
        factors["self_modification"] = 1.0
    elif has_configmod:
        factors["self_modification"] = 0.5
    else:
        factors["self_modification"] = 0.0

    logger.info("AARS detection: %s", {k: v for k, v in factors.items() if v > 0})
    return factors
