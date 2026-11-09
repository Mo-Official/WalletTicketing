from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..models import Ticket, WalletStyle
from ..validity import Phase, format_window, resolve_wallet_style, ticket_phase


class WalletNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class PassData:
    """Wallet-neutrale Beschreibung eines Tickets."""

    serial: str
    barcode: str
    title: str
    description: str
    organization: str
    holder: str
    venue: str
    valid_from: datetime
    valid_until: datetime
    validity_text: str
    style: WalletStyle
    phase: Phase
    color: str
    order_number: str
    ticket_url: str
    multi_entry: bool

    @property
    def voided(self) -> bool:
        return self.phase == Phase.VOID


def build_pass_data(ticket: Ticket, settings: Settings, now: datetime) -> PassData:
    product = ticket.product
    return PassData(
        serial=f"ticket-{ticket.id}",
        barcode=ticket.code,
        title=product.name,
        description=product.description,
        organization=settings.organization_name,
        holder=ticket.holder_name,
        venue=product.venue,
        valid_from=ticket.valid_from,
        valid_until=ticket.valid_until,
        validity_text=format_window(ticket.valid_from, ticket.valid_until, settings.tz),
        style=resolve_wallet_style(product, ticket.valid_from, ticket.valid_until, settings.tz),
        phase=ticket_phase(ticket, now),
        color=product.color or "#1f4e79",
        order_number=ticket.order.number,
        ticket_url=f"{settings.base_url}/t/{ticket.access_token}",
        multi_entry=product.multi_entry,
    )


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        return (31, 78, 121)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return (31, 78, 121)


def foreground_for(color: str) -> tuple[int, int, int]:
    r, g, b = hex_to_rgb(color)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return (0, 0, 0) if luminance > 160 else (255, 255, 255)
