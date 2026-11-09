"""Berechnung des Gültigkeitsraums von Tickets und daraus abgeleiteter Darstellung."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import Product, Ticket, TicketStatus, ValidityType, WalletStyle


class ValidityError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValidityError("Ende des Gültigkeitszeitraums muss nach dem Beginn liegen")


def _local_midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)


def compute_window(
    product: Product,
    purchased_at: datetime,
    tz: ZoneInfo,
    visit_date: date | None = None,
) -> Window:
    """Ermittelt den Gültigkeitszeitraum eines Tickets für ein Produkt."""
    vt = product.validity_type

    if vt == ValidityType.FIXED:
        if not product.valid_from or not product.valid_until:
            raise ValidityError(f"Produkt '{product.name}' hat keinen festen Zeitraum hinterlegt")
        return Window(product.valid_from, product.valid_until)

    if vt == ValidityType.DURATION:
        days = product.validity_days or 1
        local_purchase = purchased_at.astimezone(tz)
        end = _local_midnight(local_purchase.date() + timedelta(days=days), tz)
        return Window(purchased_at, end)

    if vt == ValidityType.DAY_OF_CHOICE:
        if visit_date is None:
            raise ValidityError(f"Für '{product.name}' muss ein Besuchstag gewählt werden")
        today = purchased_at.astimezone(tz).date()
        if visit_date < today:
            raise ValidityError("Der Besuchstag darf nicht in der Vergangenheit liegen")
        start = _local_midnight(visit_date, tz)
        end = _local_midnight(visit_date + timedelta(days=1), tz)
        if product.valid_from and end <= product.valid_from:
            raise ValidityError("Der gewählte Tag liegt vor dem buchbaren Zeitraum")
        if product.valid_until and start >= product.valid_until:
            raise ValidityError("Der gewählte Tag liegt nach dem buchbaren Zeitraum")
        return Window(start, end)

    raise ValidityError(f"Unbekannter Gültigkeitstyp: {vt}")


class Phase(str, enum.Enum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    EXPIRED = "expired"
    VOID = "void"


def ticket_phase(ticket: Ticket, now: datetime) -> Phase:
    if ticket.status == TicketStatus.VOID:
        return Phase.VOID
    if now < ticket.valid_from:
        return Phase.UPCOMING
    if now >= ticket.valid_until:
        return Phase.EXPIRED
    return Phase.ACTIVE


PHASE_LABELS = {
    Phase.UPCOMING: "Noch nicht gültig",
    Phase.ACTIVE: "Gültig",
    Phase.EXPIRED: "Abgelaufen",
    Phase.VOID: "Storniert",
}


def resolve_wallet_style(product: Product, start: datetime, end: datetime, tz: ZoneInfo) -> WalletStyle:
    """Wählt die Wallet-Darstellung abhängig vom Gültigkeitsraum.

    * Feste Events und Tagestickets  -> Event-Ticket (Datum prominent, Sperrbildschirm-Hinweis)
    * Zeitkarten über mehrere Tage   -> Generischer Pass (Zeitraum "gültig bis" prominent)
    """
    if product.wallet_style != WalletStyle.AUTO:
        return product.wallet_style
    if product.validity_type in (ValidityType.FIXED, ValidityType.DAY_OF_CHOICE):
        return WalletStyle.EVENT
    if is_single_day(start, end, tz):
        return WalletStyle.EVENT
    return WalletStyle.PASS


def is_single_day(start: datetime, end: datetime, tz: ZoneInfo) -> bool:
    last_moment = end - timedelta(microseconds=1)
    return start.astimezone(tz).date() == last_moment.astimezone(tz).date()


def format_window(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """Menschlich lesbare Darstellung, z.B. `Sa, 14.11.2026` oder `01.11.2026 – 30.11.2026`."""
    ls, le = start.astimezone(tz), (end - timedelta(microseconds=1)).astimezone(tz)
    lend = end.astimezone(tz)
    full_day = ls.time() == time.min and lend.time() == time.min
    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    if ls.date() == le.date():
        day = f"{weekdays[ls.weekday()]}, {ls:%d.%m.%Y}"
        return day if full_day else f"{day}, {ls:%H:%M} – {lend:%H:%M} Uhr"
    if full_day:
        return f"{ls:%d.%m.%Y} – {le:%d.%m.%Y}"
    if lend.time() == time.min:
        # Zeitkarte: ab Kaufzeitpunkt bis einschließlich letztem Kalendertag
        return f"{ls:%d.%m.%Y %H:%M} Uhr – {le:%d.%m.%Y}"
    return f"{ls:%d.%m.%Y %H:%M} – {lend:%d.%m.%Y %H:%M} Uhr"
