"""Tests for AARS factor auto-detection."""
import tempfile
from pathlib import Path

from forge.evaluation.aivss_detector import detect_aars_factors


def _make_repo(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


class TestDetectAARSFactors:
    def test_empty_repo(self):
        """Empty repo produces all-zero factors."""
        d = tempfile.mkdtemp()
        factors = detect_aars_factors(d)
        assert all(v == 0.0 for v in factors.values())

    def test_detects_tool_control(self):
        """Files with tool registrations score tool_control_surface."""
        d = _make_repo({"app.py": "@tool\ndef my_tool(): pass\n@tool\ndef another(): pass\n"})
        factors = detect_aars_factors(d)
        assert factors["tool_control_surface"] >= 0.5

    def test_detects_llm_non_determinism(self):
        """Files with LLM calls score behavioral_non_determinism."""
        d = _make_repo({
            "app.py": (
                "from openai import OpenAI\n"
                "client = OpenAI()\n"
                "result = client.chat.completions.create(temperature=0.9)\n"
            ),
        })
        factors = detect_aars_factors(d)
        assert factors["behavioral_non_determinism"] >= 0.5

    def test_detects_multi_agent(self):
        """Files with agent patterns score multi_agent_interactions."""
        d = _make_repo({"app.py": "from autogen import Agent\nswarm = MultiAgentOrchestrator()\n"})
        factors = detect_aars_factors(d)
        assert factors["multi_agent_interactions"] >= 0.5

    def test_detects_persistent_state(self):
        """Files with database patterns score persistent_state."""
        d = _make_repo({"app.py": "import sqlite3\nconn = sqlite3.connect('db.sqlite')\n"})
        factors = detect_aars_factors(d)
        assert factors["persistent_state"] >= 0.5

    def test_non_agentic_codebase(self):
        """Simple web app with no agent patterns gets low scores."""
        d = _make_repo({
            "app.py": "from flask import Flask\napp = Flask(__name__)\n@app.route('/')\ndef index(): return 'hello'\n",
        })
        factors = detect_aars_factors(d)
        agent_factors = [
            "execution_autonomy", "tool_control_surface", "natural_language_interface",
            "behavioral_non_determinism", "multi_agent_interactions", "self_modification",
        ]
        assert sum(factors[f] for f in agent_factors) <= 1.0

    def test_returns_all_10_factors(self):
        """Always returns all 10 factor keys."""
        d = tempfile.mkdtemp()
        factors = detect_aars_factors(d)
        assert len(factors) == 10

    def test_deterministic(self):
        """Two runs on same code produce identical results."""
        d = _make_repo({"app.py": "import openai\nfrom anthropic import Anthropic\n"})
        f1 = detect_aars_factors(d)
        f2 = detect_aars_factors(d)
        assert f1 == f2
