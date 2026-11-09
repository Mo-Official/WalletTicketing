"""JSON-API für Einlass-Scanner und Integrationen."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import Settings
from ..services import check_in
from .deps import get_db, get_settings

router = APIRouter(prefix="/api", tags=["api"])


def require_scanner(authorization: str = Header(default=""), settings: Settings = Depends(get_settings)) -> None:
    key = settings.scanner_api_key
    if not key:
        raise HTTPException(503, "Scanner-API ist nicht konfiguriert (SCANNER_API_KEY)")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, key):
        raise HTTPException(401, "Ungültiger API-Key")


class CheckInRequest(BaseModel):
    code: str


class CheckInResponse(BaseModel):
    ok: bool
    message: str
    product: str | None = None
    holder: str | None = None
    check_in_count: int | None = None


@router.post("/check-in", response_model=CheckInResponse, dependencies=[Depends(require_scanner)])
def api_check_in(body: CheckInRequest, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    result = check_in(db, body.code, settings)
    t = result.ticket
    return CheckInResponse(
        ok=result.ok,
        message=result.message,
        product=t.product.name if t else None,
        holder=t.holder_name if t else None,
        check_in_count=t.check_in_count if t else None,
    )
