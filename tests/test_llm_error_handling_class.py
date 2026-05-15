"""Lint test: every place that catches LLM errors must use the parent class
(LLMError) — catching only LLMConfigError leaks transient 401/429/timeout
failures up as 500 responses instead of friendly Result objects.

Two locations are intentionally excluded:
  - services/llm.py — internal retry control re-raises LLMConfigError to skip retries
  - services/preference_extractor.py — explicit re-raise lets caller decide

Anything else catching LLMConfigError is a bug.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "compass"
ALLOWED = {
    ROOT / "services/llm.py",
    ROOT / "services/preference_extractor.py",
}


def test_no_narrow_llm_config_error_catches():
    offenders = []
    for p in ROOT.rglob("*.py"):
        if "__pycache__" in str(p) or p in ALLOWED:
            continue
        text = p.read_text(encoding="utf-8")
        if "except LLMConfigError" in text:
            # Find lines for context
            for i, line in enumerate(text.splitlines(), 1):
                if "except LLMConfigError" in line:
                    offenders.append(f"{p.relative_to(ROOT.parent)}:{i}  {line.strip()}")

    assert not offenders, (
        "narrow `except LLMConfigError` (without parent LLMError) leaks "
        "transient LLM failures as 500. Use `except LLMError` (or wider). "
        "Offenders:\n  " + "\n  ".join(offenders)
    )
