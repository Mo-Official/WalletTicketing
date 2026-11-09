"""Geschäftslogik: Bestellungen anlegen, bezahlen, stornieren, Tickets prüfen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .db import utcnow
from .models import (
    LandingPage,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    Ticket,
    TicketStatus,
)
from .validity import Phase, ValidityError, compute_window, format_window, ticket_phase


class OrderError(ValueError):
    pass


@dataclass
class LineRequest:
    product: Product
    quantity: int
    visit_date: date | None = None


def release_stale_orders(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """Gibt Kontingente unbezahlter Online-Bestellungen nach Ablauf der Reservierungszeit frei."""
    now = now or utcnow()
    cutoff = now - timedelta(minutes=settings.pending_order_minutes)
    result = db.execute(
        update(Order)
        .where(
            Order.status == OrderStatus.PENDING,
            Order.payment_method != "invoice",
            Order.created_at < cutoff,
        )
        .values(status=OrderStatus.EXPIRED)
    )
    db.commit()
    return result.rowcount or 0


def sold_quantity(db: Session, product: Product) -> int:
    stmt = (
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(Order)
        .where(
            OrderItem.product_id == product.id,
            Order.status.in_([OrderStatus.PENDING, OrderStatus.PAID]),
        )
    )
    return int(db.scalar(stmt) or 0)


def remaining_stock(db: Session, product: Product) -> int | None:
    if product.stock is None:
        return None
    return max(product.stock - sold_quantity(db, product), 0)


def is_on_sale(product: Product, now: datetime) -> bool:
    if not product.active:
        return False
    # Feste Events und buchbare Zeiträume sind nach Ende nicht mehr verkäuflich
    return not (product.valid_until and product.valid_until <= now)


def create_order(
    db: Session,
    settings: Settings,
    *,
    page: LandingPage | None,
    customer_name: str,
    customer_email: str,
    lines: list[LineRequest],
    payment_method: str,
    now: datetime | None = None,
) -> Order:
    now = now or utcnow()
    customer_name = customer_name.strip()
    customer_email = customer_email.strip()
    if not customer_name:
        raise OrderError("Bitte geben Sie Ihren Namen an")
    if "@" not in customer_email or "." not in customer_email.split("@")[-1]:
        raise OrderError("Bitte geben Sie eine gültige E-Mail-Adresse an")

    lines = [line for line in lines if line.quantity > 0]
    if not lines:
        raise OrderError("Bitte wählen Sie mindestens ein Ticket")

    currencies = {line.product.currency for line in lines}
    if len(currencies) > 1:
        raise OrderError("Produkte mit unterschiedlichen Währungen können nicht zusammen bestellt werden")

    release_stale_orders(db, settings, now)

    order = Order(
        landing_page=page,
        customer_name=customer_name,
        customer_email=customer_email,
        payment_method=payment_method,
        currency=currencies.pop(),
        status=OrderStatus.PENDING,
        created_at=now,
    )
    requested: dict[int, int] = {}
    for line in lines:
        product = line.product
        if not is_on_sale(product, now):
            raise OrderError(f"'{product.name}' ist nicht mehr verfügbar")
        if line.quantity > product.max_per_order:
            raise OrderError(f"Maximal {product.max_per_order} × '{product.name}' pro Bestellung")
        try:
            compute_window(product, now, settings.tz, line.visit_date)  # Gültigkeit vorab prüfen
        except ValidityError as exc:
            raise OrderError(str(exc)) from exc
        requested[product.id] = requested.get(product.id, 0) + line.quantity
        remaining = remaining_stock(db, product)
        if remaining is not None and requested[product.id] > remaining:
            raise OrderError(f"Für '{product.name}' sind nur noch {remaining} Tickets verfügbar")
        order.items.append(
            OrderItem(
                product=product,
                quantity=line.quantity,
                unit_price_cents=product.price_cents,
                visit_date=line.visit_date,
            )
        )
    order.total_cents = sum(item.line_total_cents for item in order.items)
    db.add(order)
    db.flush()
    return order


def issue_tickets(order: Order, settings: Settings) -> list[Ticket]:
    if order.tickets:
        return order.tickets  # idempotent
    purchased_at = order.paid_at or utcnow()
    for item in order.items:
        window = compute_window(item.product, purchased_at, settings.tz, item.visit_date)
        for _ in range(item.quantity):
            order.tickets.append(
                Ticket(
                    product=item.product,
                    holder_name=order.customer_name,
                    valid_from=window.start,
                    valid_until=window.end,
                )
            )
    return order.tickets


def mark_paid(db: Session, order: Order, settings: Settings, reference: str = "") -> Order:
    """Markiert eine Bestellung als bezahlt und stellt die Tickets aus (idempotent)."""
    if order.status == OrderStatus.PAID:
        return order
    if order.status not in (OrderStatus.PENDING, OrderStatus.EXPIRED):
        raise OrderError(f"Bestellung im Status '{order.status.value}' kann nicht bezahlt werden")
    # Auch eine abgelaufene Reservierung wird akzeptiert, wenn das Geld eingegangen ist.
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    if reference:
        order.payment_reference = reference
    issue_tickets(order, settings)
    db.commit()
    return order


def cancel_order(db: Session, order: Order, status: OrderStatus = OrderStatus.CANCELLED) -> Order:
    if status not in (OrderStatus.CANCELLED, OrderStatus.REFUNDED, OrderStatus.EXPIRED):
        raise OrderError("Ungültiger Stornostatus")
    order.status = status
    for ticket in order.tickets:
        ticket.status = TicketStatus.VOID
    db.commit()
    return order


@dataclass
class CheckInResult:
    ok: bool
    message: str
    ticket: Ticket | None = None


def check_in(db: Session, code: str, settings: Settings, now: datetime | None = None) -> CheckInResult:
    now = now or utcnow()
    code = code.strip().upper()
    ticket = db.scalar(select(Ticket).where(Ticket.code == code))
    if ticket is None:
        return CheckInResult(False, "Ticket nicht gefunden")
    phase = ticket_phase(ticket, now)
    window = format_window(ticket.valid_from, ticket.valid_until, settings.tz)
    if phase == Phase.VOID:
        return CheckInResult(False, "Ticket wurde storniert", ticket)
    if phase == Phase.UPCOMING:
        return CheckInResult(False, f"Ticket ist noch nicht gültig ({window})", ticket)
    if phase == Phase.EXPIRED:
        return CheckInResult(False, f"Ticket ist abgelaufen ({window})", ticket)
    if ticket.check_in_count and not ticket.product.multi_entry:
        local = ticket.last_check_in.astimezone(settings.tz) if ticket.last_check_in else None
        when = f" am {local:%d.%m.%Y um %H:%M}" if local else ""
        return CheckInResult(False, f"Ticket wurde bereits entwertet{when}", ticket)
    ticket.check_in_count += 1
    ticket.last_check_in = now
    db.commit()
    return CheckInResult(True, f"Einlass OK – {ticket.product.name}", ticket)
