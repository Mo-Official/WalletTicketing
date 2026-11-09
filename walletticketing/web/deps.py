from __future__ import annotations

import hmac
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..payments import PaymentRegistry
from ..validity import PHASE_LABELS, format_window, ticket_phase

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


class NotAuthenticated(Exception):
    pass


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_payments(request: Request) -> PaymentRegistry:
    return request.app.state.payments


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


# --- CSRF -------------------------------------------------------------------


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def check_csrf(request: Request, form: Any) -> None:
    expected = request.session.get("csrf", "")
    sent = str(form.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(expected, sent):
        raise HTTPException(status_code=400, detail="Ungültiges Formular-Token. Bitte Seite neu laden.")


# --- Flash-Nachrichten ------------------------------------------------------


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("_flashes", []).append([category, message])


def pop_flashes(request: Request) -> list[list[str]]:
    return request.session.pop("_flashes", [])


# --- Admin-Auth -------------------------------------------------------------


def require_admin(request: Request) -> str:
    user = request.session.get("admin")
    if not user:
        raise NotAuthenticated()
    return user


# --- Templates --------------------------------------------------------------


def money(cents: int | None, currency: str = "EUR") -> str:
    if cents is None:
        return "–"
    value = f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    symbol = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}.get(currency, currency)
    return f"{value} {symbol}"


def paragraphs(text: str) -> Markup:
    blocks = [b.strip() for b in (text or "").replace("\r\n", "\n").split("\n\n") if b.strip()]
    return Markup("".join(f"<p>{escape(b).replace(chr(10), Markup('<br>'))}</p>" for b in blocks))


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    env = templates.env
    env.globals.update(
        settings=settings,
        csrf_token=csrf_token,
        pop_flashes=pop_flashes,
        now=utcnow,
        phase_of=lambda ticket: ticket_phase(ticket, utcnow()),
        phase_labels=PHASE_LABELS,
    )
    env.filters["money"] = money
    env.filters["paragraphs"] = paragraphs
    env.filters["window"] = lambda t: format_window(t.valid_from, t.valid_until, settings.tz)
    env.filters["local"] = lambda dt, fmt="%d.%m.%Y %H:%M": (
        dt.astimezone(settings.tz).strftime(fmt) if isinstance(dt, datetime) else ""
    )
    return templates


def render(request: Request, name: str, status_code: int = 200, **context: Any):
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, name, context, status_code=status_code)
