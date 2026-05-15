"""Integration test: /jobs/<id> renders JD keyword coverage when latest_attempt has keywords."""
from __future__ import annotations

import pytest

from compass.app import create_app
from compass.extensions import session_scope
from compass.models import (
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


def _seed(*, jd: str, keywords: list[str], fact_texts: list[str]) -> str:
    with session_scope() as s:
        iv = IdentityVersion(label="t", is_current=True)
        s.add(iv); s.flush()
        for ft in fact_texts:
            s.add(ResumeFact(identity_id=iv.id, kind="experience", text=ft, active=True))
        job = Job(
            title="Senior Engineer",
            company="C",
            link="https://e.com/1",
            description=jd,
        )
        s.add(job); s.flush()
        s.add(RewriteAttempt(job_id=job.id, identity_id=iv.id, keywords_extracted=keywords))
        s.flush()
        return job.id


def test_no_attempt_yields_no_coverage_bar(client):
    with session_scope() as s:
        iv = IdentityVersion(label="t", is_current=True)
        s.add(iv); s.flush()
        job = Job(title="X", company="C", link="https://e.com/x", description="JD")
        s.add(job); s.flush()
        jid = job.id

    resp = client.get(f"/jobs/{jid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "JD 关键词覆盖" not in body


def test_full_coverage_shows_100_percent(client):
    jid = _seed(
        jd="Looking for Python and SQL skills",
        keywords=["Python", "SQL"],
        fact_texts=["I write Python and SQL daily"],
    )
    resp = client.get(f"/jobs/{jid}")
    body = resp.get_data(as_text=True)
    assert "JD 关键词覆盖" in body
    assert "100%" in body
    # Every keyword renders as kw-hit at least once (in JD body)
    assert 'class="kw-hit"' in body
    # No miss chips
    assert "缺 0 个" not in body  # the "missing" details only appear when miss > 0


def test_partial_coverage_shows_miss_keywords(client):
    jid = _seed(
        jd="Need Python, SQL, Rust experience",
        keywords=["Python", "SQL", "Rust"],
        fact_texts=["I write Python and SQL"],
    )
    resp = client.get(f"/jobs/{jid}")
    body = resp.get_data(as_text=True)
    assert "67%" in body  # 2/3
    assert "缺 1 个" in body
    assert 'class="kw-miss"' in body
    # The miss keyword "Rust" should appear inside a kw-miss mark
    assert ">Rust</mark>" in body


def test_zero_coverage_all_miss(client):
    jid = _seed(
        jd="Need Rust, Haskell, OCaml",
        keywords=["Rust", "Haskell", "OCaml"],
        fact_texts=["I write Python only"],
    )
    resp = client.get(f"/jobs/{jid}")
    body = resp.get_data(as_text=True)
    assert "0%" in body
    assert "缺 3 个" in body
