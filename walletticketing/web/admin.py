"""Backend: Produkte, Landing Pages, Bestellungen und Einlasskontrolle verwalten."""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import LandingPage, Order, OrderStatus, Product, Ticket, ValidityType, WalletStyle
from ..payments import PaymentRegistry
from ..services import OrderError, cancel_order, check_in, mark_paid, sold_quantity
from .deps import check_csrf, flash, get_db, get_payments, get_settings, render, require_admin

router = APIRouter()
protected = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def slugify(value: str) -> str:
    value = value.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


# --- Login ------------------------------------------------------------------


@router.get("/admin/login")
def login_form(request: Request, next: str = "/admin"):
    return render(request, "admin/login.html", next=next, error=None)


@router.post("/admin/login")
async def login(request: Request, settings: Settings = Depends(get_settings)):
    form = await request.form()
    check_csrf(request, form)
    user = str(form.get("username", ""))
    pw = str(form.get("password", ""))
    next_url = str(form.get("next", "/admin"))
    if not next_url.startswith("/admin"):
        next_url = "/admin"
    if hmac.compare_digest(user, settings.admin_username) and hmac.compare_digest(pw, settings.admin_password):
        request.session["admin"] = user
        return _redirect(next_url)
    return render(request, "admin/login.html", status_code=401, next=next_url, error="Login fehlgeschlagen")


@router.post("/admin/logout")
async def logout(request: Request):
    form = await request.form()
    check_csrf(request, form)
    request.session.pop("admin", None)
    return _redirect("/admin/login")


# --- Dashboard --------------------------------------------------------------


@protected.get("")
def dashboard(request: Request, db: Session = Depends(get_db), payments: PaymentRegistry = Depends(get_payments)):
    since = utcnow() - timedelta(days=30)
    stats = {
        "revenue_30d": db.scalar(
            select(func.coalesce(func.sum(Order.total_cents), 0)).where(
                Order.status == OrderStatus.PAID, Order.paid_at >= since
            )
        ),
        "orders_paid": db.scalar(select(func.count()).where(Order.status == OrderStatus.PAID)),
        "orders_pending": db.scalar(select(func.count()).where(Order.status == OrderStatus.PENDING)),
        "tickets": db.scalar(select(func.count(Ticket.id))),
        "products": db.scalar(select(func.count(Product.id))),
        "pages": db.scalar(select(func.count(LandingPage.id))),
    }
    recent = db.scalars(select(Order).order_by(Order.created_at.desc()).limit(10)).all()
    return render(request, "admin/dashboard.html", stats=stats, recent=recent, providers=payments.available())


# --- Produkte ---------------------------------------------------------------


def _parse_dt(value: str, settings: Settings) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=settings.tz)


def _fmt_dt(dt: datetime | None, settings: Settings) -> str:
    return dt.astimezone(settings.tz).strftime("%Y-%m-%dT%H:%M") if dt else ""


def _product_form_values(product: Product, settings: Settings) -> dict:
    return {
        "name": product.name or "",
        "slug": product.slug or "",
        "description": product.description or "",
        "venue": product.venue or "",
        "price": f"{(product.price_cents or 0) / 100:.2f}",
        "currency": product.currency or settings.default_currency,
        "validity_type": (product.validity_type or ValidityType.FIXED).value,
        "valid_from": _fmt_dt(product.valid_from, settings),
        "valid_until": _fmt_dt(product.valid_until, settings),
        "validity_days": product.validity_days or "",
        "stock": "" if product.stock is None else product.stock,
        "max_per_order": product.max_per_order or 10,
        "multi_entry": bool(product.multi_entry),
        "wallet_style": (product.wallet_style or WalletStyle.AUTO).value,
        "color": product.color or "#1f4e79",
        "active": True if product.active is None else product.active,
    }


