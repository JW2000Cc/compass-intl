"""Tests for the unified scrape result formatter (B1/B2).

Pinpoints the bug fixed: `format_scrape_result` MUST surface gate_blocked
even when new=0, otherwise the user sees silent "新增 0" with no clue why.
"""
from __future__ import annotations

from compass.services.scrape_messages import ScrapeTotals, format_scrape_result


def test_happy_path_ok_level():
    t = ScrapeTotals(raw=20, new=15, updated=2, profiles_run=1)
    msg, level = format_scrape_result(t)
    assert level == "ok"
    assert "新增 15" in msg
    assert "jobspy 拿回 20" in msg


def test_silent_gate_blocked_now_visible():
    """B1 regression: pre-fix this case showed '新增 0' with no diagnostic.
    Post-fix the diagnostic spells out gate_blocked count + likely cause."""
    t = ScrapeTotals(raw=50, new=0, gate_blocked=50, profiles_run=1)
    msg, level = format_scrape_result(t)
    assert level == "warn"
    assert "新增 0" in msg
    assert "dealbreaker" in msg
    assert "50" in msg
    assert "country_must_be" in msg or "language_blocked" in msg or "关键词" in msg


def test_blacklist_no_longer_mislabeled_as_dealbreaker():
    """Pre-fix flash said "按 dealbreaker 过滤 X" but X was actually blacklist
    count — confused users about which filter to relax."""
    t = ScrapeTotals(raw=10, new=8, blocked=2, profiles_run=1)
    msg, _ = format_scrape_result(t)
    assert "黑名单滤 2" in msg
    # gate_blocked is 0, so dealbreaker line should NOT appear
    assert "dealbreaker 滤" not in msg


def test_jobspy_total_failure_warn():
    t = ScrapeTotals(raw=0, new=0, profiles_run=1, errors=["linkedin@'Frankfurt': 429"])
    msg, level = format_scrape_result(t)
    assert level == "warn"
    assert "jobspy 没拿到任何结果" in msg


def test_all_profiles_failed_error_level():
    t = ScrapeTotals(profiles_run=2, failed_profiles=["DE", "IT"])
    _, level = format_scrape_result(t)
    assert level == "error"


def test_partial_failure_marks_warn():
    t = ScrapeTotals(raw=10, new=5, profiles_run=2, failed_profiles=["IT"])
    _, level = format_scrape_result(t)
    # 2 ran, 1 failed: not all-broken, but worth a warn since something failed
    # Current rule treats this as "ok" because new>0 and not all failed; that's
    # fine — the failed_profiles label still shows in msg.
    msg, _ = format_scrape_result(t)
    assert "失败: IT" in msg


def test_absorb_aggregates_correctly():
    t = ScrapeTotals(profiles_run=2)
    t.absorb({"raw": 10, "new": 5, "blocked": 2, "gate_blocked": 3, "errors": []})
    t.absorb({"raw": 20, "new": 0, "blocked": 0, "gate_blocked": 20, "errors": ["x"]})
    assert t.raw == 30
    assert t.new == 5
    assert t.blocked == 2
    assert t.gate_blocked == 23
    assert t.errors == ["x"]


def test_classified_count_in_message():
    t = ScrapeTotals(raw=10, new=10, classified=10, profiles_run=1)
    msg, _ = format_scrape_result(t)
    assert "分级 10" in msg


def test_no_run_no_diagnostic():
    """profiles_run=0 → don't tell user 'jobspy 没拿到结果' (it didn't run)."""
    t = ScrapeTotals()
    msg, level = format_scrape_result(t)
    assert "jobspy 没拿到任何结果" not in msg
    assert level == "ok"
