"""Tests for AIVSS scoring calculator."""
from forge.evaluation.aivss import (
    AIVSSInput,
    AIVSSResult,
    calculate_aivss,
    build_aivss_input,
    format_aivss_report,
    _severity_from_score,
    AARS_FACTORS,
)


class TestSeverityBands:
    def test_critical(self):
        assert _severity_from_score(9.5) == "Critical"

    def test_high(self):
        assert _severity_from_score(7.5) == "High"

    def test_medium(self):
        assert _severity_from_score(5.0) == "Medium"

    def test_low(self):
        assert _severity_from_score(2.0) == "Low"

    def test_none(self):
        assert _severity_from_score(0.0) == "None"


class TestCalculateAIVSS:
    def test_default_input_produces_valid_score(self):
        result = calculate_aivss(AIVSSInput())
        assert 0.0 <= result.score <= 10.0
        assert result.severity in ("None", "Low", "Medium", "High", "Critical")

    def test_all_zero_aars_non_agentic(self):
        """Non-agentic codebase: all AARS = 0, score reflects only base + impact."""
        inp = AIVSSInput()  # defaults: all AARS = 0.0
        result = calculate_aivss(inp)
        assert result.aars_score == 0.0
        assert result.score > 0  # Still has base + impact

    def test_max_aars_high_score(self):
        """All AARS at 1.0 with high impact should produce high score."""
        inp = AIVSSInput(
            execution_autonomy=1.0,
            tool_control_surface=1.0,
            natural_language_interface=1.0,
            contextual_awareness=1.0,
            behavioral_non_determinism=1.0,
            opacity_reflexivity=1.0,
            persistent_state=1.0,
            dynamic_identity=1.0,
            multi_agent_interactions=1.0,
            self_modification=1.0,
            confidentiality_impact=1.0,
            integrity_impact=1.0,
            availability_impact=1.0,
            safety_impact=1.0,
        )
        result = calculate_aivss(inp)
        assert result.aars_score == 10.0
        assert result.score >= 6.5  # Should be high risk (base metrics cap the total)

    def test_simple_formula(self):
        """Simple formula: ((base + aars) / 2) * threat_multiplier."""
        inp = AIVSSInput(execution_autonomy=1.0, tool_control_surface=1.0)
        result = calculate_aivss(inp, formula="simple")
        assert result.formula_used == "simple"
        assert 0.0 <= result.score <= 10.0

    def test_weighted_formula(self):
        """Weighted formula is the default."""
        result = calculate_aivss(AIVSSInput())
        assert result.formula_used == "weighted"

    def test_factor_breakdown_present(self):
        """Result includes breakdown for all 10 AARS factors."""
        result = calculate_aivss(AIVSSInput())
        assert len(result.factor_breakdown) == 10
        for factor in AARS_FACTORS:
            assert factor in result.factor_breakdown
            assert "value" in result.factor_breakdown[factor]
            assert "label" in result.factor_breakdown[factor]

    def test_score_clamped_to_10(self):
        """Score never exceeds 10.0."""
        inp = AIVSSInput(
            attack_vector=0.85,
            attack_complexity=0.77,
            privileges_required=0.85,
            user_interaction=0.85,
            scope=1.50,
            execution_autonomy=1.0,
            tool_control_surface=1.0,
            natural_language_interface=1.0,
            contextual_awareness=1.0,
            behavioral_non_determinism=1.0,
            opacity_reflexivity=1.0,
            persistent_state=1.0,
            dynamic_identity=1.0,
            multi_agent_interactions=1.0,
            self_modification=1.0,
            confidentiality_impact=1.0,
            integrity_impact=1.0,
            availability_impact=1.0,
            safety_impact=1.0,
        )
        result = calculate_aivss(inp)
        assert result.score <= 10.0


class TestBuildInput:
    def test_from_empty_factors(self):
        """Empty factors produce valid input."""
        inp = build_aivss_input({})
        assert isinstance(inp, AIVSSInput)

    def test_from_detected_factors(self):
        """Detected factors are applied."""
        inp = build_aivss_input({"execution_autonomy": 1.0, "tool_control_surface": 0.5})
        assert inp.execution_autonomy == 1.0
        assert inp.tool_control_surface == 0.5


class TestFormatReport:
    def test_format_produces_string(self):
        result = calculate_aivss(AIVSSInput())
        output = format_aivss_report(result)
        assert "AIVSS Score:" in output
        assert "AARS Factors:" in output
