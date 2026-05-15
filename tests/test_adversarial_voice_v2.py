"""Tests for the ADR-0015 two-phase adversarial voice.

We don't hit a live LLM here — instead we monkeypatch `chat()` to
return scripted responses and verify:

  · Phase 1 actually runs first with fact + JD only (no variant_text in payload)
  · Phase 2 receives expected_objections from Phase 1
  · No-key path returns None gracefully
  · Empty expected_objections short-circuits (no Phase 2 call)
  · Phase 2 wording-only result surfaces as an objection string
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from compass.models import Job, ResumeFact
from compass.services import adversarial_voice


@dataclass
class _FakeResp:
    """Stand-in for the chat() response object — only needs parse_json."""

    payload: Any

    def parse_json(self, default=None):
        if isinstance(self.payload, dict):
            return self.payload
        try:
            return json.loads(self.payload)
        except Exception:
            return default if default is not None else {}


def _make_chat(scripted: list[Any]):
    """Build a chat() replacement that returns scripted responses in order."""
    calls: list[dict[str, Any]] = []
    iterator = iter(scripted)

    def fake_chat(*, system, user_content, **_kw):
        calls.append({"system": system, "user_content": user_content})
        try:
            return _FakeResp(next(iterator))
        except StopIteration:
            return _FakeResp({})

    return fake_chat, calls


def _fact_and_job():
    fact = ResumeFact(
        id="f1",
        identity_id="i1",
        kind="experience",
        text="Built ML pipeline at Siemens for fraud detection",
    )
    job = Job(
        id="j1",
        title="Senior ML Engineer",
        company="ACME",
        link="https://example.com/j1",
        description="We need 5+ years of production ML experience and team leadership.",
    )
    return fact, job


def test_no_api_key_returns_none():
    fact, job = _fact_and_job()
    out = adversarial_voice.raise_objection(
        fact=fact,
        job=job,
        variant_text="Architected end-to-end ML pipeline ...",
        provider="anthropic",
        api_key="",
        model="claude-3-5-sonnet",
    )
    assert out is None


def test_phase1_no_objections_short_circuits(monkeypatch):
    """If Phase 1 says 'no objections', Phase 2 is never called."""
    fact, job = _fact_and_job()
    fake_chat, calls = _make_chat([{"expected_objections": []}])
    monkeypatch.setattr(adversarial_voice, "chat", fake_chat)

    out = adversarial_voice.raise_objection(
        fact=fact,
        job=job,
        variant_text="Anything — should not be evaluated",
        provider="anthropic",
        api_key="key",
        model="m",
    )
    assert out is None
    assert len(calls) == 1, "Phase 2 should not run when Phase 1 finds nothing"


def test_phase1_does_not_see_variant_text(monkeypatch):
    """Critical ADR-0015 invariant: Phase 1 must NOT see the polished variant."""
    fact, job = _fact_and_job()
    fake_chat, calls = _make_chat(
        [
            {"expected_objections": ["solo project, no team scope demonstrated"]},
            {
                "objection": "rewrite uses 'led' but fact says 'built' — wording only",
                "addressed_by_facts": [],
                "addressed_by_wording": [
                    "solo project, no team scope demonstrated"
                ],
            },
        ]
    )
    monkeypatch.setattr(adversarial_voice, "chat", fake_chat)

    polished = "Architected and led end-to-end ML platform deployment"
    adversarial_voice.raise_objection(
        fact=fact,
        job=job,
        variant_text=polished,
        provider="anthropic",
        api_key="key",
        model="m",
    )
    # Phase 1 payload (calls[0]) must not contain the polished variant
    assert polished not in calls[0]["user_content"], (
        "Phase 1 leaked variant_text — adversarial loses fact-anchored independence"
    )
    # Phase 2 (calls[1]) DOES see it (that's the point)
    assert polished in calls[1]["user_content"]


def test_wording_only_address_returns_objection(monkeypatch):
    fact, job = _fact_and_job()
    fake_chat, _calls = _make_chat(
        [
            {
                "expected_objections": [
                    "personal/research scope; JD wants production team work"
                ]
            },
            {
                "objection": "rewrite swaps 'built' for 'architected' without scope evidence",
                "addressed_by_facts": [],
                "addressed_by_wording": [
                    "personal/research scope; JD wants production team work"
                ],
            },
        ]
    )
    monkeypatch.setattr(adversarial_voice, "chat", fake_chat)

    out = adversarial_voice.raise_objection(
        fact=fact,
        job=job,
        variant_text="Architected end-to-end production ML pipeline",
        provider="anthropic",
        api_key="key",
        model="m",
    )
    assert out is not None
    assert "scope" in out.lower() or "wording" in out.lower() or "architected" in out.lower()


def test_addressed_by_facts_returns_none(monkeypatch):
    """Phase 2 explicit null → no objection surfaces."""
    fact, job = _fact_and_job()
    fake_chat, _ = _make_chat(
        [
            {"expected_objections": ["scope unclear"]},
            {
                "objection": None,
                "addressed_by_facts": ["scope unclear"],
                "addressed_by_wording": [],
            },
        ]
    )
    monkeypatch.setattr(adversarial_voice, "chat", fake_chat)

    out = adversarial_voice.raise_objection(
        fact=fact,
        job=job,
        variant_text="Built ML pipeline serving 10M daily fraud events at Siemens",
        provider="anthropic",
        api_key="key",
        model="m",
    )
    assert out is None


def test_phase1_payload_contains_fact_and_jd(monkeypatch):
    """Sanity: Phase 1 sees fact text and JD title/desc."""
    fact, job = _fact_and_job()
    fake_chat, calls = _make_chat([{"expected_objections": []}])
    monkeypatch.setattr(adversarial_voice, "chat", fake_chat)

    adversarial_voice.raise_objection(
        fact=fact,
        job=job,
        variant_text="...",
        provider="anthropic",
        api_key="key",
        model="m",
    )
    payload = calls[0]["user_content"]
    assert fact.text in payload
    assert job.title in payload
    assert "fraud detection" in payload  # from fact
    assert "production ML" in payload  # from JD desc
