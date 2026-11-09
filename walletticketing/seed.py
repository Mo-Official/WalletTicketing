"""Legt Beispieldaten an: `python -m walletticketing.seed`."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import select

from .config import get_settings
from .db import Database, utcnow
from .models import LandingPage, Product, ValidityType


def seed() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    database.create_all()
    tz = settings.tz
    db = database.session_factory()
    if db.scalar(select(Product.id).limit(1)):
        print("Datenbank enthält bereits Produkte – nichts zu tun.")
        return

    event_day = (utcnow() + timedelta(days=21)).astimezone(tz).date()
    concert = Product(
        name="Open-Air Konzert",
        slug="open-air-konzert",
        description="Live-Musik unter freiem Himmel. Einlass ab 18:00 Uhr.",
        venue="Stadtpark, Hauptbühne",
        price_cents=3900,
        validity_type=ValidityType.FIXED,
        valid_from=datetime.combine(event_day, time(18, 0), tzinfo=tz),
        valid_until=datetime.combine(event_day, time(23, 30), tzinfo=tz),
        stock=500,
        color="#7a1f5c",
    )
    festival = Product(
        name="Festival-Pass (3 Tage)",
        slug="festival-pass",
        description="Zugang an allen drei Festivaltagen.",
        venue="Stadtpark",
        price_cents=8900,
        validity_type=ValidityType.FIXED,
        valid_from=datetime.combine(event_day - timedelta(days=1), time(12, 0), tzinfo=tz),
        valid_until=datetime.combine(event_day + timedelta(days=1), time(23, 59), tzinfo=tz),
        multi_entry=True,
        stock=300,
        color="#c2410c",
    )
    day_ticket = Product(
        name="Museum Tagesticket",
        slug="museum-tagesticket",
        description="Freie Wahl des Besuchstags.",
        venue="Stadtmuseum",
        price_cents=1200,
        validity_type=ValidityType.DAY_OF_CHOICE,
        multi_entry=True,
        color="#1f4e79",
    )
    monthly = Product(
        name="Monatskarte Freibad",
        slug="monatskarte-freibad",
        description="30 Tage freier Eintritt ab Kauf.",
        venue="Freibad Süd",
        price_cents=3990,
        validity_type=ValidityType.DURATION,
        validity_days=30,
        multi_entry=True,
        color="#0a7cbf",
    )
    db.add_all(
        [
            LandingPage(
                slug="sommerfestival",
                title="Sommerfestival",
                headline="Drei Tage Musik, Food und gute Laune",
                body="Sichern Sie sich jetzt Ihre Tickets!\n\nAlle Tickets landen direkt in Apple Wallet oder Google Wallet.",
                accent_color="#c2410c",
                published=True,
                products=[concert, festival],
            ),
            LandingPage(
                slug="freizeit",
                title="Museum & Freibad",
                headline="Tageskarten und Zeitkarten online kaufen",
                body="Tagesticket mit Wunschdatum oder Monatskarte – direkt aufs Smartphone.",
                accent_color="#0a7cbf",
                published=True,
                products=[day_ticket, monthly],
            ),
        ]
    )
    db.commit()
    print("Beispieldaten angelegt: /l/sommerfestival und /l/freizeit")


if __name__ == "__main__":
    seed()
