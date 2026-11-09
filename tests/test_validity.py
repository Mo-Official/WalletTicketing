from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from walletticketing.models import Product, Ticket, TicketStatus, ValidityType, WalletStyle
from walletticketing.validity import (
    Phase,
    ValidityError,
    compute_window,
    format_window,
    resolve_wallet_style,
    ticket_phase,
)

TZ = ZoneInfo("Europe/Berlin")


def product(**kw) -> Product:
    kw.setdefault("wallet_style", WalletStyle.AUTO)
    return Product(name="P", slug="p", price_cents=100, **kw)


def test_fixed_window():
    start = datetime(2026, 12, 1, 18, tzinfo=timezone.utc)
    p = product(validity_type=ValidityType.FIXED, valid_from=start, valid_until=start + timedelta(hours=5))
    w = compute_window(p, datetime(2026, 11, 1, tzinfo=timezone.utc), TZ)
    assert w.start == start and w.end == start + timedelta(hours=5)


def test_fixed_requires_dates():
    with pytest.raises(ValidityError):
        compute_window(product(validity_type=ValidityType.FIXED), datetime.now(timezone.utc), TZ)


def test_duration_until_end_of_last_local_day():
    p = product(validity_type=ValidityType.DURATION, validity_days=7)
    bought = datetime(2026, 11, 9, 22, 30, tzinfo=timezone.utc)  # 23:30 Berlin
    w = compute_window(p, bought, TZ)
    assert w.start == bought
    assert w.end == datetime(2026, 11, 16, 0, 0, tzinfo=TZ)  # 9.–15.11. (7 Tage inkl. Kauftag)


def test_day_of_choice():
    p = product(validity_type=ValidityType.DAY_OF_CHOICE)
    w = compute_window(p, datetime(2026, 11, 9, tzinfo=timezone.utc), TZ, date(2026, 11, 20))
    assert w.start == datetime(2026, 11, 20, tzinfo=TZ)
    assert w.end == datetime(2026, 11, 21, tzinfo=TZ)
    assert format_window(w.start, w.end, TZ) == "Fr, 20.11.2026"


def test_day_of_choice_rejects_past_and_out_of_range():
    now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    p = product(validity_type=ValidityType.DAY_OF_CHOICE)
    with pytest.raises(ValidityError):
        compute_window(p, now, TZ, date(2026, 11, 1))
    with pytest.raises(ValidityError):
        compute_window(p, now, TZ)
    p.valid_until = datetime(2026, 11, 30, tzinfo=TZ)
    with pytest.raises(ValidityError):
        compute_window(p, now, TZ, date(2026, 12, 5))


def test_wallet_style_depends_on_validity():
    s = datetime(2026, 11, 9, tzinfo=TZ)
    assert resolve_wallet_style(product(validity_type=ValidityType.FIXED), s, s + timedelta(hours=3), TZ) == WalletStyle.EVENT
    assert resolve_wallet_style(product(validity_type=ValidityType.DAY_OF_CHOICE), s, s + timedelta(days=1), TZ) == WalletStyle.EVENT
    assert resolve_wallet_style(product(validity_type=ValidityType.DURATION), s, s + timedelta(days=1), TZ) == WalletStyle.EVENT
    assert resolve_wallet_style(product(validity_type=ValidityType.DURATION), s, s + timedelta(days=30), TZ) == WalletStyle.PASS
    forced = product(validity_type=ValidityType.FIXED, wallet_style=WalletStyle.PASS)
    assert resolve_wallet_style(forced, s, s + timedelta(hours=3), TZ) == WalletStyle.PASS


def test_format_window_variants():
    assert format_window(datetime(2026, 11, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ), TZ) == "01.11.2026 – 30.11.2026"
    assert (
        format_window(datetime(2026, 11, 14, 19, tzinfo=TZ), datetime(2026, 11, 14, 23, tzinfo=TZ), TZ)
        == "Sa, 14.11.2026, 19:00 – 23:00 Uhr"
    )
    assert (
        format_window(datetime(2026, 11, 9, 13, 44, tzinfo=TZ), datetime(2026, 12, 9, tzinfo=TZ), TZ)
        == "09.11.2026 13:44 Uhr – 08.12.2026"
    )


def test_ticket_phase():
    now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    t = Ticket(valid_from=now + timedelta(hours=1), valid_until=now + timedelta(hours=2), status=TicketStatus.VALID)
    assert ticket_phase(t, now) == Phase.UPCOMING
    assert ticket_phase(t, now + timedelta(hours=1, minutes=30)) == Phase.ACTIVE
    assert ticket_phase(t, now + timedelta(hours=3)) == Phase.EXPIRED
    t.status = TicketStatus.VOID
    assert ticket_phase(t, now + timedelta(hours=1, minutes=30)) == Phase.VOID
