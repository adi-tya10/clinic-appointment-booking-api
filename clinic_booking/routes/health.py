from fastapi import APIRouter
from sqlalchemy import text

from clinic_booking.db import SessionDep
from clinic_booking.schemas import error_responses

router = APIRouter(tags=["health"])


@router.get("/health", responses=error_responses(503), summary="Check API and database health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
