from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Disclosure
from kalshi_ai.domain.disclosures import CURRENT_DISCLOSURE_VERSION, DEFAULT_DISCLOSURES, DISCLOSURE_SLUGS
from kalshi_ai.services.audit import audit
from kalshi_ai_api.deps import CurrentUser, SessionDep

router = APIRouter(prefix="/api/disclosures", tags=["disclosures"])


@router.get("")
async def list_disclosures() -> list[dict[str, str]]:
    return [{"slug": slug, "title": DEFAULT_DISCLOSURES[slug]["title"]} for slug in DISCLOSURE_SLUGS]


@router.get("/{slug}")
async def get_disclosure(slug: str, session: SessionDep) -> dict[str, Any]:
    if slug not in DISCLOSURE_SLUGS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown disclosure")
    row = (
        await session.execute(
            select(Disclosure)
            .where(Disclosure.slug == slug, Disclosure.active.is_(True))
            .order_by(Disclosure.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return {"slug": slug, "version": row.version, "title": row.title, "body_markdown": row.body_markdown}
    default = DEFAULT_DISCLOSURES[slug]
    return {"slug": slug, "version": CURRENT_DISCLOSURE_VERSION, **default}


@router.post("/accept")
async def accept(user: CurrentUser, session: SessionDep) -> dict[str, str]:
    user.disclosures_accepted_at = utcnow()
    user.disclosures_version = CURRENT_DISCLOSURE_VERSION
    await audit(session, "disclosures.accepted", actor_user_id=user.id, details={"version": CURRENT_DISCLOSURE_VERSION})
    return {"accepted_version": CURRENT_DISCLOSURE_VERSION}
