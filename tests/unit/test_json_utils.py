"""Tests for forge.execution.json_utils — resilient LLM response parsing."""

import pytest

from forge.execution.json_utils import (
    extract_json_object,
    safe_parse_agent_response,
    strip_json_fences,
)


# ── strip_json_fences ──────────────────────────────────────────────────


class TestStripJsonFences:
    def test_fenced_json(self):
        text = '```json\n{"a": 1}\n```'
        assert strip_json_fences(text) == '{"a": 1}'

    def test_bare_fences(self):
        text = '```\n{"a": 1}\n```'
        assert strip_json_fences(text) == '{"a": 1}'

    def test_no_fences(self):
        text = '{"a": 1}'
        assert strip_json_fences(text) == '{"a": 1}'

    def test_whitespace_stripping(self):
        text = '  {"a": 1}  '
        assert strip_json_fences(text) == '{"a": 1}'

    def test_multiple_fences_returns_first(self):
        text = '```json\n{"first": 1}\n```\n\n```json\n{"second": 2}\n```'
        result = strip_json_fences(text)
        assert '"first"' in result

    def test_fences_with_extra_text(self):
        text = 'Here is the output:\n```json\n{"a": 1}\n```\nDone.'
        assert strip_json_fences(text) == '{"a": 1}'


# ── extract_json_object ────────────────────────────────────────────────


class TestExtractJsonObject:
    def test_plain_json(self):
        assert extract_json_object('{"key": "value"}') == {"key": "value"}

    def test_fenced_json(self):
        text = '```json\n{"key": "value"}\n```'
        assert extract_json_object(text) == {"key": "value"}

    def test_json_in_prose(self):
        text = 'Here is the result: {"action": "DEFER"} done.'
        assert extract_json_object(text) == {"action": "DEFER"}

    def test_nested_json(self):
        text = '{"action": "SPLIT", "items": [{"title": "fix"}]}'
        result = extract_json_object(text)
        assert result is not None
        assert result["action"] == "SPLIT"

    def test_no_json(self):
        assert extract_json_object("no json here") is None

    def test_empty_string(self):
        assert extract_json_object("") is None

    def test_array_not_returned(self):
        """extract_json_object returns dicts only, not arrays."""
        assert extract_json_object('[1, 2, 3]') is None

    def test_json_with_leading_text(self):
        text = 'The analysis shows:\n\n{"score": 85, "label": "good"}'
        result = extract_json_object(text)
        assert result == {"score": 85, "label": "good"}

    def test_multiple_objects_returns_largest(self):
        text = '{"small": 1} and {"bigger": {"nested": true}, "key": "val"}'
        result = extract_json_object(text)
        assert result is not None
        assert "bigger" in result  # Should prefer the larger object


# ── safe_parse_agent_response ──────────────────────────────────────────


class TestSafeParseAgentResponse:
    def test_none_returns_fallback(self):
        assert safe_parse_agent_response(None) == {}

    def test_none_with_custom_fallback(self):
        assert safe_parse_agent_response(None, fallback={"default": True}) == {"default": True}

    def test_plain_dict(self):
        d = {"key": "value"}
        assert safe_parse_agent_response(d) == d

    def test_agentfield_envelope(self):
        envelope = {"result": {"key": "value"}, "status": "ok"}
        assert safe_parse_agent_response(envelope) == {"key": "value"}

    def test_agentfield_envelope_null_result(self):
        envelope = {"result": None, "status": "ok"}
        assert safe_parse_agent_response(envelope) == {}

    def test_nested_envelope(self):
        nested = {"result": {"result": {"deep": True}, "status": "ok"}, "status": "ok"}
        assert safe_parse_agent_response(nested) == {"deep": True}

    def test_text_response_with_json(self):
        raw = {"text": 'Here is the output: {"action": "DEFER"}'}
        assert safe_parse_agent_response(raw) == {"action": "DEFER"}

    def test_text_response_without_json(self):
        raw = {"text": "No JSON in this response."}
        # Returns the raw dict (text key preserved for caller)
        assert safe_parse_agent_response(raw) == raw

    def test_string_response_with_json(self):
        raw = '{"key": "value"}'
        assert safe_parse_agent_response(raw) == {"key": "value"}

    def test_string_response_fenced(self):
        raw = '```json\n{"key": "value"}\n```'
        assert safe_parse_agent_response(raw) == {"key": "value"}

    def test_string_response_no_json(self):
        assert safe_parse_agent_response("no json") == {}

    def test_unexpected_type(self):
        assert safe_parse_agent_response(42) == {}
        assert safe_parse_agent_response([1, 2, 3]) == {}

    def test_dict_with_status_but_no_result(self):
        """A dict with 'status' but no 'result' is a plain dict, not an envelope."""
        d = {"status": "ok", "data": [1, 2]}
        assert safe_parse_agent_response(d) == d
