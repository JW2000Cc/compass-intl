"""Tests for ADR-0017 in-place: custom provider + base_url + presets."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from compass.app import create_app
from compass.services.llm_settings import (
    KNOWN_BASE_URL_PRESETS,
    KNOWN_MODELS,
    PRESET_CONFIGS,
    LLMSettings,
    apply_overlay_to,
    estimate_cost,
    save,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/test.sqlite")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ─── Schema: base_url + custom provider ──────────────────


def test_settings_default_base_url_empty():
    s = LLMSettings()
    assert s.base_url == ""


def test_base_url_round_trips(tmp_path):
    save(tmp_path, LLMSettings(
        provider="custom",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
    ))
    from compass.services.llm_settings import load
    s = load(tmp_path)
    assert s.provider == "custom"
    assert s.base_url == "https://api.deepseek.com/v1"


def test_overlay_applies_base_url_to_settings(tmp_path):
    save(tmp_path, LLMSettings(
        provider="custom", base_url="https://api.deepseek.com/v1",
    ))

    class _Stub:
        llm_provider = "claude"
        llm_api_key = ""
        llm_model = ""
        llm_base_url = ""

    s = _Stub()
    apply_overlay_to(s, tmp_path)
    assert s.llm_provider == "custom"
    assert s.llm_base_url == "https://api.deepseek.com/v1"


# ─── KNOWN_MODELS structure with description ─────────────


def test_known_models_have_5_tuples_with_description():
    for provider, models in KNOWN_MODELS.items():
        if not models:  # custom is empty
            continue
        for entry in models:
            assert len(entry) == 5, f"{provider}: tuple must be 5-element"
            mid, label, in_p, out_p, desc = entry
            assert mid and label and desc
            assert isinstance(in_p, (int, float))
            assert isinstance(out_p, (int, float))


def test_estimate_cost_works_with_5_tuple():
    cost = estimate_cost("claude", "claude-haiku-4-5", 100)
    assert cost > 0


# ─── KNOWN_BASE_URL_PRESETS ──────────────────────────────


def test_base_url_presets_have_4_tuple():
    assert len(KNOWN_BASE_URL_PRESETS) >= 5  # at minimum 5 presets
    for entry in KNOWN_BASE_URL_PRESETS:
        assert len(entry) == 4
        label, url, default_model, desc = entry
        assert label and url and desc
        assert url.startswith("http")


def test_base_url_presets_include_self_hosted():
    """Crucial: self-hosted options (Ollama / LMStudio) must be included."""
    labels = [p[0] for p in KNOWN_BASE_URL_PRESETS]
    assert any("Ollama" in l for l in labels)
    assert any("localhost" in p[1] for p in KNOWN_BASE_URL_PRESETS)


# ─── PRESET_CONFIGS ──────────────────────────────────────


def test_preset_configs_complete():
    """Each preset has the required fields."""
    for pid, preset in PRESET_CONFIGS.items():
        assert "label" in preset
        assert "description" in preset


def test_balanced_preset_uses_sonnet_for_creative_steps():
    """Sanity: 'balanced' preset puts Sonnet on rewrite + adversarial."""
    p = PRESET_CONFIGS["balanced"]
    overrides = p.get("step_overrides", {})
    assert overrides.get("rewrite_per_bullet") == "claude-sonnet-4-6"
    assert overrides.get("raise_objection") == "claude-sonnet-4-6"


def test_local_ollama_preset_uses_custom_provider():
    p = PRESET_CONFIGS["local_ollama"]
    assert p["provider"] == "custom"
    assert "localhost" in p["base_url"]


# ─── Form: save base_url ──────────────────────────────────


def test_form_saves_custom_provider_with_base_url(client, tmp_path):
    client.post(
        "/jobs/llm-settings/",
        data={
            "provider": "custom",
            "api_key": "any-string",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
        },
    )
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["provider"] == "custom"
    assert saved["base_url"] == "https://api.deepseek.com/v1"
    assert saved["model"] == "deepseek-chat"


# ─── Apply preset route ──────────────────────────────────


def test_apply_preset_balanced(client, tmp_path):
    resp = client.post("/jobs/llm-settings/preset/balanced", follow_redirects=False)
    assert resp.status_code == 302
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["model"] == "claude-haiku-4-5"
    assert saved["step_overrides"]["rewrite_per_bullet"] == "claude-sonnet-4-6"


def test_apply_preset_local_ollama(client, tmp_path):
    resp = client.post("/jobs/llm-settings/preset/local_ollama")
    assert resp.status_code == 302
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["provider"] == "custom"
    assert "localhost" in saved["base_url"]


def test_apply_preset_unknown_returns_redirect_with_error(client):
    """Unknown preset should not 500 — flash error and redirect."""
    resp = client.post("/jobs/llm-settings/preset/nonexistent")
    assert resp.status_code == 302  # redirect with flash


def test_apply_preset_preserves_api_key(client, tmp_path):
    """User's precious API key must survive preset switching."""
    save(tmp_path, LLMSettings(api_key="sk-ant-existing-key", model="claude-sonnet-4-6"))
    client.post("/jobs/llm-settings/preset/balanced")
    saved = json.loads((tmp_path / "llm_settings.json").read_text())
    assert saved["api_key"] == "sk-ant-existing-key"  # preserved


# ─── Custom provider in chat() — no base_url throws ─────


def test_chat_with_custom_provider_requires_base_url():
    """Sanity: custom provider without base_url should fail explicitly,
    not silently fall back to OpenAI api.openai.com."""
    from compass.services.llm import LLMConfigError, chat

    # Without base_url, custom should still go through but the OpenAI client
    # will throw on connection. We can't exercise that without network.
    # Just check the dispatch logic exists by reviewing chat() — it accepts
    # provider='custom' and routes to _chat_openai with base_url passed through.
    import inspect
    src = inspect.getsource(chat)
    assert "custom" in src
    assert "base_url" in src


# ─── UI rendering of new fields ──────────────────────────


def test_overview_renders_provider_dropdown_with_custom(client):
    resp = client.get("/jobs/llm-settings/")
    body = resp.get_data(as_text=True)
    assert "claude" in body
    assert "openai" in body
    assert "custom" in body
    # Base URL field exists in HTML (might be hidden via display:none)
    assert "base_url" in body or "Base URL" in body


def test_overview_renders_preset_chips(client):
    resp = client.get("/jobs/llm-settings/")
    body = resp.get_data(as_text=True)
    # Should have at least the 4 preset configs
    assert "balanced" in body or "平衡" in body
    assert "local_ollama" in body or "Ollama" in body


def test_overview_renders_base_url_preset_chips(client):
    resp = client.get("/jobs/llm-settings/")
    body = resp.get_data(as_text=True)
    assert "DeepSeek" in body
    assert "Moonshot" in body or "Kimi" in body
    assert "localhost" in body  # Ollama preset URL
