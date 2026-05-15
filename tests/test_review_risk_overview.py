"""Risk overview chips on resume review page.

Verifies the L0–L5 inline 高亮 UI:
  - 每个 level 一个 chip(只显示 count > 0 的)
  - L3+ pending 计数显示警告
  - sug-card 左边框颜色按 level 区分
"""
from __future__ import annotations

import pytest

from compass.app import create_app
from compass.extensions import session_scope
from compass.models import (
    FactVariant,
    IdentityVersion,
    Job,
    ResumeFact,
    RewriteAttempt,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/t.sqlite")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_attempt(*, levels: list[int], decisions: list[str] | None = None) -> str:
    """Create a job + identity + facts + variants at given levels.
    Returns attempt_id for /resume/review/<id>."""
    decisions = decisions or ["pending"] * len(levels)
    assert len(decisions) == len(levels)

    with session_scope() as s:
        iv = IdentityVersion(label="t", is_current=True)
        s.add(iv)
        s.flush()

        job = Job(title="X", company="C", link="https://e.com/1", description="JD")
        s.add(job)
        s.flush()

        att = RewriteAttempt(job_id=job.id, identity_id=iv.id)
        s.add(att)
        s.flush()

        for i, (lvl, dec) in enumerate(zip(levels, decisions)):
            fact = ResumeFact(identity_id=iv.id, kind="experience", text=f"raw bullet {i}")
            s.add(fact)
            s.flush()
            v = FactVariant(
                fact_id=fact.id,
                job_id=job.id,
                text=f"rewritten bullet {i}",
                amplification_level=lvl,
                user_decision=dec,
            )
            s.add(v)
        s.flush()
        return att.id


def test_risk_overview_renders_only_present_levels(client):
    aid = _seed_attempt(levels=[0, 0, 1, 4])
    resp = client.get(f"/resume/review/{aid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "风险概览" in body
    assert "L0</strong>×2" in body
    assert "L1</strong>×1" in body
    assert "L4</strong>×1" in body
    # Levels with 0 count should NOT render a chip
    assert "L2</strong>×" not in body
    assert "L3</strong>×" not in body
    assert "L5</strong>×" not in body


def test_pending_high_warning_when_l3_plus_pending(client):
    aid = _seed_attempt(
        levels=[3, 4, 0],
        decisions=["pending", "pending", "approved"],
    )
    resp = client.get(f"/resume/review/{aid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "2 条 L3+ 待你确认" in body


def test_no_pending_high_when_all_low_level(client):
    aid = _seed_attempt(levels=[0, 1, 1])
    resp = client.get(f"/resume/review/{aid}")
    body = resp.get_data(as_text=True)
    assert "L3+ 待你确认" not in body


def test_no_pending_high_when_l3_already_decided(client):
    aid = _seed_attempt(
        levels=[3, 4],
        decisions=["approved", "rejected"],
    )
    resp = client.get(f"/resume/review/{aid}")
    body = resp.get_data(as_text=True)
    # Decided ones don't trigger pending warning
    assert "L3+ 待你确认" not in body


def test_left_border_color_per_level(client):
    aid = _seed_attempt(levels=[0, 4])
    resp = client.get(f"/resume/review/{aid}")
    body = resp.get_data(as_text=True)
    # L0 → muted, L4 → warn
    assert "border-left: 3px solid var(--muted)" in body
    assert "border-left: 3px solid var(--warn)" in body


def test_data_amp_level_attr_present(client):
    """data-amp-level attribute drives the JS filter — must always be set."""
    aid = _seed_attempt(levels=[2, 5])
    resp = client.get(f"/resume/review/{aid}")
    body = resp.get_data(as_text=True)
    assert 'data-amp-level="L2"' in body
    assert 'data-amp-level="L5"' in body


def test_empty_attempt_renders_overview_with_no_chips(client):
    """An attempt with zero variants still renders the overview shell."""
    with session_scope() as s:
        iv = IdentityVersion(label="t", is_current=True)
        s.add(iv); s.flush()
        job = Job(title="X", company="C", link="https://e.com/empty", description="JD")
        s.add(job); s.flush()
        att = RewriteAttempt(job_id=job.id, identity_id=iv.id)
        s.add(att); s.flush()
        aid = att.id

    resp = client.get(f"/resume/review/{aid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "风险概览" in body
    # No chips when no variants
    assert "</strong>×" not in body
