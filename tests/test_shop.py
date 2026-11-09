from datetime import timedelta

from sqlalchemy import select

from walletticketing.db import utcnow
from walletticketing.models import Order, OrderStatus, Ticket, ValidityType

from .conftest import csrf_from


def _checkout(client, slug, data):
    html = client.get(f"/l/{slug}").text
    return client.post(f"/l/{slug}/checkout", data={"csrf_token": csrf_from(html), **data}, follow_redirects=False)


def test_index_lists_published_pages(client, make_product, make_page):
    make_page([make_product()], title="Sichtbar")
    make_page([], slug="draft", title="Entwurf", published=False)
    html = client.get("/").text
    assert "Sichtbar" in html and "Entwurf" not in html
    assert client.get("/l/draft").status_code == 404


def test_landing_page_shows_products_and_payment_methods(client, make_product, make_page):
    p = make_product(name="Abendkasse")
    make_page([p])
    html = client.get("/l/sommer").text
    assert "Abendkasse" in html
    assert "25,00 €" in html
    assert 'value="demo"' in html and 'value="invoice"' in html


def test_demo_checkout_issues_tickets(client, db, make_product, make_page):
    p = make_product()
    make_page([p])
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "2", "name": "Max", "email": "max@example.com", "payment_method": "demo"})
    assert resp.status_code == 303
    pay_url = resp.headers["location"]
    assert pay_url.endswith("/demo")

    html = client.get(pay_url).text
    resp = client.post(pay_url, data={"csrf_token": csrf_from(html), "action": "pay"}, follow_redirects=False)
    order_url = resp.headers["location"]
    page = client.get(order_url).text
    assert "Bezahlt" in page
    assert page.count("Ticket mit QR-Code öffnen") == 2

    order = db.scalar(select(Order))
    assert order.status == OrderStatus.PAID and order.total_cents == 5000
    ticket = order.tickets[0]
    assert ticket.valid_from == p.valid_from
    t_page = client.get(f"/t/{ticket.access_token}").text
    assert ticket.code in t_page
    qr = client.get(f"/t/{ticket.access_token}/qr.svg")
    assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg")


def test_demo_payment_declined_cancels(client, db, make_product, make_page):
    p = make_product()
    make_page([p])
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "1", "name": "Max", "email": "max@example.com", "payment_method": "demo"})
    pay_url = resp.headers["location"]
    html = client.get(pay_url).text
    client.post(pay_url, data={"csrf_token": csrf_from(html), "action": "cancel"})
    order = db.scalar(select(Order))
    db.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.tickets == []


def test_invoice_order_pending_until_admin_confirms(admin_client, db, make_product, make_page):
    client = admin_client
    p = make_product()
    make_page([p])
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "1", "name": "Erika", "email": "erika@example.com", "payment_method": "invoice"})
    order_url = resp.headers["location"]
    page = client.get(order_url).text
    assert "IBAN" in page and "WT-000001" in page
    order = db.scalar(select(Order))
    assert order.status == OrderStatus.PENDING

    html = client.get(f"/admin/orders/{order.id}").text
    resp = client.post(f"/admin/orders/{order.id}/mark-paid", data={"csrf_token": csrf_from(html)}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    order = db.scalar(select(Order))
    assert order.status == OrderStatus.PAID and len(order.tickets) == 1


def test_day_of_choice_requires_date(client, db, make_product, make_page):
    p = make_product(validity_type=ValidityType.DAY_OF_CHOICE, valid_from=None, valid_until=None)
    make_page([p])
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "1", "name": "A", "email": "a@example.com", "payment_method": "demo"})
    assert resp.status_code == 400 and "Besuchstag" in resp.text

    visit = (utcnow() + timedelta(days=3)).date()
    resp = _checkout(
        client, "sommer",
        {f"qty_{p.id}": "1", f"date_{p.id}": visit.isoformat(), "name": "A", "email": "a@example.com", "payment_method": "demo"},
    )
    assert resp.status_code == 303
    order = db.scalar(select(Order))
    assert order.items[0].visit_date == visit


def test_stock_limit_enforced(client, make_product, make_page):
    p = make_product(stock=1)
    make_page([p])
    data = {f"qty_{p.id}": "1", "name": "A", "email": "a@example.com", "payment_method": "invoice"}
    assert _checkout(client, "sommer", data).status_code == 303
    resp = _checkout(client, "sommer", data)
    assert resp.status_code == 400


def test_validation_errors(client, make_product, make_page):
    p = make_product()
    make_page([p])
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "0", "name": "A", "email": "a@example.com", "payment_method": "demo"})
    assert resp.status_code == 400 and "mindestens ein Ticket" in resp.text
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "1", "name": "A", "email": "kaputt", "payment_method": "demo"})
    assert resp.status_code == 400
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "1", "name": "A", "email": "a@example.com", "payment_method": "stripe"})
    assert resp.status_code == 400  # Stripe nicht konfiguriert


def test_page_payment_method_restriction(client, make_product, make_page):
    p = make_product()
    make_page([p], payment_methods="invoice")
    html = client.get("/l/sommer").text
    assert 'value="invoice"' in html and 'value="demo"' not in html
    resp = _checkout(client, "sommer", {f"qty_{p.id}": "1", "name": "A", "email": "a@example.com", "payment_method": "demo"})
    assert resp.status_code == 400


def test_csrf_required(client, make_product, make_page):
    p = make_product()
    make_page([p])
    resp = client.post("/l/sommer/checkout", data={f"qty_{p.id}": "1", "payment_method": "demo"})
    assert resp.status_code == 400


def test_ended_event_not_on_sale(client, make_product, make_page):
    past = utcnow() - timedelta(days=2)
    p = make_product(name="Vorbei", valid_from=past, valid_until=past + timedelta(hours=2))
    make_page([p])
    assert "Vorbei" not in client.get("/l/sommer").text


def test_unknown_ticket_404(client):
    assert client.get("/t/doesnotexist").status_code == 404
    assert client.get("/order/doesnotexist").status_code == 404
