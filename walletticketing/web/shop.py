"""Öffentliches Frontend: Landing Pages, Checkout, Zahlungsrückkehr, Bestellübersicht."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import LandingPage, Order, OrderStatus, ValidityType
from ..payments import CheckoutUrls, PaymentError, PaymentRegistry, PaymentState
from ..services import LineRequest, OrderError, cancel_order, create_order, is_on_sale, mark_paid, remaining_stock
from .deps import check_csrf, flash, get_db, get_payments, get_settings, render

log = logging.getLogger(__name__)
router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _published_page(db: Session, slug: str) -> LandingPage:
    page = db.scalar(select(LandingPage).where(LandingPage.slug == slug, LandingPage.published.is_(True)))
    if page is None:
        raise HTTPException(404, "Seite nicht gefunden")
    return page


def _order_by_token(db: Session, token: str) -> Order:
    order = db.scalar(select(Order).where(Order.public_token == token))
    if order is None:
        raise HTTPException(404, "Bestellung nicht gefunden")
    return order


@router.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    pages = db.scalars(
        select(LandingPage).where(LandingPage.published.is_(True)).order_by(LandingPage.created_at.desc())
    ).all()
    return render(request, "shop/index.html", pages=pages)


def _landing_context(db: Session, page: LandingPage, payments: PaymentRegistry, settings: Settings) -> dict:
    now = utcnow()
    offers = []
    for product in page.products:
        if not is_on_sale(product, now):
            continue
        remaining = remaining_stock(db, product)
        min_date = max(now.astimezone(settings.tz).date(), product.valid_from.astimezone(settings.tz).date()) if (
            product.valid_from
        ) else now.astimezone(settings.tz).date()
        max_date = product.valid_until.astimezone(settings.tz).date() if product.valid_until else None
        offers.append(
            {
                "product": product,
                "remaining": remaining,
                "max_qty": min(product.max_per_order, remaining) if remaining is not None else product.max_per_order,
                "needs_date": product.validity_type == ValidityType.DAY_OF_CHOICE,
                "min_date": min_date.isoformat(),
                "max_date": max_date.isoformat() if max_date else "",
            }
        )
    return {"page": page, "offers": offers, "providers": payments.for_page(page)}


@router.get("/l/{slug}")
def landing_page(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    payments: PaymentRegistry = Depends(get_payments),
    settings: Settings = Depends(get_settings),
):
    page = _published_page(db, slug)
    return render(request, "shop/landing.html", form={}, error=None, **_landing_context(db, page, payments, settings))


@router.post("/l/{slug}/checkout")
async def checkout(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    payments: PaymentRegistry = Depends(get_payments),
    settings: Settings = Depends(get_settings),
):
    page = _published_page(db, slug)
    form = await request.form()
    check_csrf(request, form)

    def fail(message: str):
        db.rollback()
        ctx = _landing_context(db, page, payments, settings)
        return render(request, "shop/landing.html", status_code=400, form=dict(form), error=message, **ctx)

    method = str(form.get("payment_method", ""))
    provider = payments.get(method)
    if provider is None or provider not in payments.for_page(page):
        return fail("Bitte wählen Sie eine Zahlungsart")

    lines: list[LineRequest] = []
    for product in page.products:
        raw_qty = str(form.get(f"qty_{product.id}", "0") or "0")
        try:
            qty = int(raw_qty)
        except ValueError:
            return fail("Ungültige Anzahl")
        if qty < 0:
            return fail("Ungültige Anzahl")
        visit_date = None
        raw_date = str(form.get(f"date_{product.id}", "") or "")
        if qty and product.validity_type == ValidityType.DAY_OF_CHOICE:
            try:
                visit_date = date.fromisoformat(raw_date)
            except ValueError:
                return fail(f"Bitte wählen Sie einen Besuchstag für '{product.name}'")
        lines.append(LineRequest(product, qty, visit_date))

    try:
        order = create_order(
            db,
            settings,
            page=page,
            customer_name=str(form.get("name", "")),
            customer_email=str(form.get("email", "")),
            lines=lines,
            payment_method=provider.code,
        )
        base = f"{settings.base_url}/checkout/{order.public_token}"
        urls = CheckoutUrls(
            return_url=f"{base}/return",
            cancel_url=f"{base}/cancel",
            order_url=f"{settings.base_url}/order/{order.public_token}",
        )
        redirect_url = provider.start(order, urls)
    except OrderError as exc:
        return fail(str(exc))
    except PaymentError:
        log.exception("Zahlung konnte nicht gestartet werden")
        return fail("Die Zahlung konnte nicht gestartet werden. Bitte versuchen Sie es erneut oder wählen Sie eine andere Zahlungsart.")
    db.commit()
    return _redirect(redirect_url)


def _apply_result(db: Session, order: Order, state: PaymentState, settings: Settings, request: Request | None = None):
    if state == PaymentState.PAID:
        mark_paid(db, order, settings)
        if request:
            flash(request, "Vielen Dank! Ihre Zahlung ist eingegangen.", "success")
    elif state == PaymentState.FAILED:
        if order.status == OrderStatus.PENDING:
            cancel_order(db, order, OrderStatus.CANCELLED)
        if request:
            flash(request, "Die Zahlung ist fehlgeschlagen.", "error")
    elif request:
        flash(request, "Ihre Zahlung wird noch bearbeitet. Die Tickets erscheinen hier nach Zahlungseingang.", "info")


@router.get("/checkout/{token}/return")
def payment_return(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    payments: PaymentRegistry = Depends(get_payments),
    settings: Settings = Depends(get_settings),
):
    order = _order_by_token(db, token)
    if order.status == OrderStatus.PENDING or order.status == OrderStatus.EXPIRED:
        provider = payments.providers.get(order.payment_method)
        if provider:
            try:
                result = provider.confirm(order, request.query_params)
            except PaymentError:
                log.exception("Zahlungsbestätigung fehlgeschlagen")
                flash(request, "Der Zahlungsstatus konnte nicht abgefragt werden. Bitte laden Sie die Seite später neu.", "error")
            else:
                _apply_result(db, order, result.state, settings, request)
    return _redirect(f"/order/{order.public_token}")


@router.get("/checkout/{token}/cancel")
def payment_cancel(token: str, request: Request, db: Session = Depends(get_db)):
    order = _order_by_token(db, token)
    if order.status == OrderStatus.PENDING:
        cancel_order(db, order, OrderStatus.CANCELLED)
        flash(request, "Die Zahlung wurde abgebrochen.", "info")
    slug = order.landing_page.slug if order.landing_page else None
    return _redirect(f"/l/{slug}" if slug else "/")


@router.get("/checkout/{token}/demo")
def demo_payment_page(token: str, request: Request, db: Session = Depends(get_db), payments: PaymentRegistry = Depends(get_payments)):
    order = _order_by_token(db, token)
    if order.payment_method != "demo" or payments.get("demo") is None:
        raise HTTPException(404)
    return render(request, "shop/demo_pay.html", order=order)


@router.post("/checkout/{token}/demo")
async def demo_payment_submit(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    payments: PaymentRegistry = Depends(get_payments),
    settings: Settings = Depends(get_settings),
):
    order = _order_by_token(db, token)
    if order.payment_method != "demo" or payments.get("demo") is None:
        raise HTTPException(404)
    form = await request.form()
    check_csrf(request, form)
    if order.status in (OrderStatus.PENDING, OrderStatus.EXPIRED):
        state = PaymentState.PAID if form.get("action") == "pay" else PaymentState.FAILED
        _apply_result(db, order, state, settings, request)
    return _redirect(f"/order/{order.public_token}")


@router.post("/webhooks/{provider_code}")
async def payment_webhook(
    provider_code: str,
    request: Request,
    db: Session = Depends(get_db),
    payments: PaymentRegistry = Depends(get_payments),
    settings: Settings = Depends(get_settings),
):
    provider = payments.get(provider_code)
    if provider is None:
        raise HTTPException(404)
    body = await request.body()
    try:
        event = provider.parse_webhook(body, {k.lower(): v for k, v in request.headers.items()})
    except PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc
    if event is None:
        return Response(status_code=204)
    order = db.scalar(
        select(Order).where(Order.payment_method == provider.code, Order.payment_reference == event.reference)
    )
    if order is None:
        return Response(status_code=204)
    if order.status in (OrderStatus.PENDING, OrderStatus.EXPIRED):
        _apply_result(db, order, event.result.state, settings)
    return Response(status_code=204)


@router.get("/order/{token}")
def order_page(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    payments: PaymentRegistry = Depends(get_payments),
):
    order = _order_by_token(db, token)
    provider = payments.providers.get(order.payment_method)
    instructions = (
        provider.customer_instructions(order) if provider and order.status == OrderStatus.PENDING else None
    )
    return render(request, "shop/order.html", order=order, provider=provider, instructions=instructions)
