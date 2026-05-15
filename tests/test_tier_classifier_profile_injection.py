"""Tests for ADR-0016 profile injection into tier_classifier._build_user_payload.

Without profile context, the classifier rates jobs against the user's full
resume — leading to "啥岗位都有". With profile, it has a narrower lens.
These tests lock the prompt-construction behavior; the actual LLM scoring is
covered by tier_classifier's existing tests.
"""
from __future__ import annotations

import inspect

import pytest

from compass.models import Job, ResumeFact
from compass.services.scrape_config import (
    Dealbreakers,
    ProfileConfig,
)
from compass.services.tier_classifier import (
    _build_user_payload,
    classify_job,
)


def _job() -> Job:
    return Job(
        id="j1",
        title="ML Engineer",
        company="ACME",
        link="https://example.com/j1",
        location="Berlin, Germany",
        description="ML engineering role.",
    )


def _fact() -> ResumeFact:
    return ResumeFact(
        id="f1", identity_id="i1", kind="experience",
        text="Built ML pipeline at Siemens",
    )


def _profile(**kw) -> ProfileConfig:
    defaults = dict(
        id="de-electrical",
        label="德国-电气核心",
        city="Berlin", country="Germany",
        keywords=["Power Engineer", "Electrical Engineer"],
        user_prompt="电气工程方向，西门子两年经验后转型，找产品工程或现场工程。",
        dealbreakers=Dealbreakers(country_must_be=["Germany"]),
    )
    defaults.update(kw)
    return ProfileConfig(**defaults)


# ─── Profile injection ─────────────────────────────────────


def test_payload_without_profile_has_no_profile_block():
    """Backward-compat: pre-ADR-0016 callers see no behavior change."""
    payload = _build_user_payload(_job(), None, [_fact()], None, "")
    assert "Currently active job-search profile" not in payload
    assert "Frame" not in payload  # the ADR-0016 frame note


def test_payload_with_profile_includes_label_and_user_prompt():
    p = _profile()
    payload = _build_user_payload(_job(), None, [_fact()], None, "", profile=p)
    assert "德国-电气核心" in payload
    assert "西门子两年经验" in payload
    assert "Active keywords" in payload
    assert "Power Engineer" in payload


def test_payload_with_profile_lists_country_constraint():
    p = _profile()
    payload = _build_user_payload(_job(), None, [], None, "", profile=p)
    assert "Country constraint" in payload
    assert "Germany" in payload


def test_payload_with_profile_includes_adr0016_frame():
    """The crucial instruction telling LLM to judge by profile not by full resume."""
    p = _profile()
    payload = _build_user_payload(_job(), None, [], None, "", profile=p)
    assert "Frame" in payload
    # The instruction must specifically warn about resume-diversity overreach
    assert "OUTSIDE this profile" in payload or "outside this profile" in payload.lower()


def test_payload_profile_block_comes_first():
    """Profile context must appear BEFORE facts so the LLM uses it as the frame
    when reading everything else."""
    p = _profile()
    payload = _build_user_payload(_job(), None, [_fact()], None, "global doc", profile=p)
    profile_idx = payload.find("Currently active job-search profile")
    facts_idx = payload.find("User facts (current identity)")
    job_idx = payload.find("Job posting")
    assert profile_idx >= 0
    assert facts_idx >= 0
    assert job_idx >= 0
    assert profile_idx < facts_idx < job_idx


def test_payload_profile_with_empty_user_prompt_still_injects_keywords():
    """Even if user hasn't filled user_prompt, keywords + country still frame the LLM."""
    p = _profile(user_prompt="")
    payload = _build_user_payload(_job(), None, [], None, "", profile=p)
    assert "德国-电气核心" in payload
    assert "Active keywords" in payload
    assert "stated direction for THIS profile" not in payload  # no user_prompt → not added


def test_payload_truncates_long_user_prompt():
    """Defensive: extremely long user_prompt should not blow context."""
    p = _profile(user_prompt="x" * 5000)
    payload = _build_user_payload(_job(), None, [], None, "", profile=p)
    # The prompt block should cap at ~600 chars
    block_start = payload.find("stated direction for THIS profile")
    assert block_start >= 0
    # Quick sanity: the entire payload shouldn't be 5000+ chars from this alone
    assert payload.count("x") < 700


# ─── classify_job signature locks ──────────────────────────


def test_classify_job_accepts_profile_kwarg():
    params = inspect.signature(classify_job).parameters
    assert "profile" in params
    assert params["profile"].default is None  # backward-compat default


def test_payload_keywords_truncate_at_20():
    """Pathological case: 50 keywords should not all be sent."""
    p = _profile(keywords=[f"kw{i}" for i in range(50)])
    payload = _build_user_payload(_job(), None, [], None, "", profile=p)
    assert "kw0" in payload
    assert "kw19" in payload  # within first 20
    assert "kw49" not in payload  # truncated


def test_payload_with_all_inputs_combined():
    """Smoke test: profile + global target_doc + facts + calibration all present."""
    from compass.models import UserCalibration

    cal = UserCalibration(
        id=1,
        sensitive_categories_locked=["language_level", "education_credential"],
    )
    p = _profile()
    payload = _build_user_payload(
        _job(), None, [_fact()], cal, "global direction text", profile=p
    )
    # All sections present
    assert "Currently active job-search profile" in payload
    assert "User's stated direction (global)" in payload
    assert "User facts" in payload
    assert "Sensitive categories" in payload
    assert "Job posting" in payload
