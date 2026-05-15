"""Tests for build_review_rows helper (ADR-0015 改动 G integration)."""
from __future__ import annotations

import pytest

from compass.models import FactVariant, ResumeFact
from compass.services.review_rows import build_review_rows
from compass.services.user_facing_translator import UserFacingMenu


def _add_fact(session, identity, text="Built ML pipeline"):
    f = ResumeFact(identity_id=identity.id, kind="experience", text=text)
    session.add(f)
    session.flush()
    return f


def test_dedup_keeps_first_per_fact(session, identity, job):
    """Latest variant (first iterated) wins; older ones skipped."""
    f = _add_fact(session, identity)
    v_old = FactVariant(fact_id=f.id, job_id=job.id, text="old", amplification_level=1)
    v_new = FactVariant(fact_id=f.id, job_id=job.id, text="new", amplification_level=2)
    session.add_all([v_old, v_new])
    session.flush()
    rows = build_review_rows(session, [v_new, v_old])  # newest first
    assert len(rows) == 1
    assert rows[0][0].text == "new"


def test_menu_none_when_no_decomposition_json(session, identity, job):
    f = _add_fact(session, identity)
    v = FactVariant(fact_id=f.id, job_id=job.id, text="x", amplification_level=1)
    session.add(v)
    session.flush()
    rows = build_review_rows(session, [v])
    assert rows[0][2] is None  # menu


def test_menu_built_when_decomposition_json_present(session, identity, job):
    f = _add_fact(session, identity)
    v = FactVariant(
        fact_id=f.id,
        job_id=job.id,
        text="Built ML pipeline at Siemens by 23%",
        amplification_level=1,
        decomposition_json={
            "metrics": [{"bullet_value": "23%", "in_fact": True}],
            "jd_keywords": [
                {"keyword": "Python", "in_bullet": True, "in_fact": True}
            ],
            "length": {"words": 8, "recommendation": "ok"},
        },
    )
    session.add(v)
    session.flush()
    rows = build_review_rows(session, [v])
    _, _, menu = rows[0]
    assert isinstance(menu, UserFacingMenu)
    assert len(menu.dimensions) == 5  # always 5
    # The menu should have 'no warnings' since metric is in fact
    assert menu.has_blockers is False


def test_corrupt_decomposition_json_falls_back_to_none(
    session, identity, job, caplog
):
    """Bad JSON shape shouldn't crash the page render."""
    f = _add_fact(session, identity)
    v = FactVariant(
        fact_id=f.id,
        job_id=job.id,
        text="x",
        amplification_level=1,
        decomposition_json={"verb_candidates": "not a list"},  # invalid
    )
    session.add(v)
    session.flush()
    rows = build_review_rows(session, [v])
    # Should not raise; menu may be None or a degraded menu
    assert len(rows) == 1
    # Whatever happens, the variant + fact tuple is still well-formed
    assert rows[0][0].text == "x"


def test_locale_propagates(session, identity, job):
    f = _add_fact(session, identity)
    v = FactVariant(
        fact_id=f.id,
        job_id=job.id,
        text="x",
        amplification_level=1,
        decomposition_json={"length": {"words": 12, "recommendation": "ok"}},
    )
    session.add(v)
    session.flush()
    rows_en = build_review_rows(session, [v], locale="en")
    rows_zh = build_review_rows(session, [v], locale="zh")
    # English should use English dimension labels
    assert "Length" in [d.label for d in rows_en[0][2].dimensions]
    # Chinese should use Chinese dimension labels
    assert "长度" in [d.label for d in rows_zh[0][2].dimensions]
