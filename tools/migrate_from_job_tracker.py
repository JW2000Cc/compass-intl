"""One-shot migration: Job Tracker (v1) config.json → Compass data files.

Job Tracker stored everything in one big config.json:
    search.locations[]          → multiple region groups with keyword + alias maps
    matching.ai.user_prompt      → top-level matching preference text
    notification.channels.*      → Telegram / email config (skipped — Compass has no bot)
    alias_learning.*              → keyword aliases (per location)

This script extracts ONLY the user-meaningful fields (preferences, scrape config,
not API keys / tokens) and writes them into the Compass data dir as:

    data/last_scrape.json         — multi-group scrape config (drives jobs/scrape UI defaults
                                    AND conflict_detector knows what user "stated")
    data/preference_brief_jobs.json — extracted soft preferences from user_prompt
                                    (avoid X / 倾向 Y) — confidence pre-set to 0.85 since
                                    user explicitly typed them
    data/blacklist.json           — keywords explicitly rejected in user_prompt

API keys and tokens are NOT migrated — copy them by hand to .env.

Usage:
    python -m tools.migrate_from_job_tracker \\
        --v1-config "../Job Tracker/Job Tracker_Mac/config.json" \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────


def _args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--v1-config",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "Job Tracker" / "Job Tracker_Mac" / "config.json",
        help="Path to v1 config.json",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="Compass data dir (default: ../data relative to this script)",
    )
    p.add_argument("--dry-run", action="store_true", help="Print what would be written, don't touch disk")
    p.add_argument("--force", action="store_true", help="Overwrite existing files (default: skip if exists)")
    return p.parse_args()


# ─────────────────────────────────────────────────────────
# Translators
# ─────────────────────────────────────────────────────────


def translate_scrape_config(v1_locations: list[dict]) -> list[dict]:
    """v1 search.locations[] → Compass jobs/scrape multi-group format.

    v1 format (per location):
        {name, city, country, location, enabled, radius_km, keywords[],
         keyword_aliases: {kw: [variants]}, user_prompt, blacklist}

    Compass format (per group):
        {keywords: [...], locations: [...], hours_old: int, enabled: bool}

    We merge canonical keywords + their aliases into one flat keyword list per
    group — this gives the scraper maximum recall (all multi-language variants).
    """
    groups = []
    for loc in v1_locations:
        if not loc.get("enabled", True):
            continue
        canonical_kws = list(loc.get("keywords", []))
        alias_map = loc.get("keyword_aliases", {}) or {}
        flat_kws = list(canonical_kws)
        for kw, variants in alias_map.items():
            for v in variants:
                if v and v not in flat_kws:
                    flat_kws.append(v)

        location_str = loc.get("location") or f'{loc.get("city","")}, {loc.get("country","")}'.strip(", ")
        groups.append({
            "keywords": flat_kws,
            "locations": [location_str] if location_str else [],
            "hours_old": 168,  # 7 days default; user can adjust per-group in UI
            "enabled": True,
        })
    return groups


def extract_blacklist_keywords(user_prompts: list[str]) -> list[str]:
    """Pull explicit "不感兴趣" / "明确不感兴趣" / "exclude" terms from prompts.

    Conservative: only takes terms that come AFTER an explicit negative marker.
    Doesn't try to be smart — user can refine in /blacklist UI later.
    """
    bl: list[str] = []
    seen: set[str] = set()
    # Match patterns like: "不感兴趣的方向：X、Y、Z" or "exclude: X, Y" etc
    neg_markers = [
        r"明确不感兴趣的方向[：:]",
        r"不感兴趣的方向[：:]",
        r"不[要去想想]的[岗位职位方向]+[：:]",
        r"[Ee]xclude[:：]",
        r"[Aa]void[:：]",
    ]
    pattern = "|".join(neg_markers)
    for prompt in user_prompts:
        for m in re.finditer(rf"({pattern})\s*([^\n]+)", prompt):
            tail = m.group(2)
            # Split by Chinese / Latin separators
            parts = re.split(r"[、，,\s、]+", tail)
            for p in parts:
                p = p.strip(" 等。.()()/、，,").strip()
                # Drop trailing tokens like "等" / "等岗位" / "with"
                p = re.sub(r"(等|岗位|职位|.\w*\s+with.*)$", "", p).strip()
                if p and len(p) >= 2 and p not in seen:
                    seen.add(p)
                    bl.append(p)
    return bl


def extract_preference_lines(user_prompts: list[str], top_level_prompt: str) -> list[dict]:
    """Translate user_prompts into preference_brief_jobs lines (soft prefs).

    These are encoded as "+" lines (倾向) for things mentioned positively, and
    "-" lines (避开) for explicit rejections.

    Each line follows the brief schema:
        {id, text, side, axis, scope, confidence, confirmed_count,
         added_at, last_confirmed_at, expires_at, origin, confirmations}
    """
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=365)).isoformat()  # user-stated → long expiry
    now_iso = now.isoformat()

    def _new_line(text: str, side: str, axis: str, scope: str | None = None) -> dict:
        return {
            "id": uuid.uuid4().hex[:10],
            "text": text[:60],
            "side": side,
            "axis": axis,
            "scope": scope,
            "confidence": 0.85,
            "confirmed_count": 1,
            "added_at": now_iso,
            "last_confirmed_at": now_iso,
            "expires_at": expires,
            "origin": {
                "item_id": "migration",
                "item_label": "迁移自 Job Tracker config.json",
                "original_text": "用户在 v1 user_prompt 显式表述",
                "at": now_iso,
                "bullet_index": None,
            },
            "confirmations": [],
        }

    lines = []

    # Top-level prompt items — examine for explicit "+" patterns
    if top_level_prompt:
        if "薪资" in top_level_prompt or "市场平均水平" in top_level_prompt:
            lines.append(_new_line("倾向高于市场平均的薪资", "+", "comp"))
        if "远程" in top_level_prompt or "remote" in top_level_prompt.lower():
            lines.append(_new_line("倾向 remote / hybrid", "+", "remote"))
        if "纯坐班" in top_level_prompt or "office" in top_level_prompt.lower():
            lines.append(_new_line("避开纯坐班", "-", "remote"))
        if "Family Office" in top_level_prompt or "私人财富管理" in top_level_prompt:
            lines.append(_new_line(
                "倾向 Family Office / 私人财富管理 Data Analyst", "+", "industry",
                scope="for Data Analyst roles",
            ))
        if "Traineeship" in top_level_prompt or "Graduate Programme" in top_level_prompt:
            lines.append(_new_line(
                "可接受 Traineeship / Graduate Programme（不因低级别降分）",
                "+", "role",
            ))

    return lines


# ─────────────────────────────────────────────────────────
# Writers
# ─────────────────────────────────────────────────────────


def write_scrape_config(data_dir: Path, groups: list[dict], dry_run: bool, force: bool) -> Path | None:
    p = data_dir / "last_scrape.json"
    if p.exists() and not force:
        print(f"  [skip] {p.name} already exists (use --force to overwrite)")
        return None
    payload = {"groups": groups, "_migrated_from": "Job Tracker v1 config.json"}
    if dry_run:
        print(f"  [dry-run] would write {p.name}: {len(groups)} groups")
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ wrote {p.name} ({len(groups)} groups)")
    return p


def write_blacklist(data_dir: Path, keywords: list[str], dry_run: bool, force: bool) -> Path | None:
    p = data_dir / "blacklist.json"
    if p.exists() and not force:
        print(f"  [skip] {p.name} already exists (use --force to overwrite)")
        return None
    # Compass blacklist schema: flat lists of strings
    payload = {"companies": [], "keywords": sorted(set(keywords))}
    if dry_run:
        print(f"  [dry-run] would write {p.name}: {len(keywords)} blacklist keywords")
        for k in keywords[:8]:
            print(f"      · {k}")
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ wrote {p.name} ({len(keywords)} keywords)")
    return p


def write_preference_brief(data_dir: Path, lines: list[dict], dry_run: bool, force: bool) -> Path | None:
    p = data_dir / "preference_brief_jobs.json"
    if p.exists() and not force:
        print(f"  [skip] {p.name} already exists (use --force to overwrite)")
        return None
    # Detect canonical_lang from first line text — assume Chinese (user wrote in zh)
    payload = {
        "kind": "jobs",
        "canonical_lang": "zh",
        "lines": lines,
        "archive": [],
    }
    if dry_run:
        print(f"  [dry-run] would write {p.name}: {len(lines)} preference lines")
        for l in lines:
            print(f"      · [{l['side']}] {l['text']}" + (f"  scope: {l['scope']}" if l['scope'] else ""))
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ wrote {p.name} ({len(lines)} lines)")
    return p


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────


def main():
    args = _args()
    if not args.v1_config.exists():
        print(f"ERROR: v1 config not found at {args.v1_config}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading v1 config: {args.v1_config}")
    cfg = json.loads(args.v1_config.read_text(encoding="utf-8"))

    locations = cfg.get("search", {}).get("locations", []) or []
    top_level_prompt = cfg.get("matching", {}).get("ai", {}).get("user_prompt", "") or ""
    per_loc_prompts = [loc.get("user_prompt", "") for loc in locations if loc.get("user_prompt")]

    print(f"\nFound: {len(locations)} location groups + {len(per_loc_prompts)} per-loc prompts + top-level prompt {'present' if top_level_prompt else 'empty'}")

    # Translate
    groups = translate_scrape_config(locations)
    blacklist_kws = extract_blacklist_keywords(per_loc_prompts + [top_level_prompt])
    pref_lines = extract_preference_lines(per_loc_prompts, top_level_prompt)

    print("\n--- Translation summary ---")
    print(f"  scrape groups: {len(groups)}")
    for g in groups:
        loc = (g["locations"] or ["?"])[0]
        print(f"      · {loc}: {len(g['keywords'])} kws (incl aliases)")
    print(f"  blacklist keywords: {len(blacklist_kws)}")
    print(f"  preference brief lines: {len(pref_lines)}")

    print(f"\n--- Writing to {args.data_dir} {'(DRY RUN)' if args.dry_run else ''} ---")
    write_scrape_config(args.data_dir, groups, args.dry_run, args.force)
    write_blacklist(args.data_dir, blacklist_kws, args.dry_run, args.force)
    write_preference_brief(args.data_dir, pref_lines, args.dry_run, args.force)

    print("\nDone.")
    if not args.dry_run:
        print("\nNext steps (manual):")
        print("  1. Copy your Anthropic API key to Compass/.env (LLM_API_KEY=...)")
        print("  2. Open Compass → /jobs (你会看到迁移过来的 6 组 scrape 配置)")
        print("  3. Open Compass → /blacklist (review 迁移过来的不感兴趣方向)")
        print("  4. Open Compass → /feedback?tab=jobs (review preference brief)")
        print("  5. Upload your resume.pdf via /identity (this script doesn't touch DB)")


if __name__ == "__main__":
    main()
