"""Tests for LLM settings UI + JSON overlay."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compass.app import create_app
from compass.services.llm_settings import (
    KNOWN_MODELS,
    KNOWN_STEPS,
    LLMSettings,
    apply_overlay_to,
    estimate_cost,
    get_overlay,
    load,
    save,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/test.sqlite")
    # Clear any LLM_API_KEY from the dev's .env so the test_connection
    # 400-on-missing-key test is reproducible.
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ─── Schema + IO ──────────────────────────────────────────


def test_default_settings_empty():
    s = LLMSettings()
    assert s.provider == ""
    assert s.api_key == ""
    assert s.step_overrides == {}
    assert s.feature_flags == {}


def test_round_trip_through_disk(tmp_path):
    s1 = LLMSettings(
        provider="claude", api_key="sk-test-x", model="claude-haiku-4-5",
        step_overrides={"rewrite_per_bullet": "claude-sonnet-4-6"},
        feature_flags={"decompose_bullets": True},
    )
    save(tmp_path, s1)
    s2 = load(tmp_path)
    assert s2.provider == "claude"
    assert s2.api_key == "sk-test-x"
    assert s2.step_overrides["rewrite_per_bullet"] == "claude-sonnet-4-6"
    assert s2.is_enabled("decompose_bullets") is True
    assert s2.is_enabled("translate_variants") is False


def test_load_missing_file_returns_empty(tmp_path):
    s = load(tmp_path)
    assert s.provider == ""


def test_load_corrupt_file_falls_back(tmp_path):
    (tmp_path / "llm_settings.json").write_text("{not json", encoding="utf-8")
    s = load(tmp_path)
    assert s.provider == ""  # graceful


def test_masked_key_hides_full_value():
    s = LLMSettings(api_key="sk-ant-12345-secret-key-1234")
    masked = s.masked_key()
    assert "secret" not in masked
    assert masked.startswith("sk-a")
    assert masked.endswith("1234")


def test_masked_key_not_set():
    s = LLMSettings()
    assert "未设置" in s.masked_key()


def test_model_for_step_returns_override():
    s = LLMSettings(step_overrides={"rewrite_per_bullet": "claude-sonnet-4-6"})
    assert s.model_for_step("rewrite_per_bullet", "default-x") == "claude-sonnet-4-6"
    assert s.model_for_step("ground_variant", "default-x") == "default-x"


# ─── Overlay onto Settings ───────────────────────────────


def test_overlay_overrides_provider_only_when_non_empty(tmp_path):
    """Empty overlay fields should NOT override .env defaults."""
    save(tmp_path, LLMSettings(model="claude-haiku-4-5"))  # only model set

    class _Stub:
        llm_provider = "claude"
        llm_api_key = "env-key"
        llm_model = "claude-sonnet-4-6"

    s = _Stub()
    apply_overlay_to(s, tmp_path)
    assert s.llm_provider == "claude"  # unchanged (overlay was empty)
    assert s.llm_api_key == "env-key"  # unchanged
    assert s.llm_model == "claude-haiku-4-5"  # overlay won


def test_overlay_attaches_to_settings(tmp_path):
    save(tmp_path, LLMSettings(feature_flags={"decompose_bullets": True}))

    class _Stub:
        llm_provider = "claude"
        llm_api_key = ""
        llm_model = ""

    s = _Stub()
    apply_overlay_to(s, tmp_path)
    overlay = get_overlay(s)
    assert overlay.is_enabled("decompose_bullets") is True


# ─── Cost estimation ─────────────────────────────────────


def test_cost_estimate_haiku_cheaper_than_sonnet():
    haiku = estimate_cost("claude", "claude-haiku-4-5", 100)
    sonnet = estimate_cost("claude", "claude-sonnet-4-6", 100)
    assert haiku < sonnet
    # Should be roughly 4× difference
    assert sonnet / haiku > 3.0


def test_cost_estimate_unknown_model_returns_zero():
    assert estimate_cost("claude", "claude-imaginary-99", 100) == 0.0


# ─── HTTP routes ──────────────────────────────────────────


def test_overview_renders(client):
    resp = client.get("/jobs/llm-settings/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "LLM 配置" in body or "LLM" in body
    # Provider dropdown should be rendered
    assert "claude" in body
    assert "近 30 天用量" in body or "alternatives" in body.lower()


def test_save_api_key_via_form(client, tmp_path):
    resp = client.post(
        "/jobs/llm-settings/",
        data={
            "provider": "claude",
            "api_key": "sk-ant-real-test-key-9999",
            "model": "claude-haiku-4-5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["api_key"] == "sk-ant-real-test-key-9999"
    assert saved["model"] == "claude-haiku-4-5"


def test_save_keeps_existing_key_when_form_has_mask(client, tmp_path):
    """User reloaded page → form pre-filled with masked key. Submitting back
    that masked value should NOT replace the real key with mask."""
    save(tmp_path, LLMSettings(api_key="sk-ant-real-key-secret"))
    masked = LLMSettings(api_key="sk-ant-real-key-secret").masked_key()
    client.post(
        "/jobs/llm-settings/",
        data={"provider": "claude", "api_key": masked, "model": "claude-haiku-4-5"},
    )
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["api_key"] == "sk-ant-real-key-secret"  # unchanged


def test_feature_flags_round_trip_through_form(client, tmp_path):
    client.post(
        "/jobs/llm-settings/",
        data={
            "provider": "claude",
            "model": "claude-haiku-4-5",
            "flag_decompose_bullets": "on",
            "flag_native_ats_check": "on",
            # translate not checked
        },
    )
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["feature_flags"]["decompose_bullets"] is True
    assert saved["feature_flags"]["native_ats_check"] is True
    assert saved["feature_flags"]["translate_variants"] is False


def test_step_overrides_round_trip(client, tmp_path):
    client.post(
        "/jobs/llm-settings/",
        data={
            "provider": "claude",
            "model": "claude-haiku-4-5",
            "step_rewrite_per_bullet": "claude-sonnet-4-6",
            "step_raise_objection": "claude-sonnet-4-6",
            # other steps left empty → not in overrides
        },
    )
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["step_overrides"]["rewrite_per_bullet"] == "claude-sonnet-4-6"
    assert saved["step_overrides"]["raise_objection"] == "claude-sonnet-4-6"
    assert "ground_variant" not in saved["step_overrides"]


def test_test_connection_no_key_returns_400(client, app):
    """Even if the dev's .env has LLM_API_KEY, force-blank it for this test."""
    app.config["SETTINGS"].llm_api_key = ""
    resp = client.post("/jobs/llm-settings/test")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "API key" in data["msg"]


