"""Job scraper — thin wrapper around python-jobspy.

We borrow v1's hard-won knowledge:
  - Country mapping for Indeed (German/Italian/French/UK/etc.)
  - Stable URL hash for job IDs (md5 12-char prefix)
  - Pandas NaN scrubbing
  - Per-keyword × per-location iteration

But we don't import any v1 code — values flow through Compass's data model
(facts/identity/calibration), and every scrape appends a ReflectionEvent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from sqlalchemy.orm import Session

from ..models import Job, ReflectionEvent

log = logging.getLogger(__name__)

# jobspy 用 tls-client (Go cgo) 绕 LinkedIn TLS 指纹检测。LinkedIn 偶尔 drop
# TCP 不发 RST，tls-client 没设 read 超时，导致整个 scrape worker 永久挂在
# `__psynch_cvwait` —— 还连带卡住 _scrape_lock，后续任何抓取请求都被 flash
# "scrape running" 拦回。修法：起 daemon 线程跑 jobspy，主线程 join(timeout)；
# 超时就放弃这次 keyword，daemon 线程留在后台（进程退出时跟着死）。
# 不用 ThreadPoolExecutor —— 它 with-block 退出时强制 wait=True 等 future
# 完成，正好踩进我们想避免的死等。
SCRAPE_CALL_TIMEOUT_SEC = 90


# ── Progress status file ─────────────────────────────────────────────────────
# 写到 data/.scrape_progress.json，前端 /api/scrape/progress 读这个。
# 跨线程：scrape 线程写，HTTP 请求线程读。Werkzeug threaded=True 保证并发。
# 原子写：先写到 .tmp 再 os.replace，避免读到半截 JSON。
# 容错：读失败就当不存在（前端 polling 能容忍 404）。

_PROGRESS_FILE = ".scrape_progress.json"


def _progress_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / _PROGRESS_FILE


def write_progress(data_dir: Path | str, **patch) -> None:
    """Atomically merge `patch` into the progress file.

    Safe to call from inside the scrape thread. Each call updates
    `updated_at` so the frontend can show "X 秒前" if polling stalls.
    """
    p = _progress_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing.update(patch)
    existing["updated_at"] = time.time()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read_progress(data_dir: Path | str) -> dict | None:
    """Return current progress dict or None if not found / unreadable."""
    p = _progress_path(data_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_progress(data_dir: Path | str) -> None:
    """Mark scrape as inactive (keeps last totals so user can see final state).

    The flash banner from the route handler will be the user's main signal of
    "done"; this just makes the polling stop showing "still running".
    """
    p = _progress_path(data_dir)
    if not p.exists():
        return
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cur = {}
    cur["active"] = False
    cur["updated_at"] = time.time()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# Type alias for progress callback. Routes pass a closure that knows the data
# dir + current profile context; scraper.py invokes it at each phase boundary.
ProgressCB = Optional[Callable[..., None]]


def stable_job_id(url: str) -> str:
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:12]


_COUNTRY_INDEED = {
    "germany": ["german", "de", "deutschland"],
    "italy": ["ital", "it"],
    "france": ["france", "fr"],
    "uk": ["uk", "united kingdom", "gb"],
    "usa": ["usa", "united states", "us"],
    "canada": ["canada", "ca"],
    "spain": ["spain", "es"],
    "netherlands": ["netherlands", "nl"],
    "switzerland": ["switzerland", "ch"],
}


def country_for_indeed(country: str) -> str | None:
    c = (country or "").lower().strip()
    for canonical, variants in _COUNTRY_INDEED.items():
        if any(v in c for v in variants) or c == canonical:
            return canonical
    return None


def _scrub(v):
    if v is None:
        return None
    try:
        import pandas as pd

        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _row_to_job_payload(row: dict, default_source: str = "") -> dict | None:
    link = (row.get("JOB_URL") or row.get("job_url") or "").strip()
    if not link:
        return None
    title = row.get("TITLE") or row.get("title") or ""
    company = row.get("COMPANY") or row.get("company") or ""
    location = row.get("LOCATION") or row.get("location") or ""
    if isinstance(location, dict):
        parts = [str(location.get(k) or "") for k in ("city", "state", "country")]
        location = ", ".join(p for p in parts if p)
    desc = row.get("DESCRIPTION") or row.get("description") or ""
    site = (row.get("SITE") or row.get("site") or default_source).lower()
    is_remote = bool(row.get("IS_REMOTE") or row.get("is_remote") or False)
    posted_at = row.get("DATE_POSTED") or row.get("date_posted")
    salary_min = row.get("MIN_AMOUNT") or row.get("min_amount")
    salary_max = row.get("MAX_AMOUNT") or row.get("max_amount")
    currency = row.get("CURRENCY") or row.get("currency")

    return {
        "title": str(title or "")[:500],
        "company": str(company or "")[:300],
        "location": str(location or "")[:300],
        "link": link,
        "source": str(site or default_source or "")[:50],
        "posted_at": str(_scrub(posted_at)) if _scrub(posted_at) is not None else None,
        "description": str(desc or ""),
        "is_remote": is_remote,
        "salary_min": float(salary_min) if _scrub(salary_min) is not None else None,
        "salary_max": float(salary_max) if _scrub(salary_max) is not None else None,
        "salary_currency": str(_scrub(currency)) if _scrub(currency) is not None else None,
    }


def _iter_rows(df) -> Iterator[dict]:
    if df is None:
        return
    if hasattr(df, "to_dict"):
        yield from df.to_dict(orient="records")
    else:
        yield from list(df)


def scrape_one(
    *,
    site_names: Iterable[str],
    search_term: str,
    location: str,
    distance_km: int = 30,
    results_wanted: int = 20,
    country: str | None = None,
    fetch_full_description: bool = True,
    hours_old: int | None = None,
) -> tuple[list[dict], str | None]:
    """Run a single jobspy.scrape_jobs() call.

    Returns (payloads, error_message). On success error_message is None.
    On failure payloads is [] and error_message is a short human-readable
    string (for surfacing to the UI — silent failures are the real bug).
    """
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        raise RuntimeError("python-jobspy not installed; pip install python-jobspy") from exc

    sites = [s.lower() for s in site_names if s]
    if not sites:
        return [], None

    country_indeed = country_for_indeed(country or location)

    def _call_jobspy():
        return scrape_jobs(
            site_name=sites,
            search_term=search_term,
            location=location,
            distance=distance_km,
            results_wanted=results_wanted,
            country_indeed=country_indeed,
            description_format="markdown",
            linkedin_fetch_description=fetch_full_description and "linkedin" in sites,
            hours_old=hours_old,
            verbose=0,
        )

    holder: dict = {}

    def _runner():
        try:
            holder["df"] = _call_jobspy()
        except BaseException as e:  # noqa: BLE001 — propagate everything
            holder["err"] = e

    t = threading.Thread(target=_runner, daemon=True, name="jobspy")
    t.start()
    t.join(SCRAPE_CALL_TIMEOUT_SEC)
    if t.is_alive():
        # tls-client cgo goroutine 还卡在底层 read。daemon 线程会随主进程退出
        # 而被回收；这次当 raw=0 跳过，下一个 keyword 继续。
        msg = (f"{','.join(sites)}@{location!r} kw={search_term!r}: "
               f"timeout after {SCRAPE_CALL_TIMEOUT_SEC}s "
               f"(jobspy/tls-client likely hung)")
        log.warning("scrape_jobs timed out %s", msg)
        return [], msg

    try:
        if "err" in holder:
            raise holder["err"]
        df = holder["df"]
    except Exception as exc:  # noqa: BLE001
        # Surface the failure to callers — UI must show "jobspy 失败 @ X" not
        # "新增 0" silence. See B1 in routes/jobs.py: errors flow through
        # run_scrape → flash banner.
        msg = f"{','.join(sites)}@{location!r} kw={search_term!r}: {type(exc).__name__}: {str(exc)[:120]}"
        log.warning("scrape_jobs failed %s", msg)
        return [], msg

    rows = list(_iter_rows(df))
    log.info("scraped %d raw rows for sites=%s search=%r loc=%r", len(rows), sites, search_term, location)

    out = []
    for r in rows:
        p = _row_to_job_payload(r)
        if p:
            out.append(p)
    return out, None


def merge_into_db(
    session: Session,
    payloads: list[dict],
    *,
    blacklist: dict | None = None,
    profile=None,           # ADR-0016: ProfileConfig — runs dealbreaker_gate per payload
    global_intent=None,     # ADR-0016: GlobalIntent — global excludes/languages
) -> tuple[int, int, int, int]:
    """Insert new jobs / lightly update existing.

    Returns (new, updated, blocked_by_blacklist, blocked_by_gate).
    blacklist: {'companies': [...], 'keywords': [...]} (legacy soft filter, case-insensitive)
    profile / global_intent: when provided (ADR-0016), each payload runs through
        dealbreaker_gate.check_job. Failures write a ReflectionEvent('hard_gate_blocked')
        and skip merge — the job never enters the DB. New jobs get
        Job.matched_profile_id = profile.id so downstream classifiers know the context.

    Dedup (5-08 fix for IntegrityError on multi-profile scrape):
      session.get() does NOT see pending-but-unflushed objects. When two profiles
      hit the same job URL within the same session_scope (e.g. Frankfurt + Berlin
      both surfacing "Senior EE @ Big Co · Germany" via their respective LinkedIn
      queries), each got existing=None and called session.add() → bulk INSERT at
      commit time hit UNIQUE constraint. Fix: flush after this call so the next
      merge_into_db sees previously-added rows; also dedupe within the same call
      via session.new walk.
    """
    from .blacklist import is_blacklisted
    from ..models import ReflectionEvent

    n_new = n_upd = n_blocked = n_gate_blocked = 0
    now = datetime.now(timezone.utc)

    def _job_pending_in_session(jid: str) -> bool:
        # session.new is the set of pending (added, not yet flushed) instances
        for obj in session.new:
            if isinstance(obj, Job) and obj.id == jid:
                return True
        return False

    for p in payloads:
        if blacklist:
            blocked, reason = is_blacklisted(
                p.get("company"), p.get("description"), blacklist=blacklist
            )
            if blocked:
                n_blocked += 1
                log.debug("scrape: dropped %s — %s", p.get("link"), reason)
                continue

        # ADR-0016 hard gate — runs only if profile context is provided
        if profile is not None:
            from .dealbreaker_gate import check_job

            decision = check_job(p, profile, global_=global_intent)
            if not decision.allow:
                n_gate_blocked += 1
                session.add(
                    ReflectionEvent(
                        kind="hard_gate_blocked",
                        payload_json=decision.to_event_payload(p.get("link") or "?"),
                    )
                )
                log.debug(
                    "scrape: gate-dropped %s for profile %s — %s",
                    p.get("link"),
                    profile.id,
                    decision.blocked_by[:1],
                )
                continue

        jid = stable_job_id(p["link"])
        existing = session.get(Job, jid)
        # session.get misses pending-not-yet-flushed objects → walk session.new
        if existing is None and _job_pending_in_session(jid):
            # already queued for insert in this same session by an earlier
            # merge_into_db call (or earlier iteration of this one); count as
            # update so the funnel reflects "saw it twice", and skip add.
            n_upd += 1
            continue
        if existing is None:
            session.add(
                Job(
                    id=jid,
                    title=p["title"],
                    company=p["company"],
                    location=p.get("location"),
                    link=p["link"],
                    source=p.get("source"),
                    description=p.get("description"),
                    posted_at=p.get("posted_at"),
                    is_remote=p.get("is_remote", False),
                    salary_min=p.get("salary_min"),
                    salary_max=p.get("salary_max"),
                    salary_currency=p.get("salary_currency"),
                    matched_profile_id=profile.id if profile else None,
                    created_at=now,
                )
            )
            n_new += 1
        else:
            # Only refresh fields that improve the row — never overwrite with empties
            existing.description = p.get("description") or existing.description
            existing.posted_at = p.get("posted_at") or existing.posted_at
            existing.salary_min = p.get("salary_min") or existing.salary_min
            existing.salary_max = p.get("salary_max") or existing.salary_max
            existing.salary_currency = p.get("salary_currency") or existing.salary_currency
            # Lazy backfill: a job recalled by a profile this round should be
            # tagged so future tier classification picks the right profile.
            if profile and not existing.matched_profile_id:
                existing.matched_profile_id = profile.id
            n_upd += 1
    # Flush so newly-added Job rows enter the identity map before the next
    # merge_into_db call (different keyword / different profile) checks
    # session.get for the same URL. Without this, multi-profile scrapes
    # collide at commit-time with UNIQUE constraint failed on jobs.id.
    if n_new:
        try:
            session.flush()
        except Exception:
            log.exception("flush after merge_into_db failed")
            raise
    return n_new, n_upd, n_blocked, n_gate_blocked


def run_scrape(
    session: Session,
    *,
    keywords: list[str],
    locations: list[str],
    sites: list[str] | None = None,
    results_per_keyword: int = 20,
    hours_old: int | None = None,
    blacklist: dict | None = None,
    profile=None,           # ADR-0016
    global_intent=None,     # ADR-0016
    progress_cb: ProgressCB = None,  # 进度回调，每个 keyword 进入/退出调一次
) -> dict:
    """High-level scrape runner — iterates keywords × locations.

    hours_old: only return jobs posted in the last N hours (e.g. 168 = 1 week).
               None = no time filter (jobspy default).
    blacklist: {'companies': [...], 'keywords': [...]} for filtering at merge time.
    profile / global_intent: ADR-0016 — when provided, every merge step runs
        dealbreaker_gate. See merge_into_db for semantics.
    progress_cb: 可选回调。被调时机：每条 keyword 开始前（kind="keyword_start"）
        和结束后（kind="keyword_done"）。kwargs 包括 keyword/location/index/total
        + 当前 profile 累计 totals。route handler 用它把信息写到 progress 文件。

    Appends a `external_scrape` ReflectionEvent so funnel can track inflow.
    """
    sites = sites or ["linkedin", "indeed"]
    total_raw = total_new = total_upd = total_blocked = total_gate_blocked = 0
    per: list[dict] = []
    errors: list[str] = []
    total_kw_steps = len(locations) * len(keywords)
    kw_step_idx = 0
    for loc in locations:
        for kw in keywords:
            kw_step_idx += 1
            if progress_cb:
                try:
                    progress_cb(
                        kind="keyword_start",
                        keyword=kw, location=loc, sites=sites,
                        keyword_index=kw_step_idx, keyword_total=total_kw_steps,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("progress_cb keyword_start failed")
            payloads, err = scrape_one(
                site_names=sites,
                search_term=kw,
                location=loc,
                results_wanted=results_per_keyword,
                hours_old=hours_old,
            )
            if err:
                errors.append(err)
            raw_count = len(payloads)
            total_raw += raw_count
            n_new, n_upd, n_blocked, n_gate = merge_into_db(
                session,
                payloads,
                blacklist=blacklist,
                profile=profile,
                global_intent=global_intent,
            )
            total_new += n_new
            total_upd += n_upd
            total_blocked += n_blocked
            total_gate_blocked += n_gate
            per.append({
                "keyword": kw, "location": loc,
                "raw": raw_count,
                "new": n_new, "updated": n_upd,
                "blocked": n_blocked, "gate_blocked": n_gate,
            })
            if progress_cb:
                try:
                    progress_cb(
                        kind="keyword_done",
                        keyword=kw, location=loc,
                        keyword_index=kw_step_idx, keyword_total=total_kw_steps,
                        raw=raw_count, new=n_new, updated=n_upd,
                        blocked=n_blocked, gate_blocked=n_gate,
                        had_error=bool(err),
                        # Cumulative for this profile so far:
                        profile_totals={
                            "raw": total_raw, "new": total_new, "updated": total_upd,
                            "blocked": total_blocked, "gate_blocked": total_gate_blocked,
                            "errors": len(errors),
                        },
                    )
                except Exception:  # noqa: BLE001
                    log.exception("progress_cb keyword_done failed")

    session.add(
        ReflectionEvent(
            kind="external_scrape",
            payload_json={
                "raw": total_raw,
                "new": total_new, "updated": total_upd,
                "blocked": total_blocked, "gate_blocked": total_gate_blocked,
                "profile_id": profile.id if profile else None,
                "per": per,
                "errors": errors,
            },
        )
    )
    return {
        "raw": total_raw,
        "new": total_new,
        "updated": total_upd,
        "blocked": total_blocked,
        "gate_blocked": total_gate_blocked,
        "per": per,
        "errors": errors,
    }


def run_scrape_for_profile(
    session: Session,
    profile,                    # ProfileConfig
    *,
    global_intent=None,         # GlobalIntent
    sites: list[str] | None = None,
    results_per_keyword: int = 20,
    hours_old: int | None = None,
    blacklist: dict | None = None,
    progress_cb: ProgressCB = None,
) -> dict:
    """ADR-0016 entry point: run a scrape constrained to one profile's intent.

    Convenience wrapper — derives keywords / locations from the profile so callers
    in routes/jobs.py don't have to splat them out manually.
    """
    _empty = {"raw": 0, "new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0, "per": [], "errors": []}
    if not profile.enabled:
        return dict(_empty)
    location = profile.location_string
    if not location or not profile.keywords:
        log.warning(
            "skip profile %s: location='%s' keywords=%d",
            profile.id, location, len(profile.keywords),
        )
        return {**_empty, "errors": [f"profile {profile.id} skipped: location/keywords empty"]}
    # ADR-0016 补丁 A: per-profile hours_old. Explicit caller-provided
    # hours_old still wins for ad-hoc overrides; otherwise use profile's.
    effective_hours_old = hours_old if hours_old is not None else profile.hours_old
    return run_scrape(
        session,
        keywords=profile.keywords,
        locations=[location],
        sites=sites,
        results_per_keyword=results_per_keyword,
        hours_old=effective_hours_old,
        blacklist=blacklist,
        profile=profile,
        global_intent=global_intent,
        progress_cb=progress_cb,
    )
