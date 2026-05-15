"""Regression: export?lang=xx must use FactVariant.translated_json[lang].

Pre-fix bug: build_json_resume read translated_json[lang] expecting str,
but variant_translator stores TranslatedVariant.to_jsonable() (a dict with
keys text/language/preserved/...). isinstance(tx, str) was always False so
the translation was silently dropped — exports ALWAYS came out in English
no matter what `?lang=` said."""
from __future__ import annotations
import pytest
from datetime import datetime, timezone


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/t.sqlite")
    from compass.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seeded(app, tmp_path):
    """One job, one approved variant with zh/it translations."""
    from compass.extensions import session_scope
    from compass.models.core import (Job, JobMatch, FactVariant, ResumeFact,
                                       IdentityVersion)
    with session_scope() as s:
        # Module-global engine leaks state across pytest tmp_path; explicit
        # wipe ensures a clean slate per test.
        s.query(FactVariant).delete()
        s.query(JobMatch).delete()
        s.query(Job).delete()
        s.query(ResumeFact).delete()
        s.query(IdentityVersion).delete()
        iv = IdentityVersion(label="t", source_filename="x", raw_text="x",
                             parsed_json={"name": "X"}, language="en", is_current=True)
        s.add(iv); s.flush()

        exp = "Quant · Banca · 2024–2025"
        f_exp = ResumeFact(identity_id=iv.id, kind="experience", active=True,
                           text=exp, structured_json={"company": "Banca", "title": "Quant",
                                                       "start": "2024", "end": "2025"})
        f_b = ResumeFact(identity_id=iv.id, kind="experience_bullet", active=True,
                         text="Built ETL pipeline.",
                         structured_json={"belongs_to": exp})
        s.add(f_exp); s.add(f_b); s.flush()

        j = Job(title="X", company="Y", location="L", link="https://x.test/1",
                source="indeed", description="...", posted_at=datetime.now(timezone.utc),
                is_remote=False, status="new", deleted=False)
        s.add(j); s.flush()
        s.add(JobMatch(job_id=j.id, tier=1, score=0.9, model="mock"))

        v = FactVariant(
            fact_id=f_b.id, job_id=j.id,
            text="Built ETL pipeline saving 3 hrs/day",
            amplification_level=2, user_decision="approved",
            # Shape exactly as variant_translator writes it (dict per lang)
            translated_json={
                "zh": {"text": "搭建 ETL 流水线，每天省 3 小时", "language": "zh",
                       "preserved": [], "missing": [], "llm_used": True, "warnings": []},
                "it": {"text": "Realizzato pipeline ETL", "language": "it",
                       "preserved": [], "missing": [], "llm_used": True, "warnings": []},
            },
        )
        s.add(v)
        return j.id


def test_export_lang_zh_uses_chinese_translation(client, seeded):
    job_id = seeded
    r = client.get(f"/export/markdown/{job_id}?lang=zh")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "搭建 ETL 流水线" in body, (
        "Chinese translation must override English variant text. "
        f"Got: {body[:300]}"
    )
    assert "Built ETL pipeline saving 3 hrs/day" not in body, (
        "Should NOT contain English when zh is requested"
    )


def test_export_lang_it_uses_italian_translation(client, seeded):
    r = client.get(f"/export/markdown/{seeded}?lang=it")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Realizzato pipeline ETL" in body


def test_export_lang_missing_falls_back_to_english(client, seeded):
    """fr translation not provided → fall back to v.text (original English)."""
    r = client.get(f"/export/markdown/{seeded}?lang=fr")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Built ETL pipeline saving 3 hrs/day" in body, (
        "Missing translation should fall back to original variant text, not skip"
    )


def test_export_no_lang_uses_original(client, seeded):
    r = client.get(f"/export/markdown/{seeded}")  # no ?lang=
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Built ETL pipeline saving 3 hrs/day" in body
    assert "搭建 ETL 流水线" not in body


def test_export_legacy_string_translation_still_works(client, seeded):
    """Forward-compat: if a variant's translated_json[lang] is a plain str
    (legacy shape), export still uses it."""
    from compass.extensions import session_scope
    from compass.models.core import FactVariant
    from sqlalchemy import select
    with session_scope() as s:
        v = s.execute(select(FactVariant).where(FactVariant.job_id == seeded)).scalar_one()
        v.translated_json = {"de": "Aufgebautes ETL-Pipeline-System"}
    r = client.get(f"/export/markdown/{seeded}?lang=de")
    assert r.status_code == 200
    body = r.data.decode("utf-8")
    assert "Aufgebautes ETL" in body
