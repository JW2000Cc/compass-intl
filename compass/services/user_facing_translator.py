"""ADR-0015 改动 G2 — user-facing language layer.

The decomposition data structures (BulletDecomposition / GroundingTag /
MetricStatus) are开发者-shaped: they expose `level: "L5a"`, `verb_candidates`,
`amplification_level`, etc. Showing those to the end user = pseudo-transparency
(数据 surfaced but in a language the user cannot judge with).

This module is the **rendering boundary**: every UI consumer must pass the
decomposition through `to_user_facing()` first. The output uses everyday
labels keyed to the user's locale, with each choice carrying a clear "what
happens if I pick this" note.

Default mode is locale-neutral conversational language. `expert_mode=True`
exposes the raw fields back — for power users / debugging.

Design heuristics from product critique pass (ADR-0015 §问题 5):
  · L0-L5 / amplification_level NEVER shown to user
  · verb / metric / tone field names → "动作词" / "数字成果" / "语气"
  · Accept / Dismiss → "用这条" / "保留原样" / "AI 别管这点"
  · fact → "你的简历里" / "your resume"
  · Each choice has a `severity` (ok / warn / blocked) for visual treatment
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .bullet_decomposer import BulletDecomposition

# ─── Locale-keyed labels ─────────────────────────────────


GROUNDING_LABELS = {
    "zh": {
        "safe": "安全 — 跟你简历一致",
        "check": "你简历里有这意思 — 一键确认即可",
        "interview": "你简历里没说 — 回答 2 个问题就能用",
        "blocked": "你简历里完全没有 — 别用",
    },
    "en": {
        "safe": "Safe — supported by your resume",
        "check": "Your resume implies it — one-click confirm",
        "interview": "Not in your resume — answer 2 questions to add",
        "blocked": "Not supported by your resume — don't use",
    },
}

GROUNDING_SEVERITY = {
    "safe": "ok",
    "check": "ok",
    "interview": "warn",
    "blocked": "blocked",
}

DIMENSION_LABELS = {
    "zh": {
        "verb": "动作词",
        "metrics": "数字成果",
        "jd_keywords": "JD 想要的能力",
        "length": "长度",
        "tone": "语气",
    },
    "en": {
        "verb": "Action Verb",
        "metrics": "Numbers & Outcomes",
        "jd_keywords": "JD Skills",
        "length": "Length",
        "tone": "Tone",
    },
}

LENGTH_LABELS = {
    "zh": {
        "ok": "长度合适",
        "too_short": "短了 — 加细节会更清楚",
        "too_long": "长了 — 简历惯例 14-18 词",
    },
    "en": {
        "ok": "Length OK",
        "too_short": "Too short — add specifics",
        "too_long": "Too long — resume convention is 14-18 words",
    },
}

ACTION_LABELS = {
    "zh": {
        "use": "用这条",
        "keep": "保留原样",
        "skip": "AI 别管这点",
        "interview": "回答 2 个问题升级简历",
        "snap": "改回简历里的写法",
        "remove": "去掉这条",
    },
    "en": {
        "use": "Use this",
        "keep": "Keep as-is",
        "skip": "AI ignore this dimension",
        "interview": "Answer 2 questions to back this up",
        "snap": "Snap back to resume's value",
        "remove": "Remove this claim",
    },
}


def _label(table: dict, locale: str, key: str, fallback: str = "") -> str:
    """Forgiving lookup with locale fallback chain: locale → en → fallback."""
    return (
        table.get(locale, {}).get(key)
        or table.get("en", {}).get(key)
        or fallback
        or key
    )


# ─── Output shapes ────────────────────────────────────────


@dataclass
class UserFacingChoice:
    """A single picked-able option in a dimension."""

    label: str
    """Short imperative — e.g. "用 'Developed'" / "保留原样"."""

    note: str = ""
    """Why pick this — grounding rationale in user language."""

    severity: str = "ok"
    """ok / warn / blocked — for visual treatment in UI."""

    action: str = "use"
    """Machine-dispatch verb: use / keep / skip / interview / snap / remove."""

    payload: dict = field(default_factory=dict)
    """Action-specific data (e.g. for "snap": {"old": "22%", "new": "23%"})."""

    def to_jsonable(self) -> dict:
        return asdict(self)


@dataclass
class UserFacingDimension:
    """One section of the menu — verb / metric / jd_keywords / length / tone."""

    label: str
    description: str = ""
    choices: list[UserFacingChoice] = field(default_factory=list)
    severity: str = "ok"
    """Worst severity across choices — bubbles up for sorting/highlight."""

    def to_jsonable(self) -> dict:
        return {
            "label": self.label,
            "description": self.description,
            "choices": [c.to_jsonable() for c in self.choices],
            "severity": self.severity,
        }


@dataclass
class UserFacingMenu:
    dimensions: list[UserFacingDimension] = field(default_factory=list)
    has_warnings: bool = False
    has_blockers: bool = False

    def to_jsonable(self) -> dict:
        return {
            "dimensions": [d.to_jsonable() for d in self.dimensions],
            "has_warnings": self.has_warnings,
            "has_blockers": self.has_blockers,
        }


# ─── Per-dimension renderers ──────────────────────────────


def _verb_dimension(d: BulletDecomposition, locale: str) -> UserFacingDimension:
    label = _label(DIMENSION_LABELS, locale, "verb")
    grounding_lbl = GROUNDING_LABELS.get(locale, GROUNDING_LABELS["en"])
    actions = ACTION_LABELS.get(locale, ACTION_LABELS["en"])

    choices: list[UserFacingChoice] = []
    worst = "ok"
    for v in d.verb_candidates:
        sev = GROUNDING_SEVERITY.get(v.grounding.level, "warn")
        if sev == "blocked" and worst != "blocked":
            worst = "blocked"
        elif sev == "warn" and worst == "ok":
            worst = "warn"
        # Skip blocked verbs from the actionable list — but include with note
        choices.append(
            UserFacingChoice(
                label=f"{actions['use']} '{v.text}'",
                note=grounding_lbl.get(v.grounding.level, v.grounding.level),
                severity=sev,
                action="use" if sev != "blocked" else "skip",
                payload={"verb": v.text, "rationale": v.grounding.rationale},
            )
        )
    if not choices:
        # No LLM ran — empty placeholder so user sees the dimension exists
        choices.append(
            UserFacingChoice(
                label=actions["keep"], note="", severity="ok", action="keep"
            )
        )
    return UserFacingDimension(label=label, choices=choices, severity=worst)


def _metric_dimension(d: BulletDecomposition, locale: str) -> UserFacingDimension:
    label = _label(DIMENSION_LABELS, locale, "metrics")
    actions = ACTION_LABELS.get(locale, ACTION_LABELS["en"])
    is_zh = locale.lower().startswith("zh")
    choices: list[UserFacingChoice] = []
    worst = "ok"
    for m in d.metrics:
        if m.in_fact:
            choices.append(
                UserFacingChoice(
                    label=f"{m.bullet_value} ✓",
                    note=(
                        f"已对照你简历，正确"
                        if is_zh
                        else "Verified against your resume"
                    ),
                    severity="ok",
                    action="keep",
                    payload={"value": m.bullet_value, "verified": True},
                )
            )
        elif m.drift_kind == "L5a" and m.auto_fix:
            worst = "warn"
            choices.append(
                UserFacingChoice(
                    label=(
                        f"AI 写成 {m.bullet_value}，简历里是 {m.auto_fix} — 自动改回"
                        if is_zh
                        else f"AI wrote {m.bullet_value}; resume has {m.auto_fix} — auto-corrected"
                    ),
                    note=(
                        "已自动修正"
                        if is_zh
                        else "Auto-corrected"
                    ),
                    severity="warn",
                    action="snap",
                    payload={"old": m.bullet_value, "new": m.auto_fix},
                )
            )
        else:  # L5b or unknown
            worst = "blocked"
            choices.append(
                UserFacingChoice(
                    label=f"{m.bullet_value}",
                    note=(
                        "你简历里完全没有这个数字 — 不能用，去掉或改写"
                        if is_zh
                        else "This number is not in your resume — remove or rewrite"
                    ),
                    severity="blocked",
                    action="remove",
                    payload={"value": m.bullet_value},
                )
            )
    if not choices:
        choices.append(
            UserFacingChoice(
                label=(
                    "这条没数字" if is_zh else "No numbers in this bullet"
                ),
                severity="ok",
                action="keep",
            )
        )
    return UserFacingDimension(label=label, choices=choices, severity=worst)


def _jd_keyword_dimension(
    d: BulletDecomposition, locale: str
) -> UserFacingDimension:
    label = _label(DIMENSION_LABELS, locale, "jd_keywords")
    actions = ACTION_LABELS.get(locale, ACTION_LABELS["en"])
    is_zh = locale.lower().startswith("zh")
    choices: list[UserFacingChoice] = []
    worst = "ok"
    for k in d.jd_keywords:
        if k.in_bullet and k.in_fact:
            choices.append(
                UserFacingChoice(
                    label=f"{k.keyword} ✓",
                    note=(
                        "JD 要的，简历里有，已经写进去了"
                        if is_zh
                        else "JD wants this; resume has it; already in bullet"
                    ),
                    severity="ok",
                    action="keep",
                    payload={"keyword": k.keyword},
                )
            )
        elif k.needs_interview:
            worst = "warn"
            choices.append(
                UserFacingChoice(
                    label=k.keyword,
                    note=(
                        f"JD 要 '{k.keyword}'，但你简历里找不到字面证据 — 回答 2 个问题升级简历"
                        if is_zh
                        else f"JD wants '{k.keyword}' but your resume doesn't show it — answer 2 questions"
                    ),
                    severity="warn",
                    action="interview",
                    payload={"keyword": k.keyword},
                )
            )
        elif k.in_bullet and not k.in_fact:
            # Already handled by needs_interview branch above (which is set
            # when in_bullet=True and in_fact=False). This branch is for
            # safety in case of mismatch.
            worst = "warn"
            choices.append(
                UserFacingChoice(
                    label=k.keyword,
                    note=(
                        "需要简历支持"
                        if is_zh
                        else "Needs resume backing"
                    ),
                    severity="warn",
                    action="interview",
                    payload={"keyword": k.keyword},
                )
            )
        else:
            # Keyword in JD but not in bullet — neutral, just FYI
            choices.append(
                UserFacingChoice(
                    label=k.keyword,
                    note=(
                        "JD 提到，但这一条没用上"
                        if is_zh
                        else "JD mentions; this bullet doesn't use it"
                    ),
                    severity="ok",
                    action="keep",
                    payload={"keyword": k.keyword},
                )
            )
    return UserFacingDimension(label=label, choices=choices, severity=worst)


def _length_dimension(d: BulletDecomposition, locale: str) -> UserFacingDimension:
    label = _label(DIMENSION_LABELS, locale, "length")
    label_text = LENGTH_LABELS.get(locale, LENGTH_LABELS["en"]).get(
        d.length.recommendation, d.length.recommendation
    )
    is_zh = locale.lower().startswith("zh")
    sev = "ok" if d.length.recommendation == "ok" else "warn"
    return UserFacingDimension(
        label=label,
        description=(
            f"当前 {d.length.words} 词" if is_zh else f"Currently {d.length.words} words"
        ),
        choices=[
            UserFacingChoice(label=label_text, severity=sev, action="keep")
        ],
        severity=sev,
    )


def _tone_dimension(d: BulletDecomposition, locale: str) -> UserFacingDimension:
    """Mirrors verb dimension but for tone candidates."""
    label = _label(DIMENSION_LABELS, locale, "tone")
    grounding_lbl = GROUNDING_LABELS.get(locale, GROUNDING_LABELS["en"])
    actions = ACTION_LABELS.get(locale, ACTION_LABELS["en"])
    is_zh = locale.lower().startswith("zh")

    choices: list[UserFacingChoice] = []
    worst = "ok"
    for v in d.tone_candidates:
        sev = GROUNDING_SEVERITY.get(v.grounding.level, "warn")
        if sev == "blocked":
            worst = "blocked"
        elif sev == "warn" and worst == "ok":
            worst = "warn"
        choices.append(
            UserFacingChoice(
                label=f"{actions['use']} '{v.text}'",
                note=grounding_lbl.get(v.grounding.level, v.grounding.level),
                severity=sev,
                action="use" if sev != "blocked" else "skip",
                payload={"tone": v.text, "rationale": v.grounding.rationale},
            )
        )
    if not choices:
        choices.append(
            UserFacingChoice(
                label=("AI 不管语气" if is_zh else "AI ignores tone"),
                severity="ok",
                action="skip",
            )
        )
    return UserFacingDimension(label=label, choices=choices, severity=worst)


# ─── Top-level ────────────────────────────────────────────


def to_user_facing(
    decomposition: BulletDecomposition,
    locale: str = "zh",
    *,
    expert_mode: bool = False,
) -> UserFacingMenu:
    """Convert decomposition → UserFacingMenu in the user's locale.

    Standard mode (default): everyday labels, hidden field names. The user sees
    "动作词 / 数字成果 / JD 想要的能力 / 长度 / 语气" and natural-language
    grounding notes.

    expert_mode=True: TODO — passes through raw decomposition fields. Not
    implemented in v4.0; UI reads decomposition_json directly when expert
    toggle is on.
    """
    if expert_mode:
        # Power-user view consumes raw decomposition; menu is meaningless here
        return UserFacingMenu(dimensions=[], has_warnings=False, has_blockers=False)

    dims = [
        _verb_dimension(decomposition, locale),
        _metric_dimension(decomposition, locale),
        _jd_keyword_dimension(decomposition, locale),
        _length_dimension(decomposition, locale),
        _tone_dimension(decomposition, locale),
    ]
    has_warnings = any(d.severity == "warn" for d in dims)
    has_blockers = any(d.severity == "blocked" for d in dims)
    return UserFacingMenu(
        dimensions=dims,
        has_warnings=has_warnings,
        has_blockers=has_blockers,
    )
