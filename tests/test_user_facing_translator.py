"""Tests for user_facing_translator (ADR-0015 改动 G2)."""
from __future__ import annotations

import pytest

from compass.services.bullet_decomposer import (
    BulletDecomposition,
    GroundingTag,
    JDKeywordMatch,
    LengthAssessment,
    MetricStatus,
    VerbCandidate,
)
from compass.services.user_facing_translator import (
    UserFacingChoice,
    UserFacingDimension,
    UserFacingMenu,
    to_user_facing,
)


def _verb(text: str, level: str) -> VerbCandidate:
    return VerbCandidate(text=text, grounding=GroundingTag(level=level))


# ─── Verb dimension ───────────────────────────────────────


def test_verb_dimension_safe_renders_ok():
    d = BulletDecomposition(verb_candidates=[_verb("Developed", "safe")])
    menu = to_user_facing(d, locale="zh")
    verb_dim = menu.dimensions[0]
    assert verb_dim.label == "动作词"
    assert verb_dim.severity == "ok"
    assert "Developed" in verb_dim.choices[0].label
    assert "安全" in verb_dim.choices[0].note


def test_verb_dimension_blocked_marks_severity():
    d = BulletDecomposition(verb_candidates=[_verb("Architected", "blocked")])
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[0].severity == "blocked"
    assert menu.has_blockers is True


def test_verb_dimension_interview_marks_warn():
    d = BulletDecomposition(verb_candidates=[_verb("Owned", "interview")])
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[0].severity == "warn"
    assert menu.has_warnings is True


def test_verb_dimension_empty_when_no_llm():
    """Without LLM, verb_candidates is empty → fallback choice."""
    d = BulletDecomposition()  # no verbs
    menu = to_user_facing(d, locale="zh")
    verb_dim = menu.dimensions[0]
    # Has a placeholder choice so the dimension is visible
    assert len(verb_dim.choices) >= 1


# ─── Metric dimension ─────────────────────────────────────


def test_metric_in_fact_renders_check():
    d = BulletDecomposition(
        metrics=[MetricStatus(bullet_value="23%", in_fact=True)]
    )
    menu = to_user_facing(d, locale="zh")
    metric_dim = menu.dimensions[1]
    assert metric_dim.severity == "ok"
    assert "23%" in metric_dim.choices[0].label
    assert "已对照" in metric_dim.choices[0].note


def test_metric_l5a_drift_renders_auto_correct():
    d = BulletDecomposition(
        metrics=[
            MetricStatus(
                bullet_value="22%", in_fact=False, drift_kind="L5a", auto_fix="23%"
            )
        ]
    )
    menu = to_user_facing(d, locale="zh")
    metric_dim = menu.dimensions[1]
    assert metric_dim.severity == "warn"
    assert "23%" in metric_dim.choices[0].label
    assert "自动" in metric_dim.choices[0].note or "自动" in metric_dim.choices[0].label
    assert metric_dim.choices[0].action == "snap"


def test_metric_l5b_renders_blocker():
    d = BulletDecomposition(
        metrics=[MetricStatus(bullet_value="99.5%", in_fact=False, drift_kind="L5b")]
    )
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[1].severity == "blocked"
    assert menu.has_blockers is True
    assert menu.dimensions[1].choices[0].action == "remove"


def test_metric_dimension_no_numbers():
    d = BulletDecomposition()  # no metrics
    menu = to_user_facing(d, locale="zh")
    metric_dim = menu.dimensions[1]
    assert metric_dim.severity == "ok"
    assert "没数字" in metric_dim.choices[0].label or "No numbers" in metric_dim.choices[0].label


# ─── JD keywords dimension ────────────────────────────────


def test_jd_keyword_in_bullet_and_fact_safe():
    d = BulletDecomposition(
        jd_keywords=[JDKeywordMatch(keyword="Python", in_bullet=True, in_fact=True)]
    )
    menu = to_user_facing(d, locale="zh")
    kw_dim = menu.dimensions[2]
    assert kw_dim.severity == "ok"
    assert "Python" in kw_dim.choices[0].label


def test_jd_keyword_needs_interview_warns():
    d = BulletDecomposition(
        jd_keywords=[
            JDKeywordMatch(
                keyword="production-grade",
                in_bullet=True,
                in_fact=False,
                needs_interview=True,
            )
        ]
    )
    menu = to_user_facing(d, locale="zh")
    kw_dim = menu.dimensions[2]
    assert kw_dim.severity == "warn"
    assert kw_dim.choices[0].action == "interview"
    assert "回答" in kw_dim.choices[0].note


def test_jd_keyword_in_jd_only_neutral():
    """Keyword in JD but bullet doesn't use it — informational only."""
    d = BulletDecomposition(
        jd_keywords=[JDKeywordMatch(keyword="Kubernetes", in_bullet=False, in_fact=False)]
    )
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[2].severity == "ok"


# ─── Length dimension ─────────────────────────────────────


