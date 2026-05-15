# Compass

![License](https://img.shields.io/badge/license-Elastic%20v2-blue) ![Python](https://img.shields.io/badge/python-3.12%2B-blue) ![i18n](https://img.shields.io/badge/i18n-6%20languages-green) ![Status](https://img.shields.io/badge/status-production-brightgreen)

> A compass shows direction, not destination.

Compass is a **career reflection system** — not a job filter, resume rewriter, or auto-apply bot.

It is built to help you **see how you make career decisions** rather than make them for you. The success metric: three months in, you understand yourself better, and you may not need the tool anymore.

---

## Why not just another job aggregator

Existing tools optimize for retention through binary push/skip filtering, preference reinforcement (filter bubbles), and resume fabrication. Compass takes the opposite stance:

| Typical job tool | Compass |
|---|---|
| Filters jobs for you | You ask, the tool answers |
| Learns your preferences | Learns your boundaries |
| Shows you more jobs | Shows you yourself |
| Success = you keep using it | Success = you graduate |

---

## Architecture (4 layers)

```
┌────────────────────────────────────────────────────┐
│  Layer 4 — Reflection                              │
│  Drift dashboard / system assumptions / values     │
│  This is the soul of Compass, not an analytics tab │
├────────────────────────────────────────────────────┤
│  Layer 3 — Calibration (boundary learning)         │
│  Learns ethical boundaries, not preferences.       │
│  Built-in adversarial voice prevents sycophancy.   │
├────────────────────────────────────────────────────┤
│  Layer 2 — Generation                              │
│  Tier classification, interview-style fact mining, │
│  6-level claim grounding — never fabricates.       │
├────────────────────────────────────────────────────┤
│  Layer 1 — Identity                                │
│  Facts (immutable) + variants (mutable, grounded). │
│  Git model: facts = main, variants = branches.     │
└────────────────────────────────────────────────────┘
```

---

## Design principles

### 1. Facts immutable / variants mutable
Your facts (education, work history, skills) are the main branch — the LLM may never alter them. Each tailored resume is a variant; it may change *how* facts are presented, never the facts themselves.

### 2. 6-level transformation spectrum
```
L0 Verbatim                ← auto
L1 Paraphrase              ← auto
L2 Implicit → explicit     ← one-time user confirmation
L3 Perspective reframe     ← interview-led
L4 Inferential fill        ← user approval required
L5 Fabrication             ← hard block, never allowed
```
Credentials, years of experience, certificates, language levels, and salary are L5-deterministic-block. Everything else runs through L3 interview prompts.

### 3. 5-tier reachable circle (anti-bubble)
Recommendations are scoped to what is objectively reachable for you:
```
Tier 1 Core fit             70%
Tier 2 Adjacent pivot       20%   ← the anti-bubble sweet spot
Tier 3 Skill-bridgeable     10%   ← marked with explicit learning gap
Tier 4 Far transfer         only strong signals
Tier 5 Unreachable          never
```

### 4. Two-way feedback loop
LLM asks → you recall → system learns your boundaries → next prompt sharper. Every step is visible and contestable.

### 5. Counter-regulation (against reflective-flywheel runaway)
- **Drift dashboard** — review boundary movement monthly.
- **Adversarial voice** — never learns; final gatekeeper.
- **System assumptions** — six core beliefs the tool holds about you, all editable.
- **Values checkpoint** — forced re-review every three months.

---

## What it doesn't do

- ❌ Auto-apply (long-term negative EV)
- ❌ Resume fabrication (any L5 transform is hard-blocked)
- ❌ Silent data collection (you can inspect and delete everything learned)
- ❌ Retention-optimised UX
- ❌ Filter-bubble building (Tier model + ε-exploration + drift monitoring)

---

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head
cp .env.example .env       # fill in your Anthropic / OpenAI key
.venv/bin/python -m compass.run     # → http://127.0.0.1:7000
```

Or double-click `Double Click to Start_Mac.command` / `Double Click to Start_Windows.bat` — bootstrap will set up the venv, install deps, free the port, and open the browser.

**First-time flow:**
1. Visit `/jobs/llm-settings/` — paste API key, apply the **Balanced** preset.
2. Visit `/jobs/search-config/` — fill in `user_prompt` (drives tier classifier scoring).
3. Set `user_languages` and `dealbreakers.language_blocked` in the global config to filter out roles requiring languages you don't speak.

The UI ships in 6 languages (EN default, DE, FR, IT, ES, TR). The switcher is in the navbar — language preference is stored in a cookie, so it survives across visits.

---

## Feature overview

### Reflection layer (`/reflect`)
- **Drift dashboard** — calibration time series, ε-exploration monitor, stated-vs-revealed comparison.
- **🪞 Tier-quota reflexive distance** — gap between your declared tier_quota and the tier distribution you actually click / save / 👍.
- **System assumptions** — six core beliefs the LLM holds about you; each can be contested.
- **Values checkpoint** — periodic forced review of your boundaries.

### Job workflow (`/jobs` + `/funnel`)
- **Multi-profile scraping** — independent configs per region (keywords, hours_old, on/off toggle).
- **Cross-language keyword aliases** — LLM-suggested variants (💡 Suggest aliases button).
- **5-tier classification** — replaces push/skip binary with 70/20/10 ε-exploration quota.
- **Blacklist** — company + keyword filters (don't waste LLM tokens on noise).
- **Funnel tracking** — 7-stage conversion (scraped → classified → pushable → applied → interview → offer).
- **JD keyword coverage** — after a resume rewrite, hit keywords highlighted green, missing ones orange, with a coverage % at the top.

### Identity + resume (`/identity` + `/resume`)
- **Immutable facts / mutable variants** — the git model applied to resumes.
- **Interview-style discovery** — LLM helps you unearth facts; never fabricates.
- **6-level claim grounding** — L5 deterministic block on fabrication.
- **Adversarial voice** — a voice that never learns your preferences and acts as final gatekeeper.
- **JSON Resume export + HTML preview** — print via browser ⌘P → PDF; integrates with the RenderCV / open-source theme ecosystem.
- **L0–L5 risk overview** — rewrite review page shows amplification-level distribution per variant; L3+ pending items flagged red.

### Calibration + data loop (`/calibration` + `/gmail`)
- **Calibration learning** — slow drift (±1 per signal) + adversarial gate against sycophancy.
- **Gmail auto-status** — LLM classifies application emails → updates job status → closes the funnel loop.

### Extension surface (Claude Code Skill via MCP)
- Four reference skills: `ats_keyword_audit`, `cover_letter_gen`, `company_brief`, `monthly_reflection_report`.
- Six MCP tools exposed for Claude Code consumption.

Detailed in-page help is at the top of every page (the **💡 What is this / why use it / when** collapsible).

---

## Internationalisation

This is the `_intl` edition. The UI supports **English (default), German, French, Italian, Spanish, Turkish**. Translations live in `translations/` and are compiled to `.mo` via `pybabel compile -d translations`. Resume language for export is independent of UI language — the export menu lets you pick the target language per variant (with flag indicators 🇬🇧 🇩🇪 🇫🇷 🇮🇹 🇪🇸 🇹🇷 🇨🇳 etc.) and uses Compass's translation service.

---

## Philosophy

The metric is not DAU, not retention, not ARR.

It is: **how much clearer your understanding of yourself is three months from now compared to today**.

If after a while you say "I don't need this anymore" — that is the success case, not failure.

---

## Roadmap

- [x] **v1–v4** — Single-user reflection system (current)
  - 4-layer architecture, 6-level claim grounding, 5-tier reachable circle
  - Multi-profile job intent, JD keyword coverage, JSON Resume export
  - 6-language i18n, Claude Code Skill (MCP) extension surface
- [ ] **v5** — Cross-user anonymous benchmarks
  - Aggregate response rate / time-to-offer by background cluster
  - Peer-anchored tier_quota recommendations
- [ ] **v6** — ATS integration
  - Greenhouse / Lever / Workday / Ashby auto-status sync
  - Real hiring funnel data, not manual entry
- [ ] **v7** — Fine-tuned LLM on hiring outcomes
  - (resume revision → real outcome) pair dataset
  - Domain-specific judge that beats general LLMs on hiring decisions

> Single-user reflection design hits a ceiling at v4. v5–v7 require multi-user
> infrastructure and a community-scale dataset — the work shifts from solo
> product design to data flywheel building.

---

## License & Author

**Author:** Jiawen (Jiawen.Cc@outlook.com)
**License:** [Elastic License 2.0](LICENSE) — you may use, modify, and redistribute,
**except** you may not offer the software as a hosted/managed service that provides
users with substantial functionality of this software.
**Copyright:** © 2026 Jiawen. All rights reserved unless granted by the license above.

For commercial SaaS / hosted offerings, contact Jiawen.Cc@outlook.com for a separate
commercial agreement.
