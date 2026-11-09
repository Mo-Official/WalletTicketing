from __future__ import annotations

import enum
import secrets
import string
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, UTCDateTime, utcnow


def _token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


_CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


def new_ticket_code() -> str:
    """Gut lesbarer Ticketcode (ohne 0/O/1/I), z.B. `K7QF-3MZX-PA9R-W2HD`."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


class ValidityType(str, enum.Enum):
    """Gültigkeitsraum eines Produkts."""

    FIXED = "fixed"  # fester Zeitraum (Event, Festival, Saison)
    DURATION = "duration"  # N Tage ab Kauf (Zeitkarte)
    DAY_OF_CHOICE = "day_of_choice"  # Kunde wählt einen Besuchstag


class WalletStyle(str, enum.Enum):
    """Darstellung in der Wallet. AUTO leitet sie aus dem Gültigkeitsraum ab."""

    AUTO = "auto"
    EVENT = "event"  # Apple eventTicket / Google EventTicket
    PASS = "pass"  # Apple generic / Google Generic (Zeit-/Saisonkarte)


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class TicketStatus(str, enum.Enum):
    VALID = "valid"
    VOID = "void"


landing_page_products = Table(
    "landing_page_products",
    Base.metadata,
    Column("landing_page_id", ForeignKey("landing_pages.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    venue: Mapped[str] = mapped_column(String(200), default="")
    price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    validity_type: Mapped[ValidityType] = mapped_column(Enum(ValidityType), default=ValidityType.FIXED)
    # FIXED: Beginn/Ende des Zeitraums; DAY_OF_CHOICE: optional buchbarer Zeitraum
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # DURATION: Anzahl Kalendertage inkl. Kauftag
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    multi_entry: Mapped[bool] = mapped_column(Boolean, default=False)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unbegrenzt
    max_per_order: Mapped[int] = mapped_column(Integer, default=10)
    wallet_style: Mapped[WalletStyle] = mapped_column(Enum(WalletStyle), default=WalletStyle.AUTO)
    color: Mapped[str] = mapped_column(String(7), default="#1f4e79")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    landing_pages: Mapped[list[LandingPage]] = relationship(
        secondary=landing_page_products, back_populates="products"
    )

    @property
    def price(self) -> float:
        return self.price_cents / 100


class LandingPage(Base):
    __tablename__ = "landing_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    headline: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    hero_image_url: Mapped[str] = mapped_column(String(500), default="")
    accent_color: Mapped[str] = mapped_column(String(7), default="#1f4e79")
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    # Komma-getrennte Liste erlaubter Zahlungsarten, leer = alle verfügbaren
    payment_methods: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    products: Mapped[list[Product]] = relationship(
        secondary=landing_page_products, back_populates="landing_pages", order_by="Product.name"
    )

    @property
    def payment_method_list(self) -> list[str]:
        return [m.strip() for m in self.payment_methods.split(",") if m.strip()]


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_token)
    landing_page_id: Mapped[int | None] = mapped_column(ForeignKey("landing_pages.id"), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(200))
    customer_email: Mapped[str] = mapped_column(String(320))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING, index=True)
    payment_method: Mapped[str] = mapped_column(String(40))
    payment_reference: Mapped[str] = mapped_column(String(200), default="", index=True)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    landing_page: Mapped[LandingPage | None] = relationship()
    items: Mapped[list[OrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")
    tickets: Mapped[list[Ticket]] = relationship(back_populates="order", cascade="all, delete-orphan")

    @property
    def number(self) -> str:
        return f"WT-{self.id:06d}"

    @property
    def total(self) -> float:
        return self.total_cents / 100


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    visit_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # bei DAY_OF_CHOICE

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()

    @property
    def line_total_cents(self) -> int:
        return self.unit_price_cents * self.quantity


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=new_ticket_code)
    access_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_token)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    holder_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[TicketStatus] = mapped_column(Enum(TicketStatus), default=TicketStatus.VALID)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime)
    check_in_count: Mapped[int] = mapped_column(Integer, default=0)
    last_check_in: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    order: Mapped[Order] = relationship(back_populates="tickets")
    product: Mapped[Product] = relationship()
