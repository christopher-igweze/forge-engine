"""Tests for forge/__main__.py — port validation."""

import os
import pytest

from forge.__main__ import _validate_port


class TestValidatePort:
    """Tests for _validate_port() helper."""

    def test_valid_port(self, monkeypatch):
        monkeypatch.setenv("TEST_PORT", "8080")
        assert _validate_port("TEST_PORT", 8004) == 8080

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("TEST_PORT", raising=False)
        assert _validate_port("TEST_PORT", 8004) == 8004

    def test_invalid_string_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_PORT", "abc")
        assert _validate_port("TEST_PORT", 8004) == 8004
        captured = capsys.readouterr()
        assert "not a valid integer" in captured.err

    def test_port_zero_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_PORT", "0")
        assert _validate_port("TEST_PORT", 8004) == 8004
        captured = capsys.readouterr()
        assert "outside valid range" in captured.err

    def test_port_too_high_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_PORT", "70000")
        assert _validate_port("TEST_PORT", 8004) == 8004
        captured = capsys.readouterr()
        assert "outside valid range" in captured.err

    def test_negative_port_returns_default(self, monkeypatch, capsys):
        monkeypatch.setenv("TEST_PORT", "-1")
        assert _validate_port("TEST_PORT", 8004) == 8004
        captured = capsys.readouterr()
        assert "outside valid range" in captured.err

    def test_boundary_port_1(self, monkeypatch):
        monkeypatch.setenv("TEST_PORT", "1")
        assert _validate_port("TEST_PORT", 8004) == 1

    def test_boundary_port_65535(self, monkeypatch):
        monkeypatch.setenv("TEST_PORT", "65535")
        assert _validate_port("TEST_PORT", 8004) == 65535
