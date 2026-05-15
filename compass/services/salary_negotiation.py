"""Salary negotiation simulator (5-01 Stage 3).

Input:
  - offer_amount + currency + period (monthly/yearly)
  - city + country
  - role + seniority
  - your stated minimum (from preference_brief salary)

LLM does:
  1. Estimate market range for {role, seniority, city} based on its training data
  2. Position the offer in that range (low / mid / high)
  3. Generate a 4-message simulated negotiation thread (you / hiring manager)
  4. Output: "askable range" + "concrete script you can paste"

Why simulated rather than real-time chat:
  - User just needs to know "what's reasonable to ask for" + "how to phrase it"
  - Real chat is engagement loop. Simulator is reflective: read once, decide, exit.
  - Output goes to ReflectionEvent — reviewable later in /reflect/dashboard.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .llm import chat as _llm_chat

log = logging.getLogger(__name__)


@dataclass
class NegotiationInput:
    offer_amount: float
    currency: str = "EUR"
    period: str = "monthly"   # monthly | yearly
    city: str = ""
    country: str = ""
    role: str = ""
    seniority: str = "mid"     # junior | mid | senior | staff
    your_minimum: Optional[float] = None
    notes: str = ""             # 用户补充（已 offer 多份 / 关键技能 / 等）


@dataclass
class NegotiationResult:
    market_low: float
    market_mid: float
    market_high: float
    offer_position: str        # "below_market" | "low" | "fair" | "high" | "above_market"
    askable_range_low: float    # 可争取的下限
    askable_range_high: float   # 可争取的上限
    rationale: str              # 为什么可以这样争取
    user_script: str            # 用户可以直接 paste 的话术
    hm_anticipated_response: str  # HM 大概率怎么回
    user_followup: str          # 用户后续怎么接
    final_advice: str           # 总结建议
    raw_llm_response: str = field(default_factory=str)


_PROMPT = """You are a salary-negotiation reflection partner. The user has an offer
and wants to know:
  1. Is this offer fair for the market?
  2. What's a realistic range to negotiate up to?
  3. How exactly to phrase the ask?

Be CONCRETE with numbers. No vague "research the market" advice — you DO have
training data on tech salaries by city/country/seniority. Estimate ranges to
the best of your knowledge and SHOW your reasoning.

Output JSON ONLY (no preamble) with these fields:
{
  "market_low": <number, monthly in {currency}>,
  "market_mid": <number>,
  "market_high": <number>,
  "offer_position": "below_market" | "low" | "fair" | "high" | "above_market",
  "askable_range_low": <number>,
  "askable_range_high": <number>,
  "rationale": "<2-4 sentences why this range>",
  "user_script": "<the user's negotiation message, 3-5 sentences, ready to paste>",
  "hm_anticipated_response": "<the HM's likely counter, 2-3 sentences>",
  "user_followup": "<the user's follow-up, 2-3 sentences>",
  "final_advice": "<1-2 sentence summary: should they push? walk away? accept?>"
}

The script should sound like a real human, not a script kiddie. Reference the
specific role / city / their note. Avoid corporate buzzwords.

CASE:
  Role: {role} ({seniority})
  Location: {city}, {country}
  Offer: {offer_amount} {currency}/{period}
  Your stated minimum: {your_minimum}
  Notes from user: {notes}
"""


def simulate_negotiation(inp: NegotiationInput, settings) -> NegotiationResult:
    """Call LLM with the negotiation prompt, parse JSON response."""
    prompt = _PROMPT.format(
        role=inp.role or "(unspecified)",
        seniority=inp.seniority,
        city=inp.city or "(unspecified)",
        country=inp.country or "(unspecified)",
        offer_amount=inp.offer_amount,
        currency=inp.currency,
        period=inp.period,
        your_minimum=inp.your_minimum or "(not stated)",
        notes=inp.notes or "(none)",
    )

    response = _llm_chat(
        system="You are a concise salary-negotiation advisor. Output JSON only.",
        user=prompt,
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
    )
    raw = response.text or ""

    try:
        # Tolerate ```json ... ``` fences
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].lstrip()
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        data = json.loads(cleaned)
    except (ValueError, IndexError) as exc:
        log.warning("salary_negotiation: LLM JSON parse failed: %s", exc)
        # Fallback: return a structured "we couldn't parse" result so UI can still render
        return NegotiationResult(
            market_low=inp.offer_amount * 0.85,
            market_mid=inp.offer_amount,
            market_high=inp.offer_amount * 1.20,
            offer_position="fair",
            askable_range_low=inp.offer_amount,
            askable_range_high=inp.offer_amount * 1.10,
            rationale="LLM 响应解析失败 — fallback 给保守估计（offer ±10-20%）",
            user_script="（LLM 解析失败，建议手动或重试）",
            hm_anticipated_response="—",
            user_followup="—",
            final_advice="LLM JSON parse 失败，请重试或手动判断。",
            raw_llm_response=raw[:1500],
        )

    return NegotiationResult(
        market_low=float(data.get("market_low", 0)),
        market_mid=float(data.get("market_mid", 0)),
        market_high=float(data.get("market_high", 0)),
        offer_position=str(data.get("offer_position", "fair")),
        askable_range_low=float(data.get("askable_range_low", 0)),
        askable_range_high=float(data.get("askable_range_high", 0)),
        rationale=str(data.get("rationale", "")),
        user_script=str(data.get("user_script", "")),
        hm_anticipated_response=str(data.get("hm_anticipated_response", "")),
        user_followup=str(data.get("user_followup", "")),
        final_advice=str(data.get("final_advice", "")),
        raw_llm_response=raw[:1500],
    )