def _apply_product_form(db: Session, product: Product, form, settings: Settings) -> list[str]:
    errors: list[str] = []
    name = str(form.get("name", "")).strip()
    slug = str(form.get("slug", "")).strip() or slugify(name)
    if not name:
        errors.append("Name fehlt")
    if not SLUG_RE.match(slug):
        errors.append("Slug darf nur Kleinbuchstaben, Ziffern und Bindestriche enthalten")
    elif db.scalar(select(Product.id).where(Product.slug == slug, Product.id != (product.id or 0))):
        errors.append("Slug ist bereits vergeben")

    try:
        price = Decimal(str(form.get("price", "0")).replace(",", "."))
        if price < 0:
            raise InvalidOperation
        price_cents = int((price * 100).quantize(Decimal("1")))
    except InvalidOperation:
        errors.append("Ungültiger Preis")
        price_cents = 0

    try:
        validity_type = ValidityType(str(form.get("validity_type", "fixed")))
        wallet_style = WalletStyle(str(form.get("wallet_style", "auto")))
    except ValueError:
        errors.append("Ungültige Auswahl")
        validity_type, wallet_style = ValidityType.FIXED, WalletStyle.AUTO

    try:
        valid_from = _parse_dt(str(form.get("valid_from", "")), settings)
        valid_until = _parse_dt(str(form.get("valid_until", "")), settings)
    except ValueError:
        errors.append("Ungültiges Datum")
        valid_from = valid_until = None

    def _int(field: str, minimum: int, optional: bool = True) -> int | None:
        raw = str(form.get(field, "")).strip()
        if not raw:
            return None if optional else minimum
        try:
            val = int(raw)
            if val < minimum:
                raise ValueError
            return val
        except ValueError:
            errors.append(f"Ungültiger Wert für {field}")
            return None

    validity_days = _int("validity_days", 1)
    stock = _int("stock", 0)
    max_per_order = _int("max_per_order", 1, optional=False) or 10

    if validity_type == ValidityType.FIXED and not (valid_from and valid_until):
        errors.append("Bei festem Zeitraum müssen Beginn und Ende angegeben werden")
    if validity_type == ValidityType.DURATION and not validity_days:
        errors.append("Bei Zeitkarten muss die Gültigkeitsdauer in Tagen angegeben werden")
    if valid_from and valid_until and valid_until <= valid_from:
        errors.append("Ende muss nach dem Beginn liegen")

    color = str(form.get("color", "#1f4e79"))
    if not COLOR_RE.match(color):
        errors.append("Farbe muss im Format #RRGGBB angegeben werden")

    currency = str(form.get("currency", settings.default_currency)).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        errors.append("Währung muss ein 3-stelliger ISO-Code sein")

    if errors:
        return errors

    product.name = name
    product.slug = slug
    product.description = str(form.get("description", "")).strip()
    product.venue = str(form.get("venue", "")).strip()
    product.price_cents = price_cents
    product.currency = currency
    product.validity_type = validity_type
    product.valid_from = valid_from
    product.valid_until = valid_until
    product.validity_days = validity_days if validity_type == ValidityType.DURATION else None
    product.stock = stock
    product.max_per_order = max_per_order
    product.multi_entry = form.get("multi_entry") == "on"
    product.wallet_style = wallet_style
    product.color = color
    product.active = form.get("active") == "on"
    return []


@protected.get("/products")
def product_list(request: Request, db: Session = Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.created_at.desc())).all()
    sold = {p.id: sold_quantity(db, p) for p in products}
    return render(request, "admin/products.html", products=products, sold=sold)


@protected.get("/products/new")
def product_new(request: Request, settings: Settings = Depends(get_settings)):
    return render(request, "admin/product_form.html", product=None, form=_product_form_values(Product(), settings), errors=[])


@protected.post("/products/new")
async def product_create(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    form = await request.form()
    check_csrf(request, form)
    product = Product()
    errors = _apply_product_form(db, product, form, settings)
    if errors:
        return render(request, "admin/product_form.html", status_code=400, product=None, form=dict(form), errors=errors)
    db.add(product)
    db.commit()
    flash(request, f"Produkt '{product.name}' angelegt", "success")
    return _redirect("/admin/products")


def _get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404)
    return product


