"""Tests for bullet_decomposer (ADR-0015 改动 B)."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from compass.services import bullet_decomposer
from compass.services.bullet_decomposer import (
    BulletDecomposition,
    GroundingTag,
    JDKeywordMatch,
    LengthAssessment,
    MetricStatus,
    VerbCandidate,
    assess_jd_keywords,
    assess_length,
    assess_metrics,
    decompose,
    decompose_deterministic,
)


# ─── assess_length ────────────────────────────────────────


def test_length_ok_for_typical_bullet():
    text = "Built ML pipeline at Siemens reducing false positives by 23 percent overall."
    a = assess_length(text)
    assert a.recommendation == "ok"
    assert 8 <= a.words <= 25


def test_length_too_short():
    text = "Built ML pipeline."
    a = assess_length(text)
    assert a.recommendation == "too_short"


def test_length_too_long():
    text = "Built " + " ".join(f"feature{i}" for i in range(30))
    a = assess_length(text)
    assert a.recommendation == "too_long"
    assert a.words > 25


# ─── assess_metrics ───────────────────────────────────────


def test_metric_in_fact():
    metrics = assess_metrics(
        "Reduced false positives by 23%",
        "Reduced FPs by 23% on fraud system",
    )
    assert len(metrics) == 1
    assert metrics[0].in_fact is True
    assert metrics[0].drift_kind is None


def test_metric_l5a_drift_with_auto_fix():
    metrics = assess_metrics(
        "Reduced false positives by 22%",
        "23% improvement",
    )
    assert len(metrics) == 1
    m = metrics[0]
    assert m.in_fact is False
    assert m.drift_kind == "L5a"
    assert m.auto_fix == "23%"


def test_metric_l5b_fabrication_when_no_close_match():
    metrics = assess_metrics(
        "Achieved 99.5% accuracy",
        "Built ML pipeline",  # no numbers
    )
    assert len(metrics) == 1
    assert metrics[0].drift_kind == "L5b"


def test_no_metrics_when_bullet_has_no_numbers():
    metrics = assess_metrics(
        "Built ML pipeline at Siemens",
        "Reduced FPs by 23%",
    )
    assert metrics == []


# ─── assess_jd_keywords ───────────────────────────────────


def test_jd_keyword_in_bullet_and_fact_safe():
    out = assess_jd_keywords(
        bullet_text="Built ML pipeline using Python at Siemens",
        fact_text="Built ML pipeline using Python",
        jd_keywords=["Python", "Java"],
    )
    by_kw = {x.keyword: x for x in out}
    assert by_kw["Python"].in_bullet is True
    assert by_kw["Python"].in_fact is True
    assert by_kw["Python"].needs_interview is False
    # Java in neither
    assert by_kw["Java"].in_bullet is False
    assert by_kw["Java"].in_fact is False
    assert by_kw["Java"].needs_interview is False


def test_jd_keyword_in_bullet_but_not_fact_needs_interview():
    """Critical case: bullet contains a JD keyword that isn't in the fact —
    the user should be prompted via interview."""
    out = assess_jd_keywords(
        bullet_text="Architected production-grade ML pipeline at Siemens",
        fact_text="Built ML pipeline at Siemens",  # no "production-grade"
        jd_keywords=["production-grade", "ML"],
    )
    by_kw = {x.keyword: x for x in out}
    assert by_kw["production-grade"].needs_interview is True
    # ML is in both
    assert by_kw["ML"].needs_interview is False


def test_jd_keyword_filters_empty_strings():
    out = assess_jd_keywords("Built ML", "Built ML", ["ML", "", "  "])
    assert len(out) == 1


# ─── decompose_deterministic ──────────────────────────────


def test_decompose_deterministic_returns_all_dimensions():
    d = decompose_deterministic(
        bullet_text="Built ML pipeline at Siemens reducing FPs by 23 percent",
        fact_text="Built ML pipeline at Siemens, 23% FP reduction",
        jd_keywords=["ML", "Python"],
    )
    assert isinstance(d, BulletDecomposition)
    assert d.verb_candidates == []  # no LLM
    assert d.tone_candidates == []
    assert len(d.jd_keywords) == 2
    assert d.length.recommendation in ("ok", "too_short", "too_long")
    assert d.llm_used is False


def test_decompose_deterministic_no_jd_keywords():
    """Empty keyword list yields empty jd_keywords."""
    d = decompose_deterministic("Built ML", "Built ML", [])
    assert d.jd_keywords == []


# ─── decompose top-level (LLM mocked) ─────────────────────


@dataclass
class _FakeResp:
    payload: object

    def parse_json(self, default=None):
        if isinstance(self.payload, dict):
            return self.payload
        try:
            return json.loads(self.payload)
        except Exception:
            return default if default is not None else {}


def test_decompose_without_api_key_skips_llm(monkeypatch):
    """No api_key → deterministic only, no LLM call."""
    called = [0]

    def boom(*a, **kw):
        called[0] += 1
        raise AssertionError("LLM should not be called when api_key is empty")

    monkeypatch.setattr(bullet_decomposer, "chat", boom)
    d = decompose(
        fact_text="Built ML pipeline at Siemens",
        bullet_text="Built ML pipeline at Siemens, ~10 words",
        jd_keywords=["ML"],
        api_key="",  # explicit empty
    )
    assert called[0] == 0
    assert d.llm_used is False
    assert d.verb_candidates == []


def test_decompose_with_llm_returns_candidates(monkeypatch):
    fake_payload = {
        "verb_candidates": [
            {"text": "Developed", "grounding": {"level": "safe", "rationale": "synonym"}},
            {"text": "Architected", "grounding": {"level": "blocked", "rationale": "no architect role in fact"}},
        ],
        "tone_candidates": [
            {"text": "as IC", "grounding": {"level": "safe", "rationale": "fact is individual"}},
        ],
    }
    monkeypatch.setattr(
        bullet_decomposer, "chat", lambda **kw: _FakeResp(fake_payload)
    )
    d = decompose(
        fact_text="Built ML pipeline at Siemens",
        bullet_text="Built ML pipeline at Siemens for fraud detection",
        jd_keywords=["ML"],
        provider="anthropic",
        api_key="key",
        model="claude",
    )
    assert d.llm_used is True
    assert len(d.verb_candidates) == 2
    assert d.verb_candidates[0].text == "Developed"
    assert d.verb_candidates[0].grounding.level == "safe"
    assert d.verb_candidates[1].grounding.level == "blocked"
    assert len(d.tone_candidates) == 1


def test_decompose_caps_candidate_count(monkeypatch):
    """Defensive: LLM may return too many — we cap at 6 verbs / 4 tones."""
    fake = {
        "verb_candidates": [
            {"text": f"v{i}", "grounding": {"level": "check", "rationale": "x"}}
            for i in range(20)
        ],
        "tone_candidates": [
            {"text": f"t{i}", "grounding": {"level": "check", "rationale": "x"}}
            for i in range(20)
        ],
    }
    monkeypatch.setattr(bullet_decomposer, "chat", lambda **kw: _FakeResp(fake))
    d = decompose(
        fact_text="x", bullet_text="x x", jd_keywords=[],
        provider="a", api_key="key", model="m",
    )
    assert len(d.verb_candidates) <= 6
    assert len(d.tone_candidates) <= 4


def test_decompose_llm_failure_falls_back_to_deterministic(monkeypatch):
    """LLM throws → still get deterministic result."""
    from compass.services.llm import LLMConfigError

    def boom(**kw):
        raise LLMConfigError("no key")

    monkeypatch.setattr(bullet_decomposer, "chat", boom)
    d = decompose(
        fact_text="Built ML pipeline at Siemens 23%",
        bullet_text="Built ML pipeline at Siemens 23%",
        jd_keywords=["ML"],
        api_key="present_but_chat_fails",
    )
    # LLM didn't produce — but deterministic dims still populated
    assert d.llm_used is False
    assert len(d.metrics) == 1
    assert d.metrics[0].in_fact is True


# ─── round-trip serialization ─────────────────────────────


def test_decomposition_round_trip_through_json():
    """Lock the schema so FactVariant.decomposition_json reads/writes work."""
    d1 = BulletDecomposition(
        verb_candidates=[
            VerbCandidate(
                text="Developed",
                grounding=GroundingTag(level="safe", rationale="synonym"),
                source="llm",
            )
        ],
        metrics=[
            MetricStatus(bullet_value="22%", in_fact=False, drift_kind="L5a", auto_fix="23%")
        ],
        jd_keywords=[
            JDKeywordMatch(keyword="ML", in_bullet=True, in_fact=True),
            JDKeywordMatch(
                keyword="production-grade",
                in_bullet=True,
                in_fact=False,
                needs_interview=True,
            ),
        ],
        length=LengthAssessment(words=12, recommendation="ok"),
        tone_candidates=[],
        llm_used=True,
    )
    j = d1.to_jsonable()
    # Survive JSON round-trip
    j_str = json.dumps(j)
    d2 = BulletDecomposition.from_dict(json.loads(j_str))
    assert d2.verb_candidates[0].text == "Developed"
    assert d2.verb_candidates[0].grounding.level == "safe"
    assert d2.metrics[0].drift_kind == "L5a"
    assert d2.metrics[0].auto_fix == "23%"
    assert d2.jd_keywords[1].needs_interview is True
    assert d2.length.recommendation == "ok"
    assert d2.llm_used is True


def test_decomposition_from_empty_dict_returns_default():
    d = BulletDecomposition.from_dict({})
    assert d.verb_candidates == []
    assert d.length.recommendation == "ok"
