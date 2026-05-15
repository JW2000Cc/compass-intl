"""Configuration loaded from .env / environment.

We keep this dataclass-shaped (not Flask config dict) so non-Flask code
(scripts, migrations, CLI tools) can consume it directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(key, default)
    return val if val not in ("", None) else default


def _env_bool(key: str, default: bool = False) -> bool:
    val = (_env(key) or "").lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime configuration for Compass."""

    # LLM
    llm_provider: str = "claude"
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-6"
    llm_base_url: str = ""  # only used when provider is 'custom' or override
    llm_timeout: float = 60.0  # bump for slow self-hosted (Ollama on small HW)

    # App
    port: int = 7000
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    log_level: str = "INFO"
    secret_key: str = "change-me"

    # Reflection
    drift_check_interval_days: int = 14
    assumptions_regen_interval_days: int = 30
    adversarial_voice_enabled: bool = True

    # Timezone for displaying timestamps to user (storage is always UTC)
    timezone: str = "Europe/Rome"

    # PKM markdown export — writes data/contacts/<slug>.md, etc.
    # Off by default to avoid surprising users with unexpected file writes.
    pkm_export_enabled: bool = False

    # Filename template for resume exports. Variables: {name} {company} {role}
    # {job_id} {date} {theme} {lang}. Empty bindings collapse cleanly. Override
    # with COMPASS_EXPORT_FILENAME_TEMPLATE in .env, or via UI (TBD).
    export_filename_template: str = "{name}_{company}_{role}"

    # ε-exploration（5-01 P1 第 8 条）：以 ε 概率把 tier 3/4 提升到 tier 2，
    # 反茧房二阶。0 = 关闭，0.05 = 5% 随机暴露。.env: COMPASS_EPSILON_EXPLORATION
    epsilon_exploration: float = 0.0

    @classmethod
    def from_env(cls) -> "Settings":
        # Best-effort .env loading (no hard dep on python-dotenv at import time)
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

        # Default data_dir is **program-relative** (next to compass/ pkg dir), not cwd-relative.
        # This makes Compass portable: move the folder anywhere, data goes with it.
        # Users can override via COMPASS_DATA_DIR env var (absolute path) if they want data elsewhere.
        program_root = Path(__file__).resolve().parent.parent  # → Compass/
        env_data = _env("COMPASS_DATA_DIR")
        data_dir = (
            Path(env_data).expanduser().resolve()
            if env_data
            else program_root / "data"
        )
        s = cls(
            llm_provider=_env("LLM_PROVIDER", "claude") or "claude",
            llm_api_key=_env("LLM_API_KEY", "") or "",
            llm_model=_env("LLM_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6",
            llm_base_url=_env("LLM_BASE_URL", "") or "",
            llm_timeout=float(_env("LLM_TIMEOUT", "60") or 60),
            port=_env_int("COMPASS_PORT", 7000),
            data_dir=data_dir,
            log_level=_env("COMPASS_LOG_LEVEL", "INFO") or "INFO",
            secret_key=_env("COMPASS_SECRET_KEY", "change-me") or "change-me",
            drift_check_interval_days=_env_int("DRIFT_CHECK_INTERVAL_DAYS", 14),
            assumptions_regen_interval_days=_env_int("ASSUMPTIONS_REGEN_INTERVAL_DAYS", 30),
            adversarial_voice_enabled=_env_bool("ADVERSARIAL_VOICE_ENABLED", True),
            timezone=_env("COMPASS_TZ", "Europe/Rome") or "Europe/Rome",
            pkm_export_enabled=_env_bool("COMPASS_PKM_EXPORT", False),
            export_filename_template=(
                _env("COMPASS_EXPORT_FILENAME_TEMPLATE", "{name}_{company}_{role}")
                or "{name}_{company}_{role}"
            ),
            epsilon_exploration=float(_env("COMPASS_EPSILON_EXPLORATION", "0") or 0),
        )
        # Apply UI-edited LLM overlay from data/llm_settings.json (if any).
        # Empty fields in JSON do NOT override .env — user can clear an
        # override by setting it blank in the UI.
        try:
            from .services.llm_settings import apply_overlay_to

            s = apply_overlay_to(s, data_dir)
        except Exception:  # noqa: BLE001
            # Defensive — never let an overlay error block app boot.
            pass
        return s

    @property
    def db_url(self) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.data_dir / 'compass.sqlite'}"

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)
