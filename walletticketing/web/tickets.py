"""Ticketansicht, QR-Code und Wallet-Downloads."""

from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import Ticket
from ..wallet import WalletNotConfigured, build_pass_data
from ..wallet.apple import PKPASS_MIME, ApplePassSigner, build_pkpass
from ..wallet.google import GoogleWalletSigner, build_objects
from .deps import get_db, get_settings, render

router = APIRouter(prefix="/t")


def _ticket(db: Session, token: str) -> Ticket:
    ticket = db.scalar(select(Ticket).where(Ticket.access_token == token))
    if ticket is None:
        raise HTTPException(404, "Ticket nicht gefunden")
    return ticket


def detect_platform(user_agent: str) -> str:
    ua = user_agent.lower()
    if any(k in ua for k in ("iphone", "ipad", "ipod", "macintosh")):
        return "apple"
    if "android" in ua:
        return "google"
    return ""


@router.get("/{token}")
def ticket_page(token: str, request: Request, db: Session = Depends(get_db)):
    ticket = _ticket(db, token)
    platform = detect_platform(request.headers.get("user-agent", ""))
    return render(request, "shop/ticket.html", ticket=ticket, platform=platform)


@router.get("/{token}/qr.svg")
def ticket_qr(token: str, db: Session = Depends(get_db)):
    ticket = _ticket(db, token)
    img = qrcode.make(ticket.code, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=300"})


@router.get("/{token}/apple.pkpass")
def apple_pass(token: str, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    ticket = _ticket(db, token)
    try:
        signer = ApplePassSigner.from_settings(settings)
    except WalletNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    data = build_pass_data(ticket, settings, utcnow())
    content = build_pkpass(data, settings, signer)
    return Response(
        content,
        media_type=PKPASS_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="ticket-{ticket.code}.pkpass"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{token}/google")
def google_pass(token: str, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    ticket = _ticket(db, token)
    try:
        signer = GoogleWalletSigner.from_settings(settings)
    except WalletNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    data = build_pass_data(ticket, settings, utcnow())
    payload = build_objects(data, settings, ticket.product.slug)
    return RedirectResponse(signer.save_url(payload, [settings.base_url]), status_code=303)
