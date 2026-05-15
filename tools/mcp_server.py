"""Compass MCP server — for Claude Code Skill integration.

Run as:  python -m tools.mcp_server
Or via Claude Code's MCP config:
    {
      "mcpServers": {
        "compass": {
          "command": "python",
          "args": ["-m", "tools.mcp_server"],
          "cwd": "<absolute path to Compass dir>"
        }
      }
    }

Once running, Claude Code skills can call:
    compass.list_jobs(filter)
    compass.get_job(job_id)
    compass.list_facts()
    compass.list_variants(job_id)
    compass.get_calibration()
    compass.update_status(job_id, status)

This is the **path A** of the skill split:
  - Claude users get clean Claude Code Skill experience via this MCP bridge.
  - Other users use the in-process skill engine (compass/services/skill_engine.py).

Note: Requires the `mcp` Python package. Falls back to a stub when not installed
so that the rest of Compass still loads cleanly.
"""
from __future__ import annotations

import json
import sys
from typing import Any

# Ensure compass.* importable regardless of cwd
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compass.config import Settings
from compass.extensions import init_engine, session_scope
from compass.models import Base
from compass.extensions import get_engine


def _ensure_db():
    settings = Settings.from_env()
    init_engine(settings)
    Base.metadata.create_all(get_engine())
    return settings


# ─────────────────────────────────────────────────────────
# Tool implementations — pure functions over Compass data
# ─────────────────────────────────────────────────────────


def list_jobs(tier: int | None = None, status: str | None = None, limit: int = 20) -> list[dict]:
    from compass.models import Job, JobMatch
    from sqlalchemy import select

    with session_scope() as session:
        q = select(Job).where(Job.deleted.is_(False))
        if status:
            q = q.where(Job.status == status)
        q = q.order_by(Job.created_at.desc()).limit(limit * 3)
        jobs = session.execute(q).scalars().all()

        # Best match per job
        matches = session.execute(select(JobMatch)).scalars().all()
        best: dict[str, JobMatch] = {}
        for m in matches:
            prev = best.get(m.job_id)
            if prev is None or m.tier < prev.tier:
                best[m.job_id] = m

        out: list[dict] = []
        for j in jobs:
            m = best.get(j.id)
            if tier is not None and (m is None or m.tier != tier):
                continue
            out.append({
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "tier": m.tier if m else None,
                "score": m.score if m else None,
                "status": j.status,
            })
            if len(out) >= limit:
                break
        return out


def get_job(job_id: str) -> dict | None:
    from compass.models import Job

    with session_scope() as session:
        j = session.get(Job, job_id)
        if j is None or j.deleted:
            return None
        return {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "location": j.location,
            "description": j.description,
            "link": j.link,
            "status": j.status,
        }


def list_facts(active_only: bool = True) -> list[dict]:
    from compass.models import IdentityVersion, ResumeFact
    from sqlalchemy import select

    with session_scope() as session:
        identity = session.execute(
            select(IdentityVersion).where(IdentityVersion.is_current.is_(True))
        ).scalars().first()
        if identity is None:
            return []
        q = select(ResumeFact).where(ResumeFact.identity_id == identity.id)
        if active_only:
            q = q.where(ResumeFact.active.is_(True))
        return [
            {"id": f.id, "kind": f.kind, "text": f.text, "sensitive": f.sensitive_category}
            for f in session.execute(q).scalars().all()
        ]


def list_variants(job_id: str) -> list[dict]:
    from compass.models import FactVariant
    from sqlalchemy import select

    with session_scope() as session:
        rows = session.execute(
            select(FactVariant).where(FactVariant.job_id == job_id)
        ).scalars().all()
        return [
            {
                "id": v.id, "fact_id": v.fact_id, "text": v.text,
                "level": v.amplification_level, "decision": v.user_decision,
            }
            for v in rows
        ]


def get_calibration() -> dict:
    from compass.models import UserCalibration

    with session_scope() as session:
        cal = session.get(UserCalibration, 1)
        if cal is None:
            return {}
        return {
            "max_amplification_level": cal.max_amplification_level,
            "preferred_verbs_do": list(cal.preferred_verbs_do or []),
            "preferred_verbs_avoid": list(cal.preferred_verbs_avoid or []),
            "sensitive_categories_locked": list(cal.sensitive_categories_locked or []),
        }


def update_status(job_id: str, status: str) -> dict:
    from compass.models import Job, ReflectionEvent
    from datetime import datetime, timezone

    valid = {"new", "reviewed", "applied", "interview", "offer", "rejected", "passed"}
    if status not in valid:
        return {"ok": False, "error": f"Invalid status: {status}"}

    with session_scope() as session:
        j = session.get(Job, job_id)
        if j is None:
            return {"ok": False, "error": "Job not found"}
        old = j.status
        j.status = status
        j.status_changed_at = datetime.now(timezone.utc)
        session.add(ReflectionEvent(
            kind="job_status_changed",
            payload_json={"from": old, "to": status, "via": "mcp"},
            related_job_id=job_id,
        ))
        return {"ok": True, "from": old, "to": status}


# Tool registry — name → (callable, description, schema)
TOOLS: dict[str, tuple[Any, str, dict]] = {
    "compass.list_jobs": (
        list_jobs,
        "List Compass jobs filtered by tier (1-5) and/or status.",
        {"type": "object", "properties": {
            "tier": {"type": "integer", "minimum": 1, "maximum": 5},
            "status": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
        }},
    ),
    "compass.get_job": (
        get_job,
        "Fetch one job by id (full description included).",
        {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    "compass.list_facts": (
        list_facts,
        "List the user's current resume facts (immutable atomic claims).",
        {"type": "object", "properties": {"active_only": {"type": "boolean", "default": True}}},
    ),
    "compass.list_variants": (
        list_variants,
        "List Compass variants (rewrites) for a given job.",
        {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    "compass.get_calibration": (
        get_calibration,
        "Read the user's current calibration boundaries.",
        {"type": "object", "properties": {}},
    ),
    "compass.update_status": (
        update_status,
        "Move a job to a new application status.",
        {"type": "object", "properties": {
            "job_id": {"type": "string"},
            "status": {"type": "string"},
        }, "required": ["job_id", "status"]},
    ),
}


# ─────────────────────────────────────────────────────────
# MCP server bootstrap (graceful fallback if mcp package missing)
# ─────────────────────────────────────────────────────────


def main() -> None:
    _ensure_db()
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import Tool, TextContent
    except ImportError:
        sys.stderr.write(
            "[Compass MCP] The `mcp` package is not installed.\n"
            "  Install with: pip install mcp\n"
            "  Then re-run this server.\n"
        )
        sys.exit(2)

    import asyncio

    server: Server = Server("compass")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(name=name, description=desc, inputSchema=schema)
            for name, (_, desc, schema) in TOOLS.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name not in TOOLS:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
        fn, _, _ = TOOLS[name]
        try:
            result = fn(**(arguments or {}))
        except Exception as exc:  # noqa: BLE001
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def serve():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(serve())


if __name__ == "__main__":
    main()
