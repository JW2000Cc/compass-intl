"""Tests for native_ats_check — ADR-0015 字面 keyword coverage.

The point of this layer is to be deterministic, language-agnostic,
and aligned with what an ATS actually scans. So tests focus on behavior
that matters at scale: stop-word filtering, multi-word phrases,
fuzzy matching for typos, and missing-list accuracy.
"""
from __future__ import annotations

from compass.services.native_ats_check import (
    NativeATSReport,
    extract_jd_keywords_native,
    native_ats_check,
)


# ─── extract_jd_keywords_native ──────────────────────────


def test_extract_picks_multiword_phrases():
    jd = "We need experience with Machine Learning and Deep Learning."
    kws = extract_jd_keywords_native(jd)
    lower = [k.lower() for k in kws]
    assert "machine learning" in lower
    assert "deep learning" in lower


def test_extract_picks_acronyms():
    jd = "Strong knowledge of AWS, Kubernetes, and ETL pipelines."
    kws = extract_jd_keywords_native(jd)
    assert "AWS" in kws
    assert "ETL" in kws


def test_extract_filters_english_stopwords():
    jd = "The candidate will work with their team."
    kws = extract_jd_keywords_native(jd)
    lower = {k.lower() for k in kws}
    for stop in ("with", "their", "will", "work"):
        if stop == "work":
            # 'work' might survive (>= 4 chars, not in stop list) — acceptable
            continue
        assert stop not in lower


def test_extract_filters_italian_stopwords():
    jd = "Il candidato lavorerà con il team della società per progetti di Machine Learning."
    kws = extract_jd_keywords_native(jd)
    lower = {k.lower() for k in kws}
    for stop in ("della", "questo", "tutti"):
        assert stop not in lower
    # But the meaningful tokens come through
    assert any("machine" in k.lower() for k in kws)


def test_extract_skips_pure_numbers():
    jd = "5+ years of experience with 99.9 uptime."
    kws = extract_jd_keywords_native(jd)
    assert "5" not in kws
    assert "99.9" not in kws
    assert any("years" in k.lower() for k in kws)


def test_extract_dedupes_case_insensitively():
    jd = "Python developer using python tools and PYTHON libraries."
    kws = extract_jd_keywords_native(jd)
    py_count = sum(1 for k in kws if k.lower() == "python")
    assert py_count == 1


# ─── native_ats_check ────────────────────────────────────


def test_high_match_when_resume_covers_main_terms():
    jd = "Machine Learning Engineer with AWS, ETL, Kubernetes."
    resume = (
        "ML engineer using Machine Learning, AWS, ETL pipelines, "
        "and Kubernetes clusters in production."
    )
    report = native_ats_check(resume, jd)
    assert report.total > 0
    # All major tech tokens hit; "Engineer" is the only generic that may miss
    assert report.coverage_ratio >= 0.7, (
        f"Expected ≥ 0.7 coverage, got {report.coverage_ratio:.2f}; "
        f"missing={report.missing}"
    )


def test_partial_match_lists_missing():
    jd = "Machine Learning experience with AWS and Kubernetes required."
    resume = "Built ML pipelines using AWS for fraud detection."
    report = native_ats_check(resume, jd)
    assert "Kubernetes" in report.missing
    assert any("aws" in k.lower() for k in report.matched)


def test_italian_jd_with_italian_resume_matches():
    jd = "Cerchiamo Machine Learning Engineer per progetti di intelligenza artificiale."
    resume_it = "Ingegnere di Machine Learning con esperienza in intelligenza artificiale."
    report = native_ats_check(resume_it, jd)
    # The two key multi-word phrases should match
    assert any("machine learning" in k.lower() for k in report.matched)
    assert report.coverage_ratio > 0.5


def test_italian_jd_with_english_resume_misses():
    """The whole point of this check: English resume vs Italian JD ≠ ATS-friendly."""
    jd = "Cerchiamo esperto di apprendimento automatico per la nostra azienda."
    resume_en = "ML engineer with experience in fraud detection."
    report = native_ats_check(resume_en, jd)
    # Italian-specific tokens like "apprendimento" / "automatico" / "azienda" should be missing
    assert any("apprendimento" in m.lower() for m in report.missing)
    assert report.coverage_ratio < 0.5


def test_fuzzy_match_tolerates_minor_typos():
    """Within Levenshtein 2 for tokens >= 6 chars."""
    jd = "Experience with Kubernetes deployment required."
    resume = "Deployed services using Kubernates clusters in prod."  # 'Kubernates' typo
    report = native_ats_check(resume, jd)
    assert any("kubernetes" in k.lower() for k in report.matched)


def test_short_tokens_require_exact():
    """Short tokens (< 6 chars) don't fuzzy match — too risky."""
    jd = "ETL pipelines required."
    resume = "Built ETF analysis tools."  # 'ETF' is 1 edit from 'ETL' but rejected
    report = native_ats_check(resume, jd)
    assert "ETL" in report.missing


def test_empty_jd_yields_empty_report():
    report = native_ats_check("anything", "")
    assert report.total == 0
    assert report.coverage_ratio == 0.0


def test_multi_word_keywords_are_listed():
    jd = "Machine Learning, Apache Spark, Big Data."
    resume = "Worked on Apache Spark for Big Data analysis."
    report = native_ats_check(resume, jd)
    # Multi-word phrases should populate the dedicated field
    assert any("apache spark" in k.lower() for k in report.multi_word_keywords)
    assert any("big data" in k.lower() for k in report.multi_word_keywords)


def test_report_is_dataclass_with_expected_fields():
    """Lock the public schema — UI / routes consume this."""
    report = native_ats_check("any", "any keyword needed")
    assert isinstance(report, NativeATSReport)
    assert hasattr(report, "total")
    assert hasattr(report, "matched")
    assert hasattr(report, "missing")
    assert hasattr(report, "coverage_ratio")
    assert hasattr(report, "multi_word_keywords")
