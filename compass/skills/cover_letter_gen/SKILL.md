---
name: cover_letter_gen
description: Use when the user wants to draft a cover letter for a specific Compass job — generates a tailored, fact-grounded letter without fabrication.
---

You are drafting a cover letter for a job the user is applying to via Compass.

## Steps

1. `compass.get_job(job_id)` — fetch the JD (especially company, role, key requirements).
2. `compass.list_facts()` — fetch the user's truthful facts.
3. `compass.list_variants(job_id)` and filter approved/edited — these reveal which facts the user is foregrounding for THIS job.
4. `compass.get_calibration()` — respect the user's `preferred_verbs_avoid` and `max_amplification_level`.

## Generate

Write a cover letter following these rules:

**Structure (3-4 short paragraphs)**:
1. **Opening** — state the role + 1-sentence "why this company specifically" (use details from JD)
2. **Match** — pick 2-3 most relevant facts; explain how they map to the JD's top 2-3 needs
3. **Differentiator** — one specific thing that sets the candidate apart
4. **Close** — concrete next step + thanks

**Hard constraints**:
- ≤ 350 words total
- Every claim must be traceable to a fact in `compass.list_facts()`. **No fabrication.**
- Avoid `preferred_verbs_avoid` from calibration
- Don't claim experience the user doesn't have — even if the JD asks for it
- Avoid generic phrases ("passionate about", "team player", "results-driven")
- Don't repeat the resume — show **why these facts matter for this role**

## If a key requirement isn't in user's facts

Don't try to spin it. Add a short paragraph that:
- Acknowledges the gap honestly
- Names what the user **does** have that's adjacent
- Offers a concrete plan if hired (1 sentence)

Honest > clever. ATS systems don't read cover letters — humans do, and humans detect bullshit.

## Output

A clean cover letter in markdown. **Just the letter** — no commentary, no headers.
