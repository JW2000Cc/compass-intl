"""Thin LLM wrapper — Claude/OpenAI behind one shape.

Lessons inherited from v1 (don't repeat them):
- Always retry on transient errors with backoff
- Always strip ```json fences before parsing
- Be tolerant of partial JSON (regex-extract the {...} if json.loads fails)
- Fail loud on auth/quota errors (don't swallow into "fail-open")
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMConfigError(LLMError):
    """Missing API key, unsupported provider, etc. NOT for retry."""


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict

    def parse_json(self, default=None):
        return parse_json_lenient(self.text, default=default)


def parse_json_lenient(raw: str, default=None):
    """Parse JSON tolerantly — strip fences, regex-extract on fallback."""
    cleaned = re.sub(r"```(?:json)?\s*|```\s*", "", (raw or "")).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find first JSON object/array in the text
        m = re.search(r"[\[{][\s\S]*[\]}]", cleaned)
        if not m:
            return default
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return default


def chat(
    *,
    provider: str,
    api_key: str,
    model: str,
    system: str,
    user_content: str,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    retries: int = 2,
    base_url: str = "",
    timeout: float = 60.0,
) -> LLMResponse:
    """Single-turn chat. Synchronous, blocking.

    base_url: when provider='custom' (or 'openai' with override), this is sent
        as `OpenAI(base_url=...)` so any OpenAI-compatible endpoint works
        (DeepSeek / Moonshot / Groq / Together / Ollama localhost / vLLM ...).
        Anthropic SDK also accepts base_url via the same parameter.
    timeout: per-call timeout; bump to 180+ for self-hosted Ollama running on
        small hardware (default 60s suits cloud APIs).
    """
    if not api_key:
        # Custom provider with localhost base_url (Ollama / vLLM / LMStudio)
        # often doesn't need a real key — accept any non-empty placeholder.
        if not (provider == "custom" and base_url):
            raise LLMConfigError("LLM_API_KEY not configured")
        api_key = api_key or "local-no-key"

    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if provider == "claude":
                return _chat_claude(
                    api_key, model, system, user_content,
                    max_tokens, temperature, base_url=base_url, timeout=timeout,
                )
            elif provider in ("openai", "custom"):
                # 'custom' always goes through OpenAI-compatible client. The
                # only difference vs 'openai' is whether base_url is required.
                return _chat_openai(
                    api_key, model, system, user_content,
                    max_tokens, temperature, base_url=base_url, timeout=timeout,
                )
            else:
                raise LLMConfigError(f"unsupported provider: {provider}")
        except LLMConfigError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                wait = 1.0 * (2**attempt)
                log.warning("LLM attempt %d failed: %s — retrying in %.1fs", attempt + 1, exc, wait)
                time.sleep(wait)
    raise LLMError(f"LLM call failed after {retries + 1} attempts: {last_exc}") from last_exc


def _chat_claude(key, model, system, user, max_tokens, temperature,
                 *, base_url: str = "", timeout: float = 60.0) -> LLMResponse:
    import anthropic

    # 60s timeout: prevents indefinite hangs when network breaks mid-call.
    # The retry-loop in `chat()` re-attempts, so a per-call cap is the right knob.
    kwargs = {"api_key": key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.Anthropic(**kwargs)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    usage = {"in": getattr(msg.usage, "input_tokens", 0), "out": getattr(msg.usage, "output_tokens", 0)}
    return LLMResponse(text=text, model=msg.model, provider="claude", usage=usage)


def _chat_openai(key, model, system, user, max_tokens, temperature,
                 *, base_url: str = "", timeout: float = 60.0) -> LLMResponse:
    from openai import OpenAI

    kwargs = {"api_key": key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    if not resp.choices:
        raise LLMError("OpenAI returned empty choices array (filter triggered?)")
    choice = resp.choices[0]
    usage = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
    return LLMResponse(text=choice.message.content or "", model=resp.model, provider="openai", usage=usage)