@protected.get("/products/{product_id}")
def product_edit(product_id: int, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    product = _get_product(db, product_id)
    return render(request, "admin/product_form.html", product=product, form=_product_form_values(product, settings), errors=[])


@protected.post("/products/{product_id}")
async def product_update(
    product_id: int, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    product = _get_product(db, product_id)
    form = await request.form()
    check_csrf(request, form)
    errors = _apply_product_form(db, product, form, settings)
    if errors:
        db.rollback()
        return render(request, "admin/product_form.html", status_code=400, product=product, form=dict(form), errors=errors)
    db.commit()
    flash(request, "Produkt gespeichert", "success")
    return _redirect("/admin/products")


# --- Landing Pages ----------------------------------------------------------


def _page_form_values(page: LandingPage) -> dict:
    return {
        "title": page.title or "",
        "slug": page.slug or "",
        "headline": page.headline or "",
        "body": page.body or "",
        "hero_image_url": page.hero_image_url or "",
        "accent_color": page.accent_color or "#1f4e79",
        "published": bool(page.published),
        "payment_methods": page.payment_method_list if page.payment_methods else [],
        "product_ids": [p.id for p in page.products] if page.id else [],
    }


def _apply_page_form(db: Session, page: LandingPage, form, payments: PaymentRegistry) -> list[str]:
    errors: list[str] = []
    title = str(form.get("title", "")).strip()
    slug = str(form.get("slug", "")).strip() or slugify(title)
    if not title:
        errors.append("Titel fehlt")
    if not SLUG_RE.match(slug):
        errors.append("Slug darf nur Kleinbuchstaben, Ziffern und Bindestriche enthalten")
    elif db.scalar(select(LandingPage.id).where(LandingPage.slug == slug, LandingPage.id != (page.id or 0))):
        errors.append("Slug ist bereits vergeben")
    color = str(form.get("accent_color", "#1f4e79"))
    if not COLOR_RE.match(color):
        errors.append("Farbe muss im Format #RRGGBB angegeben werden")
    hero = str(form.get("hero_image_url", "")).strip()
    if hero and not hero.startswith(("https://", "http://", "/")):
        errors.append("Bild-URL muss mit https:// beginnen")

    methods = [m for m in form.getlist("payment_methods") if m in payments.providers]
    try:
        product_ids = [int(pid) for pid in form.getlist("product_ids")]
    except ValueError:
        errors.append("Ungültige Produktauswahl")
        product_ids = []
    if errors:
        return errors

    page.title = title
    page.slug = slug
    page.headline = str(form.get("headline", "")).strip()
    page.body = str(form.get("body", "")).strip()
    page.hero_image_url = hero
    page.accent_color = color
    page.published = form.get("published") == "on"
    page.payment_methods = ",".join(methods)
    page.products = list(db.scalars(select(Product).where(Product.id.in_(product_ids))).all()) if product_ids else []
    return []


def _page_form_ctx(db: Session, payments: PaymentRegistry) -> dict:
    return {
        "all_products": db.scalars(select(Product).order_by(Product.name)).all(),
        "all_providers": list(payments.providers.values()),
    }


@protected.get("/pages")
def page_list(request: Request, db: Session = Depends(get_db)):
    pages = db.scalars(select(LandingPage).order_by(LandingPage.created_at.desc())).all()
    return render(request, "admin/pages.html", pages=pages)


@protected.get("/pages/new")
def page_new(request: Request, db: Session = Depends(get_db), payments: PaymentRegistry = Depends(get_payments)):
    return render(
        request, "admin/page_form.html", page=None, form=_page_form_values(LandingPage()), errors=[], **_page_form_ctx(db, payments)
    )


def _form_echo(form) -> dict:
    data = dict(form)
    data["payment_methods"] = form.getlist("payment_methods")
    data["product_ids"] = [int(x) for x in form.getlist("product_ids") if str(x).isdigit()]
    return data


@protected.post("/pages/new")
async def page_create(request: Request, db: Session = Depends(get_db), payments: PaymentRegistry = Depends(get_payments)):
    form = await request.form()
    check_csrf(request, form)
    page = LandingPage()
    errors = _apply_page_form(db, page, form, payments)
    if errors:
        db.expunge_all()
        return render(
            request, "admin/page_form.html", status_code=400, page=None, form=_form_echo(form), errors=errors,
            **_page_form_ctx(db, payments),
        )
    db.add(page)
    db.commit()
    flash(request, f"Landing Page '{page.title}' angelegt", "success")
    return _redirect("/admin/pages")


def _get_page(db: Session, page_id: int) -> LandingPage:
    page = db.get(LandingPage, page_id)
    if page is None:
        raise HTTPException(404)
    return page


@protected.get("/pages/{page_id}")
def page_edit(page_id: int, request: Request, db: Session = Depends(get_db), payments: PaymentRegistry = Depends(get_payments)):
    page = _get_page(db, page_id)
    return render(
        request, "admin/page_form.html", page=page, form=_page_form_values(page), errors=[], **_page_form_ctx(db, payments)
    )


@protected.post("/pages/{page_id}")
async def page_update(
    page_id: int, request: Request, db: Session = Depends(get_db), payments: PaymentRegistry = Depends(get_payments)
):
    page = _get_page(db, page_id)
    form = await request.form()
    check_csrf(request, form)
    errors = _apply_page_form(db, page, form, payments)
    if errors:
        db.rollback()
        return render(
            request, "admin/page_form.html", status_code=400, page=page, form=_form_echo(form), errors=errors,
            **_page_form_ctx(db, payments),
        )
    db.commit()
    flash(request, "Landing Page gespeichert", "success")
    return _redirect("/admin/pages")


# --- Bestellungen -----------------------------------------------------------


@protected.get("/orders")
def order_list(request: Request, status: str = "", q: str = "", db: Session = Depends(get_db)):
    stmt = select(Order).order_by(Order.created_at.desc()).limit(200)
    if status:
        try:
            stmt = stmt.where(Order.status == OrderStatus(status))
        except ValueError:
            pass
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            Order.customer_email.ilike(like) | Order.customer_name.ilike(like) | Order.payment_reference.ilike(like)
        )
    orders = db.scalars(stmt).all()
    return render(request, "admin/orders.html", orders=orders, status=status, q=q, statuses=list(OrderStatus))


def _get_order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404)
    return order