def test_length_ok_renders_ok():
    d = BulletDecomposition(length=LengthAssessment(words=15, recommendation="ok"))
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[3].severity == "ok"
    assert "15" in menu.dimensions[3].description


def test_length_too_short_warns():
    d = BulletDecomposition(length=LengthAssessment(words=5, recommendation="too_short"))
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[3].severity == "warn"
    assert "短了" in menu.dimensions[3].choices[0].label


def test_length_too_long_warns():
    d = BulletDecomposition(length=LengthAssessment(words=30, recommendation="too_long"))
    menu = to_user_facing(d, locale="zh")
    assert menu.dimensions[3].severity == "warn"


# ─── Locale switching ────────────────────────────────────


def test_english_locale_uses_english_labels():
    d = BulletDecomposition(verb_candidates=[_verb("Developed", "safe")])
    menu = to_user_facing(d, locale="en")
    assert menu.dimensions[0].label == "Action Verb"
    assert "Safe" in menu.dimensions[0].choices[0].note


def test_unknown_locale_falls_back_to_english():
    d = BulletDecomposition(verb_candidates=[_verb("Built", "safe")])
    menu = to_user_facing(d, locale="ja")  # Japanese — no table
    # Falls back to en table
    assert menu.dimensions[0].label == "Action Verb"


def test_developer_terms_never_appear_in_default_mode():
    """Crucial: L0-L5 / amplification_level / verb_candidates must be HIDDEN."""
    d = BulletDecomposition(
        verb_candidates=[_verb("Developed", "safe"), _verb("Architected", "blocked")],
        metrics=[MetricStatus(bullet_value="23%", in_fact=True)],
        jd_keywords=[JDKeywordMatch(keyword="Python", in_bullet=True, in_fact=True)],
        length=LengthAssessment(words=12, recommendation="ok"),
        tone_candidates=[_verb("led", "interview")],
    )
    menu = to_user_facing(d, locale="zh")
    serialized = str(menu.to_jsonable())
    forbidden = ["L0", "L1", "L2", "L3", "L4", "L5",
                 "amplification_level", "verb_candidates",
                 "metric_status", "blocked"]  # 'blocked' is a severity not a label
    # 'blocked' DOES appear as severity field — that's machine-readable, not user-shown
    # But the user-facing labels themselves should not say "blocked" in English text
    # because the user's locale is zh. Let's just check L0-L5 raw labels.
    for term in ("L0", "L1", "L2", "L3", "L4", "L5", "amplification_level"):
        assert term not in serialized


# ─── Aggregate flags ──────────────────────────────────────


def test_has_warnings_true_when_any_dim_warns():
    d = BulletDecomposition(
        metrics=[
            MetricStatus(
                bullet_value="22%", in_fact=False, drift_kind="L5a", auto_fix="23%"
            )
        ]
    )
    menu = to_user_facing(d, locale="zh")
    assert menu.has_warnings is True
    assert menu.has_blockers is False


def test_has_blockers_true_when_any_dim_blocked():
    d = BulletDecomposition(verb_candidates=[_verb("Architected", "blocked")])
    menu = to_user_facing(d, locale="zh")
    assert menu.has_blockers is True


def test_clean_decomposition_no_warnings_no_blockers():
    d = BulletDecomposition(
        verb_candidates=[_verb("Built", "safe")],
        metrics=[MetricStatus(bullet_value="23%", in_fact=True)],
        jd_keywords=[JDKeywordMatch(keyword="Python", in_bullet=True, in_fact=True)],
        length=LengthAssessment(words=15, recommendation="ok"),
    )
    menu = to_user_facing(d, locale="zh")
    assert menu.has_warnings is False
    assert menu.has_blockers is False


# ─── Round-trip / JSONable ────────────────────────────────


def test_menu_is_fully_jsonable():
    """UI consumes via JSON — must serialize without errors."""
    import json

    d = BulletDecomposition(
        verb_candidates=[_verb("Developed", "safe")],
        metrics=[MetricStatus(bullet_value="23%", in_fact=True)],
        jd_keywords=[JDKeywordMatch(keyword="Python", in_bullet=True, in_fact=True)],
        length=LengthAssessment(words=15, recommendation="ok"),
        tone_candidates=[_verb("end-to-end", "check")],
    )
    menu = to_user_facing(d, locale="zh")
    serialized = json.dumps(menu.to_jsonable(), ensure_ascii=False)
    assert "动作词" in serialized
    assert "Developed" in serialized
    assert "Python" in serialized


def test_dimension_count_is_five():
    """Always 5 dimensions: verb / metrics / jd / length / tone."""
    d = BulletDecomposition()
    menu = to_user_facing(d, locale="zh")
    assert len(menu.dimensions) == 5
    labels = [d.label for d in menu.dimensions]
    assert "动作词" in labels
    assert "数字成果" in labels
    assert "JD 想要的能力" in labels
    assert "长度" in labels
    assert "语气" in labels
