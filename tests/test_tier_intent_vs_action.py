"""tier_intent_vs_action — stated tier_quota vs revealed engagement distribution.

反身性距离指标: 用户在 search-config 配的 tier_quota 总和(归一化) vs
用户最近 N 天实际点开/改写/👍 的岗位 tier 分布。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from compass.models import Job, JobMatch, ReflectionEvent
from compass.services.reflection_engine import tier_intent_vs_action


def _intent(*tier_quotas):
    """Build a fake JobSearchIntent-like object: each tier_quota is (t1, t2, t3)."""
    profiles = []
    for t1, t2, t3 in tier_quotas:
        p = SimpleNamespace(
            enabled=True,
            tier_quota=SimpleNamespace(tier_1=t1, tier_2=t2, tier_3=t3),
        )
        profiles.append(p)
    return SimpleNamespace(enabled_profiles=lambda: profiles)


def _add_engagement(session, job, tier, kind="job_opened", days_ago=1):
    m = JobMatch(job_id=job.id, tier=tier, score=0.5)
    session.add(m)
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    session.add(ReflectionEvent(kind=kind, related_job_id=job.id, payload_json={}, created_at=when))
    session.flush()


def _job(session, jid_suffix):
    j = Job(title="X", company=f"C{jid_suffix}", link=f"https://e.com/{jid_suffix}")
    session.add(j)
    session.flush()
    return j


def test_no_intent_no_revealed_returns_insufficient(session):
    out = tier_intent_vs_action(session, None, days=60)
    assert out["verdict"] == "insufficient_data"
    assert out["sample_size"] == 0
    assert out["stated"] == {}
    assert out["revealed"] == {}


def test_intent_set_but_no_engagement(session):
    out = tier_intent_vs_action(session, _intent((5, 3, 2)), days=60)
    assert out["verdict"] == "insufficient_data"
    assert out["stated"] == {1: 0.5, 2: 0.3, 3: 0.2}
    assert out["revealed"] == {}
    assert "没有 job 互动事件" in out["message"]


def test_aligned_when_revealed_matches_stated(session):
    intent = _intent((7, 2, 1))
    # 10 jobs: 7 tier-1, 2 tier-2, 1 tier-3
    for i, t in enumerate([1] * 7 + [2] * 2 + [3]):
        _add_engagement(session, _job(session, i), t)
    out = tier_intent_vs_action(session, intent, days=60)
    assert out["verdict"] == "aligned"
    assert out["sample_size"] == 10
    assert out["l1_distance"] < 0.20


def test_tier1_locked_when_actual_only_tier1(session):
    intent = _intent((3, 5, 2))  # 用户声称要 50% Tier 2 探索
    # 实际 8 次点开都是 Tier 1
    for i in range(8):
        _add_engagement(session, _job(session, i), 1)
    out = tier_intent_vs_action(session, intent, days=60)
    assert out["verdict"] == "tier1_locked"
    assert out["gap"][1] > 0
    assert out["gap"][2] < 0
    assert "锁在核心圈" in out["message"]


def test_explorer_revealed_when_actual_more_t2_than_stated(session):
    intent = _intent((8, 1, 1))  # 80/10/10 声称
    # 实际 30% 是 Tier 2
    for i, t in enumerate([1] * 4 + [2] * 3 + [1] * 3):
        _add_engagement(session, _job(session, i), t)
    out = tier_intent_vs_action(session, intent, days=60)
    assert out["verdict"] in ("explorer_revealed", "drifting")
    assert out["revealed"][2] > out["stated"][2]


def test_dedup_same_job_counted_once(session):
    intent = _intent((5, 3, 2))
    j = _job(session, "dup")
    # 同一 job 多个 engagement events
    _add_engagement(session, j, 2, kind="job_opened")
    when2 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    session.add(
        ReflectionEvent(
            kind="rewrite_attempt_completed",
            related_job_id=j.id,
            payload_json={},
            created_at=when2,
        )
    )
    when3 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    session.add(
        ReflectionEvent(
            kind="job_thumbed_up",
            related_job_id=j.id,
            payload_json={},
            created_at=when3,
        )
    )
    session.flush()

    out = tier_intent_vs_action(session, intent, days=60)
    assert out["sample_size"] == 1, "Same job + 3 engagement events should count as 1"


def test_engagement_outside_window_excluded(session):
    intent = _intent((5, 3, 2))
    # 90 天前的 engagement
    _add_engagement(session, _job(session, "old"), 1, days_ago=90)
    out = tier_intent_vs_action(session, intent, days=60)
    assert out["sample_size"] == 0
    assert out["verdict"] == "insufficient_data"


def test_disabled_profile_not_counted_in_stated(session):
    """Only enabled_profiles() contribute to stated quota."""
    enabled = SimpleNamespace(
        enabled=True, tier_quota=SimpleNamespace(tier_1=10, tier_2=0, tier_3=0)
    )
    disabled = SimpleNamespace(
        enabled=False, tier_quota=SimpleNamespace(tier_1=0, tier_2=10, tier_3=0)
    )
    intent = SimpleNamespace(enabled_profiles=lambda: [enabled])  # only the enabled
    out = tier_intent_vs_action(session, intent, days=60)
    assert out["stated"] == {1: 1.0, 2: 0.0, 3: 0.0}


def test_tier_4_5_engagement_excluded_from_revealed(session):
    """tier_intent_vs_action 只对 1/2/3 — Tier 4/5 不计入分布."""
    intent = _intent((5, 3, 2))
    _add_engagement(session, _job(session, "t1"), 1)
    _add_engagement(session, _job(session, "t4"), 4)  # 应被忽略
    _add_engagement(session, _job(session, "t5"), 5)  # 应被忽略
    out = tier_intent_vs_action(session, intent, days=60)
    assert out["sample_size"] == 1
    assert out["revealed"][1] == 1.0
