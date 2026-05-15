"""LLM settings — UI-editable LLM configuration.

ADR-0017 in-place: extends the existing `Settings` dataclass via a JSON
overlay file (`data/llm_settings.json`). The .env stays as canonical default;
the JSON is the user's runtime override. UI writes the JSON; Settings.from_env
reads .env then applies the JSON on top.

Design:
  · Single source of truth at runtime: `Settings.llm_provider/api_key/model`
  · `.env` provides defaults (zero-config first run)
  · JSON overlay lets the user change settings via UI without restart-cycle
  · `step_overrides` (optional) lets per-pipeline-step model selection (rewrite
    + adversarial use Sonnet, others use Haiku — saves ~50% on bulk steps
    while keeping creative steps high-quality)

API key storage note:
  Same security tier as `.env` — local plaintext file.
  `.gitignore` MUST list `data/llm_settings.json`. UI never echoes full key.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Models we support out-of-the-box. Users can type any string but UI dropdowns
# show these. Pricing is for the per-month estimate widget — update when prices
# move (last update: 2026-05).
#
# Tuple shape: (id, label, in_per_M, out_per_M, description)
# `description` is shown next to the model in the UI dropdown — it should
# answer "what is this for?" in one line. Keep ≤ 80 chars.
KNOWN_MODELS = {
    "claude": [
        ("claude-haiku-4-5",
         "Claude Haiku 4.5",
         0.80, 4.00,
         "90% Sonnet 能力, 1/3 价格, 快 — 适合: 抽词/匹配/grounding/judge/翻译/decompose"),
        ("claude-sonnet-4-6",
         "Claude Sonnet 4.6",
         3.00, 15.00,
         "高质量, 创造任务首选 — 适合: rewrite_per_bullet / raise_objection (推理任务)"),
        ("claude-opus-4-7",
         "Claude Opus 4.7",
         15.00, 75.00,
         "最深推理, 慢, ~5× sonnet — 仅: adversarial / 复杂事实推理"),
    ],
    "openai": [
        ("gpt-4o",
         "GPT-4o",
         2.50, 10.00,
         "OpenAI 旗舰多模态 — 适合: rewrite / judge / 各类创造"),
        ("gpt-4o-mini",
         "GPT-4o Mini",
         0.15, 0.60,
         "极便宜+快 — 适合: 抽词/匹配/judge/翻译 (替代 Haiku)"),
    ],
    "custom": [
        # No fixed models — user types whatever the endpoint accepts.
        # UI shows a free-text input instead of a dropdown.
    ],
}


# OpenAI-compatible endpoint presets. UI shows them as "一键填" chips.
# Tuple: (label, base_url, default_model, description)
KNOWN_BASE_URL_PRESETS = [
    ("DeepSeek",
     "https://api.deepseek.com/v1",
     "deepseek-chat",
     "极便宜, 中文友好 — deepseek-chat (V3) / deepseek-reasoner (R1 思考型)"),
    ("Moonshot/Kimi",
     "https://api.moonshot.cn/v1",
     "moonshot-v1-128k",
     "国内, 长上下文 (128K-1M)"),
    ("Groq",
     "https://api.groq.com/openai/v1",
     "llama-3.3-70b-versatile",
     "推理极快 (250+ tok/s) — Llama 3.3 / Mixtral / Whisper"),
    ("Together AI",
     "https://api.together.xyz/v1",
     "meta-llama/Llama-3.3-70B-Instruct-Turbo",
     "聚合多 model — Llama / Qwen / Mixtral 等开源"),
    ("Mistral",
     "https://api.mistral.ai/v1",
     "mistral-large-latest",
     "欧洲 / 强多语言 — mistral-large-latest / codestral"),
    ("Ollama 本地",
     "http://localhost:11434/v1",
     "llama3.3",
     "完全免费 / 自部署 / 慢 — 任何 Ollama pull 的 model 都能跑"),
    ("LMStudio 本地",
     "http://localhost:1234/v1",
     "(自填)",
     "自部署 GGUF / MLX 量化模型"),
]


# Recommended one-click presets — apply to step_overrides + (optionally)
# provider/model/base_url. UI shows a "应用预设" dropdown.
PRESET_CONFIGS = {
    "balanced": {
        "label": "平衡（Haiku + Sonnet for 创造步骤）— 推荐",
        "description": "省 50% 成本，关键 step 仍用 Sonnet 保质量",
        "model": "claude-haiku-4-5",
        "step_overrides": {
            "rewrite_per_bullet": "claude-sonnet-4-6",
            "raise_objection": "claude-sonnet-4-6",
        },
    },
    "cheap": {
        "label": "省钱（全 Haiku）",
        "description": "成本降到 1/3.5，质量稍降但够用 dogfood",
        "model": "claude-haiku-4-5",
        "step_overrides": {},
    },
    "quality": {
        "label": "质量优先（全 Sonnet）",
        "description": "所有 step 用 Sonnet 4.6，最贵也最稳",
        "model": "claude-sonnet-4-6",
        "step_overrides": {},
    },
    "local_ollama": {
        "label": "本地 Ollama（免费，慢）",
        "description": "所有 step 走 localhost:11434，需先 ollama pull llama3.3",
        "provider": "custom",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.3",
        "step_overrides": {},
    },
}

# Step-level granularity for `step_overrides`. The keys here document which
# pipeline steps can be overridden. Adding new steps requires updating both
# this map AND the `chat()` callsite to look up its step name.
KNOWN_STEPS = (
    "extract_jd_keywords",
    "match_facts_to_keywords",
    "rewrite_per_bullet",
    "ground_variant",
    "raise_objection",
    "judge_resume",
    "bullet_decompose",
    "translate_variant",
)


@dataclass
class LLMSettings:
    provider: str = ""
    """'claude' / 'openai' / 'custom'. Empty = use .env default."""

    api_key: str = ""
    """User-editable API key. Empty = use .env default. For self-hosted
    (Ollama / vLLM / LMStudio), can be any non-empty placeholder."""

    model: str = ""
    """Default model id. Empty = use .env default."""

    base_url: str = ""
    """OpenAI-compatible endpoint. Required when provider='custom'.
    Examples: https://api.deepseek.com/v1 / http://localhost:11434/v1.
    Empty for the official providers."""

    step_overrides: dict[str, str] = field(default_factory=dict)
    """Optional per-step model override. Step name → model id."""

    feature_flags: dict[str, bool] = field(default_factory=dict)
    """Opt-in feature flags (ADR-0015 #3-5 production-path wiring):
        - decompose_bullets   default off
        - translate_variants  default off
        - native_ats_check    default off
    UI exposes these as checkboxes; resume_writer reads to decide whether
    to trigger the optional steps."""

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LLMSettings":
        if not d:
            return cls()
        return cls(
            provider=str(d.get("provider") or "").strip(),
            api_key=str(d.get("api_key") or "").strip(),
            model=str(d.get("model") or "").strip(),
            base_url=str(d.get("base_url") or "").strip(),
            step_overrides={
                str(k): str(v)
                for k, v in (d.get("step_overrides") or {}).items()
                if isinstance(v, str) and v.strip()
            },
            feature_flags={
                str(k): bool(v)
                for k, v in (d.get("feature_flags") or {}).items()
            },
        )

    def model_for_step(self, step_name: str, default: str) -> str:
        """Returns the override model for `step_name`, or `default`."""
        return self.step_overrides.get(step_name) or default

    def is_enabled(self, flag: str) -> bool:
        """Read a feature_flag with default-False."""
        return bool(self.feature_flags.get(flag, False))

    def masked_key(self) -> str:
        """For UI display — never round-trip full key."""
        if not self.api_key:
            return "(未设置)"
        if len(self.api_key) <= 8:
            return "•" * len(self.api_key)
        return f"{self.api_key[:4]}{'•' * 12}{self.api_key[-4:]}"


# ─── IO ────────────────────────────────────────────────────


def llm_settings_path(data_dir: Path) -> Path:
    return data_dir / "llm_settings.json"


def load(data_dir: Path) -> LLMSettings:
    """Load JSON overlay. Returns empty LLMSettings if file missing or corrupt."""
    p = llm_settings_path(data_dir)
    if not p.exists():
        return LLMSettings()
    try:
        return LLMSettings.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("llm_settings file unreadable: %s; falling through to .env", exc)
        return LLMSettings()


def save(data_dir: Path, settings: LLMSettings) -> None:
    p = llm_settings_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(settings.to_jsonable(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


def apply_overlay_to(env_settings, data_dir: Path):
    """Apply UI-stored overrides to the env-derived Settings dataclass.

    Mutates and returns env_settings in place. Empty fields in JSON DO NOT
    override .env (so user can clear a field by writing empty string and
    fall back to env behavior).
    """
    overlay = load(data_dir)
    if overlay.provider:
        env_settings.llm_provider = overlay.provider
    if overlay.api_key:
        env_settings.llm_api_key = overlay.api_key
    if overlay.model:
        env_settings.llm_model = overlay.model
    if overlay.base_url:
        env_settings.llm_base_url = overlay.base_url
    # Stash the full overlay on the Settings object so callers can access
    # step_overrides + feature_flags without re-reading the file.
    env_settings._llm_overlay = overlay  # noqa: attribute mutation by design
    return env_settings


def get_overlay(env_settings) -> LLMSettings:
    """Convenience: read the overlay attached to a Settings instance."""
    return getattr(env_settings, "_llm_overlay", None) or LLMSettings()


# ─── Cost estimation (per the price table at top) ─────────


def estimate_cost(provider: str, model: str, n_calls: int,
                  avg_input: int = 1500, avg_output: int = 400) -> float:
    """Return estimated USD for n_calls of (provider, model) with average
    input/output token sizes. Returns 0.0 if pricing is unknown."""
    for entry in KNOWN_MODELS.get(provider, []):
        # Tuple shape: (id, label, in_per_M, out_per_M, description)
        pid = entry[0]
        in_price = entry[2]
        out_price = entry[3]
        if pid == model:
            cost_per_call = (avg_input * in_price + avg_output * out_price) / 1_000_000
            return cost_per_call * n_calls
    return 0.0