@protected.get("/orders/{order_id}")
def order_detail(order_id: int, request: Request, db: Session = Depends(get_db)):
    return render(request, "admin/order_detail.html", order=_get_order(db, order_id))


@protected.post("/orders/{order_id}/mark-paid")
async def order_mark_paid(
    order_id: int, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    order = _get_order(db, order_id)
    form = await request.form()
    check_csrf(request, form)
    try:
        mark_paid(db, order, settings)
        flash(request, f"Bestellung {order.number} als bezahlt markiert – Tickets ausgestellt", "success")
    except OrderError as exc:
        flash(request, str(exc), "error")
    return _redirect(f"/admin/orders/{order.id}")


@protected.post("/orders/{order_id}/cancel")
async def order_cancel(order_id: int, request: Request, db: Session = Depends(get_db)):
    order = _get_order(db, order_id)
    form = await request.form()
    check_csrf(request, form)
    status = OrderStatus.REFUNDED if form.get("refunded") == "on" else OrderStatus.CANCELLED
    cancel_order(db, order, status)
    flash(request, f"Bestellung {order.number} storniert, Tickets ungültig", "success")
    return _redirect(f"/admin/orders/{order.id}")


# --- Einlass ----------------------------------------------------------------


@protected.get("/scan")
def scan_form(request: Request):
    return render(request, "admin/scan.html", result=None)


@protected.post("/scan")
async def scan(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    form = await request.form()
    check_csrf(request, form)
    result = check_in(db, str(form.get("code", "")), settings)
    return render(request, "admin/scan.html", result=result)


router.include_router(protected)
