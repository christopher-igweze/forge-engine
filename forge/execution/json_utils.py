"""Shared JSON parsing utilities for FORGE orchestration.

LLM responses arrive in many shapes: raw dicts, AgentField envelopes,
markdown-fenced JSON, JSON buried in prose, or plain text with no JSON at all.
This module provides a single resilient entry point — ``safe_parse_agent_response``
— that handles all of these cases so callers don't have to.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Pre-compiled patterns
_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```",
    re.DOTALL,
)
_JSON_OBJECT_RE = re.compile(
    r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}",
    re.DOTALL,
)


def strip_json_fences(text: str) -> str:
    """Strip markdown code fences from text.

    Handles ````` ```json ... ``` ````` and bare ````` ``` ... ``` `````.
    If multiple fenced blocks exist, returns the content of the first one.
    If no fences are found, returns the original text stripped of whitespace.

    >>> strip_json_fences('```json\\n{"a": 1}\\n```')
    '{"a": 1}'
    >>> strip_json_fences('{"a": 1}')
    '{"a": 1}'
    """
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def extract_json_object(text: str) -> dict | None:
    """Find and extract the first valid JSON object from free-form text.

    Handles JSON embedded in prose, markdown, or explanatory text.
    Tries progressively less strict strategies:
      1. Direct ``json.loads`` on the full (fence-stripped) text.
      2. Regex extraction of the outermost ``{...}`` block.

    Returns ``None`` if no valid JSON object can be found.

    >>> extract_json_object('Here is the result: {"action": "DEFER"} done.')
    {'action': 'DEFER'}
    >>> extract_json_object('no json here')
    """
    # Step 1: Strip fences and try direct parse
    cleaned = strip_json_fences(text)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 2: Regex extraction — find all candidate {…} blocks,
    # try each one starting from the longest (most likely to be the full object).
    candidates = _JSON_OBJECT_RE.findall(text)
    # Sort longest-first so we prefer the outermost/complete object
    for candidate in sorted(candidates, key=len, reverse=True):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue

    return None


def safe_parse_agent_response(
    raw: Any,
    fallback: dict | None = None,
) -> dict:
    """Parse an agent response into a dict, handling all common LLM response shapes.

    Handles (in order):
      1. ``None`` / empty  → return *fallback*.
      2. ``dict`` with AgentField envelope (``{"result": ..., "status": ...}``)
         → unwrap and recurse on the inner value.
      3. ``dict`` with ``"text"`` key (raw text response) → extract JSON from text.
      4. Plain ``dict`` → return as-is.
      5. ``str`` → extract JSON from the string.
      6. All else → return *fallback*.

    Parameters
    ----------
    raw:
        The raw response from an AgentField ``app.call()``.
    fallback:
        Value to return when parsing fails entirely.  Defaults to ``{}``.

    Returns
    -------
    dict
        The parsed response, or *fallback* on total failure.
    """
    if fallback is None:
        fallback = {}

    # 1. None / empty
    if raw is None:
        logger.warning("Agent response is None — using fallback")
        return fallback

    # 2-4. Dict handling
    if isinstance(raw, dict):
        # 2. AgentField envelope
        if "result" in raw and "status" in raw:
            inner = raw.get("result")
            if inner is not None:
                logger.debug("Unwrapped AgentField envelope (status=%s)", raw.get("status"))
                return safe_parse_agent_response(inner, fallback=fallback)
            # result is None inside a valid envelope
            logger.warning("AgentField envelope has null result — using fallback")
            return fallback

        # 3. Raw text response  {"text": "..."}
        if "text" in raw and isinstance(raw["text"], str):
            extracted = extract_json_object(raw["text"])
            if extracted is not None:
                logger.debug("Extracted JSON from text response (%d chars)", len(raw["text"]))
                return extracted
            # text key present but no JSON inside — return the dict itself
            # (caller may still want the text key)
            logger.warning(
                "Could not extract JSON from text response (first 200 chars): %s",
                raw["text"][:200],
            )
            return raw

        # 4. Plain dict
        return raw

    # 5. String response
    if isinstance(raw, str):
        extracted = extract_json_object(raw)
        if extracted is not None:
            logger.debug("Extracted JSON from string response (%d chars)", len(raw))
            return extracted
        logger.warning(
            "Could not extract JSON from string response (first 200 chars): %s",
            raw[:200],
        )
        return fallback

    # 6. Unrecognised type
    logger.warning("Unexpected agent response type %s — using fallback", type(raw).__name__)
    return fallback
