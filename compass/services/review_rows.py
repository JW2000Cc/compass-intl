"""Helper for resume review pages — builds the (variant, fact, menu) row list.

Both resume.review and jobs.detail render the same `_review_panel.html`
template and need the same row shape: (FactVariant, ResumeFact | None,
UserFacingMenu | None).

Centralizing this in a helper:
  · ensures both routes attach the ADR-0015 decomposition menu identically
  · keeps `_decomposition_panel.html`-related logic in the services layer
    (template stays presentation-only)
  · the menu is None when the variant predates ADR-0015 改动 B
    (decomposition_json is empty) — template silently omits the panel
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from ..models import FactVariant, ResumeFact
from .bullet_decomposer import BulletDecomposition
from .user_facing_translator import UserFacingMenu, to_user_facing

log = logging.getLogger(__name__)


def build_review_rows(
    session: Session,
    variants: list[FactVariant],
    *,
    locale: str = "zh",
) -> list[tuple[FactVariant, Optional[ResumeFact], Optional[UserFacingMenu]]]:
    """Dedupe variants by fact_id (latest wins, GitHub-history-style),
    fetch the source fact, and attach a UserFacingMenu when decomposition
    data is available.
    """
    seen: set[str] = set()
    out: list[tuple[FactVariant, Optional[ResumeFact], Optional[UserFacingMenu]]] = []
    for v in variants:
        if v.fact_id in seen:
            continue
        seen.add(v.fact_id)
        src = session.get(ResumeFact, v.fact_id) if v.fact_id else None
        menu: Optional[UserFacingMenu] = None
        decomp_json = getattr(v, "decomposition_json", None)
        if decomp_json:
            try:
                d = BulletDecomposition.from_dict(decomp_json)
                menu = to_user_facing(d, locale=locale)
            except Exception as exc:  # noqa: BLE001
                log.debug("decomposition render failed for variant %s: %s", v.id, exc)
                menu = None
        out.append((v, src, menu))
    return out
