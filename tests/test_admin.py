from sqlalchemy import select

from walletticketing.models import LandingPage, Order, OrderStatus, Product, TicketStatus, ValidityType
from walletticketing.services import LineRequest, create_order, mark_paid

from .conftest import csrf_from


def test_admin_requires_login(client):
    resp = client.get("/admin/products", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/login")


def test_wrong_password(client):
    html = client.get("/admin/login").text
    resp = client.post("/admin/login", data={"username": "admin", "password": "nope", "csrf_token": csrf_from(html)})
    assert resp.status_code == 401


def test_dashboard(admin_client):
    html = admin_client.get("/admin").text
    assert "Dashboard" in html and "Testzahlung" in html


def test_create_product_and_page(admin_client, db):
    html = admin_client.get("/admin/products/new").text
    resp = admin_client.post(
        "/admin/products/new",
        data={
            "csrf_token": csrf_from(html),
            "name": "Monatskarte Schwimmbad",
            "price": "39,90",
            "currency": "EUR",
            "validity_type": "duration",
            "validity_days": "30",
            "max_per_order": "5",
            "wallet_style": "auto",
            "color": "#0a7cbf",
            "multi_entry": "on",
            "active": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    product = db.scalar(select(Product))
    assert product.slug == "monatskarte-schwimmbad"
    assert product.price_cents == 3990
    assert product.validity_type == ValidityType.DURATION and product.multi_entry

    html = admin_client.get("/admin/pages/new").text
    resp = admin_client.post(
        "/admin/pages/new",
        data={
            "csrf_token": csrf_from(html),
            "title": "Freibad Saison",
            "headline": "Jetzt Karten sichern",
            "body": "Absatz 1\n\nAbsatz 2",
            "accent_color": "#0a7cbf",
            "product_ids": [str(product.id)],
            "payment_methods": ["invoice"],
            "published": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    page = db.scalar(select(LandingPage))
    assert page.slug == "freibad-saison" and page.payment_methods == "invoice"
    public = admin_client.get("/l/freibad-saison").text
    assert "Monatskarte Schwimmbad" in public and "<p>Absatz 2</p>" in public


def test_product_validation(admin_client):
    html = admin_client.get("/admin/products/new").text
    resp = admin_client.post(
        "/admin/products/new",
        data={"csrf_token": csrf_from(html), "name": "X", "price": "abc", "validity_type": "fixed", "color": "#000000"},
    )
    assert resp.status_code == 400
    assert "Ungültiger Preis" in resp.text and "festem Zeitraum" in resp.text


def test_edit_product(admin_client, db, make_product):
    p = make_product()
    html = admin_client.get(f"/admin/products/{p.id}").text
    assert 'value="Konzert"' in html
    form = {
        "csrf_token": csrf_from(html), "name": "Konzert XL", "slug": p.slug, "price": "30", "currency": "EUR",
        "validity_type": "fixed", "valid_from": "2027-01-10T19:00", "valid_until": "2027-01-10T23:00",
        "max_per_order": "4", "wallet_style": "auto", "color": "#123456", "active": "on",
    }
    assert admin_client.post(f"/admin/products/{p.id}", data=form, follow_redirects=False).status_code == 303
    db.refresh(p)
    assert p.name == "Konzert XL" and p.price_cents == 3000
    assert p.valid_from.isoformat() == "2027-01-10T18:00:00+00:00"  # 19:00 Berlin (MEZ)


def _paid_order(db, settings, product):
    order = create_order(
        db, settings, page=None, customer_name="Max", customer_email="max@example.com",
        lines=[LineRequest(product, 1)], payment_method="invoice",
    )
    db.commit()
    return mark_paid(db, order, settings)


def test_cancel_voids_tickets(admin_client, db, settings, make_product):
    order = _paid_order(db, settings, make_product())
    html = admin_client.get(f"/admin/orders/{order.id}").text
    admin_client.post(f"/admin/orders/{order.id}/cancel", data={"csrf_token": csrf_from(html), "refunded": "on"})
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.status == OrderStatus.REFUNDED
    assert all(t.status == TicketStatus.VOID for t in order.tickets)


def test_scan_single_and_multi_entry(admin_client, db, settings, make_product):
    from datetime import timedelta

    from walletticketing.db import utcnow

    now = utcnow()
    single = make_product(valid_from=now - timedelta(hours=1), valid_until=now + timedelta(hours=2))
    order = _paid_order(db, settings, single)
    code = order.tickets[0].code

    html = admin_client.get("/admin/scan").text
    token = csrf_from(html)
    assert "Einlass OK" in admin_client.post("/admin/scan", data={"csrf_token": token, "code": code}).text
    assert "bereits entwertet" in admin_client.post("/admin/scan", data={"csrf_token": token, "code": code.lower()}).text
    assert "nicht gefunden" in admin_client.post("/admin/scan", data={"csrf_token": token, "code": "NOPE"}).text

    multi = make_product(slug="multi", multi_entry=True, valid_from=now - timedelta(hours=1), valid_until=now + timedelta(hours=2))
    code2 = _paid_order(db, settings, multi).tickets[0].code
    for _ in range(2):
        assert "Einlass OK" in admin_client.post("/admin/scan", data={"csrf_token": token, "code": code2}).text

    future = make_product(slug="future")
    code3 = _paid_order(db, settings, future).tickets[0].code
    assert "noch nicht gültig" in admin_client.post("/admin/scan", data={"csrf_token": token, "code": code3}).text


def test_scanner_api(client, db, settings, make_product):
    from datetime import timedelta

    from walletticketing.db import utcnow

    now = utcnow()
    p = make_product(valid_from=now - timedelta(hours=1), valid_until=now + timedelta(hours=1))
    code = _paid_order(db, settings, p).tickets[0].code
    assert client.post("/api/check-in", json={"code": code}).status_code == 401
    headers = {"Authorization": "Bearer scan-key"}
    body = client.post("/api/check-in", json={"code": code}, headers=headers).json()
    assert body["ok"] is True and body["holder"] == "Max"
    body = client.post("/api/check-in", json={"code": code}, headers=headers).json()
    assert body["ok"] is False