# ─── Regression: step_overrides MUST influence pipeline calls ──────────


def test_pick_model_resolves_overrides():
    """Helper directly testable. Confirms overrides win over default."""
    from compass.services.resume_writer import _pick_model
    overrides = {"rewrite_per_bullet": "claude-sonnet-4-6", "judge_resume": "claude-haiku-4-5"}
    assert _pick_model("rewrite_per_bullet", "default-model", overrides) == "claude-sonnet-4-6"
    assert _pick_model("judge_resume", "default-model", overrides) == "claude-haiku-4-5"
    # Step not in overrides → default wins
    assert _pick_model("ground_variant", "default-model", overrides) == "default-model"
    # No overrides at all → default wins
    assert _pick_model("rewrite_per_bullet", "default-model", None) == "default-model"
    # Empty string in overrides → default wins (don't break LLM call with empty model)
    assert _pick_model("rewrite_per_bullet", "default-model", {"rewrite_per_bullet": ""}) == "default-model"



def test_run_pipeline_accepts_step_overrides_param():
    """Sanity: signature change took effect — caller can pass step_overrides
    without TypeError. Behavioural correctness is covered by _pick_model unit
    test above; the route handler wiring is verified by inspection (no full
    end-to-end run here to avoid LLM stubbing complexity)."""
    import inspect
    from compass.services.resume_writer import run_pipeline, regenerate_single_variant
    assert "step_overrides" in inspect.signature(run_pipeline).parameters
    assert "step_overrides" in inspect.signature(regenerate_single_variant).parameters


def test_resume_route_passes_step_overrides_from_settings():
    """Route handler MUST pull step_overrides off the LLMSettings overlay
    and thread it into run_pipeline / regenerate_single_variant."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "compass/routes/resume.py"
    text = src.read_text(encoding="utf-8")
    # Both call sites should pass step_overrides explicitly
    assert text.count("step_overrides=") >= 2, (
        "resume.py should thread step_overrides into both run_pipeline and "
        "regenerate_single_variant — otherwise UI per-step picker is a placebo."
    )
    # And the value should come from settings._llm_overlay.step_overrides
    assert "_llm_overlay" in text and "step_overrides" in text
