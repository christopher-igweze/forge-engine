"""Tests that FORGE Opengrep rules are valid and loadable."""

from pathlib import Path

import yaml

RULES_DIR = Path(__file__).parent.parent.parent / "forge" / "rules"


class TestRulesValid:
    def test_all_yaml_files_parse(self):
        for yml in RULES_DIR.rglob("*.yml"):
            data = yaml.safe_load(yml.read_text())
            assert "rules" in data, f"{yml.name} missing 'rules' key"

    def test_all_rules_have_required_fields(self):
        for yml in RULES_DIR.rglob("*.yml"):
            data = yaml.safe_load(yml.read_text())
            for rule in data.get("rules", []):
                assert "id" in rule, f"Rule in {yml.name} missing 'id'"
                assert "message" in rule, f"Rule {rule.get('id')} missing 'message'"
                assert "severity" in rule, f"Rule {rule.get('id')} missing 'severity'"

    def test_all_rules_have_metadata(self):
        for yml in RULES_DIR.rglob("*.yml"):
            data = yaml.safe_load(yml.read_text())
            for rule in data.get("rules", []):
                meta = rule.get("metadata", {})
                assert "category" in meta, f"Rule {rule['id']} missing metadata.category"
                assert (
                    "forge-check-id" in meta
                ), f"Rule {rule['id']} missing metadata.forge-check-id"

    def test_minimum_rule_count(self):
        count = 0
        for yml in RULES_DIR.rglob("*.yml"):
            data = yaml.safe_load(yml.read_text())
            count += len(data.get("rules", []))
        assert count >= 30, f"Expected 30+ rules, got {count}"

    def test_security_rules_have_cwe(self):
        sec_dir = RULES_DIR / "security"
        for yml in sec_dir.rglob("*.yml"):
            data = yaml.safe_load(yml.read_text())
            for rule in data.get("rules", []):
                meta = rule.get("metadata", {})
                assert "cwe" in meta, f"Security rule {rule['id']} missing metadata.cwe"
