"""Tests for variant_translator (ADR-0015 改动 E)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from compass.services import variant_translator
from compass.services.bullet_decomposer import (
    BulletDecomposition,
    JDKeywordMatch,
    LengthAssessment,
    MetricStatus,
)
from compass.services.variant_translator import (
    TranslatedVariant,
    collect_preservation_tokens,
    translate_variant,
    verify_preservation,
)


# ─── collect_preservation_tokens ─────────────────────────


def test_collects_proper_nouns():
    tokens = collect_preservation_tokens(
        "Built ML pipeline at Siemens for fraud detection",
        decomposition=None,
    )
    assert "Siemens" in tokens
    # ML detected via acronym regex
    assert "ML" in tokens


def test_collects_numbers_and_units():
    tokens = collect_preservation_tokens("Reduced FPs by 23% over 6 months")
    assert any("23%" in t for t in tokens)
    assert any("6" in t for t in tokens)


def test_collects_from_decomposition_metrics():
    d = BulletDecomposition(
        metrics=[
            MetricStatus(bullet_value="23%", in_fact=True),
            MetricStatus(bullet_value="5K", in_fact=True),
        ]
    )
    tokens = collect_preservation_tokens("Reduced 23% on 5K events", decomposition=d)
    # Decomposition values are added even if regex would also have caught them
    assert "23%" in tokens
    assert "5K" in tokens


def test_collects_jd_keywords_present_in_bullet():
    d = BulletDecomposition(
        jd_keywords=[
            JDKeywordMatch(keyword="Kubernetes", in_bullet=True, in_fact=True),
            JDKeywordMatch(keyword="Java", in_bullet=False, in_fact=False),
        ]
    )
    tokens = collect_preservation_tokens(
        "Deployed services on Kubernetes clusters", decomposition=d
    )
    assert "Kubernetes" in tokens
    # Not present in bullet → should not be in preservation list
    assert "Java" not in tokens


def test_dedupes_tokens():
    """Same token mentioned in different clauses should appear once."""
    tokens = collect_preservation_tokens(
        "Siemens. Then later Siemens. Then once more Siemens."
    )
    assert tokens.count("Siemens") == 1


# ─── verify_preservation ─────────────────────────────────


def test_verifies_all_tokens_present():
    missing = verify_preservation(
        "Sviluppato pipeline ML presso Siemens, riducendo del 23%",
        preserved=["ML", "Siemens", "23%"],
    )
    assert missing == []


def test_detects_missing_tokens():
    missing = verify_preservation(
        "Sviluppato pipeline ML presso Siemens",  # missing 23%
        preserved=["ML", "Siemens", "23%"],
    )
    assert missing == ["23%"]


def test_verifies_skips_too_short_tokens():
    """Single-char tokens shouldn't false-positive."""
    missing = verify_preservation(
        "Sviluppato algoritmo di Machine Learning",
        preserved=["a", "x", "Machine Learning"],
    )
    assert missing == []  # 'a' / 'x' too short, ignored


# ─── translate_variant top-level ─────────────────────────


@dataclass
class _FakeResp:
    payload: Any

    def parse_json(self, default=None):
        if isinstance(self.payload, dict):
            return self.payload
        try:
            return json.loads(self.payload)
        except Exception:
            return default if default is not None else {}


def test_translate_to_english_is_noop():
    out = translate_variant(
        "Built ML pipeline at Siemens",
        target_language="en",
        api_key="key",
    )
    assert out.language == "en"
    assert out.text == "Built ML pipeline at Siemens"
    assert out.llm_used is False


def test_translate_empty_target_is_noop():
    out = translate_variant("Built ML pipeline", target_language="", api_key="key")
    assert out.language == "en"
    assert out.text == "Built ML pipeline"


def test_translate_without_api_key_warns_and_returns_english():
    out = translate_variant(
        "Built ML pipeline at Siemens",
        target_language="it",
        api_key="",
    )
    assert out.language == "en"  # fallback
    assert out.llm_used is False
    assert any("no api_key" in w for w in out.warnings)


def test_translate_to_italian_with_mock_llm(monkeypatch):
    monkeypatch.setattr(
        variant_translator,
        "chat",
        lambda **kw: _FakeResp(
            {"translated": "Sviluppato pipeline ML presso Siemens, riducendo del 23%"}
        ),
    )
    d = BulletDecomposition(
        metrics=[MetricStatus(bullet_value="23%", in_fact=True)],
    )
    out = translate_variant(
        "Built ML pipeline at Siemens reducing false positives by 23%",
        target_language="it",
        decomposition=d,
        provider="anthropic",
        api_key="key",
        model="m",
    )
    assert out.language == "it"
    assert "Siemens" in out.text
    assert "23%" in out.text
    assert "ML" in out.text
    assert out.llm_used is True
    # Critical preservation tokens (Siemens / ML / 23%) survived; we don't
    # assert missing == [] because LLM may legitimately translate "false positives"
    # into "falsi positivi" — that's not a preservation failure.
    for critical in ("Siemens", "ML", "23%"):
        assert critical not in out.missing


def test_translate_flags_missing_preservation_tokens(monkeypatch):
    """LLM dropped Siemens — translator detects and warns."""
    monkeypatch.setattr(
        variant_translator,
        "chat",
        lambda **kw: _FakeResp({"translated": "Sviluppato pipeline ML"}),
        # missing both Siemens and 23%
    )
    d = BulletDecomposition(
        metrics=[MetricStatus(bullet_value="23%", in_fact=True)],
    )
    out = translate_variant(
        "Built ML pipeline at Siemens 23%",
        target_language="it",
        decomposition=d,
        provider="a", api_key="key", model="m",
    )
    assert out.llm_used is True
    assert "Siemens" in out.missing
    assert "23%" in out.missing
    assert any("preservation token" in w for w in out.warnings)


def test_translate_llm_returns_empty_falls_back_to_english(monkeypatch):
    monkeypatch.setattr(
        variant_translator,
        "chat",
        lambda **kw: _FakeResp({}),  # no "translated" field
    )
    out = translate_variant(
        "Built ML pipeline",
        target_language="it",
        provider="a", api_key="key", model="m",
    )
    assert out.language == "en"
    assert out.text == "Built ML pipeline"
    assert any("failed" in w for w in out.warnings)


def test_translate_includes_preservation_tokens_in_payload(monkeypatch):
    """Check the LLM actually receives the preserve list."""
    captured = {}

    def fake_chat(*, system, user_content, **_kw):
        captured["user_content"] = user_content
        return _FakeResp({"translated": "Sviluppato pipeline ML presso Siemens"})

    monkeypatch.setattr(variant_translator, "chat", fake_chat)
    translate_variant(
        "Built ML pipeline at Siemens",
        target_language="it",
        provider="a", api_key="key", model="m",
    )
    payload = captured["user_content"]
    assert "Siemens" in payload
    assert "ML" in payload
    assert "Tokens to preserve" in payload


# ─── round-trip ──────────────────────────────────────────


def test_translated_variant_round_trip():
    tv = TranslatedVariant(
        text="Sviluppato pipeline ML presso Siemens",
        language="it",
        preserved=["ML", "Siemens"],
        missing=[],
        llm_used=True,
        warnings=[],
    )
    j = json.dumps(tv.to_jsonable(), ensure_ascii=False)
    tv2 = TranslatedVariant.from_dict(json.loads(j))
    assert tv2.text == tv.text
    assert tv2.preserved == tv.preserved
    assert tv2.llm_used is True
